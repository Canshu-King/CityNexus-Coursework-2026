import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0072 -- TRUTH_DEFECT: when the destination stopped being recognised, the wizard kept
 * planning on the *previous* city's country / currency / timezone and called that the fallback.
 *
 * Reproduction is by sequence, and that is the whole point of this file.  Cold, a broken city
 * looks fine: the fresh draft already holds the neutral basis, so `facts === null` and nothing
 * stale is on screen.  Only "recognise a city, then break the name" leaves JP / 日元（JPY）/
 * Asia/Tokyo in the hidden triple while the sentence says the trip falls back.  That is why a
 * source-level review passed it and a real walk-through caught it.
 *
 * The fix is one rule in two entry points -- every city keystroke (`applyCity`) and a draft
 * restored from storage (`withNeutralBasis`) -- and one sentence computed in the same pass that
 * quotes it.  Gate A below kills the reset; the wire gate kills a wording-only change.
 */

const FILE = "e2e/qoder-wizard-city-basis.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

async function openFreshWizard(page: Page, locale: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await openScreen(page, "/v8", ["trial-setup"]);
}

const CASES = {
  "zh-CN": {
    recognised: "东京",
    broken: "拉萨市不存在的写法",
    arrival: "拉萨贡嘎机场",
    departure: "拉萨火车站",
    /* 已退回中立口径 CN / 人民币（CNY）/ Asia/Shanghai 规划 */
    basis: /中立口径\s+(\S+?)\s*\/\s*(.+?)（(\w+)）\s*\/\s*(\S+?)\s*规划/,
    saysFallback: /中立口径/,
    previousCityTokens: ["日本", "日元", "东京", "JPY", "Asia/Tokyo"],
  },
  en: {
    recognised: "Tokyo",
    broken: "Zzznotacity",
    arrival: "airport",
    departure: "station",
    /* falls back to the neutral basis CN / Chinese yuan (CNY) / Asia/Shanghai, */
    basis: /neutral basis\s+(\S+?)\s*\/\s*(.+?)\s*\((\w+)\)\s*\/\s*(\S+?),/,
    saysFallback: /neutral basis/,
    previousCityTokens: ["Japan", "Japanese yen", "Tokyo", "JPY", "Asia/Tokyo"],
  },
} as const;

const tripleOf = (text: string, pattern: RegExp) => {
  const hit = text.match(pattern);
  return hit ? `${hit[1]}|${hit[3]}|${hit[4]}` : "";
};

async function warningText(page: Page) {
  return (await page.getByTestId("trial-city-warning").innerText()).replace(/\s+/g, " ").trim();
}

