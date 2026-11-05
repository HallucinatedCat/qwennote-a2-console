"""Read/generate translated note views without changing the original or its preferences."""
import re
from note_favorites import valid_uuid

OPTIONS_URI='/r/Adaptor/MinutesAppI/minutesSupportLanguages'
TRANSCRIPT_URI='/r/Adaptor/MinutesAppI/minutesParagraphTranslateList'
SUMMARY_URI='/r/Adaptor/PortalMinutesI/translateMeetingBrief'
MAX_BYTES=1024*1024
LANGUAGE=re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)?')


def valid_language(value):
    return isinstance(value,str) and LANGUAGE.fullmatch(value) is not None


def options_request(uuid):
    if not valid_uuid(uuid):raise ValueError('Invalid note identity')
    return [{'sourceLang':'zh','needReturnSource':True,'taskUuid':uuid,'enableUseNewLanguageList':True}]


def transcript_request(uuid,source,target,cursor=None):
    if not valid_uuid(uuid) or not valid_language(source) or not valid_language(target) or source==target:raise ValueError('Invalid translation selection')
    return [{'taskUuid':uuid,'startTime':cursor['startTime'] if cursor else 0,'uuid':cursor['uuid'] if cursor else None,
             'direction':0,'pageSize':200,'sourceLang':source,'targetLang':target}]


def summary_request(uuid,source,target):
    if not valid_uuid(uuid) or not valid_language(source) or not valid_language(target) or source==target:raise ValueError('Invalid translation selection')
    return [{'uuid':uuid,'sourceLang':source,'targetLang':target,'summaryKey':'qwen-plus_default'}]


def source_language(reply):
    try:source=reply['body']['lang']['langKey']
    except (KeyError,TypeError):raise ValueError('Source language unavailable') from None
    if not valid_language(source):raise ValueError('Invalid source language')
    return source


def language_options(reply,source):
    if not valid_language(source) or not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):raise ValueError('Language read failed')
    body=reply['body'];languages=body.get('languages')
    if body.get('statusCode')!=200 or not isinstance(languages,list) or not 1<=len(languages)<=128:raise ValueError('Language read rejected')
    result=[];seen=set()
    for row in languages:
        if not isinstance(row,dict) or not valid_language(row.get('langKey')) or row['langKey'] in seen:raise ValueError('Invalid language option')
        key=row['langKey'];seen.add(key);label=row.get('nativeLang') or row.get('langText')
        if not isinstance(label,str) or not label.strip() or len(label)>128 or row.get('commerceStatus')!=1:raise ValueError('Invalid language option')
        if key!=source and row.get('nativeLang')!='Original (Not Translated)':result.append({'key':key,'label':label})
    return result


def brief_items(rows,uuid,keypoint):
    if rows is None:return []
    if not isinstance(rows,list) or len(rows)>512:raise ValueError('Invalid translated brief section')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('taskUuid',uuid)!=uuid:raise ValueError('Invalid translated brief item')
        topic=row.get('topic','') if keypoint else ''
        text=row.get('summary',row.get('value','')) if keypoint else row.get('value','')
        start=row.get('beginTime');end=row.get('endTime')
        if not isinstance(topic,str) or not isinstance(text,str) or any(type(x) is not int or x<0 for x in (start,end) if x is not None) or (start is not None and end is not None and end<start):
            raise ValueError('Invalid translated brief item')
        result.append({'topic':topic,'summary':text,'start_ms':start,'end_ms':end} if keypoint else {'content':text,'start_ms':start,'end_ms':end})
    return result


def summary_result(reply,uuid):
    if not valid_uuid(uuid) or not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):raise ValueError('Summary translation failed')
    body=reply['body'];full=body.get('fullTextSummary');title=body.get('title','')
    if not isinstance(full,dict) or full.get('taskUuid')!=uuid or not isinstance(full.get('value'),str) or not isinstance(title,str):raise ValueError('Invalid translated summary')
    text=full['value']
    if len(text.encode())>MAX_BYTES or len(title)>1000:raise ValueError('Translated summary too large')
    result={'title':title,'summary':text,'keypoints':brief_items(body.get('keypoints'),uuid,True),'actions':brief_items(body.get('tingwuActions'),uuid,False)}
    size=len(text.encode())+len(title.encode())
    for row in result['keypoints']+result['actions']:size+=sum(len(value.encode()) for value in row.values() if isinstance(value,str))
    if size>MAX_BYTES:raise ValueError('Translated brief too large')
    return result
