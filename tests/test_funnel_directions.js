const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const source = html.slice(html.indexOf('  function renderFunnel(d)'), html.indexOf('  // ---------- расписание из календаря'));
const main = {innerHTML:'',querySelectorAll:()=>[]};
const nodes = new Map();
const options = [{value:'epc',name:'EPC'},{value:'bpla',name:'БПЛА'},{value:'kpi',name:'KPI'},
  {value:'investments',name:'Инвестиции'},{value:'factoring',name:'Факторинг'}];
const boxes = ['__all','__tagged',...options.map(item=>item.value)].map(value=>({value,checked:false,events:{},
  addEventListener(name,fn){this.events[name]=fn;}}));
function node(id) {
  if (!nodes.has(id)) nodes.set(id,{events:{},open:false,value:'',addEventListener(name,fn){this.events[name]=fn;},
    querySelectorAll(selector){return id==='fDirections' ? boxes.filter(item=>!selector.endsWith(':checked') || item.checked) : [];}});
  return nodes.get(id);
}
let reloads=0;
const state={boot:{current_month:'2026-11',me:{name:'Тест'}},fKind:'leads',fDirections:['bpla'],fPer:{mode:'all'}};
const context={state,PREVIEW:null,document:{getElementById:id=>id==='main'?main:node(id)},
  FUNNELS:[{kind:'leads',title:'Лиды',gen:'лидов'},{kind:'deals',title:'Сделки',gen:'сделок'}],
  Date,Map,Set,esc:v=>String(v??''),nf:String,money:String,pct:String,pad2:String,isoDay:()=> '2026-11-10',
  ruDay:String,periodHtml:()=>'',funnelPeriod:()=>['',''],bindPeriod:()=>{},loadCalendar:()=>{},calPrintHtml:()=>'',
  load:()=>{reloads++;},funnelShape:()=>'',toneColor:()=>'',logShare:()=>0,window:{print:()=>{}}};
vm.createContext(context);
vm.runInContext(source,context);
const report=()=>({total:0,sections:[{title:'В работе',count:0,amount:0,by_stage:[],by_responsible:[],deals:[]}],other_stages:[],
  filters:{responsibles:[],departments:[],categories:[],directions:options},applied:{category:''}});
const data={leads:report(),deals:report()};
for (const mode of ['date','fact']) {
  state.fMode=mode;
  context.renderFunnel(data);
  assert.ok(main.innerHTML.includes('id="fDirections"'));
  assert.ok(main.innerHTML.includes('Отбор по тегам'));
  assert.ok(!main.innerHTML.includes('БПЛА учитывается по названию или тегу'));
  assert.ok(main.innerHTML.includes('Факторинг'));
}
boxes.find(box=>box.value==='bpla').checked=true;
boxes.find(box=>box.value==='kpi').checked=true;
boxes.find(box=>box.value==='kpi').events.change();
assert.deepEqual(Array.from(state.fDirections),['bpla','kpi']);
assert.equal(reloads,1);
context.renderFunnel(data);
assert.ok(main.innerHTML.includes('Отбор по тегам'));
assert.ok(!main.innerHTML.includes('БПЛА учитывается по названию или тегу'));
boxes[0].events.change();
assert.deepEqual(Array.from(state.fDirections),[]);
context.renderFunnel(data);
assert.ok(main.innerHTML.includes('Все карточки без отбора по направлениям'));
assert.ok(main.innerHTML.includes('<td colspan="3">Все</td>'));
boxes.find(box=>box.value==='__tagged').checked=true;
boxes.find(box=>box.value==='__tagged').events.change();
assert.deepEqual(Array.from(state.fDirections),['tagged']);
context.renderFunnel(data);
assert.ok(main.innerHTML.includes('Карточки хотя бы с одним из пяти тегов направлений'));
assert.ok(main.innerHTML.includes('<td colspan="3">Все с тегом</td>'));
boxes.forEach(box=>{box.checked=['__tagged','epc'].includes(box.value);});
boxes.find(box=>box.value==='epc').events.change();
assert.deepEqual(Array.from(state.fDirections),['epc']);
boxes.forEach(box=>{box.checked=box.value==='__tagged';});
boxes.find(box=>box.value==='__tagged').events.change();
boxes.find(box=>box.value==='__tagged').checked=false;
boxes.find(box=>box.value==='__tagged').events.change();
assert.deepEqual(Array.from(state.fDirections),[]);
console.log('Funnel direction selection and both modes: OK');
