"""Interpreter configuration modelled on the official app and offline request builders.

Builders do not send RPC, publish media or play audio.
These conference-scoped contracts do not provide arbitrary assistant speech.
"""
import hashlib
import hmac
import json
import re
import secrets
from rtc_protocol import private_string
from public_errors import Refused

CONFIG_URI='/r/Adaptor/MoziConfAiInterpreterAppI/getAiInterpreterConfig'
SUBSCRIBE_URI='/r/Adaptor/MoziConfAiInterpreterAppI/subscribeAiInterpreterChannel'
SCENES=('a1_transcribe','a1_face2face')


def scene(value):
    if value not in SCENES:raise Refused('Unsupported device interpreter scene')
    return value


def config_request(conference_id=None,scene_type='a1_transcribe'):
    return [{'requestId':secrets.token_hex(16),
             'conferenceId':None if conference_id is None else private_string(conference_id),
             'scene':scene(scene_type)}]


def label(value,limit=256):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('Invalid interpreter option field')
    return value


def language(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}',value):
        raise ValueError('Invalid language identifier')
    return value


def parse_config(reply):
    body=reply.get('body') if isinstance(reply,dict) else None
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(body,dict) or type(body.get('code')) is not int or body['code'] not in (0,200):
        raise ValueError('Interpreter configuration rejected')
    rows=body.get('supportedLanguages')
    if not isinstance(rows,list) or len(rows)>256:raise ValueError('Invalid interpreter language list')
    result={}
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Invalid interpreter language')
        lang=language(row.get('lang'))
        if lang in result:raise ValueError('Duplicate interpreter language')
        voices=row.get('supportVoices') or []
        if not isinstance(voices,list) or len(voices)>128:raise ValueError('Invalid interpreter voices')
        choices={}
        for voice in voices:
            if not isinstance(voice,dict):raise ValueError('Invalid interpreter voice')
            identifier=private_string(voice.get('voiceId'),256)
            if identifier in choices:raise ValueError('Duplicate interpreter voice')
            choices[identifier]={'name':label(voice.get('name') or 'Voice')}
        result[lang]={'label':label(row.get('displayName') or lang),
                      'native_label':label(row.get('nativeLangName') or row.get('displayName') or lang),
                      'voices':choices}
    return result


def voice_handle(secret,scene_type,lang,voice_id):
    return hmac.new(secret.encode(),json.dumps([scene(scene_type),lang,voice_id],separators=(',',':')).encode(),hashlib.sha256).hexdigest()


def descriptor(options,secret,scene_type='a1_transcribe'):
    return {'scene':scene(scene_type),'languages':[{'language':lang,'label':row['label'],
            'native_label':row['native_label'],'voices':[{'handle':voice_handle(secret,scene_type,lang,vid),'label':voice['name']}
            for vid,voice in row['voices'].items()]} for lang,row in options.items()],
            'configuration_read_only':True,'translation_start_available':False,'playback_available':False,
            'verification':'Vendor configuration only; device translation and playback unverified'}


def subscription_request(receipt,options,secret,selections,*,scene_type='a1_transcribe',source_language='auto'):
    """Construct a reviewed request only. No caller is wired to send subscriptions."""
    scene(scene_type)
    if not isinstance(selections,list) or not 1<=len(selections)<=2:raise ValueError('One or two language selections required')
    if scene_type=='a1_transcribe' and len(selections)!=1:raise ValueError('Transcription scene requires one target')
    if scene_type=='a1_face2face' and len(selections)!=2:raise ValueError('Bidirectional scene requires two targets')
    if source_language!='auto' and source_language not in options:raise ValueError('Unknown source language')
    targets=[];seen=set()
    for selection in selections:
        if not isinstance(selection,dict) or set(selection)!={'language','voice_handle'}:raise ValueError('Invalid selection')
        lang=selection['language'];handle=selection['voice_handle']
        if not isinstance(lang,str) or lang not in options or lang in seen or not isinstance(handle,str):raise ValueError('Invalid target')
        matches=[vid for vid in options[lang]['voices'] if hmac.compare_digest(voice_handle(secret,scene_type,lang,vid),handle)]
        if len(matches)!=1:raise ValueError('Voice selection is stale or unsupported')
        targets.append({'lang':lang,'voiceId':matches[0]});seen.add(lang)
    return [{'requestId':secrets.token_hex(16),'conferenceId':private_string(receipt.conference_id),
             'targetLanguages':targets,'scene':scene_type,
             'sourceLanguage':'multilingual' if scene_type=='a1_face2face' else source_language}]


def unsubscribe_request(receipt,scene_type='a1_transcribe'):
    return [{'requestId':secrets.token_hex(16),'conferenceId':private_string(receipt.conference_id),
             'targetLanguages':[],'scene':scene(scene_type)}]
