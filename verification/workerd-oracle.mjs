// Used only by rust-anything app replay. Run from the recording lab produced
// by verification/http/verify.py. The hosted Worker contains none of this code.
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Miniflare } from "miniflare";

const runtime = new Miniflare({
  rootPath:fileURLToPath(new URL("../",import.meta.url)),
  modulesRoot:fileURLToPath(new URL("../dist/server/",import.meta.url)),
  modules:true, scriptPath:fileURLToPath(new URL("../dist/server/index.js",import.meta.url)),
  compatibilityDate:"2026-06-01", d1Databases:["DB"], cf:false,
  host:"127.0.0.1", port:0, inspectorPort:0
});
const db = await runtime.getD1Database("DB");
const migration=readFileSync(new URL("../drizzle/0000_browser_sandboxes.sql",import.meta.url),"utf8");
for(const statement of migration.split("--> statement-breakpoint")) await db.prepare(statement.trim()).run();
const owner="parity-fixture";
for (const letter of ["a","b","c"]) {
  const id=letter.repeat(64);
  const row=JSON.parse(readFileSync("store/"+id+".json","utf8"));
  await db.batch([
    db.prepare("INSERT INTO dashboard_sandboxes (id,owner,revision,head,expires) VALUES (?,?,?,?,?)")
      .bind(id,owner,row.revision,"initial",Math.floor(Date.now()/1000)+600),
    db.prepare("INSERT INTO dashboard_chunks (sandbox,head,part,payload) VALUES (?,?,0,?)")
      .bind(id,"initial",row.state)
  ]);
}
// Reproduce the private Sites gateway's authenticated identity header, leaving
// the dashboard's anonymous, denied and authorized cookie cases untouched.
const server=createServer(async (req,res)=>{
  try {
    const body=await Array.fromAsync(req);
    const headers=new Headers();
    for(let i=0;i<req.rawHeaders.length;i+=2) headers.append(req.rawHeaders[i],req.rawHeaders[i+1]);
    headers.set("oai-authenticated-user-id",owner);
    headers.delete("accept-encoding");headers.delete("host");headers.delete("content-length");
    const response=await runtime.dispatchFetch("http://dashboard.test"+req.url,{
      method:req.method,headers,...(body.length?{body:Buffer.concat(body)}:{})
    });
    res.writeHead(response.status,Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    res.writeHead(500);res.end("Oracle bridge failed");
  }
});
const port=Number(process.env.PORT);
if (!Number.isInteger(port) || port<=0) throw new Error("rust-anything must supply a valid PORT");
server.listen(port,"127.0.0.1");
for (const sig of ["SIGTERM","SIGINT"]) process.once(sig,async()=>{
  server.close();await runtime.dispose();process.exit(0);
});

