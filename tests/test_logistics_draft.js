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
console.log('Logistics draft shows the source criterion and missing fields in read-only form');
