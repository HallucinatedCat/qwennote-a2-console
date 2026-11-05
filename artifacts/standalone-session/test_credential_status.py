"""Credential status never leaks values and classifies states correctly. Stdlib only."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import credential_status as cs

SECRETS=('SECRET-ACCESS-123456','SECRET-REFRESH-654321','SECRET-CLIENT-777','SECRET-API-KEY-999','100200300','4242',
         'https://secret-provider.example/v1','evil-scope:write')
NOW=1_800_000_000.0


def write(path,data,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(data));os.chmod(path,mode)


def session(expires_in,**extra):
    return {'endpoint':'wss://minutes.qwennote.cn/x','app_key':'k','client_device_id':'d','user_agent':'u',
            'access_token':'SECRET-ACCESS-123456','refresh_token':'SECRET-REFRESH-654321','account_id':'100200300',
            'account_type':2,'target_device_id':4242,'native_client_id':'id','native_client_secret':'SECRET-CLIENT-777',
            'native_auth_origin':'https://minutes.qwennote.cn','expires_at':NOW+expires_in,**extra}


class CredentialStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.state=self.root/'state';self.state.mkdir(mode=0o700);os.chmod(self.state,0o700)
        self.mcp=self.root/'mcp'

    def tearDown(self):self.tmp.cleanup()

    def items(self,**kw):
        result=cs.collect(self.state,kw.pop('mcp_root',self.mcp),now=NOW,**kw)
        return {i['id']:i for i in result['items']},result

    def test_nothing_configured(self):
        items,_=self.items(mcp_root=None)
        self.assertEqual(items['native_service']['state'],'missing')
        self.assertEqual(items['native_validation']['state'],'missing')
        self.assertEqual(items['mcp_grant']['state'],'not_configured')
        self.assertEqual(items['assistant']['state'],'not_configured')
        self.assertEqual(items['login_handoff']['state'],'absent')

    def test_full_setup_reports_states_without_any_value(self):
        write(self.state/'native-session.json',session(3000))
        write(self.state/'bootstrap-validation'/'native-session.json',session(-30))
        write(self.mcp/'.private'/'tokens.json',{'expires_at':NOW-10,'tokens':{'access_token':'SECRET-ACCESS-123456','refresh_token':'SECRET-REFRESH-654321','scope':'qwennote:read','token_type':'Bearer'}})
        write(self.state/'assistant.json',{'url':'https://secret-provider.example/v1','model':'m','api_key':'SECRET-API-KEY-999'})
        write(self.state/'login-handoff'/'native-session.json',session(-9999))
        items,result=self.items(renewals={'count':2,'failures':0,'last_at':NOW-60,'last_failure_at':None,'last_failure_type':None})
        self.assertEqual(items['native_service']['state'],'valid');self.assertEqual(items['native_service']['expires_in'],3000)
        self.assertTrue(items['native_service']['refresh_token']);self.assertEqual(items['native_service']['renewals']['count'],2)
        self.assertEqual(items['native_validation']['state'],'renewable')
        self.assertEqual(items['mcp_grant']['state'],'renews_on_use');self.assertTrue(items['mcp_grant']['exact_read_scope'])
        self.assertEqual(items['assistant']['state'],'configured');self.assertTrue(items['assistant']['api_key'])
        self.assertEqual(items['login_handoff']['state'],'stale')
        dumped=json.dumps(result)
        for value in SECRETS:self.assertNotIn(value,dumped)
        self.assertNotIn('minutes.qwennote.cn',dumped)

    def test_in_memory_config_wins_and_expiring_window(self):
        write(self.state/'native-session.json',session(-100))
        items,_=self.items(config=session(90))
        self.assertEqual(items['native_service']['state'],'expiring')

    def test_expired_without_refresh_or_signing(self):
        write(self.state/'native-session.json',{**session(-5),'refresh_token':''})
        self.assertEqual(self.items()[0]['native_service']['state'],'expired')

    def test_loose_permissions_and_links_are_flagged(self):
        write(self.state/'native-session.json',session(3000),mode=0o644)
        item=self.items()[0]['native_service']
        self.assertEqual(item['state'],'unprotected');self.assertEqual(item['problem'],'file_mode')
        (self.state/'native-session.json').unlink()
        target=self.root/'elsewhere.json';write(target,session(3000))
        (self.state/'native-session.json').symlink_to(target)
        item=self.items()[0]['native_service']
        self.assertEqual(item['problem'],'symlink');self.assertFalse(item['protected'])

    def test_directory_mode_is_checked(self):
        write(self.state/'native-session.json',session(3000));os.chmod(self.state,0o755)
        try:self.assertEqual(self.items()[0]['native_service']['problem'],'directory_mode')
        finally:os.chmod(self.state,0o700)

    def test_oversized_or_garbled_files_are_invalid(self):
        path=self.state/'bootstrap-validation'/'native-session.json'
        write(path,{});path.write_text('{"x":"'+'a'*(cs.MAX_FILE+10)+'"}');os.chmod(path,0o600)
        self.assertEqual(self.items()[0]['native_validation']['state'],'invalid')
        path.write_text('not json')
        self.assertEqual(self.items()[0]['native_validation']['state'],'invalid')

    def test_write_scope_is_not_read_only_and_never_echoed(self):
        write(self.mcp/'.private'/'tokens.json',{'expires_at':NOW+900,'tokens':{'access_token':'a','scope':'qwennote:read evil-scope:write'}})
        items,result=self.items()
        self.assertFalse(items['mcp_grant']['read_only']);self.assertFalse(items['mcp_grant']['exact_read_scope'])
        self.assertNotIn('evil-scope',json.dumps(result))

    def test_plain_http_assistant_is_invalid_but_loopback_is_fine(self):
        write(self.state/'assistant.json',{'url':'http://provider.example/v1','model':'m'})
        self.assertEqual(self.items()[0]['assistant']['state'],'invalid')
        write(self.state/'assistant.json',{'url':'http://127.0.0.1:8080/v1','model':'m'})
        item=self.items()[0]['assistant'];self.assertEqual(item['state'],'configured');self.assertFalse(item['api_key'])

    def test_collect_takes_no_lock_while_session_owners_hold_theirs(self):
        write(self.state/'native-session.json',session(3000))
        write(self.state/'bootstrap-validation'/'native-session.json',session(3000))
        write(self.mcp/'.private'/'tokens.json',{'expires_at':NOW+900,'tokens':{'access_token':'a','scope':'qwennote:read'}})
        held=[]
        for path in (self.state/'native-session.lock',self.state/'bootstrap-validation'/'native-session.lock',self.mcp/'.private'/'session.lock'):
            fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX);held.append(fd)
        try:
            done=[]
            with patch('fcntl.flock',side_effect=AssertionError('lock taken')),patch('fcntl.lockf',side_effect=AssertionError('lock taken')):
                worker=threading.Thread(target=lambda:done.append(self.items()));worker.start();worker.join(5)
            self.assertTrue(done,'collect took or waited for a lock')
            self.assertEqual(done[0][0]['native_validation']['state'],'valid')
        finally:
            for fd in held:os.close(fd)

    def test_nothing_is_written(self):
        write(self.state/'native-session.json',session(3000))
        before=sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        self.items()
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')),before)


if __name__=='__main__':unittest.main()
