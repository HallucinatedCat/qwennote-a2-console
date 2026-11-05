#!/usr/bin/env python3
"""Build a static, read-only demo of the web UI (no backend).

The page is the real web.html. Its /api calls are answered in the browser by
tools/ui_preview.py's synthetic Preview, run under Pyodide. Actions that would
change a recorder, a note or a setting are refused, so the demo is read-only.

    python3 tools/build_demo.py              # writes dist/demo/
    python3 -m http.server -d dist/demo      # look at it locally
"""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
HOST=ROOT/'artifacts'/'standalone-session'
sys.path.insert(0,str(ROOT/'tools'))
import ui_preview  # noqa: E402  (also puts HOST on sys.path)
import ui_page  # noqa: E402

PYODIDE='https://cdn.jsdelivr.net/pyodide/v0.27.7/full/'
NONCE='qwennote-demo'
# Everything that would change a recorder, note, recording or setting in the real console.
WRITES=sorted('''slicing_save voiceprint_save voiceprint_clear recording_make_note recording_recover recording_delete
dangerous note_delete device_unbind note_mcp_write record confirm_stopped setting restore generation restore_generation
notification restore_notification capture_start rtc_start note_share_save device_name_save device_name_restore
memo_update memo_delete note_speaker_rename recording_marker_save recording_marker_delete recording_group_save
recording_group_delete recording_move ble_pairing_file note_replace_save note_tag_create note_tag_rename
note_tag_delete note_tags_save note_summary_save note_regenerate note_favorite mcp_login_start mcp_login_finish'''.split())

LINK='style="padding:7px 10px;border-radius:6px;color:#fff;text-decoration:none"'
# Shown inside the preview's scenario switcher, which already places itself for phone and desktop.
BANNER=('<span style="padding:7px 10px;color:#ffd479">Read-only demo · invented data</span>'
        f'<a {LINK} href="https://github.com/HallucinatedCat/qwennote-a2-console">Source</a>'
        '<span style="padding:7px 4px;opacity:.6">Scenario:</span>')
LOADER=('<div id="demo-loading" style="position:fixed;inset:0;z-index:99;display:grid;place-items:center;background:rgb(255 255 255/.85);'
        'font:15px system-ui;color:#222">Loading the demo (about 10 MB, first visit only)…</div>')

# Runs inside Pyodide after the sources are written to /demo.
BOOT='''
import sys,types,json,time
for name in ('aiohttp','aiohttp.resolver'):
    module=types.ModuleType(name)
    for attr in ('ClientSession','ClientTimeout','DummyCookieJar','TCPConnector','DefaultResolver'):setattr(module,attr,object)
    sys.modules[name]=module
time.sleep=lambda seconds:None  # no visible busy states needed; a real sleep would block the page
sys.path.insert(0,'/demo/tools')
import ui_preview as up
from public_errors import describe
WRITES=set(json.loads(WRITES_JSON))
preview=up.Preview(SCENARIO)

def reply(status,body,mime='application/json',headers=None):
    if mime=='application/json':body=json.dumps(body)
    return {'status':status,'body':body,'headers':{'Content-Type':mime,**(headers or {})}}

def demo_request(method,path,query,body):
    params=dict(item.split('=',1) for item in query.lstrip('?').split('&') if '=' in item)
    if method=='GET' and path=='/healthz':return reply(200,preview.health())
    if method=='GET' and path=='/api/status':
        since=params.get('since','');since=int(since) if since.isdigit() else None
        return reply(200,preview.status(since,params.get('settings_revision')))
    if method!='POST' or not path.startswith('/api/'):return reply(404,{'error':'NotFound'})
    action=path[5:]
    try:data=json.loads(body or '{}')
    except ValueError:return reply(409,{'error':'Refused','code':'refused','message':'The request body must be JSON'})
    if action.replace('-','_') in WRITES:
        return reply(409,{'error':'Refused','code':'refused','message':'Read-only demo: this action would change the recorder, a note or a setting.'})
    try:
        if action=='assistant':result=preview.act(action,data)
        else:
            with preview.lock:result=preview.act(action,data)
    except KeyError:return reply(404,{'error':'NotFound'})
    except up.Refusal as error:return reply(409,{'error':'Refused','code':error.code,'message':str(error)})
    except (ValueError,PermissionError,ConnectionError,RuntimeError) as error:return reply(409,describe(error))
    except Exception as error:return reply(502,describe(error))
    if action=='note-export':
        _,extension,mime,_=up.export.format_spec(data['kind'],data.get('file_format','txt'))
        return reply(200,result,mime,{'Content-Disposition':'attachment; filename="qwennote-'+data['kind']+'.'+extension+'"'})
    return reply(200,result)
'''

