"""Default Markdown summaries; preserve the app payload and selected-summary identity."""
import hashlib
import json
import re
from note_favorites import valid_uuid

DETAIL_URI='/r/Adaptor/PortalMinutesI/minutesDetailV2'
READ_URI='/r/Adaptor/PortalMinutesI/queryMeetingBrief'
WRITE_URI='/r/Adaptor/PortalMinutesI/editFullTextSummary'
REGENERATE_URI='/r/Adaptor/FlashMinutesSummaryAppI/generateMinutesSummary'
MANUAL_REGENERATE_URI='/r/Adaptor/FlashMinutesSummaryAppI/manualRegenerateSummary'
MAX_BYTES=48*1024
KEY='qwen-plus_default'

class Unsupported(ValueError):pass


def validate_text(text):
    if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>MAX_BYTES:
        raise Unsupported('Invalid summary length')
    # shortcut: rich/image summaries stay read-only until their edit representation is verified.
    if (text.lstrip().startswith(('{','[')) or re.search(r'<[A-Za-z/!]|!\[|\x00',text) or
        re.search(r'(?i)(?:downloadToken|x-oss-signature|x-amz-signature|OSSAccessKeyId|security-token)=',text)):
        raise Unsupported('Unsupported summary format')
    return text


def parse(detail,brief,uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identity')
    for reply in (detail,brief):
        if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):
            raise ValueError('Summary read failed')
        body=reply['body']
        if 'code' in body and (type(body['code']) is not int or body['code'] not in (0,200)):raise ValueError('Summary read failed')
    d=detail['body'].get('data');b=brief['body'];full=b.get('fullTextSummary')
    if not isinstance(d,dict) or d.get('uuid')!=uuid or not isinstance(full,dict) or full.get('taskUuid')!=uuid:
        raise ValueError('Summary identity mismatch')
    if (d.get('summaryKey')!=KEY or type(d.get('enableSelfModelSummary')) is not bool or
        type(b.get('extractStatus')) is not int or b['extractStatus']!=2 or d.get('diyTemplateKey')):
        raise Unsupported('Unsupported summary selection')
    value=validate_text(full.get('value'))
    return {'text':value,'full':full,'self_model':d['enableSelfModelSummary'],'key':KEY}


def revision(snapshot):
    return hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def request(uuid,snapshot,text):
    if not valid_uuid(uuid) or snapshot.get('key')!=KEY:raise ValueError('Invalid summary context')
    value=validate_text(text)
    full={'value':value} if snapshot['self_model'] else {**snapshot['full'],'value':value}
    return [{'taskUuid':uuid,'summaryKey':KEY,'fullTextSummary':full}]


def manual_regenerate_request(uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identity')
    return [{'minutesTaskUuid':uuid,'templateId':'default','modelId':'qwen-plus'}]


def regenerate_request(uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identity')
    return [{'minutesTaskUuid':uuid,'summaryTemplateId':'default','modelId':'qwen-plus'}]


def regenerated_content(reply):
    if not acknowledged(reply):raise ValueError('Regeneration was not acknowledged')
    body=reply['body']
    if not isinstance(body,dict) or body.get('containsNoteContent') is not False:
        raise ValueError('Unsupported regeneration state')
    if body.get('taskStatus')=='ongoing' and not body.get('content'):return None
    if body.get('taskStatus')!='succeed' or body.get('webSocketUrl'):
        raise ValueError('Unsupported regeneration state')
    return validate_text(body.get('content'))


def read_regenerated(reply,uuid):
    if not acknowledged(reply):raise ValueError('Summary read failed')
    body=reply.get('body');full=body.get('fullTextSummary') if isinstance(body,dict) else None
    if not isinstance(body,dict) or body.get('extractStatus')!=2 or not isinstance(full,dict):raise ValueError('Summary read failed')
    if not full:return None
    if full.get('taskUuid')!=uuid:raise ValueError('Summary identity mismatch')
    return validate_text(full.get('value'))


def acknowledged(reply):
    if not isinstance(reply,dict) or reply.get('code')!=200:return False
    body=reply.get('body')
    return body is None or isinstance(body,dict) and ('code' not in body or type(body['code']) is int and body['code'] in (0,200))
