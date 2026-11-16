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
assert.match(html, /colspan="6">Битрикс24/);
assert.match(html, /colspan="6">1С/);
console.log('Workload six-column groups and independent on-time/overdue calculations: OK');
