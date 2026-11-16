const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  function workloadStats(');
assert.ok(start > 0, 'shared workload period calculations exist');
const context = {};
vm.runInNewContext(html.slice(start, html.indexOf('  async function loadWorkload()', start)), context);
const rows = [
  {id:'open', open:true, created:'2026-10-01T09:00:00', deadline:'2026-10-31T18:00:00'},
  {id:'late', open:false, created:'2026-11-01T09:00:00', closed:'2026-11-05T10:00:00', exec_at:'2026-11-05T10:00:00', deadline:'2026-11-04T18:00:00'},
  {id:'ontime', open:false, closed:'2026-11-03T10:00:00', exec_at:'2026-11-03T10:00:00', deadline:'2026-11-03T18:00:00'},
  {id:'unknown', open:false, closed:'2026-11-03T10:00:00'},
  {id:'future', open:true, deadline:'2026-11-20T12:00:00'},
  {id:'accepted', open:true, exec_at:'2026-11-02T10:00:00', deadline:'2026-11-02T18:00:00'},
];
const result = context.workloadStats(rows, '2026-11-01', '2026-11-30', '2026-11-16T12:00:00');
assert.deepEqual(Array.from(result.late, r=>r.id), ['open', 'late']);
assert.deepEqual(Array.from(result.ontime, r=>r.id), ['ontime']);
assert.equal(result.done.length, 3);
assert.equal(result.unknown.length, 1);
assert.equal(result.open.length, 3);
assert.equal(result.nearest, '2026-11-20T12:00:00');
const historical = context.workloadStats(rows, '2026-09-01', '2026-09-30', '2026-11-16T12:00:00');
assert.equal(historical.late.length, 0);
const moved = [{open:true,deadline:'2026-11-10T18:00:00',late_intervals:[{start:'2026-11-02T18:00:00',end:'2026-11-05T10:00:00'}]}];
assert.equal(context.workloadStats(moved,'2026-11-01','2026-11-06','2026-11-06T12:00:00').late.length,1,'late deadline extension retains period lateness');
assert.equal(context.workloadStats(moved,'2026-11-06','2026-11-30','2026-11-06T12:00:00').late.length,0);
const documents = [
  {id:'1', date:'2026-11-03T09:00:00', posted:1, deadline:'2026-11-05T23:59:59'},
  {id:'2', date:'2026-11-02T09:00:00', posted:0, deadline:'2026-11-10T23:59:59'},
  {id:'3', date:'2026-11-02T09:00:00', posted:1, deadline:'2026-11-05T23:59:59', closed_at:'2026-11-05T23:59:59'},
];
const onec = context.workloadStats(documents, '2026-11-01', '2026-11-30', '2026-11-16T12:00:00', 'onec');
assert.equal(onec.received.length, 3);
assert.equal(onec.done.length, 2);
assert.equal(onec.open.length, 1);
assert.deepEqual(Array.from(onec.late, r=>r.id), ['2']);
assert.deepEqual(Array.from(onec.ontime, r=>r.id), ['3']);
assert.equal(onec.unknown.length, 1, 'posted document date never substitutes for completion time');
assert.match(html, /colspan="6">Битрикс24/);
assert.match(html, /colspan="6">1С/);
const main = {innerHTML:''};
Object.assign(context, {
  state:{wl:{per:{mode:'month',month:'2026-11'},dept:'',uid:''}},
  document:{getElementById:id=>id==='main'?main:{addEventListener(){},querySelectorAll:()=>[]},querySelectorAll:()=>[]},
  isoDay:date=>date.toISOString().slice(0,10),periodRange:()=>['2026-11-01','2026-11-30'],
  periodLabel:()=> 'Ноябрь 2026',periodHtml:()=>'',bindPeriod:()=>{},esc:String,crmLink:label=>label,
  ruMoment:value=>value||'—',ruDay:String,nf:String,
});
vm.runInNewContext(html.slice(html.indexOf('  function renderWorkload('),html.indexOf('  // ---------- «Деньги»')),context);
const data = {generated:'2026-11-16T12:00:00',has_data:true,onec:{loaded:true},tasks:[],documents:[],people:[
  {uid:1,name:'Финансовый менеджер',position:'Финансовый менеджер',dept:'Офис собственника',group:'ФО'},
  {uid:2,name:'Юрист',position:'Юрист',dept:'Офис собственника',group:'ЮО'},
  {uid:3,name:'Анастасия Шашмурина',dept:'ОС',group:'Помощники руководителя'},
  {uid:4,name:'Анастасия Петрова',dept:'Офис собственника',group:'Помощники руководителя'},
  {uid:99,name:'Руководитель вне схемы',group:null},
]};
context.renderWorkload(data);
for (const uid of [1,2,3,4]) assert.ok(main.innerHTML.includes(`data-wl-list="${uid}|card"`),'each employee keeps an individual row');
assert.ok(!main.innerHTML.includes('Руководитель вне схемы'));
assert.ok(!main.innerHTML.includes('<b>ФО</b>'),'category never substitutes for employee rows');
context.state.wl.dept='Помощники руководителя';
context.renderWorkload(data);
assert.ok(main.innerHTML.includes('data-wl-list="3|card"'));
assert.ok(main.innerHTML.includes('data-wl-list="4|card"'));
assert.ok(!main.innerHTML.includes('data-wl-list="1|card"'));
assert.ok(!main.innerHTML.includes('data-wl-list="2|card"'));
context.state.wl.dept='ФО';
context.renderWorkload(data);
assert.ok(main.innerHTML.includes('data-wl-list="1|card"'));
assert.ok(!main.innerHTML.includes('data-wl-list="2|card"'),'finance matches the specific role, not the shared department');
context.state.wl.dept='';
for (const group of ['IT','HR','Маркетолог','ЮО']) {
  const person={uid:20,name:group,group};
  context.renderWorkload({...data,people:[person],documents:[{uid:20,date:'2026-11-02T09:00:00',posted:0}]});
  assert.ok(!main.innerHTML.includes('data-wl-docs='),`${group} has no 1C metrics`);
  assert.equal((main.innerHTML.match(/class="n muted">Нет данных<\/td>/g)||[]).length,6);
  assert.ok(main.innerHTML.includes('data-wl-list="20|open"'),'Bitrix metrics remain available');
}
context.renderWorkload(data);
for (const uid of [3,4]) {
  for (const key of ['open','done']) assert.ok(!main.innerHTML.includes(`data-wl-docs="${uid}|${key}"`));
  for (const key of ['received','late','ontime']) assert.ok(main.innerHTML.includes(`data-wl-docs="${uid}|${key}"`));
}
const documentsForTotals=[
  {uid:1,date:'2026-11-02T09:00:00',posted:1},
  {uid:2,date:'2026-11-02T09:00:00',posted:1},
  {uid:3,date:'2026-11-02T09:00:00',posted:1},
  {uid:4,date:'2026-11-02T09:00:00',posted:0},
];
context.renderWorkload({...data,documents:documentsForTotals});
assert.ok(main.innerHTML.includes('data-wl-docs="all|received">3</button>'),'totals exclude roles that do not work in 1C');
assert.ok(main.innerHTML.includes('data-wl-docs="all|done">1</button>'),'closed total excludes assistants');
assert.ok(main.innerHTML.includes('data-wl-docs="all|open">0</button>'),'working total excludes assistants');
context.state.wl.dept='Помощники руководителя';
context.renderWorkload(data);
assert.ok(!main.innerHTML.includes('data-wl-docs="all|done"'),'unavailable assistant totals are not shown as zero');
context.state.wl.dept='';
const configurable = {...data,people:data.people.map(person=>({...person,included:person.uid!==3})),configuration:{
  directions:['ОП','ОС','ФО','ЮО','Помощники руководителя'],members:[
    {login:'finance',name:'Финансовый менеджер',uid:1,included:true,direction:null,default_group:'ФО',group:'ФО'},
    {login:'helper',name:'Анастасия Шашмурина',uid:3,included:false,direction:'ОП',default_group:'Помощники руководителя',group:'ОП'},
  ]}};
context.renderWorkload(configurable);
assert.ok(main.innerHTML.includes('id="wlMembersForm"'));
assert.ok(main.innerHTML.includes('data-wl-login="helper"'));
assert.ok(main.innerHTML.includes('id="wlMemberIncluded-helper"'));
assert.ok(main.innerHTML.includes('id="wlMemberDirection-helper"'),'drafts stay tied to employee identity after roster reordering');
assert.ok(main.innerHTML.includes('aria-label="Направление поиска Анастасия Шашмурина"'));
assert.ok(main.innerHTML.includes('value="ОП" selected'));
assert.ok(!main.innerHTML.includes('data-wl-list="3|card"'),'excluded employee is available in settings and absent from table');
console.log('Workload six-column groups and independent on-time/overdue calculations: OK');

(async () => {
  let sent, reloads=0;
  const events={},error={textContent:''};
  const makeRow = member => {
    const included={checked:member.included,disabled:false,addEventListener(name,handler){events[member.login+':'+name]=handler;}};
    const direction={value:member.direction||'',disabled:false,dataset:{defaultGroup:member.default_group||''}};
    return {dataset:{wlLogin:member.login},included,direction,
      querySelector:selector=>selector==='[data-wl-included]'?included:direction};
  };
  const formRows=configurable.configuration.members.map(makeRow);
  const controls=formRows.flatMap(row=>[row.included,row.direction]);
  const form={addEventListener:(name,handler)=>{events[name]=handler;},
    querySelectorAll:selector=>selector==='[data-wl-login]'?formRows:controls};
  context.document.getElementById=id=>id==='main'?main:id==='wlMembersForm'?form:id==='wlMembersError'?error:{addEventListener(){}};
  context.api=async (path,body)=>{sent=JSON.parse(JSON.stringify({path,body}));};
  context.loadWorkload=async ()=>{reloads++;};
  context.toast=()=>{};
  context.renderWorkload(configurable);
  formRows[0].included.checked=false;
  events['finance:change']();
  assert.equal(sent,undefined,'checkbox changes stay local until Save');
  await events.submit({preventDefault(){}});
  assert.deepEqual(sent,{path:'/api/workload-members',body:{members:[{login:'finance',included:false,direction:null}]}});
  assert.equal(reloads,1);
  assert.ok(controls.every(control=>!control.disabled));
  context.api=async ()=>{throw Error('Сохранение не удалось');};
  await events.submit({preventDefault(){}});
  assert.equal(error.textContent,'Сохранение не удалось');
  assert.equal(reloads,1,'failed save preserves the draft and current table');
  assert.equal(formRows[0].included.checked,false);
  const unmappedMember={login:'director',name:'Руководитель',uid:99,included:true,direction:'IT',default_group:null,group:'IT'};
  const restoredRow=makeRow(unmappedMember);
  restoredRow.direction.required=true;
  formRows.splice(0,formRows.length,restoredRow);
  context.DashboardUI={keepView:()=>()=>{restoredRow.included.checked=false;restoredRow.direction.value='';}};
  context.renderWorkload({...configurable,people:[{uid:99,name:'Руководитель',group:'IT',included:true}],
    configuration:{directions:['IT'],members:[unmappedMember]}});
  assert.equal(restoredRow.direction.required,false,'restoring an unchecked draft removes the direction requirement');
  console.log('Workload member settings submit only changed rows and preserve failed drafts: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
