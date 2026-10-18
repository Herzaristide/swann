// Lancement : SHOTS=/tmp/captures SAMPLE_EPUB=../books/<auteur>/<oeuvre>/<langue>.epub node e2e/smoke.mjs
// (console sur http://localhost:3001, utilisateur Keycloak de développement `lecteur`).
// Parcours de contrôle de la console (hors suite de tests) : connexion, pages, erreurs.
import { chromium } from "@playwright/test";

const BASE = "http://localhost:3001";
const OUT = process.env.SHOTS;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
page.on("pageerror", (e) => problems.push(`[pageerror] ${page.url()} ${e.message}`));
page.on(
  "console",
  (m) => m.type() === "error" && problems.push(`[console] ${page.url()} ${m.text()}`),
);
page.on("response", (r) => {
  if (r.url().includes("/api/corpus/") && r.status() >= 400)
    problems.push(`[api ${r.status()}] ${r.url()}`);
});

await page.goto(`${BASE}/`);
await page.waitForURL(/realms\/swann/);
await page.fill("#username", "lecteur");
await page.fill("#password", "lecteur");
await page.click("#kc-login");
await page.waitForURL(`${BASE}/`);

const pages = [
  "/",
  "/jobs",
  "/upload",
  "/quality",
  "/indexes",
  "/events",
  "/works",
  "/persons",
  "/movements",
  "/trash",
  "/alignment",
  "/alignment/review",
];
for (const p of pages) {
  await page.goto(`${BASE}${p}`);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(600);
  await page.screenshot({
    path: `${OUT}/${p === "/" ? "overview" : p.slice(1).replaceAll("/", "-")}.png`,
    fullPage: true,
  });
}
// Pages de détail : première œuvre, sa première édition (structure), première édition alignée, première tâche
await page.goto(`${BASE}/works`);
await page.locator("table a").first().click();
await page.waitForLoadState("networkidle");
await page.screenshot({ path: `${OUT}/work.png`, fullPage: true });
const editionIdMatch = await page.evaluate(async () => {
  const id = location.pathname.split("/").pop();
  const r = await fetch(`/api/corpus/v1/admin/works/${id}`);
  return (await r.json()).editions[0].id;
});
await page.goto(`${BASE}/editions/${editionIdMatch}/structure`);
await page.waitForLoadState("networkidle");
await page.screenshot({ path: `${OUT}/structure.png`, fullPage: true });
await page.goto(`${BASE}/alignment`);
await page.waitForLoadState("networkidle");
const firstAligned = page.locator("table a").first();
if (await firstAligned.count()) {
  await firstAligned.click();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/workbench.png`, fullPage: false });
}
await page.goto(`${BASE}/jobs`);
await page.waitForLoadState("networkidle");
const firstJob = page.locator("table a").first();
if (await firstJob.count()) {
  await firstJob.click();
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${OUT}/job.png`, fullPage: true });
}

// Dépôt d'un EPUB déjà en base : signalé comme doublon, rien n'est écrit
const extra = await browser.newPage({ viewport: { width: 1280, height: 900 } });
extra.on("pageerror", (e) => problems.push(`[pageerror] ${e.message}`));
await extra.context().addCookies(await page.context().cookies());
await extra.goto(`${BASE}/upload`);
const epub = process.env.SAMPLE_EPUB;
if (epub) {
  await extra.locator('input[type="file"]').setInputFiles(epub);
  await extra.getByText("déjà en base").waitFor({ timeout: 20000 });
  await extra.screenshot({ path: `${OUT}/upload-duplicate.png` });
  console.log("dépôt d'un doublon : signalé");
}
await extra.goto(`${BASE}/`);
await extra
  .getByRole("status")
  .filter({ hasText: "Temps réel actif" })
  .first()
  .waitFor({ state: "attached", timeout: 15000 });
console.log("temps réel : actif");
console.log(problems.length ? problems.join("\n") : "aucune erreur");
await browser.close();
