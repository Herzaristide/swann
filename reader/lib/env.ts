import "server-only";
import { z } from "zod";

/** Variables d'environnement du serveur, validées au premier accès. */
const schema = z.object({
  BETTER_AUTH_URL: z.url().default("http://localhost:3000"),
  BETTER_AUTH_SECRET: z.string().min(32),
  READER_DATABASE_URL: z.string().default("postgresql://swann:swann@localhost:5432/reader"),
  // Émetteur public des jetons (iss) et URL du realm joignable depuis ce serveur
  OIDC_ISSUER: z.url().default("http://localhost:8080/realms/swann"),
  OIDC_DISCOVERY_URL: z.url().optional(),
  READER_CLIENT_ID: z.string().default("swann-reader"),
  READER_CLIENT_SECRET: z.string().min(1),
  CORPUS_API_URL: z.url().default("http://localhost:8000"),
});

export type Env = z.infer<typeof schema>;

let cached: Env | undefined;

export function env(): Env {
  cached ??= schema.parse(process.env);
  return cached;
}

/** URL du realm où lire la configuration OpenID (réseau interne en conteneur). */
export function discoveryBase(): string {
  const e = env();
  return (e.OIDC_DISCOVERY_URL ?? e.OIDC_ISSUER).replace(/\/$/, "");
}
