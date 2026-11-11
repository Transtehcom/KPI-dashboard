const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const source = html.slice(html.indexOf('  function renderStaff('), html.indexOf('  function staffForm('));
const main = {innerHTML:'', querySelectorAll:()=>[]};
const state = {boot:{me:{role:'employee'}}};
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const link = html.slice(html.indexOf('  function crmLink('), html.indexOf('  function approvalControl('));
const context = {state, esc, document:{getElementById:id=>id==='main'?main:null},
  staffForm:()=>{throw Error('Employee must not receive edit form');},
  awayNow:()=>'', bindStaffForm:()=>{}, window:{}};
vm.createContext(context);
vm.runInContext(link + source, context);
const publicData = {employees:[{name:'Сотрудник <тест>', department:'Отдел', title:'Специалист',
  url:'https://crm.example.test/company/personal/user/56/'}]};
context.renderStaff(publicData, '');
assert.equal((main.innerHTML.match(/<th>/g)||[]).length, 3);
assert.ok(main.innerHTML.includes('Сотрудник &lt;тест>'));
assert.ok(main.innerHTML.includes('href="https://crm.example.test/company/personal/user/56/"'));
assert.ok(main.innerHTML.includes('rel="noopener noreferrer"'));
for (const forbidden of ['data-edit=', 'addEmp', '<form', '<th>KPI', '<th>Б24 ID', '<th>Видимость', '<th>Администратор', '<th>Логин', '<th>Пароль']) {
  assert.ok(!main.innerHTML.includes(forbidden), forbidden);
}
state.boot.me.role='manager';
context.renderStaff({...publicData, positions:[], departments:[], employees:[{...publicData.employees[0],login:'test',b24_user_id:56}]});
assert.equal((main.innerHTML.match(/<th>/g)||[]).length, 9);
assert.ok(main.innerHTML.includes('id="addEmp"'));
assert.ok(main.innerHTML.includes('data-edit="test"'));
assert.ok(main.innerHTML.includes('>56</a>'));
assert.ok(html.includes('${crmLink(b.me.name, b.me.url)}'));
assert.ok(html.includes('${!PREVIEW ? `<a href="#staff"'));
console.log('Staff directory permissions, profile links and admin columns verified');
