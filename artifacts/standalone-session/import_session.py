#!/usr/bin/env python3
"""Import a protected login handoff while the native backend is stopped."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
from lwp_client import validate_session,private_write,run


def private_load(path):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_mode&0o077 or path.parent.stat().st_mode&0o077:
        raise ValueError('Protected regular file and directory required')
    if path.stat().st_size>128*1024:raise ValueError('Unexpected session file size')
    return json.loads(path.read_text())


def select_import(candidate,existing=None,replace=False):
    config=validate_session(candidate)
    for key in ('refresh_token','native_client_id','native_client_secret','native_auth_origin'):
        if not isinstance(config.get(key),str) or not config[key]:raise ValueError('Incomplete renewable session')
    if config['native_auth_origin'] not in {'https://minutes.qwennote.cn','https://minutes.qwennote.ai'}:raise ValueError('Unverified native origin')
    if existing is not None:
        if not replace:raise ValueError('Existing store requires --replace-same-account')
        if str(existing.get('account_id'))!=str(config['account_id']):raise ValueError('Account replacement refused; use a separate store')
        if str(existing.get('target_device_id'))!=str(config['target_device_id']):raise ValueError('Target replacement refused; use a separate store')
    return config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--state',type=Path,default=Path(__file__).parent/'.private')
    parser.add_argument('--replace-same-account',action='store_true')
    args=parser.parse_args();logging.disable(logging.CRITICAL);os.umask(0o077)
    fd=None
    try:
        state=args.state
        state.mkdir(parents=True,mode=0o700,exist_ok=True)
        if state.is_symlink():raise ValueError('Real state directory required')
        if state.stat().st_mode&0o077:
            if any(state.iterdir()):raise ValueError('Existing state directory must be private')
            state.chmod(0o700)
        fd=os.open(state/'native-session.lock',os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=state/'native-session.json'
        config=select_import(private_load(args.source),private_load(path) if path.exists() else None,args.replace_same_account)
        reply=asyncio.run(run(config,'settings'))
        rows=reply.get('body',{}).get('settingList',[])
        if not any(str(s.get('accountId'))==str(config['account_id']) and s.get('accountType')==config['account_type'] and s.get('belongingType')==2 and str(s.get('belongingId'))==str(config['target_device_id']) for s in rows):
            raise ValueError('Imported target not corroborated')
        private_write(path,config)
        print('Protected session imported after live target/read verification. Credential values omitted.')
        return 0
    except Exception as error:
        print('Import not completed ('+type(error).__name__+'). Stop the backend and check protected handoff instructions.')
        return 2
    finally:
        if fd is not None:os.close(fd)

if __name__=='__main__':raise SystemExit(main())
