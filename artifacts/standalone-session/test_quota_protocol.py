import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from web_backend import Backend,make_app
from public_errors import Busy
import notification_settings as notification
import quota_protocol as p

CONFIG={'account_type':2,'account_id':'sample-account','target_device_id':4242}
DASH={'code':200,'body':{'dashboardUrl':'private-url','licenceBoardList':[{'boardType':'0','boardStatus':'1','planId':'private-plan',
 'resourceBoard':{'resourceNameContent':'Example plan','functionRangeContent':'Example entitlement','extraInfo':{'bizType':'hardware_month_quota_account'}},
 'functionBoardList':[{'functionNameContent':'','functionValueContent':'本月已用转写时长10/100分钟','functionValueTips':'Example reset date','extraInfo':{'used':'10','total':'100'},'buttonList':[{'buttonUrl':'private-url'}]}],
 'adBoardList':[{'adType':1,'adKey':'minutes_image_summary','adTitleContent':'Image summaries','adDetailContent':'8篇 剩余','limitFree':False},
 {'adType':1,'adKey':'minutes_ask_ai','adTitleContent':'AI Q&A','adDetailContent':'Unlimited during promotion','limitFree':True},
 {'adType':1,'adKey':'device_cloud_storage','adDetailContent':'1.5G 剩余'}]}]}}
DETAIL={'code':200,'body':{'list':[{'resourceTag':'personal_day_quota_account','resourceName':'Daily quota','resourceUsed':5,'resourceQuota':50,'startTime':'2026-01-01','endTime':'2026-01-31','updateTime':'2026-01-02 00:00:00'}]}}
TRAFFIC={'code':200,'body':{'used':1024,'remaining':3072,'quota':4096,'tips':'Example carrier notice','dailyUsageList':[{'used':1024,'updateTime':'2026-01-01'}],'simId':'private-sim'}}


class QuotaTests(unittest.TestCase):
 def test_fixed_read_contract_and_device_identity(self):
  r=p.requests(CONFIG);self.assertEqual(len(r),4)
  self.assertEqual(r[0][2],[{'business':'flash_minute','boardType':-1}])
  self.assertEqual(r[1][2][0]['deviceId'],'4242');self.assertEqual(r[3][2][0]['targetDeviceId'],4242)
  self.assertNotIn('corpId',r[0][2][0])
  with self.assertRaises(ValueError):p.requests(dict(CONFIG,target_device_id='invalid'))
 def test_enterprise_organization_contract_scopes_corp_id(self):
  r=p.requests(CONFIG,'12345')
  for name in ('account','device','detail'):
   payload=next(row[2][0] for row in r if row[0]==name);self.assertEqual(payload['corpId'],'12345')
  self.assertNotIn('corpId',r[3][2][0])
  for invalid in ('',True,'abc','-1',str(2**63)):
   with self.assertRaises(ValueError):p.requests(CONFIG,invalid)
 def test_counter_units_and_public_projection(self):
  board=p.dashboard(DASH)['boards'][0];self.assertEqual(board['membership'],'personal_paid')
  usage=board['usages'][0];self.assertEqual((usage['used'],usage['total'],usage['remaining'],usage['unit']),(10,100,90,'minutes'))
  self.assertEqual(board['benefits'][0]['remaining'],8);self.assertEqual(board['benefits'][2]['unit'],'G')
  self.assertNotIn('private-',json.dumps(board));self.assertFalse(board['text_summary_counter_reported'])
 def test_generic_and_future_benefit_rows_are_preserved_without_guessing(self):
  value=copy.deepcopy(DASH);value['body']['licenceBoardList'][0]['adBoardList'].append(
   {'adType':1,'adKey':'minutes_more_function','adTitleContent':'Additional feature','adDetailContent':'3篇 剩余'})
  board=p.dashboard(value)['boards'][0];row=board['benefits'][-1]
  self.assertEqual((row['category'],row['vendor_key'],row['remaining'],row['unit']),('other','minutes_more_function',3,'篇'))
  self.assertFalse(board['text_summary_counter_reported'])
 def test_unknown_negative_zero_and_unlimited_not_conflated(self):
  for val in (None,True,-1,'',float('nan'),float('inf'),'unknown','-1','9'*100,9007199254740992):self.assertIsNone(p.number(val))
  self.assertEqual(p.counters(0,0)['remaining'],0);self.assertEqual(p.counters(101,100)['remaining'],0)
  self.assertIsNone(p.counters(None,100)['remaining'])
  b=p.dashboard(DASH)['boards'][0]['benefits'][1];self.assertIsNone(b['remaining']);self.assertTrue(b['limited_time_free'])
  q=p.detail(DETAIL)['items'][0];self.assertTrue(q['daily_allowance']);self.assertEqual(q['remaining'],45);self.assertEqual(q['resets'],'2026-01-02 00:00:00')
 def test_unknown_resource_is_not_minutes(self):
  v=copy.deepcopy(DASH);v['body']['licenceBoardList'][0]['resourceBoard']['extraInfo']['bizType']='ai_credits'
  row=p.dashboard(v)['boards'][0]['usages'][0];self.assertEqual(row['category'],'other');self.assertIsNone(row['unit'])
 def test_traffic_is_kilobytes_with_unknown_billing_cycle(self):
  r=p.traffic(TRAFFIC);self.assertEqual(r['unit'],'KB');self.assertEqual(r['scale'],1024);self.assertIsNone(r['billing_cycle']);self.assertEqual(r['daily'][0]['used'],1024)
  self.assertNotIn('private-',json.dumps(r))
  v=copy.deepcopy(TRAFFIC);v['body']['remaining']=None;self.assertIsNone(p.traffic(v)['remaining'])
 def test_rejection_is_not_empty_success(self):
  for reply in ({}, {'code':403,'body':DASH['body']},{'code':200,'body':{'code':False,'licenceBoardList':[]}},{'code':200,'body':{}},{'code':200,'body':{'code':500,'licenceBoardList':[]}}):
   with self.subTest(reply=reply),self.assertRaises(ValueError):p.dashboard(reply)
  self.assertEqual(p.dashboard({'code':200,'body':{'licenceBoardList':[]}}),{'boards':[]})
 def test_resource_bounds_and_invalid_display(self):
  for change in ('too-many','bad-row','too-long','control'):
   v=copy.deepcopy(DASH)
   if change=='too-many':v['body']['licenceBoardList']*=9
   if change=='bad-row':v['body']['licenceBoardList']=[None]
   if change=='too-long':v['body']['licenceBoardList'][0]['resourceBoard']['resourceNameContent']='x'*257
   if change=='control':v['body']['licenceBoardList'][0]['resourceBoard']['resourceNameContent']='bad\x00'
   with self.subTest(change=change),self.assertRaises(ValueError):p.dashboard(v)


