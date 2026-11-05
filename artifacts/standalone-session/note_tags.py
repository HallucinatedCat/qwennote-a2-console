"""Application note tags: list, assign, and create/rename/delete tag definitions."""
import hashlib
import json
from note_favorites import valid_uuid,DETAIL_URI
from notification_settings import handle

LIST_URI='/r/Adaptor/MinutesTagI/getTagList'
READ_URI='/r/Adaptor/MinutesTagI/getMinutesTagList'
WRITE_URI='/r/Adaptor/MinutesTagI/updateMinutesTagList'
CREATE_URI='/r/Adaptor/MinutesTagI/addTag'
GET_DEF_URI='/r/Adaptor/MinutesTagI/getTag'
UPDATE_URI='/r/Adaptor/MinutesTagI/updateTag'
DELETE_URI='/r/Adaptor/MinutesTagI/deleteTag'
MAX_TAGS=200


def rows(reply):
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(reply.get('body'),dict):
        raise ValueError('Tag read failed')
    body=reply['body']
    if 'code' in body and (type(body['code']) is not int or body['code'] not in (0,200)):
        raise ValueError('Tag read failed')
    if body.get('hasMore') or body.get('nextToken'):raise ValueError('Incomplete tag list')
    tags=body.get('tagList')
    if not isinstance(tags,list) or len(tags)>MAX_TAGS:raise ValueError('Invalid tag list')
    result={}
    for tag in tags:
        if (not isinstance(tag,dict) or not valid_uuid(tag.get('tagId')) or
            not isinstance(tag.get('displayName'),str) or not tag['displayName'].strip() or len(tag['displayName'])>256 or
            type(tag.get('autoMatch')) is not bool or tag['tagId'] in result):
            raise ValueError('Invalid tag row')
        result[tag['tagId']]={'label':tag['displayName'],'auto_match':tag['autoMatch']}
    return result


def parse(detail,available,assigned,uuid):
    data=detail.get('body',{}).get('data') if isinstance(detail,dict) and isinstance(detail.get('body'),dict) else None
    if (not valid_uuid(uuid) or not isinstance(detail,dict) or detail.get('code')!=200 or
        not isinstance(data,dict) or data.get('uuid')!=uuid or
        ('code' in detail['body'] and (type(detail['body']['code']) is not int or detail['body']['code'] not in (0,200)))):raise ValueError('Note identity mismatch')
    tags=rows(available);selected=rows(assigned)
    if any(key not in tags or tags[key]!=value for key,value in selected.items()):raise ValueError('Tag catalog changed or incomplete')
    return {'task_uuid':uuid,'tags':tags,'selected':sorted(selected)}


def revision(snapshot):
    return hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def public(secret,snapshot,pending=False):
    return {'options':[{'handle':handle(secret,'note-tag:'+snapshot['task_uuid']+':'+key),**tag,'selected':key in snapshot['selected']}
                       for key,tag in snapshot['tags'].items()],
            'revision':revision(snapshot),'verified':True,'pending':pending}


def resolve(secret,snapshot,selection):
    if (not isinstance(selection,list) or len(selection)>MAX_TAGS or
        any(not isinstance(value,str) for value in selection) or len(set(selection))!=len(selection)):
        raise ValueError('Invalid tag selection')
    allowed={handle(secret,'note-tag:'+snapshot['task_uuid']+':'+key):key for key in snapshot['tags']}
    if any(value not in allowed for value in selection):raise ValueError('Tag option expired')
    return sorted(allowed[value] for value in selection)


def create_request(name):
    if not isinstance(name,str):raise ValueError('Invalid tag name')
    name=name.strip()
    if not name or len(name.encode('utf-16-le'))>30 or any(ord(c)<32 or ord(c)==127 for c in name):
        raise ValueError('Invalid tag name')
    return [{'displayName':name,'autoMatch':False}]


def created_tag(saved,current):
    # A lost receipt cannot establish which concurrent creation belongs to this request.
    key=saved.get('created_id')
    return key if (key and key not in saved['original']['tags'] and
                   current['tags'].get(key)=={'label':saved['name'],'auto_match':False}) else None


def request(uuid,ids):
    if not valid_uuid(uuid) or not isinstance(ids,list) or len(ids)>MAX_TAGS or any(not valid_uuid(x) for x in ids) or len(set(ids))!=len(ids):
        raise ValueError('Invalid tag assignment')
    return [{'taskUuid':uuid,'tagIdList':ids}]


def definition(reply,tag_id,current):
    body=reply.get('body') if isinstance(reply,dict) else None;model=body.get('tagInfoModel') if isinstance(body,dict) else None
    description=model.get('description','') if isinstance(model,dict) else None
    if (not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(model,dict) or
        ('code' in body and (type(body['code']) is not int or body['code'] not in (0,200))) or
        not valid_uuid(tag_id) or ('tagId' in model and model['tagId']!=tag_id) or
        model.get('displayName')!=current.get('label') or model.get('autoMatch') is not current.get('auto_match') or
        not isinstance(description,str) or len(description)>16384):
        raise ValueError('Tag definition mismatch')
    return {'label':model['displayName'],'auto_match':model['autoMatch'],'description':description}


def rename_request(tag_id,name,auto_match,description):
    if not valid_uuid(tag_id) or type(auto_match) is not bool or not isinstance(description,str) or len(description)>16384:
        raise ValueError('Invalid tag definition')
    name=create_request(name)[0]['displayName']
    return [{'tagId':tag_id,'tagInfoModel':{'displayName':name,'autoMatch':auto_match,'description':description}}]


def tag_request(tag_id):
    if not valid_uuid(tag_id):raise ValueError('Invalid tag identifier')
    return [{'tagId':tag_id}]


def definition_reconciliation(saved,current):
    original=saved.get('original');tag_id=saved.get('tag_id');operation=saved.get('operation')
    if not isinstance(original,dict) or not valid_uuid(tag_id) or current.get('selected')!=original.get('selected'):return None
    before=original.get('tags',{}).get(tag_id)
    if not isinstance(before,dict):return None
    if {k:v for k,v in current.get('tags',{}).items() if k!=tag_id}!={k:v for k,v in original['tags'].items() if k!=tag_id}:return None
    observed=current['tags'].get(tag_id)
    if operation=='rename':
        requested={'label':saved.get('name'),'auto_match':saved.get('auto_match',before['auto_match'])}
        return 'original' if observed==before else 'requested' if observed==requested else None
    if operation=='delete':return 'requested' if observed is None else 'original' if observed==before else None
    return None
