/** Applique les migrations de drizzle/ sur la base `console` (au démarrage du conteneur). */
import { drizzle } from "drizzle-orm/postgres-js";
import { migrate } from "drizzle-orm/postgres-js/migrator";
import postgres from "postgres";

async function main() {
  const url = process.env.CONSOLE_DATABASE_URL ?? "postgresql://swann:swann@localhost:5432/console";
  const client = postgres(url, { max: 1, onnotice: () => {} });
  await migrate(drizzle(client), { migrationsFolder: "drizzle", migrationsSchema: "drizzle" });
  await client.end();
  console.log("base console : migrations appliquées");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
