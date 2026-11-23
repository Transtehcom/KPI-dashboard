// The same ESM artifact and SQL migrations that Sites will receive, running
// inside workerd with an isolated, ephemeral D1 instance.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Miniflare } from "miniflare";

const runtime = new Miniflare({
  modules: true, scriptPath: fileURLToPath(new URL("../dist/server/index.js", import.meta.url)),
  compatibilityDate: "2026-06-01", d1Databases: ["DB"], cf: false,
  host: "127.0.0.1", port: 0, inspectorPort: 0
});
try {
  const db = await runtime.getD1Database("DB");
  const migration=readFileSync(new URL("../drizzle/0000_browser_sandboxes.sql",import.meta.url),"utf8");
  for (const statement of migration.split("--> statement-breakpoint")) await db.prepare(statement.trim()).run();
  const base="https://worker.test";
  const page=await runtime.dispatchFetch(base+"/");
  assert.equal(page.status,200);
  assert.match(await page.text(),/Транстехком/);
  const post=(path,data,cookie)=>runtime.dispatchFetch(base+path,{method:"POST",headers:{
    Origin:base,"Content-Type":"application/json","oai-authenticated-user-id":"test-owner",...(cookie?{Cookie:cookie}:{})
  },body:JSON.stringify(data)});
  const fresh=await post("/api/sandbox/session",{route:"home",time:"2026-11-23T06:00:00Z"});
  assert.equal(fresh.status,200);
  const cookie=fresh.headers.get("Set-Cookie").split(";")[0];
  const home=await fresh.json();
  assert.equal(home.route,"home");assert.equal(home.revision,1);assert.ok(!("saved" in home));
  const [one,two]=await Promise.all(["staff","carriers"].map(value=>post("/api/sandbox/action",{revision:1,key:"nav",value},cookie)));
  assert.deepEqual([one.status,two.status].sort(),[200,409]);
  const headers={Cookie:cookie,"oai-authenticated-user-id":"test-owner"};
  const read=await runtime.dispatchFetch(base+"/api/supplier-directory",{headers});
  assert.equal(read.status,200);assert.equal(read.headers.get("Cache-Control"),"no-store");
  assert.equal(read.headers.get("Vary"),"Accept-Encoding");
  const download=await runtime.dispatchFetch(base+"/api/carriers.xlsx",{headers});
  assert.equal(download.status,200);assert.equal(download.headers.get("Content-Disposition"),'attachment; filename="carriers.xlsx"');
  assert.equal(new TextDecoder().decode((await download.arrayBuffer()).slice(0,2)),"PK");
  const stranger=await runtime.dispatchFetch(base+"/api/supplier-directory",{headers:{...headers,"oai-authenticated-user-id":"test-other"}});
  assert.equal(stranger.status,401);
  console.log("workerd/D1: homepage, session, persistent state, CAS conflict, JSON, XLSX, owner isolation passed.");
} finally { await runtime.dispose(); }

