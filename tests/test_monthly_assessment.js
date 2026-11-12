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
