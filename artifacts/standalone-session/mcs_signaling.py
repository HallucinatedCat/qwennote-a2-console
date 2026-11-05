"""Bounded MCS signalling transport modelled on the official app.

Engine.IO 3 / Socket.IO 2 JSON only. No microphone, media publication or WebRTC SDP.
The native token selects a public server; TLS validates the vendor certificate name.
"""
import asyncio
import base64
import collections
import contextlib
import ipaddress
import json
import os
import re
import ssl
import time
import uuid
from urllib.parse import urlsplit
import websockets

TLS_NAME='mcs.dingtalk.com'
MAX_FRAME=2*1024*1024
KNOWN_EVENTS={'participant','stream','progress','text','drop','rejoin','conference','error','disconnect','custom','notification','sync'}


def token_info(token):
    if not isinstance(token,str) or len(token)>65536:raise ValueError('Invalid signalling token')
    try:info=json.loads(base64.b64decode(token,validate=True))
    except (ValueError,UnicodeError):raise ValueError('Invalid signalling token') from None
    if not isinstance(info,dict) or info.get('secure') is not True:raise ValueError('Secure signalling required')
    host=info.get('host');room=info.get('room')
    if not isinstance(host,str) or not isinstance(room,str) or not room:raise ValueError('Signalling token fields missing')
    if not re.fullmatch(r'[A-Za-z0-9.:[\]_-]+',host):raise ValueError('Invalid signalling authority')
    uri=urlsplit('https://'+host)
    if uri.username or uri.password or uri.path or uri.query or uri.fragment or (uri.port or 443)!=443:
        raise ValueError('Unsupported signalling authority')
    name=uri.hostname
    try:address=ipaddress.ip_address(name)
    except ValueError:address=None
    # A public IP literal is allowed: the TLS handshake still requires the TLS_NAME certificate.
    if address is not None:
        if not address.is_global:raise ValueError('Nonpublic signalling address')
    elif not isinstance(name,str) or not (name==TLS_NAME or name.endswith('.'+TLS_NAME)):
        raise ValueError('Unsupported signalling host')
    return info,'wss://'+host+'/socket.io/?EIO=3&transport=websocket'


def login_payload(token,clock=None):
    token_info(token)
    # User agent and the data/trace-id/tid envelope as the official app sends them.
    data={'token':token,'config':{'subLogicStream':False},'protocol':'1.0',
          'clientSendTime':int(((time.time() if clock is None else clock)+2208988800)*1000),
          'userAgent':{'runtime':{'name':'webrtc','version':'mcs_v1.60.0.260831.57c17dc7'},
                       'sdk':{'type':'Android','version':'mcs_v1.60.0.260831.5d52bd2e'},
                       'os':{'name':'Android','version':'11'},
                       'capabilities':{'continualIceGathering':True,'unifiedPlan':True,'streamRemovable':True}}}
    return {'data':data,'trace-id':str(uuid.uuid4()),'tid':str(uuid.uuid4())}


def login_identity(info,ack):
    if not isinstance(ack,list) or len(ack)<2 or ack[0]!='ok' or not isinstance(ack[1],dict):
        raise ValueError('MCS login rejected')
    body=ack[1];room=body.get('room');participant=body.get('id')
    if not isinstance(room,dict) or str(room.get('id'))!=info['room']:
        raise ValueError('MCS room receipt mismatch')
    if not isinstance(participant,str) or not participant or len(participant)>512 or any(ord(c)<32 for c in participant):
        raise ValueError('MCS participant receipt missing')
    return participant


