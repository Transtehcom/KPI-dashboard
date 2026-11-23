const lifetime = 30 * 24 * 60 * 60;
const cookieName = "kpi_session";
const maxRequestBytes = 3_000_000;
const maxStateBytes = 4_000_000;
const partSize = 160_000;
const utf8 = new TextEncoder();

function token() {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), n => n.toString(16).padStart(2, "0")).join("");
}

function readId(request) {
  const fields = request.headers.get("Cookie")?.split(";") || [];
  for (const field of fields) {
    const match = /^kpi_session=([0-9a-f]{64})$/.exec(field.trim());
    if (match) return match[1];
  }
  return null;
}

function problem(status, error) {
  return Response.json({error}, {status, headers: {"Cache-Control": "no-store", Vary: "Accept-Encoding"}});
}

async function readState(db, owner, id, now) {
  if (!id) return null;
  const {results} = await db.prepare(`
    SELECT s.revision, c.payload FROM dashboard_sandboxes s
    JOIN dashboard_chunks c ON c.sandbox = s.id AND c.head = s.head
    WHERE s.id = ? AND s.owner = ? AND s.expires > ? ORDER BY c.part
  `).bind(id, owner, now).all();
  if (!results.length) return null;
  return {revision: results[0].revision, saved: results.map(row => row.payload).join("")};
}

function parts(state) {
  const chunks = [];
  for (let start = 0; start < state.length;) {
    let end = Math.min(start + partSize, state.length);
    // D1 encodes each parameter on its own. A surrogate pair must stay together.
    if (end < state.length && /[\uD800-\uDBFF]/.test(state[end - 1])) end--;
    chunks.push(state.slice(start, end));
    start = end;
  }
  return chunks;
}

async function saveState(db, owner, id, snapshot, value, now) {
  const head = token();
  const expires = now + lifetime;
  const insert = db.prepare(`INSERT INTO dashboard_sandboxes (id, owner, revision, head, expires)
    VALUES (?, ?, 1, ?, ?) ON CONFLICT (id) DO NOTHING`);
  const update = db.prepare(`UPDATE dashboard_sandboxes SET revision = revision + 1, head = ?, expires = ?
    WHERE id = ? AND owner = ? AND revision = ? AND expires > ?`);
  const statements = [snapshot
    ? update.bind(head, expires, id, owner, snapshot.revision, now)
    : insert.bind(id, owner, head, expires)];
  // All writes belong to one D1 transaction. Each subordinate write checks
  // this transaction's unique head, so losing an optimistic race is a no-op.
  const chunk = db.prepare(`INSERT INTO dashboard_chunks (sandbox, head, part, payload)
    SELECT id, head, ?, ? FROM dashboard_sandboxes WHERE id = ? AND owner = ? AND head = ?`);
  for (const [index, payload] of parts(value).entries()) {
    statements.push(chunk.bind(index, payload, id, owner, head));
  }
  statements.push(db.prepare(`DELETE FROM dashboard_chunks WHERE sandbox = ? AND head <> ?
    AND EXISTS (SELECT 1 FROM dashboard_sandboxes WHERE id = ? AND owner = ? AND head = ?)`)
    .bind(id, head, id, owner, head));
  if (!snapshot) {
    statements.push(db.prepare(`DELETE FROM dashboard_sandboxes
      WHERE id IN (SELECT id FROM dashboard_sandboxes WHERE expires <= ? LIMIT 8)`).bind(now));
  }
  const results = await db.batch(statements);
  return results[0].meta.changes === 1;
}

function bytes(base64) {
  return Uint8Array.from(atob(base64), c => c.charCodeAt(0));
}

function staticResponse(asset, request) {
  const headers = {"Content-Type": asset.type, "Cache-Control": "private, no-cache", ETag: asset.etag,
    "X-Content-Type-Options": "nosniff"};
  if (request.headers.get("If-None-Match") === asset.etag) return new Response(null, {status: 304, headers});
  return new Response(request.method === "HEAD" ? null : bytes(asset.data), {headers});
}

