const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const context = {state: {}, URLSearchParams};
vm.createContext(context);
const routing = html.slice(html.indexOf('  const supQuery ='), html.indexOf('  async function loadSuppliers('));
vm.runInContext(routing, context);
context.supFromHash('suppliers?mode=directory');
assert.equal(context.state.sup.mode, 'directory');
assert.equal(context.supParams().get('mode'), 'directory');
const filter = html.slice(html.indexOf('  function supplierDirectoryFiltered('), html.indexOf('  function renderSupplierDirectory('));
vm.runInContext(filter, context);
const rows = [{fields: {'Название': 'Ёмкости А', 'Реквизит: ИНН': '00123', 'Теги отдела снабжения': 'Резервуары'}},
              {fields: {'Название': 'Б', 'Реквизит: ИНН': '456'}}];
assert.equal(context.supplierDirectoryFiltered(rows, 'емкости 00123').length, 1);
assert.equal(context.supplierDirectoryFiltered(rows, 'резервуары').length, 1);
assert.equal(context.supplierDirectoryFiltered(rows, 'unknown').length, 0);
assert.ok(html.includes('href="#suppliers?mode=directory" data-tab="suppliers" data-mode="directory"'));
assert.ok(html.includes('await api("/api/supplier-directory/gpt",'));
assert.ok(html.includes('if (d.mode === "directory") { renderSupplierDirectory(d); return; }'));
console.log('Supplier directory navigation, filtering and download: OK');

(async () => {
  const button = {isConnected: true};
  Object.assign(context, {location: {}, document: {getElementById: () => null},
    renderShell: () => {}, load: async () => {}, toast: message => { throw new Error(message); }});
  context.state.view = 'suppliers';
  context.state.sup = {mode: 'directory', search: '00123'};
  let requests = 0;
  context.api = async (url, body) => {
    requests++;
    assert.equal(url, '/api/supplier-directory/gpt');
    assert.equal(body.search, '00123');
    return {chat_id: 'own-chat'};
  };
  await Promise.all([context.startSupplierExport(button), context.startSupplierExport(button)]);
  assert.equal(requests, 1);
  assert.equal(context.state.view, 'gpt');
  assert.equal(context.state.gptChat, 'own-chat');
  assert.equal(context.location.hash, 'gpt');
  assert.equal(context.state.supplierExportStarting, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
