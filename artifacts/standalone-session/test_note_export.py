"""Exports restrict note context, outbound origin and bounded UTF-8 downloads."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_export as p
from web_backend import make_app
from public_errors import Refused,Busy,Unavailable
from test_backend_improvements import ready_backend
from test_note_favorites import UUID,LIST

URL='https://shanji.dingtalk.com/api/minutes/download/export/content?downloadToken=synthetic-only'
REPLY={'code':200,'body':{'exportUrl':URL}}

class ContractTests(unittest.TestCase):
    def test_request_defaults_to_the_legacy_txt_contract(self):
        expected={'taskUuid':UUID,'speaker':True,'timestamp':True,'exportFileType':'TXT','targetLang':None,'supportBilingual':False,'exportContentType':'content'}
        self.assertEqual(p.request(UUID,'transcript'),[expected])
        self.assertEqual(p.request(UUID,'summary')[0]['exportContentType'],'summary')
        for uuid,kind in [('bad/path','summary'),(UUID,[]),(UUID,'html')]:
            with self.assertRaises(ValueError):p.request(uuid,kind)

    def test_translation_and_bilingual_options_match_app(self):
        translated=p.request(UUID,'transcript','en',False)[0]
        bilingual=p.request(UUID,'summary','en',True)[0]
        self.assertEqual((translated['targetLang'],translated['supportBilingual']),('en',False))
        self.assertEqual((bilingual['targetLang'],bilingual['supportBilingual']),('en',True))
        self.assertEqual(bilingual['exportContentType'],'summary')
        for args in ((UUID,'transcript','bad/lang',False),(UUID,'transcript',None,True),(UUID,'transcript','en',1)):
            with self.subTest(args=args),self.assertRaises(ValueError):p.request(*args)

    def test_application_file_formats_and_kind_restrictions(self):
        expected={'txt':('TXT','txt','text/plain'),'markdown':('Markdown','md','text/markdown'),
                  'srt':('SRT','srt','text/plain'),'docx':('DOCX','docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                  'pdf':('PDF','pdf','application/pdf')}
        for kind in ('transcript','summary'):
            for file_format,(server,extension,mime) in expected.items():
                if kind=='summary' and file_format=='srt':continue
                with self.subTest(kind=kind,file_format=file_format):
                    self.assertEqual(p.request(UUID,kind,None,False,file_format)[0]['exportFileType'],server)
                    self.assertEqual(p.format_spec(kind,file_format)[1:],(extension,mime,file_format in ('txt','markdown','srt')))
        for kind,file_format in (('transcript','html'),('transcript','PDF'),('transcript',None),('summary','srt')):
            with self.subTest(kind=kind,file_format=file_format),self.assertRaises(ValueError):p.request(UUID,kind,None,False,file_format)

    def test_untrusted_urls_never_pass(self):
        self.assertEqual(p.receipt(REPLY,'transcript'),URL)
        invalid=[URL.replace('https:','http:'),URL.replace('shanji.dingtalk.com','localhost'),
                 URL.replace('shanji.dingtalk.com','shanji.dingtalk.com.evil.invalid'),
                 URL.replace('shanji.dingtalk.com','user@shanji.dingtalk.com'),
                 URL.replace('.com/','.com:8443/'),URL.replace('/content?','/summary?'),
                 URL+'#fragment',URL+'&downloadToken=duplicate',URL.replace('synthetic-only',''),
                 URL.replace('synthetic-only','%0a'),URL+'\n',URL.replace('downloadToken=','other=')]
        for url in invalid:
            with self.subTest(url=url),self.assertRaises(ValueError):p.validate_url(url,'transcript')
        for reply in (None,{'code':500,'body':REPLY['body']},{'code':200,'body':[]},{'code':200,'body':{**REPLY['body'],'code':500}}):
            with self.assertRaises(ValueError):p.receipt(reply,'transcript')

class Response:
    status=200;content_type='text/plain';content_length=None
    headers={'Content-Disposition':'attachment; filename="synthetic.txt"'}
    def __init__(self,chunks):self.chunks=chunks;self.content=self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def iter_chunked(self,size):
        for chunk in self.chunks:yield chunk

class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self,response,file_format='txt'):
        class Session:
            async def __aenter__(inner):return inner
            async def __aexit__(inner,*args):pass
            def get(inner,url,**kwargs):
                self.assertEqual(url,URL);self.assertFalse(kwargs['allow_redirects'])
                self.assertEqual(set(kwargs['headers']),{'User-Agent','Accept','Accept-Encoding'})
                self.assertEqual(kwargs['headers']['Accept'],p.format_spec('transcript',file_format)[2])
                return response
        with patch.object(p,'ClientSession',return_value=Session()) as client,patch.object(p,'TCPConnector'):
            data=await p.download(URL,'transcript','synthetic-agent',file_format)
            self.assertFalse(client.call_args.kwargs['trust_env']);self.assertFalse(client.call_args.kwargs['auto_decompress'])
            return data

    async def test_chunked_file_read_to_end(self):
        self.assertEqual(await self.fetch(Response([b'first ',b'second'])),b'first second')

    async def test_markdown_srt_and_binary_formats_validate_content(self):
        cases=[('markdown','text/plain',b'# Synthetic'),('markdown','text/markdown',b'# Synthetic'),('srt','text/plain',b'1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n'),
               ('docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',b'PK\x03\x04synthetic'),
               ('pdf','application/pdf',b'%PDF-1.7\nsynthetic')]
        for file_format,mime,data in cases:
            response=Response([data]);response.content_type=mime
            if file_format in ('docx','pdf'):response.headers={}
            with self.subTest(file_format=file_format):self.assertEqual(await self.fetch(response,file_format),data)
            response.headers={'Content-Disposition':'inline'}
            with self.subTest(file_format=file_format,disposition='inline'),self.assertRaises(ValueError):await self.fetch(response,file_format)
        for file_format,mime in (('docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document'),('pdf','application/pdf')):
            response=Response([b'not the expected file']);response.content_type=mime
            with self.subTest(file_format=file_format),self.assertRaises(ValueError):await self.fetch(response,file_format)

    async def test_redirect_wrong_type_encoding_invalid_text_and_size_rejected(self):
        cases=[{'status':302},{'status':403},{'content_type':'text/html'},
               {'headers':{'Content-Disposition':'inline'}},
               {'headers':{'Content-Disposition':'attachment','Content-Encoding':'gzip'}},
               {'content_length':20}]
        with patch.object(p,'MAX_BYTES',10):
            for changes in cases:
                response=Response([b'ok']);response.__dict__.update(changes)
                with self.subTest(changes=changes),self.assertRaises(ValueError):await self.fetch(response)
            for chunks in ([b'123456',b'78901'],[b'\xff'],[]):
                with self.assertRaises(ValueError):await self.fetch(Response(chunks))

    async def test_resolution_rejects_private_mixed_or_wrong_destinations(self):
        resolver=p.PublicResolver()
        try:
            for addresses in ([],[{'host':'127.0.0.1'}],[{'host':'::1'}],[{'host':'224.0.0.1'}],[{'host':'fec0::1'}],[{'host':'93.184.216.34'},{'host':'10.0.0.1'}]):
                with patch.object(p.DefaultResolver,'resolve',AsyncMock(return_value=addresses)),self.assertRaises(ValueError):
                    await resolver.resolve(p.HOST,443)
            with patch.object(p.DefaultResolver,'resolve',AsyncMock()) as resolve,self.assertRaises(ValueError):
                await resolver.resolve('localhost',443)
            resolve.assert_not_called()
        finally:await resolver.close()

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.b=ready_backend(Path(self.tmp.name));self.b.remember_notes(LIST)
        self.b.channel._request=AsyncMock(return_value=REPLY)
    async def test_context_type_and_busy_guards_send_nothing(self):
        for uuid,kind in [('foreign','transcript'),(UUID,[]),(UUID,'html')]:
            with self.assertRaises(Refused):await self.b.export_note(uuid,kind)
        async with self.b.export_lock:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.export_note(UUID,'transcript')
        self.b.note_refs[UUID]=time.time()-24*3600-1
        with self.assertRaises(Refused):await self.b.export_note(UUID,'transcript')
        self.b.channel._request.assert_not_called()

    async def test_journal_before_request_and_private_url_excluded_from_status(self):
        b=self.b
        async def request(*args):
            self.assertEqual(json.loads((b.state/'note-export.json').read_text())['phase'],'requested')
            self.assertFalse(b.health()['restart_safe']);return REPLY
        b.channel._request.side_effect=request
        with patch.object(p,'download',AsyncMock(return_value=b'plain text')):
            self.assertEqual(await b.export_note(UUID,'transcript'),b'plain text')
        self.assertTrue(b.health()['restart_safe']);self.assertEqual(b.ready_until,0)
        self.assertNotIn('synthetic-only',json.dumps(b.status()))
        self.assertEqual((b.state/'note-export.json').stat().st_mode&0o777,0o600)
        b.channel._request.assert_awaited_once()

    async def test_translation_requires_a_supported_target(self):
        b=self.b;b.translate_note=AsyncMock(return_value={'targets':[{'key':'en'}]})
        for target,bilingual in [('fr',False),(None,True),('en',1)]:
            with self.subTest(target=target,bilingual=bilingual),self.assertRaises(Refused):
                await b.export_note(UUID,'transcript',target,bilingual)
        b.channel._request.assert_not_called()
        with patch.object(p,'download',AsyncMock(return_value=b'bilingual text')):
            self.assertEqual(await b.export_note(UUID,'transcript','en',True),b'bilingual text')
        request=b.channel._request.await_args.args[1][0]
        self.assertEqual((request['targetLang'],request['supportBilingual']),('en',True))

    async def test_timeout_and_cancellation_release_lock_without_retry(self):
        for failure in (TimeoutError(URL),asyncio.CancelledError()):
            self.b.channel._request.reset_mock()
            with patch.object(p,'download',AsyncMock(side_effect=failure)):
                with self.assertRaises((Unavailable,asyncio.CancelledError)) as caught:await self.b.export_note(UUID,'transcript')
            self.assertNotIn(URL,str(caught.exception));self.assertFalse(self.b.export_lock.locked())
            self.b.channel._request.assert_awaited_once()

    async def test_http_csrf_shape_and_attachment(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        body={'task_uuid':UUID,'kind':'transcript'};headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        self.assertEqual((await client.post('/api/note-export',json=body)).status,403)
        self.assertEqual((await client.post('/api/note-export',json={**body,'url':URL},headers=headers)).status,409)
        b.channel._request.assert_not_called()
        with patch.object(p,'download',AsyncMock(return_value=b'plain text')):
            response=await client.post('/api/note-export',json=body,headers=headers)
        self.assertEqual(response.status,200);self.assertEqual(await response.read(),b'plain text')
        self.assertEqual(response.headers['Content-Disposition'],'attachment; filename="qwennote-transcript.txt"')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertNotIn('synthetic-only',str(response.headers))

    async def test_http_translation_and_bilingual_attachment(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();b.translate_note=AsyncMock(return_value={'targets':[{'key':'en'}]})
        client=TestClient(TestServer(make_app(b)));await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        body={'task_uuid':UUID,'kind':'transcript','target_language':'en','bilingual':True}
        with patch.object(p,'download',AsyncMock(return_value=b'bilingual text')):
            response=await client.post('/api/note-export',json=body,headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual(await response.read(),b'bilingual text')
        self.assertEqual(response.headers['Content-Disposition'],'attachment; filename="qwennote-transcript-en-bilingual.txt"')

    async def test_http_docx_attachment_and_format_validation(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)))
        await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf}
        for file_format in ('html','PDF',None):
            response=await client.post('/api/note-export',json={'task_uuid':UUID,'kind':'transcript','file_format':file_format},headers=headers)
            self.assertEqual(response.status,409)
        b.channel._request.assert_not_called()
        payload=b'PK\x03\x04synthetic'
        with patch.object(p,'download',AsyncMock(return_value=payload)) as download:
            response=await client.post('/api/note-export',json={'task_uuid':UUID,'kind':'transcript','file_format':'docx'},headers=headers)
        self.assertEqual(response.status,200);self.assertEqual(await response.read(),payload)
        self.assertEqual(response.content_type,'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        self.assertEqual(response.headers['Content-Disposition'],'attachment; filename="qwennote-transcript.docx"')
        self.assertEqual(b.channel._request.await_args.args[1][0]['exportFileType'],'DOCX')
        self.assertEqual(download.await_args.args[3],'docx')

    def test_translation_export_controls_use_the_selected_target(self):
        page=(Path(__file__).resolve().parent/'web.html').read_text()
        for target in ('translate-export','translate-export-bilingual'):self.assertIn(f'id="{target}"',page)
        self.assertIn("downloadNote('transcript',$('translate-export'),$('translate-target').value,false,$('translate-export-format').value)",page)
        self.assertIn("downloadNote('transcript',$('translate-export-bilingual'),$('translate-target').value,true,$('translate-export-format').value)",page)
        self.assertIn("toast(t('note.downloaded',{type:t('note.'+kind)}))",page)

    def test_format_selectors_cover_only_application_supported_combinations(self):
        page=(Path(__file__).resolve().parent/'web.html').read_text()
        for selector in ('transcript-export-format','summary-export-format','translate-export-format'):self.assertIn(f'id="{selector}"',page)
        self.assertIn("downloadNote('transcript',$('note-export-transcript'),null,false,$('transcript-export-format').value)",page)
        self.assertIn("downloadNote('summary',$('note-export-summary'),null,false,$('summary-export-format').value)",page)
        transcript=page.split('id="transcript-export-format"',1)[1].split('</select>',1)[0]
        summary=page.split('id="summary-export-format"',1)[1].split('</select>',1)[0]
        for value in ('txt','markdown','srt','docx','pdf'):self.assertIn(f'value="{value}"',transcript)
        for value in ('txt','markdown','docx','pdf'):self.assertIn(f'value="{value}"',summary)
        self.assertNotIn('value="srt"',summary)

if __name__=='__main__':unittest.main()
