"""Built-in QwenNote AI Assistant application transport.

The native service supplies user metadata and a short-lived aiAnswer token. Chat
uses HTTPS/SSE on the official QwenNote chat service. Tokens and upstream event
IDs stay in this module/backend; callers receive answer text only.
"""
import asyncio
import json
import time
from urllib.parse import urlsplit
import uuid as uuidlib

import aiohttp

DEFAULT_ORIGIN='https://meeting-ai-tingji.dingtalk.com'
ALLOWED_HOSTS={'meeting-ai-tingji.dingtalk.com','pre-meeting-ai-tingji.dingtalk.com','chat.qwennote.ai','pre-chat.qwennote.ai'}
USER_URI='/r/Adaptor/MinutesAppI/getUserInfo'
TOKEN_URI='/r/Adaptor/MinutesTokenI/generateDingUserIdentifierInfoToken'
MAX_TEXT=32000
MAX_REPLY=1024*1024


class AssistantError(Exception):
    """A safely reportable built-in assistant transport/upstream failure."""


def _body(reply):
    if not isinstance(reply,dict):raise AssistantError('Invalid native assistant response')
    body=reply.get('body',reply)
    if not isinstance(body,dict):raise AssistantError('Invalid native assistant body')
    return body


def user(reply):
    body=_body(reply);context=body.get('context',body)
    if isinstance(context,str):
        try:context=json.loads(context)
        except ValueError:raise AssistantError('Invalid assistant user context') from None
    if not isinstance(context,dict):raise AssistantError('Assistant user context is missing')
    raw_uid=context.get('uid');nick=context.get('nickName')
    try:uid=int(raw_uid)
    except (ValueError,TypeError):raise AssistantError('Assistant user ID is invalid') from None
    if uid<=0 or not isinstance(nick,str) or len(nick)>128 or any(ord(c)<32 for c in nick):
        raise AssistantError('Assistant user metadata is invalid')
    origin=context.get('aiQaSiteDomain') or DEFAULT_ORIGIN
    if not isinstance(origin,str):raise AssistantError('Assistant service origin is invalid')
    parsed=urlsplit(origin)
    if (parsed.scheme!='https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or
        parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/') or
        parsed.port not in (None,443)):
        raise AssistantError('Unrecognized assistant service origin')
    return {'uid':uid,'nick':nick,'origin':origin.rstrip('/')}


def token(reply,now=None):
    body=_body(reply);value=body.get('token');expires=body.get('expireTime')
    if not isinstance(value,str) or not value or len(value)>8192 or any(ord(c)<33 for c in value):
        raise AssistantError('Assistant token response is invalid')
    if type(expires) not in (int,float):raise AssistantError('Assistant token expiry is missing')
    now=time.time() if now is None else now
    seconds=expires/1000 if expires>10**12 else expires
    if not now<seconds<now+8*86400:raise AssistantError('Assistant token expiry is invalid')
    return value,seconds


def request(text,profile,task_uuid=None,session_id=None,device=None):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:
        raise ValueError('Assistant text must contain 1 to 32,000 characters')
    if task_uuid is not None and (not isinstance(task_uuid,str) or len(task_uuid)>128 or
        any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in task_uuid)):
        raise ValueError('Invalid note context')
    session_id=session_id or str(uuidlib.uuid4())
    if not isinstance(session_id,str):raise ValueError('Invalid assistant session')
    try:session_id=str(uuidlib.UUID(session_id))
    except ValueError:raise ValueError('Invalid assistant session') from None
    body={
        'query':text,'nick':profile['nick'],'is_thinking':False,'session_id':session_id,
        'condition':{'uuids':[task_uuid] if task_uuid else []},
        'description':[{'type':'current_user_info','data':{'uid':profile['uid'],'nick':profile['nick']}}],
        'scene':'',
    }
    if task_uuid:
        body['enable_generate_file']=True
        if isinstance(device,dict) and isinstance(device.get('hardwareEdition'),str) and device['hardwareEdition']:
            body['hardware_edition']=device['hardwareEdition'][:64]
            if device.get('deviceId') is not None:body['device_id']=str(device['deviceId'])[:128]
            if isinstance(device.get('sn'),str) and device['sn']:body['device_no']=device['sn'][:256]
    return ('ai/tingji/v2/chat' if task_uuid else 'ai/tingji/v1/chat'),body


