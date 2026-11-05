"""Source-contract regressions; synthetic messages are not hardware evidence."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from capture_report import analyze
from device_protocol import decode_push,TRANSCRIPT_STREAMS
from live_transcript import TranscriptStore
from test_live_transcript import event
from web_backend import Backend


def packet(kind=770001,stream='dtiot.dinger',sequence=1,final=False):
    payload=event(sequence=sequence,final=final)
    row={'objectType':kind,'streamId':stream,'data':base64.b64encode(json.dumps(payload).encode()).decode()}
    return {'lwp':'/s/para','body':{'syncPushPackage':{'data':[row]}}}


class TranscriptRoutingTests(unittest.TestCase):
    def test_both_source_contracts_on_both_native_envelopes(self):
        for uri in ('/s/para','/s/synca'):
            for kind,stream in ((770001,'dtiot.dinger'),(86000,'doc.transcribe')):
                with self.subTest(uri=uri,kind=kind):
                    message=packet(kind,stream);message['lwp']=uri
                    message['body']=json.dumps(message['body'])
                    decoded=decode_push(message)
                    self.assertEqual(len(decoded),1)
                    self.assertEqual(decoded[0]['stream_id'],stream)
                    self.assertEqual(decoded[0]['payload'],event())

    def test_document_notifications_and_crossed_pairs_are_not_transcripts(self):
        pairs=((86002,'doc.transcribe'),
               (86000,'dtiot.dinger'),(770001,'doc.transcribe'),(770000,'doc.transcribe'),
               (86000,'unknown'),(86000,None),(86000,''))
        for kind,stream in pairs:
            with self.subTest(kind=kind,stream=stream):self.assertEqual(decode_push(packet(kind,stream)),[])
        # 86001/86003 are note status notices: decoded, but never transcript rows.
        for kind in (86001,86003):
            self.assertNotIn(decode_push(packet(kind,'doc.transcribe'))[0]['type'],TRANSCRIPT_STREAMS)

    def test_legacy_missing_dinger_stream_is_canonicalized(self):
        for stream in (None,''):
            decoded=decode_push(packet(stream=stream))
            self.assertEqual(decoded[0]['stream_id'],'dtiot.dinger')
            self.assertEqual(decoded[0]['type'],770001)

    def test_equal_task_ids_on_different_sources_do_not_merge(self):
        store=TranscriptStore();payload=event(final=True)
        device=store.accept(payload);conference=store.accept(payload,source='doc.transcribe')
        self.assertNotEqual(device['stream'],conference['stream'])
        self.assertIsNone(store.accept(payload,source='doc.transcribe'))
        self.assertIsNone(store.accept(event(sequence=2),source='doc.transcribe'))
        self.assertEqual(len(store.snapshot()['rows']),2)
        self.assertNotIn('task_id',json.dumps(store.snapshot()))
        with self.assertRaises(ValueError):store.accept(payload,source='untrusted identity')

    def test_backend_and_diagnostics_accept_both_without_cloud_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=Backend(Path(tmp),18780);backend.channel=Mock()
            backend.diagnostics.start()
            try:
                backend.process_push(packet(final=True))
                backend.process_push(packet(86000,'doc.transcribe',final=True))
                backend.process_push(packet(86003,'doc.transcribe'))
            finally:counts=backend.diagnostics.stop()
            self.assertEqual(len(backend.transcripts.snapshot()['rows']),2)
            self.assertEqual(counts['transcript_candidates'],2)
            self.assertEqual(counts['transcript_updates'],2)
            self.assertEqual(counts['dinger_transcript_candidates'],1)
            self.assertEqual(counts['conference_transcript_candidates'],1)
            self.assertEqual(counts['unrecognized_frames'],0)  # 86003 is a note status notice
            self.assertEqual(backend.ready_until,0);backend.channel._request.assert_not_called()

    def test_replay_keeps_each_sources_sequence_and_content_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=Backend(Path(tmp),18780);backend.diagnostics.start()
            for message in (packet(sequence=10,final=True),packet(86000,'doc.transcribe',sequence=2,final=True)):
                backend.process_push(message)
            backend.diagnostics.stop();report=analyze(backend.diagnostics.directory)
            self.assertEqual(report['sequence_decreases'],0)
            self.assertEqual(report['replayed_updates'],2)
            self.assertEqual(report['final_paragraphs_in_bounded_replay'],2)
            self.assertEqual(report['conference_transcript_candidates'],1)
            self.assertEqual(report['dinger_transcript_candidates'],1)
            self.assertNotIn('Hello',json.dumps(report))
            self.assertFalse(report['hardware_behavior_verified'])

    def test_correct_route_with_non_transcript_event_produces_no_text(self):
        message=packet(86000,'doc.transcribe');row=message['body']['syncPushPackage']['data'][0]
        payload=event();payload['eventEnum']=1
        row['data']=base64.b64encode(json.dumps(payload).encode()).decode()
        with tempfile.TemporaryDirectory() as tmp:
            backend=Backend(Path(tmp),18780);backend.process_push(message)
            self.assertEqual(backend.transcripts.snapshot()['rows'],[])


if __name__=='__main__':unittest.main()
