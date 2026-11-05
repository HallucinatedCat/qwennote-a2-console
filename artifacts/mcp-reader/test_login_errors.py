"""Stdlib-only: every OAuth error callback with a valid state ends the wait promptly."""
import asyncio
import unittest
import urllib.error
import urllib.request
from login_portal import Callback, LoginDenied

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NOREDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def status(url):
    try:
        with NOREDIRECT.open(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class ErrorCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, code, expected_reason):
        portal = Callback(0, validator=lambda path, state: (_ for _ in ()).throw(AssertionError('success path not expected')))
        portal.expected_state = 'fixture-state'
        try:
            self.assertEqual(await asyncio.to_thread(status, portal.local_url + f'callback?error={code}&state=wrong'), 400)
            self.assertFalse(portal.completed)
            self.assertEqual(await asyncio.to_thread(status, portal.local_url + f'callback?error={code}&state=fixture-state'), 303)
            with self.assertRaises(LoginDenied) as raised:
                await asyncio.wait_for(portal.receive(), 5)
            self.assertEqual(raised.exception.reason, expected_reason)
        finally:
            await asyncio.to_thread(portal.close)

    async def test_server_error_is_terminal(self):
        await self.check('server_error', 'server_error')

    async def test_invalid_scope_is_terminal(self):
        await self.check('invalid_scope', 'invalid_scope')

    async def test_unknown_code_is_not_echoed(self):
        await self.check('%3Cscript%3E', 'other')

    async def test_access_denied_still_works(self):
        await self.check('access_denied', 'access_denied')


if __name__ == '__main__':
    unittest.main()
