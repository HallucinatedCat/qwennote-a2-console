"""Browser-handoff behavior against a local fixture, never vendor auth."""
import asyncio
import re
import unittest

import httpx
import qwennote_reader as reader
from login_portal import LoginTimeout, LoginDenied

AUTH_URL = 'https://minutes.qwennote.cn/authorize?state=fixture-state&scope=qwennote%3Aread'


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_browser_before_callback_timeout_and_can_restart(self):
        portal = reader.Callback(0, ready_timeout=5, login_timeout=0.05)
        task = asyncio.create_task(portal.redirect(AUTH_URL))
        try:
            # Longer than callback timeout: mapping the port must not consume it.
            await asyncio.sleep(0.15)
            self.assertFalse(task.done())
            self.assertFalse(portal.started.is_set())
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                landing = await client.get(portal.local_url)
                self.assertEqual(landing.status_code, 200)
                self.assertIn('Continue to QwenNote', landing.text)
                self.assertNotIn('fixture-state', landing.text)
                csrf = re.search(r'name="csrf" value="([^"]+)"', landing.text).group(1)
                headers = {'Origin': portal.local_url.rstrip('/')}
                invalid = await client.post(portal.local_url + 'start', data={'csrf':'bad'}, headers=headers)
                self.assertEqual(invalid.status_code, 403)
                self.assertFalse(portal.started.is_set())
                for _ in range(2):
                    start = await client.post(portal.local_url + 'start', data={'csrf':csrf}, headers=headers)
                    self.assertEqual(start.status_code, 303)
                    self.assertEqual(start.headers['location'], AUTH_URL)
                await asyncio.wait_for(task, 1)
                callback = await client.get(portal.local_url + 'callback?code=fixture-code&state=fixture-state')
                self.assertEqual(callback.status_code, 303)
                self.assertEqual(callback.headers['location'], '/')
                self.assertNotIn('fixture-code', callback.text)
                self.assertEqual(await portal.receive(), ('fixture-code','fixture-state'))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(portal.close)

    async def test_host_and_origin_checks(self):
        portal = reader.Callback(0)
        portal.authorization_url = AUTH_URL
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                rebound = await client.get(portal.local_url, headers={'Host':'other.invalid'})
                self.assertEqual(rebound.status_code, 403)
                cross_origin = await client.post(portal.local_url + 'start', data={'csrf':portal.csrf}, headers={'Origin':'https://other.invalid'})
                self.assertEqual(cross_origin.status_code, 403)
                self.assertFalse(portal.started.is_set())
        finally:
            await asyncio.to_thread(portal.close)

    async def test_missing_or_null_origin_still_requires_csrf(self):
        portal = reader.Callback(0)
        portal.authorization_url = AUTH_URL
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                for headers in [{}, {'Origin':'null'}]:
                    invalid = await client.post(portal.local_url + 'start', data={'csrf':'wrong'}, headers=headers)
                    self.assertEqual(invalid.status_code, 403)
                    valid = await client.post(portal.local_url + 'start', data={'csrf':portal.csrf}, headers=headers)
                    self.assertEqual(valid.status_code, 303)
                    self.assertEqual(valid.headers['location'], AUTH_URL)
        finally:
            await asyncio.to_thread(portal.close)

    async def test_denial_ends_wait_without_token(self):
        portal = reader.Callback(0)
        portal.expected_state = 'fixture-state'
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                bad = await client.get(portal.local_url + 'callback?error=access_denied&state=wrong')
                self.assertEqual(bad.status_code, 400)
                self.assertFalse(portal.completed)
                response = await client.get(portal.local_url + 'callback?error=access_denied&state=fixture-state')
                self.assertEqual(response.status_code, 303)
                with self.assertRaises(LoginDenied):
                    await portal.receive()
        finally:
            await asyncio.to_thread(portal.close)

    async def test_timeout_is_classified_without_secret_output(self):
        portal = reader.Callback(0, login_timeout=0)
        try:
            with self.assertRaises(LoginTimeout) as raised:
                await portal.receive()
            failure = ExceptionGroup('fixture-sensitive-detail', [raised.exception, RuntimeError('fixture-secret')])
            message = reader.safe_failure(failure)
            self.assertIn('callback', message)
            self.assertNotIn('fixture-sensitive-detail', message)
            self.assertNotIn('fixture-secret', message)
        finally:
            await asyncio.to_thread(portal.close)

    async def test_rejects_write_scope_and_other_origin(self):
        portal = reader.Callback(0)
        try:
            for url in [AUTH_URL.replace('minutes.qwennote.cn','other.invalid'), AUTH_URL + '+qwennote%3Awrite']:
                with self.assertRaises(ValueError):
                    await portal.redirect(url)
        finally:
            await asyncio.to_thread(portal.close)

    async def test_write_login_accepts_only_the_write_scope(self):
        portal = reader.Callback(0, scope=reader.WRITE_SCOPE, ready_timeout=0)
        try:
            with self.assertRaises(ValueError):
                await portal.redirect(AUTH_URL)  # read-only scope is not what this login asked for
            with self.assertRaises(LoginTimeout):  # accepted; it then waits for the user, who never comes here
                await portal.redirect(AUTH_URL + '+qwennote%3Awrite')
        finally:
            await asyncio.to_thread(portal.close)

    async def test_expired_before_start_is_distinct(self):
        portal = reader.Callback(0, ready_timeout=0)
        try:
            with self.assertRaises(LoginTimeout) as raised:
                await portal.redirect(AUTH_URL)
            self.assertEqual(raised.exception.stage, 'start')
            self.assertIn('before browser sign-in', reader.safe_failure(raised.exception))
        finally:
            await asyncio.to_thread(portal.close)


if __name__ == '__main__':
    unittest.main()
