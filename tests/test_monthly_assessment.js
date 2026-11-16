const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const start=html.indexOf('  function manualBlock('),end=html.indexOf('\n  // ручной ввод',start);
const context={state:{boot:{me:{role:'manager'},criteria_titles:{docs:'Документооборот'}}},esc:v=>String(v??''),nf:String};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
const item={manual:{period:'month',month:'2026-11',criteria:['docs'],scores:{docs:{score:75,comment:'Проверено за ноябрь'}}}};
const rendered=context.manualBlock({month:'2026-11'},item);
assert.ok(rendered.includes('data-month-assessment'));
assert.ok(rendered.includes('2026-11'));
assert.ok(!rendered.includes('data-week'));
assert.ok(rendered.includes('Проверено за ноябрь'));
assert.equal((rendered.match(/data-month-score/g)||[]).length,1);
context.state.boot.me.role='employee';
const employee=context.manualBlock({month:'2026-11'},item);
assert.ok(!employee.includes('<input'));assert.ok(!employee.includes('<form'));
assert.ok(employee.includes('75'));
context.state.boot.criteria_titles.cargo_safety='Сохранность груза и рекламации';
const cargo={manual:{period:'month',month:'2026-11',criteria:['cargo_safety'],scores:{cargo_safety:{score:50,comment:'Проверка груза'}}}};
context.state.boot.me.role='manager';
let cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.ok(cargoHtml.includes('data-month-criterion="cargo_safety"'));
assert.ok(cargoHtml.includes('Сохранность груза и рекламации'));
assert.ok(!cargoHtml.includes('Документооборот'));
assert.ok(!cargoHtml.includes('Эффективность задач'));
context.state.boot.me.role='employee';
cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.ok(!cargoHtml.includes('<input'));
assert.ok(!cargoHtml.includes('<form'));
assert.ok(cargoHtml.includes('50%'));
assert.ok(cargoHtml.includes('Проверка груза'));
cargo.manual.cargo_checks={rows:Array.from({length:10},(_,index)=>({
  criterion:index===0?'damage':'criterion-'+index,title:'Критерий '+index,
  cases:index===0?1:null,damage:index===0?1250.75:null,comment:index===0?'Причина установлена':''}))};
cargo.manual.cargo_checks.cargo_value=1000000;
cargo.manual.cargo_checks.auto_score=100;
context.state.boot.me.role='manager';
cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.equal((cargoHtml.match(/data-cargo-row/g)||[]).length,10);
assert.equal((cargoHtml.match(/data-cargo-cases/g)||[]).length,10);
assert.equal((cargoHtml.match(/data-cargo-damage/g)||[]).length,10);
assert.equal((cargoHtml.match(/data-cargo-comment/g)||[]).length,10);
assert.ok(cargoHtml.includes('data-cargo-assessment="1"'));
assert.ok(cargoHtml.includes('Сохранить критерии и оценку'));
assert.ok(cargoHtml.includes('data-cargo-value'));
assert.ok(cargoHtml.includes('Ручная корректировка KPI'));
assert.ok(cargoHtml.includes('Автоматическое выполнение KPI'));
context.state.boot.me.role='employee';
cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.ok(!cargoHtml.includes('<input'));
assert.ok(!cargoHtml.includes('<form'));
assert.ok(cargoHtml.includes('Причина установлена'));
cargo.manual.scores.cargo_safety.score=null;
cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.ok(cargoHtml.includes('100%'));
context.state.boot.me.role='manager';
cargoHtml=context.manualBlock({month:'2026-11'},cargo);
assert.ok(cargoHtml.includes('data-month-score value=""'));
console.log('Monthly supply assessment rendering: OK');

const componentLine=html.split('\n').find(line=>line.includes('<div class="card-b">${i.components.map'));
const countLine=html.split('\n').find(line=>line.includes('const criterionCount ='));
assert.ok(componentLine && countLine);
function contributionMarkup(components,payKnown) {
  const scope={i:{components,aggregation:'equal_mean',pay_known_components:payKnown},inProg:true,
    compRow:()=>'',nf:value=>Number(value).toFixed(2),pct:value=>value==null?'—':value*100+'%'};
  vm.createContext(scope);
  return vm.runInContext(countLine+'\n`'+componentLine.trim()+'`',scope);
}
const twoParts=contributionMarkup([{pct:null},{pct:.2},{pct:1,informative:true}],true);
assert.ok(twoParts.includes('10.00 п. п. из 50.00'));
assert.ok(twoParts.includes('0.00 п. п. из 50.00'));
assert.equal((twoParts.match(/вклад в KPI/g)||[]).length,2);
const threeParts=contributionMarkup([{pct:.2},{pct:0},{pct:1}],false);
assert.ok(threeParts.includes('6.67 п. п. из 33.33'));
assert.ok(html.includes('До загрузки данных по критерию его вклад равен 0.'));
console.log('Two- and three-part contributions match their actual criterion counts');

