import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from web_backend import Backend,make_app
from rtc_controller import ChannelFeed
from lwp_client import private_write
from test_backend import CONFIG


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.b=Backend(self.root,18780)
        self.b.config={**CONFIG,'expires_at':time.time()+3600};self.b.device={'model':'A2','cloud_online_status':1}
        self.b.channel=type('Channel',(),{'ready':True,'dropped_pushes':0,'_request':AsyncMock()})()
        self.b.ready_until=time.time()+60;self.b.connection='connected';self.gate=asyncio.Event();self.entered=asyncio.Event()
    async def asyncTearDown(self):
        await self.b.rtc.shutdown();self.tmp.cleanup()
    async def fake_lifecycle(self,channel,config,directory,seconds,**options):
        self.assertTrue(options['start_recording']);self.assertTrue(options['join_signaling']);self.entered.set()
        try:await self.gate.wait()
        finally:
            result={'cleanup_pending':False,'transcript_rows':0,'conference_end_readback':True}
            private_write(directory.parent/'rtc-probe-pending.json',{'pending':False,'result':result})
        return result
    async def test_lease_expiry_does_not_prevent_stop_and_cleanup(self):
        with patch('rtc_controller.run_probe',self.fake_lifecycle):
            await self.b.rtc.start(10);await self.entered.wait();self.b.ready_until=0
            self.b.rtc.request_stop();self.b.rtc.request_stop();await self.b.rtc.task
        self.assertFalse(self.b.rtc.active);self.assertFalse(self.b.rtc.pending)
        self.assertTrue(self.b.rtc.last_result['conference_end_readback'])
    async def test_immediate_stop_before_task_runs_sends_nothing(self):
        lifecycle=AsyncMock()
        with patch('rtc_controller.run_probe',lifecycle):
            await self.b.rtc.start(10);self.b.rtc.request_stop();await self.b.rtc.task
        lifecycle.assert_not_awaited();self.assertFalse(self.b.rtc.active);self.assertFalse(self.b.rtc.pending)
    async def test_mutations_fail_immediately_instead_of_queueing_behind_rtc(self):
        with patch('rtc_controller.run_probe',self.fake_lifecycle):
            await self.b.rtc.start(10);await self.entered.wait()
            for op in [lambda:self.b.record('start'),lambda:self.b.change_setting('enable_incognito_mode','0'),lambda:self.b.change_generation({}),lambda:self.b.change_notification('1'),lambda:self.b.rtc.start(10)]:
                with self.assertRaises(ValueError):await asyncio.wait_for(op(),.2)
            self.gate.set();await self.b.rtc.task
        self.b.channel._request.assert_not_awaited()
    async def test_pending_journal_blocks_controls_after_restart(self):
        private_write(self.b.rtc.journal,{'pending':True})
        restarted=Backend(self.root,18780);self.assertTrue(restarted.rtc.pending)
        with self.assertRaises(ValueError):await restarted.record('start')
    async def test_existing_cloud_recording_blocks_rtc_before_reservation(self):
        from test_record_tasks import TASK
        import record_tasks
        self.b.record_task=record_tasks.parse(self.b.config,TASK)
        with self.assertRaises(ValueError):await self.b.rtc.start()
        self.assertFalse(self.b.rtc.journal.exists());self.assertFalse(self.b.rtc.active)
        self.b.channel._request.assert_not_awaited()

    async def test_external_task_arriving_after_reservation_sends_nothing(self):
        from test_record_tasks import TASK
        import record_tasks
        lifecycle=AsyncMock()
        with patch('rtc_controller.run_probe',lifecycle):
            await self.b.rtc.start()
            self.b.record_task=record_tasks.parse(self.b.config,TASK)
            await self.b.rtc.task
        lifecycle.assert_not_awaited();self.b.channel._request.assert_not_awaited()
        self.assertFalse(self.b.rtc.pending);self.assertFalse(self.b.rtc.active)
        self.assertEqual(json.loads(self.b.rtc.journal.read_text())['phase'],'blocked-before-start')

    async def test_readiness_duration_online_and_session_expiry_guards(self):
        for seconds in [True,0,4*3600+1,'10']:
            with self.assertRaises(ValueError):await self.b.rtc.start(seconds)
        self.b.ready_until=0
        with self.assertRaises(PermissionError):await self.b.rtc.start()
        self.b.ready_until=time.time()+60;self.b.device['cloud_online_status']=0
        with self.assertRaises(ValueError):await self.b.rtc.start()
        self.b.device['cloud_online_status']=1;self.b.config['expires_at']=time.time()+30
        with self.assertRaises(ValueError):await self.b.rtc.start()
        self.assertFalse(self.b.rtc.active);self.assertFalse(self.b.rtc.journal.exists())
    async def test_push_fanout_keeps_host_processing_and_accounts_for_drops(self):
        feed=ChannelFeed(self.b.channel);self.b.rtc.feed=feed
        packet={'lwp':'/s/para','body':{}}
        with patch.object(self.b.diagnostics,'observe_push') as observer:self.b.process_push(packet)
        observer.assert_called_once();self.assertIs(await feed.pushes.get(),packet)
        for _ in range(515):feed.accept(packet)
        self.assertEqual(feed.pushes.qsize(),512);self.assertEqual(feed.dropped_pushes,3)
    async def test_unknown_failure_keeps_pending_and_no_secret_in_status(self):
        with patch('rtc_controller.run_probe',AsyncMock(side_effect=RuntimeError('private fixture token'))):
            await self.b.rtc.start();await self.b.rtc.task
        self.assertTrue(self.b.rtc.pending)
        self.assertNotIn('private fixture token',json.dumps(self.b.status()))
    async def test_http_rtc_routes_enforce_csrf_and_return_promptly(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)));await client.start_server()
        try:
            b.port=client.port
            await client.get('/')
            denied=await client.post('/api/rtc-start',json={'seconds':10});self.assertEqual(denied.status,403)
            headers={'Origin':f'http://127.0.0.1:{b.port}','X-CSRF-Token':b.csrf,'Cookie':'qn_ui='+b.cookie}
            with patch('rtc_controller.run_probe',self.fake_lifecycle):
                response=await client.post('/api/rtc-start',json={'seconds':10},headers=headers)
                self.assertEqual(response.status,200);self.assertTrue((await response.json())['automatic_stop'])
                await self.entered.wait()
                stopped=await client.post('/api/rtc-stop',json={},headers=headers);self.assertEqual(stopped.status,200)
                await b.rtc.task
        finally:await client.close()

if __name__=='__main__':unittest.main()
