import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0036 (d) -- the wizard keeps what you typed.
 *
 * The user's complaint was "没有保存记录". Half of that was the service worker serving a
 * dead shell; the other half was real: the creation wizard held its step and every typed
 * field in component state only, so any reload threw the trip away and started again from
 * an empty city box.
 *
 * The decision recorded in the component is localStorage rather than the URL -- a
 * half-filled trip is not a link worth sharing, and the dates are local wall-clock strings.
 * This gate asserts the traveller-visible consequence: reload, and you are back on the step
 * you were on, with the city you typed still in the review.
 */

const FILE = "e2e/qoder-trial-state.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const CITY = "厦门";

async function openFreshWizard(page: Page) {
  /* Wipe once per tab.  Clearing after boot does not reproduce a first visit -- the journey
     also lives in IndexedDB, so the app skips the wizard and `trial-setup` never appears --
     and an unguarded init script would run again on this test's own reload and delete the
     draft the assertion is about to check. */
  await page.addInitScript(() => {
    if (sessionStorage.getItem("cnx.qoder.trial-cleared")) return;
    sessionStorage.setItem("cnx.qoder.trial-cleared", "1");
    try {
      localStorage.clear();
    } catch {
      /* nothing to clear before the first commit */
    }
  });
  await openScreen(page, "/v8", ["trial-setup"]);
}

async function stepState(page: Page) {
  return page.evaluate(() =>
    [1, 2, 3].map((n) => {
      const row = document.querySelector(`[data-testid="trial-step-${n}"]`);
      return { n, state: row?.getAttribute("class") ?? null };
    }),
  );
}

test.describe("ORD-0036 (d) the trial wizard survives a reload", () => {
  test("trial-state-survives-reload — step and typed draft both come back", async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page);

    await page.getByTestId("trial-city").fill(CITY);
    await page.getByTestId("trial-arrival").fill("厦门高崎国际机场");
    await page.getByTestId("trial-departure").fill("厦门站");
    await page.getByTestId("trial-next").click();
    await page.getByTestId("trial-next").click();
    await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 15_000 });
    const before = await stepState(page);

    await page.reload({ waitUntil: "networkidle" });
    await openScreen(page, "/v8", ["trial-setup"]);
    const after = await stepState(page);
    const review = await page.locator('[data-testid="trial-step-3-body"], .v8-page').first().innerText();
    const stored = await page.evaluate(() => localStorage.getItem("citynexus.v8.trial_draft"));

    writeFileSync(
      evidencePath("ui-overnight/trial-state.json"),
      JSON.stringify(
        { generatedBy: "e345b9d6", order: "ORD-0036", node: "trial-state-survives-reload", before, after, stored },
        null,
        2,
      ) + "\n",
      "utf-8",
    );

    expect(
      after,
      node("trial-state-survives-reload",
        `a reload did not return the traveller to the step they were on (before ${JSON.stringify(before)}, `
        + `after ${JSON.stringify(after)}). Restarting the wizard throws away what they typed`),
    ).toEqual(before);
    expect(
      review.includes(CITY),
      node("trial-state-survives-reload",
        `the review page came back without the city that was typed (${JSON.stringify(CITY)}); `
        + `stored draft was ${JSON.stringify(stored)}`),
    ).toBe(true);
  });
});
