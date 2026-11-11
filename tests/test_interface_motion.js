const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/assets/interface-motion.js', 'utf8');

function fixture() {
  const frames = new Map(), animations = [], observers = [], intersections = [];
  let frameId = 0;
  class Node {
    constructor(kind, text = '') {
      this.kind = kind; this.textContent = text; this.nodeType = 1; this.isConnected = true;
      this.style = {overflow: 'visible', setProperty(name, value) { this[name] = value; }}; this.dataset = {}; this.nodes = [];
      this.classes = new Set();
      this.classList = {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)};
    }
    matches(selector) {
      if (selector.includes('.main >')) return this.kind === 'card';
      if (selector.includes('.track .fill')) return this.kind === 'bar';
      if (selector.includes('.stat .v')) return this.kind === 'value';
      if (selector.includes('details.more')) return this.kind === 'details';
      if (selector === '.empty') return this.kind === 'loading';
      return false;
    }
    closest(selector) {
      if (selector.includes('.op-stat') && this.kind === 'card') return this;
      if (selector.includes('#cJobsList') && this.kind === 'poll') return this;
      if (selector === 'summary') return this.kind === 'summary' ? this : null;
      if (selector === 'details') return this.parentElement?.kind === 'details' ? this.parentElement : null;
      return this.matches(selector) ? this : null;
    }
    querySelectorAll(selector) { return this.nodes.filter(node => node.matches(selector)); }
    querySelector(selector) {
      if (selector === '.tabs') return this.nav || null;
      if (selector === '.motion-tab-indicator') return this.indicator || null;
      if (selector.includes('a.on')) return this.active || null;
      return null;
    }
    contains(node) { return node.isConnected; }
    appendChild(node) { this.indicator = node; node.parentElement = this; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    hasAttribute(name) { return Object.hasOwn(this, name); }
    getBoundingClientRect() { return this.rect || {left: 0, top: 0, width: 200, height: 80, bottom: 80}; }
    animate(keyframes, options) {
      const animation = {node: this, keyframes, options, cancelled: false,
        cancel() { this.cancelled = true; this.oncancel?.(); }, finish() { this.finished = true; this.onfinish?.(); }};
      animations.push(animation);
      return animation;
    }
  }
  const root = new Node('root'), card = new Node('card'), bar = new Node('bar'), value = new Node('value', '120 000 ₽');
  root.nodes = [card, bar, value];
  root.nav = new Node('nav'); root.nav.rect = {left: 10, top: 0, width: 500, height: 40};
  root.nav.active = new Node('active'); root.nav.active.rect = {left: 30, top: 0, width: 90, height: 40};
  const reduced = {matches: false, addEventListener(event, handler) { this.change = handler; }};
  const document = {hidden: false, listeners: {}, getElementById: () => root,
    createElement: () => new Node('indicator'), addEventListener(event, handler) { this.listeners[event] = handler; }};
  document.fonts = {ready: {then(callback) { this.callback = callback; }},
    addEventListener(event, handler) { this[event] = handler; }};
  const window = {listeners: {}, addEventListener(event, handler) { this.listeners[event] = handler; }};
  class Observer {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(target, options) { this.target = target; this.options = options; }
    emit(records) { this.callback(records); }
  }
  class Intersections {
    constructor(callback) { this.callback = callback; this.nodes = new Set(); intersections.push(this); }
    observe(node) { this.nodes.add(node); }
    unobserve(node) { this.nodes.delete(node); }
    emit(nodes) { this.callback(nodes.map(target => ({target, isIntersecting: true}))); }
  }
  const context = {window, document, Element: Node, MutationObserver: Observer, IntersectionObserver: Intersections,
    matchMedia: () => reduced, requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame(id) { frames.delete(id); },
    getComputedStyle: () => ({paddingTop: '0', paddingBottom: '0', borderTopWidth: '0', borderBottomWidth: '0'})};
  vm.runInNewContext(source, context);
  const flush = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback()); };
  const mutate = records => observers[0].emit(records);
  return {Node, root, card, bar, value, reduced, window, document, frames, animations, intersections, flush, mutate};
}

