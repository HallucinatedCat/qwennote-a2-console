#!/usr/bin/env python3
"""Loopback QwenNote UI/backend. Physical operations require explicit readiness.

Reads, renewal and recording cloud controls are verified. Actual ASR delivery
and most setting effects need coordinated validation. Bounded RTC sessions use the host transport.
"""
import argparse
import asyncio
from collections import deque
import contextlib
import contextvars
import fcntl
import hashlib
import hmac
import html
import json
import logging
import os
import re
from pathlib import Path
import secrets
import sys
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit
from aiohttp import web,ClientSession,ClientTimeout,ClientError
from lwp_client import LWP,arguments,private_write
from native_auth import ensure_fresh
from live_transcript import TranscriptStore
from live_diagnostics import PushCapture
from rtc_controller import RTCController
import generation_settings as generation_protocol
import notification_settings as notification_protocol
import rtc_translation as interpreter_protocol
import audio_memos as memo_protocol
import builtin_assistant
import quota_protocol
import record_tasks as task_protocol
import note_favorites
import note_export
import note_media
import note_summary
import note_tags
import note_replace
import note_translate
import note_sharing
import note_status
import device_files
import account_settings
import mcp_login
import native_extras as extras
from settings_catalog import catalog as settings_catalog,status_part as settings_status_part,validate_change
from device_protocol import (record_command,setting_command,selected_device,public_device,recording_fid,device_info_change,
                             writable_settings,decode_push,owner_device,TRANSCRIPT_STREAMS,RECORD_URI,SETTING_URI)
import device_name
from public_errors import PublicError,Refused,NotReady,Unavailable,Busy,OutcomeUnknown,CloudRejected,describe
import capabilities
import credential_status
import ui_page
from cache import Cache,DAY,WEEK

ROOT=Path(__file__).resolve().parent
# Armed controls stay armed until Disarm; ready_until keeps its old meaning for older pages.
ARMED_UNTIL=4102444800.0  # 2100-01-01
DELETE_NOTE_URI='/r/Adaptor/PortalMinutesI/deleteTaskV2'
UNBIND_URI='/r/Adaptor/DeviceI/unbindV3'
# Device check interval while a recording is open, and otherwise. The official app never
# polls (it waits for pushes), so a powered-off A2 kept showing as recording there and here.
CHECK_SECONDS=(30,300)
# A request waits this long for an earlier request of the same kind before it is refused as busy.
QUEUE_SECONDS=60
REFERER={'referer':'https://n.qwennote.cn/qwennote/minutes/More.html'}
# Per request: whether the client asked for fresh data, and the save times of cached data it was served.
FORCE_REFRESH=contextvars.ContextVar('force_refresh',default=False)
request_host=contextvars.ContextVar('request_host',default='')
CACHE_SEEN=contextvars.ContextVar('cache_seen',default=None)
# Read actions served from the cache, with their lifetimes. Option lists live longer.
CACHED_ACTIONS={'account-settings':DAY,'documents':DAY,'note-summary':DAY,'note-tags':DAY,'note-share':DAY,'note-favorite-status':DAY,
                'quotas':DAY,'rtc-options':WEEK,'note-translate-options':WEEK}
# A pending journal must be reconciled by a real read, so the cache is skipped while one exists.
PENDING_JOURNALS={'note-summary':'summary_pending','note-tags':'tags_pending','note-share':'share_pending','note-favorite-status':'favorite_pending'}
# What each write makes stale, besides the note it names (tag "note:<uuid>").
WRITE_DROPS={'note-summary-save':(),'note-regenerate':(),'note-replace-save':(),'note-favorite':(),'note-share-save':(),
             'note-tags-save':('tags',),'note-tag-create':('tags',),'note-tag-rename':('tags',),'note-tag-delete':('tags',),
             'record':('notes-list','audio-memos'),'confirm-stopped':('notes-list','audio-memos'),
             'setting':('settings',),'restore':('settings',),'generation':('settings',),'restore-generation':('settings',),
             'notification':('settings',),'restore-notification':('settings',),'device-name-save':(),'device-name-restore':(),
             'note-mcp-write':('notes-list',),'slicing-save':('settings',),'voiceprint-save':('settings',),'voiceprint-clear':('settings',)}


def cache_tags(action,data):
    tags={action}
    uuid=data.get('task_uuid') if action!='documents' else (data.get('arguments') or {}).get('taskUuid')
    if isinstance(uuid,str):tags.add('note:'+uuid)
    if action=='documents' and str(data.get('tool','')).startswith('list_'):tags.add('notes-list')
    if action=='note-tags':tags.add('tags')
    if action=='account-settings':tags.add('settings')
    return tags


def mcp_write_arguments(tool,values):
    """Argument checks for the MCP write tools (names from the pinned DWS CLI)."""
    if not isinstance(values,dict):raise Refused('Write arguments must be an object')
    def text(key,limit):
        value=values.get(key)
        return value if isinstance(value,str) and value.strip() and len(value)<=limit and '\x00' not in value else None
    if tool in ('add_personal_hot_word','delete_personal_hotword'):
        words=values.get('hotWordList')
        if (not isinstance(words,list) or not 1<=len(words)<=50 or
            any(not isinstance(w,str) or not w.strip() or len(w)>20 for w in words)):
            raise Refused('Enter 1 to 50 hot words of at most 20 characters each')
        return {'hotWordList':[w.strip() for w in words]}
    if tool not in ('update_minutes_title','update_minutes_summary','replace_minutes_text','replace_speaker'):
        raise Refused('Choose one of the documented MCP write tools')
    uuid=values.get('taskUuid')
    if not note_favorites.valid_uuid(uuid):raise Refused('Load this note from your own notes list before using application actions')
    if tool=='update_minutes_title':
        if text('title',200) is None:raise Refused('Enter a title of at most 200 characters')
        return {'taskUuid':uuid,'title':values['title']}
    if tool=='update_minutes_summary':
        if text('summaryText',48*1024) is None:raise Refused('Enter a nonempty Markdown summary up to 48 KiB without images or rich content')
        return {'taskUuid':uuid,'summaryText':values['summaryText']}
    if tool=='replace_minutes_text':
        if text('originalText',1000) is None or text('replacedText',1000) is None:
            raise Refused('Enter nonempty find text and replacement text of at most 1000 characters each')
        return {'taskUuid':uuid,'originalText':values['originalText'],'replacedText':values['replacedText']}
    if text('speakerNick',64) is None or text('targetNickName',64) is None:
        raise Refused('Enter the current and the new speaker name, at most 64 characters each')
    return {'taskUuid':uuid,'speakerNick':values['speakerNick'],'targetNickName':values['targetNickName']}


