"""RTC task lifecycle shapes matching the official app; no SDK media/join implementation.

Shapes follow the official app's requests. Credentials stay private.
"""
from dataclasses import dataclass,field
import secrets

CREATE_URI='/r/Adaptor/DeviceTaskI/createRtcTranslateTask'
UPDATE_URI='/r/Adaptor/DeviceTaskI/updateRtcTranslateTask'
HEARTBEAT_URI='/r/Adaptor/MoziConfAppI/heartbeat'
JOIN_URI='/r/Adaptor/MoziConfAppI/joinConf'
CLOSE_URI='/r/Adaptor/MoziConfAppI/closeConf'
INFO_URI='/r/Adaptor/MoziConfAppI/getJoinInfo'


def identifier(value):
    if isinstance(value,bool):raise ValueError('Invalid numeric identity')
    if isinstance(value,str) and value.isdecimal():value=int(value)
    if not isinstance(value,int) or not 0<value<2**63:raise ValueError('Invalid numeric identity')
    return value


def private_string(value,limit=2048):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('Invalid private RTC field')
    return value


def account(config):
    kind=config.get('account_type')
    if type(kind) is not int or kind<0:raise ValueError('Invalid account type')
    return {'accountType':kind,'accountId':private_string(str(config['account_id']),128),
            'deviceId':identifier(config['target_device_id'])}


def create_transcription(config,request_id=None):
    # The official app's no-translation branch: no arbitrary overrides, target languages or TTS.
    attributes={'Format':'opus','SampleRate':'16000','SpeakerCount':'1','Protocol':'5',
                'scene':'Transcription','sourceLanguage':'auto','targetLanguages':'[]','LanguageHints':'[]'}
    return [{**account(config),'requestId':request_id or secrets.token_hex(16),'attributes':attributes}]


@dataclass(frozen=True,repr=False)
class Receipt:
    task_id:int=field(repr=False)
    conference_id:str=field(repr=False)
    keepalive_seconds:int
    start_time:int
    token:str=field(repr=False)
    config:str=field(repr=False)
    access_info:dict=field(repr=False)
    device_identity_present:bool

    def summary(self):
        return {'task_id_present':True,'conference_id_present':True,
                'keepalive_seconds':self.keepalive_seconds,'start_time_present':self.start_time>0,
                'conference_token_present':bool(self.token),'conference_config_present':bool(self.config),
                'one_rtc_access_info_present':bool(self.access_info),'device_identity_present':self.device_identity_present}


def parse_receipt(config,response):
    if response.get('code')!=200:raise ValueError('RTC creation rejected')
    body=response.get('body');task=body.get('rtcTranslateTask') if isinstance(body,dict) else None
    if not isinstance(task,dict):raise ValueError('RTC task receipt missing')
    device=task.get('deviceTask')
    if device is not None:
        if not isinstance(device,dict):raise ValueError('RTC device receipt invalid')
        expected=account(config)
        if any(str(device.get(k))!=str(v) for k,v in expected.items()):raise ValueError('RTC receipt identity mismatch')
    conf=task.get('moziConfCreateResultDTO')
    if not isinstance(conf,dict):raise ValueError('RTC conference receipt missing')
    keepalive=task.get('keepAlivePeriodSecond',0)
    if type(keepalive) is not int or not 0<=keepalive<=3600:raise ValueError('Invalid RTC keepalive interval')
    start=conf.get('startTime',0)
    if type(start) is not int or start<0:raise ValueError('Invalid RTC start time')
    token=conf.get('token') or '';configuration=conf.get('config') or ''
    if not isinstance(token,str) or not isinstance(configuration,str) or len(token)>65536 or len(configuration)>1024*1024:
        raise ValueError('Invalid RTC token/configuration shape')
    access=conf.get('oneRTCAccessInfo') or {}
    if not isinstance(access,dict):raise ValueError('Invalid OneRTC access info')
    return Receipt(identifier(task.get('rtcTranslateTaskId')),private_string(conf.get('conferenceId')),
                   keepalive,start,token,configuration,access,device is not None)


def keepalive(config,receipt,request_id=None):
    return [{**account(config),'requestId':request_id or secrets.token_hex(16),
             'rtcTranslateTaskId':receipt.task_id,'sourceLanguage':'auto','targetLanguage':None,
             'scene':'Transcription','rtcTranslateTts':None,'attributes':{'keepAlive':'true'}}]


def join_conference(receipt,participant_id):
    # Preserves the official app's JoinConfRequest default (true), transmitted as pushToAssistant.
    # Multi-device joining prevents this receive-only client from replacing the A2 participant.
    return [{'conferenceId':receipt.conference_id,'participantId':private_string(participant_id,512),
             'pushToAssistant':True,'multiDeviceJoin':True,'attributes':{'enable_multi_device_join':'1'},
             'cameraStatus':2,'micStatus':1,'limitJoinUserNum':30}]


def conference_heartbeat(receipt,participant_id,status_sequence=0,last_update=0):
    if any(type(v) is not int or v<0 for v in (status_sequence,last_update)):raise ValueError('Invalid conference version')
    return [{'conferenceId':receipt.conference_id,'participantId':private_string(participant_id,512),
             'forceUpdate':False,'statusSeqNum':status_sequence,'lastUpdateTime':last_update}]


def close_conference(receipt):
    # The official app only sets requestId when an SDK capability gate is true; do not assume that gate.
    return [{'conferenceId':receipt.conference_id,'reason':'user_click_close_flash_a1_conf'}]


def conference_info(receipt):
    return [{'conferenceId':receipt.conference_id}]


def reply_status(response):
    body=response.get('body')
    code=body.get('code') if isinstance(body,dict) else None
    return {'transport_code':response.get('code') if type(response.get('code')) is int else None,
            'business_code':code if type(code) is int else None}


def closed_info(receipt,response):
    # Observed after successful close of our own created conference. Other 400s are not closure proof.
    if response.get('code')==400:
        body=response.get('body')
        return isinstance(body,dict) and str(body.get('code'))=='400' and body.get('reason') in ('Meeting ended','会议已结束')
    if response.get('code')!=200:return False
    body=response.get('body');info=body.get('confInfo') if isinstance(body,dict) else None
    if not isinstance(info,dict) or info.get('conferenceId')!=receipt.conference_id:return False
    if body.get('code') not in (None,0,200):return False
    end=info.get('endTime')
    return type(end) is int and end>0
