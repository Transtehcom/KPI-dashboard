import { defineConfig } from "drizzle-kit";

export default defineConfig({
  out: "./drizzle",
  schema: "./worker/schema.ts",
  dialect: "sqlite"
});
