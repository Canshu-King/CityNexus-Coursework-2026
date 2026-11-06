import { expect, test } from "@playwright/test";
import { createFreshJourney, useJourney } from "./_qoderFreshJourney";

test.describe("V10 product-quality budget", () => {
  test("Live Journey becomes usable without layout or server-error regressions", async ({
    page,
    request,
  }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const { journeyId } = await createFreshJourney(request);
  await useJourney(page, journeyId);

    await page.setViewportSize({ width: 390, height: 844 });
    const consoleErrors: string[] = [];
    const serverErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 500) {
        serverErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    const started = Date.now();
    await page.goto("/v8/live", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Choose your next leg." })).toBeVisible({
      timeout: 4000,
    });

    expect(Date.now() - started).toBeLessThan(4000);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth + 1),
    );
    expect(serverErrors).toEqual([]);
    expect(consoleErrors).toEqual([]);
  });
});
