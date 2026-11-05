"""Application-path note scope reads and private/public changes."""
import hashlib
import hmac
from urllib.parse import urlsplit
import json
from note_favorites import valid_uuid

READ_URI='/r/Adaptor/MinutesEamI/getShareScopeV2'
WRITE_URI='/r/Adaptor/MinutesEamI/saveShareScope'
DETAIL_URI='/r/Adaptor/PortalMinutesI/minutesDetailV2'
SCOPES={0:'private',1:'organization',2:'public'}
TARGETS={'private':0,'public':2}
OPTIONAL={'orgOpenScope':('org_open_scope',int),'canSearch':('can_search',bool),
          'canRecommend':('can_recommend',bool),'requirePassword':('require_password',bool),
          'password':('password',str),'expireAt':('expire_at',int),'expireDays':('expire_days',int)}


def read_request(uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identifier')
    return [{'corpId':'','bizType':4,'resourceType':8,'resourceId':uuid,'onlineEdit':False,'sceneCode':0}]


def _ids(value):
    if value is None:return []
    if not isinstance(value,list) or len(value)>128 or any(not isinstance(x,str) or len(x)>256 for x in value):
        raise ValueError('Invalid share subresources')
    return list(value)


def _flag(value):
    if not isinstance(value,dict) or type(value.get('enable')) is not bool:return None
    return value['enable']


def parse(reply):
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):
        raise ValueError('Share scope read failed')
    body=reply['body']
    if 'code' in body and (type(body['code']) is not int or body['code'] not in (0,200)):
        raise ValueError('Share scope read failed')
    if type(body.get('scope')) is not int or body['scope'] not in SCOPES:raise ValueError('Unsupported share scope')
    result={'scope':body['scope'],'role_code':body.get('roleCode'),
            'role_subresource_ids':_ids(body.get('roleSubResourceIds')),
            'sharing_enabled':_flag(body.get('enable')),
            'can_change_owner':body.get('canChangeOwner') if type(body.get('canChangeOwner')) is bool else None,
            'options':{}}
    if result['role_code'] is not None and (not isinstance(result['role_code'],str) or len(result['role_code'])>256):
        raise ValueError('Invalid share role')
    for wire,(key,kind) in OPTIONAL.items():
        value=body.get(wire)
        if value is not None and type(value) is not kind:raise ValueError('Invalid share option')
        result[key]=value
    password_model=body.get('passwordResponseModel')
    result['password_enabled']=(password_model.get('enabled') if isinstance(password_model,dict) and
                                type(password_model.get('enabled')) is bool else result['require_password'])
    options=body.get('updateOptions',[])
    if not isinstance(options,list) or len(options)>8:raise ValueError('Invalid share options')
    for option in options:
        if not isinstance(option,dict) or type(option.get('scope')) is not int or option['scope'] not in SCOPES:
            raise ValueError('Invalid share option')
        scope=option['scope']
        if scope in result['options']:raise ValueError('Duplicate share option')
        role=option.get('defaultRoleCode')
        if role is not None and (not isinstance(role,str) or len(role)>256):raise ValueError('Invalid default share role')
        result['options'][scope]={'enabled':_flag(option.get('enable')),
                                  'default_role_code':role or None,
                                  'role_subresource_ids':_ids(option.get('defaultSelectedSubResourceIds'))}
    return result


def revision(secret,snapshot):
    if not isinstance(secret,str) or len(secret)<32:raise ValueError('Revision secret required')
    data=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return hmac.new(secret.encode(),data,hashlib.sha256).hexdigest()


def access(snapshot):
    return SCOPES[snapshot['scope']]


def public(secret,snapshot,pending=False):
    current=snapshot['scope']
    allowed=[name for name,scope in TARGETS.items()
             if scope==current or snapshot['options'].get(scope,{}).get('enabled') is True]
    return {'access':access(snapshot),'sharing_enabled':snapshot['sharing_enabled'],
            'password_protected':snapshot['password_enabled'],'expire_at':snapshot['expire_at'],
            'expire_days':snapshot['expire_days'],'can_search':snapshot['can_search'],
            'can_recommend':snapshot['can_recommend'],'allowed_access':allowed,
            'revision':revision(secret,snapshot),'pending':pending}


def valid_password(password):
    """The vendor password is a short fixed-length string of letters and digits, case-insensitive.
    The exact length is set at runtime in the vendor UI, so the cloud has the final say."""
    return isinstance(password,str) and 4<=len(password)<=16 and password.isascii() and password.isalnum()


def save_request(uuid,snapshot,target,password=None):
    """password None keeps the current password settings, '' clears them, otherwise sets one (public only)."""
    if not valid_uuid(uuid) or target not in TARGETS:raise ValueError('Invalid share target')
    if password not in (None,'') and (target!='public' or not valid_password(password)):raise ValueError('Invalid share password')
    scope=TARGETS[target]
    option=snapshot['options'].get(scope,{})
    if scope!=snapshot['scope'] and option.get('enabled') is not True:raise ValueError('Share target is disabled')
    role=option.get('default_role_code') or snapshot['role_code']
    if not isinstance(role,str) or not role:raise ValueError('Share role is unavailable')
    body={'corpId':'','resourceId':uuid,'resourceType':8,'bizType':4,'scope':scope,'roleCode':role,
          'roleSubResourceIds':list(option.get('role_subresource_ids',[]))}
    for wire,(key,kind) in OPTIONAL.items():body[wire]=snapshot[key]
    if password=='':
        body['requirePassword']=False;body['password']=None
    elif password is not None:
        body['requirePassword']=True;body['password']=password
    return [body]


def verified(original,observed,request):
    body=request[0]
    expected_roles={body['roleCode']}
    option=original['options'].get(body['scope'],original['options'].get(str(body['scope'])))
    if option is not None:expected_roles.add(option.get('default_role_code'))
    if observed['scope']!=body['scope'] or observed['role_code'] not in expected_roles:
        return False
    if observed['role_subresource_ids']!=body['roleSubResourceIds']:return False
    if observed['sharing_enabled']!=original['sharing_enabled']:return False
    same=lambda key,wire:(str(observed[key]).lower()==str(body[wire]).lower() if wire=='password' and body[wire] else observed[key]==body[wire])
    return all(same(key,wire) for wire,(key,kind) in OPTIONAL.items())


def resolved(current,pending):
    canonical=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return canonical(current)==canonical(pending['original']) or verified(pending['original'],current,pending['request'])


def acknowledged(reply):
    if not isinstance(reply,dict) or reply.get('code')!=200:return False
    body=reply.get('body')
    return body is None or isinstance(body,dict) and ('code' not in body or type(body['code']) is int and body['code'] in (0,200))

def share_url(reply,uuid):
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):raise ValueError('Note detail rejected')
    data=reply['body'].get('data')
    if not isinstance(data,dict) or data.get('uuid')!=uuid or type(data.get('taskId')) is not int:raise ValueError('Note detail mismatch')
    value=data.get('shareUrl')
    if value in (None,''):return None
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('Invalid share URL')
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None:raise ValueError('Invalid share URL')
    return value
