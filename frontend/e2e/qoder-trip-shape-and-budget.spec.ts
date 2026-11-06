import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0063 and ORD-0064(a), frontend half.
 *
 * Two sentences the traveller used to read that the product had invented:
 *   - "3 天" came from `Math.round((end - start) / 86_400_000) + 1`, so a 08:00→20:00 same-day
 *     trip was two days, and the budget was then divided by the second day that never existed;
 *   - "每人每天约 ¥200" printed `¥` regardless of the journey's currency, and `¥` is also yen.
 *
 * Both are now the backend's to say (`journey.temporal_shape`, `trip.budget_reference`), and
 * what the backend does not say must read as unknown rather than as a plausible number.  The
 * payloads below are the real `/experience` response with only those two fields replaced, so
 * nothing here depends on a fixture the product never serves.
 */

const FILE = "e2e/qoder-trip-shape-and-budget.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const SHAPE = (dayCount: number | null) => ({
  day_count: dayCount,
  night_count: dayCount === null ? null : Math.max(0, dayCount - 1),
  semantics: "CALENDAR_DAYS_INCLUSIVE",
  window_status: dayCount === null ? "UNORDERABLE" : "OK",
  min_days: 1,
  max_days: 7,
});

const REFERENCE = (over: Record<string, unknown> = {}) => ({
  available: true,
  amount_minor: 60000,
  currency: "JPY",
  party_size: 2,
  day_count: 1,
  per_day_minor: 60000,
  per_person_per_day_minor: 30000,
  floor_minor: 15000,
  floor_currency: "CNY",
  floor_comparable: false,
  reason: null,
  ...over,
});

/** Answer the journey's real /experience with only the projected fields rewritten. */
async function patchExperience(page: Page, patch: (body: any) => void) {
  await page.route("**/api/v1/journeys/*/experience", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    let body: any;
    let status = 0;
    try {
      const upstream = await route.fetch();
      status = upstream.status();
      body = await upstream.json();
    } catch {
      return route.fallback();
    }
    if (status !== 200 || !body?.journey) {
      // A stale seeded journey answers 404 with an error envelope. Falling through would
      // let the gate pass against a body that never had the fields it claims to read.
      throw new Error(
        node("gate precondition",
          `/experience answered ${status} without a journey payload, so the projection could `
          + "not be patched. The gate is not measuring the product: " + JSON.stringify(body).slice(0, 200)),
      );
    }
    patch(body);
    try {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    } catch {
      /* the page moved on; nothing left to answer */
    }
  });
}

/**
 * A journey this run really created.
 *
 * The chromium project replays a storageState holding `citynexus.v8.journey_id`
 * (playwright.config.ts); in a long run that row may belong to a database this run never
 * had, and every surface below would then be judged against a 404.
 */
async function openHubWithJourney(page: Page, readyTestIds: string[]) {
  /* Wipe once per context. An unguarded init script runs again on the navigation into the
     hub and deletes the journey the demo button had just written, which is how the first
     version of this helper ended up staring at the wizard. */
  await page.addInitScript(() => {
    if (sessionStorage.getItem("cnx.qoder.shape-seeded")) return;
    sessionStorage.setItem("cnx.qoder.shape-seeded", "1");
    localStorage.removeItem("citynexus.v8.journey_id");
  });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", readyTestIds);
}

