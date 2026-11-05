"""Account-level settings that are not device settings: smart slicing and voiceprint.

As the official app does it:
- Slicing: MoziCustomerSettingAppI.queryCustomerSetting / updateCustomerSetting with
  {bizModule:"smart_slice", settingKey:"enable_slice", settingValue:"1"|"0"}; the app shows it
  on unless the server says "0".
- Voiceprint: MoziVoicePrintAppI.getVoicePrintConfig {orgId}, toggleVoicePrint
  {openToggle, shouldClearVoicePrint:false}, clearVoicePrint {} (deletes the enrolled voiceprint).
  Enrolling a new voiceprint needs the A2 over Bluetooth and is not offered here.
"""
SLICE_READ_URI='/r/Adaptor/MoziCustomerSettingAppI/queryCustomerSetting'
SLICE_WRITE_URI='/r/Adaptor/MoziCustomerSettingAppI/updateCustomerSetting'
VOICEPRINT_URI='/r/Adaptor/MoziVoicePrintAppI/getVoicePrintConfig'
VOICEPRINT_TOGGLE_URI='/r/Adaptor/MoziVoicePrintAppI/toggleVoicePrint'
VOICEPRINT_CLEAR_URI='/r/Adaptor/MoziVoicePrintAppI/clearVoicePrint'
SLICE={'bizModule':'smart_slice','settingKey':'enable_slice'}


def acknowledged(reply):
    body=reply.get('body') if isinstance(reply,dict) else None
    return isinstance(reply,dict) and reply.get('code')==200 and (body is None or isinstance(body,dict) and body.get('code') in (None,0,200))


def slicing_read():return [dict(SLICE)]


def slicing_write(enabled):
    if type(enabled) is not bool:raise ValueError('Slicing must be on or off')
    return [{**SLICE,'settingValue':'1' if enabled else '0'}]


def slicing(reply):
    if not acknowledged(reply):raise ValueError('Slicing read failed')
    setting=(reply.get('body') or {}).get('customerSetting')
    value=setting.get('settingValue') if isinstance(setting,dict) else None
    return value!='0'  # the app's default is on


def voiceprint_read():return [{'orgId':0}]


def voiceprint_toggle(enabled):
    if type(enabled) is not bool:raise ValueError('Voiceprint must be on or off')
    return [{'openToggle':enabled,'shouldClearVoicePrint':False}]


def voiceprint(reply):
    if not acknowledged(reply):raise ValueError('Voiceprint read failed')
    body=reply.get('body') or {}
    flag=lambda key:body.get(key) if type(body.get(key)) is bool else None
    return {'allowed':flag('allowConfigVoicePrint'),'enabled':flag('enableVoicePrint'),
            'enrolled':flag('hasVoicePrintRecord'),'available_to_account':flag('hitGray')}
