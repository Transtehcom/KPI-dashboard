const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html','utf8');
const start = html.indexOf('    const job = document.getElementById("cJob")');
const end = html.indexOf('    if (isMgr) watchJob();',start);
assert.ok(start >= 0 && end > start);
const nodes = Object.fromEntries(['cJob','cProc','cJobsHint','cJobsList','cRunAt','cJobsRefresh'].map(id=>[id,{innerHTML:'',textContent:'',handlers:{},addEventListener(k,fn){this.handlers[k]=fn;}}]));
const calls=[],toasts=[],autoUpdates=[];
let response = {workers:3,timezone:'UTC+0500',auto:{enabled:false,revision:0},jobs:[{id:'job1',status:'queued',total:5,done:0,model:'terra',effort:'high',run_at:'2026-11-10T12:00',message:'<unsafe>'}]};
const context = vm.createContext({state:{view:'calls',cRunAt:'2026-11-10T12:00'},
 document:{getElementById:id=>nodes[id]},MODELS:[['terra','Model label']],EFFORTS:[['high','High label']],modelEfforts:()=>[['high','High label']],fastCalls:()=>false,
 esc:v=>String(v??'').replaceAll('<','&lt;'),nf:String,dtLabel:String,
 confirm:()=>true,pick:()=> 'terra',toast:s=>toasts.push(s),loadCalls:()=>{},updateAuto:value=>autoUpdates.push(value),
 setTimeout:()=>1,clearTimeout:()=>{},api:async(path,body)=>{calls.push([path,body]);return path==='/api/calls-job'?response:{ok:true,message:'created'};}});
vm.runInContext(html.slice(start,end)+'\nthis.watch=watchJob; this.startProc=startProc;',context);
(async()=>{
 await context.watch();
 assert.equal(autoUpdates[0].enabled,false);
 assert.match(nodes.cJobsList.innerHTML,/data-cancel-job="job1"/);
 assert.match(nodes.cJobsList.innerHTML,/Model label/);
 assert.ok(!nodes.cJobsList.innerHTML.includes('<unsafe>'));
 assert.match(nodes.cJobsHint.textContent,/3/);
 nodes.cRunAt.value='2026-11-11T13:00';nodes.cRunAt.handlers.input();
 await context.startProc({id:'call1'});
 const request=calls.find(([path])=>path.startsWith('/api/calls-process'));
 assert.equal(request[1].run_at,'2026-11-11T13:00');
 const button={dataset:{cancelJob:'job1'},disabled:false};
 await nodes.cJobsList.handlers.click({target:{closest:()=>button}});
 assert.ok(calls.some(([path,b])=>path==='/api/calls-job-cancel'&&b.id==='job1'));
 response={...response,jobs:[{...response.jobs[0],status:'cancelled'}]};
 await context.watch();
 assert.doesNotMatch(nodes.cJobsList.innerHTML,/data-cancel-job/);
 assert.ok(toasts.some(s=>s.includes('завершилась')));
 response={...response,jobs:[{...response.jobs[0],status:'failed',failed:1,errors:[{id:'call15966',message:'Timeout <script>'}]}]};
 await context.watch();
 assert.match(nodes.cJobsList.innerHTML,/call15966/);
 assert.match(nodes.cJobsList.innerHTML,/Timeout &lt;script>/);
 assert.match(nodes.cJobsList.innerHTML,/Причины ошибок/);
 assert.ok(!nodes.cJobsList.innerHTML.includes('<script>'));
 context.state.view='team';const before=calls.length;await context.watch();assert.equal(calls.length,before);
 console.log('Call job scheduling, progress, cancel controls and preserved launch time: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
