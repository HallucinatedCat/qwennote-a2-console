"""Replaceable frontend template loading and serving."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

import ui_page
from web_backend import make_app


PAGE = """<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width\"><title>Custom</title></head><body><main id=\"custom\">__MARKER__</main><script nonce=\"__NONCE__\">const CSRF='__CSRF__';const CATALOGS=__I18N__;</script></body></html>"""


class TemplateRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'custom.html'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, marker='one'):
        self.path.write_text(PAGE.replace('__MARKER__', marker), encoding='utf-8')

    def test_custom_template_is_independent_and_reloaded(self):
        self.write()
        page = ui_page.render('csrf-one', 'nonce-one', template=self.path)
        self.assertIn('id="custom">one', page)
        self.assertIn('nonce="nonce-one"', page)
        self.assertIn("const CSRF='csrf-one'", page)
        self.assertIn('"zh-CN"', page)
        self.write('two')
        self.assertIn('id="custom">two', ui_page.render('csrf-two', 'nonce-two', template=self.path))

    def test_template_must_be_a_regular_file_with_exact_placeholders(self):
        for content in (
            '<script nonce="__NONCE__">__CSRF__</script>',
            PAGE.replace('__MARKER__', '__I18N__'),
            PAGE.replace('__CSRF__', '__CSRF____CSRF__'),
        ):
            self.path.write_text(content, encoding='utf-8')
            with self.subTest(content=content[:60]):
                with self.assertRaises(ValueError):
                    ui_page.render('csrf', 'nonce', template=self.path)
        with self.assertRaises(ValueError):
            ui_page.render('csrf', 'nonce', template=self.path.parent / 'missing.html')

    def test_linked_or_non_utf8_templates_and_linked_catalogs_are_refused(self):
        self.write()
        link = self.path.parent / 'link.html'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            ui_page.render('csrf', 'nonce', template=link)
        self.path.write_bytes(PAGE.encode('utf-8').replace(b'__MARKER__', b'\xff'))
        with self.assertRaises(ValueError):
            ui_page.render('csrf', 'nonce', template=self.path)
        catalogs = self.path.parent / 'i18n'
        catalogs.mkdir()
        (catalogs / 'en.json').symlink_to(ui_page.I18N / 'en.json')
        with self.assertRaises(ValueError):
            ui_page.load_catalogs(catalogs)

    def test_template_size_is_bounded(self):
        self.path.write_text(PAGE.replace('__MARKER__', 'x' * (2 * 1024 * 1024 + 1)), encoding='utf-8')
        with self.assertRaises(ValueError):
            ui_page.render('csrf', 'nonce', template=self.path)


class CustomTemplateWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_serves_replaceable_template_with_csp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'custom.html'
            path.write_text(PAGE.replace('__MARKER__', 'first'), encoding='utf-8')
            backend = SimpleNamespace(csrf='csrf', cookie='cookie', port=1, start=AsyncMock(), stop=AsyncMock())
            client = TestClient(TestServer(make_app(backend, frontend=path)))
            await client.start_server()
            backend.port = client.server.port
            try:
                response = await client.get('/')
                self.assertEqual(response.status, 200)
                self.assertIn('id="custom">first', await response.text())
                self.assertIn("script-src 'nonce-", response.headers['Content-Security-Policy'])
                path.write_text(PAGE.replace('__MARKER__', 'second'), encoding='utf-8')
                response = await client.get('/')
                self.assertIn('id="custom">second', await response.text())
            finally:
                await client.close()


if __name__ == '__main__':
    unittest.main()
