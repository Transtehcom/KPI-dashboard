const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const start=html.indexOf('  function homeGreeting('),end=html.indexOf('  // ---------- каркас',start);
assert.ok(start>=0 && end>start,'home helpers exist');
const context={Intl,Date,esc:String,PREVIEW:null,homeTimer:null,state:{view:'home',boot:{me:{name:'Кирилл Бартов',role:'manager'}}},
  document:{getElementById:()=>null},clearTimeout:()=>{},setTimeout:()=>0};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
for(const [utc,word] of [['2026-11-13T02:00:00Z','Доброе утро'],['2026-11-13T08:00:00Z','Добрый день'],
  ['2026-11-13T14:00:00Z','Добрый вечер'],['2026-11-13T20:00:00Z','Доброй ночи']]) {
  assert.equal(context.homeGreeting('Кирилл Бартов',new Date(utc)),word+', Кирилл');
}
assert.equal(context.homeSections(true).length,9);
assert.equal(context.homeSections(false).length,5);
assert.deepEqual(Array.from(context.homeSections(false),x=>x.href),['#kpi','#funnel','#calls','#gpt','#staff']);
const main={innerHTML:''};context.document.getElementById=id=>id==='main'?main:null;
context.renderHome();
assert.equal((main.innerHTML.match(/class="home-tile"/g)||[]).length,9);
assert.ok(main.innerHTML.includes('href="#team"'));
context.state.boot.me.role='employee';context.renderHome();
assert.equal((main.innerHTML.match(/class="home-tile"/g)||[]).length,5);
assert.ok(!main.innerHTML.includes('href="#money"'));
assert.match(html,/history\.replaceState\(null,\s*"",\s*"#home"\)/,'fresh login opens home');
assert.ok(html.includes('class="brand brand-home" href="#home"'));
assert.ok(html.includes('state.view === "home"'));
assert.match(html,/\.home-tile:hover[^}]*scale\(1\.025\)/s);
assert.match(html,/\.home-tile:active[^}]*scale\(1\)/s);
console.log('Home greeting, role-based links and tile interaction rules: OK');