def _event(raw,event_sink=None):
    try:event=json.loads(raw)
    except ValueError:raise AssistantError('Invalid assistant event') from None
    if not isinstance(event,dict):raise AssistantError('Invalid assistant event object')
    if event_sink is not None:event_sink(event)
    if event.get('type')=='status':return None
    if event.get('success') is not True:raise AssistantError('The built-in assistant rejected the request')
    return event


async def chat(origin,credential,path,body,user_agent,event_sink=None):
    """One explicit request; never replay after a transport failure or cancellation."""
    if path not in {'ai/tingji/v1/chat','ai/tingji/v2/chat'}:raise ValueError('Invalid chat path')
    url=origin.rstrip('/')+'/'+path
    headers={
        'Accept':'text/event-stream','Content-Type':'application/json','dt-meeting-agent-token':credential,
        'Origin':'https://n.dingtalk.com','Referer':'https://n.dingtalk.com/dingding/minutes-ai-agent/',
        'User-Agent':user_agent,
    }
    timeout=aiohttp.ClientTimeout(total=120,connect=20,sock_connect=20,sock_read=60)
    chunks=[];size=0;events=0;finished=False;pending=[];received=0

    def consume(raw):
        nonlocal size,events,finished
        events+=1
        if events>5000:raise AssistantError('Too many assistant events')
        event=_event(raw,event_sink)
        if event is None:return
        if event.get('status')=='Finished':
            finished=True
            if not chunks:
                content=event.get('content') or (event.get('output') or {}).get('content')
                if isinstance(content,str) and content:chunks.append(content)
            return
        output=event.get('output')
        if not isinstance(output,dict) or output.get('is_function') or output.get('is_thinking') or event.get('is_thinking'):return
        content=output.get('content')
        if isinstance(content,str) and content:
            chunks.append(content);size+=len(content.encode())
            if size>MAX_REPLY:raise AssistantError('The assistant response was larger than 1 MiB')

    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(url,headers=headers,json=body,allow_redirects=False) as response:
                if response.status!=200:raise AssistantError('The built-in assistant service rejected the connection')
                async for raw in response.content:
                    received+=len(raw)
                    if received>4*MAX_REPLY:raise AssistantError('The assistant event stream was too large')
                    line=raw.decode('utf-8').rstrip('\r\n')
                    if not line:
                        if pending:consume('\n'.join(pending));pending=[]
                        if finished:break
                    elif line.startswith('data:'):
                        value=line[5:];pending.append(value[1:] if value.startswith(' ') else value)
                if pending and not finished:consume('\n'.join(pending))
    except AssistantError:raise
    except (aiohttp.ClientError,TimeoutError,UnicodeError) as error:
        raise AssistantError('The built-in assistant stream did not complete; no automatic retry was sent') from error
    text=''.join(chunks).strip()
    if not finished or not text:raise AssistantError('The built-in assistant returned no complete text answer')
    return text


