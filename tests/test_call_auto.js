const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  function chooseCallAuto(');
const end = html.indexOf('  function renderCalls(', start);
assert.ok(start >= 0 && end > start);
const models = {models: {sol: 'Sol', luna: 'Luna'}, efforts: {medium: 'Средний', high: 'Высокий'},
  model_efforts: {sol: {medium: 'Средний', high: 'Высокий'}, luna: {medium: 'Средний'}}};
function node() {
  return {value: '', checked: false, disabled: false, innerHTML: '', textContent: '', handlers: {},
    addEventListener(event, handler) { this.handlers[event] = handler; }};
}
const dialogs = [];
function dialog() {
  const value = node();
  value.nodes = Object.fromEntries(['form', '[name=model]', '[name=effort]', '[name=fast]', '[name=period]', '[data-auto-cancel]']
    .map(key => [key, node()]));
  value.querySelector = key => value.nodes[key];
  value.nodes.form.reportValidity = () => ['model', 'effort', 'fast', 'period'].every(key => value.nodes[`[name=${key}]`].value !== '');
  value.setAttribute = () => {};
  value.showModal = () => { value.open = true; };
  value.remove = () => { value.removed = true; };
  value.close = () => { value.open = false; value.handlers.close(); };
  return value;
}
const requests = [];
let saved = {enabled: true, revision: 1, model: 'sol', effort: 'high', fast: true, period: 'today'};
const context = vm.createContext({
  document: {createElement: () => dialog(), body: {append(value) { dialogs.push(value); }}},
  esc: value => String(value).replaceAll('<', '&lt;'),
  api: async (path, body) => {
    if (body) { requests.push([path, body]); saved = {...saved, ...body, revision: saved.revision + 1}; }
    return saved;
  }, toast: () => {},
});
vm.runInContext(html.slice(start, end), context);
function select(value, field, selection) {
  const control = value.nodes[`[name=${field}]`];
  control.value = selection;
  if (control.handlers.change) control.handlers.change();
}
function submit(value) { value.nodes.form.handlers.submit({preventDefault() {}}); }
(async () => {
  const first = context.chooseCallAuto(models);
  const firstDialog = dialogs.at(-1);
  submit(firstDialog);
  assert.equal(firstDialog.open, true, 'incomplete settings must keep the dialog open');
  select(firstDialog, 'model', 'sol');
  assert.match(firstDialog.nodes['[name=effort]'].innerHTML, /high/);
  select(firstDialog, 'effort', 'high');
  select(firstDialog, 'model', 'luna');
  assert.equal(firstDialog.nodes['[name=effort]'].value, '', 'model changes must require a fresh effort');
  assert.doesNotMatch(firstDialog.nodes['[name=effort]'].innerHTML, /high/);
  firstDialog.nodes['[data-auto-cancel]'].handlers.click();
  assert.equal(await first, null);
  const panel = node();
  panel.nodes = {'[data-auto-toggle]': node(), '[data-auto-state]': node()};
  panel.querySelector = key => panel.nodes[key];
  const update = context.mountCallAuto(panel, models);
  update(saved);
  const toggle = panel.nodes['[data-auto-toggle]'];
  assert.equal(toggle.checked, true);
  toggle.checked = false;
  await toggle.handlers.change();
  assert.equal(saved.enabled, false);
  assert.equal(requests.length, 1);
  assert.equal(dialogs.length, 1, 'disabling does not need model selection');
  toggle.checked = true;
  const enabling = toggle.handlers.change();
  const secondDialog = dialogs.at(-1);
  for (const field of ['model', 'effort', 'fast', 'period']) {
    assert.equal(secondDialog.nodes[`[name=${field}]`].value, '', 'each enable must start with unselected parameters');
  }
  select(secondDialog, 'model', 'sol');
  select(secondDialog, 'effort', 'high');
  select(secondDialog, 'fast', 'true');
  select(secondDialog, 'period', 'yesterday');
  submit(secondDialog);
  await enabling;
  const body = requests.at(-1)[1];
  assert.equal(body.period, 'yesterday');
  assert.equal(body.effort, 'high');
  assert.equal(body.fast, true);
  assert.equal(toggle.checked, true);
  assert.match(panel.nodes['[data-auto-state]'].textContent, /предыдущий/);
  update({...saved, revision: 1, enabled: false});
  assert.equal(toggle.checked, true, 'late polling must not undo a successful setting update');
  toggle.checked = false;
  await toggle.handlers.change();
  toggle.checked = true;
  const third = toggle.handlers.change();
  const thirdDialog = dialogs.at(-1);
  assert.equal(thirdDialog.nodes['[name=effort]'].value, '');
  thirdDialog.nodes['[data-auto-cancel]'].handlers.click();
  await third;
  assert.equal(toggle.checked, false);
  assert.equal(saved.enabled, false);
  console.log('Auto-analysis switch, fresh selection, model-specific effort, cancel and late responses verified');
})().catch(error => { console.error(error); process.exitCode = 1; });
