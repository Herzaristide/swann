/**
 * Synchronisation avec le corpus : lit le flux /v1/changes de la Corpus API
 * depuis le dernier curseur (table `sync_cursors`) et
 * - `edition.text_replaced` : recale les progressions des lecteurs dans la
 *   nouvelle révision (anchors:resolve, lots de 500) ;
 * - `work.updated` : rafraîchit l'aperçu de l'œuvre dans `work_cache`.
 *
 *   pnpm worker            boucle (toutes les WORKER_INTERVAL secondes, 60 par défaut)
 *   pnpm worker --once     une passe puis s'arrête (cron)
 *
 * Processus à part de Next.js : n'importe ni `server-only` ni lib/db/index.ts.
 */
import { and, eq, sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/postgres-js";
import createClient from "openapi-fetch";
import postgres from "postgres";
import type { paths } from "../lib/api/schema";
import * as schema from "../lib/db/schema";

const env = {
  db: process.env.READER_DATABASE_URL ?? "postgresql://swann:swann@localhost:5432/reader",
  api: process.env.CORPUS_API_URL ?? "http://localhost:8000",
  realm: (
    process.env.OIDC_DISCOVERY_URL ??
    process.env.OIDC_ISSUER ??
    "http://localhost:8080/realms/swann"
  ).replace(/\/$/, ""),
  clientId: process.env.READER_CLIENT_ID ?? "swann-reader",
  clientSecret: process.env.READER_CLIENT_SECRET ?? "dev-reader-secret",
  interval: Number(process.env.WORKER_INTERVAL ?? 60),
};

const CURSOR = "corpus-changes";
const BATCH = 500;

const client = postgres(env.db, { max: 2, onnotice: () => {} });
const db = drizzle(client, { schema, casing: "snake_case" });
const { readingProgress: rp, workCache, syncCursors } = schema;

let token: { value: string; exp: number } | null = null;

async function serviceToken(): Promise<string> {
  if (token && token.exp > Date.now() + 30_000) return token.value;
  const res = await fetch(`${env.realm}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: env.clientId,
      client_secret: env.clientSecret,
    }),
  });
  if (!res.ok) throw new Error(`Keycloak : HTTP ${res.status}`);
  const body = (await res.json()) as { access_token: string; expires_in: number };
  token = { value: body.access_token, exp: Date.now() + body.expires_in * 1000 };
  return token.value;
}

const api = createClient<paths>({ baseUrl: env.api });
api.use({
  async onRequest({ request }) {
    request.headers.set("authorization", `Bearer ${await serviceToken()}`);
    return request;
  },
});

function log(...args: unknown[]) {
  console.log(new Date().toISOString(), ...args);
}

/** Recale les progressions d'une édition dont le texte a été remplacé. */
async function relocate(editionId: string, oldRevision: number) {
  const rows = await db
    .select()
    .from(rp)
    .where(and(eq(rp.editionId, editionId), eq(rp.revision, oldRevision)));
  let moved = 0;
  for (let i = 0; i < rows.length; i += BATCH) {
    const batch = rows.slice(i, i + BATCH);
    const { data, response } = await api.POST("/v1/editions/{edition_id}/anchors:resolve", {
      params: { path: { edition_id: editionId } },
      body: {
        anchors: batch.map((r) => ({
          key: r.userSub,
          revision: r.revision,
          seq: r.seq,
          offset: r.offset,
          quote: r.quote,
        })),
      },
    });
    if (!data) {
      // Édition devenue invisible pour la liseuse (droits) : la page la recalera à l'ouverture
      log(`anchors:resolve ${editionId} : HTTP ${response.status}, ignoré`);
      return;
    }
    for (const r of data.results) {
      if (r.seq === null) continue;
      // `updated_at` inchangé : une position plus récente venue d'un appareil gagne toujours
      await db
        .update(rp)
        .set({ revision: data.revision, seq: r.seq, offset: r.offset ?? 0 })
        .where(
          and(eq(rp.userSub, r.key), eq(rp.editionId, editionId), eq(rp.revision, oldRevision)),
        );
      moved++;
    }
  }
  if (rows.length) log(`édition ${editionId} : ${moved}/${rows.length} progression(s) recalée(s)`);
}

/** Rafraîchit l'aperçu d'une œuvre déjà en cache. */
async function refreshWork(workId: string) {
  const [cached] = await db
    .select({ id: workCache.workId })
    .from(workCache)
    .where(eq(workCache.workId, workId));
  if (!cached) return;
  const { data, response } = await api.GET("/v1/works/{work_id}", {
    params: { path: { work_id: workId }, query: { lang: "fr" } },
  });
  if (!data) {
    if (response.status === 404) await db.delete(workCache).where(eq(workCache.workId, workId));
    return;
  }
  await db
    .update(workCache)
    .set({
      data: {
        title: data.title,
        original_title: data.original_title,
        first_published_year: data.first_published_year,
        authors: data.authors.map((a) => ({ id: a.id, name: a.name })),
        movements: data.movements.map((m) => ({ id: m.id, slug: m.slug, label: m.label })),
        languages: data.languages,
      },
      fetchedAt: new Date(),
    })
    .where(eq(workCache.workId, workId));
}

/** Une passe : lit le flux jusqu'au bout. Renvoie le nombre d'événements traités. */
export async function syncOnce(): Promise<number> {
  const [row] = await db.select().from(syncCursors).where(eq(syncCursors.name, CURSOR));
  let cursor = row?.cursor ?? null;
  let total = 0;
  for (;;) {
    const { data, response } = await api.GET("/v1/changes", {
      params: {
        query: { after: cursor, limit: 200, type: ["edition.text_replaced", "work.updated"] },
      },
    });
    if (!data) throw new Error(`/v1/changes : HTTP ${response.status}`);
    const refreshed = new Set<string>();
    for (const c of data.items) {
      if (c.type === "edition.text_replaced" && c.edition_id) {
        await relocate(c.edition_id, Number(c.data.old_revision));
      } else if (c.type === "work.updated" && c.work_id && !refreshed.has(c.work_id)) {
        refreshed.add(c.work_id);
        await refreshWork(c.work_id);
      }
      cursor = c.cursor;
      total++;
    }
    if (cursor !== null && data.items.length > 0) {
      await db
        .insert(syncCursors)
        .values({ name: CURSOR, cursor, updatedAt: new Date() })
        .onConflictDoUpdate({ target: syncCursors.name, set: { cursor, updatedAt: sql`now()` } });
    }
    if (data.items.length < 200) break;
  }
  return total;
}

async function main() {
  const once = process.argv.includes("--once");
  do {
    try {
      const n = await syncOnce();
      if (n > 0 || once) log(`${n} changement(s) traité(s)`);
    } catch (err) {
      log("échec de la synchronisation :", err instanceof Error ? err.message : err);
      if (once) process.exitCode = 1;
    }
    if (!once) await new Promise((r) => setTimeout(r, env.interval * 1000));
  } while (!once);
  await client.end();
}

// Utilisé aussi par les tests (import de syncOnce) : ne démarre que lancé directement
if (process.argv[1]?.endsWith("worker/index.ts") || process.argv[1]?.endsWith("worker.mjs")) {
  void main();
}