class MCS:
    def __init__(self,directory):
        self.ws=None;self.receiver=None;self.heartbeat=None;self.pending={};self.counter=0
        self.ready=False;self.participant=None;self.events=collections.Counter();self.frames=0;self.acks=0
        self.pings=0;self.pongs=0;self.error_type=None;self.truncated=False;self.bytes=0;self.authenticated=False
        self.notifications=asyncio.Queue(maxsize=128);self.notification_drops=0
        self.fd=os.open(directory/'mcs-frames.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    def trace(self,direction,frame):
        data=(json.dumps({'time':time.time(),'direction':direction,'frame':frame},ensure_ascii=False)+'\n').encode()
        if self.bytes+len(data)>4*1024*1024 or self.frames>=2000:self.truncated=True;return
        view=memoryview(data)
        while view:
            count=os.write(self.fd,view)
            if count<=0:raise OSError('Signalling trace write failed')
            view=view[count:]
        self.bytes+=len(data);self.frames+=1
    async def send(self,frame):
        self.trace('out',frame);await self.ws.send(frame)
    async def recv(self):
        frame=await self.ws.recv()
        if not isinstance(frame,str) or len(frame)>MAX_FRAME:raise ValueError('Unsupported MCS frame')
        self.trace('in',frame);return frame
    async def open(self,token,user_agent=None):
        info,uri=token_info(token)
        self.ws=await websockets.connect(uri,ssl=ssl.create_default_context(),server_hostname=TLS_NAME,
                        open_timeout=6,close_timeout=2,ping_interval=None,max_size=MAX_FRAME,user_agent_header=user_agent)
        opening=await asyncio.wait_for(self.recv(),6)
        if not opening.startswith('0'):raise ValueError('MCS Engine.IO opening missing')
        hello=json.loads(opening[1:]);period=hello.get('pingInterval')
        if type(period) is not int or not 1000<=period<=120000:raise ValueError('Invalid MCS heartbeat interval')
        namespace=await asyncio.wait_for(self.recv(),6)
        if namespace!='40':raise ValueError('MCS default namespace missing')
        self.ready=True;self.receiver=asyncio.create_task(self.receive_loop())
        self.heartbeat=asyncio.create_task(self.ping_loop(period/1000))
        ack=await self.request('login',login_payload(token))
        self.participant=login_identity(info,ack);self.authenticated=True
        return ack
    async def request(self,name,body):
        self.counter+=1;number=self.counter;future=asyncio.get_running_loop().create_future();self.pending[number]=future
        try:
            await self.send('42'+str(number)+json.dumps([name,body],separators=(',',':')))
            return await asyncio.wait_for(future,6)
        finally:self.pending.pop(number,None)
    async def ping_loop(self,period):
        try:
            while True:
                await asyncio.sleep(period);await self.send('2');self.pings+=1
        except asyncio.CancelledError:raise
        except Exception as error:self.error_type=type(error).__name__;self.ready=False
    async def receive_loop(self):
        try:
            while True:
                frame=await self.recv()
                if frame=='3':self.pongs+=1;continue
                if frame=='2':await self.send('3');continue
                match=re.fullmatch(r'43([0-9]+)(\[.*)',frame,re.S)
                if match:
                    number=int(match[1]);ack=json.loads(match[2]);self.acks+=1
                    future=self.pending.get(number)
                    if future is not None and not future.done():future.set_result(ack)
                    continue
                match=re.fullmatch(r'42([0-9]*)(\[.*)',frame,re.S)
                if match:
                    event=json.loads(match[2])
                    if not isinstance(event,list) or not event or not isinstance(event[0],str):raise ValueError('Invalid MCS event')
                    self.events[event[0] if event[0] in KNOWN_EVENTS else 'other']+=1
                    body=event[1] if len(event)>1 and isinstance(event[1],dict) else {}
                    if self.notifications.full():self.notifications.get_nowait();self.notification_drops+=1
                    self.notifications.put_nowait((event[0],body))
                    if match[1]:
                        ack=['ok']
                        if isinstance(body.get('tid'),str):ack.append(json.dumps({'tid':body['tid']}))
                        await self.send('43'+match[1]+json.dumps(ack,separators=(',',':')))
                    continue
                if frame in ('1','41'):break
                if frame.startswith('44'):raise ValueError('MCS namespace error')
        except asyncio.CancelledError:raise
        except websockets.exceptions.ConnectionClosedOK:pass
        except Exception as error:self.error_type=type(error).__name__
        finally:
            self.ready=False
            for future in self.pending.values():
                if not future.done():future.set_exception(ConnectionError('MCS disconnected'))
    async def close(self):
        if self.ws is not None:
            with contextlib.suppress(Exception):await self.ws.close()
        for task in (self.receiver,self.heartbeat):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):await task
        self.ready=False
        if self.fd is not None:os.close(self.fd);self.fd=None
    def summary(self):
        return {'authenticated':self.authenticated,'participant_id_present':self.participant is not None,
                'frames_captured':self.frames,'event_counts':dict(self.events),'acks_received':self.acks,
                'pings_sent':self.pings,'pongs_received':self.pongs,'trace_truncated':self.truncated,
                'error_type':self.error_type,'notification_drops':self.notification_drops,'media_published':False}
