const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/assets/brand-field.js', 'utf8');
const layers = [], frames = [], listeners = {};
let marks = 0, rings = 0;
const context = new Proxy({}, {get: (target, key) => {
  if (key === 'stroke') return () => marks++;
  if (key === 'arc') return () => rings++;
  return target[key] || (() => {});
}});
const media = {matches:false, addEventListener:()=>{}};
const document = {hidden:false, body:{appendChild:node=>layers.push(node)},
  documentElement:{dataset:{}, addEventListener:()=>{}},
  getElementById:()=>({}), addEventListener:()=>{},
  createElement:()=>({setAttribute:()=>{},getContext:()=>context})};
class Observer { observe() {} }
const window = {Path2D:class {}, matchMedia:()=>media, innerWidth:120, innerHeight:100,
  devicePixelRatio:1, addEventListener:(name,fn)=>listeners[name]=fn};
vm.runInNewContext(source, {window, document, matchMedia:()=>media, Path2D:window.Path2D,
  MutationObserver:Observer, performance:{now:()=>10},
  requestAnimationFrame:fn=>{frames.push(fn);return frames.length;}, cancelAnimationFrame:()=>{}});
assert.equal(layers.length, 1, 'logo field must have only the layer behind the interface');
assert.equal(layers[0].className, 'brand-field brand-field--back');
frames.shift()(20);
assert.ok(marks > 0, 'neutral logos must still be drawn on the free background');
listeners.pointermove({pointerType:'mouse',clientX:50,clientY:50});
listeners.pointerdown({button:0,clientX:50,clientY:50});
frames.shift()(100);
assert.ok(rings > 0, 'click ripple must remain on the free background');
assert.ok(!source.includes('brand-field--front'));
console.log('Background logos stay behind panels and retain the click ripple');
