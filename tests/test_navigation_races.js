const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html','utf8');
const kpiSource = html.slice(html.indexOf('  async function load() {'),html.indexOf('  // ---------- графики'));
const viewRequestSource = html.slice(html.indexOf('  function viewRequest()'),html.indexOf('  async function load() {'));
const moneySource = html.slice(html.indexOf('  async function loadMoney()'),html.indexOf('  function renderMoney('));
const callsSource = html.slice(html.indexOf('  async function loadCalls() {'),html.indexOf('  function renderCalls('));
function fixture(source,view) {
  const pending=[], painted=[], sides=[];
  const main={innerHTML:''};
  const state={view,login:'tender',month:'2026-11',boot:{me:{role:'employee'},current_month:'2026-11',employees:[{login:'tender',position:'tender'}]}};
  const context={state,PREVIEW:null,METRICS:{},sideCache:{},loadGeneration:0,callsGeneration:0,
    document:{getElementById:()=>main},api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
    esc:x=>{throw new Error('unexpected loader error: '+x);},
    renderEmp:d=>painted.push(d),renderFunnel:d=>painted.push(d),renderCalls:d=>painted.push(d),
    renderSide:d=>sides.push(d),
    clearTimeout:()=>{},moneyTimer:null,setTimeout:()=>0,renderMoney:d=>painted.push(d),loadError:e=>e.message,viewRequestSource,gptReleasePreviews:()=>{},
    periodRange:()=>['2026-11-01','2026-11-30'],funnelPeriod:()=>['',''],
    encodeURIComponent,loadCalls:()=>{},loadWorkload:()=>{},loadMoney:()=>{},loadSuppliers:()=>{},loadMetrics:()=>{}};
  vm.runInNewContext(viewRequestSource+source+'\nthis.run='+(source===callsSource?'loadCalls':source===moneySource?'loadMoney':'load')+';',context);
  return {state,pending,painted,sides,run:context.run};
}
(async()=>{
  for (const mode of ['date','fact']) {
    const f=fixture(kpiSource,'funnel');
    f.state.fMode=mode;
    f.state.fDirections=['kpi','bpla','epc'];
    const request=f.run();
    assert.equal(f.pending.length,2);
    for (const pending of f.pending) {
      const query=new URL(pending.url,'http://localhost').searchParams;
      assert.equal(query.get('directions'),'kpi,bpla,epc');
      assert.equal(query.get('mode'),mode);
      pending.resolve({});
    }
    await request;
  }
  for (const [source,view] of [[kpiSource,'emp'],[kpiSource,'funnel'],[callsSource,'calls'],[moneySource,'money']]) {
    const f=fixture(source,view), request=f.run();
    f.state.view=view==='calls'?'emp':'calls';
    f.pending.forEach(p=>p.resolve({old:true}));await request;
    assert.equal(f.painted.length,0,'late '+view+' response must not replace the active tab');
  }
  const f=fixture(callsSource,'calls'),old=f.run();
  f.state.cDeal='7';const latest=f.run();
  f.pending[1].resolve({latest:true});await latest;
  f.pending[0].resolve({old:true});await old;
  assert.equal(f.painted.length,1,'older call filters cannot overwrite newer results');
  assert.equal(f.painted[0].latest,true);
  const saved=fixture(moneySource,'money');
  saved.state.view='calls';
  await saved.run();
  assert.equal(saved.pending.length,0,'a completed Money save cannot reload Money over another tab');
  const manager=fixture(kpiSource,'emp');
  manager.state.boot.me.role='manager';
  const card=manager.run();
  const employee=manager.pending.find(p=>p.url.startsWith('/api/employee?'));
  assert.ok(employee,'employee request must start without waiting for the team overview');
  employee.resolve({employee:true});
  await card;
  assert.equal(manager.painted[0].employee,true,'employee card renders while overview is pending');
  manager.state.view='calls';
  const overview=manager.pending.find(p=>p.url.startsWith('/api/overview?'));
  assert.ok(overview,'sidebar overview still loads in the background');
  const sideCount=manager.sides.length;
  overview.resolve({groups:[]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(manager.sides.length,sideCount,'late sidebar response cannot repaint a different tab');
  console.log('Async navigation rendering: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
