"""Translation catalogs stay complete and safe. Stdlib only; runs without aiohttp."""
import ast
import json
from pathlib import Path
import re
import unittest
import capabilities
import generation_settings
import notification_settings
import public_errors
import settings_catalog
import ui_page

ROOT=Path(__file__).resolve().parent
PAGE=(ROOT/'web.html').read_text(encoding='utf-8')
CATALOGS=ui_page.load_catalogs()
EN=CATALOGS['en']['ui']
OTHERS={code:c for code,c in CATALOGS.items() if code!='en'}
PUBLIC={cls.__name__ for cls in public_errors.PUBLIC_CLASSES}
# live_diagnostics imports the websocket client; read its literal REASONS instead of importing it.
REASONS=next(ast.literal_eval(node.value) for node in ast.parse((ROOT/'live_diagnostics.py').read_text()).body
             if isinstance(node,ast.Assign) and getattr(node.targets[0],'id','')=='REASONS')
PLACEHOLDER=re.compile(r'\{(\w+)\}')
# Fixed English labels the backend sends outside of errors (notification descriptor and organization list).
DESCRIPTOR_LABELS=('Current organization not in available options','No organization selected',
                   'Load organizations to view the current selection','Personal edition')


def forms(value):
    return list(value.values()) if isinstance(value,dict) else [value]


def placeholders(value):
    return {name for text in forms(value) for name in PLACEHOLDER.findall(text)}


def public_messages():
    found={public_errors.GENERIC_MESSAGE}
    for path in sorted(ROOT.glob('*.py')):
        if path.name.startswith('test_'):continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Call) and getattr(node.func,'id',None) in PUBLIC and node.args and isinstance(node.args[0],ast.Constant):
                found.add(node.args[0].value)
    return found


def page_keys():
    """Keys the page names literally. Dynamic families are checked separately below."""
    markup,script=PAGE.split('<script nonce="__NONCE__">')
    script=script.split('</script>')[0]
    keys=set(re.findall(r'data-i18n="([^"]+)"',markup))
    for attrs in re.findall(r'data-i18n-attr="([^"]+)"',markup):
        keys.update(pair.split('=',1)[1] for pair in attrs.split(';') if '=' in pair)
    # A literal key, not the prefix of a concatenated one ('kind.'+name is a dynamic family).
    literal=r"'([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+)'(?!\s*\+)"
    keys.update(re.findall(r"\b(?:t|tpl|tk)\("+literal,script))
    for name in re.findall(r"\bempty\('([^']+)','([^']+)'\)",script):keys.update(name)
    for prefix in re.findall(r"\bsay\("+literal,script):keys.update({prefix+'.title',prefix+'.body'})
    for prefix in re.findall(r"\bconfirmDialog\('([a-zA-Z0-9_.-]+)'\)",script):keys.update({prefix+'.title',prefix+'.text',prefix+'.ok'})
    for prefix in re.findall(r"say\(restore\?'([^']+)':'([^']+)'",script):
        for p in prefix:keys.update({p+'.title',p+'.body'})
    return keys


class CatalogShapeTests(unittest.TestCase):
    def test_every_locale_has_every_key_with_the_same_placeholders(self):
        self.assertTrue(OTHERS,'at least one translation besides English')
        for code,catalog in OTHERS.items():
            ui=catalog['ui']
            with self.subTest(locale=code):
                self.assertEqual(sorted(set(EN)-set(ui)),[],'missing keys')
                self.assertEqual(sorted(set(ui)-set(EN)),[],'keys not in English')
                for key,value in EN.items():
                    self.assertEqual(placeholders(ui[key]),placeholders(value),key)
                    if isinstance(ui[key],dict):self.assertIn('other',ui[key],key)

    def test_values_are_plain_text(self):
        for code,catalog in CATALOGS.items():
            for key,value in list(catalog['ui'].items())+list(catalog.get('server',{}).items()):
                for text in forms(value):
                    with self.subTest(locale=code,key=key):
                        self.assertIsInstance(text,str);self.assertTrue(text.strip())
                        self.assertNotRegex(text,r'[<>]','no markup in catalog text')
                        self.assertNotRegex(text,'[–—]','no en or em dashes in UI copy')

    def test_plural_entries_use_cldr_categories(self):
        for code,catalog in CATALOGS.items():
            for key,value in catalog['ui'].items():
                if isinstance(value,dict):
                    self.assertLessEqual(set(value),{'zero','one','two','few','many','other'},f'{code}:{key}')

    def test_meta_names_a_locale(self):
        for code,catalog in CATALOGS.items():
            self.assertEqual(catalog['meta'].get('locale'),code);self.assertTrue(catalog['meta'].get('name'))


