import json
import tempfile
import time
import unittest
from pathlib import Path
from settings_catalog import catalog,validate_change
from import_session import private_load,select_import

CONFIG={'account_id':'123','account_type':0,'target_device_id':456}

def row(key,value,scope=2):
    return {'settingKey':key,'settingValue':value,'accountId':'123','accountType':0,'belongingId':'456' if scope==2 else 'default','belongingType':scope}

class SettingsCatalogTests(unittest.TestCase):
    def test_internal_fields_not_editable_or_identifying(self):
        response={'body':{'settingList':[row('device_org_id','private-org'),row('device_model','QwenNote A2'),row('enable_rtc_translate','1',4)]}}
        result=catalog(CONFIG,response)
        self.assertTrue(all(not r['editable'] for r in result))
        self.assertIsNone(result[0]['value'])
        self.assertNotIn('private-org',json.dumps(result))

    def test_device_override_beats_inherited_regardless_of_order(self):
        inherited=row('enable_incognito_mode','0',4);own=row('enable_incognito_mode','1')
        for rows in ([inherited,own],[own,inherited]):
            detail=catalog(CONFIG,{'body':{'settingList':rows}})[0]
            self.assertEqual(detail['value'],'1');self.assertTrue(detail['exact_restore_supported'])

    def test_account_and_product_rows_resolve_by_response_order(self):
        # Pinned current behaviour, not a verified vendor rule: without a device row, the later of
        # an account (0) and a product (4) row wins. The official Android client keeps the last row
        # per key and the H5 editor the first; neither compares belongingType.
        account=row('enable_incognito_mode','1',0);product=row('enable_incognito_mode','0',4)
        for rows,expected in (([account,product],('0',4)),([product,account],('1',0))):
            detail=catalog(CONFIG,{'body':{'settingList':rows}})[0]
            self.assertEqual((detail['value'],detail['scope']),expected)

    def test_inherited_language_requires_explicit_persistent_choice(self):
        generation={'generateMinutesConfig':{'sourceLanguages':[{'lang':'en','displayName':'English'},{'lang':'zh','displayName':'Chinese'}]}}
        detail=catalog(CONFIG,{'body':{'settingList':[row('default_source_language','en',4)]}},generation)[0]
        self.assertTrue(detail['editable']);self.assertFalse(detail['exact_restore_supported'])
        with self.assertRaises(ValueError):validate_change(detail,'zh')
        self.assertEqual(validate_change(detail,'zh',True),'zh')
        with self.assertRaises(ValueError):validate_change(detail,'invented',True)

    def test_template_uses_current_option_descriptor(self):
        template={'templateId':'demo','title':'Example','description':'Test','iconUrl':'https://example.invalid/icon','secret':'not copied'}
        detail=catalog(CONFIG,{'body':{'settingList':[row('default_minutes_template','{}',4)]}},{'generateMinutesConfig':{'minutesTemplates':[template]}})[0]
        value=next(iter(detail['choices']))
        self.assertEqual(json.loads(value),{k:template[k] for k in ('description','iconUrl','templateId','title')})
        self.assertNotIn('secret',value)

    def test_foreign_account_and_device_ignored(self):
        own=row('storage_type','0');foreign={**own,'accountId':'987'};other={**own,'belongingId':'789'}
        self.assertEqual(catalog(CONFIG,{'body':{'settingList':[foreign,other]}}),[])
        storage=catalog(CONFIG,{'body':{'settingList':[own]}})[0]
        self.assertEqual(validate_change(storage,'-1'),'-1')
        with self.assertRaises(ValueError):validate_change(storage,'1')

    def test_timing_and_boolean_string_defaults_are_not_guessed(self):
        result=catalog(CONFIG,{'body':{'settingList':[row('auto_generate_minutes_duration_limit','360',4),row('default_speaker_diarization','true',4)]}})
        self.assertTrue(all(not r['editable'] for r in result))
        self.assertEqual(result[0]['evidence'],'grouped-write')

    def test_every_known_field_has_plain_meaning_and_unknown_fields_stay_unknown(self):
        from settings_catalog import METADATA,DESCRIPTIONS
        self.assertEqual(set(DESCRIPTIONS),set(METADATA))
        response={'body':{'settingList':[row(k,'synthetic-value') for k in METADATA]+[row('future_field','synthetic-value')]}}
        result=catalog(CONFIG,response)
        for detail in result:
            self.assertTrue(detail['description']);self.assertTrue(detail['value_format'])
            self.assertNotIn('synthetic-value',detail['description']+detail['value_format'])
            self.assertIn(detail['meaning_evidence'],('source','inferred','unknown'))
        unknown=result[-1]
        self.assertEqual(unknown['meaning_evidence'],'unknown');self.assertFalse(unknown['editable'])
        self.assertFalse(unknown['choices'])

    def test_language_memory_is_described_without_granting_write_access(self):
        keys=('last_select_source_language','last_select_target_language')
        for detail in catalog(CONFIG,{'body':{'settingList':[row(k,'zh') for k in keys]}}):
            self.assertEqual(detail['meaning_evidence'],'inferred')
            self.assertIn('unverified',detail['description'])
            self.assertFalse(detail['editable']);self.assertFalse(detail['exact_restore_supported'])
            with self.assertRaises(ValueError):validate_change(detail,'en')

    def test_automatic_realtime_transcription_uses_source_values(self):
        detail=catalog(CONFIG,{'body':{'settingList':[row('auto_realtime_asr','0')]}})[0]
        self.assertTrue(detail['editable']);self.assertEqual(detail['evidence'],'write-builder')
        self.assertEqual(detail['choices'],{'0':'Enabled','-1':'Disabled'})
        self.assertEqual(validate_change(detail,'-1'),'-1')
        with self.assertRaises(ValueError):validate_change(detail,'1')

class ImportTests(unittest.TestCase):
    def config(self):
        return {**CONFIG,'endpoint':'wss://minutes.qwennote.cn/test','app_key':'synthetic','client_device_id':'synthetic','user_agent':'test','access_token':'synthetic','refresh_token':'synthetic','expires_at':time.time()+3600,'native_client_id':'synthetic','native_client_secret':'synthetic','native_auth_origin':'https://minutes.qwennote.cn'}

    def test_existing_store_needs_explicit_same_account_replacement(self):
        candidate=self.config()
        with self.assertRaises(ValueError):select_import(candidate,candidate)
        self.assertEqual(select_import(candidate,candidate,True),candidate)
        with self.assertRaises(ValueError):select_import(candidate,{**candidate,'account_id':'other'},True)
        with self.assertRaises(ValueError):select_import(candidate,{**candidate,'target_device_id':777},True)

    def test_unprotected_or_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'session.json';path.write_text('{}');path.chmod(0o644)
            with self.assertRaises(ValueError):private_load(path)
            path.chmod(0o600);self.assertEqual(private_load(path),{})
            alias=root/'alias';alias.symlink_to(path)
            with self.assertRaises(ValueError):private_load(alias)

    def test_incomplete_renewal_material_is_rejected(self):
        candidate=self.config();candidate.pop('native_client_secret')
        with self.assertRaises(ValueError):select_import(candidate)

if __name__=='__main__':unittest.main()
