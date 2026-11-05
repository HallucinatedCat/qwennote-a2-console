#!/usr/bin/env python3
"""No-device preview of the host web UI with synthetic data.

Serves artifacts/standalone-session/web.html on loopback and answers the same
/api routes as web_backend.py from an in-memory simulation. It never opens a
vendor connection, reads .private state or sends a device command. Fixture
shapes come from the project's own parsers (settings catalog, generation and
notification descriptors, transcript reducer, memo and interpreter parsers), so
the UI sees the same structures the real backend produces.

    python3 tools/ui_preview.py                 # http://127.0.0.1:18781/
    python3 tools/ui_preview.py --scenario worst

Scenarios: demo, empty, recording, attention, worst. Switch with ?scenario=NAME.
All data is invented sample data.
"""
import argparse
import copy
import json
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import os
import secrets
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs,urlsplit

HOST=Path(__file__).resolve().parents[1]/'artifacts'/'standalone-session'
if not (HOST/'web.html').is_file():HOST=Path(__file__).resolve().parents[1]  # extracted host package layout
sys.path.insert(0,str(HOST))
import generation_settings as generation  # noqa: E402
import notification_settings as notification  # noqa: E402
import audio_memos as memos  # noqa: E402
import quota_protocol as quotas  # noqa: E402
import rtc_translation as interpreter  # noqa: E402
from settings_catalog import catalog,status_part  # noqa: E402
from live_transcript import TranscriptStore  # noqa: E402
import capabilities  # noqa: E402
import device_name  # noqa: E402
import note_export as export  # noqa: E402
import record_tasks  # noqa: E402
import credential_status  # noqa: E402
import ui_page  # noqa: E402
from public_errors import describe  # noqa: E402

SCENARIOS=('demo','empty','recording','attention','worst')
CONFIG={'account_id':'100200300','account_type':2,'target_device_id':4242}
ARMED_UNTIL=4102444800.0  # mirrors web_backend: armed until Disarm
COUNTERS=('push_frames','device_events','transcript_candidates','transcript_updates','unrecognized_frames','decoder_errors',
          'queue_dropped','connections','disconnects','dinger_transcript_candidates','conference_transcript_candidates')


class Refusal(Exception):
    def __init__(self,message,code='refused'):super().__init__(message);self.code=code


def setting(key,value,scope=2):
    return {'accountId':CONFIG['account_id'],'accountType':CONFIG['account_type'],'belongingType':scope,
            'belongingId':str(CONFIG['target_device_id']) if scope==2 else '0','settingKey':key,'settingValue':value}


def template(template_id,title):
    return json.dumps({'templateId':template_id,'title':title},ensure_ascii=False,separators=(',',':'))


def generation_config(worst=False):
    titles=[('general','General meeting'),('interview','Interview notes'),('lecture','Lecture summary')]
    if worst:titles.append(('long','Quarterly operations review with action items, owners and follow-up dates for every workstream'))
    return {'generateMinutesConfig':{
        'sourceLanguages':[{'lang':'auto','localDisplayName':'Detect automatically'},{'lang':'zh','localDisplayName':'中文'},
                           {'lang':'en','localDisplayName':'English'},{'lang':'ja','localDisplayName':'日本語'}],
        'minutesTemplates':[{'templateId':t,'title':n,'description':n} for t,n in titles]}}


def settings_rows(worst=False):
    rows=[setting('storage_type','0'),setting('enable_incognito_transcribe','0'),setting('device_org_id','8800'),
          setting('message_last_read_time','1730000000000',0),setting('auto_generate_minutes','1'),setting('system_message_version','12',0),
          setting('auto_generate_minutes_duration','1'),setting('enable_incognito_mode','0'),setting('has_set_incognito_mode','1'),
          setting('auto_generate_minutes_duration_limit','240',4),setting('default_source_language','auto',4),
          setting('default_llm','qwen-max',4),setting('default_minutes_template',template('general','General meeting'),4),
          setting('default_speaker_diarization','true',4),setting('voice_button_function_mode','memo',4),
          setting('enable_record_location','0',4),setting('enable_rtc_translate','0',4),setting('agent_result_notify_type','0',4),
          setting('device_edition','standard'),setting('device_model','A2'),
          setting('last_select_source_language','auto'),setting('last_select_target_language','en')]
    if worst:rows.append(setting('device_firmware_channel','stable-long-term-support-channel-for-enterprise-fleet-devices'))
    return rows


def transcript_payload(task,sequence,index,text,*,provisional='',lang='zh',translated=False,final=True,start=0):
    body={'stableResult':[{'index':index,'result':text,'targetLang':lang}] if text else [],
          'dynamicResult':{'result':provisional,'index':index,'targetLang':lang},'isTranslateResult':translated,'startTime':start}
    return {'eventEnum':4,'taskId':task,'recordId':task*10,'sequence':sequence,'textType':'ParagraphEnd' if final else 'Sentence',
            'content':json.dumps(body,ensure_ascii=False)}


SPEECH=[('今天我们先过一下 A2 的录音测试结果。','We will start with the A2 recording test results today.'),
        ('上周的三次测试里，有一次转写延迟比较明显。','In last week\'s three tests, one transcript lagged noticeably.'),
        ('我建议这周重新跑一个十五秒的接收测试。','I suggest rerunning a fifteen-second receive-only test this week.'),
        ('结束以后记得检查会议是否已经关闭。','Afterwards, check that the conference was closed.')]


