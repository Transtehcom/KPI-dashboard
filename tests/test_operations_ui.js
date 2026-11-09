const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  // ---------- operational panels');
const end = html.indexOf('  // даты поручения экспедитору', start);
assert.ok(start > 0, 'operations panels must have their own feature-gated implementation');
const nodes = new Map();
const main = {innerHTML:'', querySelectorAll:()=>[], querySelector:()=>null};
const posts = [], notices = [];
let reloads = 0;
const state = {view:'m-supply',boot:{me:{role:'manager'},operations_enabled:true},ops:{supply:{per:{mode:'day',day:'2026-11-09'},emp:'Все',channel:'Все'}}};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const copied=[];
const context = {state,document:{getElementById:id=>id==='main'?main:nodes.get(id)||null},navigator:{clipboard:{writeText:async value=>{copied.push(value);}}},URL,URLSearchParams,
  esc,nf:String,ruMoment:v=>v?v.slice(0,16).replace('T',' '):'—',ruDay:v=>v,periodHtml:()=>'',periodLabel:(a,b)=>`${a} – ${b}`,
  isoDay:()=> '2026-11-09',periodRange:()=>['2026-11-09','2026-11-09'],bindPeriod:()=>{},
  pgInfo:(pager,total)=>({from:(pager.page||0)*25,to:Math.min(total,((pager.page||0)+1)*25)}),pgHtml:()=>'<nav>Страницы</nav>',pgBind:()=>{},
  loadMetrics:()=>{reloads++;},api:async(path,body)=>{posts.push({path,body});return {ok:true};},
  toast:message=>notices.push(message)};
vm.createContext(context);
vm.runInContext(html.slice(start,end), context);
const item = {id:'row-1',name:'<img src=x>',quantity:2,unit:'шт',included:true,filled:true,
  selected_offer:{supplier_id:'s1',supplier:'<script>supplier</script>',price:150,quantity:2},
  offers:[{supplier_id:'s1',supplier:'<script>supplier</script>',price:150,quantity:2}],
  history:{median:100,min:80,max:120,supplier_count:2,amber:true,offers:[]}};
const record = {id:'onec:supply:r1',title:'<svg onload=x>',owner:'supply',owner_name:'<b>name</b>',due:'2026-11-09T18:00:00',
  active_obligation_id:'onec:supply:child',
  due_change_pending:true,due_raw:'2026-11-10T18:00:00',
  late_seconds:0,status:'В работе',url:'javascript:alert(1)',documents:[{id:'doc',title:'Документ 1С',url:'https://example.test/doc',amount:99,vat_included:true}],
  items:[item],selected_total:300,estimate_total:200,unestimated_count:1};
const report = {kind:'supply',date_from:'2026-11-09',date_to:'2026-11-09',cutoff:'2026-11-09T12:34:56',history_start:'2026-11-09T09:00:00',
  coverage:{sources:{onec:{updated_at:'2026-11-09T09:00:00',stale:true},bitrix:{updated_at:'2026-11-09T12:00:00',stale:false}}},
  employees:[{login:'supply',name:'<b>name</b>'}],records:[record],queue:[record],events:[],
  people:[{login:'supply',name:'<b>name</b>',due_today:1,closed_late:1}],
  results:{closed_without_plan:2,closed_without_fact:3},warnings:['<script>warning</script>'],
  tiles:[{key:'open',title:'В работе',value:1,record_ids:[record.id]}]};
context.renderOperations(report,()=>true);
let result = main.innerHTML;
assert.ok(result.includes('&lt;svg onload=x&gt;'));
assert.ok(result.includes('&lt;img src=x&gt;'));
assert.ok(!result.includes('href="javascript:'));
assert.ok(result.includes('История с'));
assert.ok(result.includes('90 минут'));
assert.ok(result.includes('12:34:56'), 'cutoff must retain seconds');
assert.ok(result.includes('Выбранные предложения'));
assert.ok(result.includes('Предварительная оценка'));
const sourceAmounts={...record,best_price_total:38424181.61,delivery_options:[
  {id:'delivery-1',selected:true,carrier:'<b>Carrier</b>',from:'<from>',to:'<to>',amount:12345.67,vat_included:false},
  {id:'delivery-2',selected:false,carrier:'Alternative',amount:24000,vat_included:true,currency:'RUB'}]};
