"""App summary edits preserve originals, reject stale reads and reconcile uncertainty."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_summary as p
from web_backend import Backend,make_app
from public_errors import Refused,Unavailable,OutcomeUnknown,Busy,CloudRejected
from test_backend_improvements import ready_backend
from test_note_favorites import UUID,LIST

def detail():return {'code':200,'body':{'data':{'uuid':UUID,'summaryKey':p.KEY,'enableSelfModelSummary':True}}}
def brief(text='# Original\n\nKeep **this**.'):
    return {'code':200,'body':{'extractStatus':2,'fullTextSummary':{'taskUuid':UUID,'uuid':42,'value':text,'valueMark':[]}}}

class ContractTests(unittest.TestCase):
    def test_identity_format_selection_and_failed_business_result_rejected(self):
        self.assertEqual(p.parse(detail(),brief(),UUID)['text'],'# Original\n\nKeep **this**.')
        variants=[]
        for field,value in [('uuid','foreign'),('summaryKey','different_template'),('enableSelfModelSummary',1),('diyTemplateKey','custom')]:
            d=detail();d['body']['data'][field]=value;variants.append((d,brief()))
        b=brief();b['body']['fullTextSummary']['taskUuid']='foreign';variants.append((detail(),b))
        b=brief();b['body']['code']=500;variants.append((detail(),b))
        for d,b in variants:
            with self.assertRaises(ValueError):p.parse(d,b,UUID)
        for value in ('',' ','x'*(p.MAX_BYTES+1),'![picture](https://example.test/image)','<img src=x>','{"type":"rich"}',None):
            with self.assertRaises(ValueError):p.validate_text(value)

    def test_default_payload_preserves_metadata_and_revision(self):
        d=detail();d['body']['data']['enableSelfModelSummary']=False
        snapshot=p.parse(d,brief(),UUID);request=p.request(UUID,snapshot,'new summary')[0]
        self.assertEqual(request['fullTextSummary'],{**snapshot['full'],'value':'new summary'})
        self.assertNotEqual(p.revision(snapshot),p.revision({**snapshot,'text':'other'}))
        self.assertEqual(p.request(UUID,p.parse(detail(),brief(),UUID),'new summary')[0]['fullTextSummary'],{'value':'new summary'})
        self.assertTrue(p.acknowledged({'code':200}))
        self.assertFalse(p.acknowledged({'code':200,'body':{'code':500}}))

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.b=ready_backend(self.root);self.b.remember_notes(LIST);self.value=brief()['body']['fullTextSummary']['value'];self.calls=[];self.fail=None
        async def request(uri,body,headers):
            self.calls.append(uri)
            if uri==p.DETAIL_URI:return detail()
            if uri==p.READ_URI:
                if self.fail=='readback' and p.WRITE_URI in self.calls:raise TimeoutError('private')
                return brief(self.value)
            self.assertEqual(uri,p.WRITE_URI)
            saved=json.loads(self.b.summary_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['original']['text'],self.value)
            self.assertFalse(self.b.health()['restart_safe'])
            if self.fail=='timeout':raise TimeoutError('private payload')
            if self.fail=='cancel':raise asyncio.CancelledError()
            if self.fail=='rejected':return {'code':500}
            self.value=body[0]['fullTextSummary']['value'] if self.fail!='mismatch' else 'another editor'
            return {'code':200}
        self.b.channel._request=AsyncMock(side_effect=request)

    async def test_load_change_and_restore_have_exact_readback_and_private_journal(self):
        before=await self.b.summary(UUID);self.assertFalse(before['pending'])
        changed=await self.b.summary(UUID,'# Changed',before['revision']);self.assertEqual(changed['text'],'# Changed')
        self.assertFalse(changed['pending']);self.assertEqual(self.b.ready_until,0)
        restored=await self.b.summary(UUID,before['text'],changed['revision']);self.assertEqual(restored['text'],before['text'])
        self.assertEqual(self.calls.count(p.WRITE_URI),2);self.assertIsNone(self.b.summary_pending)
        self.assertEqual(self.b.summary_path.stat().st_mode&0o777,0o600)
        self.assertNotIn(before['text'],json.dumps(self.b.status()));self.assertNotIn(UUID,json.dumps(self.b.status()))

    async def test_foreign_stale_noop_and_busy_never_write(self):
        before=await self.b.summary(UUID)
        self.assertFalse((await self.b.summary(UUID,before['text'],before['revision']))['changed'])
        self.value='external change'
        with self.assertRaises(Refused):await self.b.summary(UUID,'new',before['revision'])
        with self.assertRaises(Refused):await self.b.summary('foreign')
        async with self.b.summary_lock:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.summary(UUID)
        self.assertNotIn(p.WRITE_URI,self.calls)

    async def test_unknown_result_survives_restart_and_refresh_reconciles_without_write(self):
        before=await self.b.summary(UUID);self.fail='timeout'
        with self.assertRaises(OutcomeUnknown):await self.b.summary(UUID,'requested',before['revision'])
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        self.assertTrue(restored.summary_pending);self.assertIn('note-summary-uncertain',restored.health()['blocking'])
        with self.assertRaises(Refused):await restored.summary(UUID,'again',before['revision'])
        self.fail=None;self.value='requested';result=await restored.summary(UUID)
        self.assertFalse(result['pending']);self.assertEqual(self.calls.count(p.WRITE_URI),1)

    async def test_unrelated_readback_retains_journal_and_prevents_replay(self):
        before=await self.b.summary(UUID);self.fail='mismatch'
        with self.assertRaises(OutcomeUnknown):await self.b.summary(UUID,'requested',before['revision'])
        refreshed=await self.b.summary(UUID);self.assertTrue(refreshed['pending'])
        with self.assertRaises(Refused):await self.b.summary(UUID,'again',refreshed['revision'])
        self.assertEqual(self.calls.count(p.WRITE_URI),1)

    async def test_cancellation_rejection_and_readback_failure_preserve_pending(self):
        for mode,error in [('cancel',asyncio.CancelledError),('rejected',CloudRejected),('readback',Unavailable)]:
            self.b.summary_pending=None;self.fail=None;self.calls=[]
            before=await self.b.summary(UUID);self.fail=mode
            with self.assertRaises(error):await self.b.summary(UUID,before['text']+' more',before['revision'])
            self.assertTrue(self.b.summary_pending);self.assertEqual(self.calls.count(p.WRITE_URI),1)
            self.assertFalse(self.b.summary_lock.locked())

    async def test_http_csrf_input_and_saved_shape(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        self.assertEqual((await client.post('/api/note-summary',json={'task_uuid':UUID})).status,403)
        response=await client.post('/api/note-summary',json={'task_uuid':UUID},headers=headers);loaded=await response.json()
        data={'task_uuid':UUID,'text':'new','expected_revision':loaded['revision']}
        for bad in ({**data,'text':None},{**data,'extra':True}):
            self.assertEqual((await client.post('/api/note-summary-save',json=bad,headers=headers)).status,409)
        response=await client.post('/api/note-summary-save',json=data,headers=headers)
        self.assertEqual(response.status,200);self.assertTrue((await response.json())['verified'])
        self.assertEqual(self.calls.count(p.WRITE_URI),1)

if __name__=='__main__':unittest.main()