class QuotaAPITests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.b=Backend(Path(self.tmp.name),1);self.b.config=dict(CONFIG)
  self.b.start=AsyncMock();self.b.stop=AsyncMock()
  async def read(uri,payload,referer):
   return copy.deepcopy(TRAFFIC if 'TrafficI/' in uri else DETAIL if uri.endswith('/getLicenceDetail') else DASH)
  self.b.channel=SimpleNamespace(ready=True,dropped_pushes=0,_request=AsyncMock(side_effect=read))
  self.client=TestClient(TestServer(make_app(self.b)));await self.client.start_server();self.b.port=self.client.server.port
  self.headers={'Cookie':'qn_ui='+self.b.cookie,'X-CSRF-Token':self.b.csrf}
 async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()
 async def test_csrf_and_fixed_scope_no_device_gate_or_writes(self):
  r=await self.client.post('/api/quotas',json={});self.assertEqual(r.status,403)
  r=await self.client.post('/api/quotas',json={'deviceId':123},headers=self.headers);self.assertEqual(r.status,409)
  self.b.channel._request.assert_not_awaited()
  r=await self.client.post('/api/quotas',json={},headers=self.headers);self.assertEqual(r.status,200)
  result=await r.json();self.assertTrue(all(s['available'] for s in result['sections'].values()));self.assertTrue(result['read_only'])
  self.assertEqual(self.b.ready_until,0);self.assertFalse(self.b.own_test);self.assertFalse(self.b.rtc.active)
  self.assertEqual(self.b.channel._request.await_count,4)
  for f in Path(self.tmp.name).glob('quota-*.json'):self.assertEqual(f.stat().st_mode&0o777,0o600)
 async def test_cache_expiry_and_target_change(self):
  first=await self.b.quotas();self.assertEqual(await self.b.quotas(),first);self.assertEqual(self.b.channel._request.await_count,4)
  self.b.quota_cache['at']-=31;await self.b.quotas();self.assertEqual(self.b.channel._request.await_count,8)
  self.b.config['target_device_id']=4243;await self.b.quotas();self.assertEqual(self.b.channel._request.await_count,12)
 async def test_partial_failure_and_no_retry(self):
  self.b.channel._request.side_effect=[TimeoutError(),DASH,DETAIL,{'code':403}]
  r=await self.b.quotas();self.assertFalse(r['sections']['account']['available']);self.assertTrue(r['sections']['device']['available']);self.assertFalse(r['sections']['traffic']['available'])
  self.assertEqual(self.b.channel._request.await_count,4);self.assertNotIn('private-',json.dumps(r));self.assertIsNone(self.b.quota_cache)
 async def test_overlap_restart_guard_and_cancellation(self):
  started=asyncio.Event();never=asyncio.Event()
  async def hold(*args):started.set();await never.wait()
  self.b.channel._request.side_effect=hold;task=asyncio.create_task(self.b.quotas());await started.wait()
  with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.quotas()
  self.assertIn('quota-read-in-progress',self.b.health()['blocking'])
  task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertFalse(self.b.quota_lock.locked());self.assertIsNone(self.b.quota_cache)

 async def test_enterprise_selection_uses_opaque_handle_and_separate_cache(self):
  self.b.organization_options={'0':'Personal edition','12345':'Example enterprise'}
  handle=notification.handle(self.b.handle_secret,'12345')
  r=await self.client.post('/api/quotas',json={'organization_handle':handle},headers=self.headers);self.assertEqual(r.status,200)
  result=await r.json();self.assertEqual(result['organization_label'],'Example enterprise');self.assertEqual(result['organization_handle'],handle)
  self.assertNotIn('12345',json.dumps(result))
  for call in self.b.channel._request.await_args_list:
   uri,payload=call.args[:2]
   if 'TrafficI/' not in uri:self.assertEqual(payload[0]['corpId'],'12345')
   else:self.assertNotIn('corpId',payload[0])
  calls=self.b.channel._request.await_count
  stale=await self.client.post('/api/quotas',json={'organization_handle':'0'*32},headers=self.headers);self.assertEqual(stale.status,409)
  self.assertEqual(self.b.channel._request.await_count,calls)
  r=await self.client.post('/api/quotas',json={},headers=self.headers);self.assertEqual(r.status,200)
  self.assertEqual(self.b.channel._request.await_count,calls+4)

if __name__=='__main__':unittest.main()
