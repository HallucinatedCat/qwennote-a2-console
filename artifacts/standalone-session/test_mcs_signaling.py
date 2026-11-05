import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mcs_signaling import MCS,token_info,login_payload,login_identity,TLS_NAME


def token(**kwargs):
    return base64.b64encode(json.dumps({'secure':True,'host':'mcs.dingtalk.com','room':'fixture-room',**kwargs}).encode()).decode()


class TokenTests(unittest.TestCase):
    def test_refuses_unsafe_authorities_and_insecure_tokens(self):
        for host in ['127.0.0.1','169.254.169.254','10.0.0.1','[::1]','example.com','mcs.dingtalk.com.evil.example','mcs.dingtalk.com@evil.example','mcs.dingtalk.com:80','mcs.dingtalk.com/path','mcs.dingtalk.com?token=foo']:
            with self.subTest(host=host),self.assertRaises(ValueError):token_info(token(host=host))
        with self.assertRaises(ValueError):token_info(token(secure=False))
        with self.assertRaises(ValueError):token_info('invalid')
        self.assertTrue(token_info(token())[1].endswith('EIO=3&transport=websocket'))

    def test_host_rules_are_pinned_and_each_refusal_names_its_reason(self):
        # Pinned current behaviour: the vendor name, its subdomains and public IP literals are
        # accepted; TLS still checks the mcs.dingtalk.com certificate (server_hostname). The
        # official app accepts any host and skips TLS checks, so it does not settle IP literals.
        for host in ['mcs.dingtalk.com','edge.mcs.dingtalk.com','mcs.dingtalk.com:443','1.1.1.1','[2606:4700:4700::1111]']:
            with self.subTest(accepted=host):
                self.assertEqual(token_info(token(host=host))[1],'wss://'+host+'/socket.io/?EIO=3&transport=websocket')
        for host,reason in [('10.0.0.1','Nonpublic signalling address'),('127.0.0.1','Nonpublic signalling address'),
                            ('[::1]','Nonpublic signalling address'),('169.254.169.254','Nonpublic signalling address'),
                            ('example.com','Unsupported signalling host'),('mcs.dingtalk.com.evil.example','Unsupported signalling host'),
                            ('mcs.dingtalk.com:8443','Unsupported signalling authority')]:
            with self.subTest(refused=host):
                with self.assertRaises(ValueError) as raised:token_info(token(host=host))
                self.assertEqual(str(raised.exception),reason)

    def test_login_payload_wraps_credentials_and_uses_ntp_milliseconds(self):
        value=token();body=login_payload(value,clock=0)
        self.assertEqual(body['data']['token'],value)
        self.assertEqual(body['data']['clientSendTime'],2208988800000)
        self.assertIn('trace-id',body);self.assertIn('tid',body)
        self.assertEqual(body['data']['protocol'],'1.0')

    def test_identity_rejects_other_room_and_missing_participant(self):
        info={'room':'fixture-room'}
        self.assertEqual(login_identity(info,['ok',{'id':'participant','room':{'id':'fixture-room'}}]),'participant')
        for ack in [['error',{}],['ok',{'id':'participant','room':{'id':'other'}}],['ok',{'room':{'id':'fixture-room'}}]]:
            with self.assertRaises(ValueError):login_identity(info,ack)


class FakeSocket:
    def __init__(self):
        self.frames=asyncio.Queue();self.sent=[];self.closed=False
        self.frames.put_nowait('0'+json.dumps({'sid':'fixture','pingInterval':1000}))
        self.frames.put_nowait('40')
    async def recv(self):return await self.frames.get()
    async def send(self,frame):
        self.sent.append(frame)
        if frame.startswith('421['):
            self.frames.put_nowait('431'+json.dumps(['ok',{'id':'participant','room':{'id':'fixture-room'}}]))
            self.frames.put_nowait('427'+json.dumps(['stream',{'tid':'server-event'}]))
            self.frames.put_nowait('428'+json.dumps(['private-unrecognized-event',{}]))
        if frame=='2':self.frames.put_nowait('3')
    async def close(self):self.closed=True


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_acks_heartbeat_and_private_trace(self):
        sock=FakeSocket()
        async def connect(uri,**kwargs):
            self.assertEqual(kwargs['server_hostname'],TLS_NAME)
            self.assertTrue(kwargs['ssl'].check_hostname)
            self.assertEqual(kwargs['ssl'].verify_mode,2)
            self.assertNotIn('token',uri)
            return sock
        with tempfile.TemporaryDirectory() as directory:
            mcs=MCS(Path(directory))
            with patch('mcs_signaling.websockets.connect',connect):
                await mcs.open(token())
                await asyncio.sleep(1.05)
                await mcs.close()
            self.assertTrue(sock.closed);self.assertEqual(mcs.participant,'participant')
            self.assertIn('437["ok","{\\"tid\\": \\"server-event\\"}"]',sock.sent)
            self.assertIn('438["ok"]',sock.sent)
            summary=mcs.summary()
            self.assertEqual(summary['event_counts'],{'stream':1,'other':1})
            self.assertGreaterEqual(summary['pongs_received'],1)
            self.assertNotIn('participant',json.dumps(summary).replace('participant_id_present',''))
            self.assertEqual((Path(directory)/'mcs-frames.jsonl').stat().st_mode&0o777,0o600)

    async def test_failed_handshake_is_closable(self):
        sock=FakeSocket();sock.frames=asyncio.Queue();sock.frames.put_nowait('bad')
        async def connect(*args,**kwargs):return sock
        with tempfile.TemporaryDirectory() as directory:
            mcs=MCS(Path(directory))
            with patch('mcs_signaling.websockets.connect',connect),self.assertRaises(ValueError):await mcs.open(token())
            await mcs.close();self.assertTrue(sock.closed)
            self.assertFalse(mcs.summary()['authenticated'])

if __name__=='__main__':unittest.main()
