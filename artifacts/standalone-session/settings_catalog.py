"""Evidence-backed setting metadata. Reported fields are not blanket write authority."""
import hashlib
import json
from generation_settings import KEYS as GENERATION_KEYS,CHOICES as GENERATION_CHOICES,LABELS as GENERATION_LABELS
from public_errors import Refused

# Values with an observed generic device write path and bounded known choices.
SIMPLE={
 'storage_type':('Storage mode',{'0':'Cloud storage enabled','-1':'Cloud storage disabled'}),
 'enable_incognito_mode':('Incognito mode',{'0':'Off','1':'On'}),
 'enable_incognito_transcribe':('Transcribe in incognito mode',{'0':'Off','1':'On'}),
 'auto_realtime_asr':('Automatic real-time transcription',{'0':'Enabled','-1':'Disabled'}),
 'auto_generate_minutes':('Generate summaries automatically',{'0':'Manual','1':'Automatic'}),
 'enable_record_location':('Record location',{'0':'Off','1':'On'}),
 # Voice-button enum as the official app defines it: the 4G A2 takes it as a cloud setting only.
 'voice_button_function_mode':('Voice button',{'1':'Voice memo','2':'Start and stop recording','4':'Send to me','5':'Send to Wukong'}),
}
METADATA={
 'storage_type':('write-builder','storage_type updateDeviceSetting; changes storage behavior'),
 'enable_incognito_mode':('live-change-restore','UI change and restored original with cloud readback'),
 'enable_incognito_transcribe':('write-builder','updateDeviceSetting builder; belongingType 2'),
 'auto_realtime_asr':('write-builder','The app realtime-ASR toggle writes device-scoped 0/-1; separate from per-record realtime_asr state'),
 'auto_generate_minutes':('write-builder','The app auto-generation batch builder; paired timing controls separate'),
 'auto_generate_minutes_duration':('grouped-write','The app batches mode, duration and duration_limit; units/ranges need UI parity'),
 'auto_generate_minutes_duration_limit':('grouped-write','The app batch writer shows this is configurable, not immutable metadata'),
 'default_source_language':('write-builder','Source-language updateDeviceSetting; options from queryGenerateMinutesConfig'),
 'default_minutes_template':('write-builder','The app template serializer/update helper; JSON template descriptor'),
 'default_llm':('unresolved-writer','Read value and generation model options observed; no proven device-default write builder'),
 'default_speaker_diarization':('unresolved-writer','Read value is a boolean string; generic 0/1 values would be incorrect'),
 'voice_button_function_mode':('write-builder','updateDeviceSetting as the official app sends it (belongingType 2); the device-task aikey_option set is BLE-only and rejected on 4G'),
 'enable_record_location':('write-builder','The app has device-scoped and account-scoped flows; backend only writes selected device override'),
 'enable_rtc_translate':('unresolved-writer','Readable default flag; not the RTC task/controller'),
 'agent_result_notify_type':('notification-editor','Voice/vibration alert method; organization changes are optional and validated separately'),
 'qwen_work_org_id':('notification-editor','Managed by the organization selector; raw identifiers remain private'),
 'has_set_incognito_mode':('internal','Companion bookkeeping flag; no direct user editor'),
 'device_org_id':('internal','Binding/organization metadata; no direct user editor'),
 'message_last_read_time':('internal','Message read-position bookkeeping'),
 'system_message_version':('internal','Server/app message-version bookkeeping'),
 'device_edition':('read-only','Edition/entitlement metadata; no verified preference writer'),
 'device_model':('read-only','Model identity; no verified preference writer'),
 'last_select_source_language':('internal','No known consumer or writer in the official app; name suggests last source-language bookkeeping'),
 'last_select_target_language':('internal','No known consumer or writer in the official app; name suggests last target-language bookkeeping'),
}


