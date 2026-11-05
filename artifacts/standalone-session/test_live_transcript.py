import json
import unittest
from live_transcript import TranscriptStore,parse_transcript,map_start_time,LONG_MAX


def event(sequence=1,index=0,stable='Hello ',dynamic='world',final=False,task=1001,language='en',translated=False):
    body={'stableResult':[{'index':index,'result':stable,'targetLang':language}],
          'dynamicResult':{'index':index,'result':dynamic,'targetLang':language},
          'startTime':500,'isTranslateResult':translated}
    return {'taskId':task,'recordId':2002,'sequence':sequence,'eventEnum':4,
            'textType':'ParagraphEnd' if final else 'SentenceChange','content':json.dumps(body)}


class LiveTranscriptTests(unittest.TestCase):
    def test_stable_dynamic_and_final_marker(self):
        row=parse_transcript(event())
        self.assertEqual(row['text'],'Hello world')
        self.assertEqual(row['stable_text'],'Hello ')
        self.assertEqual(row['provisional_text'],'world')
        self.assertFalse(row['final']);self.assertEqual(row['language'],'en')
        self.assertTrue(parse_transcript(event(final=True))['final'])
        self.assertEqual(row['timeline'],'source-relative')

    def test_filter_event_and_task(self):
        original=event()
        for changed in ({'eventEnum':1},{'taskId':None},{'taskId':True},{'taskId':-2},{'content':'malformed'}):
            with self.subTest(changed=changed):self.assertIsNone(parse_transcript({**original,**changed}))
        self.assertIsNone(parse_transcript(original,expected_task=9999))
        self.assertIsNotNone(parse_transcript(original,expected_task=1001))

    def test_language_and_index_dynamic_fallback(self):
        payload=event();payload['content']=json.dumps({'dynamicResult':{'result':'bonjour','index':7,'targetLang':'fr'},'index':42})
        payload['sequence']=0
        row=parse_transcript(payload)
        self.assertEqual((row['language'],row['paragraph_index'],row['sequence']),('fr',7,42))

    def test_source_timeline_mapping(self):
        self.assertEqual(map_start_time(500,10000,11000,11500),500)
        self.assertEqual(map_start_time(500,10000,11000),0)
        self.assertEqual(map_start_time(500,12000,11000),1500)
        self.assertEqual(map_start_time(LONG_MAX,12000,11000),LONG_MAX)
        payload=event();body=json.loads(payload['content']);body['timestamp']=11500;payload['content']=json.dumps(body)
        row=parse_transcript(payload,conference_start=10000,record_start=11000)
        self.assertEqual((row['start_ms'],row['timeline']),(500,'recording'))

    def test_duplicate_and_out_of_order_updates_do_not_regress(self):
        store=TranscriptStore()
        self.assertIsNotNone(store.accept(event(sequence=3,dynamic='there')))
        self.assertIsNone(store.accept(event(sequence=3,dynamic='there')))
        self.assertIsNone(store.accept(event(sequence=2,dynamic='old')))
        self.assertEqual(store.snapshot()['rows'][0]['text'],'Hello there')

    def test_final_paragraph_resists_provisional_update(self):
        store=TranscriptStore()
        store.accept(event(sequence=3,stable='Final text',dynamic='',final=True))
        self.assertIsNone(store.accept(event(sequence=4,stable='Changed',dynamic=' draft')))
        self.assertIsNotNone(store.accept(event(sequence=5,stable='Corrected final',dynamic='',final=True)))
        self.assertEqual(len(store.snapshot()['rows']),1)
        self.assertEqual(store.snapshot()['rows'][0]['text'],'Corrected final')

    def test_translation_language_and_task_are_separate(self):
        store=TranscriptStore()
        for item in (event(),event(language='fr',translated=True),event(task=1002)):
            store.accept(item)
        snapshot=store.snapshot();self.assertEqual(len(snapshot['rows']),3)
        self.assertEqual(len({row['stream'] for row in snapshot['rows']}),2)
        rendered=json.dumps(snapshot)
        self.assertNotIn('task_id',rendered);self.assertNotIn('record_id',rendered)
        self.assertTrue(all(row['stream'] not in ('1001','1002','2002') for row in snapshot['rows']))

    def test_unknown_paragraph_index_does_not_merge_distinct_updates(self):
        store=TranscriptStore()
        store.accept(event(sequence=1,index=-1))
        store.accept(event(sequence=2,index=-1))
        self.assertEqual(len(store.snapshot()['rows']),2)

    def test_bounded_storage_and_reconnect_gap(self):
        store=TranscriptStore(max_rows=2)
        for index in range(3):store.accept(event(index=index,sequence=index+1))
        snapshot=store.snapshot();self.assertEqual(len(snapshot['rows']),2);self.assertEqual(snapshot['evicted'],1)
        store.mark_reconnect();self.assertTrue(store.snapshot()['possible_reconnect_gaps'])
        tiny=TranscriptStore(max_bytes=2);self.assertIsNone(tiny.accept(event()))
        self.assertEqual(tiny.snapshot()['rows'],[])

    def test_malformed_nested_data_and_resource_bounds(self):
        payload=event()
        for body in ([],None,{'stableResult':'bad'},{'stableResult':[{'result':'x'*32769}]}, {'dynamicResult':{'result':'   '}}):
            with self.subTest(body_type=type(body).__name__):
                self.assertIsNone(parse_transcript({**payload,'content':json.dumps(body)}))
        self.assertIsNone(parse_transcript({**payload,'content':'x'*(256*1024+1)}))

if __name__=='__main__':unittest.main()
