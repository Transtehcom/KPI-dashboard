const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/assets/home-effects.js','utf8');
const listeners={},frames=new Map(),classes=new Set(),drawn=[];
let next=0,added=0,removed=0;
const context={clearRect:()=>{},save:()=>{},restore:()=>{},scale:()=>{},fill:()=>{},
  getImageData:()=>{
    const bytes=new Uint8ClampedArray(40*27*4);
    bytes[3]=255;bytes[39*4+3]=255;
    return {data:bytes};
  },fillRect(x,y){drawn.push({x,y,color:this.fillStyle,alpha:this.globalAlpha});}};
const paths=['#3A4185','#4BBBB8'].map(color=>({getAttribute:key=>key==='fill'?color:'M0,0L1,1Z'}));
const wrap={appendChild:()=>added++};
const svg={getBoundingClientRect:()=>({width:40,height:27}),querySelectorAll:()=>paths};
const brand={isConnected:true,querySelector:selector=>selector==='.brand-logo'?svg:wrap,
  contains:()=>false,classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)}};
const media={matches:false,addEventListener:(name,fn)=>listeners.media=fn};
const document={hidden:false,addEventListener:(name,fn)=>listeners[name]=fn,
  createElement:()=>({style:{},setAttribute:()=>{},getContext:()=>context,remove:()=>removed++})};
vm.runInNewContext(source,{document,window:{Path2D:class{},matchMedia:()=>media,devicePixelRatio:1},
  Path2D:class{},matchMedia:()=>media,performance:{now:()=>0},
  requestAnimationFrame:fn=>{frames.set(++next,fn);return next;},cancelAnimationFrame:id=>frames.delete(id)});
const event={target:{closest:()=>brand},relatedTarget:null};
listeners.pointerover(event);
assert.ok(classes.has('is-assembling'));assert.equal(added,1);
let frame=frames.values().next().value;frames.clear();frame(100);
const visible=drawn.filter(point=>point.alpha>0);
assert.equal(visible.length,2,'opposite ends assemble first');
assert.ok(visible.find(point=>point.color==='#3A4185').x<26,'blue dust approaches from left');
assert.ok(visible.find(point=>point.color==='#4BBBB8').x>65,'teal dust approaches from right');
frame=frames.values().next().value;frames.clear();frame(1200);
assert.ok(!classes.has('is-assembling'));assert.equal(removed,1);
media.matches=true;listeners.pointerover(event);assert.equal(added,1,'reduced motion keeps the original logo');
console.log('Logo assembles in opposite directions and restores its static state');
