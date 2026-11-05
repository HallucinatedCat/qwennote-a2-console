"""Native literal transcript replacement with app-matched timed-word edits."""
import copy
import hashlib
import json
from note_favorites import valid_uuid

READ_URI='/r/Adaptor/MinutesAppI/minutesParagraphList'
WRITE_URI='/r/Adaptor/PortalMinutesI/updateParagraph'
FIELDS=('uid','nickName','startTime','endTime','paragraph','speakerId','subSpeakerUid','subSpeakerNickname','subSpeakerId','content','mark')
MAX_PARAGRAPHS=2000
MAX_CHANGED=20
MAX_MATCHES=1000
MAX_BYTES=1024*1024


def read_request(uuid,cursor=None):
    if not valid_uuid(uuid):raise ValueError('Invalid note identity')
    return [{'taskUuid':uuid,'startTime':cursor['startTime']+1 if cursor else 0,
             'uuid':cursor['uuid'] if cursor else None,'direction':0,'pageSize':200}]


def page(reply):
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):raise ValueError('Transcript read failed')
    body=reply['body']
    if body.get('statusCode')!=200 or type(body.get('code',200)) is not int or body.get('code',200) not in (0,200):raise ValueError('Transcript read rejected')
    # A note with no speech may come back without a paragraph list at all.
    items=body.get('itemList') if body.get('itemList') is not None else [];more=body.get('hasNext',False)
    if not isinstance(items,list) or len(items)>200 or type(more) is not bool:raise ValueError('Invalid transcript page')
    if (more and not items) or (len(items)==200 and 'hasNext' not in body):raise ValueError('Incomplete transcript cursor')
    for row in items:
        if not isinstance(row,dict) or any(type(row.get(k)) is not int or row[k]<0 for k in ('uuid','startTime','endTime')) or row['endTime']<row['startTime'] or not isinstance(row.get('paragraph'),str):raise ValueError('Invalid transcript paragraph')
    return items,more


def snapshot(uuid,items):
    if not valid_uuid(uuid) or len(items)>MAX_PARAGRAPHS or len({p['uuid'] for p in items})!=len(items):raise ValueError('Invalid transcript snapshot')
    result={'task_uuid':uuid,'paragraphs':items}
    if len(json.dumps(result,ensure_ascii=False).encode())>MAX_BYTES:raise ValueError('Transcript too large')
    return result


def view_page(items):
    if len(items)>200 or len({row['uuid'] for row in items})!=len(items):raise ValueError('Invalid transcript view page')
    paragraphs=[]
    for row in items:
        content=row['paragraph'];display=row.get('speakerDisplayModel')
        if not isinstance(content,str) or len(content.encode())>MAX_BYTES:raise ValueError('Transcript text too large')
        if display is not None and not isinstance(display,dict):raise ValueError('Invalid speaker display')
        speaker=(display or {}).get('nickName') or row.get('nickName') or ''
        if not isinstance(speaker,str) or len(speaker)>256:raise ValueError('Invalid speaker name')
        paragraphs.append({'content':content,'speaker':speaker,'start_ms':row['startTime'],'end_ms':row['endTime']})
    if len(json.dumps(paragraphs,ensure_ascii=False).encode())>MAX_BYTES:raise ValueError('Transcript page too large')
    return {'paragraphs':paragraphs}


