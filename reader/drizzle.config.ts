import { defineConfig } from "drizzle-kit";

export default defineConfig({
  dialect: "postgresql",
  schema: "./lib/db/schema.ts",
  out: "./drizzle",
  casing: "snake_case",
  dbCredentials: {
    url: process.env.READER_DATABASE_URL ?? "postgresql://swann:swann@localhost:5432/reader",
  },
});
