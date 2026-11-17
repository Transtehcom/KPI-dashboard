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
const clarifiedDocuments = [
  {id:'old-open',date:'2026-10-01T09:00:00',posted:0,deadline:'2026-10-20T18:00:00'},
  {id:'finished-early',date:'2026-11-02T09:00:00',posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-05T17:59:59'},
  {id:'finished-late',date:'2026-11-03T09:00:00',posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-06T09:00:00'},
  {id:'finished-exactly',date:'2026-11-03T09:00:00',posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-05T18:00:00'},
  {id:'nearest',date:'2026-10-02T09:00:00',posted:0,deadline:'2026-11-16T12:01:00'},
  {id:'later',date:'2026-11-01T09:00:00',posted:0,deadline:'2026-11-20T12:00:00'},
  {id:'old-closed',date:'2026-10-01T09:00:00',posted:1,deadline:'2026-10-05T18:00:00',closed_at:'2026-10-04T09:00:00'},
  {id:'unposted-stale-close',date:'2026-10-01T09:00:00',posted:0,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-04T09:00:00'},
];
const clarified = context.workloadStats(clarifiedDocuments,'2026-11-01','2026-11-30','2026-11-16T12:00:00','onec');
assert.equal(clarified.received.length,clarified.open.length + clarified.done.length,'received is working plus closed');
assert.ok(clarified.received.some(row=>row.id==='old-open'),'older open documents contribute to received');
assert.ok(!clarified.received.some(row=>row.id==='old-closed'),'closed documents outside the selected period stay outside received');
assert.deepEqual(Array.from(clarified.late,row=>row.id),['old-open','finished-late','unposted-stale-close']);
assert.deepEqual(Array.from(clarified.ontime,row=>row.id),['finished-early','finished-exactly']);
assert.equal(clarified.nearest,'2026-11-16T12:01:00','nearest is the earliest upcoming deadline among open documents');
assert.equal(context.workloadStats(clarifiedDocuments,'2026-11-01','2026-11-30','2026-12-01T12:00:00','onec').nearest,null);
assert.equal(context.workloadDelaySeconds({posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-05T18:00:00'},'2026-11-16T12:00:00','onec'),0);
assert.equal(context.workloadDelaySeconds({posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-05T18:01:00'},'2026-11-16T12:00:00','onec'),60);
assert.equal(context.workloadDelaySeconds({posted:0,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-04T10:00:00'},'2026-11-06T19:02:00','onec'),90120);
assert.equal(context.workloadDelaySeconds({posted:1,deadline:'2026-11-05T18:00:00'},'2026-11-16T12:00:00','onec'),null);
assert.equal(context.workloadDelaySeconds({posted:0},'2026-11-16T12:00:00','onec'),null);
assert.equal(context.workloadDelaySeconds({open:false,deadline:'2026-11-05T18:00:00',exec_at:'2026-11-05T18:01:00'},'2026-11-16T12:00:00'),60);
assert.equal(context.workloadDelaySeconds({open:true,deadline:'2026-11-05T18:00:00'},'2026-11-05T18:01:00'),60);
assert.equal(context.workloadDelaySeconds({open:true,deadline:'2026-11-10T18:00:00',late_intervals:[{start:'2026-11-02T18:00:00',end:'2026-11-02T18:30:00'}]},'2026-11-06T12:00:00'),1800);
assert.equal(context.workloadDelaySeconds({posted:1,deadline:'2026-11-05T18:00:00',closed_at:'2026-11-05T13:01:00Z'},'2026-11-16T12:00:00','onec'),60);
const tenderDocuments=[
  {posted:true,is_closed:false,status:'Действует',date:'2026-11-01T09:00:00',deadline:'2026-11-20T12:00:00'},
  {posted:false,is_closed:true,status:'Выиграно',date:'2026-11-01T09:00:00',deadline:'2026-11-10T12:00:00',closed_at:'2026-11-10T12:01:00'}];
const tenderStats=context.workloadStats(tenderDocuments,'2026-11-01','2026-11-30','2026-11-17T12:00:00','onec');
assert.equal(tenderStats.open.length,1);
assert.equal(tenderStats.done.length,1);
assert.equal(tenderStats.late.length,1);
assert.equal(tenderStats.nearest,'2026-11-20T12:00:00');
assert.equal(context.workloadDelaySeconds(tenderDocuments[1],'2026-11-17T12:00:00','onec'),60);
const financialDocument={entity:'Document_ПриобретениеТоваровУслуг',posted:false,is_closed:true,timing_applicable:false,
  date:'2026-11-16T14:58:11',closed_at:'2026-11-16T14:58:11',deadline:'2026-11-01T12:00:00'};
const financialStats=context.workloadStats([financialDocument],'2026-11-01','2026-11-30','2026-11-17T12:00:00','onec');
assert.equal(financialStats.received.length,1);
assert.equal(financialStats.done.length,1);
assert.equal(financialStats.open.length,0);
assert.equal(financialStats.late.length,0);
assert.equal(financialStats.ontime.length,0);
assert.equal(financialStats.unknown.length,0);
assert.equal(financialStats.nearest,null);
assert.match(html, /colspan="6">Битрикс24/);
assert.match(html, /colspan="6">1С/);
vm.runInNewContext(html.slice(html.indexOf('  function operationsMoment('),html.indexOf('  function operationsMoney(')),context);
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
const drilldownDocuments = clarifiedDocuments.map(row=>({...row,uid:1,number:row.id,entity:'Document_ЗаказКлиента',owner_field:'Менеджер_Key'}));
context.state.wl.list={uid:'1',key:'received',source:'onec'};
context.renderWorkload({...data,documents:drilldownDocuments});
assert.ok(main.innerHTML.includes(`data-wl-docs="1|received">${clarified.received.length}</button>`));
const receivedList=main.innerHTML.slice(main.innerHTML.indexOf('id="wlList"'));
assert.ok(receivedList.includes('old-open'),'received drilldown includes the older working document');
assert.ok(!receivedList.includes('old-closed'),'received drilldown matches the count');
assert.ok(receivedList.includes('<th>Просрочка</th>'),'document drilldown displays overdue duration');
assert.ok(receivedList.includes('0 д 15 ч 0 мин 0 с'),'completed document overdue duration stops at completion');
context.state.wl.list={uid:'1',key:'received',source:'onec'};
context.renderWorkload({...data,documents:tenderDocuments.map((row,index)=>({...row,uid:1,number:String(index),entity:'Document_ТТК_Тендер',owner_field:'ВедущийМенеджер_Key'}))});
assert.ok(main.innerHTML.includes('В работе · Действует'));
assert.ok(main.innerHTML.includes('Закрыто · Выиграно'));
const supplyTimingRows=[
  {number:'Т-000637',deadline:'2026-07-15T12:00:00',closed_at:'2026-07-15T11:11:50'},
  {number:'exact-supply',deadline:'2026-07-15T12:00:00',closed_at:'2026-07-15T12:00:00'},
  {number:'late-supply',deadline:'2026-07-15T12:00:00',closed_at:'2026-07-15T12:01:00'}
].map(row=>({...row,uid:1,entity:'Document_ТТК_ЗаявкаВОтделСнабжения',owner_field:'ОтветственныйСнабжение_Key',date:'2026-11-01T09:00:00',posted:true,is_closed:true}));
context.state.wl.list={uid:'1',key:'received',source:'onec'};
context.renderWorkload({...data,documents:supplyTimingRows});
assert.ok(main.innerHTML.includes('class="nw wl-ok"'), 'early supply completion is highlighted green');
assert.ok(main.innerHTML.includes('+0 д 0 ч 48 мин 10 с'), 'supply request 637 shows its positive time balance');
assert.ok(main.innerHTML.includes('−0 д 0 ч 1 мин 0 с'), 'late supply completion shows a negative time balance');
const exactSupplyRow=main.innerHTML.split('exact-supply')[1].split('</tr>')[0];
assert.ok(exactSupplyRow.includes('<td class="nw">—</td>'), 'exactly on time has a dash');
const transport2416={uid:1,number:'000002416',entity:'Document_ТТК_ЗаявкаНаРасчетТранспортныхРасходов',
  owner_field:'ОтветственныйЛогист_Key',date:'2026-11-16T17:12:08',posted:true,is_closed:true,status:'Отработана',
  deadline:'2026-11-26T16:00:00',closed_at:'2026-11-16T15:44:43',deadline_source:'СрокДействия документа-основания Т-000910'};
context.state.wl.list={uid:'1',key:'received',source:'onec'};
const previousMoment=context.ruMoment;
context.ruMoment=value=>value ? value.slice(0,16) : '—';
context.renderWorkload({...data,documents:[transport2416]});
assert.ok(main.innerHTML.includes('Закрыто · Отработана'));
assert.ok(main.innerHTML.includes('2026-11-26T16:00:00'));
assert.ok(main.innerHTML.includes('2026-11-16T15:44:43'));
assert.ok(main.innerHTML.includes('СрокДействия документа-основания Т-000910'));
const paymentRequests=[
  {number:'unpaid-request',posted:true,is_closed:false,status:'КОплате',deadline:'2026-11-20T12:00:00'},
  {number:'paid-request',posted:false,is_closed:true,status:'Согласована',deadline:'2026-11-16T16:00:00',closed_at:'2026-11-16T16:00:01'},
  {number:'rejected-request',posted:false,is_closed:true,status:'Отклонена',deadline:'2026-11-16T16:00:00'},
].map(row=>({...row,uid:1,entity:'Document_ЗаявкаНаРасходованиеДенежныхСредств',owner_field:'Автор_Key',date:'2026-11-01T09:00:00',timing_applicable:true}));
const paymentStats=context.workloadStats(paymentRequests,'2026-11-01','2026-11-30','2026-11-17T12:00:00','onec');
assert.equal(paymentStats.done.length,2);
assert.equal(paymentStats.open.length,1);
assert.equal(paymentStats.late.length,1);
assert.equal(paymentStats.unknown.length,1);
assert.equal(paymentStats.ontime.length,0);
context.renderWorkload({...data,documents:paymentRequests});
assert.ok(main.innerHTML.includes('В работе · К оплате'));
assert.ok(main.innerHTML.includes('Закрыто · Отклонена'));
assert.ok(main.innerHTML.includes('2026-11-16T16:00:01'),'linked payment completion preserves seconds');
assert.ok(main.innerHTML.includes('0 д 0 ч 0 мин 1 с'),'payment request lateness uses the payment date');
const quoteDocuments=[
  {number:'active-quote',posted:false,is_closed:true,status:'Действует',deadline:'2026-11-16T16:00:00',closed_at:'2026-11-16T16:00:01'},
  {number:'cancelled-quote',posted:false,is_closed:true,status:'Отменено',deadline:null,closed_at:null},
  {number:'draft-quote',posted:true,is_closed:false,status:'Черновик',deadline:'2026-11-20T12:00:00'},
  {number:'unapproved-quote',posted:true,is_closed:false,status:'НеСогласовано',deadline:null},
].map(row=>({...row,uid:1,entity:'Document_КоммерческоеПредложениеКлиенту',owner_field:'Менеджер_Key',date:'2026-11-01T09:00:00',timing_applicable:true}));
context.renderWorkload({...data,documents:quoteDocuments});
assert.ok(main.innerHTML.includes('Закрыто · Действует'));
assert.ok(main.innerHTML.includes('Закрыто · Отменено'));
assert.ok(main.innerHTML.includes('В работе · Не согласовано'));
assert.ok(main.innerHTML.includes('2026-11-16T16:00:01'),'quote transition preserves seconds');
const cancelledQuote=main.innerHTML.split('cancelled-quote')[1].split('</tr>')[0];
assert.ok(cancelledQuote.includes('Нет данных'),'missing quote history remains unknown');
const supplierOrders=[
  {number:'00ЦБ-000710',posted:true,is_closed:true,payment_pct:100,receipt_pct:100,deadline:'2026-10-21T23:59:59',closed_at:'2026-10-22T15:56:41',closed_at_source:'Приобретение 00ЦБ-000728'},
  {number:'00ЦБ-000707',posted:true,is_closed:false,payment_pct:100,receipt_pct:0,deadline:'2026-11-02T23:59:59'},
  {number:'almost-paid',posted:true,is_closed:false,payment_pct:99.9999,receipt_pct:100,deadline:'2026-11-02T23:59:59'},
].map(row=>({...row,uid:1,entity:'Document_ЗаказПоставщику',owner_field:'Автор_Key',date:'2026-11-01T09:00:00',timing_applicable:true}));
context.renderWorkload({...data,documents:supplierOrders});
assert.ok(main.innerHTML.includes('Оплата 100%'));
assert.ok(main.innerHTML.includes('Поступление 0%'));
assert.ok(main.innerHTML.includes('2026-10-22T15:56:41'));
assert.ok(main.innerHTML.includes('title="Приобретение 00ЦБ-000728"'));
assert.ok(main.innerHTML.includes('Оплата <100%'),'rounding never displays partial payment as fully paid');
context.ruMoment=previousMoment;
context.state.wl.list={uid:'1',key:'received',source:'onec'};
context.renderWorkload({...data,documents:[{...financialDocument,uid:1,number:'receipt-operation',owner_field:'Автор_Key'}]});
const receiptDetails=main.innerHTML.slice(main.innerHTML.indexOf('id="wlList"'));
assert.ok(receiptDetails.includes('Не применяется'));
assert.ok(!receiptDetails.includes('wl-bad'));
assert.ok(!receiptDetails.includes('wl-ok'));
assert.ok(!main.innerHTML.includes('без даты или срока:'), 'financial operations do not create missing timing warnings');
assert.equal(context.workloadDelaySeconds(financialDocument,'2026-11-17T12:00:00','onec'),null);
const lateTask={id:'late-task',uid:1,title:'Задача с опозданием',url:'#',open:false,created:'2026-11-01T09:00:00',
  closed:'2026-11-05T18:01:00',exec_at:'2026-11-05T18:01:00',deadline:'2026-11-05T18:00:00',
  tags:[],remarks:[],moves_ok:[],moves_late:[],self_moves:[],returns:[],mode:'direct',creator:50};
context.state.wl.list={uid:'1',key:'done'};
context.renderWorkload({...data,tasks:[lateTask]});
assert.ok(main.innerHTML.includes('0 д 0 ч 1 мин 0 с'),'completed Bitrix task shows minute-level overdue duration');
context.state.wl.list=null;
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
