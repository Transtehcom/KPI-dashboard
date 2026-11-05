const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const start = html.indexOf('  function entryBlock(');
const end = html.indexOf('\n  // штрафы', start);
const context = {state:{boot:{me:{role:'manager'}}},MONTHS:[],nf:String,esc:v=>String(v??'')};
vm.createContext(context);
vm.runInContext(html.slice(html.indexOf('  function fmtCell('),html.indexOf('  // ---------- API')),context);
vm.runInContext(html.slice(start,end),context);
const fields=[{key:'planned_date',type:'date',title:'Плановая дата поставки',readonly:true},
  {key:'customer_actual_date',type:'date',title:'Фактическая дата поставки',parent_edit:true},
  {key:'delivery_delta',type:'delivery_delta',title:'Просрочка, дн.',readonly:true},
  {key:'defect',type:'bool',title:'Брак'},
  {key:'defect_fixed_date',type:'date',title:'Дата исправления брака',visible_if:'defect'}];
const rows=['675','676'].map(n=>({ref:'supplier-'+n,cells:[n,'Поставщик',100],
  values:{planned_date:'2026-11-01',customer_actual_date:'2026-11-02',delivery_delta:-1,defect:n==='675'?1:0,defect_fixed_date:null},manual:[]}));
const entry={table:'supplier_orders',columns:['Заказ клиента','Заказы поставщикам','Сумма'],fields,rows,
  groups:[{ref:'c148',edit_ref:'supplier-675',cells:['148','Заказов поставщикам: 2',200],
    values:rows[0].values,source_values:{customer_actual_date:'2026-11-03'},manual:['customer_actual_date'],rows}]};
const render=()=>context.entryBlock({login:'supply',month:'2026-11'},{entry});
let result=render();
assert.ok(result.includes('148'),'customer order must be visible as the parent');
assert.ok(!result.includes('<th>Дата</th>'),'document date must be absent from parent and child tables');
assert.ok(result.includes('delivery-delta late">-1 дн.'),'late delivery has a red signed delay');
assert.ok(!result.includes('data-field="planned_date"'),'deadline is read from the customer order');
entry.groups[0].values={...entry.groups[0].values,delivery_delta:2};
assert.ok(render().includes('delivery-delta on-time">+2 дн.'),'early delivery has a green signed margin');
entry.groups[0].values={...entry.groups[0].values,delivery_delta:0};
assert.ok(render().includes('delivery-delta on-time">0 дн.'),'same-day delivery has a green zero');
assert.equal((result.match(/data-delivery-toggle/g)||[]).length,1);
assert.ok(result.includes('aria-expanded="false"'));
assert.ok(result.includes('data-delivery-children hidden'));
assert.ok(result.includes('data-ref="supplier-675"'));
assert.ok(result.includes('data-ref="supplier-676"'));
assert.equal((result.match(/data-field="customer_actual_date"/g)||[]).length,1,'one actual date editor per customer');
assert.ok(result.includes('data-date-reset'),'manual date must be resettable to the source');
assert.ok(result.includes('03-11-2026'),'latest source date must display day first');
assert.ok(result.includes('01-11-2026'),'planned date must display day first');
assert.ok(result.includes('value="2026-11-02"'),'native date editor keeps its required machine format');
assert.equal(context.fmtCell('2026-11-12T09:10:00','',false),'12-11-2026 09:10:00');
assert.equal((result.match(/type="checkbox"/g)||[]).length,2,'defect checkboxes belong to supplier rows');
assert.equal((result.match(/data-field="defect_fixed_date"/g)||[]).length,1,'correction date shown only for a defective supplier');
context.state.deliveryOpen=new Set(['supply:2026-11:c148']);
result=render();
assert.ok(result.includes('aria-expanded="true"'));
assert.ok(!result.includes('data-delivery-children hidden'));
context.state.boot.me.role='employee';
assert.ok(!render().includes('data-entry'),'employees must not receive manual editors');
console.log('Customer delivery grouping and source date rendering: OK');
