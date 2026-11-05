"""Further native application functions, as the official app uses them.

Speaker rename (PortalMinutesI.batchUpdateParagraphNickName: the app picks the paragraphs of a
speaker itself and sends their ids), note markers (MinutesNoteI, by note), recording markers
(DeviceNoteI, by fid; noteType 0 mark, 2 text), note search and lists (searchA1Minutes,
queryCollectedMinutes, fetchMinutesListByTag, getHomeTabList), recording groups
(DeviceFileI file groups), recordings from other or old devices (DeviceFileI.loadHistory),
memo edit and delete (DingerAiAssistantI), product information (OpenDeviceI.getProductConfig)
and the phones linked to the A2 (DeviceConnectionI.queryDeviceConnections, read only; the
request/update calls would take the A2 over from the user's phone and are not offered).
"""
import json
import secrets
import time
from rtc_protocol import identifier
from device_files import owner, file_id

SPEAKER_URI = '/r/Adaptor/PortalMinutesI/batchUpdateParagraphNickName'
NOTE_MARKERS_URI = '/r/Adaptor/MinutesNoteI/queryNoteList'
NOTE_MARKER_CREATE_URI = '/r/Adaptor/MinutesNoteI/createNote'
DEVICE_NOTES_URI = '/r/Adaptor/DeviceNoteI/queryNoteList'
DEVICE_NOTE_CREATE_URI = '/r/Adaptor/DeviceNoteI/createNote'
DEVICE_NOTE_UPDATE_URI = '/r/Adaptor/DeviceNoteI/updateNote'
DEVICE_NOTE_DELETE_URI = '/r/Adaptor/DeviceNoteI/deleteNote'
SEARCH_URI = '/r/Adaptor/PortalMinutesI/searchA1Minutes'
COLLECTED_URI = '/r/Adaptor/PortalMinutesI/queryCollectedMinutes'
BY_TAG_URI = '/r/Adaptor/PortalMinutesI/fetchMinutesListByTag'
TABS_URI = '/r/Adaptor/PortalMinutesI/getHomeTabList'
GROUPS_URI = '/r/Adaptor/DeviceFileI/queryFileGroupList'
GROUP_CREATE_URI = '/r/Adaptor/DeviceFileI/createFileGroup'
GROUP_UPDATE_URI = '/r/Adaptor/DeviceFileI/updateFileGroup'
GROUP_DELETE_URI = '/r/Adaptor/DeviceFileI/deleteFileGroups'
GROUP_MOVE_URI = '/r/Adaptor/DeviceFileI/batchUpdateFileToGroup'
HISTORY_URI = '/r/Adaptor/DeviceFileI/loadHistory'
MEMO_UPDATE_URI = '/r/Adaptor/DingerAiAssistantI/updateMission'
MEMO_DELETE_URI = '/r/Adaptor/DingerAiAssistantI/deleteMission'
PRODUCT_URI = '/r/Adaptor/OpenDeviceI/getProductConfig'
CONNECTIONS_URI = '/r/Adaptor/DeviceConnectionI/queryDeviceConnections'
MARKER_TYPES = {0: 'marker', 1: 'image', 2: 'text'}
CONNECTION_STATES = {2: 'allowed', 3: 'blocked', 4: 'connected'}


