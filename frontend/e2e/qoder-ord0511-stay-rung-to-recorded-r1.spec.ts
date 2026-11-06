/**
 * ORD-0511 -- the stay screen from an honest empty to a stay this server recorded.
 *
 * Everything here runs against this lane's own stack: the front is `vite preview` over this lane's own
 * built bundle, the API is this lane's own uvicorn on a private copy of the store.  No product response
 * is invented.  The journeys are found by title through the real read door, and the screen is aimed at
 * one of them by writing the shell's own journey key -- UI state, not a stubbed answer.
 *
 * The four readings the order asked for:
 *   ① the empty rung offers one action that really calls generation, and before that call it offers no
 *      way to confirm a stay (there is nothing to confirm);
 *   ② each outcome the server can answer with reaches the traveller in words, not as a machine name;
 *   ③ a stay that the write door accepted is announced from the read door's own answer, together with
 *      the protected stops the trip already carries;
 *   ④ the counter-example pair: no candidates means no confirm control, and a refusal injected in
 *      place of the write door must leave the confirmation sentence absent -- which is what proves that
 *      sentence is driven by the server, not by the click.
 *
 * Arm ④'s fault is injected by this instrument (route interception) and is labelled as such in the
 * evidence: it shows what the screen does with a refusal shape the product can produce, never that this
 * server sent it on this run.
 */

import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";

const EVIDENCE = process.env.CITYNEXUS_ORD0511_EVIDENCE ?? "D:/moodle/_qoder_n2b_ord0511_20260930/e2e";
const UUID = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
const rows: Record<string, unknown> = {};

test.setTimeout(240_000);

function note(name: string, body: unknown) {
  rows[name] = body;
  console.log(`O511 ${name} ${JSON.stringify(body).slice(0, 400)}`);
}

async function journeys(page: Page): Promise<Array<{ id: string; title: string }>> {
  const response = await page.request.get("/api/v1/journeys");
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as Array<{ id: string; title: string }>;
}