for (const render of [context.operationsRecord,context.operationsDocument]) {
  const sourceResult=render(sourceAmounts);
  assert.ok(sourceResult.includes('Сумма лучших цен из 1С'));
  assert.ok(sourceResult.includes('38424181.61'));
  assert.ok(sourceResult.includes('база НДС не подтверждена'));
  assert.ok(sourceResult.includes('валюта не указана'));
  assert.ok(sourceResult.includes('Стоимость выбранной доставки'));
  assert.ok(sourceResult.includes('12345.67'));
  assert.ok(sourceResult.includes('Alternative'));
  assert.ok(sourceResult.includes('&lt;b&gt;Carrier&lt;/b&gt;'));
  assert.ok(sourceResult.includes('&lt;from&gt;'));
}
context.nf=(value,decimals=0)=>Number(value).toFixed(decimals);
assert.ok(context.operationsSourceDetails(sourceAmounts).includes('38424181.61'),'source totals retain kopecks');
assert.ok(context.operationsSourceDetails(sourceAmounts).includes('12345.67'),'selected delivery retains kopecks');
context.nf=String;
const sourceItemHtml=context.operationsItems({...record,items:[{...item,best_price:10.12,best_amount:20.24,selected_amount:23.45,note:'<note>'}]});
assert.ok(sourceItemHtml.includes('Сумма по лучшей цене из 1С'));
assert.ok(sourceItemHtml.includes('20.24'));
assert.ok(sourceItemHtml.includes('&lt;note&gt;'));
assert.ok(result.includes('data-op-scope'));
assert.ok(result.includes('data-op-approve'));
assert.ok(result.includes('data-op-deadline'), 'unapproved deadline moves remain visible for review');
assert.ok(result.includes('data-op-deadline data-request="onec:supply:child"'),'deadline approvals target the active obligation');
assert.ok(result.includes('#operations-document=onec%3Asupply%3Ar1'),'the parent document keeps its own source drawer');
assert.ok(result.includes('op-amber'), 'price above two-supplier maximum is amber');
assert.ok(result.includes('Срок сегодня'));
assert.ok(result.includes('Из закрытых позже срока'));
assert.ok(result.includes('Закрыты без планового срока'));
assert.ok(result.includes('Закрыты без фактической даты'));
context.renderOperations({...report,kind:'sales'},()=>true);
assert.ok(!main.innerHTML.includes('data-op-metric="due_today"'),'supply deadline columns stay within the supply report');
assert.ok(!main.innerHTML.includes('data-op-metric="closed_late"'));
context.renderOperations({...report,tiles:[...report.tiles,{key:'closed_without_plan',title:'Закрыты без планового срока',value:2,record_ids:[]}]},()=>true);
assert.ok(main.innerHTML.includes('data-op-tile="closed_without_plan"'),'review counts use exact source tiles when available');
context.renderOperations(report,()=>true);
assert.ok(context.operationsDrill(report,report.tiles[0]).includes('&lt;svg onload=x&gt;'));
const mixedReport={...report,events:[{id:'event-1',entity_id:record.id,at:'2026-11-09T10:00:00',description:'Проверено предложение'}]};
const mixedDrill=context.operationsDrill(mixedReport,{...report.tiles[0],event_ids:['event-1']});
assert.ok(mixedDrill.includes('Документы источников'),'a combined metric keeps document drilldowns');
assert.ok(mixedDrill.includes('Проверено предложение'));
const largeReport={...report,queue:Array.from({length:250},(_,index)=>({...record,id:'large-'+index,title:'Request '+index,items:[]})),records:[],tiles:[]};
context.renderOperations(largeReport,()=>true);
assert.ok(!main.innerHTML.includes('Request 249'),'large queues must be paged');
context.renderOperations(report,()=>true);
assert.equal(context.operationsLink('Bad','data:text/html,bad'),'Bad');
const nativeSource=context.operationsLink('1С','e1cib/data/Document.Request?ref=abc');
assert.ok(nativeSource.includes('Скопировать ссылку 1С'));
assert.ok(nativeSource.includes('data-op-copy-source="e1cib/data/Document.Request?ref=abc"'));
assert.ok(!nativeSource.includes('href="e1cib/'),'native references must not resolve as dashboard-relative links');
assert.equal(context.operationsTileValue({key:'on_time_rate',value:0.75}),'75%');
assert.equal(context.operationsTileValue({key:'on_time_rate',value:null}),'—');
assert.ok(context.operationsOffer({price:12,price_date:'2026-11-09T10:00:00',date_verified:false,exclusion_reasons:['quantity_mismatch']},record,item).includes('по дате наблюдения'));
state.boot.me.role='employee';
assert.ok(!context.operationsItems(record).includes('data-op-scope'));
assert.ok(!context.operationsItems(record).includes('data-op-approve'));
assert.ok(!context.operationsRecord(record).includes('data-op-deadline'));
state.boot.me.role='manager';
const form = {dataset:{request:record.id,item:item.id},elements:{included:{checked:false},reason:{value:''}},
  querySelector:()=>({disabled:false}),isConnected:true};
