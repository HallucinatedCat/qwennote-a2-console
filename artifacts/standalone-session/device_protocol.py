"""Command/push schemas; recording cloud controls are confirmed on the A2."""
import base64
import json
import secrets

RECORD_URI='/r/Adaptor/DeviceTaskI/sendDeviceTaskCommand'
SETTING_URI='/r/Adaptor/DeviceSettingI/updateDeviceSetting'
ACTIONS={'start','stop','pause','resume'}
# The official app registers distinct source/type pairs; 86001..86003 are document notifications.
TRANSCRIPT_STREAMS={770001:'dtiot.dinger',86000:'doc.transcribe'}
# 960010 is DeviceInfoChangeEventPushModel (handled by the official app): eventType
# onlineStatusChanged, devicePropertyChanged or deviceUnbound, with deviceId,
# onlineStatus and properties [{propertyKey, propertyValue, timestamp}].
DEVICE_INFO=960010
# 86001/86003 are note status notices (transcribe_completed, text_summary_generated, list refresh).
PUSH_STREAMS={770000:'dtiot.dinger',DEVICE_INFO:'lippi.device',86001:'doc.transcribe',86003:'doc.transcribe',**TRANSCRIPT_STREAMS}
SETTING_VALUES={
    'default_source_language':{'auto','zh','en','ja','ko','fr','de','es','ru'},
    'enable_incognito_mode':{'0','1'},
    'enable_incognito_transcribe':{'0','1'},
    'auto_generate_minutes':{'0','1'},
    'default_speaker_diarization':{'0','1'},
    'enable_record_location':{'0','1'},
    'enable_rtc_translate':{'0','1'},
}


def record_command(config,action,request_id=None):
    if action not in ACTIONS:raise ValueError('Unknown recording action')
    return [{'accountType':config['account_type'],'accountId':str(config['account_id']),
             'targetDeviceId':int(config['target_device_id']),'taskType':0,'action':action,
             'requestId':request_id or secrets.token_hex(16)}]


def setting_command(config,key,value,request_id=None,*,restore=False,allowed_values=None):
    if (key not in SETTING_VALUES and allowed_values is None) or not isinstance(value,str) or len(value)>8192:
        raise ValueError('Unsupported setting')
    if not restore and value not in (allowed_values if allowed_values is not None else SETTING_VALUES[key]):raise ValueError('Unsupported setting value')
    return [{'accountType':config['account_type'],'accountId':str(config['account_id']),
             'belongingType':2,'belongingId':str(config['target_device_id']),
             'settingKey':key,'settingValue':value,'requestId':request_id or secrets.token_hex(16)}]


def selected_device(config,response):
    devices=response.get('body',{}).get('deviceModel',[])
    matching=[d for d in devices if str(d.get('deviceId'))==str(config['target_device_id']) and d.get('devServId')==1446]
    if len(matching)!=1:raise ValueError('Selected A2 missing from bound devices')
    return matching[0]


def device_property(device,key):
    """A bound-device property. DevicePropertyModel keeps the key in propertyId and the value
    in propertyName (the official app reads firmware_version, device_battery and recording_fid this way)."""
    rows=device.get('deviceProperties')
    for row in rows if isinstance(rows,list) else []:
        if isinstance(row,dict) and row.get('propertyId')==key:
            value=row.get('propertyName')
            return value if isinstance(value,str) and len(value)<=256 and not any(ord(c)<32 for c in value) else None
    return None


def battery_percent(device):
    value=device_property(device,'device_battery')
    return int(value) if value is not None and value.isdecimal() and int(value)<=100 else None


def recording_fid(device):
    """The recording the A2 reports as current, or None. The app treats a missing or zero value as idle."""
    value=device_property(device,'recording_fid')
    return int(value) if value is not None and value.isdecimal() and 0<int(value)<2**63 else None


def device_info_change(config,event):
    """A 960010 change for the selected A2: {'battery_percent', 'wants_check'}, else None.
    Only an online change, an unbinding or a change of the current recording needs a device check."""
    data=event.get('payload')
    if event.get('type')!=DEVICE_INFO or not isinstance(data,dict):return None
    if str(data.get('deviceId'))!=str(config['target_device_id']):return None
    rows=[row for row in data.get('properties') or [] if isinstance(row,dict)] if isinstance(data.get('properties'),list) else []
    battery=None
    for row in rows:
        value=row.get('propertyValue')
        if row.get('propertyKey')=='device_battery' and isinstance(value,str) and value.isdecimal() and int(value)<=100:battery=int(value)
    wants_check=data.get('eventType') in ('onlineStatusChanged','deviceUnbound') or any(row.get('propertyKey')=='recording_fid' for row in rows)
    return {'battery_percent':battery,'wants_check':wants_check}


def public_device(device):
    # Never forward the vendor's deviceSecret, serial, MAC or raw properties.
    # onlineStatus 1 is online and 0 offline (the official app tests == 1).
    return {'model':'QwenNote A2','product':1446,'cloud_online_status':device.get('onlineStatus'),
            'battery_percent':battery_percent(device),'observed_via':'bound-device cloud response'}


def owner_device(device):
    # Owner-only response; never merge SN into public_device/status or forward secrets.
    if not isinstance(device,dict):raise ValueError('Device details unavailable')
    result={}
    firmware=device.get('firmwareVersion') or device_property(device,'firmware_version')
    for source,target,limit in [('deviceNick','name',128),('sn','sn',256),('firmwareVersion','firmware_version',128)]:
        value=firmware if target=='firmware_version' else device.get(source)
        if value is not None and (not isinstance(value,str) or len(value)>limit or any(ord(c)<32 for c in value)):
            raise ValueError('Invalid device detail')
        result[target]=value
    result['firmware_source']='bound-device cloud response' if result['firmware_version'] else None
    result['battery_percent']=battery_percent(device)
    return result


def writable_settings(config,response):
    return {i.get('settingKey') for i in response.get('body',{}).get('settingList',[])
            if i.get('belongingType')==2 and str(i.get('belongingId'))==str(config['target_device_id'])
            and str(i.get('accountId'))==str(config['account_id']) and i.get('accountType')==config['account_type']}


def decode_push(message):
    """Mirror the official app: /s/para or /s/synca JSON + base64 JSON objects."""
    if message.get('lwp') not in {'/s/para','/s/synca'}:return []
    body=message.get('body',{})
    if isinstance(body,str):body=json.loads(body)
    if not isinstance(body,dict):return []
    package=body.get('syncPushPackage',{})
    if not isinstance(package,dict):return []
    result=[]
    for row in package.get('data',[])[:512]:
        if not isinstance(row,dict):continue
        kind=row.get('objectType')
        if type(kind) is not int or kind not in PUSH_STREAMS:continue
        stream=PUSH_STREAMS[kind]
        # Retain the legacy Dinger envelope fallback; require an explicit stream for Flash.
        if row.get('streamId')!=stream and not (stream=='dtiot.dinger' and row.get('streamId') in (None,'')):continue
        data=row.get('data')
        if not isinstance(data,str) or len(data)>1024*1024:continue
        try:payload=json.loads(base64.b64decode(data,validate=True))
        except (ValueError,UnicodeError):continue
        if isinstance(payload,dict):result.append({'type':kind,'stream_id':stream,'sync_id':row.get('syncId'),'payload':payload})
    return result