# Meaning evidence is separate from write permission and physical verification.
DESCRIPTIONS={
 'storage_type':('Controls the cloud-storage preference for recordings. This flag alone does not prove that recording is offline.', '0 = cloud storage on; -1 = off.', 'source'),
 'enable_incognito_mode':('Selects incognito recording behavior. Cloud change and restore are verified; exact retention effects remain unverified.', '0 = off; 1 = on.', 'source'),
 'enable_incognito_transcribe':('Permits transcription for an incognito recording and is copied into record-task attributes. It does not start a live stream by itself.', '0 = off; 1 = on.', 'source'),
 'auto_realtime_asr':('Device preference for automatic real-time transcription. This is separate from per-record realtime_asr state and incognito transcription permission; physical activation remains unverified.', '0 = enabled; -1 = disabled.', 'source'),
 'auto_generate_minutes':('Selects manual or automatic summary generation. Save together with both duration settings.', '0 = manual; 1 = automatic.', 'source'),
 'auto_generate_minutes_duration':('Minimum recording duration for automatic summaries. Saved with the generation mode and maximum duration.', 'Minutes: 0, 1, 10 or 30.', 'source'),
 'auto_generate_minutes_duration_limit':('Maximum recording duration eligible for automatic summaries. Saved with the mode and minimum duration.', 'Minutes: 0 = unlimited; otherwise 60 to 480 in steps of 60.', 'source'),
 'default_source_language':('Default language for speech recognition. Available choices come from the current language catalog.', 'A language code offered by the current generation configuration.', 'source'),
 'default_minutes_template':('Default summary template. The application saves the selected template description, not just its identifier.', 'A template descriptor from the current generation configuration.', 'source'),
 'default_llm':('Default summary model preference. Model choices can be read; assignment of this device default remains unverified.', 'Model identifier; accepted write format is unverified.', 'inferred'),
 'default_speaker_diarization':('Default speaker-separation preference. Its writer and resulting transcript behavior remain unverified.', 'Boolean strings observed; do not assume numeric 0/1.', 'inferred'),
 'voice_button_function_mode':('What a press of the A2 voice button does.', '1 = voice memo; 2 = start and stop recording; 4 = send to me; 5 = send to Wukong.', 'source'),
 'enable_record_location':('Controls whether location is attached to recordings. Physical location collection has not been verified.', '0 = off; 1 = on.', 'source'),
 'enable_rtc_translate':('A reported realtime-translation flag. It does not start an RTC task or translation subscription by itself.', 'Accepted write values and device effect are unverified.', 'inferred'),
 'agent_result_notify_type':('Selects the notification method for completed assistant/work tasks. Organization routing is a separate preference.', '0 = voice alert; 1 = vibration.', 'source'),
 'qwen_work_org_id':('Selects the QwenWork organization for work-task routing. It is separate from device binding metadata.', 'Choose from the available organizations; raw identifiers stay private.', 'source'),
 'has_set_incognito_mode':('Appears to record whether incognito mode was explicitly configured. Consumers and reset rules are unverified.', 'Flag format and accepted values are unverified.', 'inferred'),
 'device_org_id':('Organization/binding metadata associated with the device. Its precise lifecycle is unverified; this is not an organization selector.', 'Private identifier; no editable value.', 'inferred'),
 'message_last_read_time':('Appears to track the last-read message position. The update trigger and timestamp units are unverified.', 'Private bookkeeping value; units are unverified.', 'inferred'),
 'system_message_version':('Appears to track app/cloud message versions. Comparison rules and the update trigger are unverified.', 'Private bookkeeping value; version format is unverified.', 'inferred'),
 'device_edition':('Device edition or entitlement metadata. This is not an editable subscription or edition control.', 'Edition string; complete mapping is unverified.', 'inferred'),
 'device_model':('Device-model metadata. This is not a model-selection control.', 'Model string; complete mapping is unverified.', 'source'),
 'last_select_source_language':('Name suggests last source-language bookkeeping. The official app has no known consumer or writer; relationship to the default transcript language and encoding remain unverified.', 'Read-only reported value; accepted write values are unverified.', 'inferred'),
 'last_select_target_language':('Name suggests last target-language bookkeeping. The official app has no known consumer or writer; relationship to RTC/assistant translation and encoding remain unverified.', 'Read-only reported value; accepted write values are unverified.', 'inferred'),
}
UNKNOWN_DESCRIPTION=('The application meaning of this reported field has not been established.', 'Accepted values and units are unknown.', 'unknown')


