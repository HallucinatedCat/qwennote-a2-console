#!/usr/bin/env node
// Exercise the shipped formatter and translation helpers, without a DOM or cloud calls.
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const file=process.argv[2]||path.join(__dirname,'../../artifacts/standalone-session/web.html');
const page=fs.readFileSync(file,'utf8');
const catalogs=Object.fromEntries(['en','zh-CN'].map(lang=>[lang,JSON.parse(fs.readFileSync(path.join(path.dirname(file),'i18n',lang+'.json'),'utf8'))]));
const catalog=page.slice(page.indexOf('const catalog='),page.indexOf('function t('));
const translate=page.slice(page.indexOf('function t('),page.indexOf('function serverText'));
const fmt=page.slice(page.indexOf('const fmt='),page.indexOf('// State'));
const formatter=page.slice(page.indexOf('function quotaNumber('),page.indexOf('function quotaTable('));
assert(catalog&&translate&&fmt&&formatter);
const context=vm.createContext({CATALOGS:catalogs,locale:'en',Intl,console});
vm.runInContext(catalog+translate+fmt+formatter,context);
const cases=[[null,'KB','Awaiting value'],[0,'KB','0 KB'],[1023,'KB','1,023 KB'],[1024,'KB','1 MB'],[1048576,'KB','1 GB'],[1572864,'KB','1.5 GB'],[1.5,'G','1.5 G'],[1,'minutes','1 minute'],[2,'minutes','2 minutes'],[1,'篇','1 summary'],[2,'篇','2 summaries'],[1,'次','1 question'],[2,'次','2 questions'],[0.0001,'KB','Less than 0.001 KB']];
for(const [value,unit,expected] of cases)assert.equal(context.quotaNumber(value,unit),expected);
context.locale='zh-CN';
for(const [value,unit,expected] of [[null,'KB','等待数据'],[1,'minutes','1 分钟'],[2,'篇','2 篇'],[2,'次','2 次'],[1048576,'KB','1 GB'],[0.0001,'KB','小于 0.001 KB']])assert.equal(context.quotaNumber(value,unit),expected);
for(const key of ['quota.notReported','quota.lessThan','quota.minuteCount','quota.summaryCount','quota.questionCount'])for(const lang of ['en','zh-CN'])assert(catalogs[lang].ui[key],`${lang} missing ${key}`);
console.log('Quota formatting: 20 assertions passed (units, counts, zero, missing and tiny values, English/Chinese).');
