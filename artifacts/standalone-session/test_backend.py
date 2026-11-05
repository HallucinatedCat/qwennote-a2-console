import asyncio
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
from urllib.parse import parse_qs,urlsplit
import urllib.request
from aiohttp.test_utils import TestClient,TestServer
import native_auth
from native_auth import client_token,parse_renewal
from device_protocol import record_command,setting_command,decode_push,public_device,owner_device
from public_errors import GENERIC_MESSAGE
from web_backend import Backend,make_app

CONFIG=dict(endpoint='wss://minutes.qwennote.cn/example',app_key='test',client_device_id='test',user_agent='test',access_token='test',account_id='123',account_type=2,target_device_id=456,native_client_id='test-client',native_client_secret='synthetic-secret')
BUILD_OPENER=urllib.request.build_opener  # the tests patch the shared module attribute
RENEWABLE={**CONFIG,'native_auth_origin':'https://minutes.qwennote.cn','refresh_token':'synthetic-refresh','expires_at':time.time()+30}


def renewal_body(**extra):
    return json.dumps({'success':True,'data':{'accountId':'123','accessToken':'new-access','refreshToken':'new-refresh','accessTokenExpireAt':(time.time()+3600)*1000},**extra}).encode()


class AuthServer:
    """Loopback stand-in for the renewal origin. `reply` is (status, headers, body) for the
    silent-login path; any other path answers with a valid renewal, so following a redirect
    or ignoring a limit would turn a refusal into a success."""
    def __init__(self,reply):
        self.requests=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                owner.requests.append({'method':self.command,'path':self.path,'headers':dict(self.headers)})
                status,headers,body=reply if urlsplit(self.path).path=='/api/login/silent' else (200,{},renewal_body())
                self.send_response(status)
                for key,value in headers.items():self.send_header(key,value)
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            do_GET=do_POST=answer
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.base=f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def opener(self,*handlers):
        """build_opener replacement: keep renew()'s handlers, send the same request to loopback."""
        built=BUILD_OPENER(urllib.request.ProxyHandler({}),*handlers);send=built.open
        def open_local(request,timeout=None):
            url=urlsplit(request.full_url)
            local=urllib.request.Request(self.base+url.path+'?'+url.query,data=request.data,headers=dict(request.header_items()),method=request.get_method())
            return send(local,timeout=timeout)
        built.open=open_local
        return built

    def close(self):self.server.shutdown();self.server.server_close()