test.describe("ORD-0063/0064a Today states the projected shape and currency", () => {
  test("trip-length-comes-from-the-backend", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    // A twelve-hour same-day trip: the removed arithmetic called this "2 天".
    await patchExperience(page, (body) => {
      body.journey.temporal_shape = SHAPE(1);
      body.trip.budget_reference = REFERENCE();
    });
    await openHubWithJourney(page, ["today-heading", "today-when"]);
    const when = page.getByTestId("today-when");
    const oneDay = (await when.innerText()).replace(/\s+/g, " ").trim();
    expect(
      oneDay,
      node("trip-length-comes-from-the-backend", `the projected single day was not what Today said: ${JSON.stringify(oneDay)}`),
    ).toMatch(/1 天|1 days/);
    expect(
      /2 天|2 days/.test(oneDay),
      node("trip-length-comes-from-the-backend", `Today still invented a second day: ${JSON.stringify(oneDay)}`),
    ).toBe(false);

    // And when the window cannot be ordered, Today must say unknown, not fall back to 1.
    await page.unroute("**/api/v1/journeys/*/experience");
    await patchExperience(page, (body) => {
      body.journey.temporal_shape = SHAPE(null);
      body.trip.budget_reference = { ...REFERENCE(), available: false, reason: "WINDOW_UNORDERABLE" };
    });
    await openHubWithJourney(page, ["today-heading", "today-when"]);
    const unknown = (await page.getByTestId("today-when").innerText()).replace(/\s+/g, " ").trim();
    expect(
      unknown,
      node("trip-length-comes-from-the-backend",
        `an unprojectable window was presented as a fact instead of as unknown: ${JSON.stringify(unknown)}`),
    ).toMatch(/天数未知|length unknown/);
    expect(
      /1 天|1 days/.test(unknown),
      node("trip-length-comes-from-the-backend", `unknown still rendered as one day: ${JSON.stringify(unknown)}`),
    ).toBe(false);
    writeFileSync(
      evidencePath("ui-overnight/trip-shape.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0063", node: "trip-length-comes-from-the-backend", oneDay, unknown }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("budget-line-never-borrows-a-symbol", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    const seen: Record<string, string> = {};
    for (const arm of [
      // 66.66 units per person per day is under the floor as a raw number.  The floor is
      // quoted in CNY and this journey is priced in yen, so `floor_comparable` is false and
      // comparing them at all would hand the traveller a verdict reached in the wrong money.
      { currency: "JPY", comparable: false, dayCount: 1, perPerson: 6666, wantTight: "false" },
      // Same amount, same floor, and this time the backend says the money is comparable, so
      // suppressing the warning would be its own untruth.  Only `floor_comparable` differs.
      { currency: "CNY", comparable: true, dayCount: 3, perPerson: 6666, wantTight: "true" },
    ] as const) {
      await page.unroute("**/api/v1/journeys/*/experience");
      await patchExperience(page, (body) => {
        body.journey.temporal_shape = SHAPE(arm.dayCount);
        body.trip.budget_reference = REFERENCE({
          currency: arm.currency,
          day_count: arm.dayCount,
          per_day_minor: arm.perPerson * 2,
          per_person_per_day_minor: arm.perPerson,
          floor_comparable: arm.comparable,
        });
      });
      /* ORD-0212 (user, 15:00): the hub keeps only the `tight` verdict.  A non-tight reference
         no longer renders the advisory at all, so the two arms now assert different things:
         JPY (incomparable, non-tight) -> the block is absent from the hub, and CNY (comparable,
         tight) -> the block is present with its verdict.  The full text+hint pair still lives on
         the budget-review surface. */
      if (arm.wantTight === "true") {
        await openHubWithJourney(page, ["today-heading", "budget-advisory"]);
        const advisory = page.getByTestId("budget-advisory");
        const text = (await advisory.innerText()).replace(/\s+/g, " ").trim();
        seen[arm.currency] = text;
        expect(
          text.includes("¥"),
          node("budget-line-never-borrows-a-symbol",
            `${arm.currency}: the advisory prints a symbol again, and ¥ is ambiguous between CNY and JPY: ${JSON.stringify(text)}`),
        ).toBe(false);
        expect(
          text,
          node("budget-line-never-borrows-a-symbol", `${arm.currency}: the ISO code the money is in never reaches the line`),
        ).toContain(arm.currency);
        expect(
          await advisory.getAttribute("data-tight"),
          node("budget-line-never-borrows-a-symbol",
            `${arm.currency}: floor_comparable=${arm.comparable} but the screen chose ${JSON.stringify(
              await advisory.getAttribute("data-tight"),
            )}, expected ${arm.wantTight}`),
        ).toBe(arm.wantTight);
      } else {
        await openHubWithJourney(page, ["today-heading"]);
        await page.waitForTimeout(1_500);
        const advisoryCount = await page.getByTestId("budget-advisory").count();
        const hubText = (await page.locator("body").innerText()).replace(/\s+/g, " ");
        seen[arm.currency] = `budget-advisory count=${advisoryCount}`;
        expect(
          advisoryCount,
          node("budget-line-never-borrows-a-symbol",
            `${arm.currency}: the hub still renders the averaged-reference advisory that ORD-0212 removed (count=${advisoryCount})`),
        ).toBe(0);
        expect(
          /每人每天约/.test(hubText),
          node("budget-line-never-borrows-a-symbol",
            `${arm.currency}: the derived per-person number still appears on the hub without its tier: "${hubText.slice(0, 200)}"`),
        ).toBe(false);
      }
    }
    writeFileSync(
      evidencePath("ui-overnight/budget-reference.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0064", node: "budget-line-never-borrows-a-symbol", seen }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("no-client-side-day-or-currency-invention", async () => {
    // The arithmetic leaving the file is the fix; a gate that only checks today's rendered
    // sentence would still pass after someone re-added the same division for another string.
    const path = new URL("../src/app/V8Experience.tsx", import.meta.url);
    expect(existsSync(path), node("no-client-side-day-or-currency-invention", "the source under guard is not where it was expected")).toBe(true);
    const source = readFileSync(path, "utf8");
    const offenders = [
      /86_?400_?000/,
      /Math\.round\([^)]*\/\s*24\)/,
      /BUDGET_DAILY_FLOOR_MINOR/,
      /每人每天约\s*¥/,
      /about\s+¥\$\{/,
    ].filter((pattern) => pattern.test(source));
    expect(
      offenders.map(String),
      node("no-client-side-day-or-currency-invention",
        `Today computes days or picks a currency symbol again: ${JSON.stringify(offenders.map(String))}`),
    ).toEqual([]);
  });
});
