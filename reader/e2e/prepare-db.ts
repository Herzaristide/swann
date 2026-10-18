/** Recrée la base jetable `reader_e2e` des tests de bout en bout (lancé avant le serveur). */
import postgres from "postgres";

async function main() {
  const admin = postgres("postgresql://swann:swann@localhost:5432/postgres", {
    max: 1,
    onnotice: () => {},
  });
  await admin.unsafe("DROP DATABASE IF EXISTS reader_e2e WITH (FORCE)");
  await admin.unsafe("CREATE DATABASE reader_e2e");
  await admin.end();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
