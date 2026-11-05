import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import notification_settings as protocol
from lwp_client import arguments
from web_backend import Backend

CONFIG={'account_id':'123','account_type':0,'target_device_id':456}

class NotificationProtocolTests(unittest.TestCase):
    def test_lookup_shape_and_allowlisted_options(self):
        self.assertEqual(set(arguments(CONFIG,'organizations')[0]),{'requestId'})
        response={'code':200,'body':{'organizations':[{'dingOrgId':111,'orgName':'Example team'},{'dingOrgId':True,'orgName':'Invalid'},{'dingOrgId':'-1','orgName':'Invalid'}]}}
        self.assertEqual(protocol.organizations(response),{'0':'Personal edition','111':'Example team'})
        with self.assertRaises(ValueError):protocol.organizations({'code':403,'body':{}})

    def test_opaque_option_is_bound_to_instance_and_current_membership(self):
        token=protocol.handle('secret','111');allowed={'111':'Example team'}
        self.assertEqual(protocol.resolve('secret',token,allowed),'111')
        with self.assertRaises(ValueError):protocol.resolve('other',token,allowed)
        with self.assertRaises(ValueError):protocol.resolve('secret',token,{'0':'Personal'})
        public=protocol.descriptor({protocol.METHOD:{'value':'0','scope':2},protocol.ORGANIZATION:{'value':'111','scope':2}},allowed,'secret')
        self.assertNotIn('dingOrgId',json.dumps(public));self.assertNotIn('"111"',json.dumps(public))

    def test_method_change_keeps_organization_and_noop_sends_nothing(self):
        rows={protocol.METHOD:{'value':'0','scope':2},protocol.ORGANIZATION:{'value':'111','scope':4}}
        changed,saved=protocol.prepare(rows,'1')
        self.assertEqual(changed,{protocol.METHOD:'1'});self.assertTrue(saved['exact_restore_supported'])
        self.assertEqual(protocol.prepare(rows,'0'),({},None))
        body=protocol.command(CONFIG,changed,request_id=321)[0]
        self.assertEqual(body['settingRequestModelList'],[{'settingKey':protocol.METHOD,'settingValue':'1'}])
        self.assertEqual(body['belongingId'],'456');self.assertEqual(body['accountId'],'123')

    def test_new_organization_requires_confirmation_and_cannot_restore_absence(self):
        rows={protocol.METHOD:{'value':'0','scope':2}}
        with self.assertRaises(ValueError):protocol.prepare(rows,'0','111')
        changed,saved=protocol.prepare(rows,'0','111',persistent_override=True)
        self.assertEqual(changed,{protocol.ORGANIZATION:'111'})
        self.assertIsNone(saved['original'][protocol.ORGANIZATION]);self.assertFalse(saved['exact_restore_supported'])
        with self.assertRaises(ValueError):protocol.restoration(rows,saved,{'111':'Example'})

    def test_scope_priority_identity_and_external_changes(self):
        base={'settingKey':protocol.METHOD,'accountId':'123','accountType':0,'settingValue':'0','belongingType':4,'belongingId':'1446'}
        device={**base,'settingValue':'1','belongingType':2,'belongingId':'456'}
        for rows in ([base,device],[device,base]):
            current=protocol.current(CONFIG,{'body':{'settingList':rows+[dict(device,accountId='other')]}})
            self.assertEqual(current[protocol.METHOD]['value'],'1')
        current={protocol.METHOD:{'value':'0','scope':2}}
        _,saved=protocol.prepare(current,'1');current[protocol.METHOD]['value']='unrecognized'
        with self.assertRaises(ValueError):protocol.restoration(current,saved)

    def test_invalid_method_and_arbitrary_fields_rejected(self):
        rows={protocol.METHOD:{'value':'0','scope':2}}
        for method in ('2',True,{},None):
            with self.assertRaises(ValueError):protocol.prepare(rows,method)
        with self.assertRaises(ValueError):protocol.command(CONFIG,{'arbitrary':'1'})

class NotificationBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.b=Backend(self.path,18780)
        self.b.config=CONFIG;self.b.device={'model':'test'};self.b.ready_until=time.time()+60
        self.rows={protocol.METHOD:{'value':'0','scope':2},protocol.ORGANIZATION:{'value':'111','scope':2}}
        self.allowed=[{'dingOrgId':111,'orgName':'First team'},{'dingOrgId':222,'orgName':'Second team'}]
        self.writes=[];self.reads=[];self.timeout=False
        async def refresh():self.b.notification_rows=copy.deepcopy(self.rows)
        async def read(tool,body):
            self.reads.append((tool,body));return {'code':200,'body':{'organizations':self.allowed}}
        async def request(uri,body,headers):
            self.writes.append((uri,body))
            if self.timeout:raise TimeoutError()
            for item in body[0]['settingRequestModelList']:self.rows[item['settingKey']]={'value':item['settingValue'],'scope':2}
            return {'code':200}
        self.b.refresh=refresh;self.b.channel=SimpleNamespace(ready=True,read=read,_request=request)
        await refresh()

    async def asyncTearDown(self):self.tmp.cleanup()

    async def test_lookup_does_not_need_readiness_and_leaks_no_raw_ids(self):
        self.b.ready_until=0;result=await self.b.refresh_organizations()
        self.assertTrue(result['organizations_loaded']);self.assertEqual(len(result['organization_options']),3)
        self.assertNotIn('"111"',json.dumps(result));self.assertEqual(self.writes,[])

    async def test_stale_organization_selection_is_revalidated_before_write(self):
        options=await self.b.refresh_organizations();token=options['organization_options'][2]['handle']
        self.allowed=[]
        with self.assertRaises(ValueError):await self.b.change_notification('0',token)
        self.assertEqual(self.writes,[]);self.assertFalse(self.b.notification_restore_path.exists())

    async def test_timeout_journal_restart_and_method_restore(self):
        self.timeout=True
        with self.assertRaises(RuntimeError):await self.b.change_notification('1')
        self.assertEqual(len(self.writes),1)
        saved=Backend(self.path,18780).notification_restore
        self.assertEqual(saved['original'],{protocol.METHOD:'0'})
        self.timeout=False;result=await self.b.change_notification(restore=True)
        self.assertTrue(result['cloud_readback_matches']);self.assertIsNone(self.b.notification_restore)

    async def test_old_organization_cannot_be_restored_after_membership_loss(self):
        token=protocol.handle(self.b.handle_secret,'222')
        await self.b.change_notification('0',token)
        self.allowed=[{'dingOrgId':222,'orgName':'Second team'}]
        with self.assertRaises(ValueError):await self.b.change_notification(restore=True)
        self.assertEqual(len(self.writes),1)

    async def test_readiness_guards_new_writes(self):
        self.b.ready_until=0
        with self.assertRaises(PermissionError):await self.b.change_notification('1')
        self.assertEqual(self.writes,[])

if __name__=='__main__':unittest.main()
