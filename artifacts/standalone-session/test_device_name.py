import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient, TestServer

import device_name
from public_errors import Busy, OutcomeUnknown, Refused
from web_backend import Backend, make_app

SECRET='ab'*32
CONFIG={'account_type':2,'account_id':'synthetic-account','target_device_id':456}


def record(name='Office recorder'):
    return {'devServId':1446,'deviceId':456,'accountType':2,'accountId':'synthetic-account',
            'deviceNick':name,'sn':'synthetic-serial','deviceSecret':'not-for-release'}


class DeviceNameContractTests(unittest.TestCase):
    def test_application_payload_and_trimmed_name(self):
        body=device_name.request(CONFIG,record(),'  Office A2  ')
        self.assertEqual(body,[{'devServId':1446,'deviceId':456,'deviceNick':'Office A2',
                                'accountType':2,'accountId':'synthetic-account'}])
        self.assertEqual(device_name.URI,'/r/Adaptor/DeviceI/updateDeviceNick')
        raw=record();raw.pop('accountType');raw.pop('accountId')
        self.assertEqual(device_name.request(CONFIG,raw,'Office A2'),
                         [{**body[0],'deviceNick':'Office A2'}])

    def test_application_weighted_length_boundaries(self):
        for name in ['a'*32,'é'*32,'中'*16,'😀'*16]:
            self.assertEqual(device_name.weight(name),32,name)
            self.assertEqual(device_name.normalize(name),name)
        for name in ['a'*33,'中'*17,'😀'*17,'Ā'*17]:
            self.assertGreater(device_name.weight(name),32)
            with self.assertRaises(ValueError):device_name.normalize(name)
        self.assertEqual(device_name.weight('Aé中😀'),6)

    def test_empty_control_and_foreign_identity_are_rejected(self):
        for name in ['', '   ', 'bad\x00name', 'bad\nname', None, 3]:
            with self.assertRaises(ValueError):device_name.normalize(name)
        for changed in [{'deviceId':457},{'devServId':1},{'accountType':1},{'accountId':'other'}]:
            value=record();value.update(changed)
            with self.assertRaises(ValueError):device_name.request(CONFIG,value,'Valid')

    def test_revision_binds_identity_and_name_without_exposing_them(self):
        identity=device_name.identity(CONFIG,record())
        revision=device_name.revision(SECRET,identity,'Office recorder')
        self.assertRegex(revision,r'^[a-f0-9]{64}$')
        self.assertNotIn('synthetic',revision)
        self.assertNotEqual(revision,device_name.revision(SECRET,identity,'Other'))
        other=copy.deepcopy(identity);other['device_id']='457'
        self.assertNotEqual(revision,device_name.revision(SECRET,other,'Office recorder'))
        self.assertTrue(device_name.same_identity(identity,identity))
        self.assertFalse(device_name.same_identity(identity,other))

    def test_acknowledgement_requires_transport_and_service_success(self):
        for reply in [{'code':200,'body':None},{'code':200,'body':{}},{'code':200,'body':{'code':0}}]:
            self.assertTrue(device_name.acknowledged(reply))
        for reply in [{'code':500,'body':None},{'code':200,'body':'ok'},
                      {'code':200,'body':{'code':500}},{'code':200,'body':{'success':False}}]:
            self.assertFalse(device_name.acknowledged(reply))


class DeviceNameBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.b=Backend(self.root,18780);self.b.config=copy.deepcopy(CONFIG)
        self.b.handle_secret=SECRET;self.b.connection='connected';self.b.device={'model':'QwenNote A2'}
        self.b.ready_until=time.time()+60;self.current='Office recorder';self.calls=[]
        self.b.channel=SimpleNamespace(ready=True,_request=AsyncMock(side_effect=self.reply))
        self.b.refresh=AsyncMock(side_effect=self.refresh)

    async def asyncTearDown(self):self.tmp.cleanup()

    def refresh(self):
        self.b.device_record=record(self.current);self.b.last_refresh_at=1000
        return self.b.status()

    def reply(self,uri,body,headers):
        self.calls.append((uri,copy.deepcopy(body)))
        self.current=body[0]['deviceNick']
        return {'code':200,'body':None}

    def revision(self):
        return device_name.revision(SECRET,device_name.identity(CONFIG,record(self.current)),self.current)

    async def test_save_readback_and_restore_preserve_original(self):
        result=await self.b.rename_device('  Desk A2  ',self.revision())
        self.assertEqual(result['name'],'Desk A2');self.assertTrue(result['changed'] and result['readback_matches'])
        self.assertTrue(result['restore_available']);self.assertEqual(self.current,'Desk A2')
        self.assertEqual(self.calls,[(device_name.URI,device_name.request(CONFIG,record(),'Desk A2'))])
        journal=json.loads(self.b.device_name_path.read_text())
        self.assertEqual((journal['original'],journal['requested'],journal['pending']),('Office recorder','Desk A2',False))
        self.assertEqual(self.b.device_name_path.stat().st_mode&0o777,0o600)
        restored=await self.b.rename_device(None,self.revision(),restore=True)
        self.assertEqual(restored['name'],'Office recorder');self.assertTrue(restored['restored'])
        self.assertEqual(self.current,'Office recorder');self.assertIsNone(self.b.device_name_restore)
        self.assertEqual(len(self.calls),2)

    async def test_stale_revision_readiness_and_busy_reject_before_write(self):
        with self.assertRaises(Refused):await self.b.rename_device('Other','0'*64)
        self.b.ready_until=0
        with self.assertRaises(PermissionError):await self.b.rename_device('Other',self.revision())
        self.b.ready_until=time.time()+60
        async with self.b.mutation_lock:
            with self.assertRaises(Busy):await asyncio.wait_for(self.b.rename_device('Other',self.revision()),.1)
        self.assertEqual(self.calls,[])

    async def test_noop_does_not_write_or_create_restore_journal(self):
        result=await self.b.rename_device('Office recorder',self.revision())
        self.assertFalse(result['changed']);self.assertEqual(self.calls,[])
        self.assertIsNone(self.b.device_name_restore);self.assertFalse(self.b.device_name_path.exists())

    async def test_unknown_outcome_blocks_restart_and_read_reconciles_without_replay(self):
        async def lost(uri,body,headers):
            self.calls.append((uri,copy.deepcopy(body)));self.current='Desk A2';raise ConnectionError('lost')
        self.b.channel._request.side_effect=lost
        with self.assertRaises(OutcomeUnknown):await self.b.rename_device('Desk A2',self.revision())
        self.assertFalse(self.b.health()['restart_safe']);self.assertIn('device-name-uncertain',self.b.health()['blocking'])
        reloaded=Backend(self.root,18780)
        self.assertTrue(reloaded.device_name_restore['pending']);self.assertFalse(reloaded.health()['restart_safe'])
        self.assertEqual(len(self.calls),1)
        result=await self.b.device_name_status()
        self.assertEqual(result['name'],'Desk A2');self.assertFalse(result['pending'])
        self.assertTrue(self.b.health()['restart_safe']);self.assertEqual(len(self.calls),1)
        self.b.channel._request.side_effect=self.reply
        again=await self.b.rename_device('Another',self.revision())
        self.assertTrue(again['changed']);self.assertEqual(len(self.calls),2)
        journal=json.loads(self.b.device_name_path.read_text())
        self.assertEqual(journal['original'],'Office recorder')

    async def test_unrelated_reconciliation_state_never_replays(self):
        async def lost(uri,body,headers):
            self.calls.append((uri,copy.deepcopy(body)));raise ConnectionError('lost')
        self.b.channel._request.side_effect=lost
        with self.assertRaises(OutcomeUnknown):await self.b.rename_device('Desk A2',self.revision())
        self.current='Changed elsewhere'
        with self.assertRaises(OutcomeUnknown):await self.b.rename_device('Third',self.revision())
        self.assertTrue(self.b.device_name_restore['pending']);self.assertEqual(len(self.calls),1)

    async def test_private_journal_and_public_status_do_not_expose_device_fields(self):
        await self.b.rename_device('Desk A2',self.revision())
        public=json.dumps(self.b.status());health=json.dumps(self.b.health())
        for value in ['synthetic-account','synthetic-serial','Office recorder','Desk A2','not-for-release']:
            self.assertNotIn(value,public);self.assertNotIn(value,health)
        self.assertNotIn('456',json.dumps(self.b.status()['device']))


class DeviceNameRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.b=Backend(Path(self.tmp.name),1)
        self.b.start=AsyncMock();self.b.stop=AsyncMock()
        self.client=TestClient(TestServer(make_app(self.b)));await self.client.start_server()
        self.b.port=self.client.server.port
        self.headers={'Cookie':'qn_ui='+self.b.cookie,'X-CSRF-Token':self.b.csrf}

    async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()

    async def test_owner_routes_validate_exact_shapes(self):
        self.b.device_name_status=AsyncMock(return_value={'name':'Office recorder'})
        self.b.rename_device=AsyncMock(return_value={'name':'Desk A2'})
        self.assertEqual((await self.client.post('/api/device-name-save',json={})).status,403)
        invalid=await self.client.post('/api/device-name-save',json={'name':'Desk A2','expected_revision':'a'*64,'deviceId':456},headers=self.headers)
        self.assertEqual(invalid.status,409);self.b.rename_device.assert_not_awaited()
        response=await self.client.post('/api/device-name-save',json={'name':'Desk A2','expected_revision':'a'*64},headers=self.headers)
        self.assertEqual(response.status,200);self.b.rename_device.assert_awaited_once_with('Desk A2','a'*64)
        response=await self.client.post('/api/device-name-restore',json={'expected_revision':'b'*64},headers=self.headers)
        self.assertEqual(response.status,200);self.b.rename_device.assert_awaited_with(None,'b'*64,restore=True)
        response=await self.client.post('/api/device-name',json={},headers=self.headers)
        self.assertEqual(response.status,200);self.b.device_name_status.assert_awaited_once_with()


if __name__=='__main__':unittest.main()