SHIM='''(()=>{
const PYODIDE=%(pyodide)s, FILES=%(files)s, BOOT=%(boot)s, WRITES=%(writes)s, SCENARIOS=%(scenarios)s;
const realFetch=window.fetch.bind(window);
function load(src){return new Promise((ok,fail)=>{const s=document.createElement('script');s.src=src;s.onload=ok;s.onerror=fail;document.head.append(s);});}
const ready=(async()=>{
  await load(PYODIDE+'pyodide.js');
  const py=await loadPyodide({indexURL:PYODIDE});
  for(const name of FILES){
    const text=await (await realFetch('py/'+name)).text();
    const path='/demo/'+name;py.FS.mkdirTree(path.slice(0,path.lastIndexOf('/')));py.FS.writeFile(path,text);
  }
  py.FS.writeFile('/demo/artifacts/standalone-session/web.html','');
  const wanted=new URLSearchParams(location.search).get('scenario');
  py.globals.set('SCENARIO',SCENARIOS.includes(wanted)?wanted:'demo');py.globals.set('WRITES_JSON',JSON.stringify(WRITES));
  py.runPython(BOOT);
  document.getElementById('demo-loading')?.remove();
  return py.globals.get('demo_request');
})();
ready.catch(()=>{const n=document.getElementById('demo-loading');if(n)n.textContent='The demo could not load (it needs cdn.jsdelivr.net).';});
window.fetch=async(input,init={})=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href);
  if(url.origin!==location.origin||!(url.pathname.startsWith('/api/')||url.pathname==='/healthz'))return realFetch(input,init);
  const handle=await ready;
  const out=handle((init.method||'GET').toUpperCase(),url.pathname,url.search,init.body==null?'':String(init.body));
  const result=out.toJs({dict_converter:Object.fromEntries});out.destroy();
  return new Response(result.body,{status:result.status,headers:result.headers});
};
})();
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out',type=Path,default=ROOT/'dist'/'demo')
    out=parser.parse_args().out
    unknown=[w for w in WRITES if not hasattr(ui_preview.Preview,'do_'+w)]
    assert not unknown,f'WRITES lists actions the preview does not have: {unknown}'
    if out.exists():shutil.rmtree(out)
    (out/'py').mkdir(parents=True)
    # Ship exactly the project modules the preview imports.
    files=sorted(str(Path(m.__file__).resolve().relative_to(ROOT)) for n,m in list(sys.modules.items())
                 if n!='__main__' and getattr(m,'__file__',None) and Path(m.__file__).resolve().is_relative_to(ROOT))
    for name in files:
        (out/'py'/name).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,out/'py'/name)
    shim=SHIM%{'pyodide':json.dumps(PYODIDE),'files':json.dumps(files),'boot':json.dumps(BOOT),
               'writes':json.dumps(WRITES),'scenarios':json.dumps(list(ui_preview.SCENARIOS))}
    (out/'demo.js').write_text(shim)
    links=BANNER+''.join(f'<a {LINK} href="?scenario={s}">{s}</a>' for s in ui_preview.SCENARIOS)
    page=ui_page.render('demo',NONCE,HOST)
    page=page.replace('<head>','<head><link rel="icon" href="data:,"><script src="demo.js"></script>',1)
    page=page.replace('</body>',LOADER+ui_preview.PICKER.format(links=links)+'</body>',1)
    assert page.count('demo.js')==1 and 'Read-only demo' in page,'web.html layout changed; update build_demo.py'
    (out/'index.html').write_text(page)
    csp=ui_page.csp(NONCE).replace("script-src 'nonce-"+NONCE+"'","script-src 'self' 'nonce-"+NONCE+"' 'wasm-unsafe-eval' https://cdn.jsdelivr.net")
    csp=csp.replace("connect-src 'self'","connect-src 'self' https://cdn.jsdelivr.net")
    (out/'_headers').write_text(f'/*\n  Content-Security-Policy: {csp}\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n')
    print(f'Demo written to {out} ({len(files)} Python files)')


if __name__=='__main__':main()
