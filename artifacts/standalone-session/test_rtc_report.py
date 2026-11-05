"""Offline RTC signalling evidence: counts/timing only, no native or network IO."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import rtc_report

LOGIN=['ok',{'id':'private-self','room':{'id':'private-room','participants':[
    {'id':'private-self'},{'id':'private-peer','user':'private-user'}]}}]


def event(at,action,data,direction='in'):
    return {'time':at,'direction':direction,'frame':'42'+json.dumps(['participant',{'action':action,'data':data}])}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.run=self.root/'run';self.run.mkdir(mode=0o700)
        self.write('mcs-login-ack.json',LOGIN)
        self.write('conference-join-request.json',{'sent_at':100,'body':{'private':'secret'}})
        self.write('device-stop-request.json',{'sent_at':115})
        self.write('result.json',{'mcs':{'trace_truncated':False,'error_type':None}})
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,value):
        path=self.run/name;path.write_text(json.dumps(value));path.chmod(0o600)
    def frames(self,rows):
        path=self.run/'mcs-frames.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows));path.chmod(0o600)
    def test_peer_leave_detected_without_id_or_text_leak(self):
        self.frames([event(100.423,'leave','private-peer')])
        result=rtc_report.analyze(self.run)
        self.assertEqual(result['initial_remote_participants'],1)
        self.assertEqual(result['peer_departures_during_observation'],1)
        self.assertEqual(result['first_peer_leave_after_join_seconds'],0.423)
        self.assertFalse(result['no_peer_leave_observed'])
        self.assertFalse(result['physical_device_identity_verified'])
        for value in ('private-peer','private-self','private-user','private-room','secret'):self.assertNotIn(value,json.dumps(result))
    def test_cleanup_departure_is_not_counted_as_join_replacement(self):
        self.frames([event(116,'leave',{'id':'private-peer'})])
        r=rtc_report.analyze(self.run)
        self.assertEqual(r['peer_departures_during_observation'],0)
        self.assertTrue(r['no_peer_leave_observed']);self.assertTrue(r['observation_trace_complete'])
    def test_other_participant_and_outbound_frames_do_not_count(self):
        self.frames([event(101,'leave','not-in-roster'),event(102,'leave','private-self'),event(103,'leave','private-peer','out')])
        self.assertEqual(rtc_report.analyze(self.run)['peer_departures_during_observation'],0)
    def test_missing_peer_or_missing_window_never_claims_retention(self):
        login=copy.deepcopy(LOGIN);login[1]['room']['participants']=[];self.write('mcs-login-ack.json',login)
        self.frames([])
        self.assertIsNone(rtc_report.analyze(self.run)['no_peer_leave_observed'])
        self.write('mcs-login-ack.json',LOGIN);self.write('device-stop-request.json',{'sent_at':99})
        self.assertIsNone(rtc_report.analyze(self.run)['no_peer_leave_observed'])
    def test_truncated_or_bad_trace_is_inconclusive(self):
        self.frames([{'time':102,'direction':'in','frame':'42[broken'}])
        r=rtc_report.analyze(self.run);self.assertEqual(r['invalid_rows'],1);self.assertIsNone(r['no_peer_leave_observed'])
        self.frames([]);self.write('result.json',{'mcs':{'trace_truncated':True}})
        self.assertIsNone(rtc_report.analyze(self.run)['no_peer_leave_observed'])
    def test_duplicate_leave_count_is_distinct_peers(self):
        self.frames([event(101,'leave','private-peer'),event(102,'leave','private-peer')])
        self.assertEqual(rtc_report.analyze(self.run)['peer_departures_during_observation'],1)
    def test_report_is_read_only_and_refuses_unsafe_file(self):
        self.frames([]);before={p.name:p.read_bytes() for p in self.run.iterdir()}
        rtc_report.analyze(self.run)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.run.iterdir()})
        (self.run/'mcs-frames.jsonl').chmod(0o644)
        with self.assertRaises(ValueError):rtc_report.analyze(self.run)
    def test_record_task_flags_are_target_checked_and_timed(self):
        import base64
        self.frames([])
        identifier='a'*24;capture=self.run/'live-captures'/identifier
        capture.parent.mkdir(mode=0o700);capture.mkdir(mode=0o700)
        self.write('create-response.json',{'body':{'rtcTranslateTask':{'deviceTask':{'accountId':'123','accountType':2,'deviceId':456}}}})
        self.write('result.json',{'mcs':{'trace_truncated':False,'error_type':None},'capture':{'capture_id':identifier,'started_at':98}})
        task={'realTimeAsr':False,'deviceTask':{'accountId':'123','accountType':2,'deviceId':456,'taskType':0,
              'taskId':'private-task','state':0,'updateTime':1000,'attributes':{'fid':'777888'}}}
        rows=[]
        for elapsed,device in [(1,456),(3,456),(4,999),(18,456)]:
            task['deviceTask']['deviceId']=device
            payload={'deviceTaskEventDTO':{'taskType':0,'recordTask':task}}
            packet={'lwp':'/s/para','body':{'syncPushPackage':{'data':[{'objectType':770000,'streamId':'dtiot.dinger',
                    'data':base64.b64encode(json.dumps(payload).encode()).decode()}]}}}
            rows.append({'kind':'push','elapsed_seconds':elapsed,'packet':packet})
        path=capture/'pushes.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows));path.chmod(0o600)
        before={p:p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        report=rtc_report.analyze(self.run)['record_tasks']
        self.assertEqual(report['matched_events'],3);self.assertEqual(report['invalid_events'],1)
        self.assertEqual(report['real_time_asr_false'],3);self.assertEqual(report['distinct_recordings'],1)
        self.assertEqual(report['during_observation']['false'],1)
        self.assertFalse(report['physical_state_verified'])
        for private in ('777888','private-task','456','123'):self.assertNotIn(private,json.dumps(report))
        self.assertEqual(before,{p:p.read_bytes() for p in self.run.rglob('*') if p.is_file()})
        with self.subTest('path traversal rejected'):
            self.write('result.json',{'mcs':{},'capture':{'capture_id':'../outside'}})
            with self.assertRaises(ValueError):rtc_report.analyze(self.run)

    def test_transcript_receipt_identity_and_join_timing_without_private_leaks(self):
        import base64
        self.frames([])
        identifier='b'*24;capture=self.run/'live-captures'/identifier
        capture.parent.mkdir(mode=0o700);capture.mkdir(mode=0o700)
        self.write('create-response.json',{'body':{'rtcTranslateTask':{'rtcTranslateTaskId':789,
                  'deviceTask':{'accountId':'123','accountType':2,'deviceId':456},
                  'moziConfCreateResultDTO':{'startTime':100000}}}})
        self.write('result.json',{'mcs':{'trace_truncated':False,'error_type':None},'capture':{'capture_id':identifier,'started_at':98}})
        rows=[]
        for elapsed,task_id in [(1,999),(3,789),(5,999),(18,789)]:
            content=json.dumps({'stableResult':[{'result':'private transcript','index':1}],'dynamicResult':{},'startTime':1200})
            payload={'eventEnum':4,'textType':'ParagraphEnd','taskId':task_id,'recordId':'777888','sequence':elapsed,'content':content}
            packet={'lwp':'/s/para','body':{'syncPushPackage':{'data':[{'objectType':770001,'streamId':'dtiot.dinger',
                    'data':base64.b64encode(json.dumps(payload).encode()).decode()}]}}}
            rows.append({'kind':'push','elapsed_seconds':elapsed,'packet':packet})
        path=capture/'pushes.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows));path.chmod(0o600)
        report=rtc_report.analyze(self.run)['transcript_receipts']
        self.assertEqual(report['parsed_candidates'],4);self.assertEqual(report['receipt_task_matches'],2)
        self.assertEqual(report['other_task_candidates'],2);self.assertEqual(report['distinct_task_identities'],2)
        self.assertEqual(report['joined_receipt_task_matches'],1)
        self.assertEqual(report['first_receipt_match_after_join_seconds'],1.0)
        self.assertFalse(report['all_parsed_candidates_match_receipt'])
        for private in ('private transcript','777888','789','999','456','123'):self.assertNotIn(private,json.dumps(report))

    def test_nonfinite_timestamps_rejected(self):
        self.frames([event(float('nan'),'leave','private-peer')])
        r=rtc_report.analyze(self.run);self.assertEqual(r['invalid_rows'],1);self.assertIsNone(r['no_peer_leave_observed'])


if __name__=='__main__':unittest.main()
