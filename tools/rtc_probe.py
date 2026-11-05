#!/usr/bin/env python3
"""Bounded owner-authorized RTC transcription probe using an isolated native session.

Optional signalling join and receive-only audio. No translation targets or TTS.
Raw replies/SDP/receipts stay private; decoded audio is not saved or played.
"""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import time
import urllib.request
import http.cookiejar

CASE=Path(__file__).resolve().parents[1]
SOURCE=CASE/'artifacts/standalone-session'
if not SOURCE.is_dir():SOURCE=CASE  # standalone host source package
sys.path.insert(0,str(SOURCE))
from lwp_client import LWP,arguments,validate_session
from device_protocol import selected_device
from live_diagnostics import protected_directory
from rtc_session import run_probe
import record_tasks


def check_host_preflight(status,now):
    if (status.get('connection')!='connected' or status.get('own_test') or
        status.get('ready_until',0)>now or status.get('rtc',{}).get('active') or
        status.get('rtc',{}).get('cleanup_pending') or status.get('mutation_in_progress') or
        status.get('pending_restores') or status.get('live_diagnostics',{}).get('active') or
        status.get('document_read_in_progress') or status.get('assistant_in_progress') or
        status.get('record_task',{}).get('read_in_progress')):
        raise ValueError('Host is unavailable or has unfinished work')
    if record_tasks.blocks_start(status.get('record_task')):
        raise ValueError('An existing cloud recording task must be checked before a new probe')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=10,choices=range(1,46))
    parser.add_argument('--start-recording',action='store_true',help='Official 4G startup order: start recording before RTC task creation')
    parser.add_argument('--receive-audio',action='store_true',help='Receive conference Opus and count decoded frames; no audio saved or played')
    parser.add_argument('--join-signaling',action='store_true',help='Join MCS and application signalling; no local media or TTS')
    parser.add_argument('--inspect-record-task',action='store_true',help='Read the matched active recording task once during the joined observation window')
    parser.add_argument('--owner-authorized-idle-device',action='store_true',required=True)
    args=parser.parse_args();logging.disable(logging.CRITICAL);os.umask(0o077);lock=None
    try:
        if args.inspect_record_task and not (args.start_recording and args.join_signaling):raise ValueError('Task inspection requires recording and conference join')
        if args.receive_audio:
            if not args.join_signaling:raise ValueError('Signalling join required')
            from mcs_media import AudioReceiver
        session=args.session
        protected_directory(session.parent)
        if session.is_symlink() or session.stat().st_mode&0o077:raise ValueError('Protected isolated session required')
        if session.resolve()==(SOURCE/'.private/native-session.json').resolve():raise ValueError('Current host session must not be used')
        lock=os.open(session.parent/'native-session.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pending=session.parent/'rtc-probe-pending.json'
        if pending.exists() and json.loads(pending.read_text()).get('pending'):raise ValueError('Previous RTC probe needs cleanup')
        config=json.loads(session.read_text());working=json.loads((SOURCE/'.private/native-session.json').read_text())
        if str(config['account_id'])!=str(working['account_id']) or str(config['target_device_id'])!=str(working['target_device_id']):raise ValueError('Isolated target differs')
        if config.get('refresh_token')==working.get('refresh_token') or config.get('client_device_id')==working.get('client_device_id'):
            raise ValueError('Host owns this native client/session')
        if config.get('expires_at',0)<time.time()+120:raise ValueError('Renew the isolated session before the probe')
        validate_session(config)
        jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
        opener.open('http://127.0.0.1:18780/',timeout=5).close()
        status=json.load(opener.open('http://127.0.0.1:18780/api/status',timeout=5))
        check_host_preflight(status,time.time())
        directory=session.parent/('rtc-probe-'+secrets.token_hex(8));directory.mkdir(mode=0o700)
        async def operate():
            task=asyncio.current_task()
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,task.cancel)
            headers={'app-key':config['app_key'],'did':config['client_device_id'],'ua':config['user_agent'],'token':config['access_token']}
            async with LWP(config['endpoint'],headers,timeout=6) as channel:
                device=selected_device(config,await channel.read('devices',arguments(config,'devices')))
                if device.get('onlineStatus')!=1:raise ValueError('Selected device is not cloud-online')
                return await run_probe(channel,config,directory,args.seconds,args.join_signaling,args.receive_audio,args.start_recording,args.inspect_record_task)
        result=asyncio.run(operate())
        print(json.dumps(result,indent=2))
        return 0 if not result['cleanup_pending'] and result['error_type'] is None and (not args.inspect_record_task or result['record_task_read']['matched']) else 2
    except Exception as error:
        print(json.dumps({'probe_failed':True,'error_type':type(error).__name__,'private_details_omitted':True}));return 2
    finally:
        if lock is not None:os.close(lock)

if __name__=='__main__':raise SystemExit(main())
