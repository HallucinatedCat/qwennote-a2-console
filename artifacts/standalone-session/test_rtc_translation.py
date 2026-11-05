import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient,TestServer
from web_backend import Backend,make_app
import rtc_translation as p

REPLY={'code':200,'body':{'code':0,'configVersion':'fixture', 'supportedLanguages':[
    {'lang':'en','displayName':'English','nativeLangName':'English','supportVoices':[
        {'voiceId':'fixture-voice-en','name':'English voice','audioMediaId':'private fixture media','avatarMediaId':'private fixture avatar'}]},
    {'lang':'zh','displayName':'Chinese','nativeLangName':'中文','supportVoices':[{'voiceId':'fixture-voice-zh','name':'Chinese voice'}]}]}}


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.options=p.parse_config(copy.deepcopy(REPLY));self.receipt=SimpleNamespace(conference_id='fixture-conference');self.secret='fixture-secret'
    def selection(self,lang='en',scene='a1_transcribe'):
        vid=next(iter(self.options[lang]['voices']))
        return {'language':lang,'voice_handle':p.voice_handle(self.secret,scene,lang,vid)}
    def test_read_request_has_no_translation_subscription_or_targets(self):
        request=p.config_request()[0]
        self.assertIsNone(request['conferenceId']);self.assertEqual(request['scene'],'a1_transcribe')
        self.assertEqual(set(request),{'requestId','conferenceId','scene'})
        self.assertNotEqual(request['requestId'],p.config_request()[0]['requestId'])
        with self.assertRaises(ValueError):p.config_request(scene_type='unknown')
    def test_only_successful_complete_config_is_accepted(self):
        for reply in [None,[],{}, {'code':403,'body':REPLY['body']}, {'code':200,'body':{'code':True}}, {'code':200,'body':{'code':401,'supportedLanguages':[]}}, {'code':200,'body':{'code':0,'supportedLanguages':'bad'}}]:
            with self.subTest(reply=reply),self.assertRaises(ValueError):p.parse_config(reply)
        reply=copy.deepcopy(REPLY);reply['body']['code']=200;self.assertEqual(len(p.parse_config(reply)),2)
    def test_duplicate_ids_and_unbounded_fields_rejected(self):
        for change in ['duplicate-language','duplicate-voice','bad-language','bad-name','too-many','bad-voices']:
            reply=copy.deepcopy(REPLY);rows=reply['body']['supportedLanguages']
            if change=='duplicate-language':rows.append(copy.deepcopy(rows[0]))
            elif change=='duplicate-voice':rows[0]['supportVoices']*=2
            elif change=='bad-language':rows[0]['lang']='en/path'
            elif change=='bad-name':rows[0]['displayName']='x'*257
            elif change=='too-many':reply['body']['supportedLanguages']=[{}]*257
            else:rows[0]['supportVoices']='bad'
            with self.subTest(change=change),self.assertRaises(ValueError):p.parse_config(reply)
    def test_public_options_omit_raw_voice_and_media_identifiers(self):
        output=json.dumps(p.descriptor(self.options,self.secret))
        for private in ['fixture-voice-en','private fixture media','private fixture avatar','fixture-secret','voiceId','audioMediaId']:
            self.assertNotIn(private,output)
        self.assertFalse(p.descriptor(self.options,self.secret)['playback_available'])
    def test_validated_subscription_and_empty_unsubscribe_shapes(self):
        row=p.subscription_request(self.receipt,self.options,self.secret,[self.selection()])[0]
        self.assertEqual(row['targetLanguages'],[{'lang':'en','voiceId':'fixture-voice-en'}])
        self.assertEqual(row['sourceLanguage'],'auto');self.assertEqual(row['scene'],'a1_transcribe')
        self.assertEqual(set(row),{'requestId','conferenceId','targetLanguages','scene','sourceLanguage'})
        off=p.unsubscribe_request(self.receipt)[0];self.assertEqual(off['targetLanguages'],[])
        self.assertNotIn('text',row);self.assertNotIn('url',row)
    def test_stale_cross_scene_or_wrong_language_voice_handle_rejected(self):
        invalid=[self.selection(scene='a1_face2face'),{**self.selection(),'language':'zh'},{**self.selection(),'voice_handle':'fixture-voice-en'},{**self.selection(),'text':'unsolicited speech'}]
        for selection in invalid:
            with self.subTest(selection=selection),self.assertRaises(ValueError):p.subscription_request(self.receipt,self.options,self.secret,[selection])
        with self.assertRaises(ValueError):p.subscription_request(self.receipt,self.options,'changed-secret',[self.selection()])
    def test_bidirectional_selection_uses_multilingual_and_distinct_targets(self):
        scene='a1_face2face';choices=[self.selection('en',scene),self.selection('zh',scene)]
        row=p.subscription_request(self.receipt,self.options,self.secret,choices,scene_type=scene)[0]
        self.assertEqual(row['sourceLanguage'],'multilingual');self.assertEqual(len(row['targetLanguages']),2)
        for bad in [choices[:1],[choices[0],choices[0]],[]]:
            with self.assertRaises(ValueError):p.subscription_request(self.receipt,self.options,self.secret,bad,scene_type=scene)
    def test_unsupported_source_and_target_fail_before_request_build(self):
        with self.assertRaises(ValueError):p.subscription_request(self.receipt,self.options,self.secret,[self.selection()],source_language='unknown')
        with self.assertRaises(ValueError):p.subscription_request(self.receipt,self.options,self.secret,[{'language':'unknown','voice_handle':'x'}])


class OptionsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.backend=Backend(Path(self.tmp.name),1)
        self.backend.start=AsyncMock();self.backend.stop=AsyncMock()
        self.backend.channel=SimpleNamespace(ready=True,dropped_pushes=0,_request=AsyncMock(return_value=copy.deepcopy(REPLY)))
        self.client=TestClient(TestServer(make_app(self.backend)));await self.client.start_server()
        self.backend.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+self.backend.cookie,'X-CSRF-Token':self.backend.csrf}
    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()
    async def test_read_only_endpoint_requires_csrf_without_arming_device(self):
        denied=await self.client.post('/api/rtc-options',json={});self.assertEqual(denied.status,403)
        response=await self.client.post('/api/rtc-options',json={},headers=self.headers)
        self.assertEqual(response.status,200);body=await response.json();self.assertEqual(len(body['languages']),2)
        self.assertEqual(self.backend.ready_until,0);self.assertFalse(self.backend.rtc.active)
        self.assertEqual(self.backend.channel._request.await_args.args[0],p.CONFIG_URI)
        self.assertEqual((Path(self.tmp.name)/'interpreter-options-a1_transcribe.json').stat().st_mode&0o777,0o600)
    async def test_concurrent_reads_share_one_bounded_cache_request(self):
        replies=await asyncio.gather(*(self.backend.interpreter_options() for _ in range(3)))
        self.assertEqual(len(replies),3);self.backend.channel._request.assert_awaited_once()
    async def test_bad_scene_or_rejected_reply_is_not_cached(self):
        with self.assertRaises(ValueError):await self.backend.interpreter_options('unknown')
        self.backend.channel._request.assert_not_awaited()
        self.backend.channel._request.return_value={'code':403}
        with self.assertRaises(ValueError):await self.backend.interpreter_options()
        self.assertEqual(self.backend.interpreter_cache,{})


if __name__=='__main__':unittest.main()
