"""Transcript parsing modelled on the official app, and bounded UI state.

Transport acceptance and hardware delivery remain unverified. The reducer's
late-update policy is local behavior, not a claim about vendor replay guarantees.
"""
from collections import OrderedDict
import hashlib
import hmac
import json
import secrets

MAX_CONTENT=256*1024
MAX_TEXT=32*1024
LONG_MAX=2**63-1


def integer(value,default=0):
    if isinstance(value,bool):return default
    if isinstance(value,int) and -LONG_MAX<=value<=LONG_MAX:return value
    if isinstance(value,str):
        try:
            result=int(value)
            if -LONG_MAX<=result<=LONG_MAX:return result
        except ValueError:pass
    return default


def text(value):
    return value if isinstance(value,str) else ''


def map_start_time(start,conference_start=0,record_start=0,timestamp=0):
    """Map as the official app does; only map when the real timeline origins are known."""
    start=max(0,integer(start))
    conference_start=integer(conference_start);record_start=integer(record_start);timestamp=integer(timestamp)
    if timestamp>0 and record_start>0:return max(0,timestamp-record_start)
    if conference_start>0 and record_start>0:return max(0,min(LONG_MAX,start+conference_start-record_start))
    return start


def parse_transcript(payload,*,expected_task=None,conference_start=0,record_start=0):
    if not isinstance(payload,dict) or payload.get('eventEnum')!=4:return None
    task=integer(payload.get('taskId'))
    if task<=0 or (expected_task is not None and task!=integer(expected_task)):return None
    content=payload.get('content')
    if not isinstance(content,str) or len(content)>MAX_CONTENT:return None
    try:body=json.loads(content)
    except (ValueError,RecursionError):return None
    if not isinstance(body,dict):return None
    stable=body.get('stableResult',[])
    if not isinstance(stable,list) or len(stable)>512:return None
    dynamic=body.get('dynamicResult',{})
    if not isinstance(dynamic,dict):dynamic={}
    stable_text=''.join(text(item.get('result')) for item in stable if isinstance(item,dict))
    dynamic_text=text(dynamic.get('result'))
    combined=stable_text+dynamic_text
    if not combined.strip() or len(combined)>MAX_TEXT:return None
    first=stable[0] if stable and isinstance(stable[0],dict) else {}
    language=text(first.get('targetLang'))
    if not language.strip():language=text(dynamic.get('targetLang'))
    index=integer(first.get('index'),-1)
    if index<0:index=integer(dynamic.get('index'),-1)
    sequence=integer(payload.get('sequence'))
    if sequence<=0:sequence=max(0,integer(body.get('index')))
    timestamp=max(0,integer(body.get('timestamp')))
    return {'task_id':task,'record_id':integer(payload.get('recordId')),
            'sequence':sequence,'paragraph_index':index,'text':combined,
            'stable_text':stable_text,'provisional_text':dynamic_text,
            'final':payload.get('textType')=='ParagraphEnd',
            'translated':body.get('isTranslateResult') is True,'language':language[:64],
            'start_ms':map_start_time(body.get('startTime'),conference_start,record_start,timestamp),
            'timestamp_ms':timestamp or None,
            'timeline':'recording' if integer(record_start)>0 and (timestamp>0 or integer(conference_start)>0) else 'source-relative'}


class TranscriptStore:
    def __init__(self,max_rows=256,max_bytes=1024*1024):
        self.rows=OrderedDict();self.sizes={};self.bytes=0
        self.max_rows=max_rows;self.max_bytes=max_bytes
        self.salt=secrets.token_bytes(32)
        self.generation=secrets.token_hex(8)
        self.revision=0;self.evicted=0;self.possible_gaps=False

    def mark_reconnect(self):
        self.possible_gaps=True
        self.revision+=1

    def accept(self,payload,*,source='dtiot.dinger'):
        if source not in ('dtiot.dinger','doc.transcribe'):raise ValueError('Unknown transcript source')
        parsed=parse_transcript(payload)
        if parsed is None:return None
        identity=json.dumps([source,parsed.pop('task_id'),parsed.pop('record_id')],separators=(',',':')).encode()
        stream=hmac.new(self.salt,identity,hashlib.sha256).hexdigest()[:20]
        index=parsed['paragraph_index']
        # Without a paragraph index, don't pretend successive messages are the same paragraph.
        position=('index',index) if index>=0 else ('sequence',parsed['sequence'],parsed['start_ms'])
        key=(stream,parsed['language'],parsed['translated'],position)
        row={'stream':stream,'source':source,**parsed}
        previous=self.rows.get(key)
        if previous:
            if row['sequence']<previous['sequence']:return None
            if previous['final'] and not row['final']:return None
            if row==previous:return None
        size=len(json.dumps(row,ensure_ascii=False).encode())
        if size>self.max_bytes:return None
        self.bytes-=self.sizes.get(key,0)
        self.rows[key]=row;self.sizes[key]=size;self.bytes+=size
        while len(self.rows)>self.max_rows or self.bytes>self.max_bytes:
            oldest,_=self.rows.popitem(last=False)
            self.bytes-=self.sizes.pop(oldest);self.evicted+=1
        self.revision+=1
        return dict(row)

    def snapshot(self):
        rows=sorted(self.rows.values(),key=lambda r:(r['stream'],r['start_ms'],r['paragraph_index'],r['sequence'],r['language']))
        return {'generation':self.generation,'revision':self.revision,'rows':[dict(r) for r in rows],
                'evicted':self.evicted,'possible_reconnect_gaps':self.possible_gaps,
                'live_delivery_verified':False}
