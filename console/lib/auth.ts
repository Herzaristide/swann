import "server-only";
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { nextCookies } from "better-auth/next-js";
import { genericOAuth, keycloak } from "better-auth/plugins/generic-oauth";
import { headers } from "next/headers";
import { cache } from "react";
import { db, schema } from "@/lib/db";
import { discoveryBase, env } from "@/lib/env";

const e = env();

/**
 * Better Auth : sessions de la console (cookie httpOnly, base `console`,
 * schéma `auth`), connexion par Keycloak (OIDC, PKCE, client `swann-console`).
 * Les jetons Keycloak restent côté serveur ; la Corpus API vérifie elle-même
 * les rôles corpus:admin / corpus:review à chaque appel.
 */
export const auth = betterAuth({
  baseURL: e.BETTER_AUTH_URL,
  secret: e.BETTER_AUTH_SECRET,
  database: drizzleAdapter(db, {
    provider: "pg",
    schema: {
      user: schema.user,
      session: schema.session,
      account: schema.account,
      verification: schema.verification,
    },
  }),
  emailAndPassword: { enabled: false },
  user: {
    additionalFields: {
      sub: { type: "string", required: true },
    },
  },
  disabledPaths: [
    "/update-user",
    "/change-email",
    "/change-password",
    "/set-password",
    "/delete-user",
    "/link-social",
    "/unlink-account",
  ],
  session: {
    // Outil d'administration : sessions courtes
    expiresIn: 60 * 60 * 12,
    updateAge: 60 * 60,
    cookieCache: { enabled: true, maxAge: 5 * 60 },
  },
  account: {
    accountLinking: { enabled: false },
  },
  plugins: [
    genericOAuth({
      config: [
        {
          ...keycloak({
            issuer: e.OIDC_ISSUER,
            clientId: e.CONSOLE_CLIENT_ID,
            clientSecret: e.CONSOLE_CLIENT_SECRET,
            scopes: ["openid", "profile", "email", "offline_access"],
            pkce: true,
            postLogoutRedirectURI: "/login",
          }),
          discoveryUrl: `${discoveryBase()}/.well-known/openid-configuration`,
          mapProfileToUser: (profile) => ({
            sub: String(profile.sub),
            name:
              (profile.name as string | undefined) ||
              (profile.preferred_username as string | undefined) ||
              String(profile.email ?? "Admin"),
          }),
        },
      ],
    }),
    nextCookies(),
  ],
});

export type Session = typeof auth.$Infer.Session;

/** Session courante (une seule lecture par requête). */
export const getSession = cache(async (): Promise<Session | null> => {
  return auth.api.getSession({ headers: await headers() });
});
