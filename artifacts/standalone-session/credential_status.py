"""Credential health for the UI: times, counts and booleans only. Stdlib only.

Never returns a token, secret, identifier, endpoint or scope value that came from
a vendor. Reads are bounded, never follow links and never take a lock, so this
cannot interfere with the process that owns a session (the service, or rtc_probe on the
validation lineage). Writes nothing.
"""
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

MAX_FILE=128*1024
RENEW_WINDOW=120        # The service renews 60 s before expiry; rtc_probe needs 120 s left.
READ_SCOPE='qwennote:read'


def protection(path):
    """{'exists','protected','problem'} for a credential file and its directory."""
    path=Path(path)
    try:info=path.lstat()
    except FileNotFoundError:return {'exists':False,'protected':None,'problem':None}
    except OSError:return {'exists':True,'protected':False,'problem':'unreadable'}
    if stat.S_ISLNK(info.st_mode):return {'exists':True,'protected':False,'problem':'symlink'}
    if not stat.S_ISREG(info.st_mode):return {'exists':True,'protected':False,'problem':'not_a_file'}
    if info.st_mode&0o077:return {'exists':True,'protected':False,'problem':'file_mode'}
    if hasattr(os,'getuid') and info.st_uid!=os.getuid():return {'exists':True,'protected':False,'problem':'owner'}
    try:parent=path.parent.lstat()
    except OSError:return {'exists':True,'protected':False,'problem':'unreadable'}
    if stat.S_ISLNK(parent.st_mode) or parent.st_mode&0o077:return {'exists':True,'protected':False,'problem':'directory_mode'}
    return {'exists':True,'protected':True,'problem':None}


def read_json(path):
    """Bounded read that refuses links. Returns (data, problem)."""
    try:fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    except FileNotFoundError:return None,'missing'
    except OSError:return None,'unreadable'
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_FILE:return None,'invalid'
        raw=os.read(fd,MAX_FILE+1)
    finally:os.close(fd)
    try:data=json.loads(raw)
    except (ValueError,UnicodeError):return None,'invalid'
    return (data,None) if isinstance(data,dict) else (None,'invalid')


def modified(path):
    try:return Path(path).lstat().st_mtime
    except OSError:return None


def number(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) else None


def expiry_state(expires_at,now,window=RENEW_WINDOW):
    if expires_at is None:return 'unknown'
    left=expires_at-now
    return 'expired' if left<=0 else 'expiring' if left<=window else 'valid'


def native(path,now,*,config=None,renewals=None,kind='native_service'):
    """A native session file. `config` (the service's in-memory copy) wins over disk."""
    guard=protection(path)
    item={'id':kind,'protected':guard['protected'],'problem':guard['problem'],'updated_at':modified(path)}
    data=config
    if data is None:
        if not guard['exists']:return {**item,'state':'missing'}
        data,problem=read_json(path)
        if data is None:return {**item,'state':'invalid' if problem!='missing' else 'missing','problem':item['problem'] or problem}
    expires_at=number(data.get('expires_at'))
    has_refresh=isinstance(data.get('refresh_token'),str) and bool(data['refresh_token'])
    signing=all(isinstance(data.get(k),str) and data[k] for k in ('native_client_id','native_client_secret','native_auth_origin'))
    state=expiry_state(expires_at,now)
    if state=='expired' and has_refresh and signing:state='renewable'
    item.update(state=state,expires_at=expires_at,refresh_token=has_refresh,renewal_configured=signing)
    if renewals is not None:item['renewals']=dict(renewals)
    if guard['protected'] is False:item['state']='unprotected'
    return item


def mcp(root,now):
    if root is None:return {'id':'mcp_grant','state':'not_configured','protected':None,'problem':None,'updated_at':None}
    path=Path(root)/'.private'/'tokens.json'
    guard=protection(path)
    item={'id':'mcp_grant','protected':guard['protected'],'problem':guard['problem'],'updated_at':modified(path)}
    if not guard['exists']:return {**item,'state':'not_authorized'}
    data,problem=read_json(path)
    tokens=data.get('tokens') if data else None
    if not isinstance(tokens,dict):return {**item,'state':'invalid','problem':item['problem'] or problem or 'invalid'}
    expires_at=number(data.get('expires_at'))
    has_refresh=isinstance(tokens.get('refresh_token'),str) and bool(tokens['refresh_token'])
    scope=tokens.get('scope')
    scopes=scope.split() if isinstance(scope,str) else []
    # Report only whether the grant is exactly read-only; never echo server-supplied scope text.
    read_only=bool(scopes) and all(re.fullmatch(r'[a-z]+:read',s) for s in scopes)
    state=expiry_state(expires_at,now,window=60)
    if state in ('expired','expiring') and has_refresh:state='renews_on_use'
    item.update(state=state,expires_at=expires_at,refresh_token=has_refresh,read_only=read_only,
                exact_read_scope=scopes==[READ_SCOPE])
    if guard['protected'] is False:item['state']='unprotected'
    return item


def assistant(path):
    guard=protection(path)
    item={'id':'assistant','protected':guard['protected'],'problem':guard['problem'],'updated_at':modified(path)}
    if not guard['exists']:return {**item,'state':'not_configured'}
    if guard['protected'] is False:return {**item,'state':'unprotected'}
    data,problem=read_json(path)
    if data is None or not isinstance(data.get('url'),str) or not isinstance(data.get('model'),str):
        return {**item,'state':'invalid','problem':problem or 'invalid'}
    url=urlsplit(data['url'])
    secure=url.scheme=='https' or (url.scheme=='http' and url.hostname in {'127.0.0.1','localhost','::1'})
    return {**item,'state':'configured' if secure else 'invalid','secure_endpoint':secure,
            'api_key':isinstance(data.get('api_key'),str) and bool(data['api_key']),
            'system_prompt':isinstance(data.get('system_prompt'),str) and bool(data['system_prompt'].strip())}


def handoff(directory):
    """The historical login handoff: stale after the first rotation, never to be imported again.
    Only the directory and its file modes are inspected; no file is opened."""
    directory=Path(directory)
    item={'id':'login_handoff','protected':None,'problem':None,'updated_at':modified(directory)}
    try:info=directory.lstat()
    except FileNotFoundError:return {**item,'state':'absent'}
    except OSError:return {**item,'state':'stale','protected':False,'problem':'unreadable'}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):return {**item,'state':'stale','protected':False,'problem':'symlink'}
    files=[f for f in directory.iterdir() if f.is_file() or f.is_symlink()]
    if not files:return {**item,'state':'absent'}
    problems=[protection(f)['problem'] for f in files]
    bad=next((x for x in problems if x),None) or ('directory_mode' if info.st_mode&0o077 else None)
    return {**item,'state':'stale','protected':bad is None,'problem':bad,'files':len(files),
            'updated_at':max(filter(None,(modified(f) for f in files)),default=item['updated_at'])}


def collect(state,mcp_root=None,*,config=None,renewals=None,now=None):
    """Everything the Dev tools credential panel shows, in display order."""
    now=time.time() if now is None else now
    state=Path(state)
    items=[native(state/'native-session.json',now,config=config,renewals=renewals),
           native(state/'bootstrap-validation'/'native-session.json',now,kind='native_validation'),
           mcp(mcp_root,now),
           assistant(state/'assistant.json'),
           handoff(state/'login-handoff')]
    for item in items:
        if item.get('expires_at') is not None:item['expires_in']=round(item['expires_at']-now)
    return {'checked_at':now,'items':items}