(async()=>{
  const copyButton={dataset:{opCopySource:'e1cib/data/Document.Request?ref=abc'}};
  context.bindOperationsSources({querySelectorAll:selector=>selector==='[data-op-copy-source]'?[copyButton]:[]},report,()=>true);
  let copyStopped=false;
  await copyButton.onclick({preventDefault:()=>{},stopPropagation:()=>{copyStopped=true;}});
  assert.equal(copied[0],copyButton.dataset.opCopySource);
  assert.ok(copyStopped,'copying a native source reference keeps the current source card open');
  let selected=false,removed=false,copyField;
  context.navigator.clipboard.writeText=async()=>{throw new Error('Clipboard unavailable');};
  context.document.createElement=()=>({style:{},setAttribute:()=>{},focus:()=>{},select:()=>{selected=true;},remove:()=>{removed=true;}});
  context.document.execCommand=()=>true;
  copyButton.after=field=>{copyField=field;};
  await copyButton.onclick({preventDefault:()=>{},stopPropagation:()=>{}});
  assert.equal(copyField.value,copyButton.dataset.opCopySource);
  assert.ok(selected && removed,'the native reference supports textarea copy fallback');
  await context.operationsSaveScope(form,()=>true);
  assert.equal(posts.length,0,'exclusion requires a reason');
  form.elements.reason.value='Позиция отменена клиентом';
  await context.operationsSaveScope(form,()=>true);
  assert.equal(posts[0].path,'/api/operations/item-scope');
  assert.equal(posts[0].body.included,false);
  assert.equal(posts[0].body.reason,'Позиция отменена клиентом');
  const button={dataset:{request:record.id,item:item.id,supplier:'s1'},disabled:false,isConnected:true};
  await context.operationsApprove(button,()=>true);
  assert.equal(posts[1].path,'/api/operations/price-approval');
  assert.equal(posts[1].body.supplier_id,'s1');
  const deadlineForm={...form,dataset:{request:record.active_obligation_id,due:record.due_raw},elements:{reason:{value:'Согласовано с отделом продаж'}}};
  await context.operationsSaveDeadline(deadlineForm,()=>true);
  assert.equal(posts[2].path,'/api/operations/deadline-approval');
  assert.equal(posts[2].body.due,record.due_raw);
  assert.equal(posts[2].body.request_id,record.active_obligation_id);
  const before=reloads;
  await context.operationsApprove(button,()=>false);
  assert.equal(reloads,before,'late saves cannot replace another view');
  const handlers = new Map();
  const tileButton={dataset:{opTile:'open'},addEventListener:(type,handler)=>handlers.set('tile:'+type,handler),setAttribute:()=>{}};
  const scopeForm={...form,addEventListener:(type,handler)=>handlers.set('scope:'+type,handler)};
  const approveButton={...button,addEventListener:(type,handler)=>handlers.set('approve:'+type,handler)};
  const deadlineNode={...deadlineForm,addEventListener:(type,handler)=>handlers.set('deadline:'+type,handler)};
  const dueTodayButton={dataset:{opPerson:'0',opMetric:'due_today'},addEventListener:(type,handler)=>handlers.set('due_today:'+type,handler)};
  const closedLateButton={dataset:{opPerson:'0',opMetric:'closed_late'},addEventListener:(type,handler)=>handlers.set('closed_late:'+type,handler)};
  const drill={innerHTML:'',querySelectorAll:()=>[]};
  nodes.set('opDrill',drill);
  main.querySelectorAll=selector=>({'[data-op-tile]':[tileButton],'[data-op-scope]':[scopeForm],
    '[data-op-approve]':[approveButton],'[data-op-deadline]':[deadlineNode],'[data-op-person]':[dueTodayButton,closedLateButton]}[selector]||[]);
  const dueTomorrow={...record,id:'tomorrow',title:'Due tomorrow',due:'2026-11-10T12:00:00'};
  const otherEmployee={...record,id:'other-employee',title:'Other employee due',owner:'other'};
  const lateInPeriod={...record,id:'late-in-period',title:'Late within period',timeliness:'late',closed_at:'2026-11-09T10:00:00'};
  const lateOutsidePeriod={...lateInPeriod,id:'late-outside-period',title:'Late outside period',closed_at:'2026-11-08T10:00:00'};
  const personReport={...report,records:[record,dueTomorrow,otherEmployee,lateInPeriod,lateOutsidePeriod],queue:[record,dueTomorrow,otherEmployee]};
  context.bindOperations(personReport,()=>true);
  const pageBefore=main.innerHTML;
  handlers.get('tile:click')();
  assert.ok(drill.innerHTML.includes('&lt;svg onload=x&gt;'));
  assert.equal(main.innerHTML,pageBefore,'stat drilldowns preserve drafts elsewhere on the page');
  handlers.get('due_today:click')();
  assert.ok(drill.innerHTML.includes('&lt;svg onload=x&gt;'));
  assert.ok(!drill.innerHTML.includes('Due tomorrow'),'due today uses the selected final day');
  assert.ok(!drill.innerHTML.includes('Other employee due'),'due today retains employee attribution');
  handlers.get('closed_late:click')();
  assert.ok(drill.innerHTML.includes('Late within period'));
  assert.ok(!drill.innerHTML.includes('Late outside period'),'closed late excludes closures outside the chosen period');
  assert.ok(!drill.innerHTML.includes('&lt;svg onload=x&gt;'),'closed late excludes open queue records');
  let prevented=false;
  handlers.get('scope:submit')({preventDefault:()=>{prevented=true;}});
  handlers.get('approve:click')();
  handlers.get('deadline:submit')({preventDefault:()=>{}});
  await new Promise(setImmediate);
  assert.ok(prevented,'scope submit prevents page navigation');
  assert.equal(posts.at(-3).path,'/api/operations/item-scope');
  assert.equal(posts.at(-2).path,'/api/operations/price-approval');
  assert.equal(posts.at(-1).path,'/api/operations/deadline-approval');
  let inserted;
  const enclosingDetails={open:false};
  const sourceLink={getAttribute:()=> '#operations-document=onec%3Asupply%3Ar1',isConnected:true,
    closest:selector=>selector==='details'?enclosingDetails:{after:body=>{inserted=body;}}};
  context.document.createElement=()=>({innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null});
  context.api=async(path,body)=>{posts.push({path,body});return {document:record,history:[],events:[]};};
  context.bindOperationsSources({querySelectorAll:selector=>selector.startsWith('a[')?[sourceLink]:[]},report,()=>true);
  let stopped=false;
  await sourceLink.onclick({preventDefault:()=>{},stopPropagation:()=>{stopped=true;}});
  assert.ok(stopped,'source detail opens inline without navigating away');
  assert.equal(enclosingDetails.open,true,'source links expand the enclosing request so fetched content is visible');
  assert.ok(posts.at(-1).path.includes('date_to=2026-11-09'),'document drilldown retains the historical date');
  assert.ok(inserted.innerHTML.includes('&lt;svg onload=x&gt;'));
  assert.ok(!notices.some(message=>message.includes('not a function')),'inline source cards bind without rendering errors');
  let active=true, resolveReport, painted=0;
  context.api=()=>new Promise(resolve=>{resolveReport=resolve;});
  context.renderOperations=()=>{painted++;};
  const pending=context.loadOperations('supply',()=>active);
  active=false;
  resolveReport(report);
  await pending;
  assert.equal(painted,0,'a stale report cannot replace a different screen');
  const metrics=html.slice(html.indexOf('  async function loadMetrics()'),html.indexOf('  const ruMoment'));
  assert.ok(metrics.includes('state.boot.operations_enabled'),'old servers retain existing screens');
  const routes=[];
  const routedState={view:'m-supply',boot:{operations_enabled:false,current_month:'2026-11'}};
  const routedContext={state:routedState,METRICS:{'m-supply':{kind:'supply'},'m-sales':{kind:'sales'}},
    viewRequest:()=>({main,current:()=>true}),periodRange:()=>['2026-11-01','2026-11-30'],
    api:async url=>{routes.push(url);return {};},renderSupply:()=>{},
    loadOperations:async kind=>{routes.push('operations:'+kind);},loadSales:async()=>{routes.push('legacy:sales');},loadError:()=>''};
  vm.createContext(routedContext);
  vm.runInContext(metrics,routedContext);
  await routedContext.loadMetrics();
  assert.ok(routes.pop().startsWith('/api/metrics?kind=supply'));
  routedState.boot.operations_enabled=true;
  await routedContext.loadMetrics();
  assert.equal(routes.pop(),'operations:supply');
  routedState.view='m-sales';
  await routedContext.loadMetrics();
  assert.equal(routes.pop(),'operations:sales');
  routedState.sp.tab='reg';
  await routedContext.loadMetrics();
  assert.equal(routes.pop(),'legacy:sales','existing registry remains accessible through the original loader');
  new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
  console.log('Operations source drilldown, escaped labels, scope and price actions: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
