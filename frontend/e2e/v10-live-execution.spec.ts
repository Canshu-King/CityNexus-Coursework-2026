import { expect, test } from "@playwright/test";
import { createFreshJourney, useJourney } from "./_qoderFreshJourney";

test("V10 live execution panel exposes the server clock and evidence trail", async ({
  page,
  request,
}) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  /* pass-12 A: this was the last spec of its family still riding the shared
     global seed, which is why it alone failed on "Choose your next leg." — the
     shell bounced it back to the trial wizard. It now owns a journey created
     through public product APIs, the pattern v10-accessibility and
     v10-quality-budget already proved. Every execution-clock and evidence-trail
     assertion below is unchanged. */
  const { journeyId: ownedJourney } = await createFreshJourney(request);
  await useJourney(page, ownedJourney);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/live");
  await expect(page.getByRole("heading", { name: "Choose your next leg." })).toBeVisible();

  const panel = page.getByTestId("v10-live-execution-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("CLOCK", { exact: true })).toBeVisible();
  /* The panel's status line is i18n'd under exec.readOnlyReady, whose English
     value is "available offline" — the spec was still asserting the wording that
     key replaced. */
    await expect(panel.getByText("available offline", { exact: true })).toBeVisible();

  const journeyId = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
  expect(journeyId).toBeTruthy();
  const clockResponse = await request.get(
    `/api/v1/journeys/${journeyId}/execution-clock`,
  );
  expect(clockResponse.ok()).toBeTruthy();
  const clock = await clockResponse.json();
  expect(clock.source).toBe("EXECUTION_CLOCK");
  expect(clock.timezone).toBe("Asia/Shanghai");

  await panel.locator("summary").click();
  await expect(panel.getByText(/No web observation is authoritative/)).toBeVisible();
});
