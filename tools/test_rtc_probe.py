import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rtc_probe import run_probe,SOURCE,check_host_preflight
import sys
sys.path.insert(0,str(SOURCE))
from test_rtc_protocol import CONFIG,REPLY
import rtc_protocol as p
from device_protocol import RECORD_URI

class Channel:
    ready=True
    dropped_pushes=0
    def __init__(self,mode):self.mode=mode;self.pushes=asyncio.Queue();self.calls=[]
    async def _request(self,uri,body,headers):
        self.calls.append(uri)
        if uri==RECORD_URI and body[0]['action']=='start' and self.mode=='start-timeout':raise TimeoutError()
        if uri==RECORD_URI and body[0]['action']=='start' and self.mode=='start-busy':return {'code':500,'body':{'code':'500','reason':'synthetic busy'}}
        if uri==p.CREATE_URI:
            if self.mode=='timeout':raise TimeoutError()
            reply=copy.deepcopy(REPLY)
            if self.mode=='wrong-device':reply['body']['rtcTranslateTask']['deviceTask']['deviceId']=999
            return reply
        if uri==p.JOIN_URI:return {'code':200,'body':{'code':200,'heartBeatTimeMillis':10000,'statusSeqNum':1,'confInfoModel':{'lastModifyTime':2}}}
        if uri==p.HEARTBEAT_URI:return {'code':200,'body':{'code':403 if self.mode=='heartbeat-rejected' else 200}}
        if uri==p.CLOSE_URI:
            if self.mode=='close-timeout':raise TimeoutError()
            return {'code':200,'body':{'code':200}}
        if uri==p.INFO_URI:
            return {'code':200,'body':{'confInfo':{'conferenceId':'synthetic-conference','endTime':0 if self.mode=='not-closed' else 200}}}
        return {'code':200,'body':{'code':0}}

class FakeMCS:
    ready=True
    participant='synthetic-participant'
    def __init__(self,directory):self.closed=False
    async def open(self,*args):return ['ok',{}]
    async def close(self):self.closed=True
    def summary(self):return {'closed':self.closed}

