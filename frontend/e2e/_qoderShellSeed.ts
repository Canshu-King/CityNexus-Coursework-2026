import { expect, type Page } from "@playwright/test";

/**
 * Seed a real, fully-populated journey the way the product itself does, so the
 * Today (/v8/hub) and Live (/v8/live) surfaces render against genuine backend
 * API responses. The setup screen's "Tianjin demo" button creates the traveler
 * + journey and generates stay options, persisting `citynexus.v8.journey_id`.
 *
 * Shared by the Qoder frontend gates (dark-mode audit, mobile overflow) so they
 * audit the same reachable surfaces without a fragile per-spec fixture.
 * Idempotent: if a journey is already loaded it returns immediately.
 */
export async function ensureDemoJourney(page: Page): Promise<void> {
  await page.goto("/v8", { waitUntil: "load" });
  await page.waitForTimeout(600);
  const setup = page.getByTestId("trial-setup");
  if (await setup.isVisible().catch(() => false)) {
    await page.getByTestId("trial-tianjin-demo").click();
    await page
      .waitForFunction(() => !!localStorage.getItem("citynexus.v8.journey_id"), null, {
        timeout: 30_000,
      });
    await page.waitForTimeout(1000);
  }
}

/** Land on a route inside the V8 shell and wait for its anchor surface. */
export async function openScreen(
  page: Page,
  url: string,
  readyTestIds: string[],
): Promise<void> {
  await page.goto(url, { waitUntil: "load" });
  for (const id of readyTestIds) {
    await expect(page.getByTestId(id).first()).toBeVisible({ timeout: 25_000 });
  }
  await page.waitForTimeout(1200);
}