def body(reply):
    """The reply body of an acknowledged call, else ValueError."""
    payload = reply.get('body') if isinstance(reply, dict) else None
    if not isinstance(reply, dict) or reply.get('code') != 200:
        raise ValueError('Read rejected')
    if payload is None:
        return {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ValueError('Invalid reply') from None
    if not isinstance(payload, dict) or payload.get('code') not in (None, 0, 200) or payload.get('statusCode') not in (None, 0, 200):
        raise ValueError('Read rejected')
    return payload


def acknowledged(reply):
    try:
        body(reply)
        return True
    except ValueError:
        return False


def text(value, limit):
    return value if isinstance(value, str) and value.strip() and len(value) <= limit and '\x00' not in value else None


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# Speaker rename -------------------------------------------------------------------

def speaker_of(row):
    display = row.get('speakerDisplayModel') if isinstance(row.get('speakerDisplayModel'), dict) else {}
    return display.get('nickName') or row.get('nickName') or ''


def speaker_request(uuid, paragraphs, current, new):
    """paragraphs: transcript items (uuid, startTime) of the speaker named current."""
    ids = [{'paragraphId': row['uuid'], 'startTime': row['startTime']} for row in paragraphs if speaker_of(row) == current]
    if not ids:
        return None
    return [{'taskUuid': uuid, 'paragraphIDList': ids, 'nickName': new.strip(), 'uid': '-2',
             'globalChange': 'true', 'currentParagraph': ids[0]}]


# Markers ------------------------------------------------------------------------------

def note_markers(reply):
    rows = []
    for item in body(reply).get('noteList') or []:
        if not isinstance(item, dict) or item.get('type') not in MARKER_TYPES:
            continue
        at = number(item.get('relativeTimeMs'))
        if at is None and number(item.get('noteTime')) is not None and number(item.get('audioStartTime')) is not None:
            at = max(0, item['noteTime'] - item['audioStartTime'])
        rows.append({'kind': MARKER_TYPES[item['type']], 'time_ms': at,
                     'text': item.get('content') if item.get('type') == 2 and isinstance(item.get('content'), str) else None})
    return sorted(rows, key=lambda row: row['time_ms'] if row['time_ms'] is not None else -1)


def note_marker_request(uuid, time_ms):
    return [{'taskUuid': uuid, 'type': 0, 'relativeTimeMs': time_ms}]


def device_notes_request(config, fid, fid_file):
    return [{**owner(config), 'fid': identifier(fid), 'fileId': file_id(fid_file), 'requestId': secrets.token_hex(16)}]


def device_notes(reply):
    """(rows with private note ids)"""
    rows = []
    for item in body(reply).get('noteList') or []:
        if not isinstance(item, dict) or item.get('noteType') not in MARKER_TYPES or not isinstance(item.get('noteId'), str):
            continue
        rows.append({'note_id': item['noteId'], 'kind': MARKER_TYPES[item['noteType']], 'time_ms': number(item.get('relativeTime')),
                     'text': item.get('content') if item.get('noteType') == 2 and isinstance(item.get('content'), str) else None})
    return sorted(rows, key=lambda row: row['time_ms'] if row['time_ms'] is not None else -1)


def device_note_create(config, fid, time_ms, note_text=None):
    kind = 2 if note_text else 0
    return [{**owner(config), 'fid': identifier(fid), 'deviceId': identifier(config['target_device_id']),
             'noteTime': int(time.time() * 1000), 'noteType': kind, 'contentType': 0, 'content': note_text or None,
             'relativeTime': time_ms, 'requestId': secrets.token_hex(16)}]


def device_note_update(config, note_id, note_text):
    return [{**owner(config), 'noteId': note_id, 'noteType': 2, 'contentType': 0, 'content': note_text, 'requestId': secrets.token_hex(16)}]


def device_note_delete(config, note_id):
    return [{**owner(config), 'noteId': note_id, 'requestId': secrets.token_hex(16)}]


# Search and lists -----------------------------------------------------------------------

def search_request(query, offset=''):
    return [{'offset': offset, 'pageSize': 20, 'query': query}]


def collected_request(offset='0'):
    return [{'offset': offset, 'pageSize': 20, 'sourceConditionIdList': ['all']}]


def by_tag_request(tag_id, offset='0'):
    return [{'offset': offset, 'pageSize': 20, 'tagId': tag_id, 'sourceConditionIdList': ['all']}]


def note_list(reply):
    """(notes in the list_my_minutes shape the page already shows, next offset or None)"""
    data = body(reply)
    notes = []
    for item in data.get('itemList') or []:
        if not isinstance(item, dict) or not isinstance(item.get('uuid'), str):
            continue
        notes.append({'uuid': item['uuid'], 'title': item.get('title') if isinstance(item.get('title'), str) else '',
                      'gmtCreate': number(item.get('gmtCreate')), 'durationMillis': number(item.get('duration')),
                      'collected': item.get('collected') is True})
    offset = data.get('offset') if data.get('hasMore') is True and isinstance(data.get('offset'), str) else None
    return notes, offset


def tabs(reply):
    """User tags shown as list filters (tabType 2), as (tab id, name)."""
    data = body(reply)
    rows = []
    for tab in data.get('tabList') or []:
        if isinstance(tab, dict) and tab.get('tabType') == 2 and isinstance(tab.get('tabId'), (str, int)) and isinstance(tab.get('tabName'), str):
            rows.append((str(tab['tabId']), tab['tabName'][:100]))
    return rows


# Recording groups and other-device recordings ----------------------------------------------

def groups_request(config):
    return [{**owner(config), 'deviceId': identifier(config['target_device_id']), 'requestId': secrets.token_hex(16)}]


def groups(reply):
    """(groups with private ids and their full records, counts)"""
    data = body(reply)
    counts = data.get('groupFileCount') if isinstance(data.get('groupFileCount'), dict) else {}
    rows = []
    for group in data.get('groupList') or []:
        if not isinstance(group, dict) or number(group.get('groupId')) in (None, 0) or not text(group.get('groupName'), 200):
            continue
        rows.append({'group_id': group['groupId'], 'name': group['groupName'], 'files': number(counts.get(str(group['groupId']), counts.get(group['groupId']))),
                     'record': {key: group.get(key) for key in ('groupId', 'groupName', 'parentGroupId', 'keywords', 'syncKnowledgeConfig') if key in group}})
    return rows, {'total': number(data.get('totalFileCount')), 'ungrouped': number(data.get('noGroupFileCount')),
                  'recycle_bin': number(data.get('recycleFileCount'))}


def group_create(config, name):
    return [{**owner(config), 'groupName': name.strip(), 'parentGroupId': 0, 'keywords': [],
             'deviceId': identifier(config['target_device_id']), 'requestId': secrets.token_hex(16)}]


def group_rename(config, record, name):
    # updateFileGroup replaces the whole group, so every known field is sent back unchanged.
    return [{**owner(config), **{k: v for k, v in record.items() if k != 'groupName'}, 'groupName': name.strip(), 'requestId': secrets.token_hex(16)}]


def group_delete(config, group_id):
    return [{**owner(config), 'groupIds': [group_id], 'requestId': secrets.token_hex(16)}]


def group_move(config, fids, group_id):
    return [{**owner(config), 'fileIds': [file_id(f) for f in fids], 'groupId': group_id, 'requestId': secrets.token_hex(16)}]


def history_request(config, cursor=None):
    return [{**owner(config), 'requestId': secrets.token_hex(16), 'pageSize': 50, 'cursor': cursor, 'deviceId': None,
             'excludeDeviceId': [identifier(config['target_device_id'])], 'excludeMinutes': False}]


# Memos, product, linked phones --------------------------------------------------------------

def memo_update(mission_id, content):
    return [{'missionId': mission_id, 'content': content}]


def memo_delete(mission_id):
    return [{'missionIds': [mission_id]}]


def memo_deleted(reply):
    count = body(reply).get('deletedCount')
    return isinstance(count, int) and count >= 0


def product_request(serv_id):
    return [{'devServId': serv_id}]


def product(reply):
    data = body(reply)
    model = data.get('deviceConfigModel') if isinstance(data.get('deviceConfigModel'), dict) else data
    return {key: model.get(key) for key in ('productName', 'deviceType', 'connectType', 'bindType', 'oneDevice') if isinstance(model.get(key), (str, int, bool))}


def connections_request(config):
    return [{**owner(config), 'deviceId': identifier(config['target_device_id']), 'scene': 'device_setting', 'requestId': secrets.token_hex(16)}]


def connections(reply):
    rows = []
    for item in body(reply).get('connectionList') or []:
        if isinstance(item, dict):
            rows.append({'name': text(item.get('appDeviceName'), 100) or None, 'state': CONNECTION_STATES.get(item.get('state'), 'unknown'),
                         'app_type': item.get('appType') if isinstance(item.get('appType'), (str, int)) else None})
    return rows