const f = fixture();
f.document.listeners.pointermove({target:f.card, pointerType:'mouse', clientX:30, clientY:20});
f.flush();
assert.ok(f.card.classes.has('motion-pointer'));
assert.equal(f.card.style['--pointer-x'], '30px');
assert.equal(f.card.style['--pointer-y'], '20px');
f.document.listeners.pointermove({target:f.root, pointerType:'mouse', clientX:30, clientY:20});
f.flush();
assert.ok(!f.card.classes.has('motion-pointer'), 'pointer highlight leaves the previous card');
f.flush();
assert.ok(f.intersections[0].nodes.has(f.card));
assert.ok(f.intersections[0].nodes.has(f.bar));
f.intersections[0].emit([f.card, f.bar]);
assert.equal(f.animations.length, 2);
assert.equal(f.value.textContent, '120 000 ₽', 'motion must preserve actual numeric values');
assert.equal(f.intersections[0].nodes.size, 0);
f.root.nav.active.rect.width = 104;
f.document.fonts.loadingdone(); f.flush();
assert.equal(f.root.nav.indicator.style.width, '104px', 'font loading must refresh the indicator geometry');
f.root.nav.active.rect.left = 160;
f.mutate([{type: 'childList', target: f.root, addedNodes: [], removedNodes: []}]);
f.flush();
assert.equal(f.root.nav.indicator.style.left, '150px');
assert.ok(f.animations.some(animation => animation.node === f.root.nav.indicator), 'active tab indicator must move');
f.root.nav.active.rect.top = 42;
f.mutate([{type: 'childList', target: f.root, addedNodes: [], removedNodes: []}]); f.flush();
assert.equal(f.root.nav.indicator.style.top, '79px', 'wrapped navigation must underline the selected row');
const count = f.animations.length;
f.mutate([{type: 'childList', target: f.root.nav, addedNodes: [f.root.nav.indicator], removedNodes: []}]);
f.flush();
assert.equal(f.animations.length, count, 'the indicator must not restart its own animation');
const loading = new f.Node('loading', 'Загрузка…');
f.mutate([{type: 'childList', target: f.root, addedNodes: [loading], removedNodes: []}]); f.flush();
assert.equal(loading['aria-busy'], 'true');
assert.ok(loading.classes.has('motion-loading'));
loading.textContent = 'За период записей нет';
f.mutate([{type: 'characterData', target: {parentElement: loading}}]); f.flush();
assert.ok(!loading.classes.has('motion-loading'));
assert.equal(loading['aria-busy'], undefined);
f.value.textContent = '150 000 ₽';
for (let index = 0; index < 20; index++) {
  f.mutate([{type: 'characterData', target: {parentElement: f.value}}]);
}
assert.equal(f.frames.size, 1, 'updates must batch into one browser frame');
f.flush();
assert.equal(f.animations.at(-1).node, f.value);
assert.equal(f.value.textContent, '150 000 ₽');
const poll = new f.Node('poll');
f.mutate([{type: 'childList', target: poll, addedNodes: [new f.Node('card')], removedNodes: []}]);
assert.equal(f.frames.size, 0, 'background job polling must not retrigger page motion');
const detached = new f.Node('card');
f.mutate([{type: 'childList', target: f.root, addedNodes: [detached], removedNodes: []}]);
f.flush();
assert.ok(f.intersections[0].nodes.has(detached));
detached.isConnected = false;
f.mutate([{type: 'childList', target: f.root, addedNodes: [], removedNodes: [detached]}]);
f.flush();
assert.ok(!f.intersections[0].nodes.has(detached), 'detached cards must leave the observer');

const details = new f.Node('details'), summary = new f.Node('summary');
summary.parentElement = details; summary.rect = {height: 30}; details.open = false; details.scrollHeight = 140;
const click = () => {
  const event = {target: summary, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  f.document.listeners.click(event);
  return event;
};
assert.equal(click().defaultPrevented, true);
assert.equal(details.open, true);
f.animations.at(-1).finish();
assert.equal(details.style.overflow, 'visible');
click();
assert.equal(details.open, true, 'closing should remain visible for its short transition');
f.animations.at(-1).finish();
assert.equal(details.open, false);
click(); click(); click();
f.animations.at(-1).finish();
assert.equal(details.open, true, 'rapid toggles must end in the latest requested state');
details.setAttribute('data-motion-native', '');
assert.equal(click().defaultPrevented, false, 'stateful disclosures must retain immediate native toggles');
details.removeAttribute('data-motion-native');
click();
f.reduced.matches = true; f.reduced.change();
assert.equal(details.open, false, 'reducing motion must finish the requested close');
assert.equal(details.style.overflow, 'visible');
const reducedCount = f.animations.length;
assert.equal(click().defaultPrevented, false, 'reduced motion preserves native disclosure behavior');
f.value.textContent = '170 000 ₽';
f.mutate([{type: 'characterData', target: {parentElement: f.value}}]); f.flush();
assert.equal(f.animations.length, reducedCount);
f.reduced.matches = false; f.reduced.change(); f.flush();
f.document.hidden = true; f.document.listeners.visibilitychange();
assert.equal(f.animations.filter(animation => !animation.cancelled && !animation.finished).length, 0);
f.document.hidden = false; f.document.listeners.visibilitychange(); f.flush();
f.window.listeners.beforeprint();
const printCount = f.animations.length;
f.value.textContent = '190 000 ₽';
f.mutate([{type: 'characterData', target: {parentElement: f.value}}]); f.flush();
assert.equal(f.animations.length, printCount);
console.log('Motion entry, tab transitions, real values, polling, disclosures, reduced motion and cleanup verified');
