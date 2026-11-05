"""User consent for MCP access through the console's own port.

The reader's command-line login listens on its own callback port, which the user had to map to
the browser. Here the backend builds the same PKCE authorization request the MCP SDK builds
(response_type code, S256, state, resource) with the console's /mcp/callback address. The
vendor checks the return address against the client registration, so the console registers its
own public client for the address the browser uses (once, then reused) and the vendor sends the
browser back to the console, which exchanges the code. Each saved grant records the client that
issued it, so refreshes use the right one. The older path (the existing registration's address
plus a pasted final address) remains for the command-line client.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from public_errors import Busy, Refused, Unavailable

ISSUER = 'https://minutes.qwennote.cn'
RESOURCE = ISSUER + '/mcp'
SCOPES = {'read': 'qwennote:read', 'write': 'qwennote:read qwennote:write'}
PENDING_SECONDS = 900


def stores(mcp_root):
    """(read store, write store): the write store sits inside the read store and shares its client."""
    read = Path(mcp_root) / '.private'
    return read, read / 'write'


def client(mcp_root):
    path = stores(mcp_root)[0] / 'client.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise Unavailable('No MCP client registration on this host; run the document reader login once')
    data = json.loads(path.read_text())
    if not isinstance(data.get('client_id'), str) or not data['client_id']:
        raise Unavailable('No MCP client registration on this host; run the document reader login once')
    return data


CLIENT_FIELDS = ('client_id', 'client_name', 'redirect_uris', 'grant_types', 'response_types',
                 'token_endpoint_auth_method', 'scope', 'client_id_issued_at')


def console_client(mcp_root, redirect_uri):
    """A console client registered earlier for this return address, or None."""
    path = stores(mcp_root)[0] / 'console-client.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        return None
    data = json.loads(path.read_text())
    return data if data.get('redirect_uris') == [redirect_uri] and isinstance(data.get('client_id'), str) else None


def registration(redirect_uri):
    return {'client_name': 'QwenNote Personal Console', 'redirect_uris': [redirect_uri],
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none', 'scope': SCOPES['write']}


def keep_client(mcp_root, info, redirect_uri):
    if not isinstance(info, dict) or not isinstance(info.get('client_id'), str) or not info['client_id']:
        raise Unavailable('The authorization server did not register the console; try again')
    client_info = {key: info[key] for key in CLIENT_FIELDS if key in info}
    client_info['redirect_uris'] = [redirect_uri]
    write_private(stores(mcp_root)[0], 'console-client.json', client_info)
    return client_info


def write_private(root, name, value):
    temp = root / ('.' + name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, root / name)


def check_metadata(meta):
    if not isinstance(meta, dict) or str(meta.get('issuer', '')).rstrip('/') != ISSUER:
        raise Unavailable('The authorization server did not match the expected QwenNote server')
    for key in ('authorization_endpoint', 'token_endpoint', 'registration_endpoint'):
        url = urlsplit(str(meta.get(key, '')))
        if url.scheme != 'https' or url.hostname != 'minutes.qwennote.cn':
            raise Unavailable('The authorization server did not match the expected QwenNote server')
    return meta


def start(meta, client_info, access, redirect_uri):
    """(authorization URL, pending login). The pending login stays in backend memory only."""
    if access not in SCOPES:
        raise Refused('Choose read or write access')
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    params = {'response_type': 'code', 'client_id': client_info['client_id'], 'redirect_uri': redirect_uri,
              'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
              'resource': RESOURCE, 'scope': SCOPES[access]}
    pending = {'state': state, 'verifier': verifier, 'redirect_uri': redirect_uri, 'access': access,
               'client_id': client_info['client_id'], 'client': client_info,
               'token_endpoint': meta['token_endpoint'], 'created': time.time()}
    return meta['authorization_endpoint'] + '?' + urlencode(params), pending


def callback(query):
    """(code, state) from the callback query string or a pasted callback URL."""
    if not isinstance(query, str) or len(query) > 8192:
        raise Refused('Paste the whole address from the browser bar')
    params = parse_qs(urlsplit(query).query if '://' in query else query.lstrip('?'))
    if 'error' in params:
        raise Refused('The authorization was declined or ended early; start it again')
    if 'iss' in params and params['iss'] != [ISSUER]:
        raise Unavailable('The authorization server did not match the expected QwenNote server')
    if any(len(params.get(key, [])) != 1 for key in ('code', 'state')):
        raise Refused('Paste the whole address from the browser bar')
    return params['code'][0], params['state'][0]


def token_request(pending, code):
    return {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': pending['redirect_uri'],
            'client_id': pending['client_id'], 'code_verifier': pending['verifier'], 'resource': RESOURCE}


def check_token(token, access):
    if (not isinstance(token, dict) or not isinstance(token.get('access_token'), str) or
            str(token.get('token_type', '')).lower() != 'bearer'):
        raise Unavailable('The authorization server returned no usable token; start the login again')
    if access == 'write' and 'qwennote:write' not in str(token.get('scope') or ''):
        raise Refused('The authorization server did not grant write access')
    keep = {k: token[k] for k in ('access_token', 'token_type', 'expires_in', 'scope', 'refresh_token') if k in token}
    return keep


def save(mcp_root, access, token, client_info=None):
    """Write tokens.json in the reader's Storage format, holding the store's session lock. The
    issuing client id is recorded; a console client is also written as the store's client.json."""
    read, write = stores(mcp_root)
    root = write if access == 'write' else read
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise Unavailable('The MCP credential store must be private')
    lock = os.open(root / 'session.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy('The document reader is busy; finish the login again in a moment') from None
        value = {'tokens': token, 'expires_at': None if token.get('expires_in') is None else time.time() + float(token['expires_in'])}
        if client_info:
            value['client_id'] = client_info['client_id']
            if client_info.get('client_name') == 'QwenNote Personal Console':
                write_private(root, 'client.json', client_info)
        write_private(root, 'tokens.json', value)
    finally:
        os.close(lock)
