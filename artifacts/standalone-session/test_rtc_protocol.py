import copy
import json
import unittest
import rtc_protocol as p

CONFIG={'account_id':'123','account_type':0,'target_device_id':456}
REPLY={'code':200,'body':{'rtcTranslateTask':{'rtcTranslateTaskId':789,'keepAlivePeriodSecond':10,
       'deviceTask':{'accountId':'123','accountType':0,'deviceId':456},
       'moziConfCreateResultDTO':{'conferenceId':'synthetic-conference','startTime':100,
                                'token':'private-synthetic-token','config':'private-synthetic-config',
                                'oneRTCAccessInfo':{'appId':'private-synthetic-app'}}}}}

class RTCProtocolTests(unittest.TestCase):
    def test_transcription_shape_excludes_translation_and_arbitrary_settings(self):
        body=p.create_transcription(CONFIG,'request')[0]
        self.assertEqual(set(body),{'accountType','accountId','deviceId','requestId','attributes'})
        self.assertEqual(body['attributes'],{'Format':'opus','SampleRate':'16000','SpeakerCount':'1','Protocol':'5','scene':'Transcription','sourceLanguage':'auto','targetLanguages':'[]','LanguageHints':'[]'})
        self.assertNotIn('rtcTranslateTts',body);self.assertNotIn('fid',body)

    def test_receipt_rejects_identity_mismatch_missing_ids_and_invalid_interval(self):
        self.assertEqual(p.parse_receipt(CONFIG,REPLY).keepalive_seconds,10)
        variants=[]
        for field,value in [('rtcTranslateTaskId',0),('keepAlivePeriodSecond',True),('keepAlivePeriodSecond',3601)]:
            reply=copy.deepcopy(REPLY);reply['body']['rtcTranslateTask'][field]=value;variants.append(reply)
        reply=copy.deepcopy(REPLY);reply['body']['rtcTranslateTask']['deviceTask']['deviceId']=999;variants.append(reply)
        reply=copy.deepcopy(REPLY);reply['body']['rtcTranslateTask']['moziConfCreateResultDTO']['conferenceId']='';variants.append(reply)
        variants.extend([{'code':403},{'code':200,'body':{}},{'code':200,'body':None}])
        for reply in variants:
            with self.subTest(reply_shape=list(reply)),self.assertRaises(ValueError):p.parse_receipt(CONFIG,reply)

    def test_receipt_summary_and_repr_do_not_leak_material(self):
        receipt=p.parse_receipt(CONFIG,REPLY)
        summary=json.dumps(receipt.summary())+repr(receipt)
        for value in ('synthetic-conference','private-synthetic-token','private-synthetic-config','private-synthetic-app'):
            self.assertNotIn(value,summary)
        self.assertTrue(receipt.summary()['conference_token_present'])

    def test_keepalive_uses_owned_receipt_and_no_tts(self):
        receipt=p.parse_receipt(CONFIG,REPLY);body=p.keepalive(CONFIG,receipt,'beat')[0]
        self.assertEqual(body['rtcTranslateTaskId'],789);self.assertEqual(body['attributes'],{'keepAlive':'true'})
        self.assertIsNone(body['targetLanguage']);self.assertIsNone(body['rtcTranslateTts'])
        self.assertEqual(body['deviceId'],456)

    def test_close_and_readback_use_only_receipt_conference(self):
        receipt=p.parse_receipt(CONFIG,REPLY)
        self.assertEqual(p.close_conference(receipt),[{'conferenceId':'synthetic-conference','reason':'user_click_close_flash_a1_conf'}])
        self.assertEqual(p.conference_info(receipt),[{'conferenceId':'synthetic-conference'}])
        for identifier,end,expected in [('synthetic-conference',200,True),('other',200,False),('synthetic-conference',0,False),('synthetic-conference',True,False)]:
            response={'code':200,'body':{'confInfo':{'conferenceId':identifier,'endTime':end}}}
            self.assertEqual(p.closed_info(receipt,response),expected)
        self.assertFalse(p.closed_info(receipt,{'code':404}))
        self.assertTrue(p.closed_info(receipt,{'code':400,'body':{'code':'400','reason':'Meeting ended'}}))
        self.assertFalse(p.closed_info(receipt,{'code':400,'body':{'code':'400','reason':'Invalid parameters'}}))
        self.assertTrue(p.closed_info(receipt,{'code':400,'body':{'code':'400','reason':'会议已结束'}}))
        self.assertFalse(p.closed_info(receipt,{'code':403,'body':{'code':'400','reason':'Meeting ended'}}))

    def test_a2_join_keeps_device_participant_and_receiver_quiet(self):
        receipt=p.parse_receipt(CONFIG,REPLY)
        body=p.join_conference(receipt,'receiver')[0]
        self.assertTrue(body['multiDeviceJoin'])
        self.assertEqual(body['attributes']['enable_multi_device_join'],'1')
        self.assertEqual(body['participantId'],'receiver')
        self.assertTrue(body['pushToAssistant'])
        self.assertEqual(body['cameraStatus'],2)
        self.assertNotIn('micStreamId',body)

    def test_conference_heartbeat_uses_participant_and_versions(self):
        receipt=p.parse_receipt(CONFIG,REPLY)
        body=p.conference_heartbeat(receipt,'participant',5,10)[0]
        self.assertEqual(body['participantId'],'participant');self.assertEqual(body['statusSeqNum'],5)
        self.assertFalse(body['forceUpdate']);self.assertEqual(body['lastUpdateTime'],10)
        with self.assertRaises(ValueError):p.conference_heartbeat(receipt,'participant',True,0)

    def test_booleans_and_bad_numeric_identities_rejected(self):
        for value in (True,False,0,-1,2**63,1.0,'not-an-id'):
            with self.assertRaises(ValueError):p.create_transcription({**CONFIG,'target_device_id':value})
        with self.assertRaises(ValueError):p.create_transcription({**CONFIG,'account_type':True})

if __name__=='__main__':unittest.main()
