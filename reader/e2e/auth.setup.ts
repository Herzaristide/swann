import { expect, test as setup } from "@playwright/test";

/** Connexion Keycloak de l'utilisateur de développement `lecteur`, gardée pour les autres tests. */
setup("connexion", async ({ page }) => {
  await page.goto("/login");
  await page.locator("#username").fill("lecteur");
  await page.locator("#password").fill(process.env.KEYCLOAK_DEV_USER_PASSWORD ?? "lecteur");
  await page.locator("#kc-login").click();
  await page.waitForURL("http://localhost:3000/**");
  await expect(page.getByText("Lecteur Swann").first()).toBeVisible();
  await page.context().storageState({ path: "e2e/.auth/lecteur.json" });
});
