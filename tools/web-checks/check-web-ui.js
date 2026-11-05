#!/usr/bin/env node
// Static checks for artifacts/standalone-session/web.html. Reads the real page instead of a copy.
//   node tools/web-checks/check-web-ui.js [path/to/web.html]
'use strict';
const fs=require('fs');const path=require('path');const vm=require('vm');
const file=process.argv[2]||path.join(__dirname,'..','..','artifacts','standalone-session','web.html');
const page=fs.readFileSync(file,'utf8');const problems=[];
const scripts=[...page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
if(scripts.length!==1)problems.push(`expected exactly one inline script, found ${scripts.length}`);
for(const [,attrs,body] of scripts){
  if(!/nonce="__NONCE__"/.test(attrs))problems.push('script tag must carry nonce="__NONCE__" (replaced per response by the backend)');
  try{new vm.Script(body,{filename:'web.html <script>'})}catch(error){problems.push('script syntax: '+error.message)}
  if(/\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write/.test(body))problems.push('script must build DOM with textContent/createElement only');
  if(/\beval\(|new Function\(/.test(body))problems.push('script must not evaluate strings');
}
const count=(s)=>page.split(s).length-1;
if(count('__CSRF__')!==1)problems.push(`expected one __CSRF__ placeholder, found ${count('__CSRF__')}`);
if(count('__NONCE__')!==1)problems.push(`expected one __NONCE__ placeholder, found ${count('__NONCE__')}`);
if(count('__I18N__')!==1)problems.push(`expected one __I18N__ placeholder (catalogs are inlined by ui_page.py), found ${count('__I18N__')}`);
// Catalogs: every locale has English's keys, and every data-i18n key in the markup exists.
const dir=path.join(path.dirname(file),'i18n');
try{
  const catalogs=Object.fromEntries(fs.readdirSync(dir).filter(f=>f.endsWith('.json')).map(f=>[f.slice(0,-5),JSON.parse(fs.readFileSync(path.join(dir,f),'utf8'))]));
  const en=catalogs.en?.ui;if(!en)throw new Error('i18n/en.json missing');
  for(const [code,c] of Object.entries(catalogs)){
    const missing=Object.keys(en).filter(k=>!(k in c.ui));if(missing.length)problems.push(`${code}.json is missing ${missing.length} keys, e.g. ${missing.slice(0,3).join(', ')}`);
  }
  const markup=page.split('<script')[0];
  const used=[...markup.matchAll(/data-i18n="([^"]+)"/g)].map(m=>m[1]);
  for(const [,attrs] of markup.matchAll(/data-i18n-attr="([^"]+)"/g))for(const pair of attrs.split(';'))used.push(pair.split('=')[1]);
  const unknown=[...new Set(used)].filter(k=>!(k in en));if(unknown.length)problems.push('markup uses unknown i18n keys: '+unknown.join(', '));
}catch(error){problems.push('i18n catalogs: '+error.message)}
if(/[–—]/.test(page))problems.push('visible copy must not contain en or em dashes');
if(/<(link|script)[^>]+(src|href)="https?:/i.test(page))problems.push('no external scripts or stylesheets: the page runs offline under a strict CSP');
if(/\bstyle="[^"]*\bon\w+=|\son(click|load|error|input|change)=/i.test(page))problems.push('no inline event handler attributes (CSP blocks them)');
if(problems.length){console.error('web.html check failed:\n- '+problems.join('\n- '));process.exit(1)}
console.log('web.html check passed: one nonce script, valid syntax, no HTML injection sinks, no external assets, catalogs complete.');
