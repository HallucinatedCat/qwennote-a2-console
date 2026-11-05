import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from capture_report import analyze
from device_protocol import decode_push
from live_diagnostics import PushCapture
from web_backend import Backend,make_app


def packet(sequence=1,final=False,text='private synthetic transcript',kind=770001):
    payload={'recordId':456,'taskId':789,'sequence':sequence,'eventEnum':4,
             'textType':'ParagraphEnd' if final else 'SentenceChange',
             'content':json.dumps({'stableResult':[{'index':0,'result':text,'targetLang':'en'}]})}
    row={'objectType':kind,'streamId':'dtiot.dinger','data':base64.b64encode(json.dumps(payload).encode()).decode()}
    return {'lwp':'/s/para','body':{'syncPushPackage':{'data':[row]}}}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.clock=100.0
        self.capture=PushCapture(self.path,clock=lambda:self.clock)

    def tearDown(self):self.capture.stop();self.tmp.cleanup()

    def test_disabled_capture_creates_nothing(self):
        p=packet();self.capture.observe_push(p,decode_push(p),1)
        self.assertEqual(list(self.path.iterdir()),[])
        self.assertIsNone(self.capture.snapshot()['capture_id'])

    def test_private_files_and_public_counts(self):
        self.capture.start();p=packet(final=True);self.capture.observe_push(p,decode_push(p),1)
        public=self.capture.stop();folder=self.capture.directory
        for directory in (folder,folder.parent):self.assertEqual(directory.stat().st_mode&0o777,0o700)
        for file in folder.iterdir():self.assertEqual(file.stat().st_mode&0o777,0o600)
        raw=json.loads((folder/'pushes.jsonl').read_text());self.assertEqual(decode_push(raw['packet'])[0]['payload']['recordId'],456)
        self.assertEqual(public['transcript_candidates'],1);self.assertEqual(public['transcript_updates'],1)
        for result in (public,analyze(folder)):
            text=json.dumps(result)
            for private in ('private synthetic transcript','recordId','taskId'):self.assertNotIn(private,text)
        self.assertFalse(analyze(folder)['hardware_behavior_verified'])

    def test_expiry_prevents_late_message_capture(self):
        self.capture.start(2);self.clock+=3
        self.capture.observe_push(packet(),[],0)
        result=self.capture.snapshot();self.assertFalse(result['active'])
        self.assertEqual(result['finish_reason'],'time_limit');self.assertEqual(result['push_frames'],0)

    def test_duration_and_duplicate_start_validation(self):
        for duration in (0,301,True,1.5,'2',None):
            with self.assertRaises(ValueError):self.capture.start(duration)
        first=self.capture.start()
        with self.assertRaises(ValueError):self.capture.start()
        self.assertEqual(first['capture_id'],self.capture.snapshot()['capture_id'])

    def test_row_and_byte_limits_stop_without_excess_writes(self):
        capture=PushCapture(self.path,row_limit=2);capture.start();p=packet()
        for _ in range(3):capture.observe_push(p,decode_push(p),0)
        result=capture.snapshot();self.assertEqual(result['rows_written'],2);self.assertEqual(result['finish_reason'],'row_limit')
        capture=PushCapture(self.path,byte_limit=100);capture.start();capture.observe_push(p,decode_push(p),0)
        result=capture.snapshot();self.assertEqual(result['finish_reason'],'byte_limit')
        self.assertEqual((capture.directory/'pushes.jsonl').stat().st_size,0)
        self.assertTrue(analyze(capture.directory)['capture_truncated'])
        self.assertTrue(analyze(capture.directory)['possible_missing_events'])

    def test_linked_or_public_capture_root_rejected(self):
        target=self.path/'outside';target.mkdir(mode=0o700)
        self.capture.root.symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):self.capture.start()
        self.assertEqual(list(target.iterdir()),[])
        self.capture.root.unlink();self.capture.root.mkdir(mode=0o755);self.capture.root.chmod(0o755)
        with self.assertRaises(ValueError):self.capture.start()

    def test_total_session_limit_preserves_old_files(self):
        self.capture.root.mkdir(mode=0o700)
        for index in range(16):(self.capture.root/str(index)).mkdir(mode=0o700)
        with self.assertRaises(ValueError):self.capture.start()
        self.assertEqual(len(list(self.capture.root.iterdir())),16)

    def test_disk_failure_disables_only_capture(self):
        self.capture.start();p=packet();backend=Backend(self.path,18780);backend.diagnostics=self.capture
        with patch('live_diagnostics.os.write',side_effect=OSError('sensitive detail')):
            backend.process_push(p)
        self.assertEqual(len(backend.transcripts.snapshot()['rows']),1)
        result=self.capture.snapshot()
        self.assertFalse(result['active']);self.assertEqual(result['finish_reason'],'io_error')
        self.assertNotIn('sensitive detail',json.dumps(result));self.assertTrue(result['summary_saved'])

    def test_reconnect_and_drop_counter_reset(self):
        self.capture.start(connected=True,queue_dropped=5)
        self.capture.observe_queue(7);self.capture.connection(False,8)
        self.capture.connection(True,0);self.capture.observe_queue(3)
        result=self.capture.stop()
        self.assertEqual(result['queue_dropped'],6);self.assertEqual(result['connections'],2)
        self.assertEqual(result['disconnects'],1);self.assertTrue(analyze(self.capture.directory)['possible_missing_events'])

    def test_private_replay_counts_ordering_and_malformed_packets(self):
        backend=Backend(self.path,18780);backend.diagnostics=self.capture;self.capture.start()
        for p in (packet(2,True),packet(1),packet(2,True),packet(3,True,'correction'),packet(kind=770000),{'lwp':'/unknown'}, {'lwp':'/s/para','body':'invalid JSON'}):
            backend.process_push(p)
        self.capture.stop();report=analyze(self.capture.directory)
        self.assertEqual(report['sequence_decreases'],1);self.assertEqual(report['replayed_updates'],2)
        self.assertEqual(report['final_paragraphs_in_bounded_replay'],1);self.assertEqual(report['decoder_errors'],1)
        self.assertEqual(report['unrecognized_frames'],1);self.assertEqual(report['device_events'],1)
        self.assertEqual(self.capture.snapshot()['transcript_updates'],2)
        self.assertNotIn('correction',json.dumps(report))

    def test_unfinished_capture_is_reported_as_incomplete(self):
        self.capture.start();p=packet();self.capture.observe_push(p,decode_push(p),1)
        report=analyze(self.capture.directory)
        self.assertFalse(report['capture_was_finalized']);self.assertTrue(report['possible_missing_events'])

    def test_replay_rejects_link_or_public_file(self):
        self.capture.start();self.capture.stop();file=self.capture.directory/'pushes.jsonl'
        file.chmod(0o644)
        with self.assertRaises(ValueError):analyze(self.capture.directory)
        file.unlink();file.symlink_to(self.capture.directory/'summary.json')
        with self.assertRaises(OSError):analyze(self.capture.directory)


class CaptureAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.backend=Backend(Path(self.tmp.name),1)
        self.backend.start=AsyncMock();self.backend.stop=AsyncMock()
        self.backend.channel=SimpleNamespace(ready=True,dropped_pushes=0,_request=AsyncMock())
        self.backend.connection='connected'
        self.client=TestClient(TestServer(make_app(self.backend)))
        await self.client.start_server();self.backend.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+self.backend.cookie,'X-CSRF-Token':self.backend.csrf}

    async def asyncTearDown(self):
        self.backend.stop_capture('shutdown');await self.client.close();self.tmp.cleanup()

    async def test_capture_sends_no_command_and_does_not_arm_controls(self):
        response=await self.client.post('/api/capture-start',headers=self.headers,json={'seconds':20})
        self.assertEqual(response.status,200);self.assertTrue((await response.json())['active'])
        response=await self.client.post('/api/capture-stop',headers=self.headers,json={})
        self.assertEqual(response.status,200);self.assertFalse((await response.json())['active'])
        self.backend.channel._request.assert_not_awaited();self.assertEqual(self.backend.ready_until,0)

    async def test_capture_requires_cookie_csrf_and_bounded_duration(self):
        response=await self.client.post('/api/capture-start',json={});self.assertEqual(response.status,403)
        response=await self.client.post('/api/capture-start',headers={'Cookie':'qn_ui='+self.backend.cookie},json={})
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/capture-start',headers=self.headers,json={'seconds':301})
        self.assertEqual(response.status,409);self.assertFalse(self.backend.diagnostics.snapshot()['active'])

    async def test_timer_closes_capture_without_events_or_polling(self):
        self.backend.start_capture(1)
        await asyncio.sleep(1.1)
        self.assertIsNone(self.backend.diagnostics.fd)
        summary=json.loads((self.backend.diagnostics.directory/'summary.json').read_text())
        self.assertFalse(summary['active']);self.assertEqual(summary['finish_reason'],'time_limit')
        self.backend.channel._request.assert_not_awaited()

if __name__=='__main__':unittest.main()
