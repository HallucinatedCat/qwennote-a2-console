"""Native favorites preserve state on conflicts/ambiguous writes; no live calls."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_favorites as p
from web_backend import Backend,make_app
from public_errors import Refused,Busy,OutcomeUnknown,Unavailable,CloudRejected
from test_backend_improvements import ready_backend

UUID='synthetic-owned-note'
LIST={'content':[{'type':'text','text':json.dumps({'items':[{'uuid':UUID,'collected':False}]})}]}
def detail(value=False,uuid=UUID):return {'code':200,'body':{'data':{'uuid':uuid,'collected':value}}}

class ProtocolTests(unittest.TestCase):
    def test_app_encoding_and_identity_validation(self):
        self.assertEqual(p.collect_request(UUID,True),[{'taskUuid':UUID,'type':1}])
        self.assertEqual(p.collect_request(UUID,False)[0]['type'],2)
        self.assertEqual(p.detail_request(UUID),[{'uuid':UUID,'enableMinutesDetailTab':True}])
        for reply in (None,{},detail(1),detail(False,'foreign'),{'code':403,'body':detail()['body']}):
            with self.subTest(reply=reply),self.assertRaises(ValueError):p.collected(reply,UUID)
        for value in (1,'true',None):
            with self.assertRaises(ValueError):p.collect_request(UUID,value)
        for reply in (None,{'code':200,'body':{'code':True}},{'code':200,'body':{'code':500}}):self.assertFalse(p.acknowledged(reply))

    def test_only_bounded_own_list_shape_provides_context(self):
        self.assertEqual(p.listed_notes(LIST),[UUID])
        for bad in ({'isError':True,**LIST},{'structuredContent':{'items':[{'uuid':'bad/path'}]}},
                    {'structuredContent':{'items':[{'uuid':UUID}]*21}},{'content':[{'type':'text','text':'{broken'}]}):
            self.assertEqual(p.listed_notes(bad),[])

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.b=ready_backend(self.root)
        self.b.remember_notes(LIST);self.value=False;self.fail=None;self.calls=[]
        async def request(uri,body,headers):
            self.calls.append((uri,body))
            if uri==p.DETAIL_URI:
                if self.fail=='readback' and len(self.calls)>1:raise TimeoutError('private')
                return detail(self.value)
            self.assertEqual(uri,p.COLLECT_URI)
            saved=json.loads(self.b.favorite_path.read_text());self.assertTrue(saved['pending']);self.assertFalse(saved['original'])
            self.assertFalse(self.b.health()['restart_safe'])
            if self.fail=='timeout':raise TimeoutError('private')
            if self.fail=='cancel':raise asyncio.CancelledError()
            if self.fail=='reject':return {'code':500,'body':{'code':500}}
            self.value=body[0]['type']==1
            return {'code':200,'body':{'code':0}}
        self.b.channel._request=AsyncMock(side_effect=request)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_change_reads_journals_writes_and_verifies_without_arming_device(self):
        result=await self.b.favorite(UUID,True,False)
        self.assertTrue(result['collected']);self.assertTrue(result['readback_matches'])
        self.assertEqual([c[0] for c in self.calls],[p.DETAIL_URI,p.COLLECT_URI,p.DETAIL_URI])
        self.assertEqual(self.b.ready_until,0);self.assertFalse(self.b.own_test)
        self.assertFalse(json.loads(self.b.favorite_path.read_text())['pending'])
        self.assertNotIn(UUID,json.dumps(self.b.status()))

    async def test_noop_conflict_and_unknown_target_do_not_write(self):
        result=await self.b.favorite(UUID,False,False);self.assertFalse(result['changed'])
        with self.assertRaises(Refused):await self.b.favorite(UUID,False,True)
        with self.assertRaises(Refused):await self.b.favorite('foreign',True,False)
        self.b.note_refs[UUID]=time.time()-24*3600-1
        with self.assertRaises(Refused):await self.b.favorite(UUID,True,False)
        self.assertTrue(all(uri==p.DETAIL_URI for uri,_ in self.calls));self.assertFalse(self.b.favorite_path.exists())

    async def test_timeout_retains_journal_and_read_only_refresh_resolves_after_restart(self):
        self.fail='timeout'
        with self.assertRaises(OutcomeUnknown):await self.b.favorite(UUID,True,False)
        self.assertEqual(len(self.calls),2);self.assertTrue(self.b.favorite_pending)
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        with self.assertRaises(Refused):await restored.favorite(UUID,True,False)
        self.assertEqual(len(self.calls),2)
        self.fail=None;self.value=True
        checked=await restored.favorite(UUID)
        self.assertTrue(checked['collected']);self.assertIsNone(restored.favorite_pending)
        self.assertEqual(self.calls[-1][0],p.DETAIL_URI)

    async def test_readback_failure_retains_ack_and_never_repeats_write(self):
        self.fail='readback'
        with self.assertRaises(Unavailable):await self.b.favorite(UUID,True,False)
        self.assertEqual([u for u,_ in self.calls].count(p.COLLECT_URI),1)
        saved=json.loads(self.b.favorite_path.read_text());self.assertTrue(saved['pending']);self.assertTrue(saved['cloud_acknowledged'])

    async def test_rejection_and_cancellation_preserve_uncertainty(self):
        for mode,error in [('reject',CloudRejected),('cancel',asyncio.CancelledError)]:
            self.fail=mode;self.b.favorite_pending=None;self.calls=[]
            with self.assertRaises(error):await self.b.favorite(UUID,True,False)
            self.assertTrue(self.b.favorite_pending);self.assertEqual(len(self.calls),2)

    async def test_busy_and_rtc_guards_send_nothing(self):
        async with self.b.favorite_lock:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.favorite(UUID,True,False)
        self.b.rtc.pending=True
        with self.assertRaises(Refused):await self.b.favorite(UUID,True,False)
        self.assertEqual(self.calls,[])

    async def test_http_csrf_strict_shape_and_read_recovery(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        body={'task_uuid':UUID,'collected':True,'expected_collected':False}
        self.assertEqual((await client.post('/api/note-favorite',json=body)).status,403)
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        for bad in ({**body,'extra':True},{**body,'collected':None},{**body,'collected':1}):
            self.assertEqual((await client.post('/api/note-favorite',json=bad,headers=headers)).status,409)
        self.assertEqual(self.calls,[])
        reply=await client.post('/api/note-favorite',json=body,headers=headers)
        self.assertEqual(reply.status,200);self.assertTrue((await reply.json())['collected'])
        checked=await client.post('/api/note-favorite-status',json={'task_uuid':UUID},headers=headers)
        self.assertTrue((await checked.json())['verified'])

if __name__=='__main__':unittest.main()
