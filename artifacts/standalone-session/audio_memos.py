"""Native saved audio-memo reads. No media fetching or mission mutations.

Contract: DingerAiAssistantI / GetMissionListReqModel.
The native reader does not establish which device produced a memo.
"""
import json
from public_errors import Refused

LIST_URI='/r/Adaptor/DingerAiAssistantI/getMissionList'
MAX_TEXT=32000


def list_request(limit=20):
    if type(limit) is not int or not 1<=limit<=100:
        raise Refused('Memo limit must be an integer from 1 to 100')
    # Native initial-page contract; pagination is not inferred from a cursor field.
    return {'cursor':'','pageSize':limit,'direction':0}


def memo_text(value):
    if value is None:return ''
    if not isinstance(value,str) or len(value)>MAX_TEXT*4:
        raise ValueError('Invalid memo text')
    if not value.strip():return ''
    try:decoded=json.loads(value)
    except (ValueError,RecursionError):decoded=value
    if isinstance(decoded,dict):value=decoded.get('text','')
    elif decoded is None:value=''
    # Never stringify structured metadata into the text display.
    elif isinstance(decoded,list):value=''
    if not isinstance(value,str) or len(value)>MAX_TEXT:
        raise ValueError('Invalid memo text')
    return value


def _number(value):
    if value is None:return 0
    if type(value) is not int or not 0<=value<=9007199254740991:
        raise ValueError('Invalid memo timestamp or duration')
    return value


def parse_list(reply,limit=20,keep_ids=False):
    """keep_ids: the backend swaps mission ids for handles before anything reaches the page."""
    list_request(limit)
    if not isinstance(reply,dict) or reply.get('code')!=200:
        raise ValueError('Memo read rejected')
    body=reply.get('body')
    if not isinstance(body,dict) or str(body.get('code',0)) not in ('0','200'):
        raise ValueError('Memo read rejected')
    missions=body.get('missionList')
    if not isinstance(missions,list) or len(missions)>limit:
        raise ValueError('Invalid memo list')
    more=body.get('hasMore',False)
    if more is None:more=False
    if type(more) is not bool:raise ValueError('Invalid memo list continuation')
    rows=[];seen=set();filtered=0
    for mission in missions:
        if not isinstance(mission,dict):raise ValueError('Invalid memo row')
        kind=mission.get('type')
        if kind is not None and (type(kind) is not int or kind!=0):
            filtered+=1;continue
        identifier=mission.get('missionId')
        if not isinstance(identifier,str) or not identifier.strip() or len(identifier)>512:
            raise ValueError('Invalid memo identity')
        if identifier in seen:continue
        seen.add(identifier)
        text=memo_text(mission.get('content'))
        if not text.strip():text=memo_text(mission.get('input'))
        rows.append({**({'mission_id':identifier} if keep_ids else {}),'text':text,'created_at_ms':_number(mission.get('createTime')),
                     'duration_ms':_number(mission.get('durationMs'))})
    return {'items':rows,'has_more':more,'requested_limit':limit,'filtered_non_memos':filtered,
            'scope':'native account; device attribution unavailable','read_only':True,
            'automatic_forwarding':False}
