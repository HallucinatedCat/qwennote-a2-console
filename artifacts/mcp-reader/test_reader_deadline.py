"""Bounded reader calls; all authorization and transport are synthetic."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import qwennote_reader as reader


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_noninteractive_discovery_has_total_deadline_and_cleans_up(self):
        entered=asyncio.Event();closed=asyncio.Event()
        async def stalled():
            entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        storage=MagicMock()
        storage.get_client_info=AsyncMock(return_value=None)
        storage.get_tokens=AsyncMock(return_value=object())
        args=SimpleNamespace(port=18765,login=False,command='tools',ready_timeout=60,login_timeout=60)
        with tempfile.TemporaryDirectory() as directory, patch.object(reader,'Storage',return_value=storage), \
             patch.object(reader,'discover',stalled),patch.object(reader,'REQUEST_TIMEOUT',0.02,create=True):
            task=asyncio.create_task(reader.connected(args,Path(directory)))
            await entered.wait()
            done,_=await asyncio.wait({task},timeout=0.2)
            if not done:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(done,'A stalled read must expire without caller intervention')
            with self.assertRaises(TimeoutError):task.result()
            self.assertTrue(closed.is_set())

    async def test_interactive_login_keeps_the_owner_wait_budget(self):
        async def network():
            await asyncio.sleep(0.04)
            raise RuntimeError('fixture-end')
        storage=MagicMock();storage.get_client_info=AsyncMock(return_value=None)
        callback=MagicMock();callback.completed=False
        args=SimpleNamespace(port=18765,login=True,command='tools',ready_timeout=60,login_timeout=60)
        with tempfile.TemporaryDirectory() as directory,patch.object(reader,'Storage',return_value=storage), \
             patch.object(reader,'Callback',return_value=callback),patch.object(reader,'discover',network), \
             patch.object(reader,'REQUEST_TIMEOUT',0.01,create=True):
            with self.assertRaisesRegex(RuntimeError,'fixture-end'):
                await reader.connected(args,Path(directory))
            callback.close.assert_called_once()

    async def test_external_cancellation_remains_cancellation(self):
        storage=MagicMock();storage.get_client_info=AsyncMock(return_value=None)
        storage.get_tokens=AsyncMock(return_value=object())
        entered=asyncio.Event()
        async def network():entered.set();await asyncio.Event().wait()
        args=SimpleNamespace(port=18765,login=False,command='tools',ready_timeout=60,login_timeout=60)
        with patch.object(reader,'Storage',return_value=storage),patch.object(reader,'discover',network):
            task=asyncio.create_task(reader.connected(args,Path('.')));await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task

    async def test_deadline_covers_stream_initialize_inventory_and_call(self):
        from contextlib import asynccontextmanager
        import httpx
        metadata={'authorization_server':{'issuer':reader.ISSUER,'authorization_endpoint':reader.ISSUER+'/authorize',
            'token_endpoint':reader.ISSUER+'/token','response_types_supported':['code']},
            'resource_metadata':{'resource':reader.SERVER,'authorization_servers':[reader.ISSUER]}}
        for phase in ('stream','initialize','list_tools','call_tool'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                entered=asyncio.Event();closed=[]
                async def stall():entered.set();await asyncio.Event().wait()
                @asynccontextmanager
                async def transport(*args,**kwargs):
                    try:
                        if phase=='stream':await stall()
                        yield (None,None,None)
                    finally:closed.append('transport')
                class Session:
                    def __init__(self,*_):pass
                    async def __aenter__(self):return self
                    async def __aexit__(self,*_):closed.append('session')
                    async def initialize(self):
                        if phase=='initialize':await stall()
                        return SimpleNamespace(model_dump=lambda **_: {})
                    async def list_tools(self,**_):
                        if phase=='list_tools':await stall()
                        return SimpleNamespace(nextCursor=None,tools=[SimpleNamespace(model_dump=lambda **_: {'name':'list_my_minutes'})])
                    async def call_tool(self,*_):await stall()
                storage=MagicMock();storage.get_client_info=AsyncMock(return_value=None);storage.get_tokens=AsyncMock(return_value=object())
                args=SimpleNamespace(port=18765,login=False,command='read',tool='list_my_minutes',arguments=None,ready_timeout=60,login_timeout=60)
                with patch.object(reader,'Storage',return_value=storage),patch.object(reader,'discover',AsyncMock(return_value=metadata)), \
                     patch.object(reader,'streamable_http_client',transport),patch.object(reader,'ClientSession',Session), \
                     patch.object(reader,'REQUEST_TIMEOUT',0.1):
                    with self.assertRaises(TimeoutError):await reader.connected(args,Path(directory))
                self.assertTrue(entered.is_set());self.assertIn('transport',closed)
                if phase!='stream':self.assertIn('session',closed)

    async def test_real_mcp_stream_cancellation_unwinds(self):
        from contextlib import asynccontextmanager
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import httpx,json,threading
        reached=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if request['method']=='initialize':
                    data=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2025-11-25',
                        'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}}).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
                    self.end_headers();self.wfile.write(data)
                elif request['method']=='tools/list':
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                    self.wfile.write(b': keepalive\n\n');self.wfile.flush();reached.set();release.wait(3)
                else:self.send_response(202);self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        original=reader.streamable_http_client;closed=[]
        @asynccontextmanager
        async def transport(*args,**kwargs):
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    async with original(f'http://127.0.0.1:{server.server_port}/mcp',http_client=client) as streams:yield streams
            finally:closed.append(True)
        metadata={'authorization_server':{'issuer':reader.ISSUER,'authorization_endpoint':reader.ISSUER+'/authorize',
            'token_endpoint':reader.ISSUER+'/token','response_types_supported':['code']},
            'resource_metadata':{'resource':reader.SERVER,'authorization_servers':[reader.ISSUER]}}
        storage=MagicMock();storage.get_client_info=AsyncMock(return_value=None);storage.get_tokens=AsyncMock(return_value=object())
        args=SimpleNamespace(port=18765,login=False,command='tools',ready_timeout=60,login_timeout=60)
        try:
            with patch.object(reader,'Storage',return_value=storage),patch.object(reader,'discover',AsyncMock(return_value=metadata)), \
                 patch.object(reader,'streamable_http_client',transport),patch.object(reader,'REQUEST_TIMEOUT',0.5):
                with self.assertRaises(TimeoutError):await reader.connected(args,Path('.'))
            self.assertTrue(reached.is_set());self.assertEqual(closed,[True])
        finally:
            release.set();await asyncio.to_thread(server.shutdown);server.server_close();thread.join(2)

    def test_total_deadline_failure_is_safe_and_actionable(self):
        text=reader.safe_failure(TimeoutError('secret-fixture'))
        self.assertIn('timed out',text.lower());self.assertNotIn('secret-fixture',text)


if __name__=='__main__':unittest.main()
