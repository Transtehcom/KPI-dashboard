const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html','utf8');
const start = html.indexOf('    const job = document.getElementById("cJob")');
const end = html.indexOf('    if (isMgr) watchJob();',start);
assert.ok(start >= 0 && end > start);
const nodes = Object.fromEntries(['cJob','cProc','cJobsHint','cJobsList','cRunAt','cJobsRefresh'].map(id=>[id,{innerHTML:'',textContent:'',handlers:{},addEventListener(k,fn){this.handlers[k]=fn;}}]));
const calls=[],toasts=[],autoUpdates=[];
let processingResponses=[],repeatChoice=false;
let response = {workers:3,timezone:'UTC+0500',auto:{enabled:false,revision:0},jobs:[{id:'job1',status:'queued',total:5,done:0,model:'terra',effort:'high',run_at:'2026-11-10T12:00',message:'<unsafe>'}]};
const context = vm.createContext({state:{view:'calls',cRunAt:'2026-11-10T12:00'},
 document:{getElementById:id=>nodes[id]},MODELS:[['terra','Model label']],EFFORTS:[['high','High label']],modelEfforts:()=>[['high','High label']],fastCalls:()=>false,
 esc:v=>String(v??'').replaceAll('<','&lt;'),nf:String,dtLabel:String,
 confirm:()=>true,pick:()=> 'terra',toast:s=>toasts.push(s),loadCalls:()=>{},updateAuto:value=>autoUpdates.push(value),
 chooseCallReprocessing:async()=>repeatChoice,
 setTimeout:()=>1,clearTimeout:()=>{},api:async(path,body)=>{calls.push([path,body]);return path==='/api/calls-job'?response:processingResponses.shift()||{ok:true,message:'created',job:{id:'new'}};}});
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
 processingResponses=[{needs_choice:true,ids:['finished','new'],total:2,processed:1,pending:1},
   {ok:true,message:'created',job:{id:'skip'}}];
 const beforeSkip=calls.length;
 await context.startProc({mode:'all'});
 const skipRequests=calls.slice(beforeSkip).filter(([path])=>path.startsWith('/api/calls-process'));
 assert.equal(skipRequests.length,2);
 assert.equal(skipRequests[1][1].reprocess,false);
 assert.deepEqual(skipRequests[1][1].ids,['finished','new']);
 assert.equal(skipRequests[1][1].run_at,'2026-11-11T13:00');
 repeatChoice=true;
 processingResponses=[{needs_choice:true,ids:['finished','new'],processed:1}, {ok:true,message:'created',job:{id:'repeat'}}];
 await context.startProc({mode:'all'});
 assert.equal(calls.filter(([path])=>path.startsWith('/api/calls-process')).at(-1)[1].reprocess,true);
 repeatChoice=null;
 processingResponses=[{needs_choice:true,ids:['finished'],processed:1}];
 const beforeCancel=calls.filter(([path])=>path.startsWith('/api/calls-process')).length;
 assert.equal(await context.startProc({ids:['finished']}),false);
 assert.equal(calls.filter(([path])=>path.startsWith('/api/calls-process')).length,beforeCancel+1);
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