class CoverageTests(unittest.TestCase):
    def test_backend_refusals_are_translated(self):
        wanted=public_messages()|set(DESCRIPTOR_LABELS)
        self.assertGreater(len(wanted),50)
        for code,catalog in OTHERS.items():
            with self.subTest(locale=code):
                self.assertEqual(sorted(wanted-set(catalog.get('server',{}))),[],'add these to "server" in i18n/'+code+'.json')

    def test_server_catalog_has_no_stale_entries(self):
        wanted=public_messages()|set(DESCRIPTOR_LABELS)
        for code,catalog in OTHERS.items():
            self.assertEqual(sorted(set(catalog.get('server',{}))-wanted),[],code)

    def test_every_key_named_in_the_page_exists(self):
        keys=page_keys()
        self.assertGreater(len(keys),150,'key scanner found suspiciously few keys')
        self.assertEqual(sorted(k for k in keys if k not in EN),[])

    def test_dynamic_key_families_exist(self):
        wanted=set()
        self.assertTrue(all(level in capabilities.LEVELS for _,_,level,_ in capabilities.VERIFICATION))
        wanted|={f'tab.{x}' for x in capabilities.VIEWS}
        wanted|={f'setting.{x}' for x in settings_catalog.METADATA}|{f'setting.{x}' for x in generation_settings.KEYS}
        wanted|={f'evidence.{e}' for e,_ in settings_catalog.METADATA.values()}
        for key,(_,choices) in settings_catalog.SIMPLE.items():wanted|={f'choice.{key}.{v}' for v in choices}
        for key,choices in generation_settings.CHOICES.items():wanted|={f'choice.{key}.{v}' for v in choices}
        wanted|={f'choice.{notification_settings.METHOD}.{v}' for v in notification_settings.METHODS}
        wanted|={f'capture.finish.{r}' for r in REASONS}
        wanted|={f'err.{cls.code}' for cls in public_errors.PUBLIC_CLASSES}|{'err.offline','err.session','err.failed'}
        backend=(ROOT/'web_backend.py').read_text()
        wanted|={f'kind.{k}' for k in re.findall(r"self\.emit\('([a-z-]+)'",backend)}|{'kind.rtc'}
        wanted|={f'link.{s}' for s in re.findall(r"self\.connection='([a-z-]+)'",backend)}
        self.assertEqual(sorted(k for k in wanted if k not in EN),[])

    def test_every_setting_description_is_translated(self):
        for key,(_,_,grade) in settings_catalog.DESCRIPTIONS.items():
            self.assertIn('settingHelp.'+key,EN)
            self.assertIn('settingValues.'+key,EN)
            self.assertIn('meaning.'+grade,EN)
        self.assertIn('settingHelp.unknown',EN);self.assertIn('settingValues.unknown',EN)


class RenderTests(unittest.TestCase):
    def test_render_fills_every_placeholder_once(self):
        page=ui_page.render('csrf-value','nonce-value')
        for marker in ('__CSRF__','__NONCE__','__I18N__'):self.assertNotIn(marker,page)
        self.assertEqual(page.count('nonce="nonce-value"'),1)
        self.assertIn('"zh-CN"',page)

    def test_catalog_text_cannot_close_the_script(self):
        hostile={'en':{'meta':{'locale':'en','name':'x'},'ui':{'k':'</script><script>alert(1)</script> __CSRF__'},'server':{}}}
        page=ui_page.render('csrf-value','nonce-value',catalogs=hostile)
        script=page.split('<script nonce="nonce-value">')[1]
        self.assertNotIn('</script><script>',script.split('</script>\n</body>')[0])
        self.assertIn('__CSRF__',page,'catalog text is not rewritten by server substitution')
        decoded=json.loads(ui_page.script_json(hostile))
        self.assertEqual(decoded,hostile)


if __name__=='__main__':unittest.main()
