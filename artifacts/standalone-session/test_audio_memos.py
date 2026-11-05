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
import audio_memos as p

REPLY={'code':200,'body':{'hasMore':False,'cursor':'private-cursor','missionList':[
    {'missionId':'private-id','type':0,'content':'{"text":"Memo fixture","other":"private-value"}',
     'input':'fallback','audioUrl':'https://example.invalid/private-audio','dataExtension':'private-extension',
     'output':'private-output','createTime':1000,'durationMs':2000}]}}


class MemoTests(unittest.TestCase):
    def test_request_is_bounded_initial_read(self):
        self.assertEqual(p.list_request(),{'cursor':'','pageSize':20,'direction':0})
        for bad in [True,0,101,1.0,'20',None]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):p.list_request(bad)
    def test_text_only_projection_omits_private_metadata(self):
        result=p.parse_list(REPLY);self.assertEqual(result['items'],[{'text':'Memo fixture','created_at_ms':1000,'duration_ms':2000}])
        self.assertNotIn('private-',json.dumps(result));self.assertNotIn('https:',json.dumps(result))
        self.assertFalse(result['automatic_forwarding']);self.assertTrue(result['read_only'])
    def test_content_input_fallback_and_json_text(self):
        for content,input_,expected in [('plain','unused','plain'),('', '{"text":"fallback"}','fallback'),('{"other":"hidden"}','fallback','fallback'),('null','fallback','fallback')]:
            reply=copy.deepcopy(REPLY);reply['body']['missionList'][0].update(content=content,input=input_)
            self.assertEqual(p.parse_list(reply)['items'][0]['text'],expected)
    def test_non_memo_types_filtered_and_duplicates_not_displayed_twice(self):
        reply=copy.deepcopy(REPLY);rows=reply['body']['missionList'];rows.extend([copy.deepcopy(rows[0]),dict(rows[0],type=1),dict(rows[0],type=False)])
        result=p.parse_list(reply);self.assertEqual(len(result['items']),1);self.assertEqual(result['filtered_non_memos'],2)
    def test_rejected_or_malformed_response_does_not_become_empty_success(self):
        for bad in [None,{}, {'code':403,'body':REPLY['body']},{'code':200,'body':{}},{'code':200,'body':{'code':401,'missionList':[]}}, {'code':200,'body':{'missionList':[],'hasMore':'false'}}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):p.parse_list(bad)
        self.assertEqual(p.parse_list({'code':200,'body':{'missionList':[]}})['items'],[])
    def test_invalid_identity_and_resource_bounds(self):
        for key,value in [('missionId',''),('missionId',123),('content','x'*32001),('content','{"text":[]}'),('durationMs',True),('createTime',-1)]:
            reply=copy.deepcopy(REPLY);reply['body']['missionList'][0][key]=value
            with self.subTest(key=key,value_type=type(value).__name__),self.assertRaises(ValueError):p.parse_list(reply)
        reply=copy.deepcopy(REPLY);reply['body']['missionList']*=21
        with self.assertRaises(ValueError):p.parse_list(reply)


class MemoAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.backend=Backend(Path(self.tmp.name),1)
        self.backend.start=AsyncMock();self.backend.stop=AsyncMock();self.backend.assistant=AsyncMock()
        self.backend.channel=SimpleNamespace(ready=True,dropped_pushes=0,_request=AsyncMock(return_value=copy.deepcopy(REPLY)))
        self.client=TestClient(TestServer(make_app(self.backend)));await self.client.start_server()
        self.backend.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+self.backend.cookie,'X-CSRF-Token':self.backend.csrf}
    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()
    async def test_endpoint_requires_csrf_and_keeps_readiness_off(self):
        denied=await self.client.post('/api/audio-memos',json={});self.assertEqual(denied.status,403)
        response=await self.client.post('/api/audio-memos',json={},headers=self.headers)
        self.assertEqual(response.status,200);result=await response.json();self.assertEqual(len(result['items']),1)
        self.assertEqual(self.backend.ready_until,0);self.assertFalse(self.backend.rtc.active);self.assertFalse(self.backend.own_test)
        self.backend.assistant.assert_not_awaited();self.assertEqual(self.backend.channel._request.await_args.args[0],p.LIST_URI)
        self.assertEqual((Path(self.tmp.name)/'audio-memo-list.json').stat().st_mode&0o777,0o600)
    async def test_concurrency_and_expiry(self):
        await asyncio.gather(*(self.backend.audio_memos() for _ in range(3)))
        self.backend.channel._request.assert_awaited_once()
        self.backend.memo_cache[20]['at']-=31;await self.backend.audio_memos()
        self.assertEqual(self.backend.channel._request.await_count,2)
    async def test_invalid_limit_fails_without_rpc_and_error_is_not_cached(self):
        response=await self.client.post('/api/audio-memos',json={'limit':False},headers=self.headers)
        self.assertEqual(response.status,409);self.backend.channel._request.assert_not_awaited()
        self.backend.channel._request.return_value={'code':403}
        with self.assertRaises(ValueError):await self.backend.audio_memos()
        self.assertEqual(self.backend.memo_cache,{})


if __name__=='__main__':unittest.main()
