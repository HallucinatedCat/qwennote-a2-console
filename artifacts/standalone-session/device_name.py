"""Application-path A2 device-name contract and privacy-safe revisions."""
import hashlib
import hmac
import json

URI='/r/Adaptor/DeviceI/updateDeviceNick'
MAX_UNITS=32


def weight(value):
    if not isinstance(value,str):raise ValueError('Device name must be text')
    # The application iterates code points and charges 2 when the first UTF-16 unit is >255.
    return sum(2 if ord(char)>255 else 1 for char in value)


def normalize(value):
    if weight(value)>MAX_UNITS:raise ValueError('Device name exceeds 32 application units')
    name=value.strip()
    if not name or any(ord(char)<32 or ord(char)==127 for char in name):
        raise ValueError('Device name is empty or contains control characters')
    return name


def identity(config,record):
    """Use binding-config account fields; raw bound-device responses may omit them."""
    if not isinstance(config,dict) or not isinstance(record,dict):raise ValueError('Device identity unavailable')
    account_type=config.get('account_type');account_id=config.get('account_id');device_id=config.get('target_device_id')
    if type(account_type) is not int or isinstance(device_id,bool):raise ValueError('Invalid account or device identity')
    try:device_number=int(device_id)
    except (TypeError,ValueError):raise ValueError('Invalid device identity') from None
    if device_number<=0 or str(device_number)!=str(device_id):raise ValueError('Invalid device identity')
    if not isinstance(account_id,(str,int)) or isinstance(account_id,bool) or str(account_id) in ('','null','0'):
        raise ValueError('Invalid account identity')
    if record.get('devServId')!=1446 or str(record.get('deviceId'))!=str(device_number):
        raise ValueError('Selected device identity mismatch')
    if 'accountType' in record and record['accountType']!=account_type:raise ValueError('Device account mismatch')
    if 'accountId' in record and str(record['accountId'])!=str(account_id):raise ValueError('Device account mismatch')
    return {'dev_serv_id':1446,'device_id':str(device_number),'account_type':account_type,'account_id':str(account_id)}


def same_identity(left,right):
    return isinstance(left,dict) and isinstance(right,dict) and left==right and set(left)=={'dev_serv_id','device_id','account_type','account_id'}


def revision(secret,identity_value,name):
    if not isinstance(secret,str) or len(secret)<32 or not isinstance(name,str):raise ValueError('Revision input unavailable')
    data=json.dumps({'identity':identity_value,'name':name},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return hmac.new(secret.encode(),data,hashlib.sha256).hexdigest()


def request(config,record,name):
    nick=normalize(name);selected=identity(config,record)
    return [{'devServId':1446,'deviceId':int(selected['device_id']),'deviceNick':nick,
             'accountType':selected['account_type'],'accountId':selected['account_id']}]


def acknowledged(reply):
    if not isinstance(reply,dict) or type(reply.get('code')) is not int or reply['code']!=200:return False
    body=reply.get('body')
    if body is None:return True
    if not isinstance(body,dict) or body.get('success') is False:return False
    return 'code' not in body or type(body['code']) is int and body['code'] in (0,200)
