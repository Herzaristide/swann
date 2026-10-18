import "server-only";
import { and, eq } from "drizzle-orm";
import { auth } from "@/lib/auth";
import { db, schema } from "@/lib/db";
import { discoveryBase, env } from "@/lib/env";

/**
 * Jeton présenté à la Corpus API :
 * - lecteur connecté → son jeton d'accès Keycloak (rafraîchi par Better Auth) ;
 * - sinon → jeton du compte de service `swann-reader` (client_credentials).
 */
export async function corpusToken(reqHeaders: Headers): Promise<string> {
  const user = await userToken(reqHeaders);
  return user ?? serviceToken();
}

async function userToken(reqHeaders: Headers): Promise<string | null> {
  try {
    const session = await auth.api.getSession({ headers: reqHeaders });
    if (!session) return null;
    const [account] = await db
      .select({ id: schema.account.id })
      .from(schema.account)
      .where(
        and(eq(schema.account.userId, session.user.id), eq(schema.account.providerId, "keycloak")),
      );
    if (!account) return null;
    // Rafraîchit le jeton Keycloak s'il a expiré
    const res = await auth.api.getAccessToken({
      body: { accountId: account.id },
      headers: reqHeaders,
    });
    return res?.accessToken ?? null;
  } catch {
    // Jeton de rafraîchissement expiré ou révoqué : on lit comme un visiteur
    return null;
  }
}

let service: { token: string; expiresAt: number } | null = null;
let pending: Promise<string> | null = null;

export async function serviceToken(): Promise<string> {
  if (service && service.expiresAt > Date.now() + 30_000) return service.token;
  pending ??= fetchServiceToken().finally(() => {
    pending = null;
  });
  return pending;
}

async function fetchServiceToken(): Promise<string> {
  const e = env();
  const res = await fetch(`${discoveryBase()}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: e.READER_CLIENT_ID,
      client_secret: e.READER_CLIENT_SECRET,
    }),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Keycloak client_credentials : HTTP ${res.status}`);
  const body = (await res.json()) as { access_token: string; expires_in: number };
  service = { token: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 };
  return body.access_token;
}
