/**
 * CMDR 01:24 -- the 花费 tab read off the screen on the day24 generation, budget rung and actual rung.
 *
 * This is lane 6a150e1e reading another lane's landed face, so the judgement is deliberately the
 * narrow one: does the face say, in rendered text, the money this trip's two server answers actually
 * carry -- and does it keep 声明的预算 apart from what was priced. Every expected figure below is
 * computed inside the arm from `GET /journeys/{id}/experience` and `GET /journeys/{id}/wallet`, so a
 * wrong screen cannot be matched by a wrong expectation written next to it.
 *
 * The world is this lane's own fresh store, seeded by `spend_seed_day24_r1.py`, whose amounts are the
 * landed recipe (`review/ord0504/spend_seed_r1.py`) copied verbatim.
 *
 *  - S1 the priced trip: declared budget rung, per-currency rung (confirmed / estimated, CNY and USD
 *    kept apart), the unpriced line said as a count rather than as 0.00, and the line list.
 *  - S2 the bare trip: the named empty state, still showing the declared budget, and no total.
 *  - S3 screen against server: the same two rungs compared to the arithmetic the doors support.
 *  - S4 (CMDR 01:09 cell ③) the sentence that names the money sitting in a currency total and on no
 *    day, its count taken from the two answers, and the gap between the two faces equal to it.
 *  - S5 the same sentence must NOT appear on the bare trip -- no priced money, no gap to announce.
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";

const SEED = JSON.parse(
  readFileSync("D:/moodle/_qoder_6a_ord0531_20261001/spend_seed_day24_report.json", "utf8"),
) as {
  journeys: { bare: string; priced: string };
  answers: Record<string, { experience: any; wallet: any }>;
};

const DATED = JSON.parse(
  readFileSync("D:/moodle/_qoder_6a_ord0531_20261001/spend_offday_report_r1.json", "utf8"),
) as {
  journey: string;
  precondition_check: Record<string, string[]>;
};

const EVIDENCE = "D:/moodle/_qoder_6a_ord0531_20261001/e2e";

const readings: Record<string, unknown> = {};

test.setTimeout(180_000);

function note(name: string, body: unknown) {
  readings[name] = body;
  console.log(`SPEND ${name} ${JSON.stringify(body)}`);
}

/** `moneyText` in the product: "<CURRENCY> <amount/100 with two decimals, en-US grouping>". */
function money(minor: number, currency: string): string {
  return `${currency} ${(minor / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/**
 * The same four rules the face documents, redone here from the payloads: leg KNOWN is estimated (a
 * planned segment is not a charge), reservations and commitments whose status the server calls
 * confirmed are confirmed, an amount of null is neither a zero nor silent, and currencies never merge.
 * The recomputation lives in S3, next to the assertions it feeds, so there is exactly one copy of it.
 */

async function openSpendTab(page: Page, journeyId: string) {
  await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
  await page.goto("/v8/trips", { waitUntil: "load" });
  const tabs = page.getByTestId("trips-tabs");
  await expect(tabs, "the trips workspace should mount for a stored journey").toBeVisible({ timeout: 30_000 });
  await page.getByTestId("trips-tab-spend").click();
  const panel = page.getByTestId("trips-spend");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  await page.waitForTimeout(1_200);
  return panel;
}

test.describe.serial("CMDR 01:24 the spend face on day24", () => {
  test("S1 the priced trip says both rungs in rendered text", async ({ page }) => {
    const panel = await openSpendTab(page, SEED.journeys.priced);
    const declared = (await panel.getByTestId("spend-declared-value").innerText()).replace(/\s+/g, " ").trim();
    const groups = await panel.getByTestId("spend-group-total").allInnerTexts();
    const unpriced = await panel.getByTestId("spend-unpriced").allInnerTexts();
    const lines = [];
    for (const line of await panel.getByTestId("spend-line").all()) {
      lines.push({
        source: (await line.getByTestId("spend-line-source").innerText()).trim(),
        amount: (await line.getByTestId("spend-line-amount").innerText()).trim(),
      });
    }
    const lede = (await panel.getByTestId("spend-lede").innerText()).replace(/\s+/g, " ").trim();
    const text = (await panel.innerText()).replace(/\s+/g, " ");

    note("S1_priced", { declared, lede, groups, unpriced, lines, text_head: text.slice(0, 260) });

    /* Budget rung: the declared budget is on the page and labelled as a promise, not a payment. */
    expect(declared).toBe(money(300000, "CNY"));
    expect(text).toContain("声明的预算");
    /* Actual rung: two currencies, kept apart, and the amounts the doors answered. */
    const joined = groups.join(" | ");
    expect(joined).toContain(money(32900, "CNY"));
    expect(joined).toContain(money(4500, "CNY"));
    expect(joined).toContain(money(9900, "USD"));
    expect(joined).not.toContain(money(42800, "CNY"));
    /* The unpriced leg is a count in words, never a zero. */
    expect(unpriced.join(" ")).toContain("另有 1 项根本没有价");
    expect(text).not.toMatch(/CNY 0\.00|USD 0\.00/);
    /* Every line the server answered is listed with its own source words. */
    expect(lines.length).toBe(5);
    await page.screenshot({ path: `${EVIDENCE}/S1_priced.png`, fullPage: true });
  });

  test("S2 the bare trip names its own emptiness and still says the budget", async ({ page }) => {
    const panel = await openSpendTab(page, SEED.journeys.bare);
    const emptyVisible = await panel.getByTestId("spend-empty").count();
    const body = emptyVisible
      ? (await panel.getByTestId("spend-empty-body").innerText()).replace(/\s+/g, " ").trim()
      : null;
    const declared = (await panel.getByTestId("spend-declared-value").innerText()).replace(/\s+/g, " ").trim();
    const totals = await panel.getByTestId("spend-group-total").count();
    const text = (await panel.innerText()).replace(/\s+/g, " ");

    note("S2_bare", { empty_face_rendered: emptyVisible, empty_body: body, declared, currency_totals: totals });

    expect(emptyVisible, "a trip with nothing priced must get the named empty state").toBe(1);
    expect(body).toContain("1");
    expect(totals, "an empty face may not print a total").toBe(0);
    expect(declared).toBe(money(300000, "CNY"));
    expect(text).not.toMatch(/CNY 0\.00|USD 0\.00/);
    await page.screenshot({ path: `${EVIDENCE}/S2_bare.png`, fullPage: true });
  });

  test("S3 the screen's numbers are the doors' numbers, recomputed here", async ({ page }) => {
    const experience = SEED.answers.priced.experience;
    const wallet = SEED.answers.priced.wallet;
    const legs = (experience.active_plan?.legs ?? []) as any[];
    const reservations = (wallet.reservations ?? []) as any[];
    const commitments = (wallet.commitments ?? []) as any[];

    const byCurrency: Record<string, { confirmed: number; estimated: number }> = {};
    const noInstant: Record<string, { confirmed: number; estimated: number }> = {};
    const add = (
      into: Record<string, { confirmed: number; estimated: number }>,
      currency: string,
      bucket: "confirmed" | "estimated",
      minor: number,
    ) => {
      into[currency] = into[currency] || { confirmed: 0, estimated: 0 };
      into[currency][bucket] += minor;
    };
    /* The same four rules the face documents, redone from the payloads here so a wrong screen cannot
       be matched by a wrong expectation written next to it. */
    for (const leg of legs) {
      if (leg.cost_amount_minor != null && leg.cost_status === "KNOWN") {
        add(byCurrency, leg.currency, "estimated", leg.cost_amount_minor);
        if (!leg.depart_at) add(noInstant, leg.currency, "estimated", leg.cost_amount_minor);
      }
    }
    for (const item of reservations) {
      if (item.status === "CONFIRMED" && item.price?.amount_minor != null) {
        add(byCurrency, item.price.currency, "confirmed", item.price.amount_minor);
        if (!item.scheduled_departure_at) add(noInstant, item.price.currency, "confirmed", item.price.amount_minor);
      }
    }
    for (const item of commitments) {
      const value = item.monetary_value ?? {};
      if (["ACTIVE", "FULFILLED"].includes(item.status) && value.amount_minor != null) {
        add(byCurrency, value.currency, "confirmed", value.amount_minor);
        if (!item.policy?.scheduled_start_at) add(noInstant, value.currency, "confirmed", value.amount_minor);
      }
    }

    const panel = await openSpendTab(page, SEED.journeys.priced);
    const groups = (await panel.getByTestId("spend-group-total").allInnerTexts()).join(" | ");
    const dayTexts: string[] = [];
    for (const day of await panel.getByTestId("spend-day").all()) {
      dayTexts.push((await day.innerText()).replace(/\s+/g, " ").trim());
    }

    note("S3_server_against_screen", {
      legs_answered: legs.map((l) => [l.from_key, l.to_key, l.cost_amount_minor, l.cost_status, l.depart_at]),
      reservations_answered: reservations.map((r) => [r.confirmation_ref, r.price, r.scheduled_departure_at]),
      commitments_answered: commitments.map((c) => [c.kind, c.level, c.status, c.monetary_value, c.policy]),
      recomputed_total_by_currency: byCurrency,
      recomputed_without_an_instant: noInstant,
      on_screen_totals: groups,
      on_screen_days: dayTexts,
    });

    /* Rung 1 -- what the two doors add up to must be what the currency section says. */
    for (const [currency, buckets] of Object.entries(byCurrency)) {
      if (buckets.confirmed > 0) expect(groups).toContain(money(buckets.confirmed, currency));
      if (buckets.estimated > 0) expect(groups).toContain(money(buckets.estimated, currency));
    }

    /* Rung 2 -- and the day section must reconstruct those same totals once the lines the server
       gave no moment for are added back. A line that is in the total but reachable in no day is not a
       defect of arithmetic; it is a line the traveller cannot place, so it is measured here rather
       than asserted away. */
    const parsedDays: Record<string, { confirmed: number; estimated: number }> = {};
    for (const text of dayTexts) {
      for (const hit of text.matchAll(/(已确认|估算)\s+([A-Z]{3})\s+([\d,]+\.\d{2})/g)) {
        const bucket = hit[1] === "已确认" ? "confirmed" : "estimated";
        const currency = hit[2];
        parsedDays[currency] = parsedDays[currency] || { confirmed: 0, estimated: 0 };
        parsedDays[currency][bucket] += Math.round(Number(hit[3].replace(/,/g, "")) * 100);
      }
    }
    for (const [currency, buckets] of Object.entries(byCurrency)) {
      const inDays = parsedDays[currency] || { confirmed: 0, estimated: 0 };
      const nowhere = noInstant[currency] || { confirmed: 0, estimated: 0 };
      expect(inDays.confirmed + nowhere.confirmed, `${currency} confirmed: days plus no-instant`).toBe(buckets.confirmed);
      expect(inDays.estimated + nowhere.estimated, `${currency} estimated: days plus no-instant`).toBe(buckets.estimated);
    }
  });

  test("S4 the face says which priced money belongs to no day", async ({ page }) => {
    const experience = SEED.answers.priced.experience;
    const wallet = SEED.answers.priced.wallet;
    const legs = (experience.active_plan?.legs ?? []) as any[];
    const reservations = (wallet.reservations ?? []) as any[];
    const commitments = (wallet.commitments ?? []) as any[];

    /* The precondition first: without a priced line that has no instant, this arm measures nothing
       and can only read green. Every figure is counted from the two door answers. */
    const offDay: Array<{ source: string; currency: string; minor: number }> = [];
    for (const leg of legs) {
      if (leg.cost_amount_minor != null && leg.cost_status === "KNOWN" && !leg.depart_at) {
        offDay.push({ source: "LEG", currency: leg.currency, minor: leg.cost_amount_minor });
      }
    }
    for (const item of reservations) {
      if (item.status === "CONFIRMED" && item.price?.amount_minor != null && !item.scheduled_departure_at) {
        offDay.push({ source: "RESERVATION", currency: item.price.currency, minor: item.price.amount_minor });
      }
    }
    for (const item of commitments) {
      const value = item.monetary_value ?? {};
      if (["ACTIVE", "FULFILLED"].includes(item.status) && value.amount_minor != null
        && !item.policy?.scheduled_start_at) {
        offDay.push({ source: "COMMITMENT", currency: value.currency, minor: value.amount_minor });
      }
    }
    expect(offDay.length, "this seed must hold a priced line with no instant or S4 proves nothing")
      .toBeGreaterThan(0);

    const panel = await openSpendTab(page, SEED.journeys.priced);
    const sentence = panel.getByTestId("spend-off-day");
    const shown = await sentence.count();
    const text = shown ? (await sentence.innerText()).replace(/\s+/g, " ").trim() : null;
    const groups = (await panel.getByTestId("spend-group-total").allInnerTexts()).join(" | ");
    const dayTexts = (await panel.getByTestId("spend-day").allInnerTexts())
      .map((s) => s.replace(/\s+/g, " ").trim());

    note("S4_off_day_sentence", {
      off_day_lines: offDay,
      expected_count: offDay.length,
      sentence_rendered: shown,
      sentence_text: text,
      on_screen_totals: groups,
      on_screen_days: dayTexts,
    });

    expect(shown, "a priced line that belongs to no day must be said out loud").toBe(1);
    expect(text).toContain(String(offDay.length));
    expect(text).toContain("不在任何一天里");
    /* The sentence is not decoration: the money it points at has to be exactly the money the two
       faces disagree about, read off both of them. */
    const sumParsed = (texts: string[], currency: string): number => {
      let minor = 0;
      for (const chunk of texts) {
        for (const hit of chunk.matchAll(new RegExp(`(已确认|估算)\\s+${currency}\\s+([\\d,]+\\.\\d{2})`, "g"))) {
          minor += Math.round(Number(hit[2].replace(/,/g, "")) * 100);
        }
      }
      return minor;
    };
    for (const currency of Array.from(new Set(offDay.map((line) => line.currency)))) {
      const gap = sumParsed([groups], currency) - sumParsed(dayTexts, currency);
      const owed = offDay.filter((line) => line.currency === currency)
        .reduce((acc, line) => acc + line.minor, 0);
      expect(gap, `${currency}: totals minus days must be exactly the no-instant money`).toBe(owed);
    }
    await page.screenshot({ path: `${EVIDENCE}/S4_off_day_sentence.png`, fullPage: true });
  });

  test("S5 the face stays quiet when every priced line has a day", async ({ page }) => {
    /* The first version of this arm read the BARE trip, where nothing is priced: that face cannot
       contain the sentence whatever the component does, so the mutant that renders it unconditionally
       walked straight through it. This world is the one where the guard is load-bearing -- there is
       money on the page, and every line of it has an instant, so "1 笔不在任何一天里" must not appear. */
    const panel = await openSpendTab(page, DATED.journey);
    const shown = await panel.getByTestId("spend-off-day").count();
    const text = shown ? (await panel.getByTestId("spend-off-day").innerText()).trim() : null;
    const totals = await panel.getByTestId("spend-group-total").count();
    const pricedLines: string[] = [];
    for (const line of await panel.getByTestId("spend-line").all()) {
      pricedLines.push((await line.getByTestId("spend-line-amount").innerText()).trim());
    }
    const emptyFace = await panel.getByTestId("spend-empty").count();
    note("S5_every_line_has_a_day", {
      journey: DATED.journey,
      off_day_sentence_rendered: shown,
      off_day_sentence_text: text,
      currency_totals: totals,
      priced_line_amounts: pricedLines,
      empty_face_rendered: emptyFace,
      server_lines_without_instant: DATED.precondition_check,
    });
    /* Non-vacuous by construction: the world must really carry priced money the totals can show. */
    expect(totals, "this trip has money on the page, so the arm is not reading an empty face").toBeGreaterThan(0);
    expect(pricedLines.length, "and at least one priced line is listed").toBeGreaterThan(0);
    expect(shown, "with a day for every priced line there is no gap to announce").toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/S5_every_line_has_a_day.png`, fullPage: true });
  });

  test("S9 write the readings file", async () => {
    writeFileSync(`${EVIDENCE}/spend_readings_day24.json`, JSON.stringify(readings, null, 1), "utf8");
  });
});
