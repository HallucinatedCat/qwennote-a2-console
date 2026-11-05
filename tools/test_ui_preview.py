"""The synthetic-data preview server applies the same request guards as web_backend."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import ui_preview
from public_errors import GENERIC_MESSAGE


class PreviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.preview=ui_preview.Preview('demo')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),ui_preview.make_handler(self.preview))
        self.port=self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.auth={'Cookie':'qn_ui='+self.preview.cookie,'X-CSRF-Token':self.preview.csrf}

    def request(self,method,path,headers=None,body=None):
        connection=HTTPConnection('127.0.0.1',self.port,timeout=10)
        try:
            connection.putrequest(method,path,skip_host=True)
            for key,value in {'Host':f'127.0.0.1:{self.port}',**(headers or {})}.items():connection.putheader(key,value)
            data=b'' if body is None else json.dumps(body).encode()
            if method=='POST':connection.putheader('Content-Type','application/json');connection.putheader('Content-Length',str(len(data)))
            connection.endheaders(data if method=='POST' else None)
            response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:connection.close()

    def test_foreign_host_origin_or_cross_site_requests_are_refused(self):
        for method,path,headers in (('GET','/api/status',{**self.auth,'Host':'evil.example'}),
                                    ('GET','/healthz',{'Host':'evil.example'}),
                                    ('GET','/healthz',{'Sec-Fetch-Site':'cross-site'}),
                                    ('POST','/api/disarm',{**self.auth,'Origin':'https://evil.example'}),
                                    ('POST','/api/disarm',{**self.auth,'Origin':'null'}),
                                    ('POST','/api/disarm',{**self.auth,'Sec-Fetch-Site':'cross-site'}),
                                    ('GET','/',{'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'no-cors','Sec-Fetch-Dest':'image'})):
            with self.subTest(method=method,path=path,headers=sorted(set(headers)-{'Cookie','X-CSRF-Token'})):
                self.assertEqual(self.request(method,path,headers,{} if method=='POST' else None)[0],403)
        self.assertEqual(self.request('GET','/',{'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})[0],200)
        self.assertEqual(self.request('GET','/api/status',{**self.auth,'Host':f'localhost:{self.port}','Origin':f'http://localhost:{self.port}'})[0],200)
        self.assertEqual(self.request('POST','/api/disarm',self.auth,{})[0],200)

    def test_responses_carry_the_backend_security_headers(self):
        status,headers,_=self.request('GET','/')
        self.assertEqual(status,200);self.assertIn("form-action 'self'",headers['Content-Security-Policy'])
        self.assertIn("script-src 'nonce-",headers['Content-Security-Policy'])
        for method,path in (('GET','/api/status'),('POST','/api/ready')):
            with self.subTest(path=path):
                status,headers,_=self.request(method,path,self.auth,{} if method=='POST' else None)
                self.assertIn("script-src 'none'",headers['Content-Security-Policy'])
                self.assertEqual((headers['Cache-Control'],headers['X-Content-Type-Options'],headers['Referrer-Policy']),('no-store','nosniff','no-referrer'))

    def test_unexpected_errors_return_only_the_generic_text(self):
        for error,status in ((ValueError('internal-detail'),409),(TypeError('internal-detail'),502)):
            with self.subTest(error=type(error).__name__),patch.object(self.preview,'act',side_effect=error):
                code,_,raw=self.request('POST','/api/disarm',self.auth,{})
                body=json.loads(raw)
                self.assertEqual(code,status);self.assertEqual((body['code'],body['message']),('failed',GENERIC_MESSAGE))
                self.assertNotIn('internal-detail',raw.decode())

    def test_status_leaves_out_the_setting_catalog_the_client_already_holds(self):
        full=json.loads(self.request('GET','/api/status',self.auth)[2])
        self.assertIn('setting_details',full);revision=full['settings_revision']
        lean=json.loads(self.request('GET','/api/status?since=0&settings_revision='+revision,self.auth)[2])
        self.assertNotIn('setting_details',lean);self.assertNotIn('setting_choices',lean)
        self.assertLess(len(json.dumps(lean)),len(json.dumps(full)))

    def test_action_timings_match_the_backend_shape(self):
        self.request('POST','/api/disarm',self.auth,{});self.request('POST','/api/no-such-action',self.auth,{})
        timings=json.loads(self.request('GET','/api/status',self.auth)[2])['action_timings']
        self.assertEqual(set(timings),{'disarm'});self.assertEqual(set(timings['disarm']),{'count','failures','last_ms','max_ms','total_ms'})

    def test_oversize_body_is_refused(self):
        status,_,_=self.request('POST','/api/ready',self.auth,{'padding':'x'*(64*1024)})
        self.assertEqual(status,413)


if __name__=='__main__':unittest.main()
