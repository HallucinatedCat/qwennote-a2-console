import json
import time
import unittest
from unittest.mock import AsyncMock,patch
import uuid
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import builtin_assistant as target

class ContractTests(unittest.TestCase):
    def test_user_and_token_shapes(self):
        profile=target.user({'body':{'context':json.dumps({'uid':'123','nickName':'Owner'})}})
        self.assertEqual(profile,{'uid':123,'nick':'Owner','origin':target.DEFAULT_ORIGIN})
        with self.assertRaises(target.AssistantError):target.user({'body':{'context':json.dumps({'uid':'123','nickName':'Owner','aiQaSiteDomain':'https://attacker.example'})}})
        value,expiry=target.token({'body':{'token':'sensitive-token','expireTime':(time.time()+3600)*1000}})
        self.assertEqual(value,'sensitive-token');self.assertGreater(expiry,time.time())
        with self.assertRaises(target.AssistantError):target.token({'body':{'token':'','expireTime':0}})

    def test_general_and_note_request(self):
        profile={'uid':123,'nick':'Owner','origin':target.DEFAULT_ORIGIN};session=str(uuid.uuid4())
        path,body=target.request('Summarize',profile,session_id=session)
        self.assertEqual(path,'ai/tingji/v1/chat');self.assertEqual(body['condition'],{'uuids':[]})
        self.assertEqual(body['description'],[{'type':'current_user_info','data':{'uid':123,'nick':'Owner'}}])
        self.assertEqual(body['scene'],'');self.assertNotIn('enable_generate_file',body)
        path,note=target.request('Summarize',profile,'note-123',session,{'hardwareEdition':'A2','deviceId':456,'sn':'synthetic-serial'})
        self.assertEqual(path,'ai/tingji/v2/chat');self.assertEqual(note['condition'],{'uuids':['note-123']});self.assertTrue(note['enable_generate_file'])
        self.assertEqual(note['hardware_edition'],'A2');self.assertEqual(note['device_id'],'456');self.assertEqual(note['device_no'],'synthetic-serial')
        self.assertEqual(note['session_id'],session)
        with self.assertRaises(ValueError):target.request('',profile)
        with self.assertRaises(ValueError):target.request('x',profile,'bad/id')

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=0
        async def handler(request):
            self.calls+=1
            if request.headers.get('dt-meeting-agent-token')!='secret' or 'Authorization' in request.headers:return web.Response(status=401)
            body=await request.json()
            if body.get('query')=='reject':return web.Response(status=401)
            if body.get('query')=='incomplete':
                return web.Response(text='data: {"success":true,"output":{"content":"partial"}}\n\n',content_type='text/event-stream')
            events=[
                {'type':'status','success':True},
                {'success':True,'task_id':'private-task','output':{'content':'Hel'}},
                {'success':True,'output':{'content':'hidden','is_thinking':True}},
                {'success':True,'output':{'content':'hidden','is_function':True}},
                {'success':True,'output':{'content':'lo'}},
                {'success':True,'task_id':'private-task','status':'Finished','content':'Hello'},
            ]
            return web.Response(text=''.join('data: '+json.dumps(e)+'\n\n' for e in events),content_type='text/event-stream')
        app=web.Application();app.router.add_post('/ai/tingji/v1/chat',handler)
        self.server=TestServer(app);await self.server.start_server();self.origin=str(self.server.make_url('')).rstrip('/')
    async def asyncTearDown(self):await self.server.close()
    async def test_stream_projects_text_only(self):
        text=await target.chat(self.origin,'secret','ai/tingji/v1/chat',{'query':'test'},'test-agent')
        self.assertEqual(text,'Hello');self.assertEqual(self.calls,1)
    async def test_rejection_and_incomplete_are_not_replayed(self):
        for query in ('reject','incomplete'):
            with self.subTest(query=query),self.assertRaises(target.AssistantError):
                await target.chat(self.origin,'secret','ai/tingji/v1/chat',{'query':query},'test-agent')
        self.assertEqual(self.calls,2)

class HistoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[]
        async def handler(request):
            body=await request.json();self.calls.append((request.path,body))
            if request.path=='/ai/tingji/getConversationList':
                first={'session_id':'123e4567-e89b-12d3-a456-426614174000','title':'First synthetic conversation','ds':'20261104'}
                second={'session_id':'123e4567-e89b-12d3-a456-426614174001','title':'Second synthetic conversation','ds':'20261103'}
                return web.json_response({'code':200,'data':[first,first,second]})
            if request.path=='/ai/tingji/getConversationDetails':
                messages=[{'query':'First prompt','content':'First answer','status':'Finished','task_id':'private-task','session_id':body['session_id']}]
                return web.json_response({'code':200,'data':[json.dumps(messages[0])],'condition':'{}'})
            if body.get('task_id')!='private-task':return web.json_response({'code':400,'message':'bad task'})
            status='InProgress' if sum(path=='/tingji/rag/querySummaryResult' for path,_ in self.calls)<3 else 'Finished'
            return web.json_response({'code':200,'data':{'query':'First prompt','content':'Recovered answer' if status=='Finished' else '', 'status':status,'task_id':'private-task'}})
        app=web.Application();app.router.add_post('/ai/tingji/getConversationList',handler);app.router.add_post('/ai/tingji/getConversationDetails',handler);app.router.add_post('/tingji/rag/querySummaryResult',handler)
        self.server=TestServer(app);await self.server.start_server();self.origin=str(self.server.make_url('')).rstrip('/')
    async def asyncTearDown(self):await self.server.close()
    async def test_conversation_list_uses_application_contract_and_deduplicates(self):
        rows=await target.conversations(self.origin,'secret','test-agent')
        self.assertEqual(rows,[
            {'session_id':'123e4567-e89b-12d3-a456-426614174000','title':'First synthetic conversation','date':'20261104'},
            {'session_id':'123e4567-e89b-12d3-a456-426614174001','title':'Second synthetic conversation','date':'20261103'},
        ])
        self.assertEqual(self.calls[0],('/ai/tingji/getConversationList',{'scene':''}))
        self.assertTrue(all('query' not in body for _,body in self.calls))
    async def test_history_uses_application_contract(self):
        session=str(uuid.uuid4())
        messages=await target.history(self.origin,'secret',session,'test-agent')
        self.assertEqual(messages,[{'query':'First prompt','content':'First answer','status':'Finished','task_id':'private-task'}])
        self.assertEqual(self.calls[0],('/ai/tingji/getConversationDetails',{'session_id':session,'scene':''}))
        with self.assertRaises(ValueError):await target.history(self.origin,'secret','bad session','test-agent')
    async def test_recovery_polls_existing_task_without_prompt_replay(self):
        with patch('asyncio.sleep',new=AsyncMock()) as sleep:
            result=await target.recover(self.origin,'secret','private-task','test-agent')
        self.assertEqual(result['content'],'Recovered answer');self.assertEqual(result['status'],'Finished')
        self.assertEqual([call for call in self.calls if call[0]=='/tingji/rag/querySummaryResult'],[('/tingji/rag/querySummaryResult',{'task_id':'private-task','scene':''})]*3)
        self.assertEqual(sleep.await_count,2)
        self.assertTrue(all('query' not in body for _,body in self.calls))

if __name__=='__main__':unittest.main()