/** Aim the shell at one journey through the key the shell itself uses, then open its stay screen. */
async function openStay(page: Page, journeyId: string) {
  await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
  await page.goto("/v8/stay", { waitUntil: "load" });
  await expect(page.getByTestId("v8-stay-explorer").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(800);
  return page.getByTestId("v8-stay-explorer").first();
}

const visibleText = async (section: import("@playwright/test").Locator) =>
  (await section.innerText()).replace(/\s+/g, " ").trim();

test.describe.serial("ORD-0511 the stay rung", () => {
  let byTitle: Record<string, string> = {};

  test("S0 find the journeys this lane created through the read door", async ({ page }) => {
    const list = await journeys(page);
    for (const item of list) byTitle[item.title] = item.id;
    note("S0_journeys_found", {
      titles: Object.keys(byTitle),
      same_day: byTitle["One Day in Nexus City"] ?? null,
      no_fixture_city: byTitle["CNX-ORD0511 bare trip"] ?? null,
      tianjin: byTitle["CNX-ORD0511 tianjin two nights"] ?? null,
    });
    expect(Object.keys(byTitle).length).toBeGreaterThan(0);
  });

  test("S1 a same-day trip: the rung names the absence, the action exists, nothing may be confirmed", async ({ page }) => {
    const id = byTitle["One Day in Nexus City"];
    test.skip(!id, "the same-day demo trip is not on this store");
    const section = await openStay(page, id as string);
    const before = await visibleText(section);
    const cards = await section.getByTestId("stay-offer-card").count();
    const confirmBefore = await section.getByTestId("stay-confirmed-line").count();
    await expect(section.getByTestId("stay-generate")).toBeVisible();
    await section.getByTestId("stay-generate").click();
    await expect(section.getByTestId("stay-generation-outcome")).toBeVisible({ timeout: 180_000 });
    const outcome = (await section.getByTestId("stay-generation-outcome").innerText()).trim();
    note("S1_same_day", {
      cards_before: cards,
      confirm_control_before_click: confirmBefore,
      outcome_on_screen: outcome,
      uuid_in_rendere_before: UUID.test(before),
      uuid_in_rendered_after: UUID.test(await visibleText(section)),
    });
    // zero nights is the server's own answer for a trip that needs no lodging
    expect(outcome).toContain("同一天");
    expect(confirmBefore).toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/S1_same_day.png`, fullPage: true });
  });

  test("S2 a city the store has no accommodation data for: named empty, still nothing to confirm", async ({ page }) => {
    const id = byTitle["CNX-ORD0511 bare trip"];
    test.skip(!id, "this lane's bare trip is not on this store");
    const section = await openStay(page, id as string);
    await section.getByTestId("stay-generate").click();
    await expect(section.getByTestId("stay-generation-outcome")).toBeVisible({ timeout: 180_000 });
    const outcome = (await section.getByTestId("stay-generation-outcome").innerText()).trim();
    const facts = (await section.getByTestId("stay-generation-facts").innerText()).replace(/\s+/g, " ");
    note("S2_no_data_city", {
      outcome_on_screen: outcome,
      facts_on_screen: facts,
      cards_after: await section.getByTestId("stay-offer-card").count(),
      confirm_control: await section.getByTestId("stay-confirmed-line").count(),
      uuid_in_rendered: UUID.test(await visibleText(section)),
    });
    expect(outcome).toContain("没有交回任何住宿数据");
    expect(await section.getByTestId("stay-confirmed-line").count()).toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/S2_no_data_city.png`, fullPage: true });
  });

  /** A trip this instrument creates through the real write door, so the empty rung is a fact about a
   *  trip nobody has generated for yet -- the journeys already on this store had their candidates
   *  ranked and one recorded by an earlier probe, and a screen with cards on it correctly shows no
   *  generate action at all (that was S3's first take, and the screen was right). */
  async function freshTianjinTrip(page: Page): Promise<string> {
    const traveler = await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0511 arm fixture", locale: "zh-CN", timezone: "Asia/Shanghai" },
    });
    expect(traveler.ok()).toBeTruthy();
    const travelerId = (await traveler.json()).id as string;
    const journey = await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: travelerId,
        title: `CNX-ORD0511 arm fresh ${Date.now()}`,
        city_code: "CN-TIANJIN",
        timezone: "Asia/Shanghai",
        start_at: "2026-12-04T15:00:00+08:00",
        end_at: "2026-12-06T21:00:00+08:00",
        budget: { amount_minor: 200000, currency: "CNY" },
        planning_mode: "balanced",
      },
    });
    expect(journey.ok()).toBeTruthy();
    return (await journey.json()).id as string;
  }

  test("S3 a fresh two-night trip: one press lists real cards, and picking one is recorded", async ({ page }) => {
    const id = await freshTianjinTrip(page);
    const section = await openStay(page, id);
    await expect(section.getByTestId("stay-generate")).toBeVisible();
    const cardsBefore = await section.getByTestId("stay-offer-card").count();
    const confirmBefore = await section.getByTestId("stay-confirmed-line").count();

    /* The outcome sentence lives in the empty branch, so it disappears the moment the reload brings
       cards -- that is the screen working, not a defect.  Reading it from the response this press
       actually got keeps the claim about the server's answer true, and the card count is what the
       traveller is left with, so that is what the arm waits on. */
    const pressing = section.getByTestId("stay-generate").click();
    const answered = await page.waitForResponse(
      (response) => response.url().includes("/stay-options/generate"),
      { timeout: 180_000 },
    );
    await pressing;
    const generation = (await answered.json()).generation ?? {};
    const outcomeFromRealResponse = String(generation.outcome ?? "");
    await expect(section.getByTestId("stay-offer-card").first()).toBeVisible({ timeout: 90_000 });
    const cards = await section.getByTestId("stay-offer-card").count();
    const outcomeLineStillOnScreen = await section.getByTestId("stay-generation-outcome").count();
    const firstCard = (await section.getByTestId("stay-offer-card").first().innerText()).replace(/\s+/g, " ");
    await section.getByTestId("stay-offer-card").first().click();
    await expect(section.getByTestId("stay-confirmed-line")).toBeVisible({ timeout: 90_000 });
    const confirmed = (await section.getByTestId("stay-confirmed-line").innerText()).trim();
    const protectedLine = (await section.getByTestId("stay-protected-stations").innerText()).replace(/\s+/g, " ");
    const budgetLine = (await section.getByTestId("stay-budget-line").innerText()).replace(/\s+/g, " ");
    note("S3_fresh_tianjin", {
      fixture_created_by_instrument_over_real_write_door: true,
      cards_before_generate: cardsBefore,
      confirm_control_before_generate: confirmBefore,
      outcome_read_from_this_press_response: outcomeFromRealResponse,
      ranked_in_response: generation.options_ranked ?? null,
      outcome_line_on_screen_after_cards: outcomeLineStillOnScreen,
      cards_after_generate: cards,
      first_card_text_head: firstCard.slice(0, 90),
      confirmed_line: confirmed,
      protected_line_head: protectedLine.slice(0, 150),
      budget_line_head: budgetLine.slice(0, 150),
      uuid_in_rendered: UUID.test(await visibleText(section)),
    });
    expect(cardsBefore).toBe(0);
    expect(confirmBefore).toBe(0);
    expect(outcomeFromRealResponse).toBe("OPTIONS_FOUND");
    expect(cards).toBeGreaterThan(0);
    expect(confirmed).toContain("已按这台服务器的记录排定");
    expect(protectedLine).toContain("受保护的站");
    expect(budgetLine).toContain("没有声明住宿预算");
    await page.screenshot({ path: `${EVIDENCE}/S3_fresh_tianjin.png`, fullPage: true });
  });

  test("S4 counter-example: with the write door refused, the confirmation sentence must not appear", async ({ page }) => {
    /* This fault is injected by the instrument, not sent by this server: a card click is answered with
       the product's own conflict refusal, so the arm can show whether the screen's claim is server-driven
       or merely click-driven. */
    await page.route("**/api/v1/journeys/*/stay-options/*/select", (route) =>
      route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "VALIDATION_ERROR",
            message: "STAY_ITINERARY_CONFLICT: STAY_PINNED_CONFLICT:check-in",
            message_key: "error.validation_error",
            trace_id: "instrument-injected",
            details: null,
            reason_code: "STAY_ITINERARY_CONFLICT",
          },
        }),
      }),
    );
    const id = await freshTianjinTrip(page);
    const section = await openStay(page, id);
    const pressing = section.getByTestId("stay-generate").click();
    await page.waitForResponse((r) => r.url().includes("/stay-options/generate"), { timeout: 180_000 });
    await pressing;
    await expect(section.getByTestId("stay-offer-card").first()).toBeVisible({ timeout: 90_000 });
    const cards = await section.getByTestId("stay-offer-card").count();
    const confirmedBefore = await section.getByTestId("stay-confirmed-line").count();
    await section.getByTestId("stay-offer-card").first().click();
    await page.waitForTimeout(5_000);
    const confirmedCount = await section.getByTestId("stay-confirmed-line").count();
    const text = await visibleText(section);
    note("S4_refusal_injected", {
      fault_injected_by_instrument: true,
      cards,
      confirmed_line_before_the_click: confirmedBefore,
      confirmed_line_present: confirmedCount,
      screen_claims_confirmed_anyway: text.includes("已按这台服务器的记录排定"),
    });
    expect(confirmedCount).toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/S4_injected_refusal.png`, fullPage: true });
  });

  test("S5 write the evidence file", async () => {
    writeFileSync(`${EVIDENCE}/ord0511_screen_arms.json`, JSON.stringify(rows, null, 1), "utf8");
  });
});
