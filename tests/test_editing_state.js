const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const events = {}, timers = [], frames = [];
let main;
const document = {activeElement:null,addEventListener(name,handler) { events[name]=handler; },
  querySelector:()=>null,getElementById:id=>[main,...main.querySelectorAll('*')].find(node=>node.id===id)};
class Node {
  constructor(tag, properties={}) {
    Object.assign(this,{tagName:tag.toUpperCase(),children:[],dataset:{},style:{},id:'',type:'text',value:'',defaultValue:'',
      checked:false,defaultChecked:false,scrollLeft:0,scrollTop:0,isConnected:true,validity:{valid:true},selectionStart:null,
      classList:{contains:()=>false},rectTop:150},properties);
  }
  append(node) { this.children.push(node);node.parentElement=this;return node; }
  get firstElementChild() { return this.children[0]; }
  contains(node) { return this===node || this.children.some(child=>child.contains(node)); }
  hasAttribute(name) { return name.startsWith('data-') && this.dataset[name.slice(5)]!==undefined; }
  getAttribute(name) { return this.dataset[name.slice(5)]; }
  matches(selector) {
    if (selector.includes('input:not(')) return this.tagName==='TEXTAREA' || this.tagName==='INPUT' && !['checkbox','radio','button','submit','hidden'].includes(this.type);
    return selector.split(',').some(part=>{
      if (part==='*') return true;
      if (part.startsWith('input[type=')) return this.tagName==='INPUT' && part===`input[type=${this.type}]`;
      if (part==='[data-entry][data-ref]') return this.dataset.entry!==undefined && this.dataset.ref!==undefined;
      if (part==='[data-kpi-code]') return this.dataset.kpiCode!==undefined;
      const tagAttribute=part.match(/^(\w+)\[data-([^\]]+)\]$/);
      if (tagAttribute) return this.tagName===tagAttribute[1].toUpperCase() && this.hasAttribute('data-'+tagAttribute[2]);
      if (part.startsWith('[data-')) return this.hasAttribute(part.slice(1,-1));
      return part.toUpperCase()===this.tagName;
    });
  }
  closest(selector) { return this.matches(selector)?this:this.parentElement?.closest(selector); }
  querySelectorAll(selector) { return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]); }
  getBoundingClientRect() { return {top:this.rectTop,bottom:this.rectTop+30}; }
  focus() { document.activeElement=this; }
  setSelectionRange(start,end) { this.selectionStart=start;this.selectionEnd=end; }
  dispatchEvent(event) { event.target=this;events.change(event);if (!event.stopped) this.saved=(this.saved||0)+1; }
}
const context={document,Event:class {constructor(name,options) {Object.assign(this,{name,...options});}stopImmediatePropagation() {this.stopped=true;}},
  queueMicrotask,Promise,setTimeout:fn=>timers.push(fn),requestAnimationFrame:fn=>frames.push(fn),
  window:{scrollX:0,scrollY:400,scrollTo(position) {this.position=position;}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/assets/view-state.js','utf8'),context);
const ui=context.DashboardUI;
const change = input => {const event=new context.Event('change',{});event.target=input;events.change(event);return event;};
(async()=>{
  const date=new Node('input',{type:'date',value:'2026-11-12'});
  document.activeElement=date;
  assert.equal(change(date).stopped,true,'native changes while typing must not reach save handlers');
  date.validity.valid=false;date.value='';
  events.focusout({target:date});document.activeElement=null;await Promise.resolve();
  assert.equal(date.saved,undefined,'an incomplete date must never clear the saved value');
  date.validity.valid=true;date.value='2026-11-25';document.activeElement=date;change(date);
  events.focusout({target:date});document.activeElement=null;await Promise.resolve();
  assert.equal(date.saved,1,'the completed date is saved once after leaving the field');
  events.focusout({target:date});await Promise.resolve();
  assert.equal(date.saved,1,'leaving again does not submit a duplicate change');
  date.value='';document.activeElement=date;change(date);events.focusout({target:date});document.activeElement=null;await Promise.resolve();
  assert.equal(date.saved,2,'intentionally clearing a complete field still works');
  const actions=[], finishes=[];
  const first=ui.save(()=>new Promise(resolve=>{actions.push('first');finishes.push(resolve);}), 'same-field');
  const second=ui.save(()=>{actions.push('second');}, 'same-field');
  await Promise.resolve();await Promise.resolve();
  assert.deepEqual(actions,['first'],'rapid writes to one field are serialized');
  let painted=false;const refresh=ui.whenIdle(()=>painted=true);
  assert.equal(painted,false,'report painting waits for pending writes');
  finishes[0]();await first;await second;
  assert.deepEqual(actions,['first','second']);
  timers.shift()();await refresh;assert.equal(painted,true);
  const build = () => {
    const root=new Node('main',{id:'main',dataset:{uiView:'employee'}});
    const card=root.append(new Node('article',{id:'kpi-delivery',dataset:{kpiCode:'delivery'}}));
    const details=card.append(new Node('details',{open:false}));
    const table=details.append(new Node('div'));
    const input=table.append(new Node('input',{dataset:{entry:'',table:'orders',ref:'order-1',field:'date'},type:'text',value:'server',defaultValue:'server'}));
    return {root,card,details,table,input};
  };
  const old=build();main=old.root;document.activeElement=old.input;
  old.input.value='unfinished draft';old.input.selectionStart=3;old.input.selectionEnd=7;
  old.details.open=true;old.table.scrollLeft=130;old.table.scrollTop=80;
  const restore=ui.keepView(main,'employee');
  const next=build();main.children=next.root.children;next.card.parentElement=main;next.input.rectTop=170;
  restore();
  assert.equal(next.input.value,'unfinished draft');
  assert.equal(document.activeElement,next.input,'focus follows the same field identity');
  assert.equal(next.input.selectionStart,3);assert.equal(next.input.selectionEnd,7);
  assert.equal(next.details.open,true);assert.equal(next.table.scrollLeft,130);assert.equal(next.table.scrollTop,80);
  assert.equal(context.window.position.top,420,'scroll follows the edited field when preceding content changes height');
  assert.equal(main.dataset.uiRefresh,'','report updates suppress entry animations');
  next.input.value='';ui.saved(next.input,'',false);
  const restoreSource=ui.keepView(main,'employee');
  const source=build();main.children=source.root.children;source.card.parentElement=main;
  restoreSource();
  assert.equal(source.input.value,'server','clearing a saved override restores the latest source value');
  const sales=(id,path='/api/sales-object')=>{
    const root=new Node('main',{id:'main',dataset:{uiView:'sales'}});
    const row=root.append(new Node('table',{dataset:{path}})).append(new Node('tr',{dataset:{id}}));
    const input=row.append(new Node('input',{dataset:{f:'client'},value:'server',defaultValue:'server'}));
    return {root,input};
  };
  const rowA=sales('A');main=rowA.root;rowA.input.value='A draft';document.activeElement=rowA.input;
  const restorePage=ui.keepView(main,'sales');
  const rowB=sales('B');main.children=rowB.root.children;main.firstElementChild.parentElement=main;
  restorePage();assert.equal(rowB.input.value,'server','a paged-out sales draft cannot overwrite another record');
  const sameId=sales('A','/api/sales-action');main=rowA.root;main.children=sameId.root.children;main.firstElementChild.parentElement=main;
  restorePage();assert.equal(sameId.input.value,'server','object and action IDs have separate namespaces');
  console.log('Completed date commits, write ordering, drafts, focus, disclosures and scroll preservation: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