def options(generation):
    config=generation.get('generateMinutesConfig',{}) if isinstance(generation,dict) else {}
    languages={str(row['lang']):str(row.get('localDisplayName') or row.get('displayName') or row['lang']) for row in config.get('sourceLanguages',[]) if isinstance(row,dict) and isinstance(row.get('lang'),str) and row['lang']}
    templates={}
    for row in config.get('minutesTemplates',[]):
        if not isinstance(row,dict) or not isinstance(row.get('templateId'),str):continue
        descriptor={k:row[k] for k in ('description','iconUrl','templateId','title') if isinstance(row.get(k),str)}
        value=json.dumps(descriptor,ensure_ascii=False,separators=(',',':'))
        templates[value]=str(row.get('title') or row['templateId'])
    return {'default_source_language':languages,'default_minutes_template':templates}


def catalog(config,response,generation=None):
    choices=options(generation or {})
    # A device-scope (2) row wins in any order; between scopes 0 and 4 the later row wins.
    selected={}
    for item in response.get('body',{}).get('settingList',[]):
        if not isinstance(item,dict) or str(item.get('accountId'))!=str(config['account_id']) or item.get('accountType')!=config['account_type']:continue
        scope=item.get('belongingType');key=item.get('settingKey');value=item.get('settingValue')
        if scope not in (0,2,4) or not isinstance(key,str) or not isinstance(value,str):continue
        if scope==2 and str(item.get('belongingId'))!=str(config['target_device_id']):continue
        if key in selected and selected[key].get('scope')==2:continue
        evidence,note=METADATA.get(key,('unresolved-writer','No mapped write path'))
        description,value_format,meaning_evidence=DESCRIPTIONS.get(key,UNKNOWN_DESCRIPTION)
        known_choices=SIMPLE.get(key,('',{}))[1] or choices.get(key,{})
        editable=bool(known_choices) and evidence in {'write-builder','live-change-restore'}
        selected[key]={'key':key,'label':SIMPLE.get(key,({'default_source_language':'Transcript language','default_minutes_template':'Summary template'}.get(key,key),{}))[0],
                       'value':None if key in {'device_org_id','qwen_work_org_id','message_last_read_time','system_message_version'} else value,
                       'scope':scope,'inherited':scope!=2,'evidence':evidence,'note':note,
                       'description':description,'value_format':value_format,'meaning_evidence':meaning_evidence,
                       'choices':known_choices,'editable':editable,
                       'exact_restore_supported':editable and scope==2,
                       'requires_persistent_override_confirmation':editable and scope!=2}
    group_available=all(k in selected for k in GENERATION_KEYS)
    group_inherited=any(selected[k]['scope']!=2 for k in GENERATION_KEYS if k in selected)
    for key in GENERATION_KEYS:
        if key in selected:
            selected[key].update(label=GENERATION_LABELS[key],choices=GENERATION_CHOICES[key],editable=group_available,editor='generation',evidence='grouped-write',exact_restore_supported=group_available and not group_inherited,requires_persistent_override_confirmation=group_inherited,note='Use the grouped generation editor; all three preferences are written together, in minutes')
    if 'agent_result_notify_type' in selected:
        selected['agent_result_notify_type'].update(label='Task completion alert',editable=True,editor='notification',choices={'0':'Voice alert','1':'Vibration alert'})
    if 'qwen_work_org_id' in selected:
        selected['qwen_work_org_id'].update(label='QwenWork organization',editable=True,editor='notification')
    return list(selected.values())


def status_part(details):
    """The catalog keys of /api/status and their revision, shared by web_backend and tools/ui_preview.

    The catalog is most of a full status and changes only on a settings refresh, so a client
    that sends back the current revision gets it left out.
    """
    part={'setting_choices':{k:list(row['choices']) for k,row in details.items() if row['editable'] and row.get('editor') not in {'generation','notification'}},
          'setting_details':list(details.values())}
    revision=hashlib.sha256(json.dumps(part,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:16]
    return part,revision


def validate_change(detail,value,persistent_override=False):
    if not detail or not detail.get('editable') or value not in detail.get('choices',{}):raise Refused('Setting/value has no enabled validated builder')
    if detail.get('inherited') and not persistent_override:raise Refused('Creating a device override requires explicit persistent-change confirmation')
    return value