def revision(current):
    return hashlib.sha256(json.dumps(current,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def modification(row):
    return {'paragraphId':row['uuid'],**{key:copy.deepcopy(row.get(key)) for key in FIELDS}}


def edit(row,needle,replacement):
    # shortcut: refuse marked paragraphs until annotation shifts and note links are verified.
    if row.get('mark'):raise ValueError('Marked paragraphs require annotation preservation')
    content=row.get('content')
    if not isinstance(content,list) or not content:raise ValueError('Timed transcript required')
    tokens=[];last_start=-1
    for sentence in content:
        if not isinstance(sentence,dict) or any(type(sentence.get(k)) is not int for k in ('startTime','endTime')) or not 0<=sentence['startTime']<=sentence['endTime']:raise ValueError('Invalid sentence timing')
        words=sentence.get('words')
        if not isinstance(words,list):raise ValueError('Timed words required')
        for word in words:
            if (not isinstance(word,dict) or not isinstance(word.get('word'),str) or not word['word'] or
                any(type(word.get(k)) is not int for k in ('startTime','endTime')) or word['startTime']<=last_start or word['endTime']<word['startTime']):raise ValueError('Unsupported word timing')
            last_start=word['startTime'];tokens.append({'text':word['word'],'word':word,'sentence':sentence})
        if ''.join(w['word'] for w in words)!=sentence.get('sentence'):raise ValueError('Sentence text mismatch')
    if ''.join(t['text'] for t in tokens)!=row['paragraph']:raise ValueError('Paragraph text mismatch')
    offset=0;shift=0
    while True:
        original_start=row['paragraph'].find(needle,offset)
        if original_start<0:break
        start=original_start+shift;end=start+len(needle);position=0;remaining=[]
        for token in tokens:
            text=token['text'];stop=position+len(text)
            if stop<=start or position>=end:remaining.append(token)
            elif position<start or stop>end:
                token={**token,'text':text[:max(0,start-position)]+text[max(0,end-position):]};remaining.append(token)
            position=stop
        tokens=remaining
        if replacement:
            if not tokens:
                tokens=[{'text':replacement,'word':{'startTime':row['startTime'],'endTime':row['endTime'],'wordId':str(row['uuid'])+'#0'},
                         'sentence':{'uid':row.get('uid'),'startTime':row['startTime'],'endTime':row['endTime']}}]
            else:
                position=0
                for token in tokens:
                    stop=position+len(token['text'])
                    if stop>=start:
                        at=start-position;token['text']=token['text'][:at]+replacement+token['text'][at:];break
                    position=stop
                else:raise ValueError('Insertion offset mismatch')
        offset=original_start+len(needle);shift+=len(replacement)-len(needle)
    sentences=[]
    for token in tokens:
        source=token['sentence'];word={k:token['word'].get(k) for k in ('startTime','endTime','wordId')};word['word']=token['text']
        if not sentences or sentences[-1]['startTime']!=source['startTime']:
            sentences.append({**{k:source.get(k) for k in ('uid','startTime','endTime')},'sentence':'','words':[]})
        sentences[-1]['sentence']+=token['text'];sentences[-1]['words'].append(word)
    desired=row['paragraph'].replace(needle,replacement)
    if ''.join(s['sentence'] for s in sentences)!=desired:raise ValueError('Replacement reconstruction mismatch')
    return {**modification(row),'paragraph':desired,'content':sentences,'mark':None}


def validate_terms(needle,replacement):
    if not isinstance(needle,str) or not needle or not isinstance(replacement,str) or max(len(needle),len(replacement))>1000:raise ValueError('Invalid replacement text')
    needle.encode();replacement.encode()
    if '\x00' in needle or '\x00' in replacement:raise ValueError('Invalid replacement text')


def plan(current,needle,replacement):
    validate_terms(needle,replacement)
    changes=[];matches=0
    if needle!=replacement and sum(row['paragraph'].count(needle) for row in current['paragraphs'])>MAX_MATCHES:raise ValueError('Too many matches')
    if needle!=replacement:
        for row in current['paragraphs']:
            count=row['paragraph'].count(needle)
            if count:
                changes.append(edit(row,needle,replacement));matches+=count
    if len(changes)>MAX_CHANGED:raise ValueError('Too many changed paragraphs for one batch')
    request=[{'taskUuid':current['task_uuid'],'modifyParagraphs':changes}]
    if len(json.dumps(request,ensure_ascii=False).encode())>MAX_BYTES:raise ValueError('Replacement too large')
    return request,matches


def semantic(current):
    # The server renumbers word IDs on replacement; verify text/timing, retain raw IDs in the journal.
    rows={}
    for row in current['paragraphs']:
        item=modification(row);item['mark']=item['mark'] or []
        item['content']=[{**{k:s.get(k) for k in ('uid','startTime','endTime','sentence')},
                          'words':[{k:w.get(k) for k in ('startTime','endTime','word')} for w in s.get('words',[])]}
                         for s in row.get('content',[])]
        rows[row['uuid']]=item
    return rows


def desired(current,request):
    changed={row['paragraphId']:row for row in request[0]['modifyParagraphs']}
    return {'task_uuid':current['task_uuid'],'paragraphs':[
        {**row,**{k:v for k,v in changed[row['uuid']].items() if k!='paragraphId'}} if row['uuid'] in changed else row
        for row in current['paragraphs']]}


def preview(current,request,matches,pending=False):
    changed={row['paragraphId']:row for row in request[0]['modifyParagraphs']}
    return {'revision':revision(current),'matches':matches,'paragraphs_changed':len(changed),'pending':pending,
            'changes':[{'paragraph':i+1,'start_ms':row['startTime'],'before':row['paragraph'],'after':changed[row['uuid']]['paragraph']}
                       for i,row in enumerate(current['paragraphs']) if row['uuid'] in changed]}
