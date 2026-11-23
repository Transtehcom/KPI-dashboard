import { index, integer, primaryKey, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const sandboxes = sqliteTable("dashboard_sandboxes", {
  id: text().primaryKey(),
  owner: text().notNull(),
  revision: integer().notNull(),
  head: text().notNull(),
  expires: integer().notNull()
}, t => [index("dashboard_sandbox_expiry").on(t.expires)]);

// A saved help request may contain several images. Chunk the serialized Rust
// state so an individual D1 row or parameter always fits its size limit.
export const chunks = sqliteTable("dashboard_chunks", {
  sandbox: text().notNull().references(() => sandboxes.id, { onDelete: "cascade" }),
  head: text().notNull(),
  part: integer().notNull(),
  payload: text().notNull()
}, t => [primaryKey({ columns: [t.sandbox, t.head, t.part] })]);