test.describe("ORD-0072 an unrecognised destination falls back, it does not inherit", () => {
  for (const [locale, c] of Object.entries(CASES)) {
    test(`stale-city-basis-is-not-called-the-default [${locale}] — 门 A, by sequence`, async ({
      page,
    }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 390, height: 844 });
      await openFreshWizard(page, locale);

      // Reference reading: a broken city typed cold.  Nothing stale exists yet, so this is the
      // product's real neutral basis -- measured, not asserted from a literal this lane invented.
      await page.getByTestId("trial-city").fill(c.broken);
      const coldText = await warningText(page);
      const coldBasis = tripleOf(coldText, c.basis);
      expect(
        coldBasis,
        node("stale-city-basis-is-not-called-the-default",
          `the cold caution does not name a basis at all: ${JSON.stringify(coldText)}`),
      ).not.toBe("");

      // Now recognise a city, so the hidden triple genuinely holds Japan's values...
      await page.getByTestId("trial-city").fill(c.recognised);
      await expect(
        page.getByTestId("trial-inferred"),
        node("stale-city-basis-is-not-called-the-default",
          "the city was never recognised in the first place, so this run cannot demonstrate a leftover"),
      ).toBeVisible();

      // ...then break the name again, which is the user's actual hand sequence.
      await page.getByTestId("trial-city").fill(c.broken);
      const brokenText = await warningText(page);
      const brokenBasis = tripleOf(brokenText, c.basis);

      expect(
        brokenBasis,
        node("stale-city-basis-is-not-called-the-default",
          `after a recognised city the caution stopped naming a basis: ${JSON.stringify(brokenText)}`),
      ).not.toBe("");
      expect(
        brokenBasis,
        node("stale-city-basis-is-not-called-the-default",
          `the caution quotes ${brokenBasis} after ${JSON.stringify(c.recognised)}, while a cold start quotes ${coldBasis} — the sentence is the previous city's leftovers, not a fallback`),
      ).toBe(coldBasis);
      expect(
        brokenText,
        node("stale-city-basis-is-not-called-the-default",
          `the sentence does not say it is a fallback: ${JSON.stringify(brokenText)}`),
      ).toMatch(c.saysFallback);
      for (const token of c.previousCityTokens) {
        expect(
          brokenText.includes(token),
          node("stale-city-basis-is-not-called-the-default",
            `the caution still names the previous city's ${token}: ${JSON.stringify(brokenText)}`),
        ).toBe(false);
      }
      expect(
        brokenText.includes(c.recognised) || brokenText.toLowerCase().includes(c.recognised.toLowerCase()),
        node("stale-city-basis-is-not-called-the-default",
          `the caution invented the previous city's name: ${JSON.stringify(brokenText)}`),
      ).toBe(false);

      writeFileSync(
        evidencePath(`ui-overnight/city-basis-${locale}.json`),
        JSON.stringify(
          {
            generatedBy: "e345b9d6",
            order: "ORD-0072",
            node: "stale-city-basis-is-not-called-the-default",
            locale,
            sequence: [c.broken, c.recognised, c.broken],
            coldBasis,
            brokenBasis,
            brokenText,
          },
          null,
          2,
        ) + "\n",
        "utf-8",
      );
    });

    test(`previous-city-auto-set-line-does-not-survive [${locale}] — 门 C`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 390, height: 844 });
      await openFreshWizard(page, locale);
      await page.getByTestId("trial-city").fill(c.broken);
      await page.getByTestId("trial-city").fill(c.recognised);
      await expect(page.getByTestId("trial-inferred")).toBeVisible();
      await page.getByTestId("trial-city").fill(c.broken);

      await expect(
        page.getByTestId("trial-inferred"),
        node("previous-city-auto-set-line-does-not-survive",
          "「已自动设置 / Auto-set」 is still on screen for a city that was not recognised"),
      ).toHaveCount(0);

      // Nothing anywhere on the step may quote the previous city's basis.
      const body = (await page.getByTestId("trial-step1-body").innerText()).replace(/\s+/g, " ");
      for (const token of c.previousCityTokens) {
        expect(
          body.includes(token),
          node("previous-city-auto-set-line-does-not-survive",
            `${token} from the previous city is still printed on step 1: ${JSON.stringify(body.slice(0, 320))}`),
        ).toBe(false);
      }
      // And the anchors stopped naming a city the table no longer knows.
      expect(
        body,
        node("previous-city-auto-set-line-does-not-survive",
          `the labels kept the previous city's name: ${JSON.stringify(body.slice(0, 320))}`),
      ).toMatch(/抵达地点|离开地点|Arrive at|Depart from/);
    });
  }

  test("submitted-basis-matches-what-the-screen-said — 门 A 的深度：words == wire", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    const c = CASES["zh-CN"];
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-city").fill(c.broken);
    const said = tripleOf(await warningText(page), c.basis);
    const [country, currency, timezone] = said.split("|");
    expect(
      country && currency && timezone,
      node("submitted-basis-matches-what-the-screen-said", `no basis was readable on screen: ${said}`),
    ).toBeTruthy();

    await page.getByTestId("trial-city").fill(c.recognised);
    await page.getByTestId("trial-city").fill(c.broken);
    const after = tripleOf(await warningText(page), c.basis);
    expect(
      after,
      node("submitted-basis-matches-what-the-screen-said",
        `the basis changed between two reads of the same broken city: ${after} vs ${said}`),
    ).toBe(said);

    await page.getByTestId("trial-arrival").fill(c.arrival);
    await page.getByTestId("trial-departure").fill(c.departure);
    await page.getByTestId("trial-next").click();
    await page.getByTestId("trial-next").click();
    await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 15_000 });

    /* Stop at the wire: this node is about what the wizard sends, not about the backend's
       answer.  travellers is mocked so the journeys POST fires; journeys is aborted so nothing
       is written to the shared database. */
    await page.route("**/api/v1/travelers", (route) =>
      route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ id: "gate-traveler-0000" }),
      }),
    );
    await page.route("**/api/v1/journeys", (route) => void route.abort());
    const [request] = await Promise.all([
      page.waitForRequest(
        (req) => req.url().includes("/api/v1/journeys") && req.method() === "POST",
        { timeout: 30_000 },
      ),
      page.getByTestId("trial-create").click(),
    ]);
    const sent = request.postDataJSON() as {
      city_code: string;
      timezone: string;
      budget: { currency: string };
    };
    expect(
      `${sent.city_code.split("-")[0]}|${sent.budget.currency}|${sent.timezone}`,
      node("submitted-basis-matches-what-the-screen-said",
        `the screen promised ${said} but the trip is created on ${sent.city_code}/${sent.budget.currency}/${sent.timezone}`),
    ).toBe(said);

    writeFileSync(
      evidencePath("ui-overnight/city-basis-wire.json"),
      JSON.stringify(
        {
          generatedBy: "e345b9d6",
          order: "ORD-0072",
          node: "submitted-basis-matches-what-the-screen-said",
          said,
          sent: { city_code: sent.city_code, currency: sent.budget.currency, timezone: sent.timezone },
        },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });
});
