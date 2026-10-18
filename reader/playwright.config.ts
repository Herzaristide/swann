import { defineConfig, devices } from "@playwright/test";

/**
 * Tests de bout en bout sur un build de production (service worker actif),
 * avec une base `reader_e2e` jetable : jamais la vraie base `reader`.
 * Prérequis : Postgres, Keycloak et la Corpus API démarrés ; `pnpm build`.
 * Le client Keycloak n'accepte que http://localhost:3000 : port 3000 libre.
 */
const E2E_DB = "postgresql://swann:swann@localhost:5432/reader_e2e";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3000",
    locale: "fr-FR",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], storageState: "e2e/.auth/lecteur.json" },
      dependencies: ["setup"],
      testIgnore: /(anonymous|mobile)\.spec\.ts/,
    },
    { name: "anonymous", use: { ...devices["Desktop Chrome"] }, testMatch: /anonymous\.spec\.ts/ },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], storageState: "e2e/.auth/lecteur.json" },
      dependencies: ["setup"],
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: {
    command: "tsx e2e/prepare-db.ts && pnpm db:migrate && pnpm start:standalone",
    url: "http://localhost:3000/manifest.webmanifest",
    reuseExistingServer: false,
    timeout: 60_000,
    env: { READER_DATABASE_URL: E2E_DB, PORT: "3000" },
  },
});
