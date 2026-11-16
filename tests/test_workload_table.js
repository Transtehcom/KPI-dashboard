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
  document:{getElementById:id=>id==='main'?main:{addEventListener(){}},querySelectorAll:()=>[]},
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
console.log('Workload six-column groups and independent on-time/overdue calculations: OK');
