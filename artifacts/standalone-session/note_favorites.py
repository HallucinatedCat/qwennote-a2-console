"""Application-path favorites (PortalMinutesI); no MCP writes."""
import json
import re

DETAIL_URI='/r/Adaptor/PortalMinutesI/minutesDetailV2'
COLLECT_URI='/r/Adaptor/PortalMinutesI/collectMinutes'


def valid_uuid(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value) is not None


def listed_notes(result):
    """Only the owner's list_my_minutes response may establish writable note context."""
    if not isinstance(result,dict) or result.get('isError'):return []
    body=result.get('structuredContent')
    if not isinstance(body,dict):
        body={}
        for item in result.get('content',[]):
            if isinstance(item,dict) and item.get('type')=='text' and isinstance(item.get('text'),str):
                if len(item['text'])>1024*1024:return []
                try:body=json.loads(item['text'])
                except (ValueError,RecursionError):return []
                break
    rows=body.get('items',[]) if isinstance(body,dict) else []
    if not isinstance(rows,list) or len(rows)>20:return []
    return [r['uuid'] for r in rows if isinstance(r,dict) and valid_uuid(r.get('uuid'))]


def detail_request(uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identifier')
    return [{'uuid':uuid,'enableMinutesDetailTab':True}]


def collected(reply,uuid):
    body=reply.get('body') if isinstance(reply,dict) else None
    data=body.get('data') if isinstance(body,dict) else None
    if (not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(data,dict) or
        data.get('uuid')!=uuid or type(data.get('collected')) is not bool):
        raise ValueError('Favorite state unavailable or mismatched')
    return data['collected']


def collect_request(uuid,desired):
    if not valid_uuid(uuid) or type(desired) is not bool:raise ValueError('Invalid favorite request')
    return [{'taskUuid':uuid,'type':1 if desired else 2}]


def acknowledged(reply):
    body=reply.get('body') if isinstance(reply,dict) else None
    return (isinstance(reply,dict) and reply.get('code')==200 and isinstance(body,dict)
            and (body.get('code') is None or type(body['code']) is int and body['code'] in (0,200)))