class Backend:
    def __init__(self,state,port,mcp_root=None):
        self.state=state;self.path=state/'native-session.json';self.port=port;self.mcp_root=mcp_root
        self.config=None;self.channel=None;self.task=None;self.fd=None
        self.connection='disconnected';self.device=None;self.device_record=None;self.device_checked_at=None;self.settings={};self.writable_keys=set();self.setting_details={};self.generation={}
        self.events=deque(maxlen=256);self.event_id=0;self.transcripts=TranscriptStore()
        self.diagnostics=PushCapture(state);self.capture_timer=None
        self.csrf=secrets.token_urlsafe(32);self.cookie=secrets.token_urlsafe(32)
        self.started_at=time.time()
        self.armed_path=state/'armed.json'
        if self.armed_path.is_symlink():raise ValueError('Armed state must not be a link')
        armed=self.armed_path.exists() and json.loads(self.armed_path.read_text()).get('armed') is True
        self.ready_until=ARMED_UNTIL if armed else 0;self.record_phase='unconfirmed'
        self.record_state={'phase':'idle','action':None,'at':None}
        self.last_fid=None  # fid of the recording the A2 last reported, for Save audio after Stop
        self.test_path=state/'disposable-test.json'
        if self.test_path.is_symlink():raise ValueError('Disposable-test record must not be a link')
        self.own_test=self.test_path.exists() and json.loads(self.test_path.read_text()).get('open') is True
        if self.own_test:self.record_state={'phase':'open-after-restart','action':None,'at':None}
        self.mutation_lock=asyncio.Lock();self.document_lock=asyncio.Lock();self.mcp_write_lock=asyncio.Lock()
        self.files_lock=asyncio.Lock();self.recording_handles={};self.recording_pages={}
        self.mcp_logins={}  # OAuth state -> pending consent (PKCE verifier stays in memory only)
        self.handles={}  # (kind, handle) -> private id: recording fids, groups, markers, memos
        self.extras_lock=asyncio.Lock()
        # Design: destructive actions exist, behind a switch in Dev tools.
        self.dangerous_path=state/'dangerous.json'
        if self.dangerous_path.is_symlink():raise ValueError('Dangerous-actions state must not be a link')
        self.dangerous_enabled=self.dangerous_path.exists() and json.loads(self.dangerous_path.read_text()).get('enabled') is True
        self.device_state=None;self.device_check_lock=asyncio.Lock();self.device_check_task=None;self.next_device_check=0
        self.assistant_task=None;self.assistant_request_id=None
        self.assistant_profile=None;self.assistant_credential=None;self.assistant_credential_expires=0
        self.assistant_session_id=None;self.assistant_task_id=None;self.assistant_recovery_available=False
        self.assistant_conversation_handles={}
        self.setting_restore_path=state/'setting-restore.json'
        if self.setting_restore_path.is_symlink():raise ValueError('Setting restore journal must not be a link')
        self.restores=json.loads(self.setting_restore_path.read_text()) if self.setting_restore_path.exists() else {}
        self.generation_restore_path=self.state/'generation-restore.json'
        if self.generation_restore_path.is_symlink():raise ValueError('Generation restore journal must not be a link')
        self.generation_restore=json.loads(self.generation_restore_path.read_text()) if self.generation_restore_path.exists() else None
        self.notification_restore_path=self.state/'notification-restore.json'
        if self.notification_restore_path.is_symlink():raise ValueError('Notification restore journal must not be a link')
        self.notification_restore=json.loads(self.notification_restore_path.read_text()) if self.notification_restore_path.exists() else None
        self.notification_rows={};self.organization_options=None
        # One HMAC key per process for every opaque handle and keyed revision sent to the
        # browser, so handles issued before a restart no longer resolve.
        self.handle_secret=secrets.token_hex(32)
        self.rtc=RTCController(self)
        self.interpreter_cache={};self.interpreter_lock=asyncio.Lock()
        self.memo_cache={};self.memo_lock=asyncio.Lock()
        self.native_transcript_pages={};self.translation_pages={};self.native_transcript_lock=asyncio.Lock()
        self.quota_cache=None;self.quota_lock=asyncio.Lock()
        # One lock per note feature, because each feature keeps a single pending journal.
        # Features do not block each other or device commands; see note_locks().
        self.favorite_lock=asyncio.Lock();self.summary_lock=asyncio.Lock();self.share_lock=asyncio.Lock()
        self.tags_lock=asyncio.Lock();self.replace_lock=asyncio.Lock()
        self.export_lock=asyncio.Lock();self.media_lock=asyncio.Lock()
        self.note_refs={};self.favorite_path=state/'note-favorite.json'
        if self.favorite_path.is_symlink():raise ValueError('Favorite journal must not be a link')
        saved=json.loads(self.favorite_path.read_text()) if self.favorite_path.exists() else {}
        self.favorite_pending=saved if saved.get('pending') else None
        self.summary_path=state/'note-summary.json'
        if self.summary_path.is_symlink():raise ValueError('Summary journal must not be a link')
        saved=json.loads(self.summary_path.read_text()) if self.summary_path.exists() else {}
        self.summary_pending=saved if saved.get('pending') else None
        self.tags_path=state/'note-tags.json'
        if self.tags_path.is_symlink():raise ValueError('Tag journal must not be a link')
        saved=json.loads(self.tags_path.read_text()) if self.tags_path.exists() else {}
        self.tags_pending=saved if saved.get('pending') else None
        self.replace_path=state/'note-replace.json'
        if self.replace_path.is_symlink():raise ValueError('Replacement journal must not be a link')
        saved=json.loads(self.replace_path.read_text()) if self.replace_path.exists() else {}
        self.replace_pending=saved if saved.get('pending') else None
        self.share_path=state/'note-share.json'
        if self.share_path.is_symlink():raise ValueError('Share journal must not be a link')
        saved=json.loads(self.share_path.read_text()) if self.share_path.exists() else {}
        self.share_pending=saved if saved.get('pending') else None
        self.device_name_path=state/'device-name.json'
        if self.device_name_path.is_symlink():raise ValueError('Device-name journal must not be a link')
        saved=json.loads(self.device_name_path.read_text()) if self.device_name_path.exists() else {}
        self.device_name_restore=saved or None
        self.record_task=None;self.record_task_source=None;self.record_task_observed_at=None
        self.record_task_lock=asyncio.Lock()
        self.latest_event=None;self.latest_event_handle=None
        self.last_refresh_at=None
        # Renewal outcomes of the service lineage since this process started (counts and times only).
        self.renewals={'count':0,'last_at':None,'failures':0,'last_failure_at':None,'last_failure_type':None}
        self.credential_cache=None
        # Per-action request timings since start (counts and milliseconds only), for Dev tools.
        self.action_timings={}
        self.cache=Cache(state/'cache')

    def emit(self,kind,data):
        self.event_id+=1
        self.events.append({'id':self.event_id,'kind':kind,'time':time.time(),'data':data})

    @contextlib.asynccontextmanager
    async def queued(self,lock):
        """Hold `lock`, waiting behind an earlier request of the same kind instead of refusing it."""
        try:
            async with asyncio.timeout(QUEUE_SECONDS):await lock.acquire()
        except TimeoutError:
            raise Busy('An earlier request of the same kind is running; try again shortly') from None
        try:yield
        finally:lock.release()

    async def read_together(self,*reads):
        """Send independent reads at once (the channel matches replies by message id).
        Replies come back in request order; if one fails, the others are cancelled."""
        async with asyncio.TaskGroup() as group:
            tasks=[group.create_task(self.channel._request(uri,body,REFERER)) for uri,body in reads]
        return [task.result() for task in tasks]

    def cached(self,key,ttl):
        """A cached value younger than ttl, unless this request asked for fresh data."""
        if FORCE_REFRESH.get():return None
        entry=self.cache.get(key,ttl)
        if entry is None:return None
        seen=CACHE_SEEN.get()
        if seen is not None:seen.append(entry['at'])
        return entry['value']

    async def cloud_read(self,uri,body,ttl=DAY,tags=(),final=lambda reply:True):
        """A cloud read used only for display, served from the cache while fresh. Writes and
        their readbacks call the channel directly so they always see the current cloud state.
        final(reply) false (a note still processing) keeps the reply out of the cache."""
        key='read:'+uri+':'+json.dumps(body,sort_keys=True,ensure_ascii=False)
        value=self.cached(key,ttl)
        if value is not None:return value
        reply=await self.channel._request(uri,body,REFERER)
        if isinstance(reply,dict) and reply.get('code')==200 and final(reply):self.cache.put(key,reply,tags)
        return reply

    async def read_tool(self,tool,ttl,tags):
        """A named LWP read (settings, generation config) with an optional cache lifetime."""
        key='tool:'+tool+':'+json.dumps(arguments(self.config,tool),sort_keys=True,ensure_ascii=False)
        value=self.cached(key,ttl) if ttl else None
        if value is not None:return value
        value=await self.channel.read(tool,arguments(self.config,tool))
        self.cache.put(key,value,tags)
        return value

    def invalidate_after(self,action,data):
        if action not in WRITE_DROPS:return
        tags=set(WRITE_DROPS[action])
        uuid=data.get('task_uuid') if 'task_uuid' in data else (data.get('arguments') or {}).get('taskUuid') if isinstance(data.get('arguments'),dict) else None
        if isinstance(uuid,str):tags.add('note:'+uuid)
        if tags:self.cache.drop(*tags)

    def record_timing(self,action,seconds,ok):
        if action not in self.action_timings and len(self.action_timings)>=100:return
        row=self.action_timings.setdefault(action,{'count':0,'failures':0,'last_ms':0,'max_ms':0,'total_ms':0})
        ms=round(seconds*1000)
        row['count']+=1;row['failures']+=not ok;row['last_ms']=ms;row['max_ms']=max(row['max_ms'],ms);row['total_ms']+=ms

    def note_locks(self):
        return (self.favorite_lock,self.summary_lock,self.share_lock,self.tags_lock,self.replace_lock)

    # Lifecycle -----------------------------------------------------------------

    async def start(self,app):
        if self.path.is_symlink() or self.state.is_symlink() or self.path.stat().st_mode&0o077 or self.state.stat().st_mode&0o077:
            raise ValueError('Protected native session store required')
        self.fd=os.open(self.state/'native-session.lock',os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.config=json.loads(self.path.read_text())
        self.task=asyncio.create_task(self.worker())

    async def stop(self,app):
        if self.assistant_task is not None:
            self.assistant_task.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):await self.assistant_task
        await self.rtc.shutdown()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.task
        self.stop_capture('shutdown')
        self.flush_latest_event()
        if self.fd is not None:os.close(self.fd)

    def worker_running(self):
        return self.task is not None and not self.task.done()

    async def worker(self):
        failures=0
        while True:
            renewing=False
            try:
                await self.renew()
                c=self.config
                headers={'app-key':c['app_key'],'did':c['client_device_id'],'ua':c['user_agent'],'token':c['access_token']}
                self.connection='connecting'
                async with LWP(c['endpoint'],headers) as channel:
                    self.channel=channel;self.connection='connected'
                    self.diagnostics.connection(True,channel.dropped_pushes)
                    await self.refresh(cached=True);failures=0;self.next_device_check=0
                    self.transcripts.mark_reconnect()
                    self.emit('connection',{'state':'connected','events_since_reconnect_may_have_gaps':True})
                    while channel.ready and not channel.failure:
                        self.diagnostics.observe_queue(channel.dropped_pushes)
                        if time.time()>=self.next_device_check:self.schedule_device_check()
                        if c.get('expires_at',0)<time.time()+60:
                            # Planned reconnect: renew before expiry. Not a failure.
                            await self.rtc.shutdown();renewing=True;break
                        try:message=await asyncio.wait_for(channel.pushes.get(),1)
                        except asyncio.TimeoutError:continue
                        self.process_push(message)
            except asyncio.CancelledError:raise
            except Exception as error:
                self.emit('connection',{'state':'disconnected','error_type':type(error).__name__})
            finally:
                if self.channel is not None:self.diagnostics.connection(False,self.channel.dropped_pushes)
                self.channel=None;self.connection='disconnected'
            if renewing:
                self.emit('connection',{'state':'renewing'})
                continue
            failures+=1
            if failures>=3:
                self.connection='needs-attention'
                self.emit('connection',{'state':'needs-attention','reason':'Three failed connections; check the session, then reconnect'})
                return
            await asyncio.sleep(min(2**failures,10))

    async def renew(self):
        """ensure_fresh with outcome bookkeeping. Still the only refresh owner for this lineage."""
        try:self.config,evidence=await asyncio.to_thread(ensure_fresh,self.path,self.config)
        except Exception as error:
            self.renewals.update(failures=self.renewals['failures']+1,last_failure_at=time.time(),last_failure_type=type(error).__name__)
            self.credential_cache=None
            self.emit('credential',{'renewed':False,'error_type':type(error).__name__})
            raise
        if evidence is not None:
            self.renewals.update(count=self.renewals['count']+1,last_at=time.time())
            self.credential_cache=None
            self.emit('credential',{'renewed':True})

    def credentials(self):
        """Cached for 10 s because every status poll asks, and each collect() stats and reads several private files."""
        now=time.time()
        if self.credential_cache and now-self.credential_cache['checked_at']<10:return self.credential_cache
        self.credential_cache=credential_status.collect(self.state,self.mcp_root,config=self.config,renewals=self.renewals,now=now)
        return self.credential_cache

    def reconnect(self):
        """Restart the connection worker after it gave up. Never runs two workers."""
        if self.config is None:raise Unavailable('The native session has not been loaded')
        if self.worker_running():raise Refused('The connection is already being managed; wait for it to settle')
        self.connection='disconnected'
        self.task=asyncio.create_task(self.worker())
        self.emit('connection',{'state':'reconnect-requested'})
        return {'reconnecting':True}

    # Diagnostics -----------------------------------------------------------------

    def start_capture(self,seconds=120):
        result=self.diagnostics.start(seconds,connected=self.connection=='connected',
                                      queue_dropped=getattr(self.channel,'dropped_pushes',0))
        if self.capture_timer is not None:self.capture_timer.cancel()
        self.capture_timer=asyncio.get_running_loop().call_later(seconds,self.stop_capture,'time_limit')
        return result

    def stop_capture(self,reason='manual'):
        if self.capture_timer is not None:self.capture_timer.cancel();self.capture_timer=None
        return self.diagnostics.stop(reason)

    # Push processing -----------------------------------------------------------

    def process_push(self,message):
        self.rtc.accept(message)
        error=False
        try:events=decode_push(message)
        except (TypeError,ValueError,KeyError,AttributeError):
            events=[];error=True
            self.emit('event-error',{'reason':'Unrecognized push shape'})
        updates=sum(bool(self.process_event(event)) for event in events)
        self.diagnostics.observe_push(message,events,updates,decoder_error=error)

    def process_event(self,event):
        data=event['payload']
        # Only the latest raw event is kept, and only in protected storage.
        self.keep_latest_event(event)
        if event['type'] in TRANSCRIPT_STREAMS:
            row=self.transcripts.accept(data,source=TRANSCRIPT_STREAMS[event['type']])
            if row is not None:
                self.emit('transcript',row)
                return True
        elif event['type'] in note_status.NOTE_EVENTS:
            # A note finished transcribing or summarizing, or the list changed: drop saved reads.
            uuid,name=note_status.event(data)
            self.cache.drop('notes-list',*(('note:'+uuid,) if uuid else ()))
            self.emit('note-status',{'uuid':uuid,'event':name})
        else:
            repeat=False
            try:
                task=task_protocol.from_event(self.config,event)
                if task is not None:
                    previous=self.record_task;self.remember_record_task(task,'push');current=self.record_task
                    # The cloud repeats task pushes every few seconds while nothing changes;
                    # only a different recording, state or end is worth a device check or a log line.
                    repeat=previous is not None and (current.fid,current.state,current.end_ms)==(previous.fid,previous.state,previous.end_ms)
                    if not repeat:self.check_soon()
                change=device_info_change(self.config,event)
                if change is not None:
                    if change['battery_percent'] is not None and self.device:self.device={**self.device,'battery_percent':change['battery_percent']}
                    if change['wants_check']:self.check_soon()
            except (ValueError,TypeError,KeyError):pass
            if not repeat:self.emit('device-event',{'event_type':data.get('eventType'),'event_time':data.get('eventTime'),'physical_state_confirmed':False})
        return False

    def keep_latest_event(self,event):
        """Debounced private write: live text can arrive several times a second, and
        an fsync per event on the event loop stalls every other request."""
        self.latest_event=event
        if self.latest_event_handle is not None:return
        try:loop=asyncio.get_running_loop()
        except RuntimeError:
            self.flush_latest_event();return
        self.latest_event_handle=loop.call_later(1.0,self.flush_latest_event)

    def flush_latest_event(self):
        if self.latest_event_handle is not None:
            self.latest_event_handle.cancel();self.latest_event_handle=None
        event,self.latest_event=self.latest_event,None
        if event is None:return
        # A full disk must not tear down the native connection.
        with contextlib.suppress(OSError,TypeError,ValueError):
            private_write(self.state/'latest-device-event.json',event)

    # Reads ---------------------------------------------------------------------

    async def refresh(self,cached=False):
        """Device list (always fresh), settings and generation options. On connect (cached=True)
        settings may come from the cache for a day and option lists for a week."""
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        response=await self.channel.read('devices',arguments(self.config,'devices'))
        self.device_record=selected_device(self.config,response)
        self.device=public_device(self.device_record)
        self.device_checked_at=time.time()
        response=await self.read_tool('settings',DAY if cached else 0,('settings',))
        generation=await self.read_tool('generation-config',WEEK if cached else 0,('settings',))
        self.generation=generation.get('body',{})
        self.notification_rows=notification_protocol.current(self.config,response)
        self.setting_details={row['key']:row for row in settings_catalog(self.config,response,self.generation)}
        self.settings={key:row['value'] for key,row in self.setting_details.items() if row['value'] is not None}
        self.writable_keys=writable_settings(self.config,response)
        self.last_refresh_at=time.time()
        return self.status()

    async def readback(self):
        """Refresh after an acknowledged write. A failed readback must not hide the acknowledgement."""
        try:
            await self.refresh()
            return True
        except Exception as error:
            self.emit('readback',{'completed':False,'error_type':type(error).__name__})
            return False

    def device_identity(self):
        if not isinstance(self.device_record,dict):raise Unavailable('Device details have not been read')
        result={**owner_device(self.device_record),'read_at':self.last_refresh_at,'stale':self.connection!='connected',
                'revision':None,'pending':bool(self.device_name_restore and self.device_name_restore.get('pending')),
                'restore_available':False}
        name=result['name'];saved=self.device_name_restore
        if self.config and isinstance(name,str) and name:
            try:identity=device_name.identity(self.config,self.device_record)
            except ValueError:return result
            result['revision']=device_name.revision(self.handle_secret,identity,name)
            result['restore_available']=bool(saved and not saved.get('pending') and
                device_name.same_identity(saved.get('identity'),identity) and name==saved.get('requested') and
                name!=saved.get('original'))
        return result

    def _current_device_name(self):
        if not self.config or not isinstance(self.device_record,dict):raise Unavailable('The selected device is unavailable')
        identity=device_name.identity(self.config,self.device_record)
        name=self.device_record.get('deviceNick')
        if not isinstance(name,str) or not name:raise Unavailable('The current device name is unavailable')
        return identity,name

    def _reconcile_device_name(self):
        saved=self.device_name_restore
        if not saved or not saved.get('pending'):return
        try:identity,name=self._current_device_name()
        except (ValueError,Unavailable):return
        if not device_name.same_identity(saved.get('identity'),identity):return
        if name not in (saved.get('original'),saved.get('requested')):return
        if saved.get('requested')==saved.get('original'):
            private_write(self.device_name_path,{})
            self.device_name_restore=None
        else:
            saved={**saved,'pending':False,'verified_at':time.time()}
            private_write(self.device_name_path,saved)
            self.device_name_restore=saved

    async def device_name_status(self):
        if self.mutation_lock.locked():raise Busy('A device command is already in progress')
        async with self.mutation_lock:
            async with asyncio.timeout(30):await self.refresh()
            self._reconcile_device_name()
            return self.device_identity()

    async def rename_device(self,name,expected,restore=False):
        if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
            raise Refused('Load the current device name before saving')
        if restore:
            if name is not None:raise Refused('Restoration does not accept a new name')
        else:
            try:name=device_name.normalize(name)
            except ValueError:raise Refused('Use a nonempty device name within 32 application units, without control characters') from None
        self.rtc.require_clear()
        if self.mutation_lock.locked():raise Busy('A device command is already in progress')
        async with self.mutation_lock:
            if self.device_name_restore and self.device_name_restore.get('pending'):
                try:
                    async with asyncio.timeout(30):await self.refresh()
                except Exception:raise OutcomeUnknown('The device-name result is still unknown; retry the read-only refresh') from None
                self._reconcile_device_name()
                if self.device_name_restore and self.device_name_restore.get('pending'):
                    raise OutcomeUnknown('The device name changed elsewhere or remains uncertain; manual review is required')
                return {**self.device_identity(),'changed':False,'reconciled':True,'restored':restore}
            self.require_ready()
            async with asyncio.timeout(30):await self.refresh()
            identity,current=self._current_device_name()
            revision=device_name.revision(self.handle_secret,identity,current)
            if not secrets.compare_digest(revision,expected):raise Refused('The device name changed elsewhere; refresh it before saving')
            saved=self.device_name_restore
            if restore:
                if not saved or saved.get('pending') or not device_name.same_identity(saved.get('identity'),identity):
                    raise Refused('No saved original device name is available')
                if current not in (saved.get('original'),saved.get('requested')):
                    raise Refused('The device name changed elsewhere; automatic restoration was refused')
                target=saved['original'];original=saved['original']
            else:
                target=name
                if saved and device_name.same_identity(saved.get('identity'),identity) and current==saved.get('requested'):
                    original=saved['original']
                else:original=current
            if current==target:
                if restore:
                    private_write(self.device_name_path,{})
                    self.device_name_restore=None
                return {**self.device_identity(),'changed':False,'restored':restore}
            request=device_name.request(self.config,self.device_record,target)
            saved={'identity':identity,'original':original,'requested':target,'request':request,
                   'pending':True,'restore':restore,'started_at':time.time()}
            private_write(self.device_name_path,saved);self.device_name_restore=saved
            try:
                async with asyncio.timeout(30):reply=await self.channel._request(device_name.URI,request,REFERER)
            except asyncio.CancelledError:raise
            except Exception:raise OutcomeUnknown('The device-name result is unknown; use read-only refresh before another change') from None
            if not device_name.acknowledged(reply):
                raise CloudRejected('The device-name change was not acknowledged; use read-only refresh before another change')
            saved['cloud_acknowledged']=True
            private_write(self.device_name_path,saved);self.device_name_restore=saved
            try:
                async with asyncio.timeout(30):await self.refresh()
                observed_identity,observed=self._current_device_name()
            except Exception:raise OutcomeUnknown('The device-name acknowledgement arrived but readback failed; use read-only refresh') from None
            if observed!=target or not device_name.same_identity(observed_identity,identity):
                raise OutcomeUnknown('The device-name readback differs; use read-only refresh before another change')
            if restore:
                private_write(self.device_name_path,{})
                self.device_name_restore=None
            else:
                saved={**saved,'pending':False,'verified_at':time.time()}
                private_write(self.device_name_path,saved);self.device_name_restore=saved
            return {**self.device_identity(),'changed':True,'cloud_acknowledged':True,
                    'readback_matches':True,'restored':restore,'automatic_retry':False}

    def status(self,since=None,settings_revision=None):
        now=time.time()
        events=list(self.events)
        if since is not None:events=[e for e in events if e['id']>since]
        catalog,revision=settings_status_part(self.setting_details)
        result={'connection':self.connection,'device':self.device,'settings':self.settings,
                'server_time':now,'started_at':self.started_at,'last_refresh_at':self.last_refresh_at,
                'worker_running':self.worker_running(),'settings_revision':revision,
                'generation_group':generation_protocol.descriptor(self.setting_details,self.generation_restore),
                'notification_group':notification_protocol.descriptor(self.notification_rows,self.organization_options,self.handle_secret,self.notification_restore),
                'ready_until':self.ready_until,'armed':self.ready_until>time.time(),
                'record_command_phase':self.record_phase,'record_state':dict(self.record_state),'own_test':self.own_test,
                'record_task':self.record_task_status(),'device_check':self.device_check_status(),'note_favorite_pending':self.favorite_pending is not None,'note_summary_pending':self.summary_pending is not None,'note_tags_pending':self.tags_pending is not None,'note_tag_creation':True,'note_tag_definitions':True,'note_replace_pending':self.replace_pending is not None,'note_share_pending':self.share_pending is not None,
                'device_name_pending':bool(self.device_name_restore and self.device_name_restore.get('pending')),
                'mutation_in_progress':self.mutation_lock.locked(),'document_read_in_progress':self.document_lock.locked(),
                'pending_restores':[k for k,v in self.restores.items() if v.get('exact_restore_supported',True)],
                'persistent_overrides':[k for k,v in self.restores.items() if not v.get('exact_restore_supported',True)],
                'events':events,'event_cursor':self.event_id,'transcript':self.transcripts.snapshot(),
                'live_diagnostics':self.diagnostics.snapshot(),'rtc':self.rtc.status(),
                'credentials':self.credentials(),
                'mcp_available':self.mcp_root is not None,'mcp_write_available':self.mcp_write_available(),'mcp_login_pending':bool(self.mcp_logins),'dangerous_enabled':self.dangerous_enabled,
                'assistant_builtin_available':self.connection=='connected' and self.channel is not None and self.channel.ready,
                'assistant_custom_configured':(self.state/'assistant.json').is_file(),
                'assistant_configured':self.connection=='connected' or (self.state/'assistant.json').is_file(),
                'assistant_in_progress':self.assistant_task is not None,'assistant_request_id':self.assistant_request_id,
                'assistant_conversation_active':self.assistant_session_id is not None,
                'assistant_recovery_available':self.assistant_recovery_available,'action_timings':self.action_timings,
                'cache':self.cache.stats()}
        if settings_revision!=revision:result.update(catalog)
        return result

    def health(self):
        """No identifiers or values: connection state, booleans and blocker names only, because /healthz needs no cookie."""
        blocking=[]
        if self.own_test:blocking.append('disposable-test-open')
        if self.rtc.active:blocking.append('rtc-active')
        if self.rtc.pending:blocking.append('rtc-cleanup-pending')
        if self.diagnostics.snapshot().get('active'):blocking.append('capture-active')
        if self.mutation_lock.locked():blocking.append('command-in-progress')
        if self.record_task_lock.locked():blocking.append('record-task-read-in-progress')
        if self.document_lock.locked():blocking.append('document-read-in-progress')
        if any(lock.locked() for lock in self.note_locks()):blocking.append('note-operation-in-progress')
        if self.export_lock.locked():blocking.append('note-export-in-progress')
        if self.media_lock.locked():blocking.append('note-media-download-in-progress')
        if self.quota_lock.locked():blocking.append('quota-read-in-progress')
        if self.native_transcript_lock.locked():blocking.append('native-transcript-read-in-progress')
        if self.assistant_task is not None:blocking.append('assistant-request-in-progress')
        if self.favorite_pending is not None:blocking.append('note-favorite-uncertain')
        if self.summary_pending is not None:blocking.append('note-summary-uncertain')
        if self.tags_pending is not None:blocking.append('note-tags-uncertain')
        if self.replace_pending is not None:blocking.append('note-replace-uncertain')
        if self.share_pending is not None:blocking.append('note-share-uncertain')
        if self.device_name_restore and self.device_name_restore.get('pending'):blocking.append('device-name-uncertain')
        return {'connection':self.connection,'worker_running':self.worker_running(),
                'restart_safe':not blocking,'blocking':blocking}

    # Guards and mutations -------------------------------------------------------

    def arm(self,data):
        # Design: one confirmation arms the controls until Disarm, across restarts.
        # The earlier three-statement body is still accepted.
        legacy=all(data.get(key) is True for key in ('physical_ready','disposable_only','device_idle'))
        if data.get('confirm') is not True and not legacy:raise Refused('Confirm that you control this A2')
        self.ready_until=ARMED_UNTIL;private_write(self.armed_path,{'armed':True,'at':time.time()})
        self.emit('readiness',{'armed':True})
        return {'armed':True,'ready_until':self.ready_until,'server_time':time.time()}

    def disarm(self):
        self.ready_until=0;private_write(self.armed_path,{'armed':False,'at':time.time()})
        self.emit('readiness',{'armed':False})
        return {'disarmed':True}

    def require_ready(self,cleanup=False):
        self.rtc.require_clear()
        if not (cleanup and self.own_test) and time.time()>self.ready_until:raise NotReady('Confirm physical readiness first')
        if not self.channel or not self.channel.ready or not self.device:raise Unavailable('The device session is unavailable')

    def require_no_cloud_recording(self):
        state=(self.device_state or {}).get('state')
        if state in ('recording','paused'):raise Refused('The A2 is already recording')
        if state=='idle':return  # a device check is newer evidence than an older task observation
        if task_protocol.blocks_start(self.record_task_status()):
            raise Refused('A cloud recording task is unfinished or uncertain; check the device and refresh task status before starting')

    async def mutate(self,uri,body,cleanup=False):
        self.require_ready(cleanup)
        # Save intent before sending. Never automatically retry an ambiguous write.
        private_write(self.state/'last-command.json',{'uri':uri,'body':body,'time':time.time(),'result':'pending'})
        try:reply=await self.channel._request(uri,body,REFERER)
        except Exception:
            self.emit('command',{'acknowledgement':'unknown','retry_performed':False})
            raise OutcomeUnknown('No reply arrived, so the result is unknown. Check the A2 before sending another command') from None
        private_write(self.state/'last-command.json',{'uri':uri,'body':body,'reply':reply,'time':time.time()})
        if reply.get('code')!=200:raise CloudRejected('The vendor cloud rejected the command')
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False}

    async def record(self,action):
        self.rtc.require_clear()
        if self.mutation_lock.locked():raise Busy('A device command is already in progress')
        if action=='start':
            self.require_ready()
            # Verify the actual A2 first; the last known state may be stale.
            state=(await self.check_device())['state']
            if state=='offline':raise Refused('The A2 is offline')
            if state in ('recording','paused'):raise Refused('The A2 is already recording')
        async with self.mutation_lock:
            self.require_ready(cleanup=action=='stop')
            if action=='start':
                if self.own_test:raise Refused('A recording is still open; stop it or check the device first')
                self.require_no_cloud_recording()
                self.own_test=True
                private_write(self.test_path,{'open':True,'started_at':time.time()})
            elif action not in {'stop','pause','resume'} or not self.own_test:
                raise Refused('The A2 is not recording; check the device first')
            self.record_phase='outcome-unknown'
            self.record_state={'phase':'pending','action':action,'at':time.time()}
            try:result=await self.mutate(RECORD_URI,record_command(self.config,action),cleanup=action=='stop')
            except CloudRejected:
                self.record_phase=action+' rejected by cloud; check device'
                self.record_state={'phase':'rejected','action':action,'at':time.time()}
                raise
            except (OutcomeUnknown,asyncio.CancelledError):
                self.record_state={'phase':'unknown','action':action,'at':time.time()}
                raise
            self.record_phase=action+' acknowledged'
            self.record_state={'phase':'acknowledged','action':action,'at':time.time()}
            # The device check that follows confirms the result (the app ignores task
            # states for 5 s after a start) and closes the recording after a stop.
            self.next_device_check=time.time()+8
            self.emit('command',{'action':action,**result})
            return result

    async def confirm_stopped(self,data):
        self.rtc.require_clear()
        if data.get('physically_stopped') is not True:raise Refused('Confirm that you saw the A2 stop recording')
        async with self.mutation_lock:
            self.own_test=False;private_write(self.test_path,{'open':False})
            self.record_phase='physically stopped (owner confirmation)'
            self.record_state={'phase':'confirmed-stopped','action':'stop','at':time.time()}
        self.emit('command',{'action':'confirm-stopped','owner_confirmed':True})
        return {'confirmed':True}

    async def change_setting(self,key,value,restore=False,persistent_override=False):
        self.rtc.require_clear()
        async with self.mutation_lock:
            self.require_ready()
            await self.refresh()
            if key not in self.settings:raise Refused('This setting is not in the current account or device response')
            if key in notification_protocol.KEYS:raise Refused('Use the alert editor for this setting')
            if key in generation_protocol.KEYS and (not restore or self.generation_restore):
                raise Refused('Use the grouped summary editor for this setting')
            detail=self.setting_details.get(key)
            if restore:
                saved=self.restores.get(key)
                if not saved:raise Refused('No saved original value for this setting')
                if not saved.get('exact_restore_supported',True):raise Refused('No verified override removal exists, so inheritance cannot be restored')
                if self.settings[key] not in (saved['original'],saved['requested']):
                    raise Refused('This setting changed elsewhere; automatic restore refused')
                value=saved['original']
            if not restore:validate_change(detail,value,persistent_override)
            allowed=detail.get('choices',{}) if detail else None
            body=setting_command(self.config,key,value,restore=restore,allowed_values=allowed)
            if not restore:
                original=self.restores.get(key,{}).get('original',self.settings[key])
                exact=self.restores.get(key,{}).get('exact_restore_supported',key in self.writable_keys)
                self.restores[key]={'original':original,'requested':value,'exact_restore_supported':exact}
                private_write(self.setting_restore_path,self.restores)
            result=await self.mutate(SETTING_URI,body)
            if not await self.readback():
                result.update(cloud_readback_matches=None,readback_failed=True)
                return result
            result['cloud_readback_matches']=self.settings.get(key)==value
            if restore and result['cloud_readback_matches']:
                del self.restores[key];private_write(self.setting_restore_path,self.restores)
            return result

    async def change_generation(self,values=None,restore=False,persistent_override=False):
        self.rtc.require_clear()
        async with self.mutation_lock:
            self.require_ready()
            if any(k in self.restores for k in generation_protocol.KEYS):
                raise Refused('Restore the earlier individual summary setting before a group change')
            await self.refresh()
            if restore:
                values=generation_protocol.restore_values(self.setting_details,self.generation_restore)
            else:
                saved=generation_protocol.next_journal(self.setting_details,values,self.generation_restore,persistent_override)
                # Persist all original fields/scopes before a possibly ambiguous or partial batch.
                private_write(self.generation_restore_path,saved)
                self.generation_restore=saved
            body=generation_protocol.command(self.config,values,restore=restore)
            result=await self.mutate(generation_protocol.URI,body)
            if not await self.readback():
                result.update(cloud_readback_matches=None,readback_failed=True)
                return result
            matches={k:self.settings.get(k)==values[k] for k in generation_protocol.KEYS}
            result.update(cloud_readback_matches=all(matches.values()),field_matches=matches)
            if restore and result['cloud_readback_matches']:
                private_write(self.generation_restore_path,None)
                self.generation_restore=None
            return result

    async def refresh_organizations(self):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        self.organization_options=None
        reply=await self.channel.read('organizations',arguments(self.config,'organizations'))
        options=notification_protocol.organizations(reply)
        private_write(self.state/'organizations-result.json',reply)
        self.organization_options=options
        return notification_protocol.descriptor(self.notification_rows,options,self.handle_secret,self.notification_restore)

    async def change_notification(self,method=None,organization_handle=None,restore=False,persistent_override=False):
        self.rtc.require_clear()
        async with self.mutation_lock:
            self.require_ready()
            await self.refresh()
            if restore:
                if self.notification_restore and notification_protocol.ORGANIZATION in self.notification_restore['original']:
                    await self.refresh_organizations()
                changes=notification_protocol.restoration(self.notification_rows,self.notification_restore,self.organization_options)
            else:
                organization=None
                if organization_handle not in (None,''):
                    await self.refresh_organizations()
                    organization=notification_protocol.resolve(self.handle_secret,organization_handle,self.organization_options)
                changes,saved=notification_protocol.prepare(self.notification_rows,method,organization,self.notification_restore,persistent_override)
                if not changes:return {'unchanged':True,'cloud_write_sent':False}
                private_write(self.notification_restore_path,saved);self.notification_restore=saved
            body=notification_protocol.command(self.config,changes,restore=restore)
            result=await self.mutate(notification_protocol.URI,body)
            if not await self.readback():
                result.update(cloud_readback_matches=None,readback_failed=True)
                return result
            matches={k:self.notification_rows.get(k,{}).get('value')==v for k,v in changes.items()}
            result.update(cloud_readback_matches=all(matches.values()),field_matches=matches)
            if restore and result['cloud_readback_matches']:
                private_write(self.notification_restore_path,None);self.notification_restore=None
            return result

    async def check_device(self):
        """Fresh device read plus, when the A2 reports a current recording, that recording's task.
        The recorder state shown and used for commands follows the result."""
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.device_check_lock):
            response=await self.channel.read('devices',arguments(self.config,'devices'))
            record=selected_device(self.config,response)
            self.device_record=record;self.device=public_device(record)
            online=record.get('onlineStatus')==1;fid=recording_fid(record);task=None
            if fid is not None:self.last_fid=fid
            if online and fid is not None:
                try:
                    reply=await self.channel._request(task_protocol.QUERY_URI,task_protocol.query(self.config,fid),REFERER)
                    task=task_protocol.parse_reply(self.config,reply,fid)
                    self.remember_record_task(task,'query')
                except (TimeoutError,ConnectionError,ValueError,TypeError,KeyError):task=None
            self.reconcile_recording(task_protocol.device_state(online,fid,task),online)
        return self.device_check_status()

    def reconcile_recording(self,state,online):
        now=time.time();previous=(self.device_state or {}).get('state')
        self.device_checked_at=now;self.device_state={'state':state,'online':online,'checked_at':now}
        if state!=previous:self.emit('device-check',{'state':state,'online':online})
        # A command in flight, or acknowledged under 8 s ago, is not judged yet (the app waits 5 s).
        if self.mutation_lock.locked() or (self.record_state.get('phase')=='acknowledged' and now-self.record_state['at']<8):return
        if state in ('recording','paused'):
            # Also adopts a recording started with the A2's button, so it can be stopped here.
            if not self.own_test:self.own_test=True;private_write(self.test_path,{'open':True,'started_at':now})
        elif state in ('idle','offline'):
            if self.own_test:self.own_test=False;private_write(self.test_path,{'open':False})
        if state!='unknown':self.record_state={'phase':'checked','action':state,'at':now};self.record_phase=state+' (device check)'

    def check_soon(self):
        """A push reported a change: check in about a second, but not within 10 s of the last check."""
        self.next_device_check=min(self.next_device_check,max(time.time()+1,(self.device_checked_at or 0)+10))

    def schedule_device_check(self):
        self.next_device_check=time.time()+CHECK_SECONDS[0 if self.own_test else 1]
        if self.device_check_task is not None and not self.device_check_task.done():return
        async def run():
            try:await self.check_device()
            except Exception as error:self.emit('device-check',{'state':'failed','error_type':type(error).__name__})
            self.next_device_check=time.time()+CHECK_SECONDS[0 if self.own_test else 1]
        self.device_check_task=asyncio.create_task(run())

    def device_check_status(self):
        return {**(self.device_state or {'state':'unchecked','online':None,'checked_at':None}),
                'battery_percent':(self.device or {}).get('battery_percent'),'in_progress':self.device_check_lock.locked()}

    def remember_record_task(self,task,source):
        if self.record_task is not None and task.order()<self.record_task.order():return
        self.record_task=task;self.record_task_source=source;self.record_task_observed_at=time.time()

    def record_task_status(self):
        return {'available':self.record_task is not None,'source':self.record_task_source,
                'observed_at':self.record_task_observed_at,'read_in_progress':self.record_task_lock.locked(),
                **(self.record_task.summary() if self.record_task is not None else {'physical_state_verified':False})}

    async def refresh_record_task(self):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        if self.record_task is None:raise Refused('No matched recording task has been observed in this host session')
        async with self.queued(self.record_task_lock):
            current=self.record_task;body=task_protocol.query(self.config,current.fid)
            try:reply=await self.channel._request(task_protocol.QUERY_URI,body,REFERER)
            except (TimeoutError,ConnectionError):
                raise Unavailable('The recording task read did not complete; no automatic retry was sent') from None
            private_write(self.state/'record-task-read.json',{'request':body,'reply':reply})
            try:task=task_protocol.parse_reply(self.config,reply,current.fid)
            except (ValueError,TypeError,KeyError):
                raise Unavailable('The recording task reply did not match the observed recording') from None
            # A newer push wins over an older in-flight query, including a change of recording.
            if self.record_task is current:self.remember_record_task(task,'query')
        return self.record_task_status()

    async def interpreter_options(self,scene_type='a1_transcribe'):
        interpreter_protocol.scene(scene_type)
        async with self.interpreter_lock:
            saved=self.interpreter_cache.get(scene_type)
            if saved and time.monotonic()-saved['at']<300:
                options=saved['options']
            else:
                if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
                # A configuration read only: no conference creation, subscription or device command.
                reply=await self.channel._request(interpreter_protocol.CONFIG_URI,
                    interpreter_protocol.config_request(scene_type=scene_type),REFERER)
                private_write(self.state/('interpreter-options-'+scene_type+'.json'),reply)
                options=interpreter_protocol.parse_config(reply)
                self.interpreter_cache[scene_type]={'at':time.monotonic(),'options':options}
            return interpreter_protocol.descriptor(options,self.handle_secret,scene_type)

    async def quotas(self,organization_handle=None):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        if organization_handle is not None and self.organization_options is None:
            raise Refused('Load organization options before selecting enterprise quotas')
        corp_id=notification_protocol.resolve(self.handle_secret,organization_handle,self.organization_options) if organization_handle is not None else None
        organization_label=self.organization_options.get(corp_id) if corp_id is not None else None
        async with self.queued(self.quota_lock):
            requests=quota_protocol.requests(self.config,corp_id)
            key=json.dumps(requests,sort_keys=True)
            saved=self.quota_cache
            if saved and saved['key']==key and time.monotonic()-saved['at']<30:return saved['result']
            result={'read_only':True,'queried_at':time.time(),'organization_handle':organization_handle,'organization_label':organization_label,'sections':{}}
            async def section(name,uri,payload):
                try:
                    reply=await asyncio.wait_for(self.channel._request(uri,payload,REFERER),10)
                    private_write(self.state/('quota-'+name+'.json'),{'request':payload,'reply':reply})
                    return {'available':True,'data':quota_protocol.PARSERS[name](reply)}
                except (TimeoutError,ConnectionError,ValueError,TypeError,KeyError):
                    return {'available':False}
            # The sections are independent reads, so they are sent together.
            async with asyncio.timeout(45):
                sections=await asyncio.gather(*(section(*row) for row in requests))
            result['sections']={row[0]:value for row,value in zip(requests,sections)}
            self.quota_cache={'key':key,'at':time.monotonic(),'result':result} if all(v['available'] for v in result['sections'].values()) else None
            return result

    async def audio_memos(self,limit=20):
        request=memo_protocol.list_request(limit)
        async with self.memo_lock:
            saved=self.memo_cache.get(limit)
            if saved and time.monotonic()-saved['at']<30:return saved['result']
            if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
            reply=await self.channel._request(memo_protocol.LIST_URI,request,REFERER)
            private_write(self.state/'audio-memo-list.json',reply)
            result=memo_protocol.parse_list(reply,limit,keep_ids=True)
            result['items']=[{**{k:v for k,v in row.items() if k!='mission_id'},'handle':self.handle('memo',row['mission_id'])} for row in result['items']]
            self.memo_cache[limit]={'at':time.monotonic(),'result':result}
            return result

    async def documents(self,tool,values):
        if self.mcp_root is None:raise Unavailable('The document reader is not configured on this host')
        root=str(self.mcp_root)
        if root not in sys.path:sys.path.insert(0,root)
        from qwennote_reader import connected,READ_TOOLS
        if tool not in READ_TOOLS or not isinstance(values,dict):raise Refused('Choose one of the read-only document tools')
        state=self.mcp_root/'.private'
        if not state.is_dir():raise Unavailable('The document reader has no authorization on this host yet')
        async with self.queued(self.document_lock):
            fd=os.open(state/'session.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise Busy('Another process is using the document reader; try again shortly') from None
                argfile=state/'web-arguments.json';private_write(argfile,values)
                args=SimpleNamespace(port=18765,login=False,command='read',tool=tool,arguments=str(argfile),ready_timeout=120,login_timeout=300)
                try:result=await connected(args,state)
                except TimeoutError:
                    raise Unavailable('The document read timed out; no automatic retry was sent') from None
                response=result['call']['result']
                if tool=='list_my_minutes':self.remember_notes(response)
                return response
            finally:os.close(fd)

    # Recordings (DeviceFileI) -------------------------------------------------------

    def recording_handle(self,fid):
        return hmac.new(self.handle_secret.encode(),('file:'+fid).encode(),hashlib.sha256).hexdigest()[:24]

    def recording_file(self,handle):
        fid=self.recording_handles.get(handle) if isinstance(handle,str) else None
        if fid is None:raise Refused('Load the recordings list again before using this recording')
        return fid

    async def recordings(self,next_token=None,source='this'):
        """One page of the A2's recordings in the cloud, with opaque handles instead of file ids.
        source 'other' lists recordings of other or earlier devices (DeviceFileI.loadHistory)."""
        if next_token is not None and next_token not in self.recording_pages:raise Refused('Load the recordings list again before using this recording')
        if source not in ('this','other'):raise Refused('Load the recordings list again before using this recording')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        cursor=self.recording_pages.get(next_token,'0') if next_token else '0'
        async with self.queued(self.files_lock):
            try:
                if source=='other':
                    reply=await self.channel._request(extras.HISTORY_URI,extras.history_request(self.config,int(cursor) if next_token else None),REFERER)
                    rows,_,more=device_files.parse_list(reply)
                    cursor=reply['body'].get('nextCursor') if more else None
                    cursor=str(cursor) if isinstance(cursor,int) and not isinstance(cursor,bool) else None
                else:
                    reply=await self.channel._request(device_files.LIST_URI,device_files.list_request(self.config,cursor),REFERER)
                    rows,cursor,more=device_files.parse_list(reply)
            except (ValueError,TypeError,KeyError):raise Unavailable('The recording list could not be read; no automatic retry was sent') from None
        public=[]
        for row in rows:
            handle=self.recording_handle(row['file_id']);self.recording_handles[handle]=row['file_id']
            if row.get('fid'):self.handles[('fid',handle)]=row['fid']
            group=self.handle('group-id',row['group_id']) if row.get('group_id') else None
            public.append({**{k:v for k,v in row.items() if k not in ('file_id','fid','group_id')},'handle':handle,'group':group,'has_fid':bool(row.get('fid'))})
        while len(self.recording_handles)>5000:self.recording_handles.pop(next(iter(self.recording_handles)))
        token=None
        if more and cursor:
            token=secrets.token_urlsafe(24);self.recording_pages[token]=cursor
            while len(self.recording_pages)>64:self.recording_pages.pop(next(iter(self.recording_pages)))
        return {'recordings':public,'has_more':bool(more and cursor),'next_token':token}

    async def recording_audio(self,handle,file_format='wav',wait=False):
        """Save one recording's original audio in protected storage and send it to the browser.
        The download does not depend on incognito (queryFileDownloadInfo has no such check).
        wait turns a still-preparing cloud file into None instead of Unavailable, for polling."""
        fid=self.recording_file(handle)
        if file_format not in device_files.FORMATS:raise Refused('Only WAV audio is available')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.media_lock):
            # Stage names and error types only, so a failed save shows where it stopped.
            journal=self.state/'recording-audio.json';saved={'format':file_format,'phase':'info','started_at':time.time()}
            private_write(journal,saved)
            try:
                reply=await self.channel._request(device_files.DOWNLOAD_URI,device_files.download_request(self.config,fid,file_format),REFERER)
                saved['phase']='parse';ready=device_files.parse_download(reply)
                if ready is None:
                    private_write(journal,{**saved,'phase':'not-ready'})
                    if wait:return None
                    raise Unavailable('The cloud is still preparing this recording; try again in a minute')
                returned=reply['body']['fileDownloadInfoDTO'].get('headers')
                saved.update(phase='download',headers_returned=len(returned) if isinstance(returned,dict) else 0,headers_kept=len(ready[1]))
                result=await note_media.download(ready[0],self.config['user_agent'],self.state/'media',None,ready[1])
            except Unavailable:raise
            except device_files.NoAudio:
                private_write(journal,{**saved,'phase':'no-audio'})
                raise CloudRejected('The cloud keeps no audio file for this recording; incognito recordings lose their audio when transcription finishes, so save it right after the recording stops') from None
            except asyncio.CancelledError:
                private_write(journal,{**saved,'cancelled':True});raise
            except Exception as error:
                private_write(journal,{**saved,'failed':True,'error_type':getattr(error,'error_type',type(error).__name__),'download_stage':getattr(error,'stage',None),
                                       **({'reply_shape':device_files.shape(reply)} if saved['phase']=='parse' else {})})
                if isinstance(error,(ValueError,TypeError,KeyError)):raise Unavailable('The recording could not be saved; no automatic retry was sent') from None
                raise
            private_write(journal,{**saved,'phase':'complete'})
        name='recording-'+handle[:8]+result['path'].suffix
        return web.FileResponse(result['path'],headers={'Content-Disposition':f'attachment; filename="{name}"','Cache-Control':'no-store'})

    async def latest_recording_audio(self):
        """Save audio for the recording the A2 last reported (Recorder → Download original audio).
        A read only, so the page may call it repeatedly; {'ready': False} until the cloud lists the file and has it ready."""
        if self.last_fid is None:raise Refused('No recording was seen while recording; save it from Recordings')
        listed=await self.recordings()
        handle=next((row['handle'] for row in listed['recordings'] if str(self.handles.get(('fid',row['handle'])))==str(self.last_fid)),None)
        if handle is None:return {'ready':False}
        response=await self.recording_audio(handle,'wav',wait=True)
        return {'ready':False} if response is None else response

    async def recording_write(self,uri,body,journal_name,dangerous=False):
        """One recording write (make a note, delete, restore): journaled first, never retried."""
        if dangerous:self.require_dangerous()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.files_lock):
            journal={'action':journal_name,'pending':True,'started_at':time.time()}
            private_write(self.state/'recording-write.json',journal)
            try:reply=await self.channel._request(uri,body,REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The recording change result is unknown; reload the recordings before another change') from None
            private_write(self.state/'recording-write.json',{**journal,'pending':False,'acknowledged':device_files.acknowledged(reply)})
            if not device_files.acknowledged(reply):raise CloudRejected('The cloud did not accept the recording change')
        self.cache.drop('notes-list')
        return {'applied':True}

    # Further native functions (native_extras) ------------------------------------------

    def handle(self,kind,value):
        """An opaque, process-local handle for a private id; the page never sees the id."""
        handle=hmac.new(self.handle_secret.encode(),f'{kind}:{value}'.encode(),hashlib.sha256).hexdigest()[:24]
        self.handles[(kind,handle)]=value
        while len(self.handles)>20000:self.handles.pop(next(iter(self.handles)))
        return handle

    def resolve(self,kind,handle):
        value=self.handles.get((kind,handle)) if isinstance(handle,str) else None
        if value is None:raise Refused('Load the list again before using this item')
        return value

    async def native_read(self,uri,body,parse):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        try:return parse(await self.channel._request(uri,body,REFERER))
        except (ValueError,TypeError,KeyError,AttributeError):raise Unavailable('The cloud read did not complete; no automatic retry was sent') from None

    async def native_write(self,name,uri,body,dangerous=False,check=extras.acknowledged,drops=()):
        """One write: journaled first, never retried."""
        if dangerous:self.require_dangerous()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.extras_lock):
            journal={'action':name,'pending':True,'started_at':time.time()}
            private_write(self.state/'native-write.json',journal)
            try:reply=await self.channel._request(uri,body,REFERER)
            except asyncio.CancelledError:raise
            except Exception:raise OutcomeUnknown('The change result is unknown; reload before another change') from None
            ok=check(reply)
            private_write(self.state/'native-write.json',{**journal,'pending':False,'acknowledged':ok})
            if not ok:raise CloudRejected('The cloud did not accept the change')
        if drops:self.cache.drop(*drops)
        return reply

    async def rename_speaker(self,uuid,current,new):
        """Native speaker rename: every paragraph of the named speaker, as the app's 'all' choice."""
        self.require_listed_note(uuid)
        if not extras.text(current,64) or not extras.text(new,64):raise Refused('Enter the current and the new speaker name, at most 64 characters each')
        async with self.queued(self.replace_lock):
            snapshot=await self._read_replace(uuid)
        body=extras.speaker_request(uuid,snapshot['paragraphs'],current,new)
        if body is None:raise Refused('No paragraph has that speaker name')
        await self.native_write('speaker-rename',extras.SPEAKER_URI,body,drops=('note:'+uuid,))
        return {'renamed':len(body[0]['paragraphIDList'])}

    async def note_markers(self,uuid,add_ms=None):
        self.require_listed_note(uuid)
        if add_ms is not None:
            if type(add_ms) is not int or not 0<=add_ms<=24*3600*1000:raise Refused('Enter a time within the recording')
            await self.native_write('note-marker',extras.NOTE_MARKER_CREATE_URI,extras.note_marker_request(uuid,add_ms))
        return {'markers':await self.native_read(extras.NOTE_MARKERS_URI,[{'taskUuid':uuid}],extras.note_markers)}

    def recording_fid(self,handle):
        file=self.recording_file(handle)
        fid=self.handles.get(('fid',handle))
        if fid is None:raise Refused('This recording has no device recording id, so it cannot hold markers')
        return file,fid

    async def recording_markers(self,handle):
        file,fid=self.recording_fid(handle)
        rows=await self.native_read(extras.DEVICE_NOTES_URI,extras.device_notes_request(self.config,fid,file),extras.device_notes)
        return {'markers':[{**{k:v for k,v in row.items() if k!='note_id'},'handle':self.handle('marker',row['note_id'])} for row in rows]}

    async def recording_marker_save(self,data):
        text=data.get('text')
        if text is not None and not extras.text(text,2000):raise Refused('Enter a marker text of at most 2000 characters')
        if data.get('marker'):
            if text is None:raise Refused('Enter a marker text of at most 2000 characters')
            await self.native_write('marker-update',extras.DEVICE_NOTE_UPDATE_URI,extras.device_note_update(self.config,self.resolve('marker',data['marker']),text))
            return {'saved':True}
        file,fid=self.recording_fid(data.get('handle'))
        at=data.get('time_ms')
        if type(at) is not int or not 0<=at<=24*3600*1000:raise Refused('Enter a time within the recording')
        await self.native_write('marker-create',extras.DEVICE_NOTE_CREATE_URI,extras.device_note_create(self.config,fid,at,text))
        return {'saved':True}

    async def notes_browse(self,mode,query=None,tag=None):
        """Search all notes, favorites, a tag, or the tag list. Listed notes become usable here."""
        if mode=='tags':
            rows=await self.native_read(extras.TABS_URI,[{}],extras.tabs)
            return {'tags':[{'handle':self.handle('tab',tab_id),'name':name} for tab_id,name in rows]}
        if mode=='search':
            if not extras.text(query,100):raise Refused('Enter a search of at most 100 characters')
            uri,body=extras.SEARCH_URI,extras.search_request(query.strip())
        elif mode=='favorites':uri,body=extras.COLLECTED_URI,extras.collected_request()
        elif mode=='tag':uri,body=extras.BY_TAG_URI,extras.by_tag_request(self.resolve('tab',tag))
        else:raise Refused('Choose search, favorites or a tag')
        notes,_=await self.native_read(uri,body,extras.note_list)
        now=time.time()
        for note in notes:self.note_refs.pop(note['uuid'],None);self.note_refs[note['uuid']]=now
        return {'items':notes}

    async def recording_groups(self):
        rows,counts=await self.native_read(extras.GROUPS_URI,extras.groups_request(self.config),extras.groups)
        public=[]
        for row in rows:
            handle=self.handle('group-id',row['group_id']);self.handles[('group',handle)]=row['record']
            public.append({'handle':handle,'name':row['name'],'files':row['files']})
        return {'groups':public,'counts':counts}

    async def recording_group_save(self,name,group=None):
        if not extras.text(name,60):raise Refused('Enter a group name of at most 60 characters')
        if group:await self.native_write('group-rename',extras.GROUP_UPDATE_URI,extras.group_rename(self.config,self.resolve('group',group),name))
        else:await self.native_write('group-create',extras.GROUP_CREATE_URI,extras.group_create(self.config,name))
        return await self.recording_groups()

    async def recording_move(self,handle,group):
        group_id=self.resolve('group-id',group)
        await self.native_write('group-move',extras.GROUP_MOVE_URI,extras.group_move(self.config,[self.recording_file(handle)],group_id))
        return {'moved':True}

    async def device_info(self):
        serv=(self.device_record or {}).get('devServId') if isinstance(self.device_record,dict) else None
        if type(serv) is not int:raise Unavailable('Device details have not been read')
        product=await self.native_read(extras.PRODUCT_URI,extras.product_request(serv),extras.product)
        phones=await self.native_read(extras.CONNECTIONS_URI,extras.connections_request(self.config),extras.connections)
        return {'product':product,'phones':phones}

    # Account settings (slicing, voiceprint) ------------------------------------------

    async def account_settings(self):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        try:
            sliced,voice=await self.read_together((account_settings.SLICE_READ_URI,account_settings.slicing_read()),
                                                  (account_settings.VOICEPRINT_URI,account_settings.voiceprint_read()))
            return {'slicing':{'enabled':account_settings.slicing(sliced)},'voiceprint':account_settings.voiceprint(voice)}
        except (ValueError,TypeError,KeyError,AttributeError):raise Unavailable('Account settings could not be read; no automatic retry was sent') from None

    async def account_setting_write(self,name,uri,body,dangerous=False):
        """One account-setting write: armed controls, journal first, never retried, then a fresh read."""
        if dangerous:self.require_dangerous()
        self.require_ready()
        if self.mutation_lock.locked():raise Busy('A device command is already in progress')
        async with self.mutation_lock:
            journal={'setting':name,'pending':True,'started_at':time.time()}
            private_write(self.state/'account-setting.json',journal)
            try:reply=await self.channel._request(uri,body,REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The setting result is unknown; reload the account settings before another change') from None
            private_write(self.state/'account-setting.json',{**journal,'pending':False,'acknowledged':account_settings.acknowledged(reply)})
            if not account_settings.acknowledged(reply):raise CloudRejected('The cloud did not accept the setting change')
        self.cache.drop('settings')
        return await self.account_settings()

    # Dangerous actions -------------------------------------------------------------

    def set_dangerous(self,data):
        enabled=data.get('enabled') is True
        if enabled and data.get('confirm') is not True:raise Refused('Confirm that you want to enable dangerous actions')
        self.dangerous_enabled=enabled;private_write(self.dangerous_path,{'enabled':enabled,'at':time.time()})
        self.emit('dangerous',{'enabled':enabled})
        return {'dangerous_enabled':enabled}

    def require_dangerous(self):
        if not self.dangerous_enabled:raise Refused('Enable dangerous actions in Dev tools first')

    async def delete_note(self,uuid,confirm):
        """PortalMinutesI.deleteTaskV2. Notes have no recycle bin; this cannot be undone."""
        self.require_dangerous();self.require_listed_note(uuid)
        if confirm!='delete':raise Refused('Type delete to confirm')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.files_lock):
            try:data=await self._read_note_identity(uuid)
            except Exception:raise Unavailable('The note could not be read; no automatic retry was sent') from None
            body=[{'taskId':data['taskId'],'uuid':uuid,'bizType':data.get('businessType')}]
            journal={'action':'note-delete','task_uuid':uuid,'pending':True,'started_at':time.time()}
            private_write(self.state/'note-delete.json',journal)
            try:reply=await self.channel._request(DELETE_NOTE_URI,body,REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The delete result is unknown; reload the notes list before another change') from None
            private_write(self.state/'note-delete.json',{**journal,'pending':False,'acknowledged':note_summary.acknowledged(reply)})
            if not note_summary.acknowledged(reply):raise CloudRejected('The cloud did not delete the note')
        self.note_refs.pop(uuid,None);self.cache.drop('notes-list','note:'+uuid)
        self.emit('note-status',{'uuid':uuid,'event':'deleted'})
        return {'deleted':True}

    async def unbind_device(self,confirm):
        """DeviceI.unbindV3 (server side only). Binding again needs the official app and Bluetooth."""
        self.require_dangerous()
        if confirm!='unbind':raise Refused('Type unbind to confirm')
        if self.mutation_lock.locked() or self.own_test or self.rtc.active:raise Busy('A device command is already in progress')
        if not self.channel or not self.channel.ready or not isinstance(self.device_record,dict):raise Unavailable('The device session is unavailable')
        async with self.mutation_lock:
            body=[{'accountType':self.config['account_type'],'accountId':str(self.config['account_id']),
                   'deviceId':int(self.config['target_device_id']),'devServId':self.device_record.get('devServId'),
                   'requestId':secrets.token_hex(16)}]
            journal={'action':'device-unbind','pending':True,'started_at':time.time()}
            private_write(self.state/'device-unbind.json',journal)
            try:reply=await self.channel._request(UNBIND_URI,body,REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The unbind result is unknown; check the A2 in the official app before anything else') from None
            private_write(self.state/'device-unbind.json',{**journal,'pending':False,'acknowledged':note_summary.acknowledged(reply)})
            if not note_summary.acknowledged(reply):raise CloudRejected('The cloud did not unbind the A2')
        self.emit('device-unbound',{'unbound':True})
        return {'unbound':True}

    async def mcp_login_start(self,access,host,return_to='console'):
        """Authorization URL for MCP read or write access, returning to this console's own port."""
        if self.mcp_root is None:raise Unavailable('The document reader is not configured on this host')
        if return_to not in ('console','registered'):raise Refused('Choose read or write access')
        console=f'http://{host}/mcp/callback'
        try:
            async with ClientSession(timeout=ClientTimeout(total=20)) as session:
                async with session.get(mcp_login.ISSUER+'/.well-known/oauth-authorization-server',allow_redirects=False) as response:
                    meta=await response.json(content_type=None) if response.status==200 else None
                meta=mcp_login.check_metadata(meta)
                info=mcp_login.console_client(self.mcp_root,console) if return_to=='console' else mcp_login.client(self.mcp_root)
                if info is None:
                    # The vendor checks the return address against the registration: register this one once.
                    async with session.post(meta['registration_endpoint'],json=mcp_login.registration(console),allow_redirects=False) as response:
                        registered=await response.json(content_type=None) if response.status in (200,201) else None
                    info=mcp_login.keep_client(self.mcp_root,registered,console)
        except (ClientError,TimeoutError,ValueError):raise Unavailable('The authorization server could not be reached; try again') from None
        redirect=console if return_to=='console' else (info.get('redirect_uris') or [None])[0]
        if not isinstance(redirect,str):raise Unavailable('No MCP client registration on this host; run the document reader login once')
        url,pending=mcp_login.start(meta,info,access,redirect)
        now=time.time()
        self.mcp_logins={k:v for k,v in self.mcp_logins.items() if now-v['created']<mcp_login.PENDING_SECONDS}
        while len(self.mcp_logins)>=4:self.mcp_logins.pop(next(iter(self.mcp_logins)))
        self.mcp_logins[pending['state']]=pending
        return {'url':url,'return_to':return_to}

    async def mcp_login_finish(self,query):
        """Exchange the code from the console callback or a pasted callback address."""
        code,state=mcp_login.callback(query)
        pending=self.mcp_logins.pop(state,None)
        if pending is None or time.time()-pending['created']>mcp_login.PENDING_SECONDS:
            raise Refused('This login is no longer waiting; start it again from Settings')
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.post(pending['token_endpoint'],data=mcp_login.token_request(pending,code),allow_redirects=False) as response:
                    token=await response.json(content_type=None) if response.status==200 else None
        except (ClientError,TimeoutError,ValueError):raise Unavailable('The authorization server returned no usable token; start the login again') from None
        token=mcp_login.check_token(token,pending['access'])
        mcp_login.save(self.mcp_root,pending['access'],token,pending['client'])
        self.credential_cache=None
        self.emit('mcp-login',{'access':pending['access'],'granted':True})
        return {'granted':True,'access':pending['access']}

    def ble_pairing_file(self,confirm):
        """Private pairing file for tools/a2-ble: the A2 serial and device secret from the bound-device
        record, plus a stable client id for this console (the official app uses its own phone's id)."""
        self.require_dangerous()
        if confirm!='pair':raise Refused('Type pair to confirm')
        record=self.device_record if isinstance(self.device_record,dict) else {}
        sn,secret=record.get('sn'),record.get('deviceSecret')
        if not isinstance(sn,str) or not sn or not isinstance(secret,str) or not secret:
            raise Unavailable('Device details have not been read')
        path=self.state/'ble-client.json'
        did=json.loads(path.read_text()).get('did') if path.is_file() and not path.is_symlink() else None
        if not isinstance(did,str) or not re.fullmatch(r'[0-9a-f]{32}',did):
            did=secrets.token_hex(16);private_write(path,{'did':did})
        self.emit('ble-pairing',{'exported':True})
        body=json.dumps({'sn':sn,'device_secret':secret,'did':did,'corp_id':''},ensure_ascii=False,indent=2)
        return web.Response(text=body,content_type='application/json',
                            headers={'Content-Disposition':'attachment; filename="a2-pairing.json"','Cache-Control':'no-store'})

    def mcp_write_available(self):
        return self.mcp_root is not None and (self.mcp_root/'.private'/'write'/'tokens.json').is_file()

    async def mcp_write(self,tool,values):
        """One MCP write tool call on the write grant the user consented to. Journaled first, never retried."""
        if self.mcp_root is None:raise Unavailable('The document reader is not configured on this host')
        if not self.mcp_write_available():
            raise Unavailable('MCP write access is not authorized on this host; run the reader consent for write access first')
        arguments=mcp_write_arguments(tool,values)
        if 'taskUuid' in arguments:self.require_listed_note(arguments['taskUuid'])
        root=str(self.mcp_root)
        if root not in sys.path:sys.path.insert(0,root)
        from qwennote_reader import connected
        state=self.mcp_root/'.private'/'write'
        async with self.queued(self.mcp_write_lock):
            fd=os.open(state/'session.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise Busy('Another process is using the document reader; try again shortly') from None
                argfile=state/'web-arguments.json';private_write(argfile,arguments)
                journal={'tool':tool,'task_uuid':arguments.get('taskUuid'),'pending':True,'started_at':time.time()}
                private_write(self.state/'mcp-write.json',journal)
                args=SimpleNamespace(port=18765,login=False,command='write',write=True,tool=tool,arguments=str(argfile),ready_timeout=120,login_timeout=300)
                try:result=await connected(args,state)
                except asyncio.CancelledError:raise
                except ValueError:raise Refused('The MCP server does not offer this write tool to the current grant') from None
                except Exception:
                    raise OutcomeUnknown('The MCP write result is unknown; reload the note before another change') from None
                response=result['call']['result']
                private_write(self.state/'mcp-write.json',{**journal,'pending':False,'is_error':bool(response.get('isError')),'finished_at':time.time()})
                if response.get('isError'):raise CloudRejected('The MCP server rejected the change')
                return {'tool':tool,'applied':True,'result':response}
            finally:os.close(fd)

    def remember_notes(self,response,at=None):
        at=time.time() if at is None else at
        for uuid in note_favorites.listed_notes(response):
            self.note_refs.pop(uuid,None);self.note_refs[uuid]=at
        while len(self.note_refs)>5000:self.note_refs.pop(next(iter(self.note_refs)))

    def remember_cached_notes(self):
        """Notes from the saved list reads (kept for a day), so a restart or a long-open page
        does not make every note action ask for the list again."""
        for key,entry in sorted(self.cache.entries.items(),key=lambda item:item[1]['at']):
            if key.startswith('documents:') and '"list_my_minutes"' in key and time.time()-entry['at']<=DAY:
                self.remember_notes(entry['value'],entry['at'])

    async def _read_favorite(self,uuid):
        try:
            reply=await self.channel._request(note_favorites.DETAIL_URI,note_favorites.detail_request(uuid),REFERER)
            return note_favorites.collected(reply,uuid)
        except Exception:
            raise Unavailable('Favorite state could not be verified; no automatic retry was sent') from None

    def require_listed_note(self,uuid):
        # Note actions accept only notes from the user's own list, seen within a day.
        if note_favorites.valid_uuid(uuid) and uuid not in self.note_refs:self.remember_cached_notes()
        if not note_favorites.valid_uuid(uuid) or time.time()-self.note_refs.get(uuid,float('-inf'))>DAY:
            raise Refused('Load this note from your own notes list before using application actions')

    async def favorite(self,uuid,desired=None,expected=None):
        self.require_listed_note(uuid)
        if desired is not None and (type(desired) is not bool or type(expected) is not bool):
            raise Refused('Favorite state must be an explicit boolean with the previously displayed value')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.favorite_lock):
            if desired is not None and self.favorite_pending is not None:
                raise Refused('A favorite change awaits verification; refresh that note before another change')
            current=await self._read_favorite(uuid)
            if desired is None:
                if self.favorite_pending and self.favorite_pending.get('task_uuid')==uuid:
                    saved={**self.favorite_pending,'pending':False,'observed':current,'verified_at':time.time()}
                    private_write(self.favorite_path,saved);self.favorite_pending=None
                return {'collected':current,'verified':True,'changed':False}
            if current!=expected:raise Refused('The favorite changed elsewhere; refresh it before changing it again')
            if current==desired:return {'collected':current,'verified':True,'changed':False}
            saved={'task_uuid':uuid,'original':current,'requested':desired,'pending':True,'started_at':time.time()}
            private_write(self.favorite_path,saved);self.favorite_pending=saved
            try:
                reply=await self.channel._request(note_favorites.COLLECT_URI,note_favorites.collect_request(uuid,desired),REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The favorite result is unknown; refresh its state before any further change') from None
            if not note_favorites.acknowledged(reply):
                raise CloudRejected('The favorite change was not acknowledged; refresh its state before any further change')
            saved['cloud_acknowledged']=True;private_write(self.favorite_path,saved)
            observed=await self._read_favorite(uuid)
            private_write(self.favorite_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()})
            self.favorite_pending=None
            return {'collected':observed,'verified':True,'changed':observed!=current,
                    'cloud_acknowledged':True,'readback_matches':observed==desired}

    async def _read_summary(self,uuid):
        try:
            detail,brief=await self.read_together((note_summary.DETAIL_URI,note_favorites.detail_request(uuid)),(note_summary.READ_URI,[uuid]))
            state=note_status.processing((detail.get('body') or {}).get('data') if isinstance(detail,dict) else None)
            if state is None and note_summary.acknowledged(brief) and note_status.brief_generating(brief):state='generating'
            if state is not None:return {'processing':state}
            if note_summary.acknowledged(brief) and note_status.summary_empty(brief):
                return {'empty':True,'summary_failed':note_status.summary((detail.get('body') or {}).get('data'))=='failed'}
            return note_summary.parse(detail,brief,uuid)
        except note_summary.Unsupported:
            raise Refused('Summary editing currently supports default Markdown summaries without images or rich content') from None
        except Exception:
            raise Unavailable('The complete summary could not be verified; no automatic retry was sent') from None

    async def summary(self,uuid,text=None,expected=None):
        self.require_listed_note(uuid)
        if text is not None:
            try:note_summary.validate_text(text)
            except (ValueError,UnicodeError):
                raise Refused('Enter a nonempty Markdown summary up to 48 KiB without images or rich content') from None
            if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
                raise Refused('Load the current summary before saving')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.summary_lock):
            if text is not None and self.summary_pending:
                raise Refused('A summary save awaits verification; refresh that summary before saving again')
            current=await self._read_summary(uuid)
            if 'processing' in current:
                if text is not None:raise Refused('This note is still being processed in the cloud; save after it finishes')
                return {'text':None,'processing':current['processing'],'verified':False,'pending':self.summary_pending is not None}
            if current.get('empty'):
                if text is not None:raise Refused('This note has no summary to edit; regenerate it first')
                return {'text':'','empty':True,'summary_failed':current['summary_failed'],'revision':None,'verified':True,'pending':self.summary_pending is not None}
            if text is None:
                pending=self.summary_pending
                if pending and pending.get('task_uuid')==uuid:
                    original=pending['original']
                    same_selection=current['key']==original['key'] and current['self_model']==original['self_model']
                    if pending.get('phase')=='awaiting-final-summary':
                        resolved=same_selection and current['text']!=original['text']
                        if resolved:pending['requested']=current['text']
                    else:
                        resolved=same_selection and current['text'] in (original['text'],pending['requested'])
                    if resolved:
                        private_write(self.summary_path,{**pending,'pending':False,'observed':current,'verified_at':time.time()})
                        self.summary_pending=None
                return {'text':current['text'],'revision':note_summary.revision(current),'verified':True,'pending':self.summary_pending is not None}
            if note_summary.revision(current)!=expected:
                raise Refused('The summary changed elsewhere; refresh it before saving')
            if current['text']==text:return {'text':text,'revision':expected,'verified':True,'pending':False,'changed':False}
            saved={'task_uuid':uuid,'original':current,'requested':text,'pending':True,'started_at':time.time()}
            private_write(self.summary_path,saved);self.summary_pending=saved
            try:
                reply=await self.channel._request(note_summary.WRITE_URI,note_summary.request(uuid,current,text),REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The summary save result is unknown; refresh before another save') from None
            if not note_summary.acknowledged(reply):
                raise CloudRejected('The summary save was not acknowledged; refresh before another save')
            saved['cloud_acknowledged']=True;private_write(self.summary_path,saved)
            observed=await self._read_summary(uuid)
            if observed['text']!=text or observed['self_model']!=current['self_model']:
                raise OutcomeUnknown('The summary readback differs; refresh before another save')
            private_write(self.summary_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()})
            self.summary_pending=None
            return {'text':observed['text'],'revision':note_summary.revision(observed),'verified':True,'pending':False,'changed':True}

    async def regenerate(self,uuid,expected):
        self.require_listed_note(uuid)
        if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
            raise Refused('Load the current summary before regenerating')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.summary_lock):
            if self.summary_pending:
                raise Refused('A summary change awaits verification; refresh that summary before regenerating')
            current=await self._read_summary(uuid)
            if note_summary.revision(current)!=expected:
                raise Refused('The summary changed elsewhere; refresh it before regenerating')
            saved={'operation':'regenerate','task_uuid':uuid,'original':current,'requested':None,'pending':True,'phase':'manual','started_at':time.time()}
            private_write(self.summary_path,saved);self.summary_pending=saved
            try:
                manual=await self.channel._request(note_summary.MANUAL_REGENERATE_URI,note_summary.manual_regenerate_request(uuid),{'referer':'https://n.qwennote.cn/qwennote/minutes/TemplateSelector.html'})
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The regeneration result is unknown; refresh the summary before another change') from None
            if not note_summary.acknowledged(manual):
                raise CloudRejected('Regeneration was not acknowledged; refresh the summary before another change')
            saved['manual_acknowledged']=True;saved['manual_reply']=manual;saved['phase']='generate';private_write(self.summary_path,saved)
            try:
                reply=await self.channel._request(note_summary.REGENERATE_URI,note_summary.regenerate_request(uuid),REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The regeneration result is unknown; refresh the summary before another change') from None
            if not note_summary.acknowledged(reply):
                raise CloudRejected('Regeneration was not acknowledged; refresh the summary before another change')
            saved['cloud_acknowledged']=True;saved['reply']=reply;private_write(self.summary_path,saved)
            try:text=note_summary.regenerated_content(reply)
            except (ValueError,UnicodeError):
                raise OutcomeUnknown('Regeneration returned an unsupported or incomplete result; refresh the summary before another change') from None
            saved['requested']=text;private_write(self.summary_path,saved)
            if text is None:
                saved['phase']='awaiting-final-summary';private_write(self.summary_path,saved)
                return {'text':current['text'],'revision':expected,'verified':False,'pending':True,'accepted':True,'changed':None,'cloud_acknowledged':True}
            deadline=time.monotonic()+45;polls=0
            while True:
                polls+=1
                try:
                    read=await self.channel._request(note_summary.READ_URI,[uuid],REFERER)
                    cloud_text=note_summary.read_regenerated(read,uuid)
                except asyncio.CancelledError:raise
                except Exception:
                    raise OutcomeUnknown('The regenerated summary readback differs; refresh before another change') from None
                if cloud_text==text:break
                elif cloud_text is not None and cloud_text!=current['text']:
                    raise OutcomeUnknown('The regenerated summary readback differs; refresh before another change')
                if time.monotonic()>=deadline:
                    raise OutcomeUnknown('The regenerated summary readback differs; refresh before another change')
                await asyncio.sleep(2)
            observed=await self._read_summary(uuid)
            if observed['text']!=text or observed['self_model']!=current['self_model']:
                raise OutcomeUnknown('The regenerated summary readback differs; refresh before another change')
            private_write(self.summary_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()})
            self.summary_pending=None
            return {'text':observed['text'],'revision':note_summary.revision(observed),'verified':True,'pending':False,
                    'changed':observed['text']!=current['text'],'cloud_acknowledged':True,'readback_matches':True,'readback_attempts':polls}

    async def _read_share(self,uuid):
        try:
            reply=await self.channel._request(note_sharing.READ_URI,note_sharing.read_request(uuid),REFERER)
            return note_sharing.parse(reply)
        except Exception:
            raise Unavailable('The note sharing scope could not be verified; no automatic retry was sent') from None

    async def share(self,uuid,target=None,expected=None,password=None):
        self.require_listed_note(uuid)
        writing=target is not None
        if password not in (None,'') and (target!='public' or not note_sharing.valid_password(password)):
            raise Refused('Use 4 to 16 letters and digits for the share password, with public access')
        if writing and (target not in note_sharing.TARGETS or not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected)):
            raise Refused('Choose private or public access and load the current sharing scope first')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.share_lock):
            if writing and self.share_pending is not None:
                raise Refused('A sharing change awaits verification; refresh that note before another change')
            current=await self._read_share(uuid)
            if not writing:
                saved=self.share_pending
                if saved and saved.get('task_uuid')==uuid and note_sharing.resolved(current,saved):
                    private_write(self.share_path,{**saved,'pending':False,'observed':current,'verified_at':time.time()})
                    self.share_pending=None
                return note_sharing.public(self.handle_secret,current,self.share_pending is not None)
            if note_sharing.revision(self.handle_secret,current)!=expected:
                raise Refused('The sharing scope changed elsewhere; refresh it before saving')
            if note_sharing.access(current)==target and password is None:
                return {**note_sharing.public(self.handle_secret,current),'changed':False}
            try:request=note_sharing.save_request(uuid,current,target,password)
            except ValueError:raise Refused('This sharing change is not available for the selected note') from None
            saved={'task_uuid':uuid,'original':current,'requested':target,'request':request,'pending':True,'started_at':time.time()}
            private_write(self.share_path,saved);self.share_pending=saved
            try:reply=await self.channel._request(note_sharing.WRITE_URI,request,REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The sharing result is unknown; refresh its state before any further change') from None
            if not note_sharing.acknowledged(reply):
                raise CloudRejected('The sharing change was not acknowledged; refresh its state before any further change')
            saved['cloud_acknowledged']=True;private_write(self.share_path,saved)
            observed=await self._read_share(uuid)
            if not note_sharing.verified(current,observed,request):
                raise OutcomeUnknown('The sharing readback differs; refresh before another change')
            private_write(self.share_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()})
            self.share_pending=None
            return {**note_sharing.public(self.handle_secret,observed),'changed':True,'cloud_acknowledged':True,'readback_matches':True}

    async def share_url(self,uuid):
        self.require_listed_note(uuid)
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.share_lock):
            if self.share_pending is not None:
                raise Refused('A sharing change awaits verification; refresh that note before copying its URL')
            current=await self._read_share(uuid)
            if note_sharing.access(current) not in note_sharing.TARGETS:
                raise Refused('Copy URLs only for private or public note scopes')
            try:
                reply=await self.channel._request(note_sharing.DETAIL_URI,note_favorites.detail_request(uuid),REFERER)
                url=note_sharing.share_url(reply,uuid)
            except Exception:
                raise Unavailable('Share URL verification failed. Retry manually') from None
            if not url:raise Refused('The application returned an empty share URL for this note')
            return {'url':url,'access':note_sharing.access(current),'sharing_enabled':current['sharing_enabled']}

    async def _read_tags(self,uuid):
        try:
            detail,available,assigned=await self.read_together((note_tags.DETAIL_URI,note_favorites.detail_request(uuid)),
                                                               (note_tags.LIST_URI,[{'requestId':''}]),(note_tags.READ_URI,[{'taskUuid':uuid}]))
            return note_tags.parse(detail,available,assigned,uuid)
        except Exception:
            raise Unavailable('The complete tag list could not be verified; no automatic retry was sent') from None

    async def tags(self,uuid,selection=None,expected=None,*,create_name=None,rename_handle=None,rename_name=None,rename_auto_match=None,delete_handle=None):
        self.require_listed_note(uuid)
        operations=[selection is not None,create_name is not None,rename_handle is not None or rename_name is not None or rename_auto_match is not None,delete_handle is not None]
        if sum(operations)>1 or (rename_handle is None)!=(rename_name is None) or (rename_auto_match is not None and rename_handle is None):raise Refused('Choose exactly one tag operation')
        writing=any(operations)
        if writing and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected)):
            raise Refused('Load the current tag list before saving')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.tags_lock):
            if writing and self.tags_pending:
                raise Refused('A tag save awaits verification; refresh that note before saving again')
            current=await self._read_tags(uuid)
            if not writing:
                saved=self.tags_pending
                if saved and saved.get('task_uuid')==uuid:
                    if saved.get('operation')=='create':matched=note_tags.created_tag(saved,current)
                    elif saved.get('operation') in ('rename','delete'):matched=note_tags.definition_reconciliation(saved,current) is not None
                    else:matched=current['selected'] in (saved['original']['selected'],saved['requested'])
                    if matched:
                        private_write(self.tags_path,{**saved,'pending':False,'observed':current,'verified_at':time.time()});self.tags_pending=None
                return note_tags.public(self.handle_secret,current,self.tags_pending is not None)
            if note_tags.revision(current)!=expected:
                raise Refused('The tags changed elsewhere; refresh them before saving')
            if create_name is not None:
                try:body=note_tags.create_request(create_name)
                except (ValueError,UnicodeError):raise Refused('Enter a tag name of at most 15 UTF-16 units without control characters') from None
                name=body[0]['displayName']
                if len(current['tags'])>=note_tags.MAX_TAGS:raise Refused('The tag catalog is too large to create another tag here')
                if any(tag['label'].strip()==name for tag in current['tags'].values()):raise Refused('A tag with this name already exists; select it from the list')
                saved={'operation':'create','task_uuid':uuid,'original':current,'name':name,'pending':True,'started_at':time.time()}
                private_write(self.tags_path,saved);self.tags_pending=saved
                try:reply=await self.channel._request(note_tags.CREATE_URI,body,REFERER)
                except asyncio.CancelledError:raise
                except Exception:raise OutcomeUnknown('Tag creation is uncertain; refresh before another tag action') from None
                if not note_summary.acknowledged(reply):raise CloudRejected('Tag creation was not acknowledged; refresh before another tag action')
                created=reply.get('body',{}).get('tagId') if isinstance(reply.get('body'),dict) else None
                if not note_favorites.valid_uuid(created) or created in current['tags']:
                    raise OutcomeUnknown('The tag creation receipt could not be verified; manual review is required')
                saved['created_id']=created;private_write(self.tags_path,saved)
                observed=await self._read_tags(uuid)
                if not note_tags.created_tag(saved,observed):raise OutcomeUnknown('The created tag could not be verified; refresh before another tag action')
                private_write(self.tags_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()});self.tags_pending=None
                result=note_tags.public(self.handle_secret,observed)
                return {**result,'created_handle':note_tags.handle(self.handle_secret,'note-tag:'+uuid+':'+created)}
            if rename_handle is not None or delete_handle is not None:
                try:tag_id=note_tags.resolve(self.handle_secret,current,[rename_handle if rename_handle is not None else delete_handle])[0]
                except ValueError:raise Refused('Choose a current tag option from this note') from None
                operation='rename' if rename_handle is not None else 'delete';before=current['tags'][tag_id]
                if operation=='delete' and tag_id in current['selected']:
                    raise Refused('Remove this tag from the open note before deleting it')
                if operation=='rename':
                    try:name=note_tags.create_request(rename_name)[0]['displayName']
                    except (ValueError,UnicodeError):raise Refused('Enter a tag name of at most 15 UTF-16 units without control characters') from None
                    if any(key!=tag_id and tag['label'].strip()==name for key,tag in current['tags'].items()):
                        raise Refused('A tag with this name already exists; select it from the list')
                    requested_auto=before['auto_match'] if rename_auto_match is None else rename_auto_match
                    if type(requested_auto) is not bool:raise Refused('Automatic matching must be true or false')
                    if name==before['label'] and requested_auto==before['auto_match']:return {**note_tags.public(self.handle_secret,current),'changed':False}
                    try:
                        detail=await self.channel._request(note_tags.GET_DEF_URI,note_tags.tag_request(tag_id),REFERER)
                        preserved=note_tags.definition(detail,tag_id,before)
                        body=note_tags.rename_request(tag_id,name,requested_auto,preserved['description'])
                    except Refused:raise
                    except Exception:raise Unavailable('The tag definition could not be verified; no rename was sent') from None
                    uri=note_tags.UPDATE_URI;saved={'operation':'rename','task_uuid':uuid,'original':current,'tag_id':tag_id,'name':name,'auto_match':requested_auto,'pending':True,'started_at':time.time()}
                else:
                    uri=note_tags.DELETE_URI;body=note_tags.tag_request(tag_id)
                    saved={'operation':'delete','task_uuid':uuid,'original':current,'tag_id':tag_id,'pending':True,'started_at':time.time()}
                private_write(self.tags_path,saved);self.tags_pending=saved
                try:reply=await self.channel._request(uri,body,REFERER)
                except asyncio.CancelledError:raise
                except Exception:raise OutcomeUnknown('The tag definition change is uncertain; refresh before another tag action') from None
                if not note_summary.acknowledged(reply):raise CloudRejected('The tag definition change was not acknowledged; refresh before another tag action')
                saved['cloud_acknowledged']=True;private_write(self.tags_path,saved)
                observed=await self._read_tags(uuid)
                if note_tags.definition_reconciliation(saved,observed)!='requested':
                    raise OutcomeUnknown('The tag definition readback differs; refresh before another tag action')
                private_write(self.tags_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()});self.tags_pending=None
                return {**note_tags.public(self.handle_secret,observed),'changed':True}
            try:desired=note_tags.resolve(self.handle_secret,current,selection)
            except ValueError:raise Refused('Choose current tag options from this note without duplicates') from None
            if desired==current['selected']:return {**note_tags.public(self.handle_secret,current),'changed':False}
            saved={'task_uuid':uuid,'original':current,'requested':desired,'pending':True,'started_at':time.time()}
            private_write(self.tags_path,saved);self.tags_pending=saved
            try:reply=await self.channel._request(note_tags.WRITE_URI,note_tags.request(uuid,desired),REFERER)
            except asyncio.CancelledError:raise
            except Exception:
                raise OutcomeUnknown('The tag save result is unknown; refresh before another save') from None
            if not note_summary.acknowledged(reply):
                raise CloudRejected('The tag save was not acknowledged; refresh before another save')
            saved['cloud_acknowledged']=True;private_write(self.tags_path,saved)
            observed=await self._read_tags(uuid)
            if observed['selected']!=desired:
                raise OutcomeUnknown('The tag readback differs; refresh before another save')
            private_write(self.tags_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()});self.tags_pending=None
            return {**note_tags.public(self.handle_secret,observed),'changed':True}

    async def _read_note_identity(self,uuid,display=False):
        if display:
            detail=await self.cloud_read(note_favorites.DETAIL_URI,note_favorites.detail_request(uuid),tags=('note:'+uuid,),
                                         final=lambda reply:note_status.processing((reply.get('body') or {}).get('data'))is None)
        else:detail=await self.channel._request(note_favorites.DETAIL_URI,note_favorites.detail_request(uuid),REFERER)
        data=detail.get('body',{}).get('data')
        if not note_summary.acknowledged(detail) or not isinstance(data,dict) or data.get('uuid')!=uuid or type(data.get('taskId')) is not int:
            raise ValueError('Note identity mismatch')
        return data

    async def translate_note(self,uuid,target=None,next_token=None):
        self.require_listed_note(uuid)
        if next_token is not None and (target is not None or not isinstance(next_token,str) or not 32<=len(next_token)<=128):
            raise Refused('Choose a listed note and an issued translation continuation')
        if target is not None and not note_translate.valid_language(target):raise Refused('Choose a supported translation language')
        saved=self.translation_pages.get(next_token) if next_token else None
        if next_token and (not saved or saved['task_uuid']!=uuid or time.monotonic()-saved['at']>900):
            raise Refused('This translation page is no longer available; reload the translation')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.native_transcript_lock):
            try:
                async with asyncio.timeout(30):
                    data=await self._read_note_identity(uuid,display=True);modified=data.get('gmtModified')
                    if type(modified) is not int or modified<0:raise ValueError('Missing translation revision')
                    if saved:
                        if saved['modified']!=modified:raise Refused('The note changed; reload the translation')
                        source=saved['source'];target=saved['target'];cursor=saved['cursor'];targets=None
                    else:
                        original=await self.cloud_read(note_replace.READ_URI,note_replace.read_request(uuid),tags=('note:'+uuid,))
                        original_items,_=note_replace.page(original)
                        if any(type(item.get('taskId')) is not int or item['taskId']!=data['taskId'] for item in original_items):raise ValueError('Transcript identity mismatch')
                        source=note_translate.source_language(original);cursor=None
                        options=await self.cloud_read(note_translate.OPTIONS_URI,note_translate.options_request(uuid),WEEK,('note:'+uuid,))
                        targets=note_translate.language_options(options,source)
                        if target is None:return {'source_language':source,'targets':targets}
                        if target not in {row['key'] for row in targets}:raise Refused('Choose a language returned for this note')
                    reply=await self.cloud_read(note_translate.TRANSCRIPT_URI,note_translate.transcript_request(uuid,source,target,cursor),tags=('note:'+uuid,))
                    items,more=note_replace.page(reply)
                    if note_translate.source_language(reply)!=target:raise ValueError('Translation language mismatch')
                    if cursor and items and items[0]['uuid']==cursor['uuid']:items=items[1:]
                    if not items or any(type(item.get('taskId')) is not int or item['taskId']!=data['taskId'] for item in items):raise ValueError('Translation identity mismatch')
                    if cursor and items[0]['startTime']<=cursor['startTime']:raise ValueError('Translation cursor did not advance')
                    token=None
                    if more:
                        last=items[-1]
                        if cursor and (last['uuid']==cursor['uuid'] or last['startTime']<=cursor['startTime']):raise ValueError('Translation cursor did not advance')
                        token=secrets.token_urlsafe(32);self.translation_pages[token]={'task_uuid':uuid,'cursor':last,'modified':modified,'source':source,'target':target,'at':time.monotonic()}
                        while len(self.translation_pages)>64:self.translation_pages.pop(next(iter(self.translation_pages)))
                    translated=note_translate.summary_result(await self.cloud_read(note_translate.SUMMARY_URI,note_translate.summary_request(uuid,source,target),tags=('note:'+uuid,)),uuid) if not saved else {'title':None,'summary':None,'keypoints':[],'actions':[]}
                    return {**note_replace.view_page(items),**translated,'source_language':source,'target_language':target,'has_more':more,'next_token':token,'source':'native-translation'}
            except Refused:raise
            except Exception:raise Unavailable('The note translation could not be read; no automatic retry was sent') from None

    async def native_transcript(self,uuid,next_token=None):
        self.require_listed_note(uuid)
        if next_token is not None and (not isinstance(next_token,str) or not 32<=len(next_token)<=128):
            raise Refused('Choose a listed note and an issued transcript continuation')
        saved=self.native_transcript_pages.get(next_token) if next_token else None
        if next_token and (not saved or saved['task_uuid']!=uuid or time.monotonic()-saved['at']>900):
            raise Refused('This transcript page is no longer available; reload the transcript')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.native_transcript_lock):
            try:
                async with asyncio.timeout(20):
                    data=await self._read_note_identity(uuid,display=True)
                    state=note_status.transcription(data)
                    if state in ('recording','transcribing','failed'):
                        return {'paragraphs':[],'has_more':False,'next_token':None,'source':'native','processing':state}
                    modified=data.get('gmtModified')
                    if type(modified) is not int or modified<0:raise ValueError('Missing transcript revision')
                    if saved and saved['modified']!=modified:raise Refused('The transcript changed; reload it before continuing')
                    cursor=saved['cursor'] if saved else None
                    reply=await self.cloud_read(note_replace.READ_URI,note_replace.read_request(uuid,cursor),tags=('note:'+uuid,))
                    private_write(self.state/'native-transcript-page.json',{'task_uuid':uuid,'cursor':cursor,'reply':reply})
                    items,more=note_replace.page(reply)
                    if any(type(item.get('taskId')) is not int or item['taskId']!=data['taskId'] for item in items):raise ValueError('Transcript identity mismatch')
                    if cursor and (items[0]['startTime']<=cursor['startTime'] or items[0]['uuid']==cursor['uuid']):raise ValueError('Transcript cursor did not advance')
                    token=None
                    if more:
                        last=items[-1]
                        if cursor and (last['uuid']==cursor['uuid'] or last['startTime']<=cursor['startTime']):raise ValueError('Transcript cursor did not advance')
                        token=secrets.token_urlsafe(32)
                        self.native_transcript_pages[token]={'task_uuid':uuid,'cursor':last,'modified':modified,'at':time.monotonic()}
                        while len(self.native_transcript_pages)>64:self.native_transcript_pages.pop(next(iter(self.native_transcript_pages)))
                    return {**note_replace.view_page(items),'has_more':more,'next_token':token,'source':'native'}
            except Refused:raise
            except Exception:raise Unavailable('The native transcript could not be read; no automatic retry was sent') from None

    async def _read_replace(self,uuid):
        try:
            async with asyncio.timeout(40):
                data=await self._read_note_identity(uuid)
                rows=[];cursor=None
                for page_index in range(10):
                    reply=await self.channel._request(note_replace.READ_URI,note_replace.read_request(uuid,cursor),REFERER)
                    items,more=note_replace.page(reply)
                    if any(type(item.get('taskId')) is not int or item['taskId']!=data['taskId'] for item in items):raise ValueError('Transcript identity mismatch')
                    rows.extend(items);current=note_replace.snapshot(uuid,rows)
                    if not more:return current
                    last=items[-1]
                    if cursor and (last['uuid']==cursor['uuid'] or last['startTime']<=cursor['startTime']):raise ValueError('Transcript cursor did not advance')
                    cursor=last
                raise ValueError('Transcript page limit')
        except Exception:
            raise Unavailable('The complete native transcript could not be verified; no automatic retry was sent') from None

    async def replace_note(self,uuid,needle,replacement,expected=None):
        self.require_listed_note(uuid)
        try:note_replace.validate_terms(needle,replacement)
        except (ValueError,UnicodeError):raise Refused('Enter nonempty find text and replacement text of at most 1000 characters each') from None
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected)):
            raise Refused('Preview the replacement before saving')
        self.rtc.require_clear()
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.replace_lock):
            if expected is not None and self.replace_pending:
                raise Refused('A replacement awaits verification; preview that note before saving again')
            current=await self._read_replace(uuid)
            saved=self.replace_pending
            if expected is None and saved and saved.get('task_uuid')==uuid:
                if note_replace.semantic(current) in (note_replace.semantic(saved['original']),note_replace.semantic(saved['desired'])):
                    private_write(self.replace_path,{**saved,'pending':False,'observed':current,'verified_at':time.time()});self.replace_pending=None
            if expected is not None and note_replace.revision(current)!=expected:
                raise Refused('The transcript changed elsewhere; preview the replacement again')
            try:request,matches=note_replace.plan(current,needle,replacement)
            except (ValueError,KeyError,TypeError):
                raise Refused('Replacement requires consistent timed text, no marks in changed paragraphs, at most 20 changed paragraphs and 1000 matches') from None
            if expected is None:return note_replace.preview(current,request,matches,self.replace_pending is not None)
            if not matches:return {'changed':False,'matches':0,'verified':True,'pending':False}
            desired=note_replace.desired(current,request)
            saved={'task_uuid':uuid,'original':current,'desired':desired,'request':request,'pending':True,'started_at':time.time()}
            private_write(self.replace_path,saved);self.replace_pending=saved
            try:reply=await self.channel._request(note_replace.WRITE_URI,request,REFERER)
            except asyncio.CancelledError:raise
            except Exception:raise OutcomeUnknown('The replacement result is unknown; preview before another save') from None
            if not note_summary.acknowledged(reply):raise CloudRejected('The replacement was not acknowledged; preview before another save')
            saved['cloud_acknowledged']=True;private_write(self.replace_path,saved)
            observed=await self._read_replace(uuid)
            if note_replace.semantic(observed)!=note_replace.semantic(desired):
                raise OutcomeUnknown('The replacement readback differs; preview before another save')
            private_write(self.replace_path,{**saved,'pending':False,'observed':observed,'verified_at':time.time()});self.replace_pending=None
            return {'changed':True,'matches':matches,'paragraphs_changed':len(request[0]['modifyParagraphs']),'verified':True,'pending':False}

    async def export_note(self,uuid,kind,target=None,bilingual=False,file_format='txt'):
        self.require_listed_note(uuid)
        if not isinstance(kind,str) or kind not in note_export.KINDS:raise Refused('Choose transcript or summary export')
        try:note_export.format_spec(kind,file_format)
        except ValueError:raise Refused('Choose a supported export format for this note type') from None
        if type(bilingual) is not bool or (target is None and bilingual) or (target is not None and not note_translate.valid_language(target)):
            raise Refused('Choose a supported target language and bilingual export option')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        if target is not None:
            options=await self.translate_note(uuid)
            if target not in {row['key'] for row in options['targets']}:raise Refused('Choose a language returned for this note')
        async with self.queued(self.export_lock):
            saved={'task_uuid':uuid,'kind':kind,'target_language':target,'bilingual':bilingual,'file_format':file_format,'started_at':time.time(),'phase':'requested'}
            journal=self.state/'note-export.json';private_write(journal,saved)
            try:
                async with asyncio.timeout(40):
                    reply=await self.channel._request(note_export.URI,note_export.request(uuid,kind,target,bilingual,file_format),REFERER)
                    saved.update(phase='download',reply=reply);private_write(journal,saved)
                    url=note_export.receipt(reply,kind)
                    data=await note_export.download(url,kind,self.config['user_agent'],file_format)
            except asyncio.CancelledError:
                private_write(journal,{**saved,'phase':'cancelled'});raise
            except Exception as error:
                private_write(journal,{**saved,'phase':'failed','error_type':type(error).__name__})
                raise Unavailable('Export could not be retrieved; no automatic retry was sent') from None
            private_write(journal,{**saved,'phase':'complete','bytes':len(data)})
            return data

    async def download_note_media(self,uuid):
        self.require_listed_note(uuid)
        if self.settings.get('storage_type')!='0' or self.settings.get('enable_incognito_mode')!='0':
            raise Unavailable('Cloud storage and incognito-off state are required before original media download')
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.media_lock):
            saved={'task_uuid':uuid,'started_at':time.time(),'phase':'requested'}
            journal=self.state/'note-media.json';private_write(journal,saved)
            try:
                async with asyncio.timeout(330):
                    saved['phase']='detail';private_write(journal,saved)
                    detail=await self.channel._request(note_media.DETAIL_URI,note_favorites.detail_request(uuid),REFERER)
                    saved['phase']='play';private_write(journal,saved)
                    play=await self.channel._request(note_media.PLAY_URI,[uuid],REFERER)
                    saved['phase']='download-url';private_write(journal,saved)
                    download=await self.channel._request(note_media.DOWNLOAD_URI,note_media.download_request(uuid),REFERER)
                    selected=note_media.select(detail,play,download,uuid)
                    saved.update(phase='download',source=selected['source'],expected_size=selected['expected_size'],expected_duration_ms=selected['expected_duration_ms'])
                    private_write(journal,saved)
                    result=await note_media.download(selected['url'],self.config['user_agent'],self.state/'media',selected['expected_size'])
            except asyncio.CancelledError:
                private_write(journal,{**saved,'phase':'cancelled'});raise
            except note_media.MediaUnavailable as error:
                private_write(journal,{**saved,'phase':'unavailable','reason':error.reason})
                raise Unavailable('Original media is unavailable for this note; no file was saved') from None
            except Exception as error:
                private_write(journal,{**saved,'phase':'failed','error_type':getattr(error,'error_type',type(error).__name__),'download_stage':getattr(error,'stage',None)})
                raise Unavailable('Original media could not be saved; no automatic retry was sent') from None
            public={key:result[key] for key in ('filename','bytes','sha256','mime','probe')}
            public.update(saved=True,source=selected['source'],expected_duration_ms=selected['expected_duration_ms'])
            private_write(journal,{**saved,'phase':'complete','result':public})
            return public

    async def assistant(self,text,request_id=None,provider='builtin',task_uuid=None):
        if not isinstance(text,str) or not text.strip() or len(text)>32000:
            raise Refused('Enter between 1 and 32,000 characters')
        if provider not in {'builtin','custom'}:raise Refused('Choose the built-in or custom assistant')
        if provider=='custom' and task_uuid is not None:raise Refused('Custom providers receive only the submitted text')
        if task_uuid is not None:self.require_listed_note(task_uuid)
        if request_id is None:request_id=secrets.token_hex(16)
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):
            raise Refused('Invalid assistant request handle')
        if provider=='builtin' and self.assistant_session_id is None:self.assistant_session_id=str(uuid.uuid4())
        return await self._assistant_call(request_id,lambda:self._assistant_request(text,provider,task_uuid))

    async def assistant_note_size(self,uuid):
        """Characters in a note's saved transcript, shown when the note is attached to a question.
        The built-in assistant gets only the note's ID and its replies report no context use or limit
        (as observed in the official app), so the page can show the note's size, not a share of a limit."""
        self.require_listed_note(uuid)
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        async with self.queued(self.native_transcript_lock):
            try:
                async with asyncio.timeout(40):
                    characters=paragraphs=0;cursor=None
                    for _ in range(10):
                        reply=await self.cloud_read(note_replace.READ_URI,note_replace.read_request(uuid,cursor),tags=('note:'+uuid,))
                        items,more=note_replace.page(reply)
                        characters+=sum(len(row['paragraph']) for row in items);paragraphs+=len(items)
                        if not more:break
                        last=items[-1]
                        if cursor and (last['uuid']==cursor['uuid'] or last['startTime']<=cursor['startTime']):raise ValueError('Transcript cursor did not advance')
                        cursor=last
            except Exception:raise Unavailable('The native transcript could not be read; no automatic retry was sent') from None
        return {'characters':characters,'paragraphs':paragraphs,'complete':not more}

    async def _assistant_call(self,request_id,operation):
        if self.assistant_task is not None:raise Busy('An assistant request is already in progress')
        task=asyncio.create_task(operation())
        self.assistant_task=task;self.assistant_request_id=request_id
        try:return await task
        except asyncio.CancelledError:
            # A user cancellation stops local HTTP work; external cancellation still propagates.
            if asyncio.current_task().cancelling():raise
            return {'cancelled':True,'provider_outcome':'unknown'}
        finally:
            if self.assistant_task is task:
                self.assistant_task=None;self.assistant_request_id=None

    def cancel_assistant(self,request_id):
        if self.assistant_task is None:return {'cancel_requested':False}
        if not isinstance(request_id,str) or request_id!=self.assistant_request_id:
            raise Refused('The assistant request changed; refresh before cancelling')
        self.assistant_task.cancel()
        return {'cancel_requested':True,'provider_outcome':'unknown'}

    async def assistant_conversations(self):
        return await self._assistant_call(None,self._assistant_conversations_request)

    async def _assistant_conversations_request(self):
        profile,credential=await self._assistant_auth()
        try:rows=await builtin_assistant.conversations(profile['origin'],credential,self.config['user_agent'])
        except builtin_assistant.AssistantError:
            raise Unavailable('The built-in assistant conversation list did not complete') from None
        handles={};conversations=[]
        for row in rows:
            handle=secrets.token_urlsafe(24);handles[handle]=row['session_id']
            conversations.append({'handle':handle,'title':row['title'],'date':row['date']})
        self.assistant_conversation_handles=handles
        return {'conversations':conversations,'provider':'builtin','route':'built-in QwenNote conversation list'}

    async def select_assistant_conversation(self,handle):
        if not isinstance(handle,str) or handle not in self.assistant_conversation_handles:
            raise Refused('Refresh the built-in conversation list before restoring')
        session_id=self.assistant_conversation_handles[handle]
        return await self._assistant_call(None,lambda:self._restore_assistant_conversation(session_id))

    async def _restore_assistant_conversation(self,session_id):
        self.assistant_session_id=session_id;self.assistant_task_id=None;self.assistant_recovery_available=False
        result=await self._assistant_history_request()
        return {**result,'restored':True,'route':'built-in QwenNote restored conversation'}

    def new_assistant_conversation(self):
        if self.assistant_task is not None:raise Busy('An assistant request is already in progress')
        self.assistant_session_id=None;self.assistant_task_id=None;self.assistant_recovery_available=False
        return {'new_conversation':True,'upstream_deleted':False}

    async def assistant_history(self):
        if self.assistant_session_id is None:raise Refused('Start a built-in conversation before reading its history')
        return await self._assistant_call(None,self._assistant_history_request)

    async def _assistant_history_request(self):
        profile,credential=await self._assistant_auth()
        try:
            rows=await builtin_assistant.history(profile['origin'],credential,self.assistant_session_id,self.config['user_agent'])
        except builtin_assistant.AssistantError:
            raise Unavailable('The built-in assistant history did not complete') from None
        pending=[row for row in rows if row['status']=='InProgress' and row['task_id']]
        if pending:
            self.assistant_task_id=pending[-1]['task_id'];self.assistant_recovery_available=True
        else:self.assistant_recovery_available=False
        messages=[{'query':row['query'],'text':row['content'],'complete':row['status']=='Finished'} for row in rows]
        latest=next((row['content'] for row in reversed(rows) if row['content'].strip()),'')
        return {'messages':messages,'text':latest,'provider':'builtin','route':'built-in QwenNote conversation history'}

    async def assistant_recover(self):
        if not self.assistant_recovery_available or self.assistant_session_id is None or self.assistant_task_id is None:
            raise Refused('No interrupted built-in request is available for recovery')
        return await self._assistant_call(None,self._assistant_recover_request)

    async def _assistant_recover_request(self):
        profile,credential=await self._assistant_auth()
        try:
            message=await builtin_assistant.recover(profile['origin'],credential,self.assistant_task_id,self.config['user_agent'])
        except builtin_assistant.AssistantError:
            raise Unavailable('The built-in assistant recovery did not complete; the prompt was not replayed') from None
        self.assistant_recovery_available=False
        return {'text':message['content'].strip(),'recovered':True,'provider':'builtin','route':'existing built-in task result; prompt not replayed'}

    async def _assistant_auth(self):
        if not self.channel or not self.channel.ready:raise Unavailable('The native connection is not ready')
        if self.assistant_profile is None:
            reply=await self.channel._request(builtin_assistant.USER_URI,[{}],REFERER)
            self.assistant_profile=builtin_assistant.user(reply)
        if not self.assistant_credential or self.assistant_credential_expires<time.time()+60:
            reply=await self.channel._request(builtin_assistant.TOKEN_URI,[{'scene':'aiAnswer','corpId':''}],REFERER)
            self.assistant_credential,self.assistant_credential_expires=builtin_assistant.token(reply)
        return self.assistant_profile,self.assistant_credential

    async def _assistant_request(self,text,provider,task_uuid):
        if provider=='custom':return await self._custom_assistant_request(text)
        self.assistant_task_id=None;self.assistant_recovery_available=False
        def event(event):
            task_id=event.get('task_id')
            if isinstance(task_id,str) and 1<=len(task_id)<=128 and all(c.isalnum() or c in '_-' for c in task_id):
                self.assistant_task_id=task_id
        try:
            profile,credential=await self._assistant_auth()
            path,body=builtin_assistant.request(text,profile,task_uuid,session_id=self.assistant_session_id,device=self.device_record)
            content=await builtin_assistant.chat(profile['origin'],credential,path,body,self.config['user_agent'],event_sink=event)
        except asyncio.CancelledError:
            self.assistant_recovery_available=self.assistant_task_id is not None
            raise
        except builtin_assistant.AssistantError:
            self.assistant_recovery_available=self.assistant_task_id is not None
            raise Unavailable('The built-in assistant request did not complete; no automatic retry was sent') from None
        self.assistant_recovery_available=False
        return {'text':content,'provider':'builtin','route':'built-in QwenNote cloud; device voice button not connected'}

    async def _custom_assistant_request(self,text):
        path=self.state/'assistant.json'
        if not path.is_file() or path.is_symlink() or path.stat().st_mode&0o077:raise Unavailable('No protected assistant configuration on this host')
        if not isinstance(text,str) or not 1<=len(text)<=32000:raise Refused('Enter between 1 and 32,000 characters')
        config=json.loads(path.read_text());url=config['url'];parsed=urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment or not (parsed.scheme=='https' or (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'})):
            raise Refused('The assistant endpoint must use HTTPS or loopback HTTP')
        headers={'Content-Type':'application/json'}
        if config.get('api_key'):headers['Authorization']='Bearer '+config['api_key']
        messages=[{'role':'user','content':text}]
        if isinstance(config.get('system_prompt'),str) and config['system_prompt'].strip():
            messages.insert(0,{'role':'system','content':config['system_prompt'][:8000]})
        try:
            async with ClientSession(timeout=ClientTimeout(total=60)) as client:
                async with client.post(url,headers=headers,json={'model':config['model'],'messages':messages,'stream':False},allow_redirects=False) as response:
                    if response.status!=200:raise CloudRejected('The assistant provider rejected the request')
                    raw=bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw)>1024*1024:raise Refused('The assistant response was larger than 1 MiB')
            result=json.loads(raw);content=result['choices'][0]['message']['content']
            if not isinstance(content,str) or not content.strip():raise ValueError('Invalid reply')
        except (TimeoutError,ClientError):
            raise Unavailable('The assistant request did not complete; no automatic retry was sent') from None
        except Refused:raise
        except (ValueError,KeyError,IndexError,TypeError):
            raise Unavailable('The assistant provider returned an invalid text reply') from None
        return {'text':content,'provider':'custom','route':'user-submitted text; device voice button not connected'}


csp=ui_page.csp


def make_app(backend,frontend=None):
    b=backend
    @web.middleware
    async def protect(request,handler):
        host=request.headers.get('Host','')
        allowed={f'127.0.0.1:{b.port}',f'localhost:{b.port}'}
        if host not in allowed:raise web.HTTPForbidden(text='Loopback Host required')
        origin=request.headers.get('Origin')
        if origin is not None and origin not in {'http://'+h for h in allowed}:raise web.HTTPForbidden(text='Same origin required')
        landing=request.path in ('/','/mcp/callback') and request.method=='GET'
        probe=request.path=='/healthz' and request.method=='GET'
        if landing:
            site=request.headers.get('Sec-Fetch-Site','')
            if site=='cross-site' and not (request.headers.get('Sec-Fetch-Mode')=='navigate' and request.headers.get('Sec-Fetch-Dest')=='document'):
                raise web.HTTPForbidden()
        elif probe:
            if request.headers.get('Sec-Fetch-Site')=='cross-site':raise web.HTTPForbidden()
        else:
            if not secrets.compare_digest(request.cookies.get('qn_ui',''),b.cookie):raise web.HTTPForbidden(text='Open the local UI first')
            if request.method!='GET' and not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),b.csrf):raise web.HTTPForbidden(text='CSRF check failed')
            if request.headers.get('Sec-Fetch-Site')=='cross-site':raise web.HTTPForbidden()
        started=time.monotonic();seen=[];CACHE_SEEN.set(seen);request_host.set(host)
        try:response=await handler(request)
        except (ValueError,PermissionError,ConnectionError,RuntimeError) as error:
            # Only PublicError text is returned; upstream exception bodies/URLs never are.
            response=web.json_response(describe(error),status=409)
        except web.HTTPException:raise
        except Exception as error:
            response=web.json_response(describe(error),status=502)
        if request.method=='POST' and request.match_info.get('action'):
            b.record_timing(request.match_info['action'],time.monotonic()-started,response.status<400)
        # When any part of the answer came from the server-side cache, say when the oldest part was saved.
        if seen:response.headers['X-Cached-At']=str(round(min(seen),3))
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        # The landing page sets its own per-response script nonce; everything else may run no script.
        if 'Content-Security-Policy' not in response.headers:response.headers['Content-Security-Policy']=csp(None)
        return response
    app=web.Application(middlewares=[protect],client_max_size=64*1024)
    async def index(request):
        nonce=secrets.token_urlsafe(18)
        response=web.Response(text=ui_page.render(b.csrf,nonce,ROOT,template=frontend),content_type='text/html')
        response.headers['Content-Security-Policy']=csp(nonce)
        response.set_cookie('qn_ui',b.cookie,httponly=True,samesite='Strict')
        return response
    async def get_status(request):
        try:since=int(request.query.get('since',''))
        except ValueError:since=None
        return web.json_response(b.status(since if since is not None and since>=0 else None,request.query.get('settings_revision')))
    async def get_health(request):return web.json_response(b.health())
    async def mcp_callback(request):
        try:
            result=await b.mcp_login_finish(request.query_string)
            title,message=('Access granted','You can close this tab and return to the console. 授权完成，可以关闭此页面并返回控制台。')
            title+=' ('+result['access']+')'
        except PublicError as error:
            title,message='Login not completed',str(error)+' 登录未完成，请在控制台的设置中重新开始。'
        page=('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
              +html.escape(title)+'</title></head><body><h1>'+html.escape(title)+'</h1><p>'+html.escape(message)+'</p><p><a href="/">Back to the console / 返回控制台</a></p></body></html>')
        return web.Response(text=page,content_type='text/html')
    async def post(request):
        try:data=await request.json()
        except ValueError:raise Refused('The request body must be JSON') from None
        if not isinstance(data,dict):raise Refused('The request body must be a JSON object')
        action=request.match_info['action']
        # {"refresh": true} on any request skips cached reads; it is not one of the action's own fields.
        FORCE_REFRESH.set(data.pop('refresh',False) is True)
        ttl=CACHED_ACTIONS.get(action);key=None
        if ttl and getattr(b,PENDING_JOURNALS.get(action,''),None) is None:
            key=action+':'+json.dumps(data,sort_keys=True,ensure_ascii=False)
            hit=b.cached(key,ttl)
            if hit is not None:
                if action=='documents' and data.get('tool')=='list_my_minutes':b.remember_notes(hit)
                return web.json_response(hit)
        try:result=await dispatch(action,data)
        finally:b.invalidate_after(action,data)  # also after a failed or uncertain write
        if isinstance(result,web.StreamResponse):return result
        if key and not (isinstance(result,dict) and (result.get('isError') or result.get('processing') or result.get('empty'))):b.cache.put(key,result,cache_tags(action,data))
        return web.json_response(result)
    async def dispatch(action,data):
        if action=='refresh':result=await b.refresh()
        elif action=='reconnect':result=b.reconnect()
        elif action=='ready':result=b.arm(data)
        elif action=='disarm':result=b.disarm()
        elif action=='cache-clear':b.cache.clear();result={'cleared':True}
        elif action=='confirm-stopped':result=await b.confirm_stopped(data)
        elif action=='device-check':result=await b.check_device()
        elif action=='note-mcp-write':result=await b.mcp_write(data.get('tool'),data.get('arguments'))
        elif action=='recordings':result=await b.recordings(data.get('next_token'),data.get('source','this'))
        elif action=='recording-audio':result=await b.recording_audio(data.get('handle'),data.get('format','wav'))
        elif action=='recording-audio-latest':result=await b.latest_recording_audio()
        elif action=='recording-make-note':
            result=await b.recording_write(device_files.GENERATE_URI,device_files.generate_request(b.config,[b.recording_file(data.get('handle'))]),'make-note')
        elif action=='recording-recover':
            result=await b.recording_write(device_files.RECOVER_URI,device_files.recover_request(b.config,[b.recording_file(data.get('handle'))]),'recover')
        elif action=='recording-delete':
            state=device_files.PERMANENT if data.get('permanent') is True else device_files.RECYCLE
            if state==device_files.PERMANENT and data.get('confirm')!='delete':raise Refused('Type delete to confirm')
            result=await b.recording_write(device_files.DELETE_URI,device_files.delete_request(b.config,[b.recording_file(data.get('handle'))],state),'delete',dangerous=True)
        elif action=='dangerous':result=b.set_dangerous(data)
        elif action=='account-settings':result=await b.account_settings()
        elif action=='note-speaker-rename':result=await b.rename_speaker(data.get('task_uuid'),data.get('from'),data.get('to'))
        elif action=='note-markers':result=await b.note_markers(data.get('task_uuid'),data.get('add_ms'))
        elif action=='recording-markers':result=await b.recording_markers(data.get('handle'))
        elif action=='recording-marker-save':result=await b.recording_marker_save(data)
        elif action=='recording-marker-delete':
            await b.native_write('marker-delete',extras.DEVICE_NOTE_DELETE_URI,extras.device_note_delete(b.config,b.resolve('marker',data.get('marker'))))
            result={'deleted':True}
        elif action=='notes-browse':result=await b.notes_browse(data.get('mode'),data.get('query'),data.get('tag'))
        elif action=='recording-groups':result=await b.recording_groups()
        elif action=='recording-group-save':result=await b.recording_group_save(data.get('name'),data.get('group'))
        elif action=='recording-group-delete':
            if data.get('confirm')!='delete':raise Refused('Type delete to confirm')
            await b.native_write('group-delete',extras.GROUP_DELETE_URI,extras.group_delete(b.config,b.resolve('group-id',data.get('group'))),dangerous=True)
            result=await b.recording_groups()
        elif action=='recording-move':result=await b.recording_move(data.get('handle'),data.get('group'))
        elif action=='memo-update':
            if not extras.text(data.get('text'),4000):raise Refused('Enter a memo text of at most 4000 characters')
            await b.native_write('memo-update',extras.MEMO_UPDATE_URI,extras.memo_update(b.resolve('memo',data.get('handle')),data['text']))
            b.memo_cache.clear();result={'saved':True}
        elif action=='memo-delete':
            await b.native_write('memo-delete',extras.MEMO_DELETE_URI,extras.memo_delete(b.resolve('memo',data.get('handle'))),dangerous=True,check=extras.memo_deleted)
            b.memo_cache.clear();result={'deleted':True}
        elif action=='device-info':result=await b.device_info()
        elif action=='ble-pairing-file':result=b.ble_pairing_file(data.get('confirm'))
        elif action=='mcp-login-start':result=await b.mcp_login_start(data.get('access'),request_host.get(),data.get('return_to','console'))
        elif action=='mcp-login-finish':result=await b.mcp_login_finish(data.get('url'))
        elif action in ('slicing-save','voiceprint-save'):
            if type(data.get('enabled')) is not bool:raise Refused('Choose on or off')
            if action=='slicing-save':uri,body=account_settings.SLICE_WRITE_URI,account_settings.slicing_write(data['enabled'])
            else:uri,body=account_settings.VOICEPRINT_TOGGLE_URI,account_settings.voiceprint_toggle(data['enabled'])
            result=await b.account_setting_write(action,uri,body)
        elif action=='voiceprint-clear':
            if data.get('confirm')!='clear':raise Refused('Type clear to confirm')
            result=await b.account_setting_write('voiceprint-clear',account_settings.VOICEPRINT_CLEAR_URI,[{}],dangerous=True)
        elif action=='note-delete':result=await b.delete_note(data.get('task_uuid'),data.get('confirm'))
        elif action=='device-unbind':result=await b.unbind_device(data.get('confirm'))
        elif action=='rtc-start':b.last_fid=None;result=await b.rtc.start(data.get('seconds',20),data.get('receive_audio',False))
        elif action=='rtc-stop':result=b.rtc.request_stop()
        elif action=='rtc-options':result=await b.interpreter_options(data.get('scene','a1_transcribe'))
        elif action=='record':
            if data.get('action')=='start':b.last_fid=None
            result=await b.record(data.get('action'))
        elif action=='record-task':
            if data:raise Refused('Task refresh takes no identifiers or options')
            result=await b.refresh_record_task()
        elif action=='setting':result=await b.change_setting(data.get('key'),data.get('value'),persistent_override=data.get('persistent_override') is True)
        elif action=='restore':result=await b.change_setting(data.get('key'),None,restore=True)
        elif action=='generation':result=await b.change_generation(data.get('values'),persistent_override=data.get('persistent_override') is True)
        elif action=='restore-generation':result=await b.change_generation(restore=True)
        elif action=='notification-options':result=await b.refresh_organizations()
        elif action=='notification':result=await b.change_notification(data.get('method'),data.get('organization_handle'),persistent_override=data.get('persistent_override') is True)
        elif action=='restore-notification':result=await b.change_notification(restore=True)
        elif action=='capture-start':result=b.start_capture(data.get('seconds',120))
        elif action=='capture-stop':result=b.stop_capture()
        elif action=='quotas':
            if not set(data)<={'organization_handle'} or ('organization_handle' in data and not isinstance(data.get('organization_handle'),str)):
                raise Refused('Quota reads accept only an opaque organization handle')
            result=await b.quotas(data.get('organization_handle'))
        elif action=='audio-memos':result=await b.audio_memos(data.get('limit',20))
        elif action=='documents':result=await b.documents(data.get('tool'),data.get('arguments',{}))
        elif action=='note-summary':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional summary options')
            result=await b.summary(data['task_uuid'])
        elif action=='note-summary-save':
            if set(data)!={'task_uuid','text','expected_revision'} or not isinstance(data.get('text'),str):
                raise Refused('Provide a listed note, summary text and its current revision')
            result=await b.summary(data['task_uuid'],data['text'],data['expected_revision'])
        elif action=='note-regenerate':
            if set(data)!={'task_uuid','expected_revision'}:
                raise Refused('Provide a listed note and its current summary revision')
            result=await b.regenerate(data['task_uuid'],data['expected_revision'])
        elif action=='note-share':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional sharing options')
            result=await b.share(data['task_uuid'])
        elif action=='note-share-save':
            if not {'task_uuid','access','expected_revision'}<=set(data)<={'task_uuid','access','expected_revision','password'}:
                raise Refused('Provide the note, private/public access and current sharing revision')
            result=await b.share(data['task_uuid'],data['access'],data['expected_revision'],data.get('password'))
        elif action=='note-share-url':
            if set(data)!={'task_uuid'}:raise Refused('Choose only a listed note for URL copying')
            result=await b.share_url(data['task_uuid'])
        elif action=='note-tags':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional tag options')
            result=await b.tags(data['task_uuid'])
        elif action=='note-tag-create':
            if set(data)!={'task_uuid','name','expected_revision'} or not isinstance(data.get('name'),str):
                raise Refused('Provide a listed note, a tag name and the current tag revision')
            result=await b.tags(data['task_uuid'],expected=data['expected_revision'],create_name=data['name'])
        elif action=='note-tag-rename':
            keys=set(data);required={'task_uuid','handle','name','expected_revision'}
            if not required<=keys or not keys<=required|{'auto_match'} or not isinstance(data.get('name'),str) or not isinstance(data.get('handle'),str) or ('auto_match' in data and type(data['auto_match']) is not bool):
                raise Refused('Provide a listed note, current tag option, new name, current revision and optional automatic-matching flag')
            result=await b.tags(data['task_uuid'],expected=data['expected_revision'],rename_handle=data['handle'],rename_name=data['name'],rename_auto_match=data.get('auto_match'))
        elif action=='note-tag-delete':
            if set(data)!={'task_uuid','handle','expected_revision'} or not isinstance(data.get('handle'),str):
                raise Refused('Provide a listed note, current tag option and current revision')
            result=await b.tags(data['task_uuid'],expected=data['expected_revision'],delete_handle=data['handle'])
        elif action=='note-tags-save':
            if set(data)!={'task_uuid','selected_handles','expected_revision'} or not isinstance(data.get('selected_handles'),list):
                raise Refused('Provide a listed note, selected tag options and their current revision')
            result=await b.tags(data['task_uuid'],data['selected_handles'],data['expected_revision'])
        elif action in ('note-replace-preview','note-replace-save'):
            required={'task_uuid','find','replace'}|({'expected_revision'} if action=='note-replace-save' else set())
            if set(data)!=required or (action=='note-replace-save' and not isinstance(data.get('expected_revision'),str)):
                raise Refused('Provide a listed note, find and replacement text, and a revision only when saving')
            result=await b.replace_note(data['task_uuid'],data['find'],data['replace'],data.get('expected_revision'))
        elif action=='device-identity':
            if data:raise Refused('Device details do not accept parameters')
            result=b.device_identity()
        elif action=='device-name':
            if data:raise Refused('Device-name refresh does not accept parameters')
            result=await b.device_name_status()
        elif action=='device-name-save':
            if set(data)!={'name','expected_revision'} or not isinstance(data.get('name'),str):
                raise Refused('Provide the device name and its current revision')
            result=await b.rename_device(data['name'],data['expected_revision'])
        elif action=='device-name-restore':
            if set(data)!={'expected_revision'}:
                raise Refused('Provide the current device-name revision before restoration')
            result=await b.rename_device(None,data['expected_revision'],restore=True)
        elif action=='note-transcript':
            if set(data)!={'task_uuid','next_token'} or (data['next_token'] is not None and not isinstance(data['next_token'],str)):
                raise Refused('Choose a listed note and an issued transcript continuation')
            result=await b.native_transcript(data['task_uuid'],data['next_token'])
        elif action=='note-translate-options':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional translation options')
            result=await b.translate_note(data['task_uuid'])
        elif action=='note-translate':
            initial=set(data)=={'task_uuid','target_language'} and isinstance(data.get('target_language'),str)
            continuation=set(data)=={'task_uuid','next_token'} and isinstance(data.get('next_token'),str)
            if not (initial or continuation):raise Refused('Choose a listed note and either a target language or an issued continuation')
            result=await b.translate_note(data['task_uuid'],data.get('target_language'),data.get('next_token'))
        elif action=='note-media-download':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without a media URL or download options')
            result=await b.download_note_media(data['task_uuid'])
        elif action=='note-export':
            keys=set(data);required={'task_uuid','kind'}
            if not required<=keys or not keys<=required|{'target_language','bilingual','file_format'}:
                raise Refused('Choose a listed note and supported export options')
            target=data.get('target_language');bilingual=data.get('bilingual',False);file_format=data.get('file_format','txt')
            content=await b.export_note(data['task_uuid'],data['kind'],target,bilingual,file_format)
            _,extension,mime,is_text=note_export.format_spec(data['kind'],file_format)
            filename='qwennote-'+data['kind']+('-'+target if target else '')+('-bilingual' if bilingual else '')+'.'+extension
            return web.Response(body=content,content_type=mime,charset='utf-8' if is_text else None,
                                headers={'Content-Disposition':'attachment; filename="'+filename+'"','Cache-Control':'no-store'})
        elif action=='note-favorite-status':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional options')
            result=await b.favorite(data.get('task_uuid'))
        elif action=='note-favorite':
            if set(data)!={'task_uuid','collected','expected_collected'} or type(data['collected']) is not bool:
                raise Refused('Favorite state must be an explicit boolean with the previously displayed value')
            result=await b.favorite(data['task_uuid'],data['collected'],data['expected_collected'])
        elif action=='assistant':
            if set(data)-{'text','request_id','provider','task_uuid'}:raise Refused('Unsupported assistant options')
            result=await b.assistant(data.get('text'),data.get('request_id'),data.get('provider','builtin'),data.get('task_uuid'))
        elif action=='assistant-note-size':
            if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional options')
            result=await b.assistant_note_size(data['task_uuid'])
        elif action=='assistant-cancel':result=b.cancel_assistant(data.get('request_id'))
        elif action=='assistant-conversations':
            if data:raise Refused('Assistant conversation actions take no options')
            result=await b.assistant_conversations()
        elif action=='assistant-select-conversation':
            if set(data)!={'handle'}:raise Refused('Choose one built-in conversation handle')
            result=await b.select_assistant_conversation(data['handle'])
        elif action=='assistant-history':
            if data:raise Refused('Assistant conversation actions take no options')
            result=await b.assistant_history()
        elif action=='assistant-recover':
            if data:raise Refused('Assistant conversation actions take no options')
            result=await b.assistant_recover()
        elif action=='assistant-new':
            if data:raise Refused('Assistant conversation actions take no options')
            result=b.new_assistant_conversation()
        else:raise web.HTTPNotFound()
        return result
    app.router.add_get('/',index);app.router.add_get('/api/status',get_status);app.router.add_get('/healthz',get_health);app.router.add_get('/mcp/callback',mcp_callback)
    app.router.add_post('/api/{action}',post)
    app.on_startup.append(b.start);app.on_cleanup.append(b.stop)
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=ROOT/'.private')
    parser.add_argument('--port',type=int,default=18780)
    parser.add_argument('--mcp-root',type=Path)
    parser.add_argument('--frontend',type=Path,help='Replace web.html with an independent HTML template (reloaded per page request)')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    web.run_app(make_app(Backend(args.state,args.port,args.mcp_root),frontend=args.frontend),host='127.0.0.1',port=args.port,access_log=None,print=lambda *_:None)

if __name__=='__main__':main()