class Preview:
    def __init__(self,scenario):
        self.lock=threading.Lock();self.csrf=secrets.token_urlsafe(24);self.cookie=secrets.token_urlsafe(24)
        self.secret=secrets.token_hex(32);self.action_timings={};self.load(scenario)

    def record_timing(self,action,seconds,ok):
        """Same shape as web_backend.Backend.record_timing, for the Dev timings view."""
        if action not in self.action_timings and len(self.action_timings)>=100:return
        row=self.action_timings.setdefault(action,{'count':0,'failures':0,'last_ms':0,'max_ms':0,'total_ms':0})
        ms=round(seconds*1000)
        row['count']+=1;row['failures']+=not ok;row['last_ms']=ms;row['max_ms']=max(row['max_ms'],ms);row['total_ms']+=ms

    # Scenario setup --------------------------------------------------------
    def load(self,scenario):
        now=time.time();worst=scenario=='worst'
        self.scenario=scenario;self.started_at=now-3*3600-420;self.events=[];self.event_id=0
        self.connection='connected';self.worker=True;self.device={'model':'QwenNote A2','product':1446,'cloud_online_status':1,
                                                                    'battery_percent':None if worst else 76,'observed_via':'bound-device cloud response'}
        self.rows=settings_rows(worst);self.generation=generation_config(worst)
        self.ready_until=0;self.own_test=False;self.record_state={'phase':'idle','action':None,'at':None}
        self.device_state={'state':'idle','online':True,'checked_at':now-140};self.cache_entries=0 if scenario=='empty' else 37
        self.dangerous=False;self.mcp_write=scenario in ('demo','worst');self.deleted_notes=set()
        self.account={'slicing':True,'voiceprint':scenario=='demo'}
        self.memo_edits={};self.deleted_memos=set();self.note_markers={};self.recording_notes={}
        self.groups=[] if scenario=='empty' else [{'handle':'g1','name':'Interviews','files':2},{'handle':'g2','name':'周会','files':1}]
        self.recordings=[] if scenario=='empty' else [
            {'handle':'h'+str(n)*23,'name':name,'duration_ms':ms,'bytes':size,'created_at_ms':int((now-age)*1000),'note_state':state,
             'incognito':incognito,'in_recycle_bin':bin_,'playable_from_cloud':True,'has_fid':True,'group':'g1' if n==0 else None}
            for n,(name,ms,size,age,state,incognito,bin_) in enumerate([
                ('2026-11-04 09:07:12',37000,148000,7200,'transcribed',True,False),
                ('2026-11-04 08:30:55',612000,2400000,9000,'not-transcribed',True,False),
                ('2026-11-03 17:02:10',2712000,10800000,90000,'failed',False,False),
                ('2026-11-02 11:40:00',91000,360000,190000,'transcribed',False,True)])]
        if getattr(self,'assistant_cancel',None):self.assistant_cancel.set()
        self.assistant_cancel=None;self.assistant_request_id=None
        self.assistant_conversation=scenario!='empty';self.assistant_recovery=scenario=='attention'
        self.restores={};self.generation_restore=None;self.notification_restore=None;self.organizations=None
        self.favorites={};self.favorite_pending=False;self.summary_texts={};self.summary_pending=False;self.tag_selections={};self.created_tags=[];self.tag_names={};self.tag_auto={};self.deleted_tags=set();self.replace_texts={}
        self.device_nick='Office recorder';self.device_name_original=None;self.device_name_pending=scenario=='attention'
        self.transcripts=TranscriptStore();self.stream_step=0;self.mutating=False
        self.capture={'capture_id':None,'active':False,'finish_reason':None,'elapsed_seconds':0,'bytes_written':0,'rows_written':0,
                      'summary_saved':False,'hardware_behavior_verified':False,**dict.fromkeys(COUNTERS,0)}
        self.rtc={'active':False,'stopping':False,'cleanup_pending':False,'started_at':None,'observation_seconds':None,'maximum_seconds':14400,
                  'audio_receive_available':scenario!='empty','last_result':None,'error_type':None,
                  'capabilities':{'conference_lifecycle':'live-verified','receive_audio':'decoded frames under quiet input',
                                  'intelligible_device_speech':'unverified','live_asr':'unverified','speaker_output':'not implemented'}}
        self.mcp=scenario!='empty';self.assistant=scenario in ('demo','recording','worst');self.last_refresh=now-95
        self.make_credentials(scenario,now)
        self.record_task=None;self.record_task_source='push';self.record_task_at=now-90
        if scenario!='empty':
            self.record_task=record_tasks.parse(CONFIG,{'realTimeAsr':None if scenario=='attention' else False,
                'deviceTask':{'accountId':CONFIG['account_id'],'accountType':CONFIG['account_type'],'deviceId':CONFIG['target_device_id'],
                'taskType':0,'taskId':'preview-task','state':0 if scenario=='recording' else 2,'stateCode':0,
                'startTime':int((now-120)*1000),'updateTime':int((now-90)*1000),'attributes':{'fid':'777888'}}})
        self.org_names={'0':'Personal edition','310001':'Hangzhou field team','310002':'Shanghai research office'}
        if worst:self.org_names['310003']='Internationale Forschungs- und Entwicklungsabteilung für Sprachverarbeitung, Standort Süd'
        self.emit('connection',{'state':'connected','events_since_reconnect_may_have_gaps':True},now-3*3600)
        if scenario=='empty':
            self.connection='connecting';self.device=None;self.rows=[];self.last_refresh=None;self.events=[];self.event_id=0
            self.device_state={'state':'unchecked','online':None,'checked_at':None}
        if scenario in ('demo','worst'):
            for n,(zh,en) in enumerate(SPEECH[:3]):
                self.transcripts.accept(transcript_payload(7,n+1,n,zh,start=n*6200))
                self.transcripts.accept(transcript_payload(7,n+1,n,en,lang='en',translated=True,start=n*6200),source='dtiot.dinger')
            self.transcripts.accept(transcript_payload(7,4,3,'',provisional='结束以后记得检查',final=False,start=18600))
            self.rtc['last_result']={'seconds_requested':20,'recording_start_requested':True,'recording_start_acknowledged':True,
                'conference_join_acknowledged':True,'media_join_attempted':True,'audio_frames_received':161,'audio_seconds_received':3.22,
                'peak_pcm':2,'live_asr_candidates':0,'translation_targets':0,'tts_requested':False,'physical_effects_verified':False,
                'device_stop_acknowledged':True,'conference_close_acknowledged':True,'conference_end_readback':True,
                'requests':{'create':200,'device-start':200,'join':200,'device-stop':200,'conference-close':200},'error_type':None}
            for kind,data,ago in [('readiness',{'armed':True,'seconds':300},2400),('command',{'action':'start','cloud_acknowledged':True},2380),
                                  ('device-event',{'event_type':'recordStart','physical_state_confirmed':False},2378),
                                  ('command',{'action':'stop','cloud_acknowledged':True},2340),('command',{'action':'confirm-stopped','owner_confirmed':True},2320)]:
                self.emit(kind,data,now-ago)
        if worst:
            for n in range(120):
                zh,en=SPEECH[n%4]
                self.transcripts.accept(transcript_payload(9,n+1,n,zh*(1+n%3),start=n*4100))
            for n in range(60):self.emit('device-event',{'event_type':'heartbeatAck','physical_state_confirmed':False},now-1000+n)
            self.capture.update(capture_id='c'*24,finish_reason='byte_limit',elapsed_seconds=212.4,bytes_written=8*1024*1024,
                                rows_written=4999,push_frames=1284091,device_events=48210,transcript_candidates=99999,transcript_updates=77001,
                                unrecognized_frames=12,decoder_errors=3,queue_dropped=1028,connections=4,disconnects=3,
                                started_at=now-900,duration_limit_seconds=300)
        if scenario=='recording':
            self.ready_until=ARMED_UNTIL;self.own_test=True;self.record_state={'phase':'acknowledged','action':'start','at':now-37}
            self.device_state={'state':'recording','online':True,'checked_at':now-12}
            self.emit('readiness',{'armed':True,'seconds':300},now-88);self.emit('command',{'action':'start','cloud_acknowledged':True},now-37)
            self.capture.update(capture_id='a1b2c3',active=True,started_at=now-41,duration_limit_seconds=120,elapsed_seconds=41,
                                bytes_written=18432,rows_written=57,push_frames=57,transcript_candidates=9,transcript_updates=7,connections=1)
        if scenario=='attention':
            self.connection='needs-attention';self.worker=False;self.own_test=True
            self.record_state={'phase':'unknown','action':'stop','at':now-300}
            self.device['cloud_online_status']=0;self.device_state={'state':'offline','online':False,'checked_at':now-300}
            self.rtc.update(cleanup_pending=True,error_type='CancelledError')
            self.restores={'enable_incognito_mode':{'original':'0','requested':'1','exact_restore_supported':True},
                           'default_source_language':{'original':'auto','requested':'en','exact_restore_supported':False}}
            self.set_value('enable_incognito_mode','1');self.set_value('default_source_language','en')
            self.generation_restore={'original':{k:self.value(k) for k in generation.KEYS},'requested':{k:self.value(k) for k in generation.KEYS},
                                     'exact_restore_supported':True,'original_scopes':{k:2 for k in generation.KEYS}}
            for k in generation.KEYS:self.set_scope(k,2)
            for state,ago in [('disconnected',900),('disconnected',880),('needs-attention',860)]:
                self.emit('connection',{'state':state,'error_type':'ConnectionError'} if state=='disconnected' else {'state':state},now-ago)
            self.emit('command',{'acknowledgement':'unknown','retry_performed':False},now-300)

    def make_credentials(self,scenario,now):
        """Synthetic credential files in a private temp folder, read back by the real credential_status."""
        old=getattr(self,'cred_tmp',None)
        if old:old.cleanup()
        self.cred_tmp=tempfile.TemporaryDirectory(prefix='qn-preview-');root=Path(self.cred_tmp.name)
        self.cred_state=root/'state';self.cred_mcp=root/'mcp' if self.mcp else None
        self.renewals={'count':0,'last_at':None,'failures':0,'last_failure_at':None,'last_failure_type':None}
        def write(path,data,mode=0o600,dir_mode=0o700):
            path.parent.mkdir(parents=True,exist_ok=True);os.chmod(path.parent,dir_mode)
            path.write_text(data if isinstance(data,str) else json.dumps(data));os.chmod(path,mode)
        session=lambda left,**x:{'access_token':'sample','refresh_token':'sample','native_client_id':'sample','native_client_secret':'sample',
                                 'native_auth_origin':'https://minutes.qwennote.cn','expires_at':now+left,**x}
        self.cred_state.mkdir(mode=0o700);os.chmod(self.cred_state,0o700)
        if scenario=='empty':return
        if scenario in ('demo','recording'):
            write(self.cred_state/'native-session.json',session(3120));self.renewals.update(count=3,last_at=now-4100)
            write(self.cred_state/'bootstrap-validation'/'native-session.json',session(5*3600))
            write(root/'mcp'/'.private'/'tokens.json',{'expires_at':now-600,'tokens':{'access_token':'sample','refresh_token':'sample','scope':'qwennote:read'}})
            write(self.cred_state/'assistant.json',{'url':'https://provider.example/v1/chat/completions','model':'sample','api_key':'sample'})
            write(self.cred_state/'login-handoff'/'native-session.json',session(-86400))
            self.emit('credential',{'renewed':True},now-4100)
        elif scenario=='attention':
            write(self.cred_state/'native-session.json',session(45))
            self.renewals.update(count=1,last_at=now-7200,failures=2,last_failure_at=now-300,last_failure_type='RuntimeError')
            write(self.cred_state/'bootstrap-validation'/'native-session.json',session(-1800))
            write(root/'mcp'/'.private'/'tokens.json',{'expires_at':now+1500,'tokens':{'access_token':'sample','refresh_token':'sample','scope':'qwennote:read'}},mode=0o644)
            write(self.cred_state/'login-handoff'/'native-session.json',session(-86400))
            self.emit('credential',{'renewed':False,'error_type':'RuntimeError'},now-300)
        elif scenario=='worst':
            write(self.cred_state/'native-session.json',session(-60,refresh_token=''))
            write(self.cred_state/'bootstrap-validation'/'native-session.json','{not json')
            write(root/'mcp'/'.private'/'tokens.json',{'expires_at':now+3000,'tokens':{'access_token':'sample','scope':'qwennote:read qwennote:write'}})
            write(self.cred_state/'assistant.json',{'url':'http://provider.example/v1','model':'sample'})
            write(self.cred_state/'login-handoff'/'native-session.json',session(-86400),dir_mode=0o755)

    # Helpers ---------------------------------------------------------------
    def emit(self,kind,data,at=None):
        self.event_id+=1;self.events.append({'id':self.event_id,'kind':kind,'time':at or time.time(),'data':data})
        self.events=self.events[-256:]

    def response(self):return {'code':200,'body':{'settingList':self.rows}}

    def value(self,key):
        best=None
        for row in self.rows:
            if row['settingKey']==key and (best is None or row['belongingType']==2):best=row
        return best and best['settingValue']

    def set_value(self,key,value):
        for row in self.rows:
            if row['settingKey']==key and row['belongingType']==2:row['settingValue']=value;return
        self.rows.append(setting(key,value,2))

    def set_scope(self,key,scope):
        for row in self.rows:
            if row['settingKey']==key:row['belongingType']=scope;row['belongingId']=str(CONFIG['target_device_id']) if scope==2 else '0'

    def details(self):
        return {row['key']:row for row in catalog(CONFIG,self.response(),self.generation)} if self.rows else {}

    def armed(self):return time.time()<self.ready_until and self.connection=='connected' and not (self.rtc['active'] or self.rtc['cleanup_pending'])

    def require_ready(self,cleanup=False):
        if self.rtc['active'] or self.rtc['cleanup_pending']:raise Refusal('A realtime test is running or awaiting cleanup')
        if not (cleanup and self.own_test) and time.time()>self.ready_until:raise Refusal('Confirm physical readiness first','not_ready')
        if self.connection!='connected' or not self.device:raise Refusal('The device session is unavailable','unavailable')

    def advance(self):
        """Time-driven simulation: streaming text, capture counters, RTC window."""
        now=time.time()
        if self.scenario=='recording' and self.own_test and self.record_state.get('action') in ('start','resume'):
            step=int((now-self.record_state['at'])//4)
            while self.stream_step<min(step,len(SPEECH)*2):
                n=self.stream_step//2;zh,en=SPEECH[n%len(SPEECH)];final=self.stream_step%2==1
                text=zh if final else zh[:max(2,len(zh)//2)]
                self.transcripts.accept(transcript_payload(11,self.stream_step+1,n,text if final else '',provisional='' if final else text,final=final,start=n*4000))
                self.emit('transcript',{'final':final,'translated':False,'language':'zh'})
                self.stream_step+=1
        c=self.capture
        if c['active']:
            elapsed=now-c['started_at'];c['elapsed_seconds']=round(elapsed,3)
            c['push_frames']=int(elapsed*1.4);c['bytes_written']=int(elapsed*450);c['rows_written']=c['push_frames']
            if elapsed>=c['duration_limit_seconds']:c.update(active=False,finish_reason='time_limit',summary_saved=True)
        r=self.rtc
        if r['active'] and now-r['started_at']>=r['observation_seconds']+3:
            r.update(active=False,stopping=False,last_result={'seconds_requested':r['observation_seconds'],'recording_start_acknowledged':True,
                'conference_join_acknowledged':True,'audio_frames_received':0,'live_asr_candidates':0,'physical_effects_verified':False,
                'device_stop_acknowledged':True,'conference_close_acknowledged':True,'conference_end_readback':True,'error_type':None})
            self.ready_until=0;self.emit('rtc',{'active':False,'cleanup_pending':False,'error_type':None})

    def status(self,since=None,settings_revision=None):
        self.advance();details=self.details()
        settings={k:row['value'] for k,row in details.items() if row['value'] is not None}
        ntf_rows=notification.current(CONFIG,self.response()) if self.rows else {}
        events=[e for e in self.events if since is None or e['id']>since]
        catalog,revision=status_part(details)
        result={'connection':self.connection,'device':self.device,'settings':settings,'server_time':time.time(),'started_at':self.started_at,
                'last_refresh_at':self.last_refresh,'worker_running':self.worker,'settings_revision':revision,
                'generation_group':generation.descriptor(details,self.generation_restore),
                'notification_group':notification.descriptor(ntf_rows,self.organizations,self.secret,self.notification_restore),
                'ready_until':self.ready_until,'armed':self.ready_until>time.time(),'record_command_phase':self.record_state['phase'],'record_state':dict(self.record_state),
                'own_test':self.own_test,'mutation_in_progress':self.mutating,'document_read_in_progress':False,
                'pending_restores':[k for k,v in self.restores.items() if v.get('exact_restore_supported',True)],
                'persistent_overrides':[k for k,v in self.restores.items() if not v.get('exact_restore_supported',True)],
                'events':events,'event_cursor':self.event_id,'transcript':self.transcripts.snapshot(),
                'live_diagnostics':dict(self.capture),'rtc':copy.deepcopy(self.rtc),
                'credentials':credential_status.collect(self.cred_state,self.cred_mcp,renewals=self.renewals),
                'record_task':self.record_task_status(),'device_check':self.device_check_status(),
                'dangerous_enabled':self.dangerous,'mcp_write_available':self.mcp_write,
                'cache':{'entries':self.cache_entries,'oldest_at':time.time()-5*3600 if self.cache_entries else None},'note_tag_creation':True,'note_tag_definitions':True,
                'mcp_available':self.mcp,
                'assistant_builtin_available':self.scenario!='empty','assistant_custom_configured':self.assistant,
                'assistant_configured':self.scenario!='empty' or self.assistant,
                'assistant_in_progress':self.assistant_cancel is not None,'assistant_request_id':self.assistant_request_id,
                'assistant_conversation_active':self.assistant_conversation,'assistant_recovery_available':self.assistant_recovery,
                'action_timings':copy.deepcopy(self.action_timings)}
        if settings_revision!=revision:result.update(catalog)
        return result

    def health(self):
        blocking=[name for name,on in [('disposable-test-open',self.own_test),('rtc-active',self.rtc['active']),
                  ('rtc-cleanup-pending',self.rtc['cleanup_pending']),('capture-active',self.capture['active']),
                  ('assistant-request-in-progress',self.assistant_cancel is not None)] if on]
        return {'connection':self.connection,'worker_running':self.worker,'restart_safe':not blocking,'blocking':blocking}

    # Actions ---------------------------------------------------------------
    def act(self,action,data):
        handler=getattr(self,'do_'+action.replace('-','_'),None)
        if handler is None:raise KeyError(action)
        time.sleep(0.25)  # Make busy states visible.
        return handler(data)

    def record_task_status(self):
        return {'available':self.record_task is not None,'source':self.record_task_source,'observed_at':self.record_task_at,
                'read_in_progress':False,**(self.record_task.summary() if self.record_task else {'physical_state_verified':False})}

    def do_record_task(self,data):
        if data:raise Refusal('Task refresh takes no identifiers or options')
        if self.connection!='connected':raise Refusal('The native connection is not ready','unavailable')
        if self.record_task is None:raise Refusal('No matched recording task has been observed in this host session')
        self.record_task_source='query';self.record_task_at=time.time()
        return self.record_task_status()

    def do_refresh(self,data):
        if self.connection!='connected':raise Refusal('The native connection is not ready','unavailable')
        self.last_refresh=time.time();return {'refreshed':True}

    def do_reconnect(self,data):
        if self.worker:raise Refusal('The connection is already being managed; wait for it to settle')
        self.worker=True;self.connection='connected';self.emit('connection',{'state':'reconnect-requested'});self.emit('connection',{'state':'connected'})
        return {'reconnecting':True}

    def do_ready(self,data):
        legacy=all(data.get(k) is True for k in ('physical_ready','device_idle','disposable_only'))
        if data.get('confirm') is not True and not legacy:raise Refusal('Confirm that you control this A2')
        self.ready_until=ARMED_UNTIL;self.emit('readiness',{'armed':True});return {'armed':True,'ready_until':self.ready_until,'server_time':time.time()}

    def do_disarm(self,data):self.ready_until=0;self.emit('readiness',{'armed':False});return {'disarmed':True}

    def do_device_check(self,data):
        """Mirrors Backend.check_device: the synthetic A2 follows the last acknowledged command."""
        if self.connection!='connected' or not self.device:raise Refusal('The native connection is not ready','unavailable')
        now=time.time();online=self.device.get('cloud_online_status')==1
        phase=self.record_state.get('phase');action=self.record_state.get('action')
        if not online:state='offline'
        elif phase=='acknowledged':state={'start':'recording','resume':'recording','pause':'paused'}.get(action,'idle')
        elif phase=='checked':state=action if action in ('recording','paused') else 'idle'
        else:state='recording' if self.own_test else 'idle'
        self.device_state={'state':state,'online':online,'checked_at':now}
        self.own_test=state in ('recording','paused')
        self.record_state={'phase':'checked','action':state,'at':now}
        self.emit('device-check',{'state':state,'online':online})
        return self.device_check_status()

    def device_check_status(self):
        return {**self.device_state,'battery_percent':(self.device or {}).get('battery_percent'),'in_progress':False}

    def do_cache_clear(self,data):self.cache_entries=0;return {'cleared':True}

    def do_mcp_login_start(self,data):
        if data.get('access') not in ('read','write'):raise Refusal('Choose read or write access')
        return {'url':'about:blank','return_to':data.get('return_to','console')}

    def do_mcp_login_finish(self,data):
        if not str(data.get('url','')).strip():raise Refusal('Paste the whole address from the browser bar')
        self.mcp_write=True;return {'granted':True,'access':'write'}

    def do_account_settings(self,data):
        return {'slicing':{'enabled':self.account['slicing']},'voiceprint':{'allowed':True,'enabled':self.account['voiceprint'],'enrolled':self.scenario=='demo','available_to_account':True}}

    def do_slicing_save(self,data):
        self.require_ready();self.account['slicing']=data.get('enabled') is True;return self.do_account_settings({})

    def do_voiceprint_save(self,data):
        self.require_ready();self.account['voiceprint']=data.get('enabled') is True;return self.do_account_settings({})

    def do_voiceprint_clear(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        if data.get('confirm')!='clear':raise Refusal('Type clear to confirm')
        self.account['voiceprint']=False;return self.do_account_settings({})

    def do_recordings(self,data):
        return {'recordings':[dict(row) for row in self.recordings],'has_more':False,'next_token':None}

    def recording(self,data):
        row=next((r for r in self.recordings if r['handle']==data.get('handle')),None)
        if row is None:raise Refusal('Load the recordings list again before using this recording')
        return row

    def do_recording_audio_latest(self,data):
        return b'RIFF synthetic preview audio'

    def do_recording_audio(self,data):
        self.recording(data);return b'OggS synthetic preview audio'

    def do_recording_make_note(self,data):
        self.recording(data)['note_state']='transcribing';return {'applied':True}

    def do_recording_recover(self,data):
        self.recording(data)['in_recycle_bin']=False;return {'applied':True}

    def do_recording_delete(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        row=self.recording(data)
        if data.get('permanent') is True:
            if data.get('confirm')!='delete':raise Refusal('Type delete to confirm')
            self.recordings.remove(row)
        else:row['in_recycle_bin']=True
        return {'applied':True}

    def do_dangerous(self,data):
        enabled=data.get('enabled') is True
        if enabled and data.get('confirm') is not True:raise Refusal('Confirm that you want to enable dangerous actions')
        self.dangerous=enabled;self.emit('dangerous',{'enabled':enabled});return {'dangerous_enabled':enabled}

    def do_note_delete(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        if data.get('confirm')!='delete':raise Refusal('Type delete to confirm')
        self.deleted_notes.add(data.get('task_uuid'));return {'deleted':True}

    def do_device_unbind(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        if data.get('confirm')!='unbind':raise Refusal('Type unbind to confirm')
        raise Refusal('The preview does not unbind anything')

    def do_note_mcp_write(self,data):
        if not self.mcp_write:raise Refusal('MCP write access is not authorized on this host; run the reader consent for write access first','unavailable')
        if data.get('tool') not in ('update_minutes_title','update_minutes_summary','replace_minutes_text','replace_speaker','add_personal_hot_word','delete_personal_hotword'):
            raise Refusal('Choose one of the documented MCP write tools')
        return {'tool':data['tool'],'applied':True,'result':{'isError':False,'content':[]}}

    def do_record(self,data):
        action=data.get('action');self.require_ready(cleanup=action=='stop')
        if action=='start':
            if self.device.get('cloud_online_status')!=1:raise Refusal('The A2 is offline')
            if self.own_test:raise Refusal('A recording is still open; stop it or check the device first')
            self.own_test=True;self.stream_step=0;self.transcripts=TranscriptStore()
            if self.scenario!='recording':self.scenario='recording'
        elif action not in ('pause','resume','stop') or not self.own_test:
            raise Refusal('The A2 is not recording; check the device first')
        self.record_state={'phase':'acknowledged','action':action,'at':time.time()}
        self.emit('command',{'action':action,'cloud_acknowledged':True})
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False}

    def do_confirm_stopped(self,data):
        if data.get('physically_stopped') is not True:raise Refusal('Confirm that you saw the A2 stop recording')
        self.own_test=False;self.record_state={'phase':'confirmed-stopped','action':'stop','at':time.time()}
        self.emit('command',{'action':'confirm-stopped','owner_confirmed':True});return {'confirmed':True}

    def guard_settings(self):
        self.require_ready()

    def do_setting(self,data):
        self.guard_settings();key=data.get('key');value=data.get('value');detail=self.details().get(key)
        if not detail or not detail['editable'] or value not in detail['choices']:raise Refusal('Setting/value has no enabled validated builder')
        if detail['inherited'] and data.get('persistent_override') is not True:
            raise Refusal('Creating a device override requires explicit persistent-change confirmation')
        saved=self.restores.get(key,{'original':detail['value'],'exact_restore_supported':not detail['inherited']})
        self.restores[key]={**saved,'requested':value}
        if detail['inherited']:self.set_scope(key,2)
        self.set_value(key,value);self.emit('command',{'action':'setting','cloud_acknowledged':True})
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_restore(self,data):
        self.guard_settings();key=data.get('key');saved=self.restores.get(key)
        if not saved or not saved.get('exact_restore_supported',True):raise Refusal('No saved original value for this setting')
        self.set_value(key,saved['original']);del self.restores[key]
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_generation(self,data):
        self.guard_settings();details=self.details()
        saved=generation.next_journal(details,data.get('values'),self.generation_restore,data.get('persistent_override') is True)
        self.generation_restore=saved
        for k,v in saved['requested'].items():self.set_scope(k,2);self.set_value(k,v)
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_restore_generation(self,data):
        self.guard_settings();values=generation.restore_values(self.details(),self.generation_restore)
        for k,v in values.items():self.set_value(k,v)
        self.generation_restore=None
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_notification_options(self,data):
        if self.connection!='connected':raise Refusal('The native connection is not ready','unavailable')
        reply={'code':200,'body':{'organizations':[{'dingOrgId':int(k),'orgName':v} for k,v in self.org_names.items() if k!='0']}}
        self.organizations=notification.organizations(reply)
        return notification.descriptor(notification.current(CONFIG,self.response()),self.organizations,self.secret,self.notification_restore)

    def do_notification(self,data):
        self.guard_settings();rows=notification.current(CONFIG,self.response());organization=None
        if data.get('organization_handle'):organization=notification.resolve(self.secret,data['organization_handle'],self.organizations or {})
        changes,saved=notification.prepare(rows,data.get('method'),organization,self.notification_restore,data.get('persistent_override') is True)
        if not changes:return {'unchanged':True,'cloud_write_sent':False}
        self.notification_restore=saved
        for k,v in changes.items():self.set_scope(k,2);self.set_value(k,v)
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_restore_notification(self,data):
        self.guard_settings();original=notification.restoration(notification.current(CONFIG,self.response()),self.notification_restore,self.organizations)
        for k,v in original.items():self.set_value(k,v)
        self.notification_restore=None
        return {'cloud_acknowledged':True,'physical_result':'unverified','automatic_retry':False,'cloud_readback_matches':True}

    def do_capture_start(self,data):
        seconds=data.get('seconds',120)
        if self.capture['active']:raise Refusal('A capture is already active')
        if type(seconds) is not int or not 1<=seconds<=300:raise Refusal('Capture duration must be 1 to 300 seconds')
        self.capture.update({**dict.fromkeys(COUNTERS,0),'capture_id':secrets.token_hex(12),'active':True,'finish_reason':None,
                             'started_at':time.time(),'duration_limit_seconds':seconds,'bytes_written':0,'rows_written':0,'connections':1})
        return dict(self.capture)

    def do_capture_stop(self,data):
        if self.capture['active']:self.capture.update(active=False,finish_reason='manual',summary_saved=True)
        return dict(self.capture)

    def do_rtc_start(self,data):
        seconds=data.get('seconds',20)
        if type(seconds) is not int or not 1<=seconds<=4*3600:raise Refusal('Choose a live transcript length from 1 second to 4 hours')
        self.require_ready()
        if self.own_test:raise Refusal('Another device operation is active')
        self.rtc.update(active=True,stopping=False,started_at=time.time(),observation_seconds=seconds,last_result=None,error_type=None)
        return {'started':True,'observation_seconds':seconds,'automatic_stop':True,'audio_receive':data.get('receive_audio') is True}

    def do_rtc_stop(self,data):
        if self.rtc['active']:self.rtc.update(stopping=True,observation_seconds=max(1,int(time.time()-self.rtc['started_at'])))
        return {'stop_requested':self.rtc['stopping'],'cleanup_pending':False,'active':self.rtc['active']}

    def do_rtc_options(self,data):
        scene=interpreter.scene(data.get('scene','a1_transcribe'))
        voices=lambda *names:[{'voiceId':f'v-{n}','name':n} for n in names]
        reply={'code':200,'body':{'code':200,'supportedLanguages':[
            {'lang':'zh','displayName':'Chinese','nativeLangName':'中文','supportVoices':voices('Xiaoyun','Zhiwei','Ruoxi')},
            {'lang':'en','displayName':'English','nativeLangName':'English','supportVoices':voices('Ava','Noah')},
            {'lang':'ja','displayName':'Japanese','nativeLangName':'日本語','supportVoices':voices('Haruka')},
            {'lang':'de','displayName':'German','nativeLangName':'Deutsch','supportVoices':[]}]}}
        return interpreter.descriptor(interpreter.parse_config(reply),self.secret,scene)

    def do_note_share(self,data):
        if set(data)!={'task_uuid'}:raise Refused('Choose a listed note without additional sharing options')
        access=getattr(self,'share_access','public')
        return {'access':access,'sharing_enabled':False,'password_protected':False,'expire_at':None,'expire_days':None,
                'can_search':None,'can_recommend':None,'allowed_access':['private','public'],
                'revision':('a' if access=='public' else 'b')*64,'pending':False}

    def do_note_share_save(self,data):
        if set(data)!={'task_uuid','access','expected_revision'}:raise Refused('Provide the note, private/public access and current sharing revision')
        current=getattr(self,'share_access','public');target=data['access']
        if target not in ('private','public'):raise Refused('Choose private or public access and load the current sharing scope first')
        expected=('a' if current=='public' else 'b')*64
        if data['expected_revision']!=expected:raise Refused('The sharing scope changed elsewhere; refresh it before saving')
        self.share_access=target
        return {**self.do_note_share({'task_uuid':data['task_uuid']}),'changed':target!=current,'cloud_acknowledged':True,'readback_matches':True}

    def do_note_share_url(self,data):
        if set(data)!={'task_uuid'}:raise Refused('Choose only a listed note for URL copying')
        access=getattr(self,'share_access','public')
        return {'url':'https://minutes.qwennote.cn/share/synthetic-example?ticket=opaque','access':access,'sharing_enabled':False}

    def device_name_view(self):
        if not self.device:raise Refusal('Device details have not been read','unavailable')
        record={'devServId':1446,'deviceId':CONFIG['target_device_id'],'accountType':CONFIG['account_type'],
                'accountId':CONFIG['account_id'],'deviceNick':self.device_nick}
        identity=device_name.identity(CONFIG,record)
        return {'name':self.device_nick,'sn':'A2-DEMO-001','firmware_version':None,'firmware_source':None,
                'read_at':self.last_refresh,'stale':self.connection!='connected','pending':self.device_name_pending,
                'restore_available':self.device_name_original is not None and self.device_name_original!=self.device_nick,
                'revision':device_name.revision(self.secret,identity,self.device_nick)}

    def do_device_identity(self,data):
        if data:raise Refusal('Device details do not accept parameters')
        return self.device_name_view()

    def do_device_name(self,data):
        if data:raise Refusal('Device-name refresh does not accept parameters')
        self.device_name_pending=False
        return self.device_name_view()

    def do_device_name_save(self,data):
        if set(data)!={'name','expected_revision'} or not isinstance(data.get('name'),str):
            raise Refusal('Provide the device name and its current revision')
        self.require_ready()
        try:name=device_name.normalize(data['name'])
        except ValueError:raise Refusal('Use a nonempty device name within 32 application units, without control characters') from None
        if data['expected_revision']!=self.device_name_view()['revision']:
            raise Refusal('The device name changed elsewhere; refresh it before saving')
        if name==self.device_nick:return {**self.device_name_view(),'changed':False,'restored':False}
        if self.device_name_original is None:self.device_name_original=self.device_nick
        self.device_nick=name
        return {**self.device_name_view(),'changed':True,'cloud_acknowledged':True,'readback_matches':True,'restored':False,'automatic_retry':False}

    def do_device_name_restore(self,data):
        if set(data)!={'expected_revision'}:raise Refusal('Provide the current device-name revision before restoration')
        self.require_ready()
        if self.device_name_original is None:raise Refusal('No saved original device name is available')
        if data['expected_revision']!=self.device_name_view()['revision']:
            raise Refusal('The device name changed elsewhere; refresh it before saving')
        self.device_nick=self.device_name_original;self.device_name_original=None
        return {**self.device_name_view(),'changed':True,'cloud_acknowledged':True,'readback_matches':True,'restored':True,'automatic_retry':False}

    def do_quotas(self,data):
        if not set(data)<={'organization_handle'} or ('organization_handle' in data and not isinstance(data.get('organization_handle'),str)):
            raise Refusal('Quota reads accept only an opaque organization handle','refused')
        handle=data.get('organization_handle');label=None
        if handle is not None:
            if self.organizations is None:raise Refusal('Load organization options before selecting enterprise quotas','refused')
            corp=notification.resolve(self.secret,handle,self.organizations);label=self.organizations[corp]
        empty=self.scenario=='empty';worst=self.scenario=='worst'
        board={'boardType':'1' if handle else '0','boardStatus':'4' if handle else '1','resourceBoard':{'resourceNameContent':label or 'Example membership',
            'functionRangeContent':'Example allowance description. '* (20 if worst else 1),'extraInfo':{'bizType':'hardware_month_quota_account'}},
            'functionBoardList':[{'functionNameContent':'Transcription','functionValueContent':'This month: 20/120 minutes used',
                'functionValueTips':'Reset: 2026-12-01 00:00:00','extraInfo':{'used':'20','total':'120'}}],
            'adBoardList':[{'adType':1,'adKey':'minutes_image_summary','adTitleContent':'图文纪要','adDetailContent':'8篇 剩余'},
                {'adType':1,'adKey':'minutes_ask_ai','adTitleContent':'AI问答','adDetailContent':'20次 剩余','limitFree':True},
                {'adType':1,'adKey':'device_cloud_storage','adTitleContent':'云存储','adDetailContent':'1.5G 剩余'}]}
        dashboard=quotas.dashboard({'code':200,'body':{'licenceBoardList':[] if empty else [board]}})
        detail=quotas.detail({'code':200,'body':{'list':[] if empty else [{'resourceTag':'hardware_month_quota_account',
            'resourceName':'Example monthly transcription allowance','resourceUsed':20,'resourceQuota':120,
            'startTime':'2026-11-01','endTime':'2026-11-30','updateTime':'2026-12-01 00:00:00'}]}})
        traffic=quotas.traffic({'code':200,'body':{'used':None if empty else 1048576,'quota':None if empty else 4194304,
            'remaining':None if empty else 3145728,'tips':'Example carrier notice. '* (20 if worst else 1),
            'dailyUsageList':[] if empty else [{'used':1024,'updateTime':'2026-11-01'}]}})
        sections={n:{'available':True,'data':value} for n,value in [('account',dashboard),('device',dashboard),('detail',detail),('traffic',traffic)]}
        if worst:sections['account']={'available':False}
        return {'queried_at':time.time(),'read_only':True,'organization_handle':handle,'organization_label':label,'sections':sections}

    def do_audio_memos(self,data):
        limit=data.get('limit',20);memos.list_request(limit)
        now=int(time.time()*1000)
        texts=[] if self.scenario=='empty' else ['Call the venue about the projector before Thursday.','',
               json.dumps({'text':'记得把周五的评审改到下午三点。'},ensure_ascii=False),'Buy AA batteries for the backup recorder.']
        if self.scenario=='worst':texts+=['Long memo: '+'the field test needs a second receive-only run with the corrected join, then a saved-note check. '*30]
        reply={'code':200,'body':{'code':0,'hasMore':self.scenario=='worst','missionList':[
            {'missionId':f'm{n}','type':0,'content':t,'createTime':now-n*5400000,'durationMs':4200+n*900} for n,t in enumerate(texts[:limit])]}}
        result=memos.parse_list(reply,limit,keep_ids=True)
        result['items']=[{**{k:v for k,v in row.items() if k!='mission_id'},'handle':'memo-'+row['mission_id']} for row in result['items']
                         if row['mission_id'] not in self.deleted_memos]
        for row in result['items']:row['text']=self.memo_edits.get(row['handle'],row['text'])
        return result

    def do_memo_update(self,data):
        if not str(data.get('text','')).strip():raise Refusal('Enter a memo text of at most 4000 characters')
        self.memo_edits[data.get('handle')]=data['text'];return {'saved':True}

    def do_memo_delete(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        self.deleted_memos.add(str(data.get('handle','')).removeprefix('memo-'));return {'deleted':True}

    def do_note_speaker_rename(self,data):
        if not data.get('from') or not data.get('to'):raise Refusal('Enter the current and the new speaker name, at most 64 characters each')
        return {'renamed':3}

    def do_note_markers(self,data):
        markers=self.note_markers.setdefault(data.get('task_uuid'),[{'kind':'marker','time_ms':12000,'text':None},{'kind':'text','time_ms':47000,'text':'Decision point'}])
        if data.get('add_ms') is not None:markers.append({'kind':'marker','time_ms':data['add_ms'],'text':None});markers.sort(key=lambda m:m['time_ms'])
        return {'markers':markers}

    def do_recording_markers(self,data):
        rows=self.recording_notes.setdefault(data.get('handle'),[{'handle':'mk1','kind':'marker','time_ms':5000,'text':None}])
        return {'markers':rows}

    def do_recording_marker_save(self,data):
        if data.get('marker'):
            for rows in self.recording_notes.values():
                for row in rows:
                    if row['handle']==data['marker']:row['text']=data.get('text');row['kind']='text'
            return {'saved':True}
        rows=self.recording_notes.setdefault(data.get('handle'),[])
        rows.append({'handle':'mk'+str(len(rows)+2),'kind':'text' if data.get('text') else 'marker','time_ms':data.get('time_ms',0),'text':data.get('text')})
        return {'saved':True}

    def do_recording_marker_delete(self,data):
        for rows in self.recording_notes.values():rows[:]=[row for row in rows if row['handle']!=data.get('marker')]
        return {'deleted':True}

    def do_notes_browse(self,data):
        if data.get('mode')=='tags':return {'tags':[{'handle':'tab1','name':'Field tests'},{'handle':'tab2','name':'周会'}]}
        notes=self.do_documents({'tool':'list_my_minutes','arguments':{}})
        items=json.loads(notes['content'][0]['text'])['items'] if 'content' in notes else notes.get('structuredContent',{}).get('items',[])
        if data.get('mode')=='search':items=[n for n in items if str(data.get('query','')).lower() in (n.get('title') or '').lower()]
        elif data.get('mode')=='favorites':items=[n for n in items if n.get('collected')]
        elif data.get('mode')=='tag':items=items[:2]
        else:raise Refusal('Choose search, favorites or a tag')
        return {'items':items}

    def do_recording_groups(self,data):
        return {'groups':[dict(g) for g in self.groups],'counts':{'total':len(self.recordings),'ungrouped':2,'recycle_bin':1}}

    def do_recording_group_save(self,data):
        if not str(data.get('name','')).strip():raise Refusal('Enter a group name of at most 60 characters')
        if data.get('group'):
            for group in self.groups:
                if group['handle']==data['group']:group['name']=data['name']
        else:self.groups.append({'handle':'g'+str(len(self.groups)+1),'name':data['name'],'files':0})
        return self.do_recording_groups({})

    def do_recording_group_delete(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        if data.get('confirm')!='delete':raise Refusal('Type delete to confirm')
        self.groups=[g for g in self.groups if g['handle']!=data.get('group')];return self.do_recording_groups({})

    def do_recording_move(self,data):
        self.recording(data)['group']=data.get('group');return {'moved':True}

    def do_ble_pairing_file(self,data):
        if not self.dangerous:raise Refusal('Enable dangerous actions in Dev tools first')
        if data.get('confirm')!='pair':raise Refusal('Type pair to confirm')
        return json.dumps({'sn':'SYNTHETIC-SN','device_secret':'00'*16,'did':'0'*32,'corp_id':''})

    def do_device_info(self,data):
        return {'product':{'productName':'QwenNote A2','connectType':2},'phones':[{'name':'Owner phone','state':'connected','app_type':1},{'name':'Old tablet','state':'allowed','app_type':1}]}

    def do_documents(self,data):
        if not self.mcp:raise Refusal('The document reader is not configured on this host','unavailable')
        tool=data.get('tool');args=data.get('arguments') or {};now=int(time.time()*1000)
        long_title='Weekly sync: A2 receive-only RTC retest planning, cleanup journal review and translation path decisions for next sprint'
        notes=[{'uuid':f'n{n}','title':t,'gmtCreate':now-n*86400000-3600000,'durationMillis':d,'mediaType':'audio','fullTextSummary':s}
               for n,(t,d,s) in enumerate([('A2 receive-only test',37000,'A short disposable recording used to check the realtime join.'),
                                           ('产品周会',2712000,'讨论了 A2 录音测试、转写延迟和下周的接收测试安排。'),
                                           ('Interview with field team',1834000,''),('',61000,'')])]
        if self.scenario=='worst':notes.insert(0,{'uuid':'nw','title':long_title,'gmtCreate':now,'durationMillis':14523000,'mediaType':'audio',
                                                  'fullTextSummary':'Very long preview text that keeps going to test clamping. '*6})
        notes=[note for note in notes if note['uuid'] not in self.deleted_notes]
        for note in notes:note['collected']=self.favorites.setdefault(note['uuid'],False)
        if tool=='list_hot_words':result={'hotWordList':[] if self.scenario=='empty' else ['QwenNote','A2','悟空']}
        elif tool=='list_my_minutes':result={'total':len(notes),'hasMore':self.scenario=='worst' and not args.get('nextToken'),'nextToken':'page2','items':notes}
        elif tool=='get_minutes_ai_summary':
            result={'hasSummary':args.get('taskUuid')!='n2','fullSummary':'## Key points\n\n- The **multi-device join** fix is deployed but not live-tested.\n- Next step: one 15-second receive-only probe.\n\n## Decisions\n\n1. No playback or translation targets in the next run.\n2. Keep the cleanup journal until stop, close and end readback succeed.\n\n> Physical effects are not inferred from cloud acknowledgements.'}
        elif tool=='get_minutes_transcription':
            page=1 if args.get('nextToken') else 0
            paras=[{'speakerName':['Alice','Bob'][n%2],'startTime':(page*6+n)*9000,'content':SPEECH[n%4][0] if n%2 else SPEECH[n%4][1]} for n in range(6)]
            result={'paragraphs':paras,'hasMore':page==0,'nextToken':'t2' if page==0 else None}
        elif tool=='get_minutes_keywords':result={'keywords':['A2','realtime join','cleanup journal','转写延迟'],'total':4}
        elif tool=='list_minutes_todos':result={'items':[{'content':'Run one 15-second receive-only probe','assignee':'Alice','dueDate':'2026-11-06'},
                                                          {'content':'Inspect the saved note after the probe','assignee':'Bob'}]}
        elif tool=='get_minutes_basic_info':result={'title':'A2 receive-only test','durationMillis':37000,'mediaType':'audio','shareUrl':'https://example.com/share/preview'}
        else:result={'items':[],'hasMore':False,'note':'Preview data for '+str(tool)}
        return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'structuredContent':result,'isError':False}

    def do_note_transcript(self,data):
        uuid=data.get('task_uuid')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        second=data.get('next_token')=='synthetic-next'
        rows=[{'content':'Second synthetic page with a later speaker.','speaker':'主持人','start_ms':61000,'end_ms':63500}] if second else [
            {'content':self.replace_texts.get(uuid,'Synthetic transcript. Synthetic text with timing.'),'speaker':'Speaker 1','start_ms':1000,'end_ms':4200}]
        return {'paragraphs':[] if self.scenario=='empty' or uuid=='n3' else rows,'has_more':not second and self.scenario=='worst','next_token':'synthetic-next' if not second and self.scenario=='worst' else None,'source':'native'}

    def do_assistant_note_size(self,data):
        if set(data)!={'task_uuid'}:raise Refusal('Choose a listed note without additional options')
        if data['task_uuid'] not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        text=self.replace_texts.get(data['task_uuid'],'Synthetic transcript. Synthetic text with timing.')
        return {'characters':0 if self.scenario=='empty' or data['task_uuid']=='n3' else len(text)*(40 if self.scenario=='worst' else 1),'paragraphs':1,'complete':self.scenario!='worst'}

    def do_note_replace_preview(self,data):
        uuid=data.get('task_uuid')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        text=self.replace_texts.setdefault(uuid,'Synthetic transcript. Synthetic text with timing.')
        needle=data.get('find');replacement=data.get('replace')
        if not isinstance(needle,str) or not needle or not isinstance(replacement,str):raise Refusal('Preview the replacement before saving')
        count=text.count(needle) if needle!=replacement else 0
        return {'revision':hashlib.sha256(text.encode()).hexdigest(),'matches':count,'paragraphs_changed':int(count>0),'pending':False,
                'changes':[{'paragraph':1,'start_ms':1000,'before':text,'after':text.replace(needle,replacement)}] if count else []}

    def do_note_replace_save(self,data):
        current=self.do_note_replace_preview(data)
        if current['revision']!=data.get('expected_revision'):raise Refusal('The transcript changed elsewhere; preview the replacement again')
        if self.scenario=='attention':raise Refusal('The replacement result is unknown; preview before another save')
        self.replace_texts[data['task_uuid']]=self.replace_texts[data['task_uuid']].replace(data['find'],data['replace'])
        return {'changed':bool(current['matches']),'matches':current['matches'],'verified':True,'pending':False}

    def do_note_tags(self,data):
        uuid=data.get('task_uuid')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        selected=self.tag_selections.setdefault(uuid,['fixture-a'])
        options=[{'handle':'fixture-a','label':self.tag_names.get('fixture-a','Research'),'auto_match':False,'selected':'fixture-a' in selected},
                 {'handle':'fixture-b','label':self.tag_names.get('fixture-b','Project planning and decisions' if self.scenario=='worst' else 'Work'),'auto_match':True,'selected':'fixture-b' in selected}]
        options=[tag for tag in options if tag['handle'] not in self.deleted_tags]
        if self.scenario=='empty':options=[]
        options += [{**tag,'label':self.tag_names.get(tag['handle'],tag['label']),'selected':tag['handle'] in selected} for tag in self.created_tags if tag['handle'] not in self.deleted_tags]
        options=[{**tag,'auto_match':self.tag_auto.get(tag['handle'],tag['auto_match'])} for tag in options]
        return {'options':options,'revision':hashlib.sha256(json.dumps(options).encode()).hexdigest(),'verified':True,'pending':False}

    def do_note_tag_create(self,data):
        current=self.do_note_tags(data)
        if data.get('expected_revision')!=current['revision']:raise Refusal('The tags changed elsewhere; refresh them before saving')
        name=data['name'].strip()
        if any(t['label']==name for t in current['options']):raise Refusal('A tag with this name already exists; select it from the list')
        if self.scenario=='attention':raise Refusal('Tag creation is uncertain; refresh before another tag action')
        handle='created-'+str(len(self.created_tags));self.created_tags.append({'handle':handle,'label':name,'auto_match':False})
        return {**self.do_note_tags(data),'created_handle':handle}

    def do_note_tag_rename(self,data):
        current=self.do_note_tags(data)
        if data.get('expected_revision')!=current['revision']:raise Refusal('The tags changed elsewhere; refresh them before saving')
        option=next((x for x in current['options'] if x['handle']==data.get('handle')),None)
        name=data['name'].strip()
        if not option:raise Refusal('Choose a current tag option from this note')
        if any(x['handle']!=option['handle'] and x['label']==name for x in current['options']):raise Refusal('A tag with this name already exists; select it from the list')
        if self.scenario=='attention':raise Refusal('The tag definition change is uncertain; refresh before another tag action')
        auto=data.get('auto_match',option['auto_match'])
        if type(auto) is not bool:raise Refusal('Automatic matching must be true or false')
        self.tag_names[option['handle']]=name;self.tag_auto[option['handle']]=auto
        return {**self.do_note_tags(data),'changed':name!=option['label'] or auto!=option['auto_match']}

    def do_note_tag_delete(self,data):
        current=self.do_note_tags(data)
        if data.get('expected_revision')!=current['revision']:raise Refusal('The tags changed elsewhere; refresh them before saving')
        option=next((x for x in current['options'] if x['handle']==data.get('handle')),None)
        if not option:raise Refusal('Choose a current tag option from this note')
        if option['selected']:raise Refusal('Remove this tag from the open note before deleting it')
        if self.scenario=='attention':raise Refusal('The tag definition change is uncertain; refresh before another tag action')
        self.deleted_tags.add(option['handle'])
        for uuid in self.tag_selections:self.tag_selections[uuid]=[x for x in self.tag_selections[uuid] if x!=option['handle']]
        return {**self.do_note_tags(data),'changed':True}

    def do_note_tags_save(self,data):
        current=self.do_note_tags(data)
        if data.get('expected_revision')!=current['revision']:raise Refusal('The tags changed elsewhere; refresh them before saving')
        if self.scenario=='attention':raise Refusal('The tag save result is unknown; refresh before another save')
        self.tag_selections[data['task_uuid']]=data['selected_handles']
        return {**self.do_note_tags(data),'changed':True}

    def do_note_summary(self,data):
        uuid=data.get('task_uuid')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        if uuid=='n3' and uuid not in self.summary_texts:  # the untitled note: no speech, so no summary (mirrors an empty note)
            return {'text':'','empty':True,'summary_failed':False,'revision':None,'verified':True,'pending':False}
        text=self.summary_texts.setdefault(uuid,'# Synthetic summary\n\nPreserve **this** paragraph.')
        self.summary_pending=False
        return {'text':text,'revision':hashlib.sha256(text.encode()).hexdigest(),'verified':True,'pending':False}

    def do_note_summary_save(self,data):
        current=self.do_note_summary(data)
        if current['revision']!=data.get('expected_revision'):raise Refusal('The summary changed elsewhere; refresh it before saving')
        if self.scenario=='attention':
            self.summary_pending=True
            raise Refusal('The summary save result is unknown; refresh before another save')
        self.summary_texts[data['task_uuid']]=data['text']
        return {**self.do_note_summary(data),'changed':True}

    def do_note_regenerate(self,data):
        current=self.do_note_summary(data)
        if current['revision']!=data.get('expected_revision'):raise Refusal('The summary changed elsewhere; refresh it before regenerating')
        if self.scenario=='attention':
            self.summary_pending=True
            return {'text':current['text'],'revision':current['revision'],'verified':False,'pending':True,'accepted':True,'changed':None,'cloud_acknowledged':True}
        previous=current['text'];self.summary_texts[data['task_uuid']]='# Synthetic regenerated summary\n\nThis is sample output; no provider was called.'
        return {**self.do_note_summary(data),'changed':self.summary_texts[data['task_uuid']]!=previous,'cloud_acknowledged':True,'readback_matches':True}

    def do_note_translate_options(self,data):
        if self.scenario=='empty':return {'targets':[],'source':None}
        return {'source':'zh','targets':[{'key':'en','label':'English'},{'key':'ja','label':'日本語'}]}

    def do_note_translate(self,data):
        if self.scenario=='attention':raise Refusal('The translation service could not read this note','unavailable')
        if data.get('next_token'):
            return {'paragraphs':[{'speaker':'Speaker 2','start_ms':5000,'end_ms':8000,'content':'Synthetic second translated paragraph.'}],'next_token':None,'source':'zh','target':'en','verified':True}
        more=self.scenario=='worst'
        return {'title':'Synthetic translation','summary':'# Synthetic translated summary\n\n**Preview only.**','keypoints':[{'topic':'Synthetic translated topic','summary':'Synthetic translated key point.','start_ms':0,'end_ms':4000}],'actions':[{'content':'Synthetic translated action.','start_ms':4000,'end_ms':5000}],'paragraphs':[{'speaker':'Speaker 1','start_ms':0,'end_ms':4000,'content':'Synthetic translated paragraph.'}],'next_token':'preview-next' if more else None,'source':'zh','target':data.get('target_language','en'),'verified':True}

    def do_note_export(self,data):
        file_format=data.get('file_format','txt')
        try:export.format_spec(data.get('kind'),file_format)
        except ValueError:raise Refusal('Choose a supported export format for this note type') from None
        if file_format=='docx':return b'PK\x03\x04Synthetic DOCX preview'
        if file_format=='pdf':return b'%PDF-1.4\nSynthetic PDF preview\n'
        if file_format=='markdown':return '# Synthetic '+data['kind']+' export\n\nPreview only.\n'
        if file_format=='srt':return '1\n00:00:00,000 --> 00:00:01,000\nSynthetic transcript preview.\n'
        return 'Synthetic '+data['kind']+' export\nSpeaker 1 00:00\nPreview only.\n'

    def do_note_media_download(self,data):
        if data.get('task_uuid') not in self.favorites:raise Refusal('Load this note from your own notes list before saving media')
        return {'saved':True,'filename':'synthetic-original.mp3','bytes':15,'sha256':'a'*64,'mime':'audio/mpeg','source':'download','expected_duration_ms':12500,'probe':{'format':'mp3','codec':'mp3','duration_seconds':12.5}}

    def do_note_favorite_status(self,data):
        uuid=data.get('task_uuid')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        self.favorite_pending=False
        return {'collected':self.favorites[uuid],'verified':True,'changed':False}

    def do_note_favorite(self,data):
        uuid=data.get('task_uuid');desired=data.get('collected');expected=data.get('expected_collected')
        if uuid not in self.favorites:raise Refusal('Load this note from your own notes list before using application actions')
        if type(desired) is not bool or type(expected) is not bool:raise Refusal('Favorite state must be an explicit boolean with the previously displayed value')
        if self.scenario=='attention':
            self.favorite_pending=True
            raise Refusal('The favorite result is unknown; refresh its state before any further change','outcome_unknown')
        if expected!=self.favorites[uuid]:raise Refusal('The favorite changed elsewhere; refresh it before changing it again')
        self.favorites[uuid]=desired
        return {'collected':desired,'verified':True,'changed':desired!=expected,'cloud_acknowledged':True,'readback_matches':True}

    def do_assistant(self,data):
        with self.lock:
            provider=data.get('provider','builtin')
            if provider not in ('builtin','custom'):raise Refusal('Unknown assistant provider')
            if provider=='custom' and not self.assistant:raise Refusal('No protected assistant configuration on this host','unavailable')
            if provider=='builtin' and self.scenario=='empty':raise Refusal('The native connection is not ready','unavailable')
            self.last_assistant_request=copy.deepcopy(data)
            text=data.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>32000:raise Refusal('Enter between 1 and 32,000 characters')
            if self.assistant_cancel is not None:raise Refusal('An assistant request is already in progress','busy')
            event=threading.Event();self.assistant_cancel=event
            self.assistant_request_id=data.get('request_id') or secrets.token_hex(16)
            if provider=='builtin':self.assistant_conversation=True
        try:
            if event.wait(1.5):return {'cancelled':True,'provider_outcome':'unknown'}
            with self.lock:self.assistant_recovery=False
            return {'text':f'Preview reply. You sent {len(text)} characters.\n\n- This is sample output.\n- No provider was called.','provider':provider,'route':'preview'}
        finally:
            with self.lock:
                if self.assistant_cancel is event:self.assistant_cancel=None;self.assistant_request_id=None

    def do_assistant_cancel(self,data):
        if self.assistant_cancel is None:return {'cancel_requested':False}
        if data.get('request_id')!=self.assistant_request_id:raise Refusal('The assistant request changed; refresh before cancelling')
        self.assistant_cancel.set();return {'cancel_requested':True,'provider_outcome':'unknown'}

    def do_assistant_conversations(self,data):
        if data:raise Refusal('Assistant conversation actions take no options')
        return {'conversations':[{'handle':'preview-conversation-one','title':'Synthetic planning conversation','date':'20261104'},{'handle':'preview-conversation-two','title':'Synthetic translation conversation','date':'20261103'}],'provider':'builtin','route':'preview conversation list; no provider was called'}

    def do_assistant_select_conversation(self,data):
        if set(data)!={'handle'} or data['handle'] not in {'preview-conversation-one','preview-conversation-two'}:raise Refusal('Refresh the built-in conversation list before restoring')
        self.assistant_conversation=True;self.assistant_recovery=False
        result=self.do_assistant_history({});result.update(restored=True,route='preview restored conversation; no provider was called');return result

    def do_assistant_history(self,data):
        if data:raise Refusal('Assistant conversation actions take no options')
        if not self.assistant_conversation:raise Refusal('Start a built-in conversation before reading its history')
        return {'messages':[{'query':'Summarize the synthetic recording','text':'Synthetic answer with one actionable item.','complete':True},{'query':'Translate the action item','text':'Synthetic translated action item.','complete':True}], 'text':'Synthetic translated action item.','provider':'builtin','route':'preview history; no provider was called'}

    def do_assistant_recover(self,data):
        if data:raise Refusal('Assistant conversation actions take no options')
        if not self.assistant_recovery:raise Refusal('No interrupted built-in request is available for recovery')
        self.assistant_recovery=False
        return {'text':'Synthetic recovered answer. No prompt was replayed.','recovered':True,'provider':'builtin','route':'preview task-result recovery'}

    def do_assistant_new(self,data):
        if data:raise Refusal('Assistant conversation actions take no options')
        self.assistant_conversation=False;self.assistant_recovery=False
        return {'new_conversation':True,'upstream_deleted':False}


PICKER='''<style>.preview-picker{{position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:90;display:flex;gap:2px;padding:3px;border-radius:8px;
max-width:calc(100vw - 24px);overflow-x:auto;white-space:nowrap;background:#16191b;color:#fff;font:12px/1 system-ui;box-shadow:0 4px 16px rgb(0 0 0/.3);opacity:.9}}
@media (max-width:767px){{.preview-picker{{bottom:auto;top:calc(56px + env(safe-area-inset-top,0px))}}}}</style>
<nav class="preview-picker" aria-label="Preview scenario">
<span style="padding:7px 8px;opacity:.7">Preview data</span>{links}</nav>'''


def make_handler(preview,frontend=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def send(self,status,body,content='application/json',extra=None):
            raw=body.encode() if isinstance(body,str) else body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
            content_type=content+'; charset=utf-8' if content.startswith('text/') or content=='application/json' else content
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                     'Content-Security-Policy':ui_page.csp(None),**(extra or {})}
            self.send_response(status);self.send_header('Content-Type',content_type)
            for k,v in headers.items():self.send_header(k,v)
            self.end_headers();self.wfile.write(raw)
            if self.command=='POST' and self.path.startswith('/api/') and status not in (403,404):
                preview.record_timing(urlsplit(self.path).path[5:],time.monotonic()-self.started,status<400)

        def authorized(self):
            cookies=dict(part.strip().split('=',1) for part in self.headers.get('Cookie','').split(';') if '=' in part)
            return secrets.compare_digest(cookies.get('qn_ui',''),preview.cookie)

        def refused(self):
            """The checks of web_backend.make_app's protect(), in the same order. Sends 403 when one fails."""
            port=self.server.server_address[1];allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
            path=urlsplit(self.path).path;site=self.headers.get('Sec-Fetch-Site','');origin=self.headers.get('Origin')
            reason=None
            if self.headers.get('Host','') not in allowed:reason='Loopback Host required'
            elif origin is not None and origin not in {'http://'+h for h in allowed}:reason='Same origin required'
            elif self.command=='GET' and path=='/':
                if site=='cross-site' and not (self.headers.get('Sec-Fetch-Mode')=='navigate' and self.headers.get('Sec-Fetch-Dest')=='document'):reason='Forbidden'
            elif self.command=='GET' and path=='/healthz':
                if site=='cross-site':reason='Forbidden'
            elif not self.authorized():reason='Open the local UI first'
            elif self.command!='GET' and not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),preview.csrf):reason='CSRF check failed'
            elif site=='cross-site':reason='Forbidden'
            if reason is not None:self.send(403,reason,'text/plain')
            return reason is not None

        def do_GET(self):
            if self.refused():return
            url=urlsplit(self.path);query=parse_qs(url.query)
            if url.path=='/':
                with preview.lock:
                    if query.get('scenario',[''])[0] in SCENARIOS:preview.load(query['scenario'][0])
                    current=preview.scenario
                nonce=secrets.token_urlsafe(18)
                page=ui_page.render(preview.csrf,nonce,HOST,template=frontend)
                links=''.join(f'<a href="/?scenario={s}" style="padding:7px 10px;border-radius:6px;color:#fff;text-decoration:none;{"background:#3a4046" if s==current else ""}">{s}</a>' for s in SCENARIOS)
                page=page.replace('</body>',PICKER.format(links=links)+'</body>')
                self.send(200,page,'text/html',{'Set-Cookie':f'qn_ui={preview.cookie}; HttpOnly; SameSite=Strict; Path=/','Content-Security-Policy':ui_page.csp(nonce)})
            elif url.path=='/healthz':
                with preview.lock:self.send(200,preview.health())
            elif url.path=='/api/status':
                try:since=int(query.get('since',[''])[0])
                except ValueError:since=None
                with preview.lock:self.send(200,preview.status(since if since is not None and since>=0 else None,query.get('settings_revision',[None])[0]))
            else:self.send(404,{'error':'NotFound'})

        def do_POST(self):
            self.started=time.monotonic()
            if self.refused():return
            url=urlsplit(self.path)
            if not url.path.startswith('/api/'):return self.send(404,{'error':'NotFound'})
            try:length=int(self.headers.get('Content-Length','0') or 0)
            except ValueError:return self.send(400,'Invalid Content-Length','text/plain')
            if not 0<=length<=64*1024:
                # Like the backend's 64 KiB client_max_size. Drain a bounded body so the reply is not reset.
                if length<=1024*1024:self.rfile.read(length)
                self.close_connection=True
                return self.send(413,'Request body too large','text/plain')
            try:data=json.loads(self.rfile.read(length) or b'{}')
            except ValueError:return self.send(409,{'error':'Refused','code':'refused','message':'The request body must be JSON'})
            if not isinstance(data,dict):return self.send(409,{'error':'Refused','code':'refused','message':'The request body must be a JSON object'})
            try:
                if url.path=='/api/assistant':result=preview.act('assistant',data)
                else:
                    with preview.lock:result=preview.act(url.path[5:],data)
            except KeyError:return self.send(404,{'error':'NotFound'})
            except Refusal as error:return self.send(409,{'error':'Refused','code':error.code,'message':str(error)})
            # As in the backend, only authored refusals are echoed; anything else gets the generic text.
            except (ValueError,PermissionError,ConnectionError,RuntimeError) as error:return self.send(409,describe(error))
            except Exception as error:return self.send(502,describe(error))
            if url.path=='/api/note-export':
                _,extension,mime,_=export.format_spec(data['kind'],data.get('file_format','txt'))
                return self.send(200,result,mime,{'Content-Disposition':'attachment; filename="qwennote-'+data['kind']+'.'+extension+'"'})
            self.send(200,result)
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port',type=int,default=18781)
    parser.add_argument('--scenario',choices=SCENARIOS,default='demo')
    parser.add_argument('--frontend',type=Path,help='Preview an independent HTML template instead of the default web.html')
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(Preview(args.scenario),frontend=args.frontend))
    print(f'UI preview with synthetic data at http://127.0.0.1:{args.port}/ (Ctrl+C to stop)')
    try:server.serve_forever()
    except KeyboardInterrupt:pass


if __name__=='__main__':main()
