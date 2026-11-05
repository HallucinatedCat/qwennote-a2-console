"""Regression tests for the UI-facing backend changes. Synthetic data only."""
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import capabilities
import web_backend
from web_backend import Backend,make_app
from public_errors import Busy,CloudRejected,NotReady,OutcomeUnknown,Refused

CONFIG=dict(endpoint='wss://minutes.qwennote.cn/example',app_key='test',client_device_id='test',user_agent='test',access_token='test',account_id='123',account_type=2,target_device_id=456,native_client_id='test-client',native_client_secret='synthetic-secret')


def ready_backend(root):
    b=Backend(root,18780);b.config=dict(CONFIG);b.device={'model':'QwenNote A2','cloud_online_status':1}
    b.writable_keys={'default_source_language'}
    b.setting_details={'default_source_language':{'editable':True,'inherited':False,'choices':{'en':'English','zh':'Chinese'}}}
    b.settings={'default_source_language':'en'}
    b.channel=SimpleNamespace(ready=True,dropped_pushes=0,_request=AsyncMock(return_value={'code':200}),read=AsyncMock(return_value=devices()))
    return b


def devices(online=1,properties=()):
    """A bound-device read for the synthetic A2 (target_device_id 456)."""
    return {'body':{'deviceModel':[{'deviceId':456,'devServId':1446,'onlineStatus':online,
                                    'deviceProperties':[{'propertyId':k,'propertyName':v} for k,v in properties]}]}}


class BackendLogicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.b=ready_backend(self.root)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_unarmed_refusal_is_public_and_sends_nothing(self):
        with self.assertRaises(NotReady):await self.b.record('start')
        self.b.channel._request.assert_not_awaited()

    async def test_cloud_rejection_is_distinct_and_keeps_ownership(self):
        b=self.b;b.ready_until=time.time()+60;b.channel._request.return_value={'code':500}
        with self.assertRaises(CloudRejected):await b.record('start')
        self.assertEqual(b.record_state['phase'],'rejected');self.assertTrue(b.own_test)
        self.assertEqual(b.channel._request.await_count,1)

    async def test_timeout_is_outcome_unknown_and_not_retried(self):
        b=self.b;b.ready_until=time.time()+60;b.channel._request.side_effect=TimeoutError
        with self.assertRaises(OutcomeUnknown):await b.record('start')
        self.assertEqual(b.record_phase,'outcome-unknown');self.assertEqual(b.record_state['phase'],'unknown')
        self.assertEqual(b.channel._request.await_count,1)

    async def test_failed_readback_keeps_acknowledgement_and_journal(self):
        b=self.b;b.ready_until=time.time()+60
        calls={'n':0}
        async def refresh():
            calls['n']+=1
            if calls['n']>1:raise ConnectionError('synthetic')
        b.refresh=refresh
        result=await b.change_setting('default_source_language','zh')
        self.assertTrue(result['cloud_acknowledged']);self.assertTrue(result['readback_failed'])
        self.assertIsNone(result['cloud_readback_matches'])
        saved=json.loads(b.setting_restore_path.read_text());self.assertEqual(saved['default_source_language']['original'],'en')

    async def test_confirm_stop_requires_explicit_true(self):
        b=self.b;b.own_test=True
        with self.assertRaises(Refused):await b.confirm_stopped({'physically_stopped':'yes'})
        self.assertTrue(b.own_test)
        await b.confirm_stopped({'physically_stopped':True})
        self.assertFalse(b.own_test);self.assertEqual(b.record_state['phase'],'confirmed-stopped')

    async def test_status_since_returns_only_new_events_and_server_time(self):
        b=self.b
        for n in range(5):b.emit('device-event',{'n':n})
        full=b.status();self.assertEqual(full['event_cursor'],5);self.assertEqual(len(full['events']),5)
        partial=b.status(since=3);self.assertEqual([e['id'] for e in partial['events']],[4,5])
        self.assertAlmostEqual(partial['server_time'],time.time(),delta=5)
        self.assertNotIn('verification_levels',full)  # the evidence register is not shown in the UI

    async def test_health_reports_blockers_without_identifiers(self):
        b=self.b;self.assertTrue(b.health()['restart_safe'])
        b.own_test=True;health=b.health()
        self.assertFalse(health['restart_safe']);self.assertIn('disposable-test-open',health['blocking'])
        self.assertNotIn('456',json.dumps(health));self.assertNotIn('123',json.dumps(health))

    async def test_latest_event_write_is_debounced(self):
        b=self.b;path=self.root/'latest-device-event.json'
        for n in range(20):b.process_event({'type':770000,'payload':{'eventType':'x','n':n}})
        self.assertFalse(path.exists())
        b.flush_latest_event()
        self.assertEqual(json.loads(path.read_text())['payload']['n'],19);self.assertEqual(path.stat().st_mode&0o777,0o600)

    async def test_command_intent_and_ownership_are_saved_before_sending(self):
        b=self.b;b.ready_until=time.time()+60;journal=self.root/'last-command.json';seen=[]
        async def send(uri,body,referer):
            seen.append((uri,json.loads(journal.read_text())['result'],json.loads(b.test_path.read_text())['open']))
            raise TimeoutError
        b.channel._request.side_effect=send
        with self.assertRaises(OutcomeUnknown):await b.record('start')
        self.assertEqual(seen,[(web_backend.RECORD_URI,'pending',True)])
        self.assertEqual(json.loads(journal.read_text())['result'],'pending');self.assertEqual(journal.stat().st_mode&0o777,0o600)

    async def test_acknowledged_command_replaces_the_pending_intent(self):
        b=self.b;b.ready_until=time.time()+60
        await b.record('start')
        saved=json.loads((self.root/'last-command.json').read_text())
        self.assertNotIn('result',saved);self.assertEqual(saved['reply'],{'code':200})

    async def test_health_names_each_in_flight_operation(self):
        b=self.b
        def flag(owner,attribute,value,cleared):
            return (lambda:setattr(owner,attribute,value)),(lambda:setattr(owner,attribute,cleared))
        cases=[('rtc-active',*flag(b.rtc,'active',True,False)),
               ('rtc-cleanup-pending',*flag(b.rtc,'pending',True,False)),
               ('note-favorite-uncertain',*flag(b,'favorite_pending',{'pending':True},None)),
               ('capture-active',lambda:b.start_capture(5),b.stop_capture),
               ('command-in-progress',b.mutation_lock.acquire,b.mutation_lock.release),
               ('record-task-read-in-progress',b.record_task_lock.acquire,b.record_task_lock.release)]
        for name,enter,leave in cases:
            with self.subTest(blocker=name):
                entered=enter()
                if asyncio.iscoroutine(entered):await entered
                health=b.health()
                self.assertEqual(health['blocking'],[name]);self.assertFalse(health['restart_safe'])
                leave();self.assertTrue(b.health()['restart_safe'])

    async def test_every_startup_journal_must_not_be_a_link(self):
        names=('disposable-test.json','setting-restore.json','generation-restore.json','notification-restore.json',
               'note-favorite.json','note-summary.json','note-tags.json','note-replace.json','note-share.json','device-name.json')
        for name in names:
            with self.subTest(journal=name),tempfile.TemporaryDirectory() as tmp:
                state=Path(tmp);target=state/'elsewhere.json';target.write_text('{}')
                (state/name).symlink_to(target)
                with self.assertRaises(ValueError):Backend(state,18780)

    async def test_rtc_cleanup_journal_blocks_restart_after_reload(self):
        journal=self.root/'rtc-runs'/'rtc-probe-pending.json'
        web_backend.private_write(journal,{'pending':True})
        health=Backend(self.root,18780).health()
        self.assertEqual(health['blocking'],['rtc-cleanup-pending']);self.assertFalse(health['restart_safe'])

    async def test_latest_event_disk_error_does_not_raise(self):
        b=self.b
        with patch('web_backend.private_write',side_effect=OSError('disk full')):
            b.keep_latest_event({'type':770000,'payload':{}});b.flush_latest_event()

    async def test_reconnect_refuses_while_worker_runs_and_restarts_after(self):
        b=self.b;b.task=asyncio.create_task(asyncio.sleep(10))
        with self.assertRaises(Refused):b.reconnect()
        b.task.cancel()
        with self.assertRaises(asyncio.CancelledError):await b.task
        with patch.object(Backend,'worker',AsyncMock()):
            self.assertTrue(b.reconnect()['reconnecting']);await b.task

    async def test_document_lock_contention_is_busy(self):
        mcp=self.root/'mcp';(mcp/'.private').mkdir(parents=True,mode=0o700)
        (mcp/'qwennote_reader.py').write_text('READ_TOOLS={"list_my_minutes"}\nasync def connected(args,state):\n    return {"call":{"result":{}}}\n')
        b=self.b;b.mcp_root=mcp
        import sys
        sys.modules.pop('qwennote_reader',None)
        self.addCleanup(lambda:(sys.modules.pop('qwennote_reader',None),str(mcp) in sys.path and sys.path.remove(str(mcp))))
        with patch('web_backend.fcntl.flock',side_effect=BlockingIOError):
            with self.assertRaises(Busy):await b.documents('list_my_minutes',{})
        await b.documents('list_my_minutes',{});await b.documents('list_my_minutes',{})
        self.assertEqual(sys.path.count(str(mcp)),1)

    async def test_document_reads_wait_for_the_earlier_read(self):
        import sys
        fake=SimpleNamespace(READ_TOOLS={'list_my_minutes'},connected=AsyncMock(return_value={'call':{'result':{'isError':False}}}))
        b=self.b;b.mcp_root=self.root/'mcp';(b.mcp_root/'.private').mkdir(parents=True,mode=0o700)
        with patch.dict(sys.modules,{'qwennote_reader':fake}):
            await b.document_lock.acquire()
            waiting=asyncio.create_task(b.documents('list_my_minutes',{}))
            await asyncio.sleep(0.05)
            self.assertFalse(waiting.done());fake.connected.assert_not_awaited()
            b.document_lock.release()
            self.assertEqual(await waiting,{'isError':False});fake.connected.assert_awaited_once()

    async def test_document_timeout_releases_both_locks(self):
        import sys,fcntl,os
        from public_errors import Unavailable
        fake=SimpleNamespace(READ_TOOLS={'list_my_minutes'},connected=AsyncMock(side_effect=TimeoutError('secret-fixture')))
        b=self.b;b.mcp_root=self.root/'mcp';state=b.mcp_root/'.private';state.mkdir(parents=True,mode=0o700)
        with patch.dict(sys.modules,{'qwennote_reader':fake}):
            with self.assertRaises(Unavailable) as raised:await b.documents('list_my_minutes',{})
            self.assertNotIn('secret-fixture',str(raised.exception));self.assertFalse(b.document_lock.locked())
            fd=os.open(state/'session.lock',os.O_RDWR)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
            fake.connected.side_effect=None;fake.connected.return_value={'call':{'result':{'isError':False}}}
            self.assertEqual(await b.documents('list_my_minutes',{}),{'isError':False})

    async def test_each_request_queues_on_its_own_feature_lock_then_reports_busy(self):
        import sys
        b=self.b;uuid='synthetic-note';b.note_refs={uuid:time.time()}
        b.settings.update(storage_type='0',enable_incognito_mode='0')
        b.mcp_root=self.root/'mcp';(b.mcp_root/'.private').mkdir(parents=True,mode=0o700)
        fake=SimpleNamespace(READ_TOOLS={'list_my_minutes'},connected=AsyncMock())
        cases=[('transcript',b.native_transcript_lock,lambda:b.native_transcript(uuid,None)),
               ('translation',b.native_transcript_lock,lambda:b.translate_note(uuid)),
               ('export',b.export_lock,lambda:b.export_note(uuid,'transcript',None,False,'txt')),
               ('media',b.media_lock,lambda:b.download_note_media(uuid)),
               ('documents',b.document_lock,lambda:b.documents('list_my_minutes',{})),
               ('favorite',b.favorite_lock,lambda:b.favorite(uuid)),
               ('summary',b.summary_lock,lambda:b.summary(uuid)),
               ('share',b.share_lock,lambda:b.share(uuid)),
               ('tags',b.tags_lock,lambda:b.tags(uuid)),
               ('replace',b.replace_lock,lambda:b.replace_note(uuid,'a','b')),
               ('quotas',b.quota_lock,lambda:b.quotas())]
        with patch.dict(sys.modules,{'qwennote_reader':fake}),patch('web_backend.QUEUE_SECONDS',.05):
            for name,lock,call in cases:
                with self.subTest(operation=name):
                    await lock.acquire()
                    try:
                        with self.assertRaises(Busy) as raised:await call()
                        self.assertEqual(str(raised.exception),'An earlier request of the same kind is running; try again shortly')
                    finally:lock.release()
            # Another process holding the MCP session lock is the one case that is "another process".
            with patch('web_backend.fcntl.flock',side_effect=BlockingIOError),self.assertRaises(Busy) as raised:
                await b.documents('list_my_minutes',{})
            self.assertEqual(str(raised.exception),'Another process is using the document reader; try again shortly')
        b.channel._request.assert_not_awaited();fake.connected.assert_not_awaited()

    async def test_note_reads_run_during_device_commands_and_alongside_each_other(self):
        b=self.b;uuid='synthetic-note';b.note_refs={uuid:time.time()};gate=asyncio.Event();sent=[]
        async def request(uri,*args):
            sent.append(uri);await gate.wait();return {}
        b.channel._request=AsyncMock(side_effect=request)
        async with b.mutation_lock:  # a device command is in progress
            reads=[asyncio.create_task(call) for call in (b.favorite(uuid),b.share(uuid),b.summary(uuid),b.tags(uuid))]
            for _ in range(50):
                if len(sent)>=4:break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(len(sent),4,'every feature read reached the channel while the others were in flight')
            self.assertIn('note-operation-in-progress',b.health()['blocking'])
            gate.set()
            await asyncio.gather(*reads,return_exceptions=True)
        self.assertNotIn('note-operation-in-progress',b.health()['blocking'])

    async def test_independent_reads_are_sent_together(self):
        b=self.b;uuid='synthetic-note';b.note_refs={uuid:time.time()}
        for label,call,expected in (('summary',lambda:b._read_summary(uuid),2),('tags',lambda:b._read_tags(uuid),3),
                                    ('quotas',lambda:b.quotas(),len(web_backend.quota_protocol.requests(b.config,None)))):
            with self.subTest(read=label):
                gate=asyncio.Event();in_flight=[]
                async def request(uri,*args):
                    in_flight.append(uri);await gate.wait();return {}
                b.channel._request=AsyncMock(side_effect=request)
                task=asyncio.create_task(call())
                for _ in range(50):
                    if len(in_flight)>=expected:break
                    await asyncio.sleep(0.01)
                self.assertEqual(len(in_flight),expected,'all reads in flight before any reply')
                gate.set();await asyncio.gather(task,return_exceptions=True)

    async def test_document_in_flight_blocks_safe_restart(self):
        await self.b.document_lock.acquire()
        try:
            health=self.b.health();self.assertFalse(health['restart_safe'])
            self.assertIn('document-read-in-progress',health['blocking'])
            self.assertTrue(self.b.status()['document_read_in_progress'])
        finally:self.b.document_lock.release()

    async def test_planned_renewal_reconnects_without_backoff(self):
        b=self.b;entered=[]
        expiring={**CONFIG,'expires_at':time.time()+30};fresh={**CONFIG,'expires_at':time.time()+3600}
        configs=iter([expiring,fresh,fresh])
        class FakeLWP:
            def __init__(self,endpoint,headers):pass
            async def __aenter__(self):
                entered.append(time.monotonic())
                return SimpleNamespace(ready=True,failure=None,dropped_pushes=0,pushes=asyncio.Queue())
            async def __aexit__(self,*_):return False
        b.refresh=AsyncMock()
        with patch('web_backend.ensure_fresh',side_effect=lambda path,config:(next(configs),None)),patch('web_backend.LWP',FakeLWP):
            started=time.monotonic();b.task=asyncio.create_task(b.worker())
            while len(entered)<2 and time.monotonic()-started<3:await asyncio.sleep(0.02)
            b.task.cancel()
            with self.assertRaises(asyncio.CancelledError):await b.task
        self.assertEqual(len(entered),2);self.assertLess(entered[1]-entered[0],1.0)
        kinds=[(e['kind'],e['data'].get('state')) for e in b.events]
        self.assertIn(('connection','renewing'),kinds);self.assertNotIn(('connection','disconnected'),kinds)

    async def test_renewal_outcomes_are_counted_and_never_leak_values(self):
        b=self.b;b.config={**CONFIG,'refresh_token':'SECRET-REFRESH-1','access_token':'SECRET-ACCESS-1','expires_at':time.time()+3000}
        fresh={**b.config,'access_token':'SECRET-ACCESS-2'}
        with patch('web_backend.ensure_fresh',return_value=(fresh,{'http_status':200})):await b.renew()
        self.assertEqual(b.renewals['count'],1);self.assertIsNotNone(b.renewals['last_at'])
        with patch('web_backend.ensure_fresh',side_effect=RuntimeError('SECRET-ACCESS-2 in message')):
            with self.assertRaises(RuntimeError):await b.renew()
        self.assertEqual(b.renewals['failures'],1);self.assertEqual(b.renewals['last_failure_type'],'RuntimeError')
        kinds=[e['data'] for e in b.events if e['kind']=='credential']
        self.assertEqual(kinds,[{'renewed':True},{'renewed':False,'error_type':'RuntimeError'}])
        status=json.dumps(b.status())
        for value in ('SECRET-REFRESH-1','SECRET-ACCESS-1','SECRET-ACCESS-2','synthetic-secret','100200300'):self.assertNotIn(value,status)
        service=next(i for i in b.status()['credentials']['items'] if i['id']=='native_service')
        self.assertEqual(service['state'],'valid');self.assertEqual(service['renewals']['failures'],1)

    async def test_no_renewal_needed_is_not_counted(self):
        b=self.b
        with patch('web_backend.ensure_fresh',return_value=(b.config,None)):await b.renew()
        self.assertEqual(b.renewals['count'],0);self.assertFalse([e for e in b.events if e['kind']=='credential'])

    async def test_credential_status_is_cached_briefly(self):
        b=self.b
        with patch('web_backend.credential_status.collect',wraps=__import__('credential_status').collect) as collect:
            b.credentials();b.credentials()
            self.assertEqual(collect.call_count,1)
            b.credential_cache['checked_at']-=11;b.credentials()
            self.assertEqual(collect.call_count,2)


class WebLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();b=self.b=ready_backend(Path(self.tmp.name));b.port=1
        b.start=AsyncMock();b.stop=AsyncMock()
        self.client=TestClient(TestServer(make_app(b)));await self.client.start_server();b.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()

    async def test_refusal_message_reaches_ui(self):
        r=await self.client.post('/api/ready',headers=self.headers,json={'physical_ready':True})
        self.assertEqual(r.status,409);body=await r.json()
        self.assertEqual(body['code'],'refused');self.assertIn('control',body['message'])

    async def test_non_object_body_is_refused(self):
        r=await self.client.post('/api/ready',headers=self.headers,json=[1,2])
        self.assertEqual(r.status,409);self.assertEqual((await r.json())['code'],'refused')
        r=await self.client.post('/api/ready',headers={**self.headers,'Content-Type':'application/json'},data='{not json')
        self.assertEqual(r.status,409)

    async def test_each_page_load_gets_its_own_script_nonce(self):
        nonces=[]
        for _ in range(2):
            r=await self.client.get('/');page=await r.text()
            policy=r.headers['Content-Security-Policy'];nonce=re.search(r"'nonce-([^']+)'",policy).group(1)
            self.assertIn(f'nonce="{nonce}"',page);self.assertNotEqual(nonce,self.b.csrf);nonces.append(nonce)
            self.assertNotIn('__NONCE__',page);self.assertNotIn('__CSRF__',page)
        self.assertNotEqual(nonces[0],nonces[1])
        r=await self.client.get('/api/status',headers=self.headers)
        self.assertIn("script-src 'none'",r.headers['Content-Security-Policy'])

    async def test_healthz_needs_loopback_host_but_no_cookie(self):
        r=await self.client.get('/healthz');self.assertEqual(r.status,200)
        self.assertEqual(set(await r.json()),{'connection','worker_running','restart_safe','blocking'})
        r=await self.client.get('/healthz',headers={'Host':'evil.example'});self.assertEqual(r.status,403)
        r=await self.client.get('/healthz',headers={'Sec-Fetch-Site':'cross-site'});self.assertEqual(r.status,403)

    async def test_status_leaves_out_the_setting_catalog_the_client_already_holds(self):
        full=await (await self.client.get('/api/status',headers=self.headers)).json()
        self.assertIn('setting_details',full);self.assertIn('setting_choices',full);revision=full['settings_revision']
        r=await self.client.get('/api/status?since=0&settings_revision='+revision,headers=self.headers)
        lean=await r.json()
        self.assertNotIn('setting_details',lean);self.assertNotIn('setting_choices',lean);self.assertEqual(lean['settings_revision'],revision)
        self.assertEqual({k:v for k,v in full.items() if k not in {'setting_details','setting_choices','server_time','credentials'}},
                         {k:v for k,v in lean.items() if k not in {'server_time','credentials'}})
        self.b.setting_details['default_source_language']['choices']={'en':'English'}
        changed=await (await self.client.get('/api/status?settings_revision='+revision,headers=self.headers)).json()
        self.assertNotEqual(changed['settings_revision'],revision);self.assertIn('setting_details',changed)

    async def test_cloud_reads_are_cached_until_refresh_or_a_write_and_survive_restart(self):
        from test_note_favorites import UUID,detail
        b=self.b;b.note_refs={UUID:time.time()};b.channel._request=AsyncMock(return_value=detail(False))
        body={'task_uuid':UUID}
        read=lambda extra={}:self.client.post('/api/note-favorite-status',headers=self.headers,json={**body,**extra})
        first=await read();self.assertEqual(first.status,200);self.assertNotIn('X-Cached-At',first.headers)
        second=await read();self.assertIn('X-Cached-At',second.headers);self.assertEqual(await second.json(),await first.json())
        self.assertEqual(b.channel._request.await_count,1)
        await read({'refresh':True});self.assertEqual(b.channel._request.await_count,2)
        b.invalidate_after('note-favorite',body)  # what a favorite write does, success or not
        await read();self.assertEqual(b.channel._request.await_count,3)
        self.assertTrue(any('note:'+UUID in entry['tags'] for entry in Backend(b.state,18780).cache.entries.values()))

    async def test_listed_notes_survive_a_restart_through_the_saved_list(self):
        import json
        from test_note_favorites import UUID
        listing={'isError':False,'content':[{'type':'text','text':json.dumps({'items':[{'uuid':UUID}]})}]}
        self.b.cache.put('documents:'+json.dumps({'arguments':{'pageSize':20},'tool':'list_my_minutes'},sort_keys=True),listing,('notes-list',))
        restarted=Backend(self.b.state,18780)
        restarted.require_listed_note(UUID)  # no refusal: the saved list still names the note
        with self.assertRaises(Refused):restarted.require_listed_note('00000000-0000-4000-8000-000000000000')

    async def test_action_timings_count_outcomes_without_values(self):
        for _ in range(2):self.assertEqual((await self.client.post('/api/disarm',headers=self.headers,json={})).status,200)
        self.assertEqual((await self.client.post('/api/ready',headers=self.headers,json={})).status,409)
        self.assertEqual((await self.client.post('/api/no-such-action',headers=self.headers,json={})).status,404)
        timings=(await (await self.client.get('/api/status',headers=self.headers)).json())['action_timings']
        self.assertEqual(set(timings),{'disarm','ready'})
        self.assertEqual((timings['disarm']['count'],timings['disarm']['failures']),(2,0))
        self.assertEqual((timings['ready']['count'],timings['ready']['failures']),(1,1))
        self.assertEqual(set(timings['ready']),{'count','failures','last_ms','max_ms','total_ms'})

    async def test_status_since_query(self):
        for n in range(3):self.b.emit('device-event',{'n':n})
        r=await self.client.get('/api/status?since=2',headers=self.headers)
        self.assertEqual([e['id'] for e in (await r.json())['events']],[3])
        r=await self.client.get('/api/status?since=nope',headers=self.headers)
        self.assertEqual(len((await r.json())['events']),3)


if __name__=='__main__':unittest.main()
