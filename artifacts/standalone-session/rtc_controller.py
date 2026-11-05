"""Host RTC ownership and bounded background lifecycle; one existing refresh owner."""
import asyncio
import contextlib
import importlib.util
import json
import secrets
import time
from live_diagnostics import protected_directory
from lwp_client import private_write
from rtc_session import run_probe
from public_errors import Refused



MAX_SECONDS=4*3600
class ChannelFeed:
    """Fan-out only: never consume the host's native receive queue."""
    def __init__(self,channel):
        self.channel=channel;self.pushes=asyncio.Queue(maxsize=512);self.dropped=0
    @property
    def ready(self):return self.channel.ready
    @property
    def dropped_pushes(self):return self.channel.dropped_pushes+self.dropped
    async def _request(self,*args,**kwargs):return await self.channel._request(*args,**kwargs)
    def accept(self,message):
        if self.pushes.full():self.pushes.get_nowait();self.dropped+=1
        self.pushes.put_nowait(message)


class RTCController:
    def __init__(self,backend):
        self.backend=backend;self.directory=backend.state/'rtc-runs'
        protected_directory(backend.state)
        self.directory.mkdir(mode=0o700,exist_ok=True);protected_directory(self.directory)
        self.journal=self.directory/'rtc-probe-pending.json';self.summary_path=self.directory/'last-result.json'
        self.task=None;self.feed=None;self.entered=False;self.active=False;self.stopping=False;self.started_at=None
        self.seconds=None;self.error_type=None;self.last_result=None;self.pending=False
        self.audio_available=importlib.util.find_spec('aiortc') is not None
        if self.journal.exists():
            if self.journal.is_symlink():raise ValueError('RTC journal must not be a link')
            self.pending=json.loads(self.journal.read_text()).get('pending') is True
        if self.summary_path.exists():
            if self.summary_path.is_symlink():raise ValueError('RTC summary must not be a link')
            self.last_result=json.loads(self.summary_path.read_text())
    def status(self):
        return {'active':self.active,'stopping':self.stopping,'cleanup_pending':self.pending,
                'started_at':self.started_at,'observation_seconds':self.seconds,'maximum_seconds':MAX_SECONDS,
                'audio_receive_available':self.audio_available,'last_result':self.last_result,'error_type':self.error_type,
                'capabilities':{'conference_lifecycle':'live-verified','receive_audio':'decoded frames under quiet input',
                                'intelligible_device_speech':'unverified','live_asr':'unverified','speaker_output':'not implemented'}}
    def require_clear(self):
        if self.active or self.pending:raise Refused('A realtime test is running or awaiting cleanup')
    def accept(self,message):
        if self.feed is not None:self.feed.accept(message)
    async def start(self,seconds=20,receive_audio=False):
        # The official app records a 4G A2 with live text this way (createRtcTranslateTask,
        # conference join, keep-alives). The recorder uses it as its live-transcript mode, which
        # runs until Stop; the Dev test uses short windows.
        if type(seconds) is not int or not 1<=seconds<=MAX_SECONDS or type(receive_audio) is not bool:
            raise Refused('Choose a live transcript length from 1 second to 4 hours')
        b=self.backend;b.require_ready()
        # Note operations refuse while RTC runs (rtc.require_clear), so RTC also waits for them.
        if b.own_test or b.mutation_lock.locked() or any(lock.locked() for lock in b.note_locks()):
            raise Refused('Another device operation is active')
        b.require_no_cloud_recording()
        if not b.device or b.device.get('cloud_online_status')!=1:raise Refused('The A2 is not online in the cloud')
        if b.config.get('expires_at',0)<time.time()+120:raise Refused('The session renews within two minutes; start the realtime test after renewal')
        # The planned session renewal ends the conference, so the window stops just before it.
        seconds=max(1,min(seconds,int(b.config['expires_at']-time.time()-120)))
        if receive_audio:
            if not self.audio_available:raise Refused('Audio measurement needs the optional aiortc dependency on this host')
            from mcs_media import AudioReceiver  # Validate the optional native dependency before reserving ownership.
        directory=self.directory/('session-'+secrets.token_hex(8));directory.mkdir(mode=0o700)
        # Reserve before returning; concurrent mutation/start requests cannot slip in while the task is scheduled.
        private_write(self.journal,{'pending':True,'probe_directory':directory.name,'created_at':time.time(),'phase':'reserved'})
        self.pending=True;self.active=True;self.stopping=False;self.started_at=time.time();self.seconds=seconds
        self.error_type=None;self.last_result=None;self.entered=False;self.feed=ChannelFeed(b.channel)
        self.task=asyncio.create_task(self._run(directory,seconds,receive_audio))
        return {'started':True,'observation_seconds':seconds,'automatic_stop':True,'audio_receive':receive_audio}
    async def _run(self,directory,seconds,receive_audio):
        result=None
        try:
            self.entered=True
            if self.stopping:
                private_write(self.journal,{'pending':False,'phase':'cancelled-before-start'})
                return
            async with self.backend.mutation_lock:
                try:self.backend.require_no_cloud_recording()
                except Refused:
                    # No request was sent after reservation; do not inherit an external task.
                    private_write(self.journal,{'pending':False,'phase':'blocked-before-start'})
                    raise
                result=await run_probe(self.feed,self.backend.config,directory,seconds,
                                       join_signaling=True,receive_audio=receive_audio,start_recording=True)
        except asyncio.CancelledError:
            self.error_type='CancelledError'
        except Exception as error:
            self.error_type=type(error).__name__
        finally:
            self.feed=None;self.active=False;self.stopping=False;self.backend.ready_until=0
            # Do not infer cleanup from task exit; only the persistent lifecycle result can settle ownership.
            if self.journal.exists():
                saved=json.loads(self.journal.read_text());self.pending=saved.get('pending') is True
                result=result or saved.get('result')
            if isinstance(result,dict):
                self.last_result=result;private_write(self.summary_path,result)
            self.backend.emit('rtc',{'active':False,'cleanup_pending':self.pending,'error_type':self.error_type})
    def request_stop(self):
        if self.active and self.task is not None:
            self.stopping=True
            if self.entered and not self.task.cancelling():self.task.cancel()
        return {'stop_requested':self.stopping,'cleanup_pending':self.pending,'active':self.active}
    async def shutdown(self):
        if self.task is not None:
            self.request_stop()
            with contextlib.suppress(asyncio.CancelledError):await self.task