const handlerStart=html.indexOf('    main.querySelectorAll("form[data-month-assessment]")');
const handlerEnd=html.indexOf('    main.querySelectorAll("select[data-score]")',handlerStart);
let submit;
const input={value:'50'},requests=[];
const form={dataset:{monthCriterion:'cargo_safety'},
  querySelector:selector=>selector==='[data-month-score]'?input:{value:'Проверка груза'},
  addEventListener:(event,handler)=>{submit=handler;}};
Object.assign(context,{main:{querySelectorAll:()=>[form]},d:{login:'logist',month:'2026-11'},
  save:async(path,body)=>requests.push({path,body}),toast:()=>{},refreshKpi:()=>{},PREVIEW:false});
vm.runInContext(html.slice(handlerStart,handlerEnd),context);
(async()=>{
  await submit({preventDefault:()=>{}});
  assert.equal(requests[0].path,'/api/month-score');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),
    {emp:'logist',month:'2026-11',criterion:'cargo_safety',score:50,comment:'Проверка груза'});
  input.value='';
  await submit({preventDefault:()=>{}});
  assert.equal(requests[1].body.score,null);
  form.dataset.monthCriterion='docs';
  input.value='90';
  await submit({preventDefault:()=>{}});
  assert.equal(requests[2].body.criterion,'docs');
  form.dataset.monthCriterion='cargo_safety';
  form.dataset.cargoAssessment='1';
  const oldQuery=form.querySelector;
  form.querySelector=selector=>selector==='[data-cargo-value]'?{value:'1000000'}:oldQuery(selector);
  const cargoValues=[{criterion:'damage',cases:'1',damage:'1250.75',comment:'Причина'},
    {criterion:'loss',cases:'',damage:'0',comment:''}];
  form.querySelectorAll=()=>cargoValues.map(row=>({dataset:{criterion:row.criterion},querySelector:selector=>({
    value:row[{'[data-cargo-cases]':'cases','[data-cargo-damage]':'damage','[data-cargo-comment]':'comment'}[selector]]})}));
  await submit({preventDefault:()=>{}});
  assert.equal(requests[3].path,'/api/logistics-cargo-assessment');
  assert.equal(requests[3].body.cargo_value,1000000);
  assert.deepEqual(JSON.parse(JSON.stringify(requests[3].body.criteria)),[
    {criterion:'damage',cases:1,damage:1250.75,comment:'Причина'},
    {criterion:'loss',cases:null,damage:0,comment:''}]);
  input.value='';
  await submit({preventDefault:()=>{}});
  assert.equal(requests[4].body.score,null);
  assert.equal(requests[4].body.cargo_value,1000000);
  console.log('Monthly form saves the selected criterion and supports clearing the grade');
})().catch(error=>{console.error(error);process.exitCode=1;});

context.dLabel=String;
context.state.boot.me.role='manager';
const weeklyCargo={manual:{criteria:['cargo_safety'],weeks:{cargo_safety:[
  {week:'2026-11-02',score:50,comment:'Оценка недели'},
  {week:'2026-11-09',score:null,comment:null}]}}};
const weeklyHtml=context.manualBlock({weeks:['2026-11-02','2026-11-09']},weeklyCargo);
assert.equal((weeklyHtml.match(/data-score/g)||[]).length,2);
assert.ok(weeklyHtml.includes('data-crit="cargo_safety"'));
assert.ok(weeklyHtml.includes('Оценка недели'));
assert.ok(weeklyHtml.includes('Пустые недели исключаются'));
assert.ok(!weeklyHtml.includes('data-cargo-row'));
context.state.boot.me.role='employee';
const readonlyWeekly=context.manualBlock({weeks:['2026-11-02','2026-11-09']},weeklyCargo);
assert.ok(!readonlyWeekly.includes('<select'));
assert.ok(!readonlyWeekly.includes('<input'));
assert.ok(readonlyWeekly.includes('Оценка недели'));
console.log('Weekly logistics assessment rendering and read-only access: OK');
