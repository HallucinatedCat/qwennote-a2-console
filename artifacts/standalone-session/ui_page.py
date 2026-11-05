"""Build the single-page UI from a replaceable HTML template and translation catalogs.

Used by web_backend.py and tools/ui_preview.py so both serve the same default page.
The optional template argument selects an independent regular UTF-8 HTML file (maximum
2 MiB), read again on every page request. Placeholders (each must appear exactly once):
  __CSRF__   per-process CSRF value
  __NONCE__  per-response CSP script nonce
  __I18N__   JSON object {locale: catalog} built from i18n/*.json
"""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
I18N=ROOT/'i18n'
MAX_CATALOG=512*1024
MAX_TEMPLATE=2*1024*1024


def load_catalogs(directory=I18N):
    """Read every <locale>.json. English must exist; it is the fallback for missing keys."""
    catalogs={}
    for path in sorted(Path(directory).glob('*.json')):
        if path.is_symlink() or path.stat().st_size>MAX_CATALOG:raise ValueError('Unexpected catalog file')
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('meta'),dict) or not isinstance(data.get('ui'),dict):
            raise ValueError('Catalog needs meta and ui objects')
        catalogs[path.stem]=data
    if 'en' not in catalogs:raise ValueError('English catalog required')
    return catalogs


def csp(nonce):
    """Content-Security-Policy for the page (with its script nonce) or, with None, for everything else."""
    script="script-src 'nonce-"+nonce+"'" if nonce else "script-src 'none'"
    return ("default-src 'self'; "+script+"; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; connect-src 'self'; base-uri 'none'; form-action 'self'")


def script_json(value):
    """JSON that is safe inside an inline <script>: no tag close or HTML comment openers."""
    return (json.dumps(value,ensure_ascii=False,separators=(',',':'))
            .replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
            .replace('\u2028','\\u2028').replace('\u2029','\\u2029'))


def render(csrf,nonce,root=ROOT,catalogs=None,template=None):
    path=Path(template) if template is not None else Path(root)/'web.html'
    try:
        if path.is_symlink() or not path.is_file():raise ValueError('Unexpected frontend template')
        data=path.read_bytes()
    except OSError:
        raise ValueError('Unexpected frontend template') from None
    if len(data)>MAX_TEMPLATE:raise ValueError('Frontend template is too large')
    page=data.decode('utf-8')
    for marker in ('__CSRF__','__NONCE__','__I18N__'):
        if page.count(marker)!=1:raise ValueError('Frontend template placeholder count changed')
    catalogs=load_catalogs(Path(root)/'i18n') if catalogs is None else catalogs
    # Substitute the server values before the catalogs, so catalog text can never be rewritten.
    return page.replace('__CSRF__',csrf).replace('__NONCE__',nonce).replace('__I18N__',script_json(catalogs))
