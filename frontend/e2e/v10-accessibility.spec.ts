import { expect, test } from "@playwright/test";
import { createFreshJourney, useJourney } from "./_qoderFreshJourney";
import AxeBuilder from "@axe-core/playwright";

test("V10 live execution surface has no automated accessibility violations", async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const { journeyId } = await createFreshJourney(request);
  await useJourney(page, journeyId);

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/live");
  await expect(page.getByRole("heading", { name: "Choose your next leg." })).toBeVisible();
  await page.waitForTimeout(150);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});
