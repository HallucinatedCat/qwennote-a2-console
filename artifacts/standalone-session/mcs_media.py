"""Receive-only Opus probe. Protocol: subscription/soac as the official app uses it.

Optional aiortc dependency. No microphone, local media tracks, speaker playback or audio files.
Raw SDP stays in MCS's protected trace; public results contain counts only.
"""
import array
import asyncio
import contextlib
import ipaddress
import uuid
from aiortc import RTCPeerConnection,RTCConfiguration,RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from aiortc.mediastreams import MediaStreamError


def envelope(data):return {'data':data,'trace-id':str(uuid.uuid4()),'tid':str(uuid.uuid4())}


def active_audio_stream(login):
    if not isinstance(login,list) or len(login)<2 or login[0]!='ok':raise ValueError('Missing MCS room')
    streams=login[1].get('room',{}).get('streams',[])
    for stream in streams:
        audio=stream.get('media',{}).get('audio')
        if isinstance(audio,dict) and audio.get('status')=='active' and isinstance(stream.get('id'),str):
            return stream['id']
    raise ValueError('No active conference audio stream')


def public_candidate(candidate):
    try:return ipaddress.ip_address(candidate.ip).is_global
    except ValueError:return False


def validate_answer(sdp):
    if not isinstance(sdp,str) or len(sdp)>262144 or not sdp.startswith('v=0'):raise ValueError('Invalid SDP answer')
    lines=[];public_count=0;candidate_count=0
    for line in sdp.splitlines():
        if line.startswith('a=candidate:'):
            candidate_count+=1
            if not public_candidate(candidate_from_sdp(line[len('a=candidate:'):])):continue
            public_count+=1
        lines.append(line)
    if candidate_count and not public_count:raise ValueError('No public remote ICE candidate')
    return '\r\n'.join(lines)+'\r\n'


class AudioReceiver:
    def __init__(self,mcs):
        self.mcs=mcs;self.pc=RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.subscription=None;self.worker=None;self.tracks=[];self.closed=False;self.error_type=None
        self.frames=0;self.samples=0;self.seconds=0.;self.nonzero_frames=0;self.peak=0
        self.states=[];self.answer_received=False;self.subscription_ready=False;self.unsubscribe_ack=False
        self.pc.addTransceiver('audio',direction='recvonly')
        @self.pc.on('track')
        def on_track(track):
            if track.kind=='audio':self.tracks.append(asyncio.create_task(self.consume(track)))
        @self.pc.on('connectionstatechange')
        async def on_state():self.states.append(self.pc.connectionState)
    async def consume(self,track):
        try:
            while True:
                frame=await track.recv();self.frames+=1;self.samples+=frame.samples
                if frame.sample_rate:self.seconds+=frame.samples/frame.sample_rate
                if frame.format.name in ('s16','s16p'):
                    peak=0
                    sample_bytes=2*frame.samples*(1 if frame.format.is_planar else len(frame.layout.channels))
                    for plane in frame.planes:
                        # Decoder allocation padding is not part of the audio signal.
                        values=array.array('h',bytes(plane)[:sample_bytes]);peak=max(peak,max((abs(v) for v in values),default=0))
                    self.peak=max(self.peak,peak)
                    if peak>0:self.nonzero_frames+=1
        except (MediaStreamError,asyncio.CancelledError):pass
        except Exception as error:self.error_type=type(error).__name__
    async def open(self,login):
        stream=active_audio_stream(login)
        ack=await self.mcs.request('subscribe',envelope({'media':{'audio':{'from':stream},'video':False}}))
        if not isinstance(ack,list) or len(ack)<2 or ack[0]!='ok' or not isinstance(ack[1],dict):
            raise ValueError('Audio subscription rejected')
        identifier=ack[1].get('id')
        if not isinstance(identifier,str) or not identifier or len(identifier)>512:raise ValueError('Missing audio subscription identity')
        self.subscription=identifier;self.worker=asyncio.create_task(self.events())
        offer=await self.pc.createOffer();await asyncio.wait_for(self.pc.setLocalDescription(offer),8)
        ack=await self.mcs.request('soac',envelope({'id':identifier,'signaling':{'type':'offer','sdp':self.pc.localDescription.sdp,'options':{}}}))
        if not isinstance(ack,list) or not ack or ack[0]!='ok':raise ValueError('SDP offer rejected')
    async def events(self):
        try:
            while True:
                name,body=await self.mcs.notifications.get()
                if name!='progress' or body.get('id')!=self.subscription:continue
                status=body.get('status');data=body.get('data')
                if status=='ready':self.subscription_ready=True
                elif status=='error':raise ValueError('Media progress error')
                elif status=='soac' and isinstance(data,dict):
                    if data.get('type')=='answer':
                        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=validate_answer(data.get('sdp')),type='answer'))
                        self.answer_received=True
                    elif data.get('type')=='candidate':
                        obj=data.get('candidate')
                        if isinstance(obj,dict) and isinstance(obj.get('candidate'),str):
                            raw=obj['candidate'].removeprefix('candidate:');candidate=candidate_from_sdp(raw)
                            if not public_candidate(candidate):continue
                            candidate.sdpMid=obj.get('sdpMid');candidate.sdpMLineIndex=obj.get('sdpMLineIndex')
                            await self.pc.addIceCandidate(candidate)
        except asyncio.CancelledError:raise
        except Exception as error:self.error_type=type(error).__name__
    async def close(self):
        if self.closed:return
        self.closed=True
        if self.subscription and self.mcs.ready:
            try:
                ack=await self.mcs.request('unsubscribe',envelope({'id':self.subscription}))
                self.unsubscribe_ack=isinstance(ack,list) and bool(ack) and ack[0]=='ok'
            except Exception as error:
                if self.error_type is None:self.error_type=type(error).__name__
        await self.pc.close()
        for task in ([self.worker] if self.worker else [])+self.tracks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await task
    def summary(self):
        return {'subscription_accepted':self.subscription is not None,'answer_received':self.answer_received,
                'subscription_ready_event':self.subscription_ready,'connection_states':self.states,
                'decoded_audio_frames':self.frames,'decoded_samples_per_channel':self.samples,'decoded_audio_seconds':round(self.seconds,3),
                'nonzero_pcm_frames':self.nonzero_frames,'peak_absolute_pcm':self.peak,'unsubscribe_acknowledged':self.unsubscribe_ack,
                'error_type':self.error_type,'audio_saved':False,'audio_played':False,'local_media_published':False}
