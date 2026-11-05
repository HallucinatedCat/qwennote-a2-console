"""Keep the frontend's machine contract, documentation and backend routes aligned."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent
CONTRACT_PATH = ROOT / 'frontend-contract.json'
DOCUMENT_PATH = ROOT / 'FRONTEND_API.md'


def backend_actions():
    source = (ROOT / 'web_backend.py').read_text(encoding='utf-8')
    source = source[source.index('    async def post(request):'):]
    names = set(re.findall(r"(?:if|elif) action=='([^']+)'", source))
    for group in re.findall(r'elif action in \(([^)]+)\)', source):
        names.update(re.findall(r"'([^']+)'", group))
    return names


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding='utf-8'))
        cls.document = DOCUMENT_PATH.read_text(encoding='utf-8')

    def test_every_backend_action_has_an_exact_contract_and_reference(self):
        actions = self.contract['actions']
        self.assertEqual(set(actions), backend_actions())
        for name, spec in actions.items():
            with self.subTest(action=name):
                self.assertIn(f'### `{name}`', self.document)
                self.assertTrue(spec['purpose'])
                self.assertTrue(spec['response'])
                self.assertTrue(spec['side_effects'])
                self.assertIsInstance(spec['errors'], list)
                request = spec['request']
                self.assertEqual(set(request), {'required', 'optional', 'fields'})
                self.assertEqual(set(request['required'] + request['optional']), set(request['fields']))
                self.assertFalse(set(request['required']) & set(request['optional']))

    def test_every_documented_action_has_a_browser_caller(self):
        page = (ROOT / 'web.html').read_text(encoding='utf-8')
        called = set(re.findall(r"\b(?:api|run)\('([^']+)'", page))
        self.assertEqual(set(self.contract['actions']), called)

    def test_reference_fields_match_the_machine_contract(self):
        for name, spec in self.contract['actions'].items():
            with self.subTest(action=name):
                section = self.document.split(f'### `{name}`', 1)[1].split('\n### ', 1)[0]
                request = spec['request']
                for field, description in request['fields'].items():
                    presence = 'Required' if field in request['required'] else 'Optional'
                    self.assertIn(f'| `{field}` | {description} | {presence} |', section)

    def test_transport_and_template_contract_are_explicit(self):
        self.assertEqual(self.contract['version'], 1)
        self.assertEqual(self.contract['template']['placeholders'], ['__CSRF__', '__NONCE__', '__I18N__'])
        self.assertEqual(self.contract['template']['maximum_bytes'], 2 * 1024 * 1024)
        self.assertIn('--frontend', self.contract['template']['replacement'])
        self.assertEqual(self.contract['transport']['post_prefix'], '/api/')
        self.assertEqual(self.contract['transport']['csrf_header'], 'X-CSRF-Token')
        for key in ('GET /api/status', 'GET /healthz'):
            self.assertIn(key, self.contract['reads'])
            self.assertIn(key, self.document)


if __name__ == '__main__':
    unittest.main()
