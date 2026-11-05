import asyncio
import contextlib
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from websockets.asyncio.server import serve
import lwp_client
from lwp_client import LWP,arguments,private_write,validate_session


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_read_push_ack_and_heartbeat(self):
        events=[]
        heartbeat=asyncio.Event()
        ack=asyncio.Event()
        async def server(socket):
            async for raw in socket:
                packet=json.loads(raw);events.append(packet)
                if 'code' in packet:
                    ack.set();continue
                route=packet['lwp']
                if route=='/reg':
                    self.assertEqual(packet['headers']['token'],'synthetic-token')
                    self.assertNotIn('body',packet)
                if route=='/!':heartbeat.set()
                if route.endswith('queryDeviceSettings'):
                    self.assertEqual(packet['body'][0]['accountId'],'123')
                    await socket.send(json.dumps({'lwp':'/push/example','headers':{'mid':'push1 0'},'body':{'event':'synthetic'}}))
                    # An unmatched reply must not satisfy the pending request.
                    await socket.send(json.dumps({'code':200,'headers':{'mid':'99999 0'},'body':{'wrong':True}}))
                await socket.send(json.dumps({'code':200,'headers':{'mid':packet['headers']['mid']},'body':{'settingList':[]}}))
        async with serve(server,'127.0.0.1',0) as service:
            port=service.sockets[0].getsockname()[1]
            async with LWP(f'ws://127.0.0.1:{port}',{'token':'synthetic-token'},timeout=.5,heartbeat_interval=.02,test_loopback=True) as client:
                reply=await client.read('settings',[{'accountId':'123'}])
                self.assertIn('settingList',reply['body'])
                await asyncio.wait_for(heartbeat.wait(),.5)
                await asyncio.wait_for(ack.wait(),.5)
                self.assertEqual(client.push_count,1)
        mids=[p['headers']['mid'] for p in events if 'lwp' in p]
        self.assertEqual(len(mids),len(set(mids)))

    async def test_registration_rejection_never_sends_read(self):
        routes=[]
        async def server(socket):
            async for raw in socket:
                packet=json.loads(raw);routes.append(packet.get('lwp'))
                await socket.send(json.dumps({'code':401,'headers':packet['headers']}))
        async with serve(server,'127.0.0.1',0) as service:
            port=service.sockets[0].getsockname()[1]
            with self.assertRaises(ValueError):
                async with LWP(f'ws://127.0.0.1:{port}',{},test_loopback=True):pass
        self.assertEqual(routes,['/reg'])

    async def test_timeout_does_not_retry(self):
        routes=[]
        async def server(socket):
            async for raw in socket:routes.append(json.loads(raw).get('lwp'))
        async with serve(server,'127.0.0.1',0) as service:
            port=service.sockets[0].getsockname()[1]
            with self.assertRaises(TimeoutError):
                async with LWP(f'ws://127.0.0.1:{port}',{},timeout=.03,test_loopback=True):pass
        self.assertEqual(routes,['/reg'])

    def test_session_requires_native_tls_vendor_endpoint(self):
        config=dict(endpoint='wss://minutes.qwennote.cn/example',app_key='synthetic',client_device_id='synthetic',user_agent='test',access_token='synthetic',account_id='123',account_type=2,target_device_id=456)
        self.assertEqual(validate_session(config),config)
        for endpoint in ['ws://minutes.qwennote.cn/example','wss://minutes.qwennote.cn.example.com/example','wss://token@minutes.qwennote.cn/example']:
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):validate_session({**config,'endpoint':endpoint})
        with self.assertRaises(ValueError):validate_session({**config,'expires_at':1})

    def test_only_read_tools_and_preserved_identifier_types(self):
        config={'account_id':'123','account_type':2,'target_device_id':456}
        self.assertIsInstance(arguments(config,'settings')[0]['deviceId'],int)
        self.assertIsInstance(arguments(config,'generation-config')[0]['deviceId'],str)
        with self.assertRaises(ValueError):arguments(config,'start')


class PrivateWriteTests(unittest.TestCase):
    def test_replaces_the_file_with_owner_only_mode_and_keeps_it_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';path.write_text('{"old":true}');path.chmod(0o644)
            private_write(path,{'new':True})
            self.assertEqual(json.loads(path.read_text()),{'new':True});self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(TypeError):private_write(path,{'unserializable':object()})
            self.assertEqual(json.loads(path.read_text()),{'new':True})
            self.assertEqual([p.name for p in Path(tmp).iterdir()],['state.json'])


class ReadCliTests(unittest.TestCase):
    def test_host_read_cli_still_renews_through_native_auth(self):
        self.addCleanup(logging.disable,logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/'private';private.mkdir(mode=0o700)
            session=private/'native-session.json';session.write_text('{}');session.chmod(0o600)
            with patch('native_auth.ensure_fresh',side_effect=RuntimeError('stopped before any network')) as fresh,\
                 patch('sys.argv',['lwp_client.py','settings','--session',str(session)]),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(lwp_client.main(),2)
            fresh.assert_called_once();self.assertIn('RuntimeError',out.getvalue())


if __name__=='__main__':unittest.main()
