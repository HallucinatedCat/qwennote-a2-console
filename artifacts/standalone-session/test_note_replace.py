"""Synthetic checks for the native timed-word replacement contract."""
import copy
import unittest
import note_replace as p


def paragraph(parts=('ab','cd'),ident=10):
    words=[{'word':s,'startTime':i*100+1,'endTime':(i+1)*100,'wordId':str(i)} for i,s in enumerate(parts)]
    text=''.join(parts)
    return {'uuid':ident,'taskId':9,'paragraph':text,'uid':'synthetic','nickName':'Speaker','speakerId':'speaker',
            'startTime':1,'endTime':len(parts)*100,'mark':[],
            'content':[{'uid':'synthetic','startTime':1,'endTime':len(parts)*100,'sentence':text,'words':words,'subSentences':[]}]}


class ReplacementTests(unittest.TestCase):
    def test_view_page_projects_text_speaker_and_range_without_private_ids(self):
        row=paragraph();row['speakerDisplayModel']={'nickName':'Displayed speaker','uniqueId':'private-speaker'}
        result=p.view_page([row]);item=result['paragraphs'][0]
        self.assertEqual(item,{'content':'abcd','speaker':'Displayed speaker','start_ms':1,'end_ms':200})
        self.assertNotIn('private-speaker',json.dumps(result));self.assertNotIn('wordId',json.dumps(result))
        row=paragraph();row.pop('speakerDisplayModel',None)
        self.assertEqual(p.view_page([row])['paragraphs'][0]['speaker'],'Speaker')
    def test_view_page_rejects_duplicate_or_oversized_text(self):
        row=paragraph()
        with self.assertRaises(ValueError):p.view_page([row,row])
        row=paragraph();row['paragraph']='x'*(p.MAX_BYTES+1)
        with self.assertRaises(ValueError):p.view_page([row])

    def test_cross_word_edit_keeps_surviving_times_and_input(self):
        row=paragraph();before=copy.deepcopy(row)
        result=p.edit(row,'bc','XYZ')
        self.assertEqual(result['paragraph'],'aXYZd');self.assertEqual(row,before)
        self.assertEqual([w['word'] for w in result['content'][0]['words']],['aXYZ','d'])
        self.assertEqual([(w['startTime'],w['endTime'],w['wordId']) for w in result['content'][0]['words']],[(1,100,'0'),(101,200,'1')])
        self.assertEqual(result['speakerId'],'speaker');self.assertEqual(result['nickName'],'Speaker')

    def test_complete_word_removal_inserts_at_preceding_boundary(self):
        row=paragraph(('one ','two',' three'))
        result=p.edit(row,'two','X')
        self.assertEqual([w['word'] for w in result['content'][0]['words']],['one X',' three'])
        self.assertEqual([w['wordId'] for w in result['content'][0]['words']],['0','2'])

    def test_unicode_literals_multiple_matches_and_whole_paragraph(self):
        for text,needle,replacement in [('😀a😀','😀','猫猫'),('aa aa','aa','a'),('a.b.a.b','a.b','X'),('aaaa','aa',''),('abcd','abcd','whole')]:
            row=paragraph((text,));result=p.edit(row,needle,replacement)
            self.assertEqual(result['paragraph'],text.replace(needle,replacement))
            self.assertEqual(''.join(s['sentence'] for s in result['content']),result['paragraph'])
        whole=p.edit(paragraph(),'abcd','whole')
        self.assertEqual(whole['content'][0]['words'][0]['wordId'],'10#0')

    def test_sentence_groups_follow_original_timing(self):
        row=paragraph();second=copy.deepcopy(row['content'][0]);second.update(sentence='ef',startTime=201,endTime=300)
        second['words']=[{'word':'ef','wordId':'2','startTime':201,'endTime':300}]
        row['content'].append(second);row['paragraph']='abcdef';row['endTime']=300
        result=p.edit(row,'de','Z')
        self.assertEqual([s['sentence'] for s in result['content']],['abcZ','f'])
        self.assertEqual([s['startTime'] for s in result['content']],[1,201])

    def test_marks_inconsistent_text_and_bad_timing_refused(self):
        for change in ('mark','paragraph','timing','sentence','negative-sentence','reversed-word'):
            row=paragraph()
            if change=='mark':row['mark']=[{'index':0,'length':2}]
            if change=='paragraph':row['paragraph']='inconsistent'
            if change=='timing':row['content'][0]['words'][1]['startTime']=1
            if change=='sentence':row['content'][0]['sentence']='inconsistent'
            if change=='negative-sentence':row['content'][0]['startTime']=-1
            if change=='reversed-word':row['content'][0]['words'][0]['endTime']=0
            with self.assertRaises(ValueError):p.edit(row,'a','x')

    def test_generated_word_ids_do_not_hide_speaker_or_timing_changes(self):
        current=p.snapshot('synthetic-note',[paragraph()]);other=copy.deepcopy(current)
        other['paragraphs'][0]['content'][0]['words'][0]['wordId']='server-reassigned'
        self.assertEqual(p.semantic(current),p.semantic(other))
        other['paragraphs'][0]['nickName']='发言人 1'
        self.assertNotEqual(p.semantic(current),p.semantic(other))
        other=copy.deepcopy(current);other['paragraphs'][0]['content'][0]['words'][0]['endTime']+=1
        self.assertNotEqual(p.semantic(current),p.semantic(other))
        with self.assertRaises(ValueError):p.plan(p.snapshot('synthetic-note',[paragraph(('a'*1001,))]),'a','b')

    def test_snapshot_batch_limit_noop_and_original_payload(self):
        rows=[paragraph(ident=i) for i in range(21)]
        current=p.snapshot('synthetic-note',rows)
        with self.assertRaises(ValueError):p.plan(current,'ab','X')
        request,count=p.plan(current,'missing','X');self.assertEqual(count,0);self.assertEqual(request[0]['modifyParagraphs'],[])
        self.assertEqual(p.plan(current,'ab','ab')[1],0)
        for needle,replacement in [('', 'a'),('a',None),('a','\ud800')]:
            with self.assertRaises((ValueError,UnicodeError)):p.plan(current,needle,replacement)
        old=p.modification(rows[0]);self.assertEqual(old['content'],rows[0]['content']);self.assertEqual(old['paragraphId'],0)
        self.assertNotIn('uuid',old)
        with self.assertRaises(ValueError):p.snapshot('synthetic-note',[rows[0],rows[0]])

    def test_page_status_and_cursor_completeness(self):
        row=paragraph();reply={'code':200,'body':{'statusCode':200,'itemList':[row]}}
        self.assertEqual(p.page(reply),([row],False))
        for body in ({'statusCode':500,'itemList':[]},{'statusCode':200,'itemList':[],'hasNext':True},{'statusCode':200,'itemList':[row]*200}):
            with self.assertRaises(ValueError):p.page({'code':200,'body':body})
        self.assertEqual(p.read_request('synthetic-note',row)[0]['startTime'],2)

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import web_backend
from web_backend import Backend,make_app
from public_errors import Refused,OutcomeUnknown,CloudRejected,Unavailable,Busy
from test_backend_improvements import ready_backend
from test_note_favorites import UUID,LIST,detail
import note_favorites
import json


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.b=ready_backend(self.root);self.b.remember_notes(LIST);self.rows=[paragraph()];self.calls=[];self.mode=None
        async def request(uri,body,headers):
            self.calls.append(uri)
            if uri==note_favorites.DETAIL_URI:
                result=detail();result['body']['data'].update(taskId=9,gmtModified=100);return result
            if uri==p.READ_URI:
                if self.mode=='readback' and p.WRITE_URI in self.calls:raise TimeoutError('private')
                return {'code':200,'body':{'statusCode':200,'itemList':copy.deepcopy(self.rows)}}
            self.assertEqual(uri,p.WRITE_URI)
            saved=json.loads(self.b.replace_path.read_text());self.assertTrue(saved['pending'])
            self.assertEqual(saved['original']['paragraphs'],self.rows);self.assertFalse(self.b.health()['restart_safe'])
            if self.mode=='cancel':raise asyncio.CancelledError()
            if self.mode=='timeout':raise TimeoutError('private')
            if self.mode=='reject':return {'code':500}
            changes=body[0]['modifyParagraphs'][:1] if self.mode=='partial' else body[0]['modifyParagraphs']
            for modified in changes:
                row=next(r for r in self.rows if r['uuid']==modified['paragraphId']);row.update({k:copy.deepcopy(v) for k,v in modified.items() if k!='paragraphId'})
                for sentence in row['content']:
                    for i,word in enumerate(sentence['words']):word['wordId']='server-'+str(i)
            return {'code':200,'body':{'itemList':copy.deepcopy(self.rows)}}
        self.b.channel._request=AsyncMock(side_effect=request)

    async def test_preview_save_checks_timing_and_keeps_private_original(self):
        preview=await self.b.replace_note(UUID,'bc','XYZ');self.assertEqual(preview['matches'],1);self.assertNotIn(p.WRITE_URI,self.calls)
        self.assertEqual(preview['changes'][0]['after'],'aXYZd')
        self.assertNotIn('paragraphId',json.dumps(preview));self.assertNotIn(UUID,json.dumps(preview))
        result=await self.b.replace_note(UUID,'bc','XYZ',preview['revision']);self.assertTrue(result['verified']);self.assertEqual(self.rows[0]['paragraph'],'aXYZd')
        saved=json.loads(self.b.replace_path.read_text());self.assertEqual(saved['original']['paragraphs'][0]['paragraph'],'abcd')
        self.assertFalse(saved['pending']);self.assertEqual(self.b.replace_path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.calls.count(p.WRITE_URI),1);self.assertEqual(self.b.ready_until,0)
        self.assertNotIn('abcd',json.dumps(self.b.status()))

    async def test_stale_preview_marks_noop_ownership_and_busy_reject_without_write(self):
        preview=await self.b.replace_note(UUID,'bc','X');self.rows[0]['nickName']='changed'
        with self.assertRaises(Refused):await self.b.replace_note(UUID,'bc','X',preview['revision'])
        self.rows[0]['mark']=[{'index':0,'length':2}]
        with self.assertRaises(Refused):await self.b.replace_note(UUID,'bc','X')
        preview=await self.b.replace_note(UUID,'absent','X');result=await self.b.replace_note(UUID,'absent','X',preview['revision']);self.assertFalse(result['changed'])
        with self.assertRaises(Refused):await self.b.replace_note('foreign','a','b')
        async with self.b.replace_lock:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.replace_note(UUID,'a','b')
        self.assertNotIn(p.WRITE_URI,self.calls)

    async def test_uncertain_write_survives_restart_and_recovers_without_replay(self):
        preview=await self.b.replace_note(UUID,'bc','X');self.mode='readback'
        with self.assertRaises(Unavailable):await self.b.replace_note(UUID,'bc','X',preview['revision'])
        restored=Backend(self.root,18780);restored.channel=self.b.channel;restored.remember_notes(LIST)
        self.assertIn('note-replace-uncertain',restored.health()['blocking'])
        with self.assertRaises(Refused):await restored.replace_note(UUID,'bc','X',preview['revision'])
        self.mode=None;result=await restored.replace_note(UUID,'bc','X');self.assertFalse(result['pending']);self.assertEqual(result['matches'],0)
        self.assertEqual(self.calls.count(p.WRITE_URI),1)

    async def test_partial_batch_is_not_cleared_by_preview(self):
        self.rows.append(paragraph(ident=11));preview=await self.b.replace_note(UUID,'bc','X');self.mode='partial'
        with self.assertRaises(OutcomeUnknown):await self.b.replace_note(UUID,'bc','X',preview['revision'])
        self.mode=None;result=await self.b.replace_note(UUID,'bc','X');self.assertTrue(result['pending'])
        with self.assertRaises(Refused):await self.b.replace_note(UUID,'bc','X',result['revision'])
        self.assertEqual(self.calls.count(p.WRITE_URI),1)

    async def test_cancellation_rejection_timeout_and_identity_validation(self):
        for mode,error in [('cancel',asyncio.CancelledError),('reject',CloudRejected),('timeout',OutcomeUnknown)]:
            self.b.replace_pending=None;self.mode=None;self.calls=[]
            preview=await self.b.replace_note(UUID,'bc','X');self.mode=mode
            with self.assertRaises(error):await self.b.replace_note(UUID,'bc','X',preview['revision'])
            self.assertTrue(self.b.replace_pending);self.assertFalse(self.b.replace_lock.locked());self.assertEqual(self.calls.count(p.WRITE_URI),1)
        self.b.replace_pending=None;self.mode=None;self.rows[0]['taskId']=10
        with self.assertRaises(Unavailable):await self.b.replace_note(UUID,'bc','X')

    async def test_pagination_uses_advancing_native_cursor_and_refuses_duplicates(self):
        requests=[];first=paragraph();second=paragraph(ident=11);second['startTime']=101
        original=self.b.channel._request
        async def paged(uri,body,headers):
            if uri!=p.READ_URI:return await original(uri,body,headers)
            requests.append(body[0])
            rows=[first] if len(requests)%2 else [second]
            return {'code':200,'body':{'statusCode':200,'itemList':copy.deepcopy(rows),'hasNext':len(requests)%2==1}}
        self.b.channel._request=AsyncMock(side_effect=paged)
        result=await self.b.replace_note(UUID,'bc','X');self.assertEqual(result['matches'],2)
        self.assertEqual(requests[1]['startTime'],2);self.assertEqual(requests[1]['uuid'],first['uuid'])
        second['uuid']=first['uuid']
        with self.assertRaises(Unavailable):await self.b.replace_note(UUID,'bc','X')

    async def test_native_page_is_read_only_identity_checked_and_opaque(self):
        result=await self.b.native_transcript(UUID)
        self.assertEqual(result['source'],'native');self.assertFalse(result['has_more']);self.assertIsNone(result['next_token'])
        self.assertEqual(result['paragraphs'][0]['speaker'],'Speaker')
        self.assertNotIn(UUID,json.dumps(result));self.assertNotIn('paragraphId',json.dumps(result));self.assertNotIn('taskId',json.dumps(result))
        self.assertNotIn(p.WRITE_URI,self.calls);self.assertEqual(self.b.ready_until,0)
        self.assertEqual((self.root/'native-transcript-page.json').stat().st_mode&0o777,0o600)
    async def test_native_pagination_uses_host_tokens_and_rejects_stale_or_foreign_pages(self):
        detail_reply=detail();detail_reply['body']['data']['taskId']=9;detail_reply['body']['data']['gmtModified']=100
        async def request(uri,body,headers):
            if uri==note_favorites.DETAIL_URI:return copy.deepcopy(detail_reply)
            return {'code':200,'body':{'statusCode':200,'itemList':copy.deepcopy(self.rows),'hasNext':True}}
        self.b.channel._request=AsyncMock(side_effect=request)
        result=await self.b.native_transcript(UUID);token=result['next_token'];self.assertTrue(token)
        self.assertNotEqual(str(self.rows[0]['uuid']),token)
        self.assertRegex(token,r'^[A-Za-z0-9_-]{32,}$')
        second=paragraph(ident=11);second['startTime']=101;self.rows=[second]
        next_page=await self.b.native_transcript(UUID,token);self.assertEqual(next_page['paragraphs'][0]['content'],'abcd')
        self.assertEqual(self.b.channel._request.await_args_list[-1].args[1][0]['uuid'],10)
        detail_reply['body']['data']['gmtModified']=101
        web_backend.FORCE_REFRESH.set(True)  # a change elsewhere is noticed on a fresh read, not from the cache
        with self.assertRaises(Refused):await self.b.native_transcript(UUID,token)
        with self.assertRaises(Refused):await self.b.native_transcript('foreign',token)
        with self.assertRaises(Refused):await self.b.native_transcript(UUID,'not-issued')
        self.assertNotIn(p.WRITE_URI,self.calls)
    async def test_native_read_does_not_share_mcp_process_lock(self):
        async with self.b.document_lock:
            result=await self.b.native_transcript(UUID)
        self.assertEqual(result['source'],'native')
    async def test_native_read_busy_cancellation_and_restart_guard(self):
        started=asyncio.Event();never=asyncio.Event()
        async def hold(*args):started.set();await never.wait()
        self.b.channel._request=AsyncMock(side_effect=hold)
        task=asyncio.create_task(self.b.native_transcript(UUID));await started.wait()
        with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.native_transcript(UUID)
        self.assertIn('native-transcript-read-in-progress',self.b.health()['blocking'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.b.native_transcript_lock.locked())
    async def test_http_shape_csrf_and_explicit_save(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)));await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf};body={'task_uuid':UUID,'find':'bc','replace':'X'}
        self.assertEqual((await client.post('/api/note-replace-preview',json=body)).status,403)
        response=await client.post('/api/note-replace-preview',json=body,headers=headers);preview=await response.json();self.assertEqual(response.status,200)
        for bad in (body,{**body,'expected_revision':None},{**body,'expected_revision':preview['revision'],'paragraphId':10}):
            self.assertEqual((await client.post('/api/note-replace-save',json=bad,headers=headers)).status,409)
        self.assertNotIn(p.WRITE_URI,self.calls)
        self.assertEqual((await client.post('/api/note-transcript',json={'task_uuid':UUID,'next_token':None})).status,403)
        response=await client.post('/api/note-transcript',json={'task_uuid':UUID,'next_token':None},headers=headers)
        self.assertEqual(response.status,200);self.assertEqual((await response.json())['source'],'native')
        for bad in ({'task_uuid':UUID},{'task_uuid':UUID,'next_token':1},{'task_uuid':UUID,'next_token':None,'cursor':1}):
            self.assertEqual((await client.post('/api/note-transcript',json=bad,headers=headers)).status,409)
        self.assertNotIn(p.WRITE_URI,self.calls)
        response=await client.post('/api/note-replace-save',json={**body,'expected_revision':preview['revision']},headers=headers)
        self.assertEqual(response.status,200);self.assertTrue((await response.json())['verified'])

if __name__=='__main__':unittest.main()