class AuthProtocolTests(unittest.TestCase):
    def test_native_signature_and_time(self):
        header,payload,sig=client_token(CONFIG,100).split('.')
        claims=json.loads(base64.urlsafe_b64decode(payload+'=='))
        self.assertEqual(claims,{'clientId':'test-client','iat':100,'exp':3700})
        expected=hmac.new(b'synthetic-secret',(header+'.'+payload).encode(),hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(sig+'=='),expected)

    def test_renewal_rejects_identity_endpoint_and_expiry(self):
        response={'success':True,'data':{'accountId':'123','accessToken':'new','refreshToken':'new-refresh','accessTokenExpireAt':(time.time()+60)*1000}}
        updated=parse_renewal(CONFIG,response)
        self.assertEqual(updated['access_token'],'new')
        for change in [{'accountId':'other'},{'metaLinkEndpoint':'wss://evil.example/x'},{'accessTokenExpireAt':1},{'refreshToken':''}]:
            with self.subTest(change=change),self.assertRaises(ValueError):parse_renewal(CONFIG,{'success':True,'data':{**response['data'],**change}})

    def renew_against(self,reply):
        server=AuthServer(reply);self.addCleanup(server.close)
        with patch('native_auth.urllib.request.build_opener',side_effect=server.opener):
            try:return server,native_auth.renew(RENEWABLE),None
            except RuntimeError as error:return server,None,error

    def test_renewal_posts_signed_refresh_and_returns_rotated_session(self):
        server,(updated,evidence),_=self.renew_against((200,{'Content-Type':'application/json'},renewal_body()))
        self.assertEqual((updated['access_token'],updated['refresh_token']),('new-access','new-refresh'))
        self.assertEqual(evidence,{'http_status':200,'account_match':True,'access_token_changed':True,'refresh_token_changed':True,'expiry_in_future':True})
        [request]=server.requests;url=urlsplit(request['path'])
        self.assertEqual((request['method'],url.path),('POST','/api/login/silent'));self.assertEqual(parse_qs(url.query),{'refreshToken':['synthetic-refresh']})
        token=request['headers']['X-Client-Token'];issued=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'=='))['iat']
        self.assertEqual(token,client_token(RENEWABLE,issued))

    def test_renewal_refuses_redirects_and_oversize_replies_without_detail(self):
        redirect=(302,{'Location':'/elsewhere'},b'')
        # Valid JSON within the first MiB, so only the size check can refuse it.
        oversize=(200,{'Content-Type':'application/json'},renewal_body()+b' '*(1024*1024))
        for reply in (redirect,oversize):
            with self.subTest(status=reply[0]):
                server,result,error=self.renew_against(reply)
                self.assertIsNone(result)
                self.assertEqual(str(error),'Native renewal transport failed');self.assertTrue(error.__suppress_context__)
                self.assertEqual([urlsplit(r['path']).path for r in server.requests],['/api/login/silent'])

    def test_renewal_refuses_unlisted_origin_or_missing_token_before_any_request(self):
        opener=Mock()
        with patch('native_auth.urllib.request.build_opener',opener):
            for change in ({'native_auth_origin':'https://evil.example'},{'native_auth_origin':None},{'refresh_token':''}):
                with self.subTest(change=change),self.assertRaises(ValueError):native_auth.renew({**RENEWABLE,**change})
        opener.assert_not_called()

    def test_ensure_fresh_skips_fresh_sessions_and_saves_renewals_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'native-session.json'
            fresh={**RENEWABLE,'expires_at':time.time()+3600}
            with patch('native_auth.renew',side_effect=AssertionError('renewed a fresh session')):
                self.assertEqual(native_auth.ensure_fresh(path,fresh),(fresh,None))
            self.assertFalse(path.exists())
            rotated={**RENEWABLE,'access_token':'rotated','expires_at':time.time()+3600}
            with patch('native_auth.renew',return_value=(rotated,{'http_status':200})):
                self.assertEqual(native_auth.ensure_fresh(path,RENEWABLE),(rotated,{'http_status':200}))
            self.assertEqual(json.loads(path.read_text()),rotated);self.assertEqual(path.stat().st_mode&0o777,0o600)
            with patch('native_auth.renew',side_effect=RuntimeError('Native renewal transport failed')),self.assertRaises(RuntimeError):
                native_auth.ensure_fresh(path,RENEWABLE)
            self.assertEqual(json.loads(path.read_text()),rotated)

    def test_commands_exclude_destructive_actions(self):
        for action in ('start','stop','pause','resume'):
            body=record_command(CONFIG,action,'stable-id')[0]
            self.assertEqual(body['taskType'],0);self.assertEqual(body['requestId'],'stable-id')
        for action in ('reset','unbind','upgrade'):
            with self.assertRaises(ValueError):record_command(CONFIG,action)
        with self.assertRaises(ValueError):setting_command(CONFIG,'device_org_id','new')
        with self.assertRaises(ValueError):setting_command(CONFIG,'enable_rtc_translate','99')

    def test_live_push_envelope_and_invalid_payloads(self):
        data={'sequence':3,'content':'synthetic transcript'}
        row={'objectType':770001,'streamId':'dtiot.dinger','data':base64.b64encode(json.dumps(data).encode()).decode()}
        packet={'lwp':'/s/para','body':{'syncPushPackage':{'data':[row]}}}
        self.assertEqual(decode_push(packet)[0]['payload'],data)
        for changed in [{'data':'!!'},{'objectType':999},{'streamId':'another.stream'}]:
            self.assertEqual(decode_push({'lwp':'/s/para','body':{'syncPushPackage':{'data':[{**row,**changed}]}}}),[])

    def test_owner_identity_is_separate_from_public_status_projection(self):
        row={'deviceNick':'Office recorder','sn':'synthetic-serial','firmwareVersion':'1.2.3-test','deviceSecret':'DO-NOT-EXPOSE'}
        result=owner_device(row)
        self.assertEqual(result,{'name':'Office recorder','sn':'synthetic-serial','firmware_version':'1.2.3-test','firmware_source':'bound-device cloud response','battery_percent':None})
        props={'deviceProperties':[{'propertyId':'firmware_version','propertyName':'2.0.1'},{'propertyId':'device_battery','propertyName':'64'}]}
        self.assertEqual(owner_device(props)['firmware_version'],'2.0.1');self.assertEqual(public_device(props)['battery_percent'],64)
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(result));self.assertNotIn('sn',public_device(row))
        self.assertIsNone(owner_device({'sn':'synthetic-serial'})['firmware_version'])
        for bad in [{'sn':'x'*257},{'deviceNick':'bad\x00'},{'firmwareVersion':1}]:
            with self.assertRaises(ValueError):owner_device(bad)

        result=public_device({'onlineStatus':1,'deviceSecret':'DO-NOT-EXPOSE','sn':'private'})
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(result));self.assertNotIn('sn',result)

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.backend=Backend(self.root,18780)
        self.backend.config=CONFIG
        self.backend.device={'model':'QwenNote A2'}
        self.backend.writable_keys={'default_source_language'}
        self.backend.setting_details={'default_source_language':{'editable':True,'inherited':False,'choices':{'en':'English','zh':'Chinese'}}}
        self.backend.channel=type('Channel',(),{'ready':True,'_request':AsyncMock(return_value={'code':200}),
                                                'read':AsyncMock(return_value={'body':{'deviceModel':[{'deviceId':456,'devServId':1446,'onlineStatus':1}]}})})()

    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_start_refuses_an_unprotected_session_file_before_locking(self):
        session=self.root/'native-session.json';session.write_text(json.dumps(CONFIG));session.chmod(0o644)
        with self.assertRaises(ValueError):await self.backend.start(None)
        session.unlink();target=self.root.parent/(self.root.name+'-elsewhere.json')
        target.write_text(json.dumps(CONFIG));target.chmod(0o600);self.addCleanup(target.unlink)
        session.symlink_to(target)
        with self.assertRaises(ValueError):await self.backend.start(None)
        self.assertFalse((self.root/'native-session.lock').exists());self.assertIsNone(self.backend.task)

    async def test_stop_cannot_touch_unowned_recording(self):
        self.backend.ready_until=time.time()+60
        with self.assertRaises(ValueError):await self.backend.record('stop')
        self.backend.channel._request.assert_not_awaited()

    async def test_ambiguous_start_not_retried_and_cleanup_after_lease(self):
        b=self.backend;b.ready_until=time.time()+60
        b.channel._request.side_effect=TimeoutError
        with self.assertRaises(RuntimeError):await b.record('start')
        self.assertEqual(b.channel._request.await_count,1)
        self.assertEqual(b.record_phase,'outcome-unknown')
        b.ready_until=0;b.channel._request.side_effect=None
        result=await b.record('stop')
        self.assertTrue(result['cloud_acknowledged'])
        self.assertEqual(result['physical_result'],'unverified')

    async def test_open_test_survives_backend_restart(self):
        b=self.backend;b.ready_until=time.time()+60
        await b.record('start')
        restarted=Backend(self.root,18780)
        self.assertTrue(restarted.own_test)
        self.assertEqual(restarted.ready_until,0)

    async def test_inherited_defaults_cannot_leave_new_overrides(self):
        b=self.backend;b.ready_until=time.time()+60;b.refresh=AsyncMock()
        b.settings={'default_source_language':'en'};b.writable_keys=set()
        b.setting_details['default_source_language']['inherited']=True
        with self.assertRaises(ValueError):await b.change_setting('default_source_language','zh')
        b.channel._request.assert_not_awaited()

    async def test_setting_original_saved_before_timeout(self):
        b=self.backend;b.ready_until=time.time()+60
        b.settings={'default_source_language':'en'};b.refresh=AsyncMock()
        b.channel._request.side_effect=TimeoutError
        with self.assertRaises(RuntimeError):await b.change_setting('default_source_language','zh')
        saved=json.loads(b.setting_restore_path.read_text())
        self.assertEqual(saved['default_source_language']['original'],'en')
        self.assertEqual(b.channel._request.await_count,1)

    async def test_restore_preserves_external_changes(self):
        b=self.backend;b.ready_until=time.time()+60;b.refresh=AsyncMock()
        b.settings={'default_source_language':'ja'}
        b.restores={'default_source_language':{'original':'en','requested':'zh'}}
        with self.assertRaises(ValueError):await b.change_setting('default_source_language',None,restore=True)
        b.channel._request.assert_not_awaited()

    async def test_private_push_dedup_does_not_leak_record_id(self):
        event={'type':770001,'payload':{'recordId':456,'taskId':8,'sequence':1,'textType':'ParagraphEnd','eventEnum':4,'content':json.dumps({'stableResult':[{'index':0,'result':'hello'}]})}}
        b=self.backend;b.process_event(event);b.process_event(event)
        self.assertEqual(len(b.events),1)
        self.assertNotIn('recordId',b.events[0]['data'])

    async def test_builtin_assistant_uses_native_identity_and_cached_token(self):
        b=self.backend;b.connection='connected'
        b.channel._request.side_effect=[
            {'body':{'context':json.dumps({'uid':'123','nickName':'Owner'})}},
            {'body':{'token':'private-ai-token','expireTime':(time.time()+3600)*1000}},
        ]
        with patch('builtin_assistant.chat',new=AsyncMock(return_value='Synthetic answer')) as chat:
            result=await b.assistant('Question','request-1')
            self.assertEqual(result['text'],'Synthetic answer');self.assertEqual(result['provider'],'builtin')
            self.assertNotIn('private-ai-token',json.dumps(result))
            self.assertEqual(chat.await_args.args[2],'ai/tingji/v1/chat')
            self.assertEqual(chat.await_args.args[3]['condition'],{'uuids':[]})
            await b.assistant('Another','request-2')
        self.assertEqual(b.channel._request.await_count,2)
        self.assertEqual([call.args[0] for call in b.channel._request.await_args_list],
                         ['/r/Adaptor/MinutesAppI/getUserInfo','/r/Adaptor/MinutesTokenI/generateDingUserIdentifierInfoToken'])

    async def test_assistant_rejects_unknown_provider_before_native_calls(self):
        with self.assertRaises(ValueError):await self.backend.assistant('Question',provider='unknown')
        self.backend.channel._request.assert_not_awaited()

class WebAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();b=self.backend=Backend(Path(self.tmp.name),1)
        b.start=AsyncMock();b.stop=AsyncMock()
        self.client=TestClient(TestServer(make_app(b)))
        await self.client.start_server();b.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}

    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()

    async def test_device_identity_requires_owner_auth_and_makes_no_cloud_call(self):
        self.backend.device_record={'deviceNick':'Office recorder','sn':'synthetic-serial','deviceSecret':'DO-NOT-EXPOSE'}
        self.backend.channel=type('Channel',(),{'_request':AsyncMock()})()
        self.backend.last_refresh_at=1000
        self.assertEqual((await self.client.post('/api/device-identity',json={})).status,403)
        r=await self.client.post('/api/device-identity',json={},headers=self.headers);self.assertEqual(r.status,200)
        result=await r.json();self.assertEqual(result['sn'],'synthetic-serial');self.assertIsNone(result['firmware_version'])
        self.assertEqual(result['read_at'],1000);self.assertNotIn('DO-NOT-EXPOSE',json.dumps(result))
        self.backend.channel._request.assert_not_awaited();self.assertEqual(self.backend.ready_until,0)
        status=await (await self.client.get('/api/status',headers=self.headers)).json();self.assertNotIn('synthetic-serial',json.dumps(status))
        self.assertEqual((await self.client.post('/api/device-identity',json={'deviceId':1},headers=self.headers)).status,409)
    async def test_landing_top_navigation_and_no_framing(self):
        r=await self.client.get('/',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})
        self.assertEqual(r.status,200);self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        r=await self.client.get('/',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'no-cors','Sec-Fetch-Dest':'image'})
        self.assertEqual(r.status,403)

    async def test_foreign_host_and_origin_rejected(self):
        for headers in [{'Host':'evil.example'},{**self.headers,'Origin':'https://evil.example'}]:
            r=await self.client.get('/api/status',headers=headers);self.assertEqual(r.status,403)

    async def test_cookie_and_csrf_required(self):
        r=await self.client.get('/api/status');self.assertEqual(r.status,403)
        r=await self.client.post('/api/ready',headers={'Cookie':'qn_ui='+self.backend.cookie},json={});self.assertEqual(r.status,403)
        r=await self.client.post('/api/ready',headers=self.headers,json={});self.assertEqual(r.status,409)
        self.assertEqual(self.backend.ready_until,0)

    async def test_readiness_requires_all_three_confirmations(self):
        r=await self.client.post('/api/ready',headers=self.headers,json={'physical_ready':True,'disposable_only':True,'device_idle':False})
        self.assertEqual(r.status,409)
        r=await self.client.post('/api/ready',headers=self.headers,json={'physical_ready':True,'disposable_only':True,'device_idle':True})
        self.assertEqual(r.status,200);self.assertGreater(self.backend.ready_until,time.time())

    async def test_wrong_cookie_csrf_origin_or_fetch_site_cannot_arm(self):
        armed={'physical_ready':True,'disposable_only':True,'device_idle':True}
        for headers in ({'Cookie':'qn_ui=wrong','X-CSRF-Token':self.backend.csrf},
                        {**self.headers,'X-CSRF-Token':'wrong'},
                        {**self.headers,'Origin':'https://evil.example'},
                        {**self.headers,'Origin':'null'},
                        {**self.headers,'Origin':f'http://127.0.0.1:{self.backend.port+1}'},
                        {**self.headers,'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers={k:v for k,v in headers.items() if k!='Cookie'}):
                r=await self.client.post('/api/ready',headers=headers,json=armed)
                self.assertEqual(r.status,403);self.assertEqual(self.backend.ready_until,0)
        r=await self.client.post('/api/ready',headers={**self.headers,'Origin':f'http://localhost:{self.backend.port}','Sec-Fetch-Site':'same-origin'},json=armed)
        self.assertEqual(r.status,200)

    async def test_reads_need_the_cookie_but_not_the_csrf_token(self):
        cookie={'Cookie':'qn_ui='+self.backend.cookie}
        self.assertEqual((await self.client.get('/api/status',headers=cookie)).status,200)
        self.assertEqual((await self.client.get('/api/status',headers={**cookie,'Host':f'localhost:{self.backend.port}'})).status,200)

    async def test_landing_cookie_is_http_only_and_same_site_strict(self):
        r=await self.client.get('/')
        cookie=r.cookies['qn_ui'];self.assertEqual(cookie.value,self.backend.cookie)
        self.assertTrue(cookie['httponly']);self.assertEqual(cookie['samesite'],'Strict')

    async def test_oversize_body_is_refused_before_any_action(self):
        body=json.dumps({'physical_ready':True,'disposable_only':True,'device_idle':True,'padding':'x'*(64*1024)})
        r=await self.client.post('/api/ready',headers={**self.headers,'Content-Type':'application/json'},data=body)
        self.assertEqual(r.status,413);self.assertEqual(self.backend.ready_until,0)

    async def test_internal_errors_return_only_generic_text_and_unknown_actions_404(self):
        for error,status in ((ValueError('internal-detail'),409),(KeyError('internal-detail'),502)):
            with self.subTest(error=type(error).__name__),patch.object(self.backend,'disarm',side_effect=error):
                r=await self.client.post('/api/disarm',headers=self.headers,json={})
                self.assertEqual(r.status,status);body=await r.json()
                self.assertEqual((body['code'],body['message']),('failed',GENERIC_MESSAGE));self.assertNotIn('internal-detail',json.dumps(body))
        self.assertEqual((await self.client.post('/api/no-such-action',headers=self.headers,json={})).status,404)

    async def test_api_responses_carry_no_store_nosniff_no_referrer_and_scriptless_csp(self):
        for response in (await self.client.get('/api/status',headers=self.headers),
                         await self.client.post('/api/ready',headers=self.headers,json={})):
            with self.subTest(status=response.status):
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
                self.assertIn("script-src 'none'",response.headers['Content-Security-Policy'])

if __name__=='__main__':unittest.main()
