const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  // ---------- оценки разбора звонков');
assert.ok(start >= 0, 'call feedback UI must exist');
const end = html.indexOf('  // ---------- звонки:', start);
const context = {
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  dtLabel: value => value || '',
};
vm.runInNewContext(html.slice(start, end), context);
const criteria = [{key: 'secretary', label: 'Секретарь'}, {key: 'tone', label: 'Интонация'}];
const call = {id: 'call1', can_rate: true, feedback: []};
assert.match(context.callFeedbackCell(call, false), /Оценить разбор/);
assert.doesNotMatch(context.callFeedbackCell({...call, can_rate: false}, false), /data-feedback/);
const feedback = {author: 'pm', rating: 3, comment: '<script>bad</script>', criteria: ['tone'], status: 'pending', revision: 1};
const reviewed = {...call, feedback: [feedback]};
const employee = context.callFeedbackHtml(reviewed, criteria, false);
assert.match(employee, /name="comment"[^>]*required/);
assert.match(employee, /value="tone" checked/);
assert.ok(!employee.includes('<script>'));
assert.match(context.callFeedbackHtml(reviewed, criteria, true), /data-feedback-review/);
assert.match(context.callFeedbackHtml({...call, feedback: [{...feedback, status: 'resolved'}]}, criteria, false), /Проверено/);
const form = {elements: {rating: {value: '5'}, comment: {required: true}},
  querySelector: () => form.details, details: {hidden: false}};
context.updateCallRating(form);
assert.equal(form.details.hidden, true);
assert.equal(form.elements.comment.required, false);
for (const rating of ['1', '2', '3', '4']) {
  form.elements.rating.value = rating;
  context.updateCallRating(form);
  assert.equal(form.details.hidden, false);
  assert.equal(form.elements.comment.required, true);
}
console.log('Call feedback forms, permissions and rating requirements: OK');

const rechecked = {...reviewed, feedback: [{...feedback, revision: 2, rechecks: [{status:'completed',revision:1,model:'test',source_comment:'<bad>',findings:[{criterion:'agreement',verdict:'not_confirmed',explanation:'<script>no</script>',evidence:[{at_seconds:65,quote:'Не договорились'}]}],changed_fields:[]}]}]};
const response = context.callFeedbackHtml(rechecked,criteria,true);
assert.match(response,/Повторный разбор с замечаниями/);
assert.match(response,/Замечание не подтверждено/);
assert.match(response,/предыдущая версия/);
assert.match(response,/1:05/);
assert.ok(!response.includes('<script>'));
assert.doesNotMatch(context.callFeedbackHtml(rechecked,criteria,false),/data-feedback-reprocess/);
