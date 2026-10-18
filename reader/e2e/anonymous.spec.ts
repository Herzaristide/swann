import { expect, test } from "@playwright/test";

test("un visiteur voit l'accueil et peut se connecter", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Bienvenue sur Swann" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Se connecter" }).first()).toBeVisible();
});

test("les pages personnelles demandent une connexion", async ({ page }) => {
  await page.goto("/library");
  await page.waitForURL(/\/realms\/swann\/protocol\/openid-connect\/auth/);
});

test("le proxy refuse les routes hors liste blanche", async ({ request }) => {
  expect((await request.get("/api/corpus/v1/changes")).status()).toBe(404);
  expect((await request.post("/api/corpus/v1/alignment/links", { data: {} })).status()).toBe(404);
  expect((await request.get("/api/me/favorites")).status()).toBe(401);
});

test("le manifeste et le service worker sont servis", async ({ request }) => {
  const manifest = await (await request.get("/manifest.webmanifest")).json();
  expect(manifest.display).toBe("standalone");
  const sw = await request.get("/serwist/sw.js");
  expect(sw.ok()).toBe(true);
  expect(sw.headers()["service-worker-allowed"]).toBe("/");
});
