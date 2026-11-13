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
  console.log('Monthly form saves the selected criterion and supports clearing the grade');
})().catch(error=>{console.error(error);process.exitCode=1;});
