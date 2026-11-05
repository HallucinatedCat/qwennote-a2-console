"""Default-summary regeneration is explicit, journaled and never replayed."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
import note_summary as p
from web_backend import Backend, make_app
from public_errors import Refused, OutcomeUnknown, CloudRejected
from test_backend_improvements import ready_backend
from test_note_favorites import UUID, LIST
from test_note_summary import detail, brief


def generated(text='# Regenerated', **changes):
    body={'code':200,'taskStatus':'succeed','subTaskStatus':2,'containsNoteContent':False,'content':text}
    body.update(changes)
    return {'code':200,'body':body}


def empty_brief():return {'code':200,'body':{'extractStatus':2,'fullTextSummary':{}}}


class ContractTests(unittest.TestCase):
    def test_default_request_and_bounded_success_shape(self):
        self.assertEqual(p.manual_regenerate_request(UUID),[{'minutesTaskUuid':UUID,'templateId':'default','modelId':'qwen-plus'}])
        self.assertEqual(p.regenerate_request(UUID),[{'minutesTaskUuid':UUID,'summaryTemplateId':'default','modelId':'qwen-plus'}])
        self.assertEqual(p.regenerated_content(generated()),'# Regenerated')
        self.assertIsNone(p.regenerated_content(generated('',taskStatus='ongoing')))
        self.assertIsNone(p.read_regenerated(empty_brief(),UUID))
        self.assertEqual(p.read_regenerated(brief('# Visible'),UUID),'# Visible')
        foreign=brief();foreign['body']['fullTextSummary']['taskUuid']='foreign'
        with self.assertRaises(ValueError):p.read_regenerated(foreign,UUID)
        with self.assertRaises(ValueError):p.regenerate_request('bad uuid')
        for reply in (generated(''),generated('![image](https://example.test/x)'),generated('# Rich',containsNoteContent=True),
                      generated('# Later',taskStatus='ongoing'),generated('# Later',webSocketUrl='wss://example.test'),
                      generated('# Bad',code=500),{'code':500,'body':{}}):
            with self.assertRaises(ValueError):p.regenerated_content(reply)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.b=ready_backend(self.root);self.b.remember_notes(LIST);self.value=brief()['body']['fullTextSummary']['value'];self.generated_text='# Regenerated';self.eventual_reads=0;self.empty_reads=0;self.calls=[];self.fail=None
        async def request(uri,body,headers):
            self.calls.append(uri)
            if uri==p.DETAIL_URI:return detail()
            if uri==p.READ_URI:
                if self.fail=='eventual' and p.REGENERATE_URI in self.calls:
                    self.eventual_reads+=1
                    if self.eventual_reads==3:self.value=self.generated_text
                if self.fail=='empty' and p.REGENERATE_URI in self.calls:
                    self.empty_reads+=1
                    if self.empty_reads==1:return empty_brief()
                    if self.empty_reads==3:self.value=self.generated_text
                if self.fail=='async' and p.REGENERATE_URI in self.calls:
                    self.empty_reads+=1
                    if self.empty_reads==1:return empty_brief()
                    if self.empty_reads==2:self.value=self.generated_text
                return brief(self.value)
            if uri==p.MANUAL_REGENERATE_URI:
                saved=json.loads(self.b.summary_path.read_text());self.assertEqual(saved['phase'],'manual')
                if self.fail=='manual_timeout':raise TimeoutError('private')
                return {'code':200,'body':{}}
            self.assertEqual(uri,p.REGENERATE_URI);self.assertEqual(body,p.regenerate_request(UUID))
            saved=json.loads(self.b.summary_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['operation'],'regenerate')
            self.assertFalse(self.b.health()['restart_safe'])
            if self.fail=='timeout':raise TimeoutError('private')
            if self.fail=='cancel':raise asyncio.CancelledError()
            if self.fail=='rejected':return {'code':500}
            if self.fail=='ongoing':return generated(taskStatus='ongoing')
            if self.fail=='async':return generated('',taskStatus='ongoing')
            if self.fail in ('eventual','empty'):return generated(self.generated_text)
            if self.fail=='mismatch':
                self.value='another generator';return generated(self.generated_text)
            self.value=self.generated_text
            return generated(self.value)
        self.b.channel._request=AsyncMock(side_effect=request)

    async def before(self):return await self.b.summary(UUID)

    async def test_explicit_regeneration_readback_and_same_result(self):
        before=await self.before();result=await self.b.regenerate(UUID,before['revision'])
        self.assertEqual(result['text'],'# Regenerated');self.assertTrue(result['changed']);self.assertTrue(result['readback_matches'])
        self.assertIsNone(self.b.summary_pending);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)
        self.assertEqual(self.b.summary_path.stat().st_mode&0o777,0o600)
        self.value=before['text'];self.generated_text=before['text'];before=await self.before();result=await self.b.regenerate(UUID,before['revision'])
        self.assertFalse(result['changed']);self.assertEqual(result['text'],before['text'])

    async def test_stale_busy_and_foreign_never_regenerate(self):
        before=await self.before();self.value='external change'
        with self.assertRaises(Refused):await self.b.regenerate(UUID,before['revision'])
        with self.assertRaises(Refused):await self.b.regenerate('foreign','0'*64)
        async with self.b.mutation_lock:
            with self.assertRaises(Exception):await asyncio.wait_for(self.b.regenerate(UUID,before['revision']),.1)
        self.assertNotIn(p.REGENERATE_URI,self.calls)

    async def test_eventual_readback_uses_bounded_reads_without_replay(self):
        before=await self.before();self.fail='eventual'
        with patch('asyncio.sleep',new=AsyncMock()) as sleep:
            result=await self.b.regenerate(UUID,before['revision'])
        self.assertEqual(result['text'],'# Regenerated');self.assertTrue(result['changed'])
        self.assertEqual(self.calls.count(p.REGENERATE_URI),1);self.assertEqual(self.calls.count(p.MANUAL_REGENERATE_URI),1);self.assertEqual(self.eventual_reads,4);self.assertEqual(sleep.await_count,2)

    async def test_empty_intermediate_summary_is_polled_without_replay(self):
        before=await self.before();self.fail='empty'
        with patch('asyncio.sleep',new=AsyncMock()) as sleep:result=await self.b.regenerate(UUID,before['revision'])
        self.assertEqual(result['text'],'# Regenerated');self.assertEqual(self.empty_reads,4);self.assertEqual(sleep.await_count,2)
        self.assertEqual(self.calls.count(p.MANUAL_REGENERATE_URI),1);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)

    async def test_async_generation_uses_final_brief_without_websocket_or_replay(self):
        before=await self.before();self.fail='async'
        accepted=await self.b.regenerate(UUID,before['revision'])
        self.assertTrue(accepted['accepted']);self.assertTrue(accepted['pending']);self.assertFalse(accepted['verified'])
        self.assertEqual(self.calls.count(p.MANUAL_REGENERATE_URI),1);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)
        self.fail=None;self.value=self.generated_text;loaded=await self.b.summary(UUID)
        self.assertEqual(loaded['text'],'# Regenerated');self.assertFalse(loaded['pending']);self.assertIsNone(self.b.summary_pending)
        self.assertEqual(self.calls.count(p.MANUAL_REGENERATE_URI),1);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)

    async def test_manual_timeout_never_calls_generator_or_replays(self):
        before=await self.before();self.fail='manual_timeout'
        with self.assertRaises(OutcomeUnknown):await self.b.regenerate(UUID,before['revision'])
        self.assertEqual(self.calls.count(p.MANUAL_REGENERATE_URI),1);self.assertEqual(self.calls.count(p.REGENERATE_URI),0)
        self.fail=None;loaded=await self.b.summary(UUID);self.assertFalse(loaded['pending'])

    async def test_timeout_refresh_reconciles_without_replay(self):
        before=await self.before();self.fail='timeout'
        with self.assertRaises(OutcomeUnknown):await self.b.regenerate(UUID,before['revision'])
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        self.assertIn('note-summary-uncertain',restored.health()['blocking'])
        with self.assertRaises(Refused):await restored.regenerate(UUID,before['revision'])
        self.fail=None;self.value=before['text'];loaded=await restored.summary(UUID)
        self.assertFalse(loaded['pending']);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)

    async def test_rejection_cancellation_and_async_result_remain_pending(self):
        for mode,error in [('cancel',asyncio.CancelledError),('rejected',CloudRejected),('ongoing',OutcomeUnknown),('mismatch',OutcomeUnknown)]:
            self.b.summary_pending=None;self.fail=None;self.calls=[];self.value=brief()['body']['fullTextSummary']['value']
            before=await self.before();self.fail=mode
            with self.assertRaises(error):await self.b.regenerate(UUID,before['revision'])
            self.assertTrue(self.b.summary_pending);self.assertEqual(self.calls.count(p.REGENERATE_URI),1)
            self.assertFalse(self.b.summary_lock.locked())

    async def test_http_requires_revision_and_exact_shape(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        response=await client.post('/api/note-summary',json={'task_uuid':UUID},headers=headers);loaded=await response.json()
        data={'task_uuid':UUID,'expected_revision':loaded['revision']}
        for bad in ({'task_uuid':UUID},{**data,'text':'unexpected'},{**data,'expected_revision':'bad'}):
            self.assertEqual((await client.post('/api/note-regenerate',json=bad,headers=headers)).status,409)
        response=await client.post('/api/note-regenerate',json=data,headers=headers)
        self.assertEqual(response.status,200);self.assertTrue((await response.json())['verified'])
        self.assertEqual(self.calls.count(p.REGENERATE_URI),1)


if __name__=='__main__':unittest.main()
