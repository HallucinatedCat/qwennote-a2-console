"""Recording task reads matching the official app and the recorder state derived from them.

Contracts: QueryRecordTaskRequestModel, DeviceTaskDTO, RecordTaskDTO.
"""
from dataclasses import dataclass,field
import secrets
from rtc_protocol import account,identifier,private_string

QUERY_URI='/r/Adaptor/DeviceTaskI/queryRecordTask'


def number(value,limit=8_640_000_000_000_000):
    if value is None:return None
    if type(value) is not int or not 0<=value<=limit:raise ValueError('Invalid cloud task number')
    return value


@dataclass(frozen=True)
class RecordTask:
    fid:int=field(repr=False)
    task_id:str=field(repr=False)
    state:int|None
    state_code:int|None
    real_time_asr:bool|None
    start_ms:int|None
    updated_ms:int|None
    end_ms:int|None

    def summary(self):
        return {'cloud_state':self.state,'cloud_state_code':self.state_code,'real_time_asr':self.real_time_asr,
                'cloud_started_at_ms':self.start_ms,'cloud_updated_at_ms':self.updated_ms,'cloud_ended_at_ms':self.end_ms,
                'physical_state_verified':False}

    def order(self):return (self.start_ms or 0,self.updated_ms or 0)


def blocks_start(summary):
    """A known unfinished/uncertain task vetoes a new recording; absence proves nothing."""
    if not summary or summary.get('available') is False:return False
    ended=summary.get('cloud_ended_at_ms')
    return not (type(summary.get('cloud_state')) is int and summary['cloud_state']==2
                and type(ended) is int and ended>0)


# DeviceTaskDTO.state as the official app maps it. 6 means the A2 is offline with its last status kept.
STATES={0:'recording',2:'idle',5:'paused',6:'offline'}


def device_state(online,fid,task):
    """Recorder state as the official app derives it: offline unless onlineStatus is 1,
    idle when the A2 reports no recording_fid, otherwise the state of that recording's task."""
    if not online:return 'offline'
    if fid is None:return 'idle'
    if task is None:return 'unknown'
    return STATES.get(task.state,'unknown')


def query(config,fid):
    return [{**account(config),'fid':identifier(fid),'requestId':secrets.token_hex(16)}]


def parse(config,value,expected_fid=None):
    if not isinstance(value,dict) or not isinstance(value.get('deviceTask'),dict):
        raise ValueError('Missing cloud recording task')
    task=value['deviceTask'];expected=account(config)
    if (not isinstance(task.get('accountId'),str) or task['accountId']!=expected['accountId'] or
        type(task.get('accountType')) is not int or task['accountType']!=expected['accountType'] or
        identifier(task.get('deviceId'))!=expected['deviceId'] or
        type(task.get('taskType')) is not int or task['taskType']!=0):
        raise ValueError('Cloud task target mismatch')
    attributes=task.get('attributes')
    if not isinstance(attributes,dict):raise ValueError('Missing cloud task attributes')
    fid=identifier(attributes.get('fid'))
    if expected_fid is not None and fid!=identifier(expected_fid):raise ValueError('Cloud task recording mismatch')
    realtime=value.get('realTimeAsr')
    if realtime is not None and type(realtime) is not bool:raise ValueError('Invalid realtime ASR flag')
    return RecordTask(fid,private_string(task.get('taskId'),256),number(task.get('state'),2**31-1),
                      number(task.get('stateCode'),2**31-1),realtime,number(task.get('startTime')),
                      number(task.get('updateTime')),number(task.get('endTime')))


def parse_reply(config,reply,fid):
    body=reply.get('body') if isinstance(reply,dict) else None
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(body,dict) or body.get('code') not in (None,0,200):
        raise ValueError('Cloud task read rejected')
    return parse(config,body.get('recordTask'),expected_fid=fid)


def from_event(config,event):
    if event.get('type')!=770000 or event.get('stream_id')!='dtiot.dinger':return None
    data=event.get('payload')
    envelope=data.get('deviceTaskEventDTO') if isinstance(data,dict) else None
    if not isinstance(envelope,dict) or type(envelope.get('taskType')) is not int or envelope['taskType']!=0:return None
    return parse(config,envelope.get('recordTask'))
