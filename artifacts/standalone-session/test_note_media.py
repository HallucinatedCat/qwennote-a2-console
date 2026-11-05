"""Application-path original-media retrieval. All fixtures are synthetic."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
import note_media as p
from public_errors import Busy,Refused,Unavailable
from test_backend_improvements import ready_backend
from web_backend import make_app

UUID='synthetic-note'
URL='https://media.dingtalk.com/synthetic/audio.mp3?signature=synthetic-only'
PROBE={'format':'mp3','codec':'mp3','duration_seconds':12.5}


def replies(incognito=None):
    return {
        p.DETAIL_URI:{'code':200,'body':{'data':{'uuid':UUID,'mediaType':'audio','isIncognitoMode':incognito,'duration':12500,'size':12}}},
        p.PLAY_URI:{'code':200,'body':{'statusCode':200,'mobileAudioUrl':'https://mobile.dingtalk.com/synthetic/audio.mp3','duration':12500,'size':12}},
        p.DOWNLOAD_URI:{'code':200,'body':{'exportUrl':URL,'exportFilename':'original.mp3','isLiveTask':False}},
    }


def saved_file(root):
    return {'path':root/'media'/'saved.mp3','filename':'saved.mp3','bytes':12,'sha256':'a'*64,'mime':'audio/mpeg','probe':PROBE}


class Stream:
    def __init__(self,chunks):self.chunks=chunks
    async def iter_chunked(self,size):
        for chunk in self.chunks:yield chunk


class Response:
    def __init__(self,body=b'synthetic audio',content_type='audio/mpeg',length=None):
        self.content=Stream([body[:5],body[5:]]);self.content_type=content_type
        self.headers={'Content-Length':str(len(body) if length is None else length),'Content-Encoding':'identity'}
        self.content_length=int(self.headers['Content-Length'])
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass


class Session:
    def __init__(self,response):self.response=response
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def get(self,*args,**kwargs):return self.response


class ContractTests(unittest.TestCase):
    def test_requests_match_application_contract(self):
        self.assertEqual(p.DETAIL_URI,'/r/Adaptor/PortalMinutesI/minutesDetailV2')
        self.assertEqual(p.PLAY_URI,'/r/Adaptor/PortalMinutesI/queryPlayInfo')
        self.assertEqual(p.DOWNLOAD_URI,'/r/Adaptor/PortalMinutesI/queryDownloadUrl')
        self.assertEqual(p.download_request(UUID),[{'uuid':UUID,'needSubTitle':False}])
        with self.assertRaises(ValueError):p.download_request('../bad')

    def test_selection_uses_application_non_incognito_semantics(self):
        for incognito in (None,False):
            data=replies(incognito);selected=p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)
            self.assertEqual(selected['url'],URL);self.assertEqual(selected['expected_size'],12);self.assertEqual(selected['expected_duration_ms'],12500)
        for field,value in (('isIncognitoMode',True),('mediaType',0),('mediaType',1),('uuid','other')):
            data=replies();data[p.DETAIL_URI]['body']['data'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)
        data=replies();data[p.DOWNLOAD_URI]['body']['isLiveTask']=True
        with self.assertRaises(ValueError):p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)

    def test_fallback_urls_and_rejected_replies(self):
        for download_reply in ({'code':200,'body':{'exportUrl':''}},{'code':500,'body':None},{'code':200,'body':None}):
            data=replies();data[p.DOWNLOAD_URI]=download_reply
            selected=p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)
            self.assertEqual(selected['source'],'mobile_play')
        for uri in (p.DETAIL_URI,p.PLAY_URI):
            data=replies();data[uri]={'code':500,'body':None}
            with self.subTest(uri=uri),self.assertRaises(ValueError):p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)
        data=replies();data[p.DOWNLOAD_URI]={'code':500,'body':None};data[p.PLAY_URI]['body']['mobileAudioUrl']='';data[p.PLAY_URI]['body']['pcAudioUrl']='https://pc.dingtalk.com/synthetic/audio.mp3'
        self.assertEqual(p.select(*[data[uri] for uri in (p.DETAIL_URI,p.PLAY_URI,p.DOWNLOAD_URI)],UUID)['source'],'pc_play')

    def test_signed_url_validation_and_dns_pinning(self):
        for url in (URL,'https://oss.alicdn.com/media/audio.m4a'):
            self.assertEqual(p.validate_url(url),url)
        for url in ('http://media.dingtalk.com/a','https://media.dingtalk.com.evil.example/a','https://user@media.dingtalk.com/a','https://media.dingtalk.com:8443/a','https://media.dingtalk.com/a#fragment','https://example.com/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):p.validate_url(url)
        async def checks():
            resolver=p.PublicResolver('media.dingtalk.com',443)
            with self.assertRaises(ValueError):await resolver.resolve('localhost',443)
            with patch.object(p.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
                with self.assertRaises(ValueError):await resolver.resolve('media.dingtalk.com',443)
            with self.assertRaises(ValueError):await resolver.resolve('other.dingtalk.com',443)
            await resolver.close()
        asyncio.run(checks())


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_to_private_final_file_and_hashes_without_url_metadata(self):
        body=b'synthetic audio'
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.aiohttp,'ClientSession',return_value=Session(Response(body))),patch.object(p,'probe',return_value=PROBE):
            result=await p.download(URL,'synthetic-agent',Path(tmp)/'media',len(body))
            path=result['path'];self.assertEqual(path.read_bytes(),body);self.assertEqual(result['sha256'],hashlib.sha256(body).hexdigest())
            self.assertEqual(result['filename'],path.name);self.assertTrue(path.name.endswith('.mp3'));self.assertNotIn(URL,json.dumps({k:str(v) for k,v in result.items()}))
            self.assertEqual(os.stat(path).st_mode&0o777,0o600);self.assertEqual(os.stat(path.parent).st_mode&0o777,0o700)
            self.assertEqual(list(path.parent.glob('*.part')),[])

    async def test_failed_download_leaves_no_media_or_partial_file(self):
        cases=(Response(content_type='text/html'),Response(length=100),Response(length=p.MAX_BYTES+1))
        for response in cases:
            with self.subTest(content_type=response.content_type,length=response.content_length),tempfile.TemporaryDirectory() as tmp,patch.object(p.aiohttp,'ClientSession',return_value=Session(response)),patch.object(p,'probe',return_value=PROBE):
                with self.assertRaises(ValueError) as caught:await p.download(URL,'synthetic-agent',Path(tmp)/'media')
                self.assertNotIn(URL,str(caught.exception));self.assertEqual(list((Path(tmp)/'media').iterdir()),[])

    async def test_redirects_are_not_followed_and_non_200_or_encoded_replies_save_nothing(self):
        redirect=Response();redirect.status=302;redirect.headers['Location']='https://media.dingtalk.com/elsewhere'
        encoded=Response();encoded.headers['Content-Encoding']='gzip'
        for response,stage in ((redirect,'http-3xx'),(encoded,'encoding')):
            requests=[]
            class Recording(Session):
                def get(self,*args,**kwargs):requests.append(kwargs);return self.response
            with self.subTest(status=getattr(response,'status',200)),tempfile.TemporaryDirectory() as tmp,patch.object(p.aiohttp,'ClientSession',return_value=Recording(response)),patch.object(p,'probe',return_value=PROBE):
                with self.assertRaises(ValueError) as caught:await p.download(URL,'synthetic-agent',Path(tmp)/'media')
                self.assertEqual(caught.exception.stage,stage)  # the failure report depends on this name
                self.assertEqual(requests,[{'allow_redirects':False}]);self.assertEqual(list((Path(tmp)/'media').iterdir()),[])

    async def test_probe_requires_an_audio_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audio';path.write_bytes(b'synthetic')
            completed=type('Completed',(),{'returncode':0,'stdout':json.dumps({'format':{'format_name':'mp3','duration':'1.25','size':'9'},'streams':[{'codec_type':'audio','codec_name':'mp3'}]}),'stderr':b''})()
            with patch.object(p.subprocess,'run',return_value=completed) as run:
                self.assertEqual(p.probe(path),{'format':'mp3','codec':'mp3','duration_seconds':1.25})
                self.assertNotIn(str(path),run.call_args.args[0][1])
            completed.stdout='{"format":{},"streams":[{"codec_type":"video"}]}'
            with patch.object(p.subprocess,'run',return_value=completed),self.assertRaises(ValueError):p.probe(path)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.b=ready_backend(self.root)
        self.b.settings.update(storage_type='0',enable_incognito_mode='0')
        listed={'content':[{'type':'text','text':json.dumps({'items':[{'uuid':UUID}]})}]};self.b.remember_notes(listed)
        data=replies()
        async def rpc(uri,args,*_,**kwargs):return data[uri]
        self.b.channel._request=AsyncMock(side_effect=rpc)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_application_reads_then_private_download_without_url_or_path_in_response(self):
        with patch.object(p,'download',AsyncMock(return_value=saved_file(self.root))) as download:
            result=await self.b.download_note_media(UUID)
        self.assertTrue(result['saved']);self.assertEqual(result['bytes'],12);self.assertNotIn('path',result);self.assertNotIn('url',result)
        self.assertEqual(self.b.channel._request.await_count,3);self.assertEqual(download.await_count,1)
        journal=(self.root/'note-media.json').read_text();self.assertNotIn(URL,journal);self.assertNotIn(str(self.root/'media'),journal)
        self.assertFalse(self.b.media_lock.locked());self.assertTrue(self.b.health()['restart_safe'])

    async def test_unavailable_and_failed_download_are_not_retried_or_leaked(self):
        self.b.channel._request.side_effect=lambda uri,args,*_,**kwargs:replies(True)[uri]
        with patch.object(p,'download',AsyncMock()) as download,self.assertRaises(Unavailable):await self.b.download_note_media(UUID)
        download.assert_not_awaited()
        self.b.channel._request.side_effect=lambda uri,args,*_,**kwargs:replies()[uri]
        with patch.object(p,'download',AsyncMock(side_effect=ValueError(URL))) as download,self.assertRaises(Unavailable) as caught:await self.b.download_note_media(UUID)
        self.assertNotIn(URL,str(caught.exception));self.assertEqual(download.await_count,1);self.assertFalse(self.b.media_lock.locked())
        self.assertNotIn(URL,(self.root/'note-media.json').read_text())

    async def test_foreign_note_and_busy_lock_send_nothing(self):
        with self.assertRaises(Refused):await self.b.download_note_media('other-note')
        await self.b.media_lock.acquire()
        try:
            with patch('web_backend.QUEUE_SECONDS',.05),self.assertRaises(Busy):await self.b.download_note_media(UUID)
        finally:self.b.media_lock.release()
        self.b.channel._request.assert_not_awaited()

    async def test_storage_and_incognito_settings_gate_all_native_calls(self):
        for key,value in (('storage_type','-1'),('storage_type',None),('enable_incognito_mode','1'),('enable_incognito_mode',None)):
            original=self.b.settings.get(key);self.b.settings[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(Unavailable):await self.b.download_note_media(UUID)
            if original is None:self.b.settings.pop(key,None)
            else:self.b.settings[key]=original
        self.b.channel._request.assert_not_awaited()

    async def test_http_csrf_shape_and_safe_json(self):
        b=self.b;b.start=AsyncMock();b.stop=AsyncMock();client=TestClient(TestServer(make_app(b)));await client.start_server();self.addAsyncCleanup(client.close);b.port=client.port
        headers={'Cookie':'qn_ui='+b.cookie,'X-CSRF-Token':b.csrf};body={'task_uuid':UUID}
        self.assertEqual((await client.post('/api/note-media-download',json=body)).status,403)
        self.assertEqual((await client.post('/api/note-media-download',json={**body,'url':URL},headers=headers)).status,409)
        with patch.object(p,'download',AsyncMock(return_value=saved_file(self.root))):
            response=await client.post('/api/note-media-download',json=body,headers=headers)
        self.assertEqual(response.status,200);result=await response.json();self.assertTrue(result['saved']);self.assertNotIn(URL,await response.text())

    def test_ui_has_one_explicit_save_action_and_no_remote_media_url(self):
        page=(Path(__file__).resolve().parent/'web.html').read_text()
        self.assertEqual(page.count('id="note-media-download"'),1)
        self.assertIn("run('note-media-download',{task_uuid:state.selected.uuid}",page)
        self.assertNotIn('result.media_url',page);self.assertNotIn('audio.src=',page)


if __name__=='__main__':unittest.main()
