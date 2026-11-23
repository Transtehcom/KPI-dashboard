import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createDashboardWorker } from "../worker/adapter.mjs";
import dashboard from "../dist/server/index.js";

const dbMigration = readFileSync(new URL("../drizzle/0000_browser_sandboxes.sql",import.meta.url),"utf8");
// Use real SQLite transactions for the D1 subset exercised by the Worker.
class TestD1 {
  constructor() {
    this.sql = new DatabaseSync(":memory:");
    this.sql.exec("PRAGMA foreign_keys = ON");
    this.sql.exec(dbMigration);
  }
  withSession(consistency) { assert.equal(consistency,"first-primary"); return this; }
  prepare(sql) {
    const command = this.sql.prepare(sql);
    return {bind:(...args)=>({
      all:async()=>({results:command.all(...args)}), run:()=>({meta:command.run(...args)})
    })};
  }
  async batch(statements) {
    this.sql.exec("BEGIN");
    try { const rows=statements.map(s=>s.run()); this.sql.exec("COMMIT"); return rows; }
    catch(e) { this.sql.exec("ROLLBACK"); throw e; }
  }
}
const fixedTime = "2026-11-23T06:00:00Z";
const sessionPath = "/api/sandbox/session";
const actionPath = "/api/sandbox/action";
function client(worker, db, owner="tester") {
  return { cookie:"", async call(path, payload) {
    const headers = {"oai-authenticated-user-id":owner};
    if (this.cookie) headers.Cookie=this.cookie;
    if (payload!==undefined) headers["Content-Type"]="application/json";
    const req = new Request("https://example.test"+path,{method:payload===undefined?"GET":"POST",headers,
      body:payload===undefined?undefined:JSON.stringify(payload)});
    const res=await worker.fetch(req,{DB:db});
    if(res.headers.has("Set-Cookie")) this.cookie=res.headers.get("Set-Cookie").split(";")[0];
    return res;
  } };
}
async function ok(response) { assert.equal(response.status,200);return response.json(); }

test("Rust on the Worker agrees with the source Wasm for all 10 sections and employee access",async()=>{
  const db=new TestD1(); const http=client(dashboard,db);
  const module=await WebAssembly.instantiate(readFileSync(new URL("../dist/dashboard.wasm",import.meta.url)),{});
  const {reserve,output,memory,server_run}=module.instance.exports;
  let revision=0, saved=null;
  const cases = [[sessionPath,{route:"home",time:fixedTime}],
      ...["kpi","funnel","funnel-fact","calls","gpt","workload","metrics-sales","metrics-supply","metrics-logistics",
          "money","carriers","suppliers","requests","purchases","staff"].map(value=>[actionPath,{key:"nav",value}]),
      [actionPath,{key:"session",value:"supply"}],[actionPath,{key:"nav",value:"money"}]];
  for (const [path,data] of cases) {
    const body=path===actionPath?{...data,revision}:data;
    const bytes=new TextEncoder().encode(JSON.stringify({target:path,method:"POST",body,revision,saved}));
    const ptr=reserve(bytes.length);new Uint8Array(memory.buffer,ptr,bytes.length).set(bytes);
    const len=server_run(), reference=JSON.parse(new TextDecoder().decode(new Uint8Array(memory.buffer,output(),len)));
    const res=await http.call(path,body), actual=await ok(res);
    assert.deepEqual(actual,JSON.parse(reference.body),path+" "+(data.value || "home"));
    assert.ok(!("saved" in actual));
    saved=reference.saved; revision=actual.revision;
  }
  assert.equal(JSON.parse(saved).route,"home");
  assert.match(http.cookie,/^kpi_session=[0-9a-f]{64}$/);
  db.sql.close();
});

test("reopens saved state, isolates visitors and rejects concurrent stale updates",async()=>{
  const db=new TestD1(), first=client(dashboard,db), second=client(dashboard,db), stranger=client(dashboard,db,"stranger");
  let r=await ok(await first.call(sessionPath,{route:"home",time:fixedTime}));
  assert.equal(r.theme,"light");
  const a=()=>first.call(actionPath,{key:"theme",value:"",revision:r.revision});
  const [a1,a2]=await Promise.all([a(),a()]);
  assert.deepEqual([a1.status,a2.status].sort(),[200,409]);
  const restored=await ok(await first.call(sessionPath,{route:"money",time:fixedTime}));
  assert.equal(restored.theme,"dark");assert.equal(restored.revision,3);
  const other=await ok(await second.call(sessionPath,{route:"home",time:fixedTime}));
  assert.equal(other.theme,"light");assert.notEqual(first.cookie,second.cookie);
  stranger.cookie=first.cookie;
  assert.equal((await stranger.call(actionPath,{key:"theme",value:"",revision:3})).status,401);
  await ok(await stranger.call(sessionPath,{route:"home",time:fixedTime}));
  assert.notEqual(stranger.cookie,first.cookie);
  const noCookie=client(dashboard,db);
  const denial=await noCookie.call("/api/supplier-directory");
  assert.equal(denial.status,401); assert.equal(denial.headers.get("Cache-Control"),"no-store");
  assert.equal(denial.headers.get("Vary"),"Accept-Encoding");
  assert.equal((await dashboard.fetch(new Request("https://example.test/api/carriers"),{DB:db})).status,401);
  db.sql.close();
});

test("bounded D1 chunks round-trip large Unicode state across a revision and clean up old chunks",async()=>{
  const db=new TestD1(), huge="a".repeat(159999)+"🚚"+"я".repeat(800000);
  const adapter=createDashboardWorker(({saved,body,revision})=>{
    if (revision) assert.equal(saved,huge);
    return {status:200,content_type:"application/json",body:JSON.stringify({revision:revision+1}),saved:body.next};
  },{});
  const http=client(adapter,db);
  await ok(await http.call(sessionPath,{next:huge}));
  assert.ok(db.sql.prepare("SELECT count(*) n FROM dashboard_chunks").get().n>1);
  await ok(await http.call(actionPath,{revision:1,next:"small"}));
  assert.equal(db.sql.prepare("SELECT count(*) n FROM dashboard_chunks").get().n,1);
  db.sql.close();
});

test("static client assets allow revalidation and do not leak private modules",async()=>{
  for(const path of ["/","/assets/ttk.svg","/app.css","/browser.js"]) {
    const url="https://example.test"+path;
    const res=await dashboard.fetch(new Request(url),{});
    assert.equal(res.status,200);assert.ok((await res.arrayBuffer()).byteLength>0);
    assert.equal((await dashboard.fetch(new Request(url,{headers:{"If-None-Match":res.headers.get("ETag")}}),{})).status,304);
  }
  assert.equal((await dashboard.fetch(new Request("https://example.test/server/index.js"),{})).status,404);
});
