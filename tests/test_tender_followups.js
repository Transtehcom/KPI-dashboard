const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  function auditFollowups(');
const end = html.indexOf('  function auditRecord(', start);
assert.ok(start >= 0 && end > start);
const context = {
  state: {boot: {me: {role: 'manager'}}},
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  nf: String,
  operationsMoment: String,
};
context.crmLink = (title, url) => `<a href="${context.esc(url)}">${context.esc(title)}</a>`;
vm.runInNewContext(html.slice(start, end), context);
const rows = [{id: '201', subject: '<script>test</script>', url: 'https://crm.example.test/crm/company/details/42/?activity_id=201',
  state: 'excluded', created: '2026-11-03T10:30:00', deadline: '2026-11-12T12:00:00',
  moves: 2, override_moves: 5, total_moves: 6}];
const manager = context.auditFollowups(rows);
assert.ok(manager.includes('data-followup-id="201"'));
assert.ok(manager.includes('value="6"'));
assert.ok(manager.includes('Более 5 переносов'));
assert.ok(!manager.includes('<script>'));
context.state.boot.me.role = 'employee';
const employee = context.auditFollowups(rows);
assert.ok(!employee.includes('<form'));
assert.ok(employee.includes('Переносов 6'));
const summary = context.followupSummary({title: 'Зафиксирован следующий шаг', rows: [['Назначено дело не выполнено', 2],
  ['Назначено выполнено', 3], ['Назначено просрочено', 1]], hint: '24 часа', unknown: 0});
assert.equal((summary.match(/class="followup-count"/g) || []).length, 3);
assert.ok(summary.includes('Назначено выполнено'));
console.log('Tender follow-up summary, admin-only inputs and escaping verified');
