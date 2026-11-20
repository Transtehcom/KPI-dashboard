const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const source = html.slice(html.indexOf('  function renderEmp(d) {'), html.indexOf('    const zeroed = d.zero_all.length')) + '}';
const main = {innerHTML:''};
const context = {state:{boot:{me:{role:'employee'}}}, document:{getElementById:id=>id==='main'?main:null},
  esc:value=>String(value??'').replaceAll('<','&lt;'), mLabel:value=>value,
  crmLink:label=>label, fmtVal:value=>value==null?'—':value*100+'%', fmtExtra:x=>x.value,
  detailCell:(details,ri,ci,value)=>value??'—'};
vm.createContext(context);
vm.runInContext(html.slice(html.indexOf('  function kpiRulesButton('), html.indexOf('  function showRules(')), context);
vm.runInContext(source, context);
context.renderEmp({draft:true, department:'Логистика',position_title:'Логист',name:'Сотрудник',month:'2026-10',setup_issues:[],
  items:[{n:6,weight:.15,title:'Исполнительская дисциплина',period:'Месяц',target_text:'100%',description:'Правила',
    components:[{name:'Поручения со статусом и четырьмя датами',fact:.5,unit:'%'}],
    extra:[{label:'Полностью заполнены',value:'1 из 2'}],
    details:{columns:['Поручение','Не заполнено'],rows:[['ЦБ-00000176','ДОП, ДПП, ДОФ, ДФП']]}}]});
for (const text of ['50%','справочно','1 из 2','Проверка поручений · 1 записей','ЦБ-00000176','ДОП, ДПП, ДОФ, ДФП']) {
  assert.ok(main.innerHTML.includes(text), text);
}
assert.ok(main.innerHTML.includes('начисление премии отключено'));
assert.ok(!main.innerHTML.includes('<input'));
assert.match(main.innerHTML, /data-kpi-rules>Методика</);
console.log('Logistics draft shows the source criterion and missing fields in read-only form');

vm.runInContext(html.slice(html.indexOf('  function detailCell('), html.indexOf('  function renderEmp(d) {')), context);
const budgetDetails = {columns:['Заказ','Учёт'], rows:[['ORDER','Да']],
  budget_inclusion:{column:1,rows:[{ref:'customer',value:1}]}};
let inclusion = context.detailCell(budgetDetails,0,1,'Да',true);
assert.ok(inclusion.includes('data-logistics-budget-inclusion'));
assert.ok(inclusion.includes('value="1" selected'));
assert.equal((inclusion.match(/<option/g)||[]).length,2);
budgetDetails.budget_inclusion.rows[0].value=0;
inclusion = context.detailCell(budgetDetails,0,1,'Нет',true);
assert.ok(inclusion.includes('value="0" selected'));
assert.equal(context.detailCell(budgetDetails,0,1,'Нет',false),'Нет');
budgetDetails.budget_inclusion.rows[0].value=1;
assert.equal(context.detailCell(budgetDetails,0,1,'Да',false),'Да');

const handlerSource = html.slice(html.indexOf('    main.querySelectorAll("[data-logistics-budget-inclusion]")'),
  html.indexOf('    const back = document.getElementById("backBtn");',html.indexOf('    main.querySelectorAll("[data-logistics-budget-inclusion]")')));
let change;
const requests=[];
const select={value:'0',disabled:false,dataset:{ref:'customer'},options:[{value:'1',defaultSelected:true},{value:'0',defaultSelected:false}],
  addEventListener:(event,listener)=>{change=listener;}};
Object.assign(context,{main:{querySelectorAll:()=>[select]},d:{login:'logist',month:'2026-11'},
  save:async(path,body)=>requests.push({path,body}),toast:()=>{},refreshKpi:()=>{},PREVIEW:false});
vm.runInContext(handlerSource,context);
(async()=>{
  await change();
  assert.equal(requests[0].path,'/api/logistics-budget-inclusion');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),{emp:'logist',month:'2026-11',ref:'customer',value:0});
  assert.equal(select.disabled,false);
  context.save=async()=>{throw new Error('failed');};
  select.value='0';
  await change();
  assert.equal(select.value,'1','failed save restores previous selection');
  console.log('Logistics budget inclusion renders read-only for employees and saves admin choice');
})().catch(error=>{console.error(error);process.exitCode=1;});
