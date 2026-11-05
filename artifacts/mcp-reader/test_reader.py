"""Local integrity tests only. These do not prove vendor or A2 compatibility."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from mcp.shared.auth import OAuthToken
import qwennote_reader as reader


class ReaderTests(unittest.TestCase):
    def test_callback_accepts_expected_state_only(self):
        self.assertEqual(reader.callback_values('/callback?code=test-code&state=test-state', 'test-state'), ('test-code', 'test-state'))
        for bad in [
            '/callback?code=test-code&state=other',
            '/callback?code=test-code&state=test-state&state=other',
            '/callback?code=test-code&code=other&state=test-state',
            '/other?code=test-code&state=test-state',
            '/callback?code=test-code&state=test-state&iss=https%3A%2F%2Fother.invalid',
            '/callback?error=access_denied&state=test-state',
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reader.callback_values(bad, 'test-state')

    def test_callback_loopback_rejects_bad_state_and_replay(self):
        callback = reader.Callback(0)
        callback.expected_state = 'fixture-state'
        port = callback.server.server_address[1]
        try:
            with httpx.Client(trust_env=False) as client:
                bad = client.get(f'http://127.0.0.1:{port}/callback?code=fixture-code&state=bad')
                self.assertEqual(bad.status_code, 400)
                self.assertTrue(callback.result.empty())
                good = client.get(f'http://127.0.0.1:{port}/callback?code=fixture-code&state=fixture-state')
                self.assertEqual(good.status_code, 303)
                self.assertEqual(good.headers['location'], '/')
                self.assertNotIn('fixture-code', good.text)
                self.assertEqual(callback.result.get_nowait()[1], 'fixture-state')
                replay = client.get(f'http://127.0.0.1:{port}/callback?code=fixture-code&state=fixture-state')
                self.assertEqual(replay.status_code, 400)
        finally:
            callback.close()

    def test_private_storage_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            reader.private_json(path, {'test': 1})
            reader.private_json(path, {'test': 2})
            self.assertEqual(reader.private_read(path), {'test': 2})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError):
                reader.private_read(path)

    def test_storage_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'real.json'
            reader.private_json(path, {'test': True})
            link = Path(directory) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                reader.private_read(link)

    def test_token_expiry_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = reader.Storage(Path(directory))
            token = OAuthToken(access_token='fixture-only', token_type='Bearer', expires_in=120, refresh_token='fixture-refresh')
            with patch.object(reader.time, 'time', return_value=1000):
                asyncio.run(store.set_tokens(token))
            restarted = reader.Storage(Path(directory))
            with patch.object(reader.time, 'time', return_value=1090):
                loaded = asyncio.run(restarted.get_tokens())
            self.assertEqual(loaded.expires_in, 30)
            with patch.object(reader.time, 'time', return_value=1200):
                expired = asyncio.run(restarted.get_tokens())
            self.assertEqual(expired.expires_in, 0)
            self.assertIsNotNone(expired.refresh_token)

    def test_provider_restores_expiry_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = reader.Storage(Path(directory))
            token = OAuthToken(access_token='fixture-only', token_type='Bearer', expires_in=120, refresh_token='fixture-refresh')
            with patch.object(reader.time, 'time', return_value=1000):
                asyncio.run(store.set_tokens(token))
            provider = reader.PersistentOAuthClientProvider(
                server_url=reader.SERVER, storage=store,
                client_metadata=reader.OAuthClientMetadata(redirect_uris=['http://127.0.0.1:8765/callback']),
            )
            with patch.object(reader.time, 'time', return_value=1200):
                asyncio.run(provider._initialize())
            with patch.object(reader.time, 'time', return_value=1201):
                self.assertFalse(provider.context.is_token_valid())
            self.assertIsNotNone(provider.context.token_expiry_time)

    def test_refresh_retains_or_rotates_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = reader.Storage(Path(directory))
            provider = reader.PersistentOAuthClientProvider(
                server_url=reader.SERVER, storage=store,
                client_metadata=reader.OAuthClientMetadata(redirect_uris=['http://127.0.0.1:8765/callback']),
            )
            provider.context.current_tokens = OAuthToken(access_token='fixture-old', token_type='Bearer', expires_in=0, refresh_token='fixture-refresh')
            without_replacement = httpx.Response(200, json={'access_token':'fixture-new','token_type':'Bearer','expires_in':120})
            self.assertTrue(asyncio.run(provider._handle_refresh_response(without_replacement)))
            self.assertEqual(asyncio.run(store.get_tokens()).refresh_token, 'fixture-refresh')
            with_replacement = httpx.Response(200, json={'access_token':'fixture-newer','token_type':'Bearer','expires_in':120,'refresh_token':'fixture-rotated'})
            self.assertTrue(asyncio.run(provider._handle_refresh_response(with_replacement)))
            self.assertEqual(asyncio.run(store.get_tokens()).refresh_token, 'fixture-rotated')

    def test_authorization_does_not_expand_to_write_scope(self):
        from urllib.parse import parse_qs, urlsplit
        urls = []

        async def redirect(url):
            urls.append(url)

        async def callback():
            return 'fixture-code', parse_qs(urlsplit(urls[-1]).query)['state'][0]

        with tempfile.TemporaryDirectory() as directory:
            metadata = reader.OAuthClientMetadata(
                redirect_uris=['http://127.0.0.1:18765/callback'],
                scope='qwennote:read qwennote:write',
            )
            provider = reader.PersistentOAuthClientProvider(
                server_url=reader.SERVER, storage=reader.Storage(Path(directory)),
                client_metadata=metadata, redirect_handler=redirect, callback_handler=callback,
            )
            provider.context.client_info = reader.OAuthClientInformationFull(
                client_id='fixture-client', redirect_uris=metadata.redirect_uris,
            )
            asyncio.run(provider._perform_authorization_code_grant())
            provider.requested_scope = reader.WRITE_SCOPE  # only the separate write store asks for this
            asyncio.run(provider._perform_authorization_code_grant())
        self.assertEqual(parse_qs(urlsplit(urls[0]).query)['scope'], ['qwennote:read'])
        self.assertEqual(parse_qs(urlsplit(urls[1]).query)['scope'], ['qwennote:read qwennote:write'])

    def test_refresh_uses_the_client_that_issued_the_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            read = Path(directory); write = read / 'write'; write.mkdir(mode=0o700); read.chmod(0o700)
            def save(path, value):
                path.write_text(json.dumps(value)); path.chmod(0o600)
            client = lambda cid: {'client_id': cid, 'redirect_uris': ['http://127.0.0.1:18765/callback']}
            save(read / 'client.json', client('read-client')); save(write / 'client.json', client('console-client'))
            save(write / 'tokens.json', {'tokens': {'access_token': 'a', 'token_type': 'Bearer'}, 'expires_at': None})
            store = reader.Storage(write, read)
            self.assertEqual(asyncio.run(store.get_client_info()).client_id, 'read-client')  # older grants: the shared client
            save(write / 'tokens.json', {'tokens': {'access_token': 'a', 'token_type': 'Bearer'}, 'expires_at': None, 'client_id': 'console-client'})
            self.assertEqual(asyncio.run(store.get_client_info()).client_id, 'console-client')

    def test_mutating_tools_are_not_enabled(self):
        for name in ['replace_minutes_text', 'replace_speaker', 'update_minutes_summary', 'add_personal_hot_word', 'sendDeviceTaskCommand']:
            self.assertNotIn(name, reader.READ_TOOLS)
        self.assertFalse(reader.READ_TOOLS & reader.WRITE_TOOLS)
        self.assertNotIn('sendDeviceTaskCommand', reader.WRITE_TOOLS)

    def test_no_implicit_interactive_login(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(reader.reject_login())


if __name__ == '__main__':
    unittest.main()
