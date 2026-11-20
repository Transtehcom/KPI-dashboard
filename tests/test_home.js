const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const start=html.indexOf('  function homeGreeting('),end=html.indexOf('  // ---------- каркас',start);
assert.ok(start>=0 && end>start,'home helpers exist');
const context={Intl,Date,esc:String,PREVIEW:null,homeTimer:null,state:{view:'home',boot:{me:{name:'Кирилл Бартов',role:'manager'}}},
  DashboardTheme:{button:()=>'<button data-theme-toggle>Тёмная тема</button>'},
  document:{getElementById:()=>null},clearTimeout:()=>{},setTimeout:()=>0};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
vm.runInContext(html.slice(html.indexOf('  const METRICS ='),html.indexOf('  const metricsView =')),context);
for(const [utc,word] of [['2026-11-13T02:00:00Z','Доброе утро'],['2026-11-13T08:00:00Z','Добрый день'],
  ['2026-11-13T14:00:00Z','Добрый вечер'],['2026-11-13T20:00:00Z','Доброй ночи']]) {
  assert.equal(context.homeGreeting('Кирилл Бартов',new Date(utc)),word+', Кирилл');
}
for(const [local,word] of [['00:00','Добрый вечер'],['00:01','Доброй ночи'],
  ['06:00','Доброй ночи'],['06:01','Доброе утро'],['12:00','Доброе утро'],
  ['12:01','Добрый день'],['18:00','Добрый день'],['18:01','Добрый вечер'],['23:59','Добрый вечер']]) {
  assert.equal(context.homeGreeting('Кирилл Бартов',new Date('2026-11-13T'+local+':59+05:00')),word+', Кирилл');
}
assert.equal(context.homeSections(true).length,10);
assert.equal(context.homeSections(false).length,5);
assert.deepEqual(Array.from(context.homeSections(false),x=>x.href),['#kpi','#funnel','#calls','#gpt','#staff']);
const sections=context.homeSections(true);
assert.deepEqual(Array.from(sections.find(section=>section.key==='funnel').pages,page=>page.href),['#funnel','#funnel-fact']);
assert.deepEqual(Array.from(sections.find(section=>section.key==='metrics').pages,page=>page.href),['#metrics-sales','#metrics-supply','#metrics-logistics']);
assert.deepEqual(Array.from(sections.find(section=>section.key==='suppliers').pages,page=>page.href),['#suppliers','#suppliers?mode=requests','#suppliers?mode=directory']);
const panel={};
const main={innerHTML:'',querySelector:()=>panel,querySelectorAll:()=>[],addEventListener:()=>{}};context.document.getElementById=id=>id==='main'?main:null;
context.bindHomeStats=(element,visible)=>{assert.equal(element,panel);assert.ok(visible.some(s=>s.key==='kpi'));};
context.workloadTimingApplicable=()=>true;
context.workloadOnecAvailable=()=>true;
const groups=context.homeWorkRows({generated:'2026-11-20T12:00:00',onec:{loaded:true},
  tasks:[{uid:1,title:'A',open:true,deadline:'2026-11-19'},{uid:2,title:'B',open:true}],
  documents:[{uid:1,is_closed:true,entity:'Document_Test',number:'C',closed_at:'2026-11-02'}]},
  {uid:1,group:'ОС'},'2026-11');
assert.deepEqual(Array.from(Object.values(groups),rows=>rows.length),[0,1,1]);
vm.runInContext(html.slice(html.indexOf('  function workloadSecondsBetween('),html.indexOf('  function workloadDelaySeconds(')),context);
const deadline='2026-11-20T12:00:00', later='2026-11-20T12:01:00';
const data={generated:later,people:[{uid:1,group:'ОС'},{uid:2,group:'ОЛ'}],
  tasks:[{uid:1,open:true,deadline,exec_at:deadline},{uid:1,open:false,deadline,closed:later},
         {uid:1,open:true,deadline:later},{uid:2,open:true,deadline}],
  onec:{loaded:true},documents:[{uid:1,is_closed:true,deadline,closed_at:later}]};
assert.deepEqual(Array.from(context.homeEfficiency(data,data.people[0],false),rows=>rows.length),[1,2,1,0]);
assert.deepEqual(Array.from(context.homeEfficiency(data,data.people[0],true),rows=>rows.length),[1,2,1,1]);
const detail={};
const sector={dataset:{sector:"1"},matches:()=>true,classList:{toggle(){}},setAttribute(){}};
const ring={querySelectorAll:()=>[sector],querySelector:()=>detail};
context.nf=String;
context.drawHomeRing(ring,["Без просрочки","Просрочено"],[[],["Задача 17"]],["blue","red"]);
assert.equal((ring.innerHTML.match(/<path /g)||[]).length,1);
sector.onclick();
assert.match(detail.innerHTML,/Просрочено · 1/);
assert.match(detail.innerHTML,/Задача 17/);
context.renderHome();
assert.equal((main.innerHTML.match(/class="home-tile(?:\s|")/g)||[]).length,10);
assert.equal((main.innerHTML.match(/data-home-split/g)||[]).length,3);
assert.ok(main.innerHTML.includes('aria-controls="home-pages-funnel"'));
assert.ok(main.innerHTML.includes('href="#team"'));
context.state.boot.me.role='employee';context.renderHome();
assert.equal((main.innerHTML.match(/class="home-tile(?:\s|")/g)||[]).length,5);
assert.equal((main.innerHTML.match(/data-home-split/g)||[]).length,1);
assert.ok(!main.innerHTML.includes('href="#money"'));
assert.match(html,/history\.replaceState\(null,\s*"",\s*"#home"\)/,'fresh login opens home');
assert.ok(html.includes('class="brand brand-home" href="#home"'));
assert.ok(html.includes('state.view === "home"'));
assert.match(html,/\.home-tile:hover[^}]*scale\(1\.025\)/s);
assert.match(html,/\.home-tile:active[^}]*scale\(1\)/s);

const events={},attrs={},pages={inert:true},classes=new Set();
const front={setAttribute:(name,value)=>{attrs[name]=value;},focus:()=>{context.document.activeElement=front;events.focusin();},
  addEventListener:(name,handler)=>{events['button:'+name]=handler;}};
const link={focus:()=>{context.document.activeElement=link;events.focusin();}};
const card={classList:{toggle:(name,enabled)=>enabled?classes.add(name):classes.delete(name)},
  querySelector:selector=>({'.home-split-front':front,'.home-split-pages':pages,'.home-split-link':link})[selector],
  contains:element=>[front,link,pages].includes(element),addEventListener:(name,handler)=>{events[name]=handler;}};
let outsideClick;
context.bindHomeSplits({querySelectorAll:()=>[card],addEventListener:(_,handler)=>{outsideClick=handler;}});
events.pointerenter({pointerType:'touch'});assert.equal(pages.inert,true);
events.pointerenter({pointerType:'mouse'});assert.equal(pages.inert,false);assert.equal(attrs['aria-expanded'],'true');
context.document.activeElement=null;events.pointerleave();assert.equal(pages.inert,true);
events['button:click']({detail:1});assert.equal(pages.inert,false);
events['button:click']({detail:0});assert.equal(context.document.activeElement,link);
events.keydown({key:'Escape',preventDefault:()=>{}});assert.equal(pages.inert,true);assert.equal(context.document.activeElement,front);
events.focusin();events.focusout({relatedTarget:null});assert.equal(pages.inert,true);
events.focusin();outsideClick({target:{}});assert.equal(pages.inert,true);
console.log('Home greeting, role-based links and tile interaction rules: OK');
