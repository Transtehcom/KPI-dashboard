const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('    main.querySelectorAll("[data-kpi-jump]")');
assert.ok(start >= 0, 'KPI navigation must bind actual buttons');
const source = html.slice(start, html.indexOf('    bindChart(', start));
const scrolls = [], focused = [];
let reduced = false, headerBottom = 112;
const cards = new Map(['delivery', 'discipline'].map(code => ['kpi-' + code, {
  getBoundingClientRect:()=>({top:580}), focus:options=>focused.push(options)
}]));
const buttons = ['delivery', 'discipline'].map(code => ({
  getAttribute:name=>name==='aria-controls'?'kpi-'+code:null,
  addEventListener(event,handler) { assert.equal(event,'click'); this.click=handler; }
}));
const main = {querySelectorAll:()=>buttons, contains:card=>[...cards.values()].includes(card)};
const document = {getElementById:id=>cards.get(id), querySelector:()=>({getBoundingClientRect:()=>({bottom:headerBottom})})};
vm.runInNewContext(source, {main, document, window:{scrollY:300, scrollTo:options=>scrolls.push(options),
  matchMedia:()=>({matches:reduced})}});
buttons[0].click();
assert.equal(scrolls[0].top, 752, 'sticky header and spacing must stay above the destination');
assert.equal(scrolls[0].behavior, 'smooth');
assert.equal(focused[0].preventScroll, true);
reduced=true; headerBottom=170;
buttons[1].click();
assert.equal(scrolls[1].top, 694, 'header height must be measured for each jump');
assert.equal(scrolls[1].behavior, 'auto');
cards.delete('kpi-delivery'); buttons[0].click();
assert.equal(scrolls.length, 2, 'a stale button must not scroll to another KPI');
assert.ok(html.includes('<button type="button" class="seg ${stateClass}"'));
assert.ok(html.includes('aria-controls="kpi-${esc(i.code)}"'));
assert.ok(html.includes('id="kpi-${esc(i.code)}" tabindex="-1"'));
assert.ok(!html.includes('class="weights" role="img"'));
console.log('KPI buttons navigate to their matching cards, account for the header and respect reduced motion');
