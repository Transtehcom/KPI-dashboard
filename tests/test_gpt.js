const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('web/index.html','utf8');
const start=html.indexOf('  function gptMessagesHtml('),end=html.indexOf('  async function loadGPT()',start);
const ctx={esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),dtLabel:String};
vm.runInNewContext(html.slice(start,end),ctx);
const output=ctx.gptMessagesHtml([{id:1,role:'assistant',status:'completed',content:'<script>bad</script>',created:'2026-11-05',has_preview:true,sources:[{id:'source_1',title:'Анна <bad>',url:'#emp-anna',system:'Дашборд',month:'2026-10'}]}]);
assert.ok(!output.includes('<script>'));
assert.match(output,/data-gpt-preview="1"/);
assert.match(output,/data-gpt-month="2026-10"/);
assert.match(output,/Что отправлено в Codex/);
assert.match(ctx.gptMessagesHtml([]),/Спросите/);
assert.ok(html.includes('state.gptEpoch === epoch'),'late chat requests must be ignored');
assert.ok(html.includes('state.gptDrafts[chatId]'),'polling keeps typed drafts');
console.log('GPT messages, safe rendering, sources, preview and navigation guards: OK');

const files=ctx.gptDocumentsHtml([{id:'doc1',name:'<script>.docx',size:1000,editable:true,parent_id:'original'}]);
assert.ok(!files.includes('<script>'));
assert.match(files,/data-gpt-download="doc1"/);
assert.match(files,/data-gpt-delete="doc1"/);
assert.match(ctx.gptDocumentsHtml([{id:'doc1',name:'file.txt',size:1}],true),/data-gpt-delete="doc1"[^>]*disabled/);
assert.ok(html.includes('/api/gpt-document-delete'));
assert.ok(html.includes('id="gptFiles"'));
assert.ok(html.includes('/api/gpt-upload'));
assert.ok(html.includes('link.download = file.name'));
const grouped = ctx.gptChatListHtml([{id:'1',title:'<draft>',group_name:'Продажи'},{id:'2',title:'Работа',group_name:''}], '1');
assert.match(grouped,/Продажи/);
assert.match(grouped,/Без группы/);
assert.ok(!grouped.includes('<draft>'));
assert.match(grouped,/nav-item active/);
const history = ctx.gptDocumentsHtml([], false, [{name:'<removed>.txt',created:'2026-11-06',deleted:'2026-11-07'}]);
assert.match(history,/1 удалено/);
assert.ok(!history.includes('<removed>'));
for (const action of ['rename','group','delete','rollback','clear_documents']) assert.ok(html.includes(`manage("${action}"`));
const imageMarkup = ctx.gptDocumentsHtml([{id:'img',name:'image.png',is_image:true,protected:true,protection_forced:true,size:100,mime:'image/png',width:40,height:30}]);
assert.match(imageMarkup,/data-gpt-thumbnail="img"/);
assert.match(imageMarkup,/data-gpt-protected="img"[^>]*checked[^>]*disabled/);
assert.match(imageMarkup,/Содержит документ/);
const clipboardImage = {name:'clipboard.png',type:'image/png'};
assert.deepEqual(Array.from(ctx.gptTransferFiles({files:[clipboardImage],items:[{kind:'file',getAsFile:()=>clipboardImage}]})),[clipboardImage]);
assert.deepEqual(Array.from(ctx.gptTransferFiles({files:[],items:[{kind:'file',getAsFile:()=>clipboardImage}]})),[clipboardImage]);
assert.equal(ctx.gptTransferFiles({files:[],items:[{kind:'string'}]}).length,0);

// Exercise real picker, paste and drop event wiring through the chat entry point.
async function attachmentEvents() {
  const nodes = {};
  const makeNode = () => ({value:'',innerHTML:'',textContent:'',disabled:false,hidden:false,handlers:{},
    scrollHeight:0,scrollTop:0,clientHeight:0,selectionStart:0,selectionEnd:0,
    addEventListener(name, callback) { this.handlers[name] = callback; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    classList:{add(){},remove(){}},contains(){return false;},
    setRangeText(value) { this.value += value; }});
  for (const id of ['main','gptMessages','gptText','gptFast','gptModel','gptEffort','gptSend','gptFiles','gptDocuments','gptRollback','gptStop','gptRename','gptGroup','gptDeleteChat','gptUploadStatus','gptError','gptForm','gptMonth','gptNew']) nodes[id] = makeNode();
  nodes.gptModel.value = 'model';
  const uploads = [], documents = [], revoked = [];
  const context = {...ctx, state:{view:'gpt',gptDrafts:{},month:'2026-11'},
    document:{getElementById:id=>nodes[id],querySelectorAll:()=>[]},
    URL:{revokeObjectURL:url=>revoked.push(url)}, clearTimeout(){},setTimeout(){return 0;},
    Uint8Array, btoa:value=>Buffer.from(value,'binary').toString('base64'), encodeURIComponent,
    loadError:error=>{throw error;},
    api:async (path, body)=>{
      if (path === '/api/gpt-chats') return {chats:[{id:'chat',title:'test'}],models:{model:'Model'},efforts:{medium:'Medium'},model_efforts:{model:{medium:'Medium'}}};
      if (path.startsWith('/api/gpt-chat?')) return {messages:[],documents,document_history:[]};
      if (path === '/api/gpt-upload') { uploads.push(body); return {}; }
      throw Error('Unexpected API '+path);
    }};
  const loadStart = html.indexOf('  async function loadGPT()');
  const loadEnd = html.indexOf('  // ---------- оценки разбора звонков',loadStart);
  vm.runInNewContext(html.slice(start,loadEnd),context);
  await context.loadGPT();
  const file = {name:'image.png',size:3,arrayBuffer:async()=>Uint8Array.from([1,2,3]).buffer};
  await nodes.gptFiles.handlers.change({target:{files:[file]}});
  assert.equal(uploads.length,1);
  assert.equal(uploads[0].name,'image.png');
  assert.equal(uploads[0].content,'AQID');
  let prevented = false;
  nodes.gptText.handlers.paste({clipboardData:{files:[],items:[{kind:'string'}]},preventDefault(){prevented = true;}});
  assert.equal(prevented,false,'plain text keeps browser paste behavior');
  nodes.gptText.handlers.paste({clipboardData:{files:[file],getData:()=> 'Question'},preventDefault(){prevented = true;}});
  await new Promise(setImmediate);
  assert.equal(prevented,true);
  assert.equal(nodes.gptText.value,'Question');
  assert.equal(uploads.length,2);
  prevented = false;
  nodes.gptForm.handlers.drop({dataTransfer:{files:[file]},preventDefault(){prevented = true;}});
  await new Promise(setImmediate);
  assert.equal(prevented,true);
  assert.equal(uploads.length,3);
  nodes.gptFiles.disabled = true;
  nodes.gptForm.handlers.drop({dataTransfer:{files:[file]},preventDefault(){}});
  await new Promise(setImmediate);
  assert.equal(uploads.length,3,'busy chats cannot ingest dropped files');
  context.state.gptPreviewUrls = {image:'blob:image'};
  context.gptReleasePreviews();
  assert.deepEqual(revoked,['blob:image']);
}
attachmentEvents().catch(error=>{ console.error(error);process.exitCode=1; });
