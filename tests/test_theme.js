const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/assets/theme.js', 'utf8');
const html = fs.readFileSync('web/index.html', 'utf8');

function setup(saved, systemDark = false, storageBlocked = false) {
  const events = {}, writes = [], buttons = [{setAttribute(name, value) { this[name] = value; }}];
  const media = {matches:systemDark, addEventListener(name, handler) { events.media = handler; }};
  const document = {documentElement:{dataset:{}}, querySelectorAll:() => buttons,
    addEventListener(name, handler) { events[name] = handler; }};
  const context = {document, matchMedia:() => media,
    localStorage:{getItem() { if (storageBlocked) throw Error('blocked'); return saved; },
      setItem(key, value) { if (storageBlocked) throw Error('blocked'); writes.push([key, value]); }},
    addEventListener(name, handler) { events[name] = handler; }};
  vm.runInNewContext(source, context);
  return {context, document, events, writes, buttons, media};
}

const saved = setup('dark');
assert.equal(saved.document.documentElement.dataset.theme, 'dark');
assert.match(saved.context.DashboardTheme.button(), /role="switch"[^>]*aria-checked="true"/);
assert.match(saved.context.DashboardTheme.button(), /aria-label="Тёмная тема"/);
assert.match(saved.context.DashboardTheme.button(), /class="theme-orb"/);
assert.ok(!saved.context.DashboardTheme.button().includes('theme-toggle-track'));
assert.match(html, /\.theme-orb\s*\{[^}]*box-shadow:\s*inset/s);
assert.match(html, /\.theme-toggle\[aria-checked="true"\] \.theme-icon[^}]*rotate\(180deg\)/s);
saved.events.click({target:{closest:() => saved.buttons[0]}});
assert.equal(saved.document.documentElement.dataset.theme, 'light');
assert.equal(saved.buttons[0]['aria-checked'], 'false');
assert.deepEqual(saved.writes, [['ttk-theme', 'light']]);
saved.media.matches = true;
saved.events.media();
assert.equal(saved.document.documentElement.dataset.theme, 'light', 'manual choice survives system changes');
saved.events.storage({key:'ttk-theme', newValue:'dark'});
assert.equal(saved.buttons[0]['aria-checked'], 'true', 'other tabs share the preference');
saved.events.storage({key:null, newValue:null});
assert.equal(saved.document.documentElement.dataset.theme, 'dark');
assert.equal(setup('light', true).document.documentElement.dataset.theme, 'light');
assert.equal(setup('invalid', true).document.documentElement.dataset.theme, 'dark');
const automatic = setup(null);
automatic.media.matches = true;
automatic.events.media();
assert.equal(automatic.document.documentElement.dataset.theme, 'dark');
const blocked = setup(null, false, true);
blocked.events.click({target:{closest:() => blocked.buttons[0]}});
assert.equal(blocked.document.documentElement.dataset.theme, 'dark', 'blocked storage still allows switching');
assert.ok(html.indexOf('/assets/theme.js') < html.indexOf('<style>'), 'theme is restored before painting');
assert.match(html, /class="home-heading"[\s\S]*?DashboardTheme\.button\(\)/);
assert.match(html, /state\.view !== "home" \? DashboardTheme\.button\(\)/);
console.log('Theme persistence, both switches, system fallback and storage failures: OK');
