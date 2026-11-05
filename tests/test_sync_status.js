const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const start=html.indexOf('  let syncTimer = null;');
const source=html.slice(start,html.indexOf('  function renderShell()',start));
function fixture(){
  const timers=[],toasts=[],button={disabled:false,textContent:'Обновить данные'},calls=[];
  let status={running:false,last_sync:{id:1,status:'ok',finished:'2026-11-03T12:15:21'}};
  const state={boot:{last_sync:status.last_sync,sync_running:false}};
  let renders=0,loads=0;
  const context={state,PREVIEW:null,sideCache:{old:true},
    document:{getElementById:()=>button,querySelector:()=>null,querySelectorAll:()=>[]},toast:s=>toasts.push(s),
    clearTimeout:()=>{},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},
    renderShell:()=>renders++,load:()=>loads++,
    api:async path=>{calls.push(path);return path==='/api/sync'?status:{last_sync:status.last_sync,sync_running:false};}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,timers,toasts,button,calls,state,setStatus:s=>status=s,
    tick:async()=>{const t=timers.shift();assert.ok(t);await t.fn();},
    rendered:()=>renders,loaded:()=>loads};
}
(async()=>{
  const idle=fixture();idle.context.watchSync();
  assert.equal(idle.button.disabled,false,'idle monitoring must leave manual update usable');
  assert.ok(idle.timers[0].ms<=30000,'automatic updates must be discovered without reloading');
  await idle.tick();
  assert.equal(idle.toasts.length,0,'unchanged idle status must not announce another update');
  assert.equal(idle.rendered(),0,'idle polling must preserve the page and edits');
  assert.equal(idle.timers.length,1,'monitoring must continue while idle');
  idle.setStatus({running:false,last_sync:{id:2,status:'ok',finished:'2026-11-03T16:00:00'}});
  await idle.tick();
  assert.equal(idle.state.boot.last_sync.id,2);
  assert.equal(idle.rendered(),1);assert.equal(idle.loaded(),1);
  assert.equal(Object.keys(idle.context.sideCache).length,0,'new data must invalidate cached reports');
  const editing=fixture();
  editing.context.document.querySelectorAll=()=>[{tagName:'INPUT',type:'text',value:'Unsaved employee name',defaultValue:''}];
  editing.context.watchSync();
  editing.setStatus({running:false,last_sync:{id:2,status:'ok',finished:'2026-11-03T16:00:00'}});
  await editing.tick();
  assert.equal(editing.rendered(),0,'automatic completion must preserve unsaved forms');
  assert.equal(editing.state.boot.last_sync.id,2,'status must update even while content refresh is deferred');
  editing.context.document.querySelectorAll=()=>[];
  await editing.tick();assert.equal(editing.rendered(),1,'content should refresh once the draft is saved or closed');
  const structural=fixture();
  structural.context.document.querySelector=selector=>selector.includes('#empForm')?{}:null;
  structural.context.watchSync();
  structural.setStatus({running:false,last_sync:{id:2,status:'ok'}});
  await structural.tick();
  assert.equal(structural.rendered(),0,'open employee edits must retain unsaved row additions/removals');
  const active=fixture();active.context.watchSync(true);
  assert.equal(active.button.disabled,true);
  active.setStatus({running:true,step:'1С: supplier_orders'});
  await active.tick();assert.equal(active.button.title,'1С: supplier_orders');
  assert.equal(active.timers[0].ms,3000);
  const stale=fixture();let finish;
  stale.context.api=()=>new Promise(resolve=>finish=resolve);
  stale.context.watchSync();const oldTick=stale.tick();
  stale.context.watchSync();
  finish({running:false,last_sync:{id:99,status:'ok'}});await oldTick;
  assert.equal(stale.rendered(),0,'an obsolete status request must not redraw a new page');
  console.log('Automatic sync status polling: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
