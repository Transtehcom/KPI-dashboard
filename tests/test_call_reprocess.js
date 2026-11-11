const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  function chooseCallReprocessing(');
const end = html.indexOf('  function chooseCallAuto(', start);
assert.ok(start >= 0 && end > start);
const dialogs = [];
function button(choice) {
  return {dataset: {reprocess: choice}, handlers: {}, addEventListener(event, handler) { this.handlers[event] = handler; }};
}
const context = vm.createContext({nf: String, esc: String,
  document: {createElement: () => {
    const buttons = [button('skip'), button('repeat')], cancel = button('cancel');
    return {buttons, cancel, handlers: {}, setAttribute() {},
      querySelectorAll: () => buttons, querySelector: () => cancel,
      addEventListener(event, handler) { this.handlers[event] = handler; },
      showModal() {}, close() { this.handlers.close(); }, remove() { this.removed = true; }};
  }, body: {append(value) { dialogs.push(value); }}}});
vm.runInContext(html.slice(start, end), context);
(async () => {
  const skip = context.chooseCallReprocessing({total: 20, processed: 5, pending: 15});
  dialogs.at(-1).buttons[0].handlers.click();
  assert.equal(await skip, false);
  const repeat = context.chooseCallReprocessing({total: 20, processed: 5, pending: 15});
  dialogs.at(-1).buttons[1].handlers.click();
  assert.equal(await repeat, true);
  const cancel = context.chooseCallReprocessing({total: 20, processed: 20, pending: 0});
  dialogs.at(-1).cancel.handlers.click();
  assert.equal(await cancel, null);
  assert.ok(dialogs.every(dialog => dialog.removed));
  console.log('Repeat analysis dialog supports include, skip and cancellation');
})().catch(error => { console.error(error); process.exitCode = 1; });
