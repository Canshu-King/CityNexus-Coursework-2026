/**
 * ORD-0232 -- `USER_VISIBLE_HALF`: a candidate the feasibility gate dropped must say so **on the
 * screen that shows the trip**, in words a traveller reads, not in the tokens the server trades in.
 *
 * Everything the stub replays is B's real response, field for field, from
 * `agent_feed_rejection_contract_for_D.json` (blob 30c721d0716b32028a6a1e7a9a3080fd71275d68), which
 * is also why the assertions below are literal-equality: the last order this lane worked on
 * (ORD-0226) proved that judging rendered prose by keyword lists leaves "warning plus reassurance in
 * one sentence" green.  The literals live **here**, not imported from the product.
 *
 * HONEST LIMIT OF THIS FILE: the emitter for `plans.candidate_rejected` (`a96fabf`) is not an
 * ancestor of this tip, so the positive direction is measured through a replayed real envelope --
 * it proves the rendering path, not the whole pipeline.  `review/ord0232_e2e_replay.py` is the
 * end-to-end arm and it refuses to run (and says why) until that commit is merged.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText, settledBox } from "./_qoderTextScan";

const FILE = "e2e/qoder-dropped-candidates-say-why.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** B's envelope, verbatim: 17 keys, and `facts.failures[0]` is the only field naming a place. */
const REJECTION_EVENT = {
  id: "79dd1c84-7329-46bc-8fe0-71b92c9e0a28",
  actor_id: null,
  actor_type: "system",
  decision: null,
  entity_id: "22222222-2222-4222-8222-222222222222",
  entity_type: "Journey",
  event_type: "plans.candidate_rejected",
  facts: {
    reason_codes: ["CANDIDATE_REJECTED_BY_FEASIBILITY_GATE"],
    summary: "candidate dropped before persistence: STROLLER_ACCESS_UNAVAILABLE:place-tech-art",
    failures: ["STROLLER_ACCESS_UNAVAILABLE:place-tech-art"],
    hard_rule_codes: ["BUDGET_MAX", "JOURNEY_END_BY", "STROLLER"],
    candidate_sequence: 0,
    stops: [
      "place-old-town",
      "place-riverside-museum",
      "place-food-hall",
      "place-tech-art",
      "place-river-cruise",
      "place-skyline-dinner",
    ],
  },
  journey_id: "22222222-2222-4222-8222-222222222222",
  proposal_id: null,
  reason_codes: ["CANDIDATE_REJECTED_BY_FEASIBILITY_GATE"],
  source: "audit_log",
  summary: "candidate dropped before persistence: STROLLER_ACCESS_UNAVAILABLE:place-tech-art",
  timestamp: "2026-09-22T18:10:20.570399+00:00",
  tool: null,
  trace_id: "c32b8383-d8df-4c5d-88d2-37e0531b50b6",
  type: "plans.candidate_rejected",
};

/** The directory the same screen already gets: id -> name. */
const PLACES = [
  { id: "place-tech-art", name: "科技馆", city_code: "CNX-DEMO" },
  { id: "place-old-town", name: "老城", city_code: "CNX-DEMO" },
];

/** Frozen copy of what the traveller must be able to read.  Do not import the product's strings. */
const FROZEN_HEADING_ZH = "规划前被去掉的候选地点（1 个）";
const FROZEN_ROW_ZH = "去掉「科技馆」：这一站不满足婴儿车通行的硬性条件";
const FROZEN_ROW_UNKNOWN_PLACE_ZH =
  "去掉「一个没认出名字的地点」：这一站不满足婴儿车通行的硬性条件";

/** Internal tokens that must reach neither the eye nor a screen reader. */
const RAW_TOKENS = [
  "STROLLER_ACCESS_UNAVAILABLE",
  "place-tech-art",
  "candidate dropped before persistence",
  "CANDIDATE_REJECTED_BY_FEASIBILITY_GATE",
];

async function openHubWithFeed(
  page: Page,
  events: unknown[],
  places: unknown[],
  fired: { feed: number; places: number },
) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.route("**/api/v1/journeys/*/agent-feed*", async (route) => {
    fired.feed += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        events,
        journey_id: route.request().url().match(/journeys\/([^/]+)\/agent-feed/)?.[1] ?? journeyId,
        source: "audit_log",
      }),
    });
  });
  await page.route("**/api/v1/places*", async (route) => {
    fired.places += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(places),
    });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  /* A stub that never ran reads exactly like a product that rendered nothing.  Prove the arm was
     exercised before judging what came of it (this lane's own instrument lesson). */
  await expect
    .poll(() => fired.feed, {
      timeout: 20_000,
      message: node(
        "ord0232-stub",
        "the agent-feed route handler never fired, so nothing below was measured on a real read",
      ),
    })
    .toBeGreaterThan(0);
}

async function rowTexts(page: Page): Promise<string[]> {
  const locator = page.getByTestId("dropped-candidate-row");
  const count = await locator.count();
  const rows: string[] = [];
  for (let index = 0; index < count; index += 1) {
    rows.push((await locator.nth(index).innerText()).replace(/\s+/g, " ").trim());
  }
  return rows;
}

