#!/usr/bin/env python3
"""Native silent renewal using locally provisioned client configuration.

No signing constants or credentials are included. Fresh SMS login is not provided.
The caller must serialize access to the protected session store across processes.
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from lwp_client import validate_session, private_write

ORIGINS={'https://minutes.qwennote.cn','https://minutes.qwennote.ai'}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def client_token(config,now=None):
    now=int(time.time() if now is None else now)
    client_id=config.get('native_client_id')
    secret=config.get('native_client_secret')
    if not isinstance(client_id,str) or not client_id or not isinstance(secret,str) or not secret:
        raise ValueError('Native signing configuration required')
    header=b64(b'{"alg":"HS256","typ":"JWT"}')
    payload=b64(json.dumps({'clientId':client_id,'iat':now,'exp':now+3600},separators=(',',':')).encode())
    unsigned=header+'.'+payload
    return unsigned+'.'+b64(hmac.new(secret.encode(),unsigned.encode(),hashlib.sha256).digest())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Authentication redirect rejected')


def parse_renewal(config,response):
    if not isinstance(response,dict) or response.get('success') is not True:
        raise ValueError('Native renewal rejected')
    data=response.get('data')
    if not isinstance(data,dict) or str(data.get('accountId'))!=str(config['account_id']):
        raise ValueError('Renewal account mismatch')
    for key in ('accessToken','refreshToken'):
        if not isinstance(data.get(key),str) or not data[key]:raise ValueError('Incomplete renewal')
    expiry=data.get('accessTokenExpireAt')
    if not isinstance(expiry,(int,float)) or isinstance(expiry,bool):raise ValueError('Renewal expiry missing')
    # Epoch milliseconds: the app stores it unscaled as accessTokenExpireAtMs.
    updated={**config,'access_token':data['accessToken'],'refresh_token':data['refreshToken'],'expires_at':expiry/1000}
    if data.get('metaLinkEndpoint'):updated['endpoint']=data['metaLinkEndpoint']
    return validate_session(updated)


def renew(config):
    origin=config.get('native_auth_origin')
    if origin not in ORIGINS:raise ValueError('Unverified auth origin')
    token=config.get('refresh_token')
    if not isinstance(token,str) or not token:raise ValueError('Native refresh token missing')
    request=urllib.request.Request(origin+'/api/login/silent?'+urllib.parse.urlencode({'refreshToken':token}),data=b'',method='POST',headers={'User-Agent':config['user_agent'],'X-Client-Token':client_token(config),'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=15) as reply:
            status=reply.status
            body=reply.read(1024*1024+1)
        if len(body)>1024*1024:raise ValueError('Oversize auth response')
        parsed=json.loads(body)
    except Exception:
        raise RuntimeError('Native renewal transport failed') from None
    updated=parse_renewal(config,parsed)
    return updated,{'http_status':status,'account_match':True,'access_token_changed':updated['access_token']!=config['access_token'],'refresh_token_changed':updated['refresh_token']!=token,'expiry_in_future':updated['expires_at']>time.time()}


def ensure_fresh(path,config,force=False):
    if not force and config.get('expires_at',0)>time.time()+60:
        return validate_session(config),None
    updated,evidence=renew(config)
    private_write(path,updated)
    return updated,evidence
