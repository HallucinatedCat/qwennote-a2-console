import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import generation_settings as protocol
from web_backend import Backend

CONFIG={'account_id':'123','account_type':0,'target_device_id':456}
ORIGINAL=dict(zip(protocol.KEYS,('0','0','360')))
REQUESTED=dict(zip(protocol.KEYS,('1','10','120')))

def details(inherited=False):
    return {k:{'value':v,'scope':4 if inherited and k!=protocol.KEYS[0] else 2} for k,v in ORIGINAL.items()}

class GenerationProtocolTests(unittest.TestCase):
    def test_batch_matches_native_identity_and_complete_field_group(self):
        body=protocol.command(CONFIG,REQUESTED,request_id=321)[0]
        self.assertEqual(body,{'accountType':0,'accountId':'123','belongingType':2,'belongingId':'456',
                              'settingRequestModelList':[{'settingKey':k,'settingValue':REQUESTED[k]} for k in protocol.KEYS],'requestId':321})

    def test_unknown_incomplete_and_numeric_values_rejected(self):
        invalid=[{},dict(REQUESTED,extra='1'),{**REQUESTED,protocol.KEYS[1]:'60'},
                 {**REQUESTED,protocol.KEYS[2]:'59'},{**REQUESTED,protocol.KEYS[0]:True}]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):protocol.validate(value)
        self.assertEqual(protocol.validate(dict(zip(protocol.KEYS,('1','0','0'))))[protocol.KEYS[2]],'0')

    def test_inherited_siblings_need_confirmation_and_cannot_restore_inheritance(self):
        rows=details(True)
        with self.assertRaises(ValueError):protocol.next_journal(rows,REQUESTED)
        saved=protocol.next_journal(rows,REQUESTED,persistent_override=True)
        self.assertFalse(saved['exact_restore_supported'])
        with self.assertRaises(ValueError):protocol.restore_values(rows,saved)

    def test_original_survives_later_writes_and_external_changes_refuse_restore(self):
        rows=details();saved=protocol.next_journal(rows,REQUESTED)
        for key in protocol.KEYS:rows[key]['value']=REQUESTED[key]
        again=protocol.next_journal(rows,{**REQUESTED,protocol.KEYS[2]:'180'},saved)
        self.assertEqual(again['original'],ORIGINAL)
        rows[protocol.KEYS[1]]['value']='30'
        with self.assertRaises(ValueError):protocol.restore_values(rows,again)
        with self.assertRaises(ValueError):protocol.next_journal(rows,REQUESTED,again)

class GenerationBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.state=Path(self.tmp.name)
        self.backend=Backend(self.state,18780);b=self.backend
        b.config=CONFIG;b.device={'model':'test'};b.ready_until=time.time()+60
        self.rows=details();self.mode='success';self.calls=[]
        async def refresh():
            b.setting_details=copy.deepcopy(self.rows);b.settings={k:r['value'] for k,r in self.rows.items()}
        async def request(uri,body,headers):
            self.calls.append((uri,body))
            if self.mode=='timeout':raise TimeoutError()
            for n,item in enumerate(body[0]['settingRequestModelList']):
                if self.mode=='partial' and n==2:continue
                self.rows[item['settingKey']]={'value':item['settingValue'],'scope':2}
            return {'code':200}
        b.refresh=refresh;b.channel=SimpleNamespace(ready=True,_request=request)
        await refresh()

    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_timeout_is_not_retried_and_original_group_survives_restart(self):
        self.mode='timeout'
        with self.assertRaises(RuntimeError):await self.backend.change_generation(REQUESTED)
        self.assertEqual(len(self.calls),1)
        saved=json.loads((self.state/'generation-restore.json').read_text())
        self.assertEqual(saved['original'],ORIGINAL)
        restarted=Backend(self.state,18780)
        self.assertEqual(restarted.generation_restore,saved)

    async def test_restore_keeps_journal_until_every_field_matches(self):
        await self.backend.change_generation(REQUESTED)
        self.mode='partial';result=await self.backend.change_generation(restore=True)
        self.assertFalse(result['cloud_readback_matches']);self.assertIsNotNone(self.backend.generation_restore)
        self.mode='success';result=await self.backend.change_generation(restore=True)
        self.assertTrue(result['cloud_readback_matches']);self.assertIsNone(self.backend.generation_restore)
        self.assertIsNone(Backend(self.state,18780).generation_restore)

    async def test_no_readiness_or_legacy_journal_sends_no_batch(self):
        b=self.backend;b.ready_until=0
        with self.assertRaises(PermissionError):await b.change_generation(REQUESTED)
        b.ready_until=time.time()+60
        b.restores[protocol.KEYS[0]]={'original':'0','requested':'1'}
        with self.assertRaises(ValueError):await b.change_generation(REQUESTED)
        self.assertEqual(self.calls,[])

    async def test_inherited_group_cannot_bypass_confirmation_through_single_editor(self):
        self.rows=details(True);b=self.backend
        with self.assertRaises(ValueError):await b.change_generation(REQUESTED)
        with self.assertRaises(ValueError):await b.change_setting(protocol.KEYS[0],'1')
        self.assertEqual(self.calls,[])
        result=await b.change_generation(REQUESTED,persistent_override=True)
        self.assertTrue(result['cloud_readback_matches'])
        self.assertFalse(b.generation_restore['exact_restore_supported'])
        with self.assertRaises(ValueError):await b.change_generation(restore=True)
        self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()