class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name);self.directory=self.parent/'probe';self.directory.mkdir(mode=0o700)
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_record_start_precedes_rtc_and_is_stopped(self):
        channel=Channel('ok');result=await run_probe(channel,CONFIG,self.directory,0,start_recording=True)
        self.assertEqual(channel.calls,[RECORD_URI,p.CREATE_URI,RECORD_URI,p.CLOSE_URI,p.INFO_URI])
        self.assertTrue(result['recording_start_acknowledged']);self.assertFalse(result['cleanup_pending'])
    async def test_ambiguous_record_start_withholds_stop_and_preserves_pending(self):
        channel=Channel('start-timeout');result=await run_probe(channel,CONFIG,self.directory,0,start_recording=True)
        self.assertEqual(channel.calls,[RECORD_URI]);self.assertEqual(result['error_type'],'TimeoutError')
        self.assertTrue(result['cleanup_pending']);self.assertFalse(result['device_stop_acknowledged'])
        self.assertEqual(result['stop_withheld_reason'],'recording-ownership-unconfirmed')
        self.assertTrue(json.loads((self.parent/'rtc-probe-pending.json').read_text())['pending'])

    async def test_busy_start_never_stops_an_existing_recording(self):
        channel=Channel('start-busy');result=await run_probe(channel,CONFIG,self.directory,0,start_recording=True)
        self.assertEqual(channel.calls,[RECORD_URI]);self.assertFalse(result['recording_start_acknowledged'])
        self.assertTrue(result['cleanup_pending']);self.assertFalse(result['device_stop_acknowledged'])
        self.assertNotIn(p.CREATE_URI,channel.calls)

    async def test_owned_recording_is_stopped_after_ambiguous_create(self):
        channel=Channel('timeout');result=await run_probe(channel,CONFIG,self.directory,0,start_recording=True)
        self.assertEqual(channel.calls,[RECORD_URI,p.CREATE_URI,RECORD_URI])
        self.assertTrue(result['device_stop_acknowledged']);self.assertTrue(result['cleanup_pending'])
    async def test_success_requires_stop_and_end_readback(self):
        channel=Channel('ok');result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertFalse(result['cleanup_pending']);self.assertTrue(result['conference_end_readback'])
        self.assertEqual(channel.calls,[p.CREATE_URI,RECORD_URI,p.CLOSE_URI,p.INFO_URI])
        self.assertFalse(json.loads((self.parent/'rtc-probe-pending.json').read_text())['pending'])
    async def test_ambiguous_create_withholds_stop_and_preserves_pending(self):
        channel=Channel('timeout');result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertEqual(channel.calls,[p.CREATE_URI]);self.assertTrue(result['cleanup_pending'])
        self.assertEqual(result['error_type'],'TimeoutError')
    async def test_wrong_target_receipt_never_closes_foreign_conference(self):
        channel=Channel('wrong-device');result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertEqual(channel.calls,[p.CREATE_URI]);self.assertTrue(result['cleanup_pending'])
    async def test_close_ack_without_end_is_not_proof_of_cleanup(self):
        channel=Channel('not-closed');result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertTrue(result['conference_close_acknowledged']);self.assertTrue(result['cleanup_pending'])
        self.assertTrue(json.loads((self.parent/'rtc-probe-pending.json').read_text())['pending'])
    async def test_receipt_write_failure_preserves_cleanup_identity(self):
        from lwp_client import private_write
        def failing(path,value):
            if path.name=='create-response.json':raise OSError('synthetic IO failure')
            private_write(path,value)
        channel=Channel('ok')
        with patch('rtc_session.private_write',failing):result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertEqual(result['error_type'],'OSError');self.assertFalse(result['cleanup_pending'])
        self.assertEqual(channel.calls,[p.CREATE_URI,RECORD_URI,p.CLOSE_URI,p.INFO_URI])
    async def test_join_heartbeat_and_cleanup(self):
        channel=Channel('ok')
        with patch('rtc_session.MCS',FakeMCS):result=await run_probe(channel,CONFIG,self.directory,0.01,True)
        self.assertFalse(result['cleanup_pending']);self.assertTrue(result['mcs']['closed'])
        self.assertIn(p.HEARTBEAT_URI,channel.calls)
    async def test_heartbeat_rejection_cleans_up(self):
        channel=Channel('heartbeat-rejected')
        with patch('rtc_session.MCS',FakeMCS):result=await run_probe(channel,CONFIG,self.directory,0.01,True)
        self.assertEqual(result['error_type'],'ValueError');self.assertFalse(result['cleanup_pending'])
        self.assertEqual(channel.calls[-3:],[RECORD_URI,p.CLOSE_URI,p.INFO_URI])
    async def test_cancellation_during_cleanup_does_not_interrupt_or_repeat_stop(self):
        channel=Channel('ok');original=channel._request;stopping=asyncio.Event();release=asyncio.Event()
        async def held(uri,body,headers):
            if uri==RECORD_URI:
                stopping.set();await release.wait()
            return await original(uri,body,headers)
        channel._request=held
        task=asyncio.create_task(run_probe(channel,CONFIG,self.directory,0))
        await stopping.wait();task.cancel();await asyncio.sleep(0);task.cancel();release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(channel.calls,[p.CREATE_URI,RECORD_URI,p.CLOSE_URI,p.INFO_URI])
        self.assertFalse(json.loads((self.parent/'rtc-probe-pending.json').read_text())['pending'])
    async def test_both_transcript_sources_reach_probe_and_capture(self):
        from test_transcript_routing import packet
        channel=Channel('ok')
        channel.pushes.put_nowait(packet(final=True))
        channel.pushes.put_nowait(packet(86000,'doc.transcribe',final=True))
        result=await run_probe(channel,CONFIG,self.directory,0)
        self.assertEqual(result['transcript_rows'],2)
        self.assertEqual(result['capture']['dinger_transcript_candidates'],1)
        self.assertEqual(result['capture']['conference_transcript_candidates'],1)
        self.assertFalse(result['cleanup_pending'])
    async def test_task_inspection_requires_owned_recording_and_join(self):
        channel=Channel('ok')
        for start,join in [(False,True),(True,False)]:
            with self.assertRaises(ValueError):
                await run_probe(channel,CONFIG,self.directory,1,join_signaling=join,start_recording=start,inspect_record_task=True)
        self.assertEqual(channel.calls,[])

    async def run_task_inspection(self,mode):
        import base64,time
        import record_tasks as tasks
        channel=Channel('ok');original=channel._request
        task={'realTimeAsr':False,'deviceTask':{'accountId':CONFIG['account_id'],'accountType':CONFIG['account_type'],
              'deviceId':CONFIG['target_device_id'],'taskType':0,'taskId':'private-task','state':0,'stateCode':0,
              'startTime':int((time.time()-(3600 if mode=='stale' else 0))*1000),'updateTime':int(time.time()*1000),'attributes':{'fid':'777888'}}}
        payload={'deviceTaskEventDTO':{'taskType':0,'recordTask':task}}
        channel.pushes.put_nowait({'lwp':'/s/para','body':{'syncPushPackage':{'data':[{'objectType':770000,'streamId':'dtiot.dinger',
                                  'data':base64.b64encode(json.dumps(payload).encode()).decode()}]}}})
        async def request(uri,body,headers):
            if uri!=tasks.QUERY_URI:return await original(uri,body,headers)
            channel.calls.append(uri)
            if mode=='hang':await asyncio.Event().wait()
            reply=copy.deepcopy(task)
            if mode=='wrong-fid':reply['deviceTask']['attributes']['fid']='222333'
            return {'code':200,'body':{'recordTask':reply}}
        channel._request=request
        with patch('rtc_session.MCS',FakeMCS):
            result=await run_probe(channel,CONFIG,self.directory,0.5,True,start_recording=True,inspect_record_task=True)
        self.assertFalse(result['cleanup_pending']);self.assertEqual(channel.calls[-3:],[RECORD_URI,p.CLOSE_URI,p.INFO_URI])
        for value in ('private-task','777888','222333'):self.assertNotIn(value,json.dumps(result))
        return channel,result,tasks.QUERY_URI

    async def test_one_task_read_inside_joined_window(self):
        channel,result,uri=await self.run_task_inspection('ok')
        self.assertEqual(channel.calls.count(uri),1)
        self.assertLess(channel.calls.index(p.JOIN_URI),channel.calls.index(uri))
        self.assertTrue(result['record_task_read']['matched']);self.assertIs(result['record_task_read']['real_time_asr'],False)

    async def test_task_read_failure_never_retries_or_blocks_cleanup(self):
        channel,result,uri=await self.run_task_inspection('hang')
        self.assertEqual(channel.calls.count(uri),1);self.assertFalse(result['record_task_read']['matched'])
        self.assertIn(result['record_task_read']['error_type'],('TimeoutError','CancelledError'))

    async def test_foreign_task_reply_is_rejected(self):
        channel,result,uri=await self.run_task_inspection('wrong-fid')
        self.assertEqual(channel.calls.count(uri),1);self.assertFalse(result['record_task_read']['matched'])

    async def test_stale_task_is_not_queried(self):
        channel,result,uri=await self.run_task_inspection('stale')
        self.assertNotIn(uri,channel.calls);self.assertFalse(result['record_task_read']['attempted'])
        self.assertEqual(result['record_task_read']['reason'],'no-matched-active-task')

    async def test_cancellation_still_stops_and_closes(self):
        channel=Channel('ok');task=asyncio.create_task(run_probe(channel,CONFIG,self.directory,10))
        while p.CREATE_URI not in channel.calls:await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(channel.calls,[p.CREATE_URI,RECORD_URI,p.CLOSE_URI,p.INFO_URI])
        journal=json.loads((self.parent/'rtc-probe-pending.json').read_text())
        self.assertFalse(journal['pending']);self.assertEqual(journal['result']['error_type'],'CancelledError')

class PreflightTests(unittest.TestCase):
    def test_external_active_paused_or_uncertain_task_blocks_probe(self):
        for state,end in [(0,None),(1,None),(None,None),(2,None),(2,0),(True,100)]:
            with self.subTest(state=state,end=end),self.assertRaises(ValueError):
                check_host_preflight({'connection':'connected','record_task':{
                    'available':True,'cloud_state':state,'cloud_ended_at_ms':end}},100)
        check_host_preflight({'connection':'connected','record_task':{
            'available':True,'cloud_state':2,'cloud_ended_at_ms':100}},100)
        check_host_preflight({'connection':'connected','record_task':{'available':False}},100)

    def test_active_host_work_blocks_without_native_requests(self):
        for field,value in [('mutation_in_progress',True),('pending_restores',['test']),
                            ('document_read_in_progress',True),('assistant_in_progress',True),
                            ('live_diagnostics',{'active':True}),('record_task',{'read_in_progress':True})]:
            with self.subTest(field=field),self.assertRaises(ValueError):
                check_host_preflight({'connection':'connected',field:value},100)

if __name__=='__main__':unittest.main()
