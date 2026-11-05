"""QwenWork alert/organization settings following the official app's controls."""
import hashlib
import hmac
import time
from public_errors import Refused

METHOD='agent_result_notify_type'
ORGANIZATION='qwen_work_org_id'
KEYS={METHOD,ORGANIZATION}
METHODS={'0':'Voice alert','1':'Vibration alert'}
URI='/r/Adaptor/DeviceSettingI/batchUpdateDeviceSetting'


def current(config,response):
    result={};priority={4:0,0:1,2:2}
    for row in response.get('body',{}).get('settingList',[]):
        if not isinstance(row,dict) or row.get('settingKey') not in KEYS:continue
        scope=row.get('belongingType');value=row.get('settingValue')
        if scope not in priority or not isinstance(value,str):continue
        if str(row.get('accountId'))!=str(config['account_id']) or row.get('accountType')!=config['account_type']:continue
        if scope==2 and str(row.get('belongingId'))!=str(config['target_device_id']):continue
        key=row['settingKey']
        if key not in result or priority[scope]>priority[result[key]['scope']]:result[key]={'value':value,'scope':scope}
    return result


def organizations(response):
    if response.get('code')!=200 or not isinstance(response.get('body'),dict):raise ValueError('Organization read rejected')
    rows=response['body'].get('organizations') or []
    if not isinstance(rows,list) or len(rows)>2000:raise ValueError('Unexpected organization response')
    result={'0':'Personal edition'} # The organization name is appended only after a successful lookup.
    for row in rows:
        if not isinstance(row,dict):continue
        value=row.get('dingOrgId')
        if isinstance(value,bool):continue
        identifier=str(value)
        if not identifier.isdecimal() or not 0<int(identifier)<2**63:continue
        name=row.get('orgName')
        if not isinstance(name,str) or not name.strip():continue
        result[identifier]=name[:200]
    return result


def handle(secret,identifier):
    return hmac.new(secret.encode(),identifier.encode(),hashlib.sha256).hexdigest()[:32]


def resolve(secret,selection,allowed):
    if not isinstance(selection,str) or len(selection)!=32:raise Refused('Choose a current organization option')
    matches=[key for key in allowed if hmac.compare_digest(handle(secret,key),selection)]
    if len(matches)!=1:raise Refused('Organization option is stale or unavailable')
    return matches[0]


def reconcile(rows,saved):
    if not saved:return
    for key,original in saved['original'].items():
        row=rows.get(key,{})
        if row.get('value') not in (original,saved['requested'][key]) or row.get('scope') not in (saved['original_scopes'][key],2):
            raise Refused('Notification setting changed elsewhere; reconcile before writing')


def prepare(rows,method,organization=None,saved=None,persistent_override=False):
    if METHOD not in rows or not isinstance(method,str) or method not in METHODS:raise Refused('Supported notification method required')
    if organization is not None and (not isinstance(organization,str) or not organization.isdecimal()):raise Refused('Invalid organization selection')
    reconcile(rows,saved)
    desired={METHOD:method}
    if organization is not None:desired[ORGANIZATION]=organization
    changed={k:v for k,v in desired.items() if rows.get(k,{}).get('value')!=v}
    if not changed:return {},saved
    if any(rows.get(k,{}).get('scope')!=2 for k in changed) and not persistent_override:
        raise Refused('Confirm persistent notification overrides')
    original=dict(saved['original']) if saved else {};scopes=dict(saved['original_scopes']) if saved else {};requested=dict(saved['requested']) if saved else {}
    for key,value in changed.items():
        if key not in original:original[key]=rows.get(key,{}).get('value');scopes[key]=rows.get(key,{}).get('scope')
        requested[key]=value
    journal={'original':original,'original_scopes':scopes,'requested':requested,
             'exact_restore_supported':all(scopes[k]==2 and isinstance(original[k],str) for k in original)}
    return changed,journal


def restoration(rows,saved,allowed=None):
    if not saved or not saved.get('exact_restore_supported'):raise Refused('Exact notification restoration unavailable')
    reconcile(rows,saved)
    if any(rows.get(k,{}).get('scope')!=2 for k in saved['original']):raise Refused('Original override scope changed')
    original=dict(saved['original'])
    if ORGANIZATION in original and (allowed is None or original[ORGANIZATION] not in allowed):raise Refused('Original organization no longer available')
    return original


def command(config,changes,request_id=None,restore=False):
    if not isinstance(changes,dict) or not changes or not set(changes)<=KEYS:raise ValueError('Notification fields required')
    for key,value in changes.items():
        if not isinstance(value,str) or not value or len(value)>128:raise ValueError('Invalid notification value')
        if key==METHOD and not restore and value not in METHODS:raise ValueError('Unsupported notification method')
        if key==ORGANIZATION and (not value.isdecimal() or int(value)>=2**63):raise ValueError('Invalid organization identifier')
    return [{'accountType':config['account_type'],'accountId':str(config['account_id']),'belongingType':2,
             'belongingId':str(config['target_device_id']),'settingRequestModelList':[{'settingKey':k,'settingValue':v} for k,v in changes.items()],
             'requestId':request_id if request_id is not None else int(time.time()*1000)}]


def descriptor(rows,allowed,secret,saved=None):
    org=rows.get(ORGANIZATION,{}).get('value');method=rows.get(METHOD,{}).get('value')
    return {'available':METHOD in rows,'method':method if method in METHODS else None,'method_choices':METHODS,
            'method_requires_override':rows.get(METHOD,{}).get('scope')!=2,
            'organization_requires_override':rows.get(ORGANIZATION,{}).get('scope')!=2,
            'organizations_loaded':allowed is not None,
            'organization_options':[{'handle':handle(secret,k),'label':v,'personal':k=='0'} for k,v in (allowed or {}).items()],
            'current_organization_handle':handle(secret,org) if allowed is not None and org in allowed else None,
            'current_organization_label':allowed.get(org,'Current organization not in available options') if allowed is not None and org is not None else 'No organization selected' if org is None else 'Load organizations to view the current selection',
            'pending_restore':bool(saved and saved.get('exact_restore_supported')),
            'persistent_override_saved':bool(saved and not saved.get('exact_restore_supported')),
            'verification':'App-modelled notification writer; device alert behavior unverified'}