// Only platform I/O resides here. Rust owns API payloads, authorization within
// the sandbox, rendering, calculations, state transitions and exports.
export function createDashboardWorker(invoke, assets) {
  return { async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      const path = url.pathname === "/" ? "/index.html" : url.pathname;
      const asset = Object.hasOwn(assets, path) ? assets[path] : null;
      if (!asset) return new Response("Not found", {status: 404});
      if (request.method !== "GET" && request.method !== "HEAD") return new Response(null, {status:405, headers:{Allow:"GET, HEAD"}});
      return staticResponse(asset, request);
    }
    // Private Sites inject this header after authentication. A cookie is a
    // random selector scoped to the verified visitor, never a user identity.
    const owner = request.headers.get("oai-authenticated-user-id");
    if (!owner) return problem(401, "Требуется вход в приватную копию Sites.");
    if (!env.DB) return problem(503, "Хранилище тестовой копии недоступно.");
    let payload = null;
    if (request.method === "POST") {
      const origin = request.headers.get("Origin");
      if (origin && origin !== url.origin) return problem(403, "Недопустимый источник запроса.");
      if (request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json")
        return problem(415, "Ожидается application/json.");
      if (Number(request.headers.get("Content-Length")) > maxRequestBytes) return problem(413, "Слишком большой запрос.");
      const reader = request.body?.getReader();
      if (!reader) return problem(400, "Пустой запрос.");
      const chunks = [];
      let length = 0;
      for (;;) {
        const item = await reader.read();
        if (item.done) break;
        length += item.value.byteLength;
        if (length > maxRequestBytes) { await reader.cancel(); return problem(413, "Слишком большой запрос."); }
        chunks.push(item.value);
      }
      const raw = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
      try { payload = JSON.parse(new TextDecoder().decode(raw)); } catch { return problem(400, "Некорректный JSON."); }
    } else if (request.method !== "GET") {
      return problem(405, "Метод не поддерживается.");
    }
    try {
      const db = env.DB.withSession ? env.DB.withSession("first-primary") : env.DB;
      const now = Math.floor(Date.now() / 1000);
      let id = readId(request);
      const snapshot = await readState(db, owner, id, now);
      const start = url.pathname === "/api/sandbox/session" && request.method === "POST";
      if (!snapshot) id = start ? token() : null;
      const result = invoke({saved: snapshot?.saved ?? null, method: request.method,
        target: url.pathname + url.search, body: payload, revision: snapshot?.revision ?? 0});
      const headers = new Headers({"Content-Type": result.content_type, "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"});
      if (result.content_type.startsWith("application/json")) headers.set("Vary", "Accept-Encoding");
      if (result.saved != null) {
        if (!id || typeof result.saved !== "string") return problem(500, "Некорректная операция сохранения.");
        if (utf8.encode(result.saved).length > maxStateBytes) return problem(413, "Хранилище песочницы заполнено.");
        if (!await saveState(db, owner, id, snapshot, result.saved, now))
          return problem(409, "Данные изменились в другой вкладке. Обновите представление.");
        headers.set("Set-Cookie", `${cookieName}=${id}; Path=/; Max-Age=${lifetime}; HttpOnly; Secure; SameSite=Lax`);
      }
      if (result.filename) {
        const disposition = /^[A-Za-z0-9._-]+$/.test(result.filename)
          ? `attachment; filename="${result.filename}"`
          : `attachment; filename*=UTF-8''${encodeURIComponent(result.filename)}`;
        headers.set("Content-Disposition", disposition);
      }
      const body = result.encoding === "base64" ? bytes(result.body) : result.body;
      return new Response(body, {status: result.status, headers});
    } catch {
      return problem(503, "Не удалось обработать запрос к тестовому серверу.");
    }
  }};
}

