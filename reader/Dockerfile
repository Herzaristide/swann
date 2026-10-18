# Liseuse Swann (Next.js, sortie `standalone`).
# Contexte de build = dossier reader/ :
#   docker compose --profile reader up -d reader
FROM node:22-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1 \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile

FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Variables lues au premier accès seulement : valeurs factices le temps du build
RUN BETTER_AUTH_SECRET=build-only-build-only-build-only-0000 READER_CLIENT_SECRET=build \
    pnpm build \
 && pnpm exec esbuild lib/db/migrate.ts worker/index.ts --bundle --platform=node --format=esm \
      --target=node22 --outdir=dist --out-extension:.js=.mjs \
      --banner:js="import{createRequire}from'module';const require=createRequire(import.meta.url);"

FROM base AS runner
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/drizzle ./drizzle
COPY --from=build --chown=nextjs:nodejs /app/dist/lib/db/migrate.mjs ./migrate.mjs
COPY --from=build --chown=nextjs:nodejs /app/dist/worker/index.mjs ./worker.mjs
USER nextjs
EXPOSE 3000
# Migrations Drizzle de la base `reader`, puis le serveur
CMD ["sh", "-c", "node migrate.mjs && exec node server.js"]
