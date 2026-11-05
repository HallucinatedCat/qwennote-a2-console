#!/usr/bin/env python3
"""Native LWP WebSocket transport: registration, request/reply matching, pushes, heartbeat.

Native renewal lives in native_auth.py. Bootstrap requires a protected authorized
session import.
"""
import argparse
import asyncio
import fcntl
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from websockets.asyncio.client import connect

READS={
    'devices':'/r/Adaptor/DeviceMonitorI/queryUserBindDevices',
    'organizations':'/r/Adaptor/DeviceQwenWorkI/listActiveOrganizations',
    'settings':'/r/Adaptor/DeviceSettingI/queryDeviceSettings',
    'generation-config':'/r/Adaptor/DeviceFileI/queryGenerateMinutesConfig',
}


def validate_session(config):
    required={'endpoint','app_key','client_device_id','user_agent','access_token','account_id','account_type','target_device_id'}
    if not isinstance(config,dict) or not required.issubset(config):
        raise ValueError('Incomplete native session')
    url=urlsplit(config['endpoint'])
    host=url.hostname or ''
    domains=('qwennote.cn','qwennote.ai','dingtalk.com')
    if url.scheme!='wss' or url.username or url.password or url.fragment or url.port not in (None,443) or not any(host==d or host.endswith('.'+d) for d in domains):
        raise ValueError('Use the verified vendor login-provided TLS endpoint')
    for key in ('app_key','client_device_id','user_agent','access_token'):
        if not isinstance(config[key],str) or not config[key].strip():
            raise ValueError('Missing native registration field')
    if config.get('expires_at') is not None and config['expires_at']<=time.time():
        raise ValueError('Native session expired; renew it before opening the transport')
    return config


def arguments(config,tool):
    if tool not in READS:raise ValueError('Only validated read shapes are enabled')
    if tool in {'devices','organizations'}:return [{'requestId':secrets.token_hex(16)}]
    result={'accountType':config['account_type'],'accountId':str(config['account_id']),'deviceId':int(config['target_device_id'])}
    if tool=='generation-config':
        result.update(deviceId=str(config['target_device_id']),fileId='',requestId=int(time.time()*1000))
    return [result]


class LWP:
    def __init__(self,endpoint,headers,timeout=10,heartbeat_interval=10,*,test_loopback=False):
        url=urlsplit(endpoint)
        if url.scheme!='wss' and not (test_loopback and url.scheme=='ws' and url.hostname=='127.0.0.1'):
            raise ValueError('TLS endpoint required')
        self.endpoint=endpoint
        self.headers=headers
        self.timeout=timeout
        self.heartbeat_interval=heartbeat_interval
        self.mid=0
        self.pending={}
        self.receiver=None
        self.heartbeater=None
        self.socket=None
        self.ready=False
        self.failure=None
        self.push_count=0
        self.pushes=asyncio.Queue(maxsize=512)
        self.dropped_pushes=0

    async def __aenter__(self):
        try:
            self.socket=await connect(self.endpoint,open_timeout=self.timeout,ping_interval=None,max_size=4*1024*1024,proxy=None)
            self.receiver=asyncio.create_task(self._receive())
            registered=await self._request('/reg',headers=self.headers)
            if registered.get('code')!=200:raise ValueError('Native registration rejected')
            self.ready=True
            self.heartbeater=asyncio.create_task(self._heartbeat())
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self,*_):
        await self.close()

    async def close(self):
        self.ready=False
        for task in (self.heartbeater,self.receiver):
            if task:task.cancel()
        for task in (self.heartbeater,self.receiver):
            if task:
                try:await task
                except (asyncio.CancelledError,Exception):pass
        if self.socket:await self.socket.close()
        for future in self.pending.values():
            if not future.done():future.cancel()
        self.pending.clear()

    async def _receive(self):
        try:
            async for frame in self.socket:
                message=json.loads(frame)
                headers=message.get('headers',{})
                if not isinstance(headers,dict):continue
                mid=str(headers.get('mid','')).split(' ')[0]
                if 'code' in message:
                    future=self.pending.get(mid)
                    if future and not future.done():future.set_result(message)
                elif isinstance(message.get('lwp'),str):
                    self.push_count+=1
                    if self.pushes.full():
                        self.pushes.get_nowait();self.dropped_pushes+=1
                    self.pushes.put_nowait(message)
                    # Native A0 acknowledges push headers. Do not execute push data.
                    await self.socket.send(json.dumps({'code':200,'headers':headers},separators=(',',':')))
        except asyncio.CancelledError:raise
        except Exception:
            self.failure='Native connection failed'
        finally:
            self.ready=False
            for future in self.pending.values():
                if not future.done():future.set_exception(ConnectionError('Native connection closed'))

    async def _heartbeat(self):
        try:
            while self.ready:
                await asyncio.sleep(self.heartbeat_interval)
                response=await self._request('/!')
                if response.get('code')!=200:raise ConnectionError('Heartbeat rejected')
        except asyncio.CancelledError:raise
        except Exception:
            self.failure='Native heartbeat failed'
            self.ready=False
            if self.socket:await self.socket.close()

    async def _request(self,uri,body=None,headers=None):
        if self.failure:raise ConnectionError(self.failure)
        self.mid+=1
        mid=str(self.mid)
        future=asyncio.get_running_loop().create_future()
        self.pending[mid]=future
        packet={'lwp':uri,'headers':{**(headers or {}),'mid':mid+' 0'}}
        if body is not None:packet['body']=body
        try:
            await self.socket.send(json.dumps(packet,separators=(',',':')))
            return await asyncio.wait_for(future,self.timeout)
        finally:self.pending.pop(mid,None)

    async def read(self,tool,body):
        if tool not in READS:raise ValueError('Read operation not allowed')
        if not self.ready:raise ConnectionError('Not registered')
        response=await self._request(READS[tool],body,{'referer':'https://n.qwennote.cn/qwennote/minutes/More.html'})
        if response.get('code')!=200:raise ValueError('Private read rejected')
        return response


def private_write(path,result):
    temp=path.with_name(path.name+'.'+secrets.token_hex(5)+'.tmp')
    try:
        with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            json.dump(result,stream,indent=2,ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


async def run(config,tool):
    headers={'app-key':config['app_key'],'did':config['client_device_id'],'ua':config['user_agent'],'token':config['access_token']}
    async with LWP(config['endpoint'],headers) as client:
        return await client.read(tool,arguments(config,tool))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool',choices=sorted(READS))
    parser.add_argument('--session',type=Path,default=Path(__file__).parent/'.private/native-session.json')
    args=parser.parse_args()
    # Refuse to run without session renewal support (native_auth.py), before touching any file.
    if importlib.util.find_spec('native_auth') is None:
        print('Native reads run from the host source; this copy has no session renewal. Nothing was read.')
        return 2
    logging.disable(logging.CRITICAL)
    lock=None
    try:
        path=args.session
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_mode&0o077 or path.parent.stat().st_mode&0o077:
            raise ValueError('Private session file/directory required')
        lock=os.open(path.parent/'native-session.lock',os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from native_auth import ensure_fresh
        config,_=ensure_fresh(path,json.loads(path.read_text()))
        result=asyncio.run(run(config,args.tool))
        private_write(path.parent/(args.tool+'-result.json'),result)
        print('Registration and read returned status 200; result saved privately.')
        return 0
    except Exception as error:
        print('Operation failed ('+type(error).__name__+'). No credentials or response values printed.')
        return 2
    finally:
        if lock is not None:os.close(lock)


if __name__=='__main__':raise SystemExit(main())