test("ord0232-1-a-dropped-candidate-names-its-stop-and-its-condition — 少掉的每一站要点名", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0232-1-a-dropped-candidate-names-its-stop-and-its-condition";
  const fired = { feed: 0, places: 0 };
  await openHubWithFeed(page, [REJECTION_EVENT], PLACES, fired);

  const card = page.getByTestId("dropped-candidates");
  await expect(
    card,
    node(NODE, "the hub shows no dropped-candidate card even though the feed carried one rejection event"),
  ).toBeVisible({ timeout: 20_000 });
  await settledBox(page, '[data-testid="dropped-candidates"]');

  const heading = (await page.getByTestId("dropped-candidates-heading").innerText())
    .replace(/\s+/g, " ")
    .trim();
  expect(heading, node(NODE, `heading is not the frozen sentence, expected "${FROZEN_HEADING_ZH}" / got "${heading}"`)).toBe(
    FROZEN_HEADING_ZH,
  );
  const rows = await rowTexts(page);
  expect(rows.length, node(NODE, `one rejection event must produce exactly one row, got ${rows.length}`)).toBe(1);
  expect(
    rows[0],
    node(NODE, `row is not the frozen sentence, expected "${FROZEN_ROW_ZH}" / got "${rows[0]}"`),
  ).toBe(FROZEN_ROW_ZH);

  /* The diagnostics are only honest while a gate reads them: an asserted-once attribute cannot drift
     into contradiction with the prose beside it. */
  const attrs = await page
    .getByTestId("dropped-candidate-row")
    .evaluate((el) => ({ place: el.getAttribute("data-place-id"), code: el.getAttribute("data-rule-code") }));
  expect(
    `${attrs.place}/${attrs.code}`,
    node(NODE, `the row's own diagnostics drifted: got ${JSON.stringify(attrs)}`),
  ).toBe("place-tech-art/STROLLER_ACCESS_UNAVAILABLE");
  expect(
    fired.places,
    node(NODE, "the stop was rendered without asking the places directory, so the name cannot be real"),
  ).toBeGreaterThan(0);
  /* "is it painted, not merely spoken" is node 4's contract and lives only there: while this node
     also asserted the visual scan, the sr-only mutant reddened both and node 4 had no killer of its
     own. */
  writeFileSync(
    evidencePath("ui-overnight/ord0232-1-named-row.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        stubFired: fired,
        heading,
        rows,
        diagnostics: attrs,
        rule: "被丢的候选必须点名地点与冲突条件；地点名走 /places 目录",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0232-2-no-rejection-event-puts-nothing-on-the-screen — 没有事实就没有话", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0232-2-no-rejection-event-puts-nothing-on-the-screen";
  const fired = { feed: 0, places: 0 };
  await openHubWithFeed(page, [], PLACES, fired);
  await page.waitForTimeout(2_000);
  /* `expect(locator).toHaveCount(0)` has been measured hanging past its own timeout on a page with
     route interception active, so absence is decided by polling the count directly. */
  await expect
    .poll(() => page.getByTestId("dropped-candidates").count(), {
      timeout: 10_000,
      message: node(
        NODE,
        "a card is on screen with zero rejection events, which would be an invented reason",
      ),
    })
    .toBe(0);
  const scan = await scanScreenText(page);
  for (const phrase of ["规划前被去掉", "去掉「"]) {
    expect(
      scan.visual,
      node(NODE, `the screen still says "${phrase}" with no event behind it`),
    ).not.toContain(phrase);
  }
});

test("ord0232-3-raw-ids-and-raw-codes-reach-neither-the-eye-nor-a-screen-reader — 目录查不到也不许贴原文", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0232-3-raw-ids-and-raw-codes-reach-neither-the-eye-nor-a-screen-reader";
  const fired = { feed: 0, places: 0 };
  /* The directory answers, just not for this id: the honest output is "a stop whose name we could
     not read", never `place-tech-art` dressed up as a name. */
  await openHubWithFeed(page, [REJECTION_EVENT], [{ id: "place-old-town", name: "老城" }], fired);
  await expect(page.getByTestId("dropped-candidates")).toBeVisible({ timeout: 20_000 });
  const rows = await rowTexts(page);
  expect(
    rows[0] ?? "(no row)",
    node(NODE, `an unresolvable id must fall back to the frozen wording, got "${rows[0] ?? ""}"`),
  ).toBe(FROZEN_ROW_UNKNOWN_PLACE_ZH);

  const scan = await scanScreenText(page);
  for (const token of RAW_TOKENS) {
    expect(
      scan.visual,
      node(NODE, `VISUAL_TEXT_SCAN: internal token "${token}" is painted on screen`),
    ).not.toContain(token);
    expect(
      scan.accessible,
      node(NODE, `ACCESSIBLE_TEXT_SCAN: internal token "${token}" would be spoken aloud`),
    ).not.toContain(token);
  }
});

test("ord0232-4-the-card-is-painted-not-only-spoken — 只给读屏器说不算做到", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0232-4-the-card-is-painted-not-only-spoken";
  const fired = { feed: 0, places: 0 };
  await openHubWithFeed(page, [REJECTION_EVENT], PLACES, fired);
  await expect(page.getByTestId("dropped-candidates")).toBeVisible({ timeout: 20_000 });
  const geometry = await settledBox(page, '[data-testid="dropped-candidates"]');
  expect(
    geometry && geometry.width > 40 && geometry.height > 20,
    node(NODE, `the card has no real box (${JSON.stringify(geometry)}) -- an sr-only surface would pass a text scan`),
  ).toBe(true);
  const insideSrOnly = await page
    .getByTestId("dropped-candidates")
    .evaluate((el) => Boolean(el.closest(".v8-sr-only")));
  expect(insideSrOnly, node(NODE, "the card lives inside .v8-sr-only, so a sighted traveller sees nothing")).toBe(
    false,
  );
  const scan = await scanScreenText(page);
  expect(
    scan.visual,
    node(NODE, "the sentence is only in the accessible layer, not the painted one"),
  ).toContain("婴儿车通行");
});
