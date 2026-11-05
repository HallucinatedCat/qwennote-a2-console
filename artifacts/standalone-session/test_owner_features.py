"""Recordings, note states, dangerous actions, share password, MCP writes. Synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import device_files
import note_sharing
import note_status
import mcp_login
import native_extras as extras
from urllib.parse import parse_qs,urlsplit
from aiohttp.test_utils import TestClient,TestServer
from web_backend import make_app
from public_errors import Refused,Unavailable
from test_backend_improvements import CONFIG,ready_backend
from web_backend import mcp_write_arguments

UUID='0f0e0d0c-0b0a-4908-8706-050403020100'


class ProtocolTests(unittest.TestCase):
    def test_recordings_keep_file_ids_private_and_downloads_keep_only_safe_headers(self):
        reply={'code':200,'body':{'fileList':[{'fileId':'f1','fileName':'a','duration':1000,'fileSize':10,'createTime':5,'state':3,
                                                'minutes':{'state':0},'attributes':{'incognitoStatus':'1'}},{'fileId':'bad id!'}],
                                  'nextCursor':'c2','hasMore':True}}
        rows,cursor,more=device_files.parse_list(reply)
        self.assertEqual(len(rows),1);self.assertEqual((cursor,more),('c2',True))
        self.assertEqual({k:rows[0][k] for k in ('note_state','incognito','in_recycle_bin')},{'note_state':'not-transcribed','incognito':True,'in_recycle_bin':True})
        self.assertIsNone(device_files.parse_download({'code':200,'body':{'state':1}}))
        # The live reply for the A2's incognito recordings: ready, but no audio to download.
        with self.assertRaises(device_files.NoAudio):device_files.parse_download({'code':200,'body':{'state':2}})
        url,headers=device_files.parse_download({'code':200,'body':{'state':2,'fileDownloadInfoDTO':{'resourceUrl':'https://x.aliyuncs.com/a',
                                                  'headers':{'Authorization':'t','Cookie':'no','Host':'evil'}}}})
        self.assertEqual(headers,{'Authorization':'t'})
        # The failure journal's reply shape is read out in chat: names, types and status numbers only.
        secret={'code':200,'body':{'state':2,'fileId':123456789,'fileDownloadInfoDTO':{'resourceUrl':'https://x.aliyuncs.com/a?Signature=s','headers':{'Authorization':'t'}},'list':[{'url':'u'}]}}
        self.assertEqual(device_files.shape(secret),{'code':200,'body':{'state':2,'fileId':'int','fileDownloadInfoDTO':{'resourceUrl':'str','headers':{'Authorization':'str'}},'list':[{'url':'str'}]}})
        self.assertEqual(device_files.delete_request(CONFIG,['f1'],device_files.PERMANENT)[0]['state'],1)

    def test_note_states_follow_the_app(self):
        self.assertEqual(note_status.processing({'businessType':2,'status':14,'summaryExtractStatus':1}),'generating')
        self.assertEqual(note_status.processing({'businessType':2,'status':14,'summaryExtractStatus':2}),None)
        self.assertEqual(note_status.processing({'businessType':6,'status':22}),'transcribing')
        self.assertEqual(note_status.processing({'businessType':2,'status':15,'hardwareFileInfoModel':{'fileStatus':'noStorage'}}),'failed')
        # An empty reply is a note without a summary (e.g. two seconds of silence), not one still generating.
        self.assertFalse(note_status.brief_generating({'code':200,'body':{}}))
        self.assertTrue(note_status.summary_empty({'code':200,'body':{}}))
        self.assertTrue(note_status.brief_generating({'code':200,'body':{'extractStatus':1}}))
        self.assertFalse(note_status.summary_empty({'code':200,'body':{'fullTextSummary':{'value':'# Notes'}}}))

    def test_share_password_is_set_cleared_or_kept(self):
        snapshot={'scope':0,'role_code':'r','options':{2:{'enabled':True,'default_role_code':'p','role_subresource_ids':[]}},
                  'org_open_scope':0,'can_search':False,'can_recommend':False,'require_password':True,'password':'abcd','expire_at':None,'expire_days':None}
        kept=note_sharing.save_request(UUID,snapshot,'public')[0]
        self.assertEqual((kept['requirePassword'],kept['password']),(True,'abcd'))
        self.assertEqual(note_sharing.save_request(UUID,snapshot,'public','x7k2')[0]['password'],'x7k2')
        self.assertIs(note_sharing.save_request(UUID,snapshot,'public','')[0]['requirePassword'],False)
        for bad in ('ab','with space','密码密码'):
            with self.assertRaises(ValueError):note_sharing.save_request(UUID,snapshot,'public',bad)

    def test_mcp_write_arguments_are_checked(self):
        self.assertEqual(mcp_write_arguments('update_minutes_title',{'taskUuid':UUID,'title':'T'}),{'taskUuid':UUID,'title':'T'})
        self.assertEqual(mcp_write_arguments('add_personal_hot_word',{'hotWordList':[' A2 ']}),{'hotWordList':['A2']})
        for tool,values in (('update_minutes_title',{'taskUuid':UUID,'title':''}),('replace_speaker',{'taskUuid':UUID,'speakerNick':'a'}),
                            ('sendDeviceTaskCommand',{}),('update_minutes_title',{'taskUuid':'bad id!','title':'T'})):
            with self.subTest(tool=tool),self.assertRaises(Refused):mcp_write_arguments(tool,values)


class DangerousActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.b=ready_backend(Path(self.tmp.name))
        self.b.device_record={'devServId':1446}
    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_destructive_actions_need_the_switch_and_a_typed_word(self):
        b=self.b
        with self.assertRaises(Refused):await b.unbind_device('unbind')
        b.set_dangerous({'enabled':True,'confirm':True})
        with self.assertRaises(Refused):await b.unbind_device('yes')
        b.channel._request.assert_not_awaited()
        b.channel._request=AsyncMock(return_value={'code':200,'body':{}})
        self.assertEqual(await b.unbind_device('unbind'),{'unbound':True})
        self.assertEqual(b.channel._request.await_count,1)
        self.assertFalse(json.loads((b.state/'device-unbind.json').read_text())['pending'])
        self.assertTrue(json.loads((b.state/'dangerous.json').read_text())['enabled'])

    async def test_ble_pairing_file_is_owner_only_and_keeps_its_client_id(self):
        b=self.b;b.device_record={'devServId':1446,'sn':'SYNTH-SN','deviceSecret':'ab'*16}
        with self.assertRaises(Refused):b.ble_pairing_file('pair')  # switch off
        b.set_dangerous({'enabled':True,'confirm':True})
        with self.assertRaises(Refused):b.ble_pairing_file('yes')
        first=json.loads(b.ble_pairing_file('pair').text);second=json.loads(b.ble_pairing_file('pair').text)
        self.assertEqual((first['sn'],first['device_secret']),('SYNTH-SN','ab'*16));self.assertEqual(first['did'],second['did'])
        self.assertEqual((b.state/'ble-client.json').stat().st_mode&0o777,0o600)
        self.assertNotIn('ab'*16,json.dumps(list(b.events)))


class McpLoginTests(unittest.IsolatedAsyncioTestCase):
    def test_request_matches_the_sdk_and_tokens_are_saved_in_the_reader_format(self):
        meta=mcp_login.check_metadata({'issuer':'https://minutes.qwennote.cn','authorization_endpoint':'https://minutes.qwennote.cn/authorize',
                                       'token_endpoint':'https://minutes.qwennote.cn/token','registration_endpoint':'https://minutes.qwennote.cn/register'})
        url,pending=mcp_login.start(meta,{'client_id':'fixture-client'},'write','http://127.0.0.1:18780/mcp/callback')
        query=parse_qs(urlsplit(url).query)
        self.assertEqual(query['scope'],['qwennote:read qwennote:write']);self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertEqual(query['resource'],['https://minutes.qwennote.cn/mcp']);self.assertEqual(query['redirect_uri'],['http://127.0.0.1:18780/mcp/callback'])
        self.assertEqual(mcp_login.callback('http://127.0.0.1:18780/mcp/callback?code=c1&state='+pending['state']),('c1',pending['state']))
        with self.assertRaises(Refused):mcp_login.callback('?error=access_denied&state=s')
        with self.assertRaises(Refused):mcp_login.check_token({'access_token':'a','token_type':'Bearer','scope':'qwennote:read'},'write')
        token=mcp_login.check_token({'access_token':'a','token_type':'Bearer','scope':'qwennote:read qwennote:write','expires_in':3600,'refresh_token':'r','extra':'x'},'write')
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'.private').mkdir(mode=0o700)
            mcp_login.save(tmp,'write',token)
            saved=json.loads((Path(tmp)/'.private'/'write'/'tokens.json').read_text())
            self.assertEqual(set(saved),{'tokens','expires_at'});self.assertNotIn('extra',saved['tokens'])
            self.assertEqual((Path(tmp)/'.private'/'write'/'tokens.json').stat().st_mode&0o777,0o600)
        with self.assertRaises(Unavailable):mcp_login.check_metadata({'issuer':'https://evil.invalid','authorization_endpoint':'x','token_endpoint':'y'})

    def test_console_client_is_registered_once_and_tokens_name_their_client(self):
        body=mcp_login.registration('http://127.0.0.1:18780/mcp/callback')
        self.assertEqual((body['redirect_uris'],body['token_endpoint_auth_method']),(['http://127.0.0.1:18780/mcp/callback'],'none'))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'.private').mkdir(mode=0o700)
            info=mcp_login.keep_client(tmp,{'client_id':'console-1','client_name':'QwenNote Personal Console','unexpected':'x'},'http://127.0.0.1:18780/mcp/callback')
            self.assertNotIn('unexpected',info)
            self.assertEqual(mcp_login.console_client(tmp,'http://127.0.0.1:18780/mcp/callback')['client_id'],'console-1')
            self.assertIsNone(mcp_login.console_client(tmp,'http://localhost:18780/mcp/callback'))  # another address registers again
            mcp_login.save(tmp,'write',{'access_token':'a','token_type':'Bearer'},info)
            store=Path(tmp)/'.private'/'write'
            self.assertEqual(json.loads((store/'tokens.json').read_text())['client_id'],'console-1')
            self.assertEqual(json.loads((store/'client.json').read_text())['client_id'],'console-1')

    async def test_callback_needs_no_cookie_but_a_pending_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=ready_backend(Path(tmp));b.port=1;b.start=AsyncMock();b.stop=AsyncMock()
            client=TestClient(TestServer(make_app(b)));await client.start_server();b.port=client.server.port
            try:
                response=await client.get('/mcp/callback?code=c&state=unknown',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})
                self.assertEqual(response.status,200);self.assertIn('Login not completed',await response.text())
                self.assertEqual((await client.post('/api/mcp-login-finish',json={'url':'?code=c&state=unknown'})).status,403)  # the API still needs the cookie
            finally:await client.close()


class NativeExtrasTests(unittest.IsolatedAsyncioTestCase):
    def test_protocol_shapes(self):
        rows=[{'uuid':11,'startTime':0,'speakerDisplayModel':{'nickName':'Speaker 1'}},{'uuid':12,'startTime':900,'nickName':'Lin'},
              {'uuid':13,'startTime':1800,'speakerDisplayModel':{'nickName':'Speaker 1'}}]
        body=extras.speaker_request(UUID,rows,'Speaker 1','Wei')[0]
        self.assertEqual([p['paragraphId'] for p in body['paragraphIDList']],[11,13])
        self.assertEqual((body['uid'],body['globalChange'],body['currentParagraph']['paragraphId']),('-2','true',11))
        self.assertIsNone(extras.speaker_request(UUID,rows,'Nobody','Wei'))
        markers=extras.note_markers({'code':200,'body':{'noteList':[{'type':2,'content':'t','relativeTimeMs':5000},{'type':0,'noteTime':9000,'audioStartTime':1000},{'type':9}]}})
        self.assertEqual([(m['kind'],m['time_ms']) for m in markers],[('text',5000),('marker',8000)])
        notes,offset=extras.note_list({'code':200,'body':{'hasMore':True,'offset':'o2','itemList':[{'uuid':UUID,'title':'T','gmtCreate':1,'duration':2,'collected':True}]}})
        self.assertEqual((notes[0]['durationMillis'],offset),(2,'o2'))
        self.assertEqual(extras.tabs({'code':200,'body':'{"tabList":[{"tabType":1,"tabId":"all","tabName":"All"},{"tabType":2,"tabId":7,"tabName":"Field"}]}'}),[('7','Field')])
        renamed=extras.group_rename(CONFIG,{'groupId':5,'groupName':'Old','parentGroupId':0,'keywords':['k']},'New')[0]
        self.assertEqual((renamed['groupId'],renamed['groupName'],renamed['keywords']),(5,'New',['k']))
        self.assertEqual(extras.connections({'code':200,'body':{'connectionList':[{'appDeviceName':'P','state':4},{'state':3}]}}),
                         [{'name':'P','state':'connected','app_type':None},{'name':None,'state':'blocked','app_type':None}])
        self.assertFalse(extras.memo_deleted({'code':200,'body':{'deletedCount':-1}}))

    async def test_search_results_become_usable_and_memo_delete_needs_the_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=ready_backend(Path(tmp))
            b.channel._request=AsyncMock(return_value={'code':200,'body':{'itemList':[{'uuid':UUID,'title':'Found'}]}})
            result=await b.notes_browse('search','Found')
            self.assertEqual(result['items'][0]['uuid'],UUID);b.require_listed_note(UUID)
            handle=b.handle('memo','private-memo')
            with self.assertRaises(Refused):await b.native_write('memo-delete',extras.MEMO_DELETE_URI,extras.memo_delete(b.resolve('memo',handle)),dangerous=True)
            with self.assertRaises(Refused):b.resolve('memo','unknown-handle')
