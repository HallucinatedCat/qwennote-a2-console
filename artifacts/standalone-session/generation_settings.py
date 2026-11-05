"""Automatic-summary settings modelled on the official app; this module performs no network IO."""
import time
from public_errors import Refused

URI='/r/Adaptor/DeviceSettingI/batchUpdateDeviceSetting'
KEYS=('auto_generate_minutes','auto_generate_minutes_duration','auto_generate_minutes_duration_limit')
CHOICES={
 KEYS[0]:{'0':'Manual','1':'Automatic'},
 KEYS[1]:{'0':'No minimum','1':'1 minute','10':'10 minutes','30':'30 minutes'},
 KEYS[2]:{'0':'No maximum',**{str(60*n):f'{n} hour'+('s' if n!=1 else '') for n in range(1,9)}}
}
LABELS={KEYS[0]:'Summary generation',KEYS[1]:'Minimum recording duration',KEYS[2]:'Maximum recording duration'}


def validate(values,restore=False):
    if not isinstance(values,dict) or set(values)!=set(KEYS):raise Refused('All three generation settings are required')
    if any(not isinstance(v,str) or not v or len(v)>128 for v in values.values()):raise Refused('Invalid generation setting value')
    if not restore:
        if any(values[k] not in CHOICES[k] for k in KEYS):raise Refused('Unsupported generation timing choice')
        minimum=int(values[KEYS[1]]);maximum=int(values[KEYS[2]])
        if minimum and maximum and minimum>=maximum:raise Refused('Minimum must be less than maximum')
    return dict(values)


def command(config,values,request_id=None,restore=False):
    values=validate(values,restore)
    return [{'accountType':config['account_type'],'accountId':str(config['account_id']),
             'belongingType':2,'belongingId':str(config['target_device_id']),
             'settingRequestModelList':[{'settingKey':k,'settingValue':values[k]} for k in KEYS],
             'requestId':request_id if request_id is not None else int(time.time()*1000)}]


def descriptor(details,saved=None):
    available=all(k in details and isinstance(details[k].get('value'),str) for k in KEYS)
    inherited=[k for k in KEYS if k in details and details[k].get('scope')!=2]
    exact=(saved.get('exact_restore_supported',False) if saved else available and not inherited)
    return {'available':available,'keys':list(KEYS),'choices':CHOICES,'labels':LABELS,
            'values':{k:details[k]['value'] for k in KEYS if k in details},
            'inherited_keys':inherited,'requires_persistent_override_confirmation':bool(inherited),
            'exact_restore_supported':exact,'pending_restore':bool(saved and exact),
            'persistent_override_saved':bool(saved and not exact),
            'verification':'App-modelled batch writer; live effect unverified'}


def next_journal(details,values,saved=None,persistent_override=False):
    group=descriptor(details,saved)
    if not group['available']:raise Refused('Generation settings missing from current response')
    if group['inherited_keys'] and not persistent_override:raise Refused('Confirm persistent overrides for inherited generation fields')
    values=validate(values)
    if saved:
        for key in KEYS:
            if details[key].get('scope') not in (saved['original_scopes'][key],2) or details[key]['value'] not in (saved['original'][key],saved['requested'][key]):
                raise Refused('Generation setting changed elsewhere; reconcile before writing')
    return {'original':dict(saved['original']) if saved else dict(group['values']),
            'requested':values,'exact_restore_supported':group['exact_restore_supported'],
            'original_scopes':dict(saved['original_scopes']) if saved else {k:details[k]['scope'] for k in KEYS}}


def restore_values(details,saved):
    if not saved or not saved.get('exact_restore_supported'):raise Refused('Exact group restoration unavailable')
    if any(k not in details or details[k].get('scope')!=2 or details[k]['value'] not in (saved['original'][k],saved['requested'][k]) for k in KEYS):
        raise Refused('Generation settings changed elsewhere; automatic restoration refused')
    return validate(saved['original'],restore=True)
