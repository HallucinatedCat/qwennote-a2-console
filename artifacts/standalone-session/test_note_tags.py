"""Note-tag replacement preserves unrelated memberships and rejects stale choices."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_tags as p
from web_backend import Backend,make_app
from public_errors import Refused,Busy,OutcomeUnknown,CloudRejected,Unavailable
from test_backend_improvements import ready_backend
from test_note_favorites import UUID,LIST,detail

A='synthetic-tag-a';B='synthetic-tag-b';C='synthetic-tag-c'
TAGS=[{'tagId':A,'displayName':'Research','autoMatch':False},{'tagId':B,'displayName':'Work','autoMatch':True},{'tagId':C,'displayName':'Other','autoMatch':False}]
def reply(tags):return {'code':200,'body':{'tagList':copy.deepcopy(tags)}}

class ProtocolTests(unittest.TestCase):
    def test_complete_list_identity_and_duplicate_validation(self):
        self.assertEqual(p.parse(detail(),reply(TAGS),reply([]),UUID)['selected'],[])
        bad=[None,{'code':500,'body':{'tagList':[]}},{'code':200,'body':{}},reply(TAGS*2),reply([{'tagId':A}]),{'code':200,'body':{'tagList':TAGS,'hasMore':True}},{'code':200,'body':{'tagList':TAGS,'nextToken':'more'}}]
        for data in bad:
            with self.assertRaises(ValueError):p.rows(data)
        with self.assertRaises(ValueError):p.parse(detail(False,'foreign'),reply(TAGS),reply([]),UUID)
        rejected=detail();rejected['body']['code']=500
        with self.assertRaises(ValueError):p.parse(rejected,reply(TAGS),reply([]),UUID)
        with self.assertRaises(ValueError):p.parse(detail(),reply(TAGS[:1]),reply(TAGS[1:]),UUID)
        renamed=copy.deepcopy(TAGS[:1]);renamed[0]['displayName']='changed'
        with self.assertRaises(ValueError):p.parse(detail(),reply(TAGS),reply(renamed),UUID)

    def test_handles_bound_to_instance_and_note_no_raw_ids_in_response(self):
        snapshot=p.parse(detail(),reply(TAGS),reply(TAGS[:1]),UUID)
        public=p.public('secret',snapshot);handles=[r['handle'] for r in public['options']]
        self.assertEqual(p.resolve('secret',snapshot,handles[:2]),[A,B])
        self.assertNotIn(A,json.dumps(public));self.assertNotIn(UUID,json.dumps(public))
        for values in ([A],[handles[0],handles[0]],[None]):
            with self.assertRaises(ValueError):p.resolve('secret',snapshot,values)
        with self.assertRaises(ValueError):p.resolve('other',snapshot,handles)
        with self.assertRaises(ValueError):p.resolve('secret',{**snapshot,'task_uuid':'other'},handles)
        self.assertEqual(p.request(UUID,[]),[{'taskUuid':UUID,'tagIdList':[]}])

    def test_definition_contract_preserves_description_and_matching(self):
        detail_reply={'code':200,'body':{'tagInfoModel':{'displayName':'Research','autoMatch':False,'description':'keep'}}}
        self.assertEqual(p.definition(detail_reply,A,{'label':'Research','auto_match':False}),
                         {'label':'Research','auto_match':False,'description':'keep'})
        self.assertEqual(p.rename_request(A,'  Renamed  ',False,'keep'),
                         [{'tagId':A,'tagInfoModel':{'displayName':'Renamed','autoMatch':False,'description':'keep'}}])
        self.assertEqual(p.tag_request(A),[{'tagId':A}])
        omitted={'code':200,'body':{'tagInfoModel':{'tagId':A,'displayName':'Research','autoMatch':False}}}
        self.assertEqual(p.definition(omitted,A,{'label':'Research','auto_match':False})['description'],'')
        for bad in ({'code':500,'body':{}},{'code':200,'body':{}},
                    {'code':200,'body':{'tagInfoModel':{'displayName':'Other','autoMatch':False,'description':'keep'}}},
                    {'code':200,'body':{'tagInfoModel':{'displayName':'Research','autoMatch':True,'description':'keep'}}},
                    {'code':200,'body':{'tagInfoModel':{'displayName':'Research','autoMatch':False,'description':None}}}):
            with self.assertRaises(ValueError):p.definition(bad,A,{'label':'Research','auto_match':False})

    def test_definition_reconciliation_accepts_only_original_or_requested(self):
        snapshot=p.parse(detail(),reply(TAGS),reply(TAGS[:1]),UUID)
        renamed_row={**TAGS[0],'displayName':'Renamed','autoMatch':True}
        renamed=p.parse(detail(),reply([renamed_row,*TAGS[1:]]),reply([renamed_row]),UUID)
        saved={'operation':'rename','original':snapshot,'tag_id':A,'name':'Renamed','auto_match':True}
        self.assertEqual(p.definition_reconciliation(saved,snapshot),'original')
        self.assertEqual(p.definition_reconciliation(saved,renamed),'requested')
        legacy_row={**TAGS[0],'displayName':'Renamed'}
        legacy=p.parse(detail(),reply([legacy_row,*TAGS[1:]]),reply([legacy_row]),UUID)
        self.assertEqual(p.definition_reconciliation({'operation':'rename','original':snapshot,'tag_id':A,'name':'Renamed'},legacy),'requested')
        third_row={**TAGS[0],'displayName':'Third'}
        third=p.parse(detail(),reply([third_row,*TAGS[1:]]),reply([third_row]),UUID)
        self.assertIsNone(p.definition_reconciliation(saved,third))
        deleted=p.parse(detail(),reply([TAGS[0],TAGS[2]]),reply(TAGS[:1]),UUID)
        saved={'operation':'delete','original':snapshot,'tag_id':B}
        self.assertEqual(p.definition_reconciliation(saved,deleted),'requested')
        self.assertEqual(p.definition_reconciliation(saved,snapshot),'original')

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.b=ready_backend(self.root);self.b.remember_notes(LIST);self.selected=[A];self.tags=copy.deepcopy(TAGS);self.calls=[];self.failure_mode=None
        async def request(uri,body,headers):
            self.calls.append((uri,body))
            if uri==p.DETAIL_URI:return detail()
            if uri==p.LIST_URI:return reply(self.tags)
            if uri==p.READ_URI:
                if self.failure_mode=='readback' and any(u in (p.WRITE_URI,p.CREATE_URI) for u,_ in self.calls):raise TimeoutError('private')
                return reply([r for r in self.tags if r['tagId'] in self.selected])
            if uri==p.CREATE_URI:
                saved=json.loads(self.b.tags_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['operation'],'create')
                if self.failure_mode=='cancel':raise asyncio.CancelledError()
                if self.failure_mode=='timeout':raise TimeoutError('private')
                if self.failure_mode=='reject':return {'code':500}
                self.tags.append({'tagId':'created-id','displayName':body[0]['displayName'],'autoMatch':body[0]['autoMatch']})
                return {'code':200,'body':{'code':0,'tagId':A if self.failure_mode=='old-id' else 'created-id'}}
            if uri==p.GET_DEF_URI:
                row=next(r for r in self.tags if r['tagId']==body[0]['tagId'])
                return {'code':200,'body':{'tagInfoModel':{'displayName':row['displayName'],'autoMatch':row['autoMatch'],'description':'keep'}}}
            if uri==p.UPDATE_URI:
                saved=json.loads(self.b.tags_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['operation'],'rename')
                if self.failure_mode=='timeout':raise TimeoutError('private')
                if self.failure_mode=='reject':return {'code':500}
                row=next(r for r in self.tags if r['tagId']==body[0]['tagId']);info=body[0]['tagInfoModel']
                self.assertEqual(info['description'],'keep')
                row['displayName']=info['displayName'];row['autoMatch']=info['autoMatch'];return {'code':200}
            if uri==p.DELETE_URI:
                saved=json.loads(self.b.tags_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['operation'],'delete')
                if self.failure_mode=='timeout':raise TimeoutError('private')
                if self.failure_mode=='reject':return {'code':500}
                self.tags=[r for r in self.tags if r['tagId']!=body[0]['tagId']];return {'code':200}
            self.assertEqual(uri,p.WRITE_URI)
            saved=json.loads(self.b.tags_path.read_text());self.assertTrue(saved['pending']);self.assertEqual(saved['original']['selected'],self.selected)
            self.assertFalse(self.b.health()['restart_safe'])
            if self.failure_mode=='timeout':raise TimeoutError('private')
            if self.failure_mode=='cancel':raise asyncio.CancelledError()
            if self.failure_mode=='reject':return {'code':500}
            self.selected=body[0]['tagIdList'] if self.failure_mode!='mismatch' else [C]
            return {'code':200}
        self.b.channel._request=AsyncMock(side_effect=request)

    async def test_create_and_explicit_assignment_preserve_original(self):
        before=await self.b.tags(UUID)
        result=await self.b.tags(UUID,expected=before['revision'],create_name='  New tag  ')
        self.assertEqual(self.selected,[A]);self.assertFalse(result['pending']);self.assertFalse(result['options'][-1]['auto_match'])
        self.assertFalse(result['options'][-1]['selected']);self.assertNotIn('created-id',json.dumps(result))
        self.assertEqual([u for u,_ in self.calls if u in (p.CREATE_URI,p.WRITE_URI)],[p.CREATE_URI])
        selected=[x['handle'] for x in result['options'] if x['selected']]+[result['created_handle']]
        await self.b.tags(UUID,selected,result['revision']);self.assertEqual(self.selected,sorted([A,'created-id']))

    async def test_create_validation_conflict_duplicate_and_utf16(self):
        self.assertEqual(p.create_request('  A😀  '),[{'displayName':'A😀','autoMatch':False}])
        before=await self.b.tags(UUID)
        for name in ('', '   ', 'a'*16, '😀'*8, 'a\nname', '\ud800', 12, ' Research '):
            with self.assertRaises(Refused):await self.b.tags(UUID,expected=before['revision'],create_name=name)
        self.tags[0]['displayName']='Changed'
        with self.assertRaises(Refused):await self.b.tags(UUID,expected=before['revision'],create_name='New')
        self.assertFalse(any(u==p.CREATE_URI for u,_ in self.calls))

    async def test_create_receipt_recovers_after_restart_without_replay(self):
        before=await self.b.tags(UUID);self.failure_mode='readback'
        with self.assertRaises(Unavailable):await self.b.tags(UUID,expected=before['revision'],create_name='New')
        self.assertEqual(self.b.tags_pending['created_id'],'created-id')
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        self.failure_mode=None;result=await restored.tags(UUID);self.assertFalse(result['pending']);self.assertEqual(self.selected,[A])
        self.assertEqual(sum(u==p.CREATE_URI for u,_ in self.calls),1)

    async def test_lost_or_invalid_creation_receipt_never_guesses_identity(self):
        for mode,error in [('timeout',OutcomeUnknown),('cancel',asyncio.CancelledError),('reject',CloudRejected),('old-id',OutcomeUnknown)]:
            self.b.tags_pending=None;self.failure_mode=None;self.tags=copy.deepcopy(TAGS);self.calls=[]
            before=await self.b.tags(UUID);self.failure_mode=mode
            with self.assertRaises(error):await self.b.tags(UUID,expected=before['revision'],create_name='New')
            self.assertTrue(self.b.tags_pending)
            self.failure_mode=None
            if mode!='old-id':self.tags.append({'tagId':'other-client','displayName':'New','autoMatch':False})
            current=await self.b.tags(UUID);self.assertTrue(current['pending'])
            with self.assertRaises(Refused):await self.b.tags(UUID,expected=current['revision'],create_name='Other')
            self.assertEqual(sum(u==p.CREATE_URI for u,_ in self.calls),1)

    async def test_add_preserves_existing_then_restore_exact_set(self):
        before=await self.b.tags(UUID);selection=[x['handle'] for x in before['options'][:2]]
        changed=await self.b.tags(UUID,selection,before['revision']);self.assertEqual(self.selected,[A,B])
        restored=await self.b.tags(UUID,[changed['options'][0]['handle']],changed['revision']);self.assertEqual(self.selected,[A]);self.assertFalse(restored['pending'])
        self.assertEqual(sum(u==p.WRITE_URI for u,_ in self.calls),2);self.assertEqual(self.b.ready_until,0)
        self.assertIsNone(self.b.tags_pending);self.assertEqual(self.b.tags_path.stat().st_mode&0o777,0o600)
        for private in (A,B,UUID):self.assertNotIn(private,json.dumps(self.b.status()))

    async def test_noop_conflicts_foreign_and_busy_send_no_write(self):
        before=await self.b.tags(UUID)
        self.assertFalse((await self.b.tags(UUID,[before['options'][0]['handle']],before['revision']))['changed'])
        self.tags[0]['displayName']='renamed'
        with self.assertRaises(Refused):await self.b.tags(UUID,[],before['revision'])
        with self.assertRaises(Refused):await self.b.tags('foreign')
        async with self.b.tags_lock:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.tags(UUID)
        self.assertFalse(any(u==p.WRITE_URI for u,_ in self.calls))

    async def test_unknown_survives_restart_and_reconciles_original_without_replay(self):
        before=await self.b.tags(UUID);self.failure_mode='timeout'
        with self.assertRaises(OutcomeUnknown):await self.b.tags(UUID,[],before['revision'])
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        self.assertTrue(restored.tags_pending);self.assertIn('note-tags-uncertain',restored.health()['blocking'])
        with self.assertRaises(Refused):await restored.tags(UUID,[],before['revision'])
        self.failure_mode=None;loaded=await restored.tags(UUID);self.assertFalse(loaded['pending'])
        self.assertEqual(sum(u==p.WRITE_URI for u,_ in self.calls),1)
        self.assertNotEqual(loaded['options'][0]['handle'],before['options'][0]['handle'])

    async def test_unrelated_third_state_remains_pending(self):
        before=await self.b.tags(UUID);self.failure_mode='mismatch'
        with self.assertRaises(OutcomeUnknown):await self.b.tags(UUID,[],before['revision'])
        loaded=await self.b.tags(UUID);self.assertTrue(loaded['pending'])
        with self.assertRaises(Refused):await self.b.tags(UUID,[],loaded['revision'])
        self.assertEqual(sum(u==p.WRITE_URI for u,_ in self.calls),1)

    async def test_cancel_rejection_readback_failure_retain_journal(self):
        for mode,error in [('cancel',asyncio.CancelledError),('reject',CloudRejected),('readback',Unavailable)]:
            self.b.tags_pending=None;self.failure_mode=None;self.calls=[];self.selected=[A]
            before=await self.b.tags(UUID);self.failure_mode=mode
            with self.assertRaises(error):await self.b.tags(UUID,[],before['revision'])
            self.assertTrue(self.b.tags_pending);self.assertFalse(self.b.tags_lock.locked())
            self.assertEqual(sum(u==p.WRITE_URI for u,_ in self.calls),1)

    async def test_rename_preserves_description_updates_matching_and_membership(self):
        before=await self.b.tags(UUID);handle=before['options'][0]['handle']
        unchanged=await self.b.tags(UUID,expected=before['revision'],rename_handle=handle,rename_name='Research',rename_auto_match=False)
        self.assertFalse(unchanged['changed']);self.assertFalse(any(uri==p.UPDATE_URI for uri,_ in self.calls))
        result=await self.b.tags(UUID,expected=before['revision'],rename_handle=handle,rename_name='  Renamed  ',rename_auto_match=True)
        self.assertEqual(result['options'][0]['label'],'Renamed');self.assertTrue(result['options'][0]['selected'])
        self.assertTrue(result['options'][0]['auto_match']);self.assertEqual(self.selected,[A]);self.assertIsNone(self.b.tags_pending)
        update=next(body for uri,body in self.calls if uri==p.UPDATE_URI)
        self.assertEqual(update,[{'tagId':A,'tagInfoModel':{'displayName':'Renamed','autoMatch':True,'description':'keep'}}])
        self.assertFalse(any(uri==p.WRITE_URI for uri,_ in self.calls))
        with self.assertRaises(Refused):await self.b.tags(UUID,expected=before['revision'],rename_handle=handle,rename_name='Other')

    async def test_delete_unassigned_and_selected_refusal(self):
        before=await self.b.tags(UUID)
        with self.assertRaises(Refused):await self.b.tags(UUID,expected=before['revision'],delete_handle=before['options'][0]['handle'])
        result=await self.b.tags(UUID,expected=before['revision'],delete_handle=before['options'][1]['handle'])
        self.assertEqual([x['label'] for x in result['options']],['Research','Other']);self.assertEqual(self.selected,[A])
        self.assertEqual(next(body for uri,body in self.calls if uri==p.DELETE_URI),[{'tagId':B}])
        self.assertFalse(any(uri==p.WRITE_URI for uri,_ in self.calls))

    async def test_definition_unknown_reconciles_original_without_replay(self):
        for operation in ('rename','delete'):
            self.b.tags_pending=None;self.failure_mode=None;self.tags=copy.deepcopy(TAGS);self.selected=[A];self.calls=[]
            before=await self.b.tags(UUID);handle=before['options'][0 if operation=='rename' else 1]['handle'];self.failure_mode='timeout'
            kwargs={'rename_handle':handle,'rename_name':'Renamed'} if operation=='rename' else {'delete_handle':handle}
            with self.assertRaises(OutcomeUnknown):await self.b.tags(UUID,expected=before['revision'],**kwargs)
            self.failure_mode=None;loaded=await self.b.tags(UUID);self.assertFalse(loaded['pending'])
            self.assertEqual(sum(uri in (p.UPDATE_URI,p.DELETE_URI) for uri,_ in self.calls),1)

    async def test_http_csrf_shape_and_invalid_raw_identifier_refused(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        self.assertEqual((await client.post('/api/note-tags',json={'task_uuid':UUID})).status,403)
        loaded=await (await client.post('/api/note-tags',json={'task_uuid':UUID},headers=headers)).json()
        body={'task_uuid':UUID,'selected_handles':[],'expected_revision':loaded['revision']}
        for bad in ({**body,'selected_handles':None},{**body,'selected_handles':[A]},{**body,'tagIdList':[]}):
            self.assertEqual((await client.post('/api/note-tags-save',json=bad,headers=headers)).status,409)
        self.assertFalse(any(u==p.WRITE_URI for u,_ in self.calls))
        response=await client.post('/api/note-tags-save',json=body,headers=headers)
        self.assertEqual(response.status,200);result=await response.json();self.assertTrue(result['verified']);self.assertEqual(self.selected,[])
        body={'task_uuid':UUID,'name':'New','expected_revision':result['revision']}
        for bad in ({**body,'name':None},{**body,'autoMatch':True}):
            self.assertEqual((await client.post('/api/note-tag-create',json=bad,headers=headers)).status,409)
        self.assertEqual((await client.post('/api/note-tag-create',json=body)).status,403)
        response=await client.post('/api/note-tag-create',json=body,headers=headers)
        self.assertEqual(response.status,200);created=await response.json();self.assertIn('created_handle',created);self.assertEqual(self.selected,[])
        rename={'task_uuid':UUID,'handle':created['created_handle'],'name':'Renamed','auto_match':True,'expected_revision':created['revision']}
        for bad in ({**rename,'name':None},{**rename,'auto_match':None},{**rename,'autoMatch':True},{**rename,'tagId':'raw'},{**rename,'handle':'raw'}):
            self.assertEqual((await client.post('/api/note-tag-rename',json=bad,headers=headers)).status,409)
        response=await client.post('/api/note-tag-rename',json=rename,headers=headers);self.assertEqual(response.status,200);renamed=await response.json()
        delete={'task_uuid':UUID,'handle':created['created_handle'],'expected_revision':renamed['revision']}
        for bad in ({**delete,'name':'extra'},{**delete,'handle':'raw'}):
            self.assertEqual((await client.post('/api/note-tag-delete',json=bad,headers=headers)).status,409)
        self.assertEqual((await client.post('/api/note-tag-delete',json=delete)).status,403)
        response=await client.post('/api/note-tag-delete',json=delete,headers=headers);self.assertEqual(response.status,200)
        deleted=await response.json();self.assertNotIn(created['created_handle'],[x['handle'] for x in deleted['options']])

if __name__=='__main__':unittest.main()
