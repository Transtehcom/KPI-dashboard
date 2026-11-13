// Run the actual hash-change handler against browser-history transitions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('window.addEventListener("hashchange",');
const end = html.indexOf('\n  start();', start);
assert.ok(start >= 0 && end > start);
let hashChange;
const state = {boot:{me:{role:'employee',login:'tender'}},view:'emp',login:'tender'};
const location = {hash:'#calls'};
let renders=0, loads=0;
vm.runInNewContext(html.slice(start,end), {state, location, PREVIEW:null,
  window:{addEventListener:(event,fn)=>{assert.equal(event,'hashchange');hashChange=fn;}},
  isFunnelHash:h=>h==='funnel'||h==='funnel-fact',metricsView:()=>null,supFromHash:()=>{},
  renderShell:()=>renders++,load:()=>loads++});
hashChange();
assert.equal(state.view,'calls','employee history must open Calls');
const previousLoads=loads;
hashChange();
assert.equal(loads,previousLoads,'the hash event following a tab click must not reset the view');
location.hash='#funnel-fact';hashChange();assert.equal(state.view,'funnel');assert.equal(state.fMode,'fact');
location.hash='#calls';hashChange();assert.equal(state.view,'calls');
location.hash='#workload';hashChange();assert.equal(state.view,'emp','employee cannot open Workload by URL');
location.hash='#staff';hashChange();assert.equal(state.view,'staff','employee can open read-only staff directory by URL');
state.boot.me.role='manager';state.view='team';location.hash='#calls';hashChange();assert.equal(state.view,'calls');
location.hash='#workload';hashChange();assert.equal(state.view,'workload');
assert.ok(renders && loads);
console.log('Navigation history and Workload access: OK');

location.hash='#gpt';hashChange();assert.equal(state.view,'gpt','manager can open GPT');
state.boot.me.role='employee';hashChange();assert.equal(state.view,'gpt','employee can open own GPT by URL');
location.hash='#home';hashChange();assert.equal(state.view,'home');
state.boot.me.role='manager';state.view='emp';state.login='alice';
location.hash='#home';hashChange();
const rendersBeforeBack=renders;
location.hash='#emp-alice';hashChange();
assert.equal(state.view,'emp','back from home restores the same employee card');
assert.ok(renders>rendersBeforeBack,'employee route restores the KPI shell');
location.hash='#home';hashChange();
location.hash='#emp-bob';hashChange();
assert.equal(state.view,'emp');assert.equal(state.login,'bob');
state.boot.me.role='employee';
location.hash='#calls';hashChange();assert.equal(state.view,'calls');
location.hash='';hashChange();assert.equal(state.view,'home');
state.boot.me.role='manager';location.hash='#team';hashChange();assert.equal(state.view,'team');
location.hash='#home';hashChange();assert.equal(state.view,'home');

assert.ok(html.includes('else if (h === "gpt" && !PREVIEW)'),"employee can reload the GPT route");