async def _post(origin,credential,path,body,user_agent):
    if path not in {'ai/tingji/getConversationList','ai/tingji/getConversationDetails','tingji/rag/querySummaryResult'}:
        raise ValueError('Invalid assistant read path')
    headers={
        'Accept':'application/json','Content-Type':'application/json','dt-meeting-agent-token':credential,
        'Origin':'https://n.dingtalk.com','Referer':'https://n.dingtalk.com/dingding/minutes-ai-agent/',
        'User-Agent':user_agent,
    }
    timeout=aiohttp.ClientTimeout(total=30,connect=20,sock_connect=20,sock_read=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(origin.rstrip('/')+'/'+path,headers=headers,json=body,allow_redirects=False) as response:
                if response.status!=200:raise AssistantError('The built-in assistant service rejected the read')
                raw=await response.content.read(MAX_REPLY+1)
                if len(raw)>MAX_REPLY:raise AssistantError('The assistant read response was larger than 1 MiB')
        result=json.loads(raw)
        if not isinstance(result,dict) or result.get('code')!=200:raise AssistantError('The built-in assistant read was rejected')
        return result.get('data')
    except AssistantError:raise
    except (aiohttp.ClientError,TimeoutError,UnicodeError,ValueError) as error:
        raise AssistantError('The built-in assistant read did not complete') from error


def _message(event):
    if not isinstance(event,dict):raise AssistantError('Invalid assistant history message')
    query=event.get('query') or ''
    content=event.get('content') or (event.get('output') or {}).get('content') or ''
    status=event.get('status') or ''
    task_id=event.get('task_id') or event.get('taskId')
    if (not isinstance(query,str) or len(query)>MAX_TEXT or not isinstance(content,str) or len(content.encode())>MAX_REPLY or
        not isinstance(status,str) or len(status)>32 or (task_id is not None and (not isinstance(task_id,str) or not 1<=len(task_id)<=128))):
        raise AssistantError('Invalid assistant history fields')
    return {'query':query,'content':content,'status':status,'task_id':task_id}


async def conversations(origin,credential,user_agent,scene=''):
    """Read at most 100 vendor conversations; IDs remain inside the backend."""
    if not isinstance(scene,str) or len(scene)>64:raise ValueError('Invalid assistant scene')
    data=await _post(origin,credential,'ai/tingji/getConversationList',{'scene':scene},user_agent)
    if not isinstance(data,list) or len(data)>100:raise AssistantError('Invalid assistant conversation list')
    rows=[];seen=set()
    for item in data:
        if not isinstance(item,dict):raise AssistantError('Invalid assistant conversation entry')
        try:session_id=str(uuidlib.UUID(item.get('session_id')))
        except (ValueError,TypeError,AttributeError):raise AssistantError('Invalid assistant conversation identity') from None
        title=item.get('title') or '';date=item.get('ds') or ''
        if not isinstance(title,str) or len(title)>512 or not isinstance(date,str) or len(date)>32:
            raise AssistantError('Invalid assistant conversation metadata')
        if session_id in seen:continue
        seen.add(session_id);rows.append({'session_id':session_id,'title':title,'date':date})
    return rows


async def history(origin,credential,session_id,user_agent,scene=''):
    """Read the current vendor conversation; no prompt is replayed."""
    try:session_id=str(uuidlib.UUID(session_id))
    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid assistant session') from None
    if not isinstance(scene,str) or len(scene)>64:raise ValueError('Invalid assistant scene')
    data=await _post(origin,credential,'ai/tingji/getConversationDetails',{'session_id':session_id,'scene':scene},user_agent)
    if not isinstance(data,list) or len(data)>1000:raise AssistantError('Invalid assistant history response')
    messages=[];size=0
    for raw in data:
        if not isinstance(raw,str):raise AssistantError('Invalid encoded assistant history message')
        try:event=json.loads(raw)
        except ValueError:raise AssistantError('Invalid encoded assistant history message') from None
        message=_message(event);size+=len(message['content'].encode())
        if size>MAX_REPLY:raise AssistantError('The assistant history was larger than 1 MiB')
        messages.append(message)
    return messages


async def recover(origin,credential,task_id,user_agent,scene=''):
    """Poll an existing server task, matching the application recovery path."""
    allowed='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'
    if not isinstance(task_id,str) or not 1<=len(task_id)<=128 or any(c not in allowed for c in task_id):
        raise ValueError('Invalid assistant task')
    if not isinstance(scene,str) or len(scene)>64:raise ValueError('Invalid assistant scene')
    async with asyncio.timeout(120):
        for attempt,delay in enumerate((2,4,8,16,16,16,0)):
            data=await _post(origin,credential,'tingji/rag/querySummaryResult',{'task_id':task_id,'scene':scene},user_agent)
            message=_message(data)
            if message['status']!='InProgress':
                if message['status']!='Finished' or not message['content'].strip():
                    raise AssistantError('The built-in assistant returned no complete recovered answer')
                return message
            if delay:await asyncio.sleep(delay)
    raise AssistantError('The built-in assistant recovery did not complete')
