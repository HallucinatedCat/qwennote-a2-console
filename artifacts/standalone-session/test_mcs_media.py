import array
import asyncio
import unittest
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace
from aiortc.mediastreams import MediaStreamError
from aiortc import RTCPeerConnection,RTCConfiguration,RTCSessionDescription,AudioStreamTrack
from mcs_media import AudioReceiver,active_audio_stream,validate_answer

class MediaContractTests(unittest.TestCase):
    def test_refuses_private_ice_candidates(self):
        for address in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','host.local']:
            sdp='v=0\r\na=candidate:1 1 udp 2130706431 '+address+' 12345 typ host\r\n'
            with self.assertRaises(ValueError):validate_answer(sdp)
        self.assertTrue(validate_answer('v=0\r\na=candidate:1 1 udp 2130706431 8.8.8.8 12345 typ host\r\n'))
    def test_mixed_candidates_keep_only_public_routes(self):
        sdp='v=0\r\na=candidate:1 1 udp 1 10.0.0.1 12345 typ host\r\na=candidate:2 1 udp 1 8.8.8.8 12345 typ host\r\n'
        result=validate_answer(sdp);self.assertNotIn('10.0.0.1',result);self.assertIn('8.8.8.8',result)
    def test_chooses_only_advertised_active_audio(self):
        with self.assertRaises(ValueError):active_audio_stream(['ok',{'room':{'streams':[]}}])
        self.assertEqual(active_audio_stream(['ok',{'room':{'streams':[{'id':'own-conference-stream','media':{'audio':{'status':'active'}}}]}}]),'own-conference-stream')

class TestTone(AudioStreamTrack):
    async def recv(self):
        frame=await super().recv()
        for plane in frame.planes:
            values=array.array('h',(1000 if (i//4)%2 else -1000 for i in range(plane.buffer_size//2)))
            plane.update(values.tobytes())
        return frame

class LocalConference:
    ready=True
    def __init__(self):
        self.notifications=asyncio.Queue();self.pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));self.pc.addTrack(TestTone());self.calls=[]
    async def request(self,name,body):
        self.calls.append(name);data=body['data']
        if name=='subscribe':
            if data['media']['video'] is not False:raise ValueError('Video should be disabled')
            return ['ok',{'id':'fixture-subscription'}]
        if name=='soac':
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=data['signaling']['sdp'],type='offer'))
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            self.notifications.put_nowait(('progress',{'id':'fixture-subscription','status':'soac','data':{'type':'answer','sdp':self.pc.localDescription.sdp}}))
            self.notifications.put_nowait(('progress',{'id':'fixture-subscription','status':'ready'}))
        return ['ok']

class MediaLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_meter_ignores_padding_in_packed_and_planar_frames(self):
        for planar in (False,True):
            for samples in ([0,0,0,0],[0,700,0,-900]):
                with self.subTest(planar=planar,samples=samples):
                    channels=[samples[::2],samples[1::2]] if planar else [samples]
                    planes=[array.array('h',values+[30000]*8).tobytes() for values in channels]
                    frame=SimpleNamespace(samples=2,sample_rate=48000,planes=planes,
                        format=SimpleNamespace(name='s16p' if planar else 's16',is_planar=planar),
                        layout=SimpleNamespace(channels=('left','right')))
                    track=SimpleNamespace(recv=AsyncMock(side_effect=[frame,MediaStreamError()]))
                    receiver=AudioReceiver(SimpleNamespace(ready=False))
                    try:
                        await receiver.consume(track)
                        self.assertIsNone(receiver.error_type)
                        self.assertEqual(receiver.peak,max(abs(value) for value in samples))
                        self.assertEqual(receiver.nonzero_frames,int(any(samples)))
                    finally:await receiver.close()

    async def test_receive_only_sdp_opus_decode_and_teardown(self):
        conference=LocalConference();receiver=AudioReceiver(conference)
        try:
            # This test explicitly permits only its local in-process peer's answer.
            with patch('mcs_media.validate_answer',lambda value:value):
                await receiver.open(['ok',{'room':{'streams':[{'id':'fixture-stream','media':{'audio':{'status':'active'}}}]}}])
                async def wait_frames():
                    while receiver.frames<5 and receiver.error_type is None:await asyncio.sleep(.02)
                await asyncio.wait_for(wait_frames(),5)
            self.assertIsNone(receiver.error_type);self.assertGreaterEqual(receiver.frames,5)
            self.assertTrue(all(sender.track is None for sender in receiver.pc.getSenders()))
            self.assertGreater(receiver.nonzero_frames,0)
        finally:
            await receiver.close();await conference.pc.close()
        summary=receiver.summary();self.assertTrue(summary['answer_received']);self.assertTrue(summary['unsubscribe_acknowledged'])
        self.assertFalse(summary['audio_saved']);self.assertFalse(summary['audio_played']);self.assertFalse(summary['local_media_published'])
        self.assertEqual(conference.calls,['subscribe','soac','unsubscribe'])

if __name__=='__main__':unittest.main()
