"""Control transitions and assistant lifecycle. Synthetic/loopback only."""
import asyncio
import builtin_assistant
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestServer,TestClient
from web_backend import make_app
from public_errors import Busy,Refused,Unavailable
from test_backend_improvements import ready_backend


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.b=ready_backend(self.root);self.b.ready_until=time.time()+60
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_recorder_commands_need_an_open_recording(self):
        b=self.b
        with self.assertRaises(Refused):await b.record('pause')
        b.channel._request.assert_not_awaited()
        await b.record('start');await b.record('pause');await b.record('resume')
        b.ready_until=0  # Stop stays available after Disarm.
        await b.record('stop');self.assertTrue(b.own_test)  # until a device check or the user closes it
        await b.confirm_stopped({'physically_stopped':True});self.assertFalse(b.own_test)

    async def test_overlapping_control_fails_fast(self):
        b=self.b
        async with b.mutation_lock:
            with self.assertRaises(Busy):await asyncio.wait_for(b.record('start'),0.1)
        b.channel._request.assert_not_awaited();self.assertFalse(b.own_test)

    async def test_cancelled_record_retains_unknown_outcome_and_ownership(self):
        b=self.b;entered=asyncio.Event()
        async def request(*args,**kwargs):entered.set();await asyncio.Event().wait()
        b.channel._request.side_effect=request
        task=asyncio.create_task(b.record('start'));await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(b.record_state['phase'],'unknown');self.assertTrue(b.own_test)
        self.assertFalse(b.mutation_lock.locked())


class AssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.b=ready_backend(self.root)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_overlap_cancel_and_restart_blocker(self):
        entered=asyncio.Event();finished=asyncio.Event()
        async def work(text,*args):
            entered.set()
            try:await asyncio.Event().wait()
            finally:finished.set()
        b=self.b
        with patch.object(b,'_assistant_request',side_effect=work) as send:
            task=asyncio.create_task(b.assistant('synthetic prompt','request-one'))
            await asyncio.wait_for(entered.wait(),1)
            self.assertTrue(b.status()['assistant_in_progress']);self.assertFalse(b.health()['restart_safe'])
            self.assertIn('assistant-request-in-progress',b.health()['blocking'])
            with self.assertRaises(Busy):await b.assistant('another prompt','request-two')
            with self.assertRaises(Refused):b.cancel_assistant('request-two')
            self.assertFalse(task.done());b.cancel_assistant('request-one')
            result=await task
            self.assertTrue(result['cancelled']);self.assertEqual(result['provider_outcome'],'unknown')
            self.assertTrue(finished.is_set());self.assertFalse(b.status()['assistant_in_progress'])
            self.assertTrue(b.health()['restart_safe']);self.assertEqual(send.call_count,1)
        with patch.object(b,'_assistant_request',AsyncMock(return_value={'text':'second reply'})):
            self.assertEqual((await b.assistant('retry explicitly','request-three'))['text'],'second reply')

    async def test_cancel_route_requires_csrf_and_matching_handle(self):
        entered=asyncio.Event()
        async def work(text,*args):entered.set();await asyncio.Event().wait()
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock()
        client=TestClient(TestServer(make_app(b)));await client.start_server();b.port=client.server.port
        self.addAsyncCleanup(client.close)
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        with patch.object(b,'_assistant_request',side_effect=work):
            task=asyncio.create_task(client.post('/api/assistant',headers=headers,json={'text':'synthetic','request_id':'request-one'}))
            await asyncio.wait_for(entered.wait(),1)
            denied=await client.post('/api/assistant-cancel',json={'request_id':'request-one'})
            self.assertEqual(denied.status,403)
            stale=await client.post('/api/assistant-cancel',headers=headers,json={'request_id':'stale'})
            self.assertEqual(stale.status,409);self.assertFalse(task.done())
            cancelled=await client.post('/api/assistant-cancel',headers=headers,json={'request_id':'request-one'})
            self.assertTrue((await cancelled.json())['cancel_requested'])
            reply=await asyncio.wait_for(task,1)
            self.assertTrue((await reply.json())['cancelled'])
        self.assertTrue(b.health()['restart_safe'])

    async def test_history_recovery_and_new_conversation_routes_require_csrf(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock()
        client=TestClient(TestServer(make_app(b)));await client.start_server();b.port=client.server.port
        self.addAsyncCleanup(client.close);headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        with patch.object(b,'assistant_history',new=AsyncMock(return_value={'messages':[]})), patch.object(b,'assistant_recover',new=AsyncMock(return_value={'text':'recovered'})), patch.object(b,'new_assistant_conversation',return_value={'new_conversation':True}):
            for action in ('assistant-history','assistant-recover','assistant-new'):
                with self.subTest(action=action):
                    denied=await client.post('/api/'+action,json={});self.assertEqual(denied.status,403)
                    invalid=await client.post('/api/'+action,headers=headers,json={'unexpected':True});self.assertEqual(invalid.status,409)
                    accepted=await client.post('/api/'+action,headers=headers,json={});self.assertEqual(accepted.status,200)

    async def test_conversation_list_and_restore_routes_require_csrf_and_bounded_input(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock()
        client=TestClient(TestServer(make_app(b)));await client.start_server();b.port=client.server.port
        self.addAsyncCleanup(client.close);headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        with patch.object(b,'assistant_conversations',new=AsyncMock(return_value={'conversations':[]})), patch.object(b,'select_assistant_conversation',new=AsyncMock(return_value={'restored':True})):
            denied=await client.post('/api/assistant-conversations',json={});self.assertEqual(denied.status,403)
            invalid=await client.post('/api/assistant-conversations',headers=headers,json={'unexpected':True});self.assertEqual(invalid.status,409)
            accepted=await client.post('/api/assistant-conversations',headers=headers,json={});self.assertEqual(accepted.status,200)
            denied=await client.post('/api/assistant-select-conversation',json={'handle':'opaque'});self.assertEqual(denied.status,403)
            for body in ({},{'handle':'opaque','unexpected':True}):
                invalid=await client.post('/api/assistant-select-conversation',headers=headers,json=body);self.assertEqual(invalid.status,409)
            accepted=await client.post('/api/assistant-select-conversation',headers=headers,json={'handle':'opaque'});self.assertEqual(accepted.status,200)

    async def test_external_cancellation_is_preserved(self):
        entered=asyncio.Event()
        async def work(text,*args):entered.set();await asyncio.Event().wait()
        with patch.object(self.b,'_assistant_request',side_effect=work):
            task=asyncio.create_task(self.b.assistant('synthetic','request-one'))
            await asyncio.wait_for(entered.wait(),1);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.b.status()['assistant_in_progress'])

    async def test_failure_releases_busy_state(self):
        with patch.object(self.b,'_assistant_request',AsyncMock(side_effect=Unavailable('synthetic'))):
            with self.assertRaises(Unavailable):await self.b.assistant('synthetic','request-one')
        self.assertFalse(self.b.status()['assistant_in_progress']);self.assertTrue(self.b.health()['restart_safe'])

    async def test_blank_prompt_and_bad_request_id_do_not_send(self):
        with patch.object(self.b,'_assistant_request',AsyncMock()) as send:
            for text,request_id in [('   ','valid'),('hello','bad\n'),('hello',[]),('x'*32001,'valid')]:
                with self.assertRaises(Refused):await self.b.assistant(text,request_id)
            send.assert_not_awaited()

    def prepare_builtin(self):
        b=self.b;b.assistant_profile={'uid':123,'nick':'Owner','origin':builtin_assistant.DEFAULT_ORIGIN}
        b.assistant_credential='synthetic-token';b.assistant_credential_expires=time.time()+3600
        return b

    async def test_builtin_session_continues_until_explicit_new_conversation(self):
        b=self.prepare_builtin();sessions=[]
        async def chat(origin,credential,path,body,user_agent,event_sink=None):
            sessions.append(body['session_id'])
            if event_sink:event_sink({'success':True,'task_id':'private-task'})
            return 'synthetic answer'
        with patch('builtin_assistant.chat',side_effect=chat):
            await b.assistant('first','request-one');await b.assistant('second','request-two')
            self.assertEqual(sessions[0],sessions[1])
            b.new_assistant_conversation();await b.assistant('third','request-three')
        self.assertNotEqual(sessions[0],sessions[2]);self.assertEqual(len(sessions),3)

    async def test_vendor_conversation_list_uses_opaque_handles_and_restores_history(self):
        b=self.prepare_builtin();first='123e4567-e89b-12d3-a456-426614174000';second='123e4567-e89b-12d3-a456-426614174001'
        rows=[{'session_id':first,'title':'First synthetic','date':'20261104'},{'session_id':second,'title':'Second synthetic','date':'20261103'}]
        with patch('builtin_assistant.conversations',new=AsyncMock(return_value=rows)):
            listing=await b.assistant_conversations()
        self.assertEqual([(row['title'],row['date']) for row in listing['conversations']],[('First synthetic','20261104'),('Second synthetic','20261103')])
        self.assertNotIn(first,json.dumps(listing));self.assertNotIn(second,json.dumps(listing))
        handle=listing['conversations'][0]['handle']
        history=[{'query':'restored prompt','content':'restored answer','status':'Finished','task_id':'private-task'}]
        with patch('builtin_assistant.history',new=AsyncMock(return_value=history)) as read:
            restored=await b.select_assistant_conversation(handle)
        self.assertEqual(b.assistant_session_id,first);self.assertTrue(restored['restored']);self.assertEqual(restored['text'],'restored answer')
        self.assertNotIn(first,json.dumps(restored));self.assertNotIn('private-task',json.dumps(restored));read.assert_awaited_once()
        with self.assertRaises(Refused):await b.select_assistant_conversation('stale-handle')

    async def test_history_returns_text_without_upstream_identifiers(self):
        b=self.prepare_builtin();b.assistant_session_id='123e4567-e89b-12d3-a456-426614174000'
        rows=[{'query':'synthetic prompt','content':'synthetic answer','status':'Finished','task_id':'private-task'}]
        with patch('builtin_assistant.history',new=AsyncMock(return_value=rows)) as history:
            result=await b.assistant_history()
        self.assertEqual(result['messages'],[{'query':'synthetic prompt','text':'synthetic answer','complete':True}])
        serialized=json.dumps(result)
        for value in ('private-task',b.assistant_session_id,'synthetic-token'):self.assertNotIn(value,serialized)
        history.assert_awaited_once()

    async def test_cancelled_builtin_stream_offers_existing_task_recovery(self):
        b=self.prepare_builtin();started=asyncio.Event()
        async def interrupted(origin,credential,path,body,user_agent,event_sink=None):
            event_sink({'success':True,'task_id':'private-task'});started.set();await asyncio.Event().wait()
        with patch('builtin_assistant.chat',side_effect=interrupted):
            task=asyncio.create_task(b.assistant('synthetic long request','request-one'))
            await asyncio.wait_for(started.wait(),1);b.cancel_assistant('request-one');result=await task
        self.assertTrue(result['cancelled']);self.assertTrue(b.status()['assistant_recovery_available'])

    async def test_interrupted_stream_recovers_existing_task_without_prompt_replay(self):
        b=self.prepare_builtin()
        async def interrupted(origin,credential,path,body,user_agent,event_sink=None):
            event_sink({'success':True,'task_id':'private-task'})
            raise builtin_assistant.AssistantError('synthetic disconnect')
        with patch('builtin_assistant.chat',side_effect=interrupted):
            with self.assertRaises(Unavailable):await b.assistant('do not replay this','request-one')
        self.assertTrue(b.status()['assistant_recovery_available'])
        recovered={'query':'do not replay this','content':'recovered answer','status':'Finished','task_id':'private-task'}
        with patch('builtin_assistant.recover',new=AsyncMock(return_value=recovered)) as recover:
            result=await b.assistant_recover()
        self.assertEqual(result['text'],'recovered answer');self.assertTrue(result['recovered'])
        self.assertFalse(b.status()['assistant_recovery_available'])
        self.assertNotIn('do not replay this',str(recover.await_args))

    async def configure(self,handler):
        app=web.Application();app.router.add_post('/chat',handler)
        server=TestServer(app);await server.start_server();self.addAsyncCleanup(server.close)
        path=self.root/'assistant.json'
        path.write_text(json.dumps({'url':str(server.make_url('/chat')),'model':'synthetic','api_key':'synthetic-secret'}))
        path.chmod(0o600)

    async def test_chunked_response_is_read_to_eof_and_prompt_is_not_logged(self):
        calls=[]
        async def handler(request):
            calls.append(await request.json())
            response=web.StreamResponse(headers={'Content-Type':'application/json'});await response.prepare(request)
            await response.write(b'{"choices":[');await asyncio.sleep(0.02)
            await response.write(b'{"message":{"content":"synthetic reply"}}]}');await response.write_eof();return response
        await self.configure(handler)
        self.assertEqual((await self.b.assistant('synthetic prompt',provider='custom'))['text'],'synthetic reply')
        self.assertEqual(len(calls),1);self.assertFalse(calls[0]['stream'])
        status=json.dumps(self.b.status())
        for value in ('synthetic prompt','synthetic reply','synthetic-secret'):self.assertNotIn(value,status)

    async def test_malformed_response_is_public_and_never_retried(self):
        calls=[]
        async def handler(request):calls.append(1);return web.json_response({'choices':[{'message':{'content':None}}]})
        await self.configure(handler)
        with self.assertRaises(Unavailable):await self.b.assistant('synthetic',provider='custom')
        self.assertEqual(len(calls),1)

    async def test_oversized_response_is_bounded(self):
        async def handler(request):return web.Response(body=b' '*(1024*1024+1))
        await self.configure(handler)
        with self.assertRaises(Refused):await self.b.assistant('synthetic',provider='custom')
        self.assertFalse(self.b.status()['assistant_in_progress'])


if __name__=='__main__':unittest.main()
