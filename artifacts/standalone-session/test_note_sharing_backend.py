import copy,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from web_backend import Backend
from public_errors import Refused,OutcomeUnknown
import note_sharing as share
from test_note_sharing import reply,UUID,SECRET

class SharingBackendTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.b=Backend(Path(self.tmp.name),1);self.b.handle_secret=SECRET
  self.b.channel=SimpleNamespace(ready=True,_request=AsyncMock());self.b.note_refs={UUID:time.time()}

 def tearDown(self):self.tmp.cleanup()

 async def test_read_route_and_noop_do_not_write(self):
  self.b.channel._request.return_value=reply();value=await self.b.share(UUID)
  self.assertEqual(value['access'],'public');self.assertEqual(self.b.channel._request.await_count,1)
  noop=await self.b.share(UUID,'public',value['revision']);self.assertFalse(noop['changed']);self.assertEqual(self.b.channel._request.await_count,2)
  for call in self.b.channel._request.await_args_list:self.assertEqual(call.args[0],share.READ_URI)

 async def test_save_has_strict_readback_and_clears_journal(self):
  original=share.parse(reply());request=share.save_request(UUID,original,'private');observed=copy.deepcopy(reply());body=observed['body']
  body.update(scope=0,roleCode=request[0]['roleCode'],roleSubResourceIds=request[0]['roleSubResourceIds'])
  self.b.channel._request.side_effect=[reply(),{'code':200,'body':{}},observed]
  value=await self.b.share(UUID,'private',share.revision(SECRET,original))
  self.assertTrue(value['changed'] and value['readback_matches']);self.assertEqual(value['access'],'private');self.assertIsNone(self.b.share_pending)
  self.assertEqual(self.b.channel._request.await_count,3);self.assertEqual(self.b.channel._request.await_args_list[1].args[:2],(share.WRITE_URI,request))
  journal=json.loads(self.b.share_path.read_text());self.assertFalse(journal['pending']);self.assertEqual(journal['observed']['scope'],0)

 async def test_stale_revision_never_writes(self):
  self.b.channel._request.return_value=reply()
  with self.assertRaises(Refused):await self.b.share(UUID,'private','0'*64)
  self.assertEqual(self.b.channel._request.await_count,1);self.assertIsNone(self.b.share_pending)

 async def test_unknown_result_blocks_restart_and_read_reconciles_without_replay(self):
  original=share.parse(reply());request=share.save_request(UUID,original,'private');observed=copy.deepcopy(reply());observed['body'].update(scope=0,roleCode=request[0]['roleCode'],roleSubResourceIds=[])
  self.b.channel._request.side_effect=[reply(),ConnectionError('lost')]
  with self.assertRaises(OutcomeUnknown):await self.b.share(UUID,'private',share.revision(SECRET,original))
  self.assertIsNotNone(self.b.share_pending);self.assertFalse(self.b.health()['restart_safe']);self.assertEqual(self.b.channel._request.await_count,2)
  self.b.channel._request.reset_mock();self.b.channel._request.side_effect=None;self.b.channel._request.return_value=observed
  value=await self.b.share(UUID);self.assertEqual(value['access'],'private');self.assertIsNone(self.b.share_pending);self.assertTrue(self.b.health()['restart_safe'])
  self.assertEqual(self.b.channel._request.await_count,1);self.assertEqual(self.b.channel._request.await_args.args[0],share.READ_URI)

 async def test_journal_is_on_disk_before_the_write_and_blocks_writes_after_restart(self):
  original=share.parse(reply());seen=[]
  async def send(uri,body,referer):
   if uri!=share.WRITE_URI:return reply()
   seen.append(json.loads(self.b.share_path.read_text())['pending']);raise ConnectionError('lost')
  self.b.channel._request.side_effect=send
  with self.assertRaises(OutcomeUnknown):await self.b.share(UUID,'private',share.revision(SECRET,original))
  self.assertEqual(seen,[True])
  restarted=Backend(Path(self.tmp.name),1);restarted.channel=SimpleNamespace(ready=True,_request=AsyncMock());restarted.note_refs={UUID:time.time()}
  self.assertIn('note-share-uncertain',restarted.health()['blocking'])
  with self.assertRaises(Refused):await restarted.share(UUID,'public','0'*64)
  restarted.channel._request.assert_not_awaited()

 async def test_copy_url_is_read_only_for_private_and_public_scopes(self):
  for scope,access in ((0,'private'),(2,'public')):
   self.b.channel._request.reset_mock();self.b.channel._request.side_effect=None
   detail={'code':200,'body':{'data':{'uuid':UUID,'taskId':1,'shareUrl':'https://minutes.qwennote.cn/share/example?ticket=opaque'}}}
   self.b.channel._request.side_effect=[reply(scope),detail]
   value=await self.b.share_url(UUID)
   self.assertEqual(value['access'],access);self.assertEqual(value['url'],detail['body']['data']['shareUrl'])
   self.assertEqual([call.args[0] for call in self.b.channel._request.await_args_list],[share.READ_URI,share.DETAIL_URI])
   self.assertEqual(self.b.channel._request.await_count,2)
 async def test_copy_url_rejects_missing_url_without_a_write(self):
  detail={'code':200,'body':{'data':{'uuid':UUID,'taskId':1,'shareUrl':''}}}
  self.b.channel._request.side_effect=[reply(0),detail]
  with self.assertRaises(Refused):await self.b.share_url(UUID)
  self.assertEqual(self.b.channel._request.await_count,2)
  self.assertFalse(self.b.share_path.exists())

if __name__=='__main__':unittest.main()
