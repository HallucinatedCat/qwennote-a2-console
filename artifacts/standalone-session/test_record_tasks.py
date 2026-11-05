"""Cloud task identity, ordering and read-only backend contract; no device IO."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from test_backend_improvements import CONFIG,devices,ready_backend
from public_errors import Busy,Refused,Unavailable
from web_backend import make_app
import record_tasks as p

TASK={'realTimeAsr':False,'keepAlivePeriodSecond':0,'location':{'title':'private-place'},
      'deviceTask':{'accountId':CONFIG['account_id'],'accountType':2,'deviceId':456,'taskType':0,
                    'taskId':'private-task','state':0,'stateCode':0,'startTime':1000,'updateTime':2000,
                    'attributes':{'fid':'777888','private':'private-text'}}}


def event(task=None):
    return {'type':770000,'stream_id':'dtiot.dinger','payload':{'eventType':1,'eventTime':2000,
            'deviceTaskEventDTO':{'taskType':0,'recordTask':copy.deepcopy(TASK if task is None else task)}}}


class ProtocolTests(unittest.TestCase):
    def test_query_and_public_shape_never_include_private_values(self):
        task=p.parse(CONFIG,TASK);body=p.query(CONFIG,task.fid)[0]
        self.assertEqual(body['deviceId'],456);self.assertEqual(body['fid'],777888)
        self.assertIsInstance(body['accountId'],str)
        view=task.summary();self.assertIs(view['real_time_asr'],False)
        self.assertFalse(view['physical_state_verified'])
        for value in ('777888','456','123','private-task','private-place','private-text'):
            self.assertNotIn(value,json.dumps(view))
        self.assertNotIn('private-task',repr(task))

    def test_false_missing_and_invalid_flags_are_distinct(self):
        for value in (True,False,None):
            task=copy.deepcopy(TASK);task['realTimeAsr']=value
            self.assertIs(p.parse(CONFIG,task).real_time_asr,value)
        for value in ('false',0,[],{}):
            task=copy.deepcopy(TASK);task['realTimeAsr']=value
            with self.assertRaises(ValueError):p.parse(CONFIG,task)

    def test_identity_fid_and_timestamp_validation(self):
        for key,value in [('accountId','other'),('accountType',True),('deviceId',999),('taskType',1),('state',True),('updateTime',-1)]:
            task=copy.deepcopy(TASK);task['deviceTask'][key]=value
            with self.subTest(field=key),self.assertRaises(ValueError):p.parse(CONFIG,task)
        for fid in (True,'0','-1',[],2**63):
            with self.subTest(fid=fid),self.assertRaises(ValueError):p.query(CONFIG,fid)
        with self.assertRaises(ValueError):p.parse(CONFIG,TASK,expected_fid=42)

    def test_reply_requires_success_and_record_task(self):
        self.assertEqual(p.parse_reply(CONFIG,{'code':200,'body':{'recordTask':TASK}},777888).state,0)
        for reply in ({'code':403,'body':{'recordTask':TASK}},{'code':200,'body':{}},{'code':200,'body':{'code':500,'recordTask':TASK}}):
            with self.assertRaises(ValueError):p.parse_reply(CONFIG,reply,777888)

    def test_only_matching_task_pushes_are_accepted(self):
        self.assertEqual(p.from_event(CONFIG,event()).fid,777888)
        for change in ({'type':86000},{'stream_id':'other'},{'payload':{'fileEventDTO':{}}}):
            self.assertIsNone(p.from_event(CONFIG,{**event(),**change}))


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.b=ready_backend(Path(self.tmp.name))
    async def asyncTearDown(self):
        self.b.flush_latest_event();self.tmp.cleanup()

    async def test_start_checks_the_device_first(self):
        import time
        b=self.b;b.ready_until=time.time()+60
        def reply(state):
            task=copy.deepcopy(TASK);task['deviceTask']['state']=state
            return {'code':200,'body':{'recordTask':task}}
        for state in (0,5):
            b.own_test=False;b.channel._request.reset_mock(return_value=True)
            b.channel.read.return_value=devices(properties=[('recording_fid','777888')]);b.channel._request.return_value=reply(state)
            with self.subTest(state=state),self.assertRaises(Refused):await b.record('start')
            self.assertEqual(b.channel._request.await_args.args[0],p.QUERY_URI)  # the task read, no command
            self.assertTrue(b.own_test)  # adopted, so it can be stopped here
        b.own_test=False;b.channel.read.return_value=devices(online=0);b.channel._request.reset_mock()
        with self.assertRaises(Refused):await b.record('start')
        b.channel._request.assert_not_awaited();self.assertEqual(b.status()['device_check']['state'],'offline')
        b.channel.read.return_value=devices(properties=[('recording_fid','777888')]);b.channel._request.return_value=reply(2)
        await b.record('start');self.assertTrue(b.own_test);self.assertEqual(b.channel._request.await_count,2)

    async def test_only_pushes_that_report_a_change_start_a_check(self):
        import time
        b=self.b;b.device={'model':'QwenNote A2','cloud_online_status':1}
        def push(kind,**fields):
            return {'type':960010,'stream_id':'lippi.device','payload':{'eventType':kind,'deviceId':456,**fields}}
        def waiting():return b.next_device_check>time.time()+200
        b.next_device_check=time.time()+300
        b.process_event(push('devicePropertyChanged',properties=[{'propertyKey':'device_battery','propertyValue':'41'}]))
        self.assertEqual(b.device['battery_percent'],41);self.assertTrue(waiting())
        b.process_event({**push('onlineStatusChanged'),'payload':{'eventType':'onlineStatusChanged','deviceId':999,'onlineStatus':0}})
        self.assertTrue(waiting())  # another device
        b.process_event(push('onlineStatusChanged',onlineStatus=0));self.assertFalse(waiting())
        b.next_device_check=time.time()+300;b.process_event(event());self.assertFalse(waiting())  # first task seen
        b.next_device_check=time.time()+300;b.process_event(event());self.assertTrue(waiting())  # same task repeated
        b.device_checked_at=time.time();b.process_event(push('onlineStatusChanged',onlineStatus=1))
        self.assertGreaterEqual(b.next_device_check,time.time()+9)  # not within 10 s of the last check
        b.channel._request.assert_not_awaited()

    async def test_device_check_closes_a_recording_that_ended(self):
        b=self.b;b.own_test=True;b.channel.read.return_value=devices()  # no recording_fid: idle
        result=await b.check_device()
        self.assertEqual(result['state'],'idle');self.assertFalse(b.own_test)
        self.assertFalse(json.loads(b.test_path.read_text())['open']);b.channel._request.assert_not_awaited()

    async def test_push_updates_cloud_view_without_unlocking_controls(self):
        b=self.b;b.process_event(event());view=b.status()['record_task']
        self.assertTrue(view['available']);self.assertEqual(view['source'],'push')
        self.assertIs(view['real_time_asr'],False);self.assertEqual(b.ready_until,0);self.assertFalse(b.own_test)
        self.assertNotIn('777888',json.dumps(b.status()));b.channel._request.assert_not_awaited()
        malformed=event();malformed['payload']['deviceTaskEventDTO']['recordTask']['deviceTask']['deviceId']=999
        b.process_event(malformed);self.assertEqual(b.record_task.fid,777888)

    async def test_older_task_or_update_does_not_replace_newer(self):
        b=self.b;b.process_event(event())
        for start,update in ((1000,1000),(500,9000)):
            t=copy.deepcopy(TASK);t['deviceTask'].update(startTime=start,updateTime=update,state=2)
            b.process_event(event(t));self.assertEqual(b.record_task.state,0)

    async def test_explicit_read_does_not_arm_mutate_or_change_ownership(self):
        b=self.b;b.process_event(event());b.own_test=True
        b.channel._request.return_value={'code':200,'body':{'recordTask':TASK}}
        result=await b.refresh_record_task()
        self.assertEqual(b.channel._request.await_count,1);self.assertEqual(b.channel._request.call_args.args[0],p.QUERY_URI)
        self.assertEqual(result['source'],'query');self.assertTrue(b.own_test);self.assertEqual(b.ready_until,0)
        self.assertTrue((b.state/'record-task-read.json').is_file())

    async def test_no_context_busy_timeout_and_mismatch_preserve_state(self):
        b=self.b
        with self.assertRaises(Refused):await b.refresh_record_task()
        b.process_event(event());before=b.record_task
        async with b.record_task_lock:
            self.assertFalse(b.health()['restart_safe'])
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await b.refresh_record_task()
        b.channel._request.side_effect=TimeoutError
        with self.assertRaises(Unavailable):await b.refresh_record_task()
        self.assertFalse(b.record_task_lock.locked());self.assertIs(b.record_task,before)
        b.channel._request.side_effect=None
        bad=copy.deepcopy(TASK);bad['deviceTask']['attributes']['fid']='444555'
        b.channel._request.return_value={'code':200,'body':{'recordTask':bad}}
        with self.assertRaises(Unavailable):await b.refresh_record_task()
        self.assertIs(b.record_task,before)

    async def test_late_query_cannot_overwrite_new_push(self):
        b=self.b;b.process_event(event())
        newer=copy.deepcopy(TASK);newer['deviceTask'].update(updateTime=3000,state=2)
        async def reply(*_):
            b.process_event(event(newer));return {'code':200,'body':{'recordTask':TASK}}
        b.channel._request.side_effect=reply
        result=await b.refresh_record_task()
        self.assertEqual(result['cloud_state'],2);self.assertEqual(result['source'],'push')

    async def test_route_rejects_caller_identifiers_and_requires_csrf(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock()
        client=TestClient(TestServer(make_app(b)));await client.start_server();b.port=client.server.port
        self.addAsyncCleanup(client.close)
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        self.assertEqual((await client.post('/api/record-task',json={})).status,403)
        self.assertEqual((await client.post('/api/record-task',json={'fid':123},headers=headers)).status,409)
        b.process_event(event());b.channel._request.return_value={'code':200,'body':{'recordTask':TASK}}
        reply=await client.post('/api/record-task',json={},headers=headers)
        self.assertEqual(reply.status,200);self.assertEqual((await reply.json())['source'],'query')


if __name__=='__main__':unittest.main()
