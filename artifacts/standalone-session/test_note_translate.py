"""Application translation is bounded, identity-checked and never edits the original note."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_translate as p
import note_favorites,note_replace
import web_backend
from web_backend import Backend,make_app
from public_errors import Refused,Busy,Unavailable
from test_backend_improvements import ready_backend
from test_note_favorites import UUID,LIST

ROW={'uuid':9,'startTime':0,'endTime':1000,'paragraph':'原文','taskId':77,'speakerDisplayModel':{'nickName':'Speaker'}}
ROW2={'uuid':10,'startTime':1001,'endTime':2000,'paragraph':'第二段','taskId':77,'speakerDisplayModel':{'nickName':'Speaker'}}

def detail(modified=100):return {'code':200,'body':{'data':{'uuid':UUID,'taskId':77,'gmtModified':modified}}}
def original(more=False):return {'code':200,'body':{'statusCode':200,'itemList':[copy.deepcopy(ROW)],'hasNext':more,'lang':{'langKey':'zh','langText':'Chinese'}}}
def options():return {'code':200,'body':{'statusCode':200,'languages':[{'langKey':'zh','nativeLang':'Original','commerceStatus':1},{'langKey':'en','nativeLang':'English','commerceStatus':1},{'langKey':'ja','nativeLang':'日本語','langText':'Japanese','commerceStatus':1}]}}
def translated(more=False,second=False):
 rows=[copy.deepcopy(ROW2 if second else ROW)]
 if second:rows.insert(0,copy.deepcopy(ROW))
 for row in rows:row['paragraph']='Second paragraph' if row['uuid']==10 else 'Translated text'
 return {'code':200,'body':{'statusCode':200,'itemList':rows,'hasNext':more,'lang':{'langKey':'en','langText':'English'}}}
def summary():return {'code':200,'body':{'title':'Translated title','fullTextSummary':{'taskUuid':UUID,'uuid':42,'value':'# Translated summary','valueMark':[]}}}

class ContractTests(unittest.TestCase):
 def test_requests_language_options_and_summary_projection(self):
  self.assertEqual(p.options_request(UUID),[{'sourceLang':'zh','needReturnSource':True,'taskUuid':UUID,'enableUseNewLanguageList':True}])
  self.assertEqual(p.transcript_request(UUID,'zh','en'),[{'taskUuid':UUID,'startTime':0,'uuid':None,'direction':0,'pageSize':200,'sourceLang':'zh','targetLang':'en'}])
  cursor={'uuid':9,'startTime':0};self.assertEqual(p.transcript_request(UUID,'zh','en',cursor)[0]['uuid'],9)
  self.assertEqual(p.summary_request(UUID,'zh','en'),[{'uuid':UUID,'sourceLang':'zh','targetLang':'en','summaryKey':'qwen-plus_default'}])
  self.assertEqual(p.source_language(original()),'zh')
  self.assertEqual(p.language_options(options(),'zh'),[{'key':'en','label':'English'},{'key':'ja','label':'日本語'}])
  self.assertEqual(p.summary_result(summary(),UUID),{'title':'Translated title','summary':'# Translated summary','keypoints':[],'actions':[]})
  for bad in ({'code':500,'body':{}},{'code':200,'body':{'statusCode':500,'languages':[]}}, {'code':200,'body':{'statusCode':200,'languages':[{'langKey':'en','nativeLang':'English','commerceStatus':1},{'langKey':'en','nativeLang':'Duplicate','commerceStatus':1}]}}):
   with self.assertRaises(ValueError):p.language_options(bad,'zh')
  foreign=summary();foreign['body']['fullTextSummary']['taskUuid']='foreign'
  with self.assertRaises(ValueError):p.summary_result(foreign,UUID)
  for args in (('bad uuid','zh','en'),(UUID,'zh','zh'),(UUID,'bad lang','en'),(UUID,'zh','BAD')):
   with self.assertRaises(ValueError):p.transcript_request(*args)

 def test_brief_sections_project_text_and_time_without_private_fields(self):
  reply=summary();reply['body']['keypoints']=[{'topic':'Translated topic','summary':'Translated point','beginTime':1000,'endTime':2000,'taskUuid':UUID,'uuid':99,'summaryMark':[{'private':True}]}]
  reply['body']['tingwuActions']=[{'value':'Translated action','beginTime':2000,'endTime':3000,'uuid':'private'}]
  self.assertEqual(p.summary_result(reply,UUID),{'title':'Translated title','summary':'# Translated summary','keypoints':[{'topic':'Translated topic','summary':'Translated point','start_ms':1000,'end_ms':2000}],'actions':[{'content':'Translated action','start_ms':2000,'end_ms':3000}]})
  for key,row in [('keypoints',{'topic':'Bad','summary':42}),('tingwuActions',{'value':42})]:
   bad=summary();bad['body'][key]=[row]
   with self.assertRaises(ValueError):p.summary_result(bad,UUID)
  foreign=summary();foreign['body']['keypoints']=[{'topic':'Foreign','summary':'Foreign','taskUuid':'foreign'}]
  with self.assertRaises(ValueError):p.summary_result(foreign,UUID)
  oversized=summary();oversized['body']['keypoints']=[{'topic':'x','summary':'x'}]*513
  with self.assertRaises(ValueError):p.summary_result(oversized,UUID)

class BackendTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.b=ready_backend(Path(self.tmp.name));self.b.remember_notes(LIST);self.calls=[];self.more=False;self.modified=100;self.fail=None
  async def request(uri,body,headers):
   self.calls.append(uri)
   if self.fail=='timeout':raise TimeoutError('private')
   if uri==note_favorites.DETAIL_URI:return detail(self.modified)
   if uri==note_replace.READ_URI:return original(self.more)
   if uri==p.OPTIONS_URI:self.assertEqual(body,p.options_request(UUID));return options()
   if uri==p.TRANSCRIPT_URI:
    second=body[0]['uuid'] is not None
    self.assertEqual(body,p.transcript_request(UUID,'zh','en',{'uuid':9,'startTime':0} if second else None))
    return translated(self.more and not second,second)
   if uri==p.SUMMARY_URI:self.assertEqual(body,p.summary_request(UUID,'zh','en'));return summary()
   raise AssertionError(uri)
  self.b.channel._request=AsyncMock(side_effect=request)
 async def test_options_then_translation_have_no_original_or_preference_write(self):
  available=await self.b.translate_note(UUID);self.assertEqual(available['source_language'],'zh');self.assertEqual(len(available['targets']),2)
  self.assertNotIn(p.TRANSCRIPT_URI,self.calls);self.assertNotIn(p.SUMMARY_URI,self.calls)
  result=await self.b.translate_note(UUID,'en');self.assertEqual(result['paragraphs'][0]['content'],'Translated text');self.assertEqual(result['summary'],'# Translated summary');self.assertEqual(result['source_language'],'zh');self.assertEqual(result['target_language'],'en')
  self.assertEqual(self.calls.count(p.TRANSCRIPT_URI),1);self.assertEqual(self.calls.count(p.SUMMARY_URI),1);self.assertEqual(self.b.ready_until,0)
 async def test_continuation_is_opaque_bound_and_drops_duplicate(self):
  self.more=True;first=await self.b.translate_note(UUID,'en');token=first['next_token'];self.assertTrue(token)
  second=await self.b.translate_note(UUID,next_token=token);self.assertEqual([x['content'] for x in second['paragraphs']],['Second paragraph']);self.assertIsNone(second['summary']);self.assertEqual(second['keypoints'],[]);self.assertEqual(second['actions'],[]);self.assertIsNone(second['next_token'])
  with self.assertRaises(Refused):await self.b.translate_note('foreign',next_token=token)
  with self.assertRaises(Refused):await self.b.translate_note(UUID,next_token='bad')
  self.assertEqual(self.calls.count(p.SUMMARY_URI),1)
 async def test_stale_busy_invalid_and_failure_never_replay(self):
  self.more=True;first=await self.b.translate_note(UUID,'en');self.modified=101
  web_backend.FORCE_REFRESH.set(True)  # a change elsewhere is noticed on a fresh read, not from the cache
  with self.assertRaises(Refused):await self.b.translate_note(UUID,next_token=first['next_token'])
  with self.assertRaises(Refused):await self.b.translate_note(UUID,'fr')
  self.assertEqual(self.calls.count(p.TRANSCRIPT_URI),1)
  async with self.b.native_transcript_lock:
   with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.translate_note(UUID,'en')
  self.fail='timeout'
  with self.assertRaises(Unavailable):await self.b.translate_note(UUID,'en')
  self.assertEqual(self.calls.count(p.TRANSCRIPT_URI),1)
 async def test_http_strict_shapes_and_csrf(self):
  self.b.start=AsyncMock();self.b.stop=AsyncMock();client=TestClient(TestServer(make_app(self.b)));await client.start_server();self.addAsyncCleanup(client.close);self.b.port=client.port
  headers={'Cookie':'qn_ui='+self.b.cookie,'X-CSRF-Token':self.b.csrf}
  self.assertEqual((await client.post('/api/note-translate-options',json={'task_uuid':UUID},headers=headers)).status,200)
  for path,data in [('/api/note-translate-options',{}),('/api/note-translate',{'task_uuid':UUID}),('/api/note-translate',{'task_uuid':UUID,'target_language':'en','next_token':None}),('/api/note-translate',{'task_uuid':UUID,'next_token':'x'})]:
   self.assertEqual((await client.post(path,json=data,headers=headers)).status,409)
  response=await client.post('/api/note-translate',json={'task_uuid':UUID,'target_language':'en'},headers=headers);self.assertEqual(response.status,200);self.assertEqual((await response.json())['target_language'],'en')
  self.assertEqual((await client.post('/api/note-translate',json={'task_uuid':UUID,'target_language':'en'})).status,403)

if __name__=='__main__':unittest.main()
