"""Recordings stored for the A2 in the cloud (DeviceFileI), including ones that are not notes yet.

Contracts as the official app uses them: queryFileList pages with a string cursor
starting at "0"; FileDTO carries fileId, fileName, duration (ms), fileSize, createTime (ms),
state (3 = recycle bin), minutes {state: 0/5 not transcribed, 4 transcribing, 2 done, 3 failed}
and attributes (fid, incognitoStatus 1 = incognito). queryFileDownloadInfo returns a short-lived
resourceUrl plus request headers once its state is 2; it does not check incognito.
createGenerateMinutesTask turns recordings into notes. deleteFiles takes state 3 (recycle bin) or 1
(permanent; the app also tells a connected recorder to delete its copy); recoverFiles restores.
"""
import secrets
from rtc_protocol import account,identifier


def owner(config):
    """accountType and accountId only, for models without a deviceId field."""
    fields=account(config);fields.pop('deviceId');return fields

LIST_URI='/r/Adaptor/DeviceFileI/queryFileList'
DOWNLOAD_URI='/r/Adaptor/DeviceFileI/queryFileDownloadInfo'
GENERATE_URI='/r/Adaptor/DeviceTaskI/createGenerateMinutesTask'
DELETE_URI='/r/Adaptor/DeviceFileI/deleteFiles'
RECOVER_URI='/r/Adaptor/DeviceFileI/recoverFiles'
RECYCLE,PERMANENT=3,1
FORMATS=('wav',)  # the A2 uploads WAV; ogg and mp3 are not available
NOTE_STATES={0:'not-transcribed',5:'not-transcribed',4:'transcribing',2:'transcribed',3:'failed'}
HEADER_NAMES={'authorization','x-oss-security-token','x-acs-security-token','range','referer','x-oss-date','date'}


def file_id(value):
    if isinstance(value,int) and not isinstance(value,bool) and 0<value<2**63:return str(value)
    if isinstance(value,str) and 0<len(value)<=128 and value.replace('-','').replace('_','').isalnum():return value
    raise ValueError('Invalid file identifier')


def list_request(config,cursor='0',page_size=50):
    return [{**account(config),'deviceId':identifier(config['target_device_id']),'cursor':cursor,'pageSize':page_size,
             'requestId':secrets.token_hex(16)}]


def _number(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


def parse_list(reply):
    """(rows, next_cursor, has_more). Rows keep fileId for the backend only."""
    body=reply.get('body') if isinstance(reply,dict) else None
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(body,dict):raise ValueError('Recording list read failed')
    if 'code' in body and body['code'] not in (0,200):raise ValueError('Recording list read failed')
    rows=[]
    for item in body.get('fileList') or []:
        if not isinstance(item,dict):continue
        try:fid=file_id(item.get('fileId'))
        except ValueError:continue
        minutes=item.get('minutes') if isinstance(item.get('minutes'),dict) else {}
        attributes=item.get('attributes') if isinstance(item.get('attributes'),dict) else {}
        name=item.get('fileName') if isinstance(item.get('fileName'),str) and len(item['fileName'])<=256 else None
        recording=attributes.get('fid')
        recording=int(recording) if isinstance(recording,(str,int)) and not isinstance(recording,bool) and str(recording).isdecimal() and 0<int(recording)<2**63 else None
        rows.append({'file_id':fid,'fid':recording,'group_id':_number(item.get('groupId')) or None,
                     'name':name,'duration_ms':_number(item.get('duration')),'bytes':_number(item.get('fileSize')),
                     'created_at_ms':_number(item.get('createTime')),'in_recycle_bin':item.get('state')==3,
                     'note_state':NOTE_STATES.get(minutes.get('state'),'not-transcribed' if not minutes else 'unknown'),
                     'incognito':str(attributes.get('incognitoStatus'))=='1',
                     'playable_from_cloud':item.get('canPlayFromServer') is True})
    cursor=body.get('nextCursor')
    cursor=str(cursor) if isinstance(cursor,(str,int)) and not isinstance(cursor,bool) and len(str(cursor))<=128 else None
    return rows,cursor,body.get('hasMore') is True


def download_request(config,fid,file_format='wav'):
    if file_format not in FORMATS:raise ValueError('Unsupported audio format')
    return [{**owner(config),'fileId':file_id(fid),'format':file_format,'requestId':secrets.token_hex(16)}]


class NoAudio(ValueError):
    """Ready (state 2) but no download info. Seen live for the A2's incognito recordings, whose
    original audio is deleted after transcription; the app also treats this reply as invalid."""


def parse_download(reply):
    """(url, headers) when ready, None while the cloud is still preparing the file."""
    body=reply.get('body') if isinstance(reply,dict) else None
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(body,dict):raise ValueError('Recording download read failed')
    if body.get('state')!=2:return None
    info=body.get('fileDownloadInfoDTO')
    if info is None:raise NoAudio('No audio file in the cloud')
    if not isinstance(info,dict) or not isinstance(info.get('resourceUrl'),str):raise ValueError('Recording download read failed')
    headers={}
    raw=info.get('headers') if isinstance(info.get('headers'),dict) else {}
    for name,value in raw.items():
        if isinstance(name,str) and name.lower() in HEADER_NAMES and isinstance(value,str) and len(value)<=4096 and not any(ord(c)<32 for c in value):
            headers[name]=value
    return info['resourceUrl'],headers


def shape(value,depth=4,key=''):
    """A reply's field names and value types for a failure journal. Only status numbers
    (code, state, statusCode) keep their value; strings, URLs, IDs and other numbers never do."""
    if isinstance(value,dict):
        return {str(k)[:64]:shape(v,depth-1,str(k)) for k,v in list(value.items())[:40]} if depth else 'dict'
    if isinstance(value,list):return [shape(value[0],depth-1)] if value and depth else 'list'
    if value is None or isinstance(value,bool):return value
    if isinstance(value,int) and key in ('code','state','statusCode'):return value
    return type(value).__name__


def generate_request(config,fids):
    return [{**account(config),'deviceId':identifier(config['target_device_id']),'fileIds':[file_id(f) for f in fids],
             'attributes':{},'requestId':secrets.token_hex(16)}]


def delete_request(config,fids,state=RECYCLE):
    if state not in (RECYCLE,PERMANENT):raise ValueError('Invalid delete state')
    return [{**owner(config),'fileIds':[file_id(f) for f in fids],'state':state,'requestId':secrets.token_hex(16)}]


def recover_request(config,fids):
    return [{**owner(config),'fileIds':[file_id(f) for f in fids],'requestId':secrets.token_hex(16)}]


def acknowledged(reply):
    body=reply.get('body') if isinstance(reply,dict) else None
    return isinstance(reply,dict) and reply.get('code')==200 and (body is None or isinstance(body,dict) and body.get('code') in (None,0,200))
