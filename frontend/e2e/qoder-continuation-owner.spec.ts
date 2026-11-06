/**
 * ORD-0269 cells 1 and 3 -- the continuation reveal is a durable fact, not a silence.
 *
 * Judging shape (commander 10:0x, from the arbiter's ruling):
 *   cell 1  leg 1 answered, marker persisted, re-enter the same journey
 *           => the painted sentence AND a working 「继续这次规划」 gate (innerText, not data attrs);
 *   cell 3  feed read fails (stub 500) OR the marker's mission is excluded from the ascending,
 *           capped page => the named 「无法确认上次规划进度」 line, never silence;
 *   R-4     a brand-new journey (no marker) shows NO pending card;
 *   R-6     the read is issued with limit=200 AND the target round is asserted to be on that page;
 *           a page without the marker's mission yields no conclusion of either kind; timestamps are
 *           never treated as a mission association;
 *   clause 5 the marker must NOT be cleared before a request goes out (sampled mid-flight).
 *
 * The resume gate is MANUAL_RESUME / NOT-EXACTLY-ONCE: one press re-issues the second leg with the
 * recorded mission (ORD-0278), but nothing here proves exactly-once delivery, and that boundary is
 * named rather than claimed away.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-continuation-owner.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const PENDING_TEXT = "上次有一次规划在这台设备上没跑完：前半段已完成，但还没有产出方案。";
const RESUME_LABEL = "继续这次规划";
const UNCONFIRMABLE_TEXT = "上次规划有记录，但现在读不到它的进度，无法确认进行到哪一步。";
/** The mission id leg 1 will "receive"; injected through the stub so the marker links to it. */
const MISSION_ID = "11111111-2222-4333-8444-555555555555";

type Route = (route: { fulfill: (options: object) => Promise<void>; request: { method: () => string; url: () => string } }) => Promise<void>;

/**
 * Shared stack: the agent-feed route hands the stub events the marker's mission needs.  The feed is
 * served ascending; whether the target round is ON the page is itself the R-6 experiment variable.
 */
async function installStack(page: Page, options: { includeMissionOnPage: boolean; feedStatus?: number }) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  /* The init script runs on EVERY navigation; a blanket clear() on reload would wipe the marker
     leg 1 wrote, and cell 1 exists to test that the marker SURVIVES.  So the clear is one-time,
     gated on a seed flag this script itself sets. */
  await page.addInitScript((id) => {
    if (localStorage.getItem("citynexus.storage-seeded") !== "1") {
      localStorage.clear();
      localStorage.setItem("citynexus.storage-seeded", "1");
    }
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);

  const feedRequests: string[] = [];
  const events = [];
  if (options.includeMissionOnPage) {
    events.push({
      event_type: "agent.mission.created",
      entity_id: MISSION_ID,
      facts: { mission_id: MISSION_ID, event_id: null, requested_autonomy: "EXECUTE_WITHIN_BOUNDS" },
      summary: "Agent.mission.created",
      timestamp: "2026-09-24T00:00:00+00:00",
    });
    events.push({
      event_type: "agent.run.completed",
      entity_id: MISSION_ID,
      facts: { run_id: MISSION_ID, mission_id: MISSION_ID, status: "COMPLETED", outcome: "stub" },
      summary: "Agent.run.completed",
      timestamp: "2026-09-24T00:00:01+00:00",
    });
  }
  await page.route("**/api/v1/journeys/*/agent-feed*", async (route) => {
    feedRequests.push(route.request().url());
    if (options.feedStatus) {
      await route.fulfill({ status: options.feedStatus, contentType: "text/plain", body: "stub" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ journey_id: journeyId, source: "stub", events }),
    });
  });
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mission: { id: MISSION_ID, status: "COMPLETED", failure_reason: null, reasoning_summary: null },
      }),
    }),
  );
  await page.route("**/api/v1/journeys/*/plans", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }),
  );

  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  return { journeyId, feedRequests };
}

/** Leg 1: run one command through the sheet so the marker is written by the product itself. */
async function runLeg1(page: Page) {
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景，别排太满");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result")).toBeVisible({ timeout: 90_000 });
  await expect
    .poll(() => page.getByTestId("plan-ai-cancel").count(), { timeout: 30_000 })
    .toBe(0);
  await page.getByTestId("plan-ai-dismiss").click();
}

test("ord0269-cell1-reveals-and-names-its-boundary — 腿1 之后重进必须看见那句话和那扇门", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const { journeyId, feedRequests } = await installStack(page, { includeMissionOnPage: true });
  await runLeg1(page);

  // Marker present and linked to the mission leg 1 received.
  const marker = await page.evaluate(
    (jid) => JSON.parse(localStorage.getItem(`citynexus.v8.continuation.${jid}`) ?? "null"),
    journeyId,
  );
  expect(
    marker?.missionId === MISSION_ID,
    node("ord0269-cell1", `the persisted marker does not link to leg 1's mission (${JSON.stringify(marker)})`),
  ).toBe(true);

  // R-6 hard assertion A: the read was issued with limit=200.
  expect(
    feedRequests.some((url) => url.includes("limit=200")),
    node("ord0269-cell1", `no agent-feed read carried limit=200 (seen: ${feedRequests.join(" | ")})`),
  ).toBe(true);

  // Re-enter: reload, then the reveal must be painted words plus a working gate.
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  const card = page.getByTestId("continuation-pending");
  await expect(card).toBeVisible({ timeout: 15_000 });
  const heading = (await page.getByTestId("continuation-pending-heading").innerText()).replace(/\s+/g, " ").trim();
  expect(
    heading.includes("没跑完") && heading.includes("还没有产出方案"),
    node("ord0269-cell1", `the painted heading reads "${heading}" -- not the promised sentence`),
  ).toBe(true);
  const gate = page.getByTestId("continuation-resume");
  await expect(gate).toBeEnabled();
  expect((await gate.innerText()).trim(), node("ord0269-cell1", "the resume gate has no label")).toBe(RESUME_LABEL);

  /* Clause 5, measured against what the press does NOW.  ORD-0278 turned this gate into "re-issue
     the second leg carrying the recorded mission", so asserting that the sheet opens is no longer
     the contract -- and the clause this node owns cannot be judged by a click that sends nothing.
     The stub answers after a delay, which gives a real in-flight window; what must hold inside it
     is that the marker is still there.  The outcome sentence and the after-acceptance clearing are
     judged by ORD-0278's own gate, not duplicated here. */
  let leg2Started = 0;
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) => {
    leg2Started += 1;
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify([]) });
  });
  await gate.click();
  await expect
    .poll(() => leg2Started, {
      timeout: 20_000,
      message: node("ord0269-cell1", "pressing the resume gate sent no second-leg request at all"),
    })
    .toBeGreaterThan(0);
  const markerDuringFlight = await page.evaluate(
    (jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`),
    journeyId,
  );
  expect(
    markerDuringFlight,
    node(
      "ord0269-cell1",
      "the marker was gone while the second leg was still in flight -- clause 5 forbids clearing ahead of a visible outcome",
    ),
  ).not.toBeNull();

  writeFileSync(
    evidencePath("ord0269-cell1-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0269-cell1",
        journeyId,
        marker,
        feedRequests,
        heading,
        leg2StartedDuringFlight: leg2Started,
        markerBytesDuringFlight: String(markerDuringFlight ?? "").length,
      },
      null,
      1,
    ),
  );
});

test("ord0269-cell3-feed-failure-and-page-exclusion-are-named — 读不到就明说，不静默", async ({ page }) => {
  test.setTimeout(300_000);
  // Cell 3a: the marker exists but the feed read fails (stub 500).
  const { journeyId } = await installStack(page, { includeMissionOnPage: true, feedStatus: 500 });
  await runLeg1(page);
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByTestId("continuation-unconfirmable"),
    node("ord0269-cell3", "a feed 500 with a marker present stayed silent"),
  ).toBeVisible({ timeout: 15_000 });
  const line = (await page.getByTestId("continuation-unconfirmable").innerText()).replace(/\s+/g, " ").trim();
  expect(line, node("ord0269-cell3", `the named line reads "${line}"`)).toBe(UNCONFIRMABLE_TEXT);
  // The pending card must NOT show: an unconfirmable state is not a pending state.
  await expect(page.getByTestId("continuation-pending")).toHaveCount(0);

  /* Cell 3b: the feed ANSWERS -- 200, JSON -- but the body carries no events array.  That is a
     different emitter than 3a (the classifier's malformed guard, not the hook's isError branch),
     so it needs its own read: the first arms run proved that 3a alone left this branch untested. */
  await page.route("**/api/v1/journeys/*/agent-feed*", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ journey_id: journeyId, source: "stub" }),
    }),
  );
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByTestId("continuation-unconfirmable"),
    node("ord0269-cell3", "a 200 feed with no events array stayed silent instead of naming itself"),
  ).toBeVisible({ timeout: 15_000 });
  const malformedLine = (await page.getByTestId("continuation-unconfirmable").innerText())
    .replace(/\s+/g, " ")
    .trim();
  expect(malformedLine, node("ord0269-cell3", `the malformed-200 line reads "${malformedLine}"`)).toBe(
    UNCONFIRMABLE_TEXT,
  );
  await expect(page.getByTestId("continuation-pending")).toHaveCount(0);

  writeFileSync(
    evidencePath("ord0269-cell3-feed-failure.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0269-cell3",
        journeyId,
        line,
        malformedTwentyHundredLine: malformedLine,
        note: "two emitters, two reads: 3a the read fails (500), 3b it answers 200 with no events array",
      },
      null,
      1,
    ),
  );
});

test("ord0269-r6-page-exclusion-yields-no-conclusion — 本页不含目标轮次 ⇒ 不下结论", async ({ page }) => {
  test.setTimeout(300_000);
  // R-6: the marker's mission is NOT on the served page (ascending capped feed pushed it out).
  const { journeyId } = await installStack(page, { includeMissionOnPage: false });
  await runLeg1(page);
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });

  /* An absence is not a judgement: `toHaveCount(0)` is satisfied the moment the element is missing,
     which includes "the read has not come back yet".  Measured -- with the exclusion branch mutated to
     paint `pending`, this node stayed green in 1.3s.  So wait for the named state this shape must
     paint, and only then require the pending card. */
  await expect(
    page.getByTestId("continuation-unconfirmable"),
    node(
      "ord0269-r6",
      "a page without the marker's mission painted no named state at all -- the read is unresolved or the shape was swallowed",
    ),
  ).toBeVisible({ timeout: 15_000 });
  const exclusionLine = (await page.getByTestId("continuation-unconfirmable").innerText())
    .replace(/\s+/g, " ")
    .trim();
  expect(exclusionLine, node("ord0269-r6", `the exclusion line reads "${exclusionLine}"`)).toBe(
    UNCONFIRMABLE_TEXT,
  );
  await expect(
    page.getByTestId("continuation-pending"),
    node("ord0269-r6", "a page without the marker's mission still painted the pending card"),
  ).toHaveCount(0);

  writeFileSync(
    evidencePath("ord0269-r6-exclusion-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0269-r6",
        journeyId,
        paintedLine: exclusionLine,
        rule: "marker's mission off-page => pending card absent; the unconfirmable line is the only named state this shape may paint",
      },
      null,
      1,
    ),
  );
});

test("ord0269-r4-a-brand-new-journey-shows-nothing — 全新行程不得亮待续卡", async ({ page }) => {
  test.setTimeout(300_000);
  /* R-4 read the strong way.  "No marker anywhere, no card" is nearly free: it stays green even if
     the marker stops being journey-scoped, which is the failure this control exists to catch.  So
     leg 1 writes a REAL marker on journey A first, then a second journey that was never planned is
     created through the product API and opened -- journey A's card must not follow the traveller. */
  const { journeyId } = await installStack(page, { includeMissionOnPage: true });
  await runLeg1(page);
  const markerOnA = await page.evaluate(
    (jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`) ?? "",
    journeyId,
  );
  expect(
    markerOnA.length > 0,
    node("ord0269-r4", `journey A has no marker to leak (storage key empty) -- negative control is vacuous`),
  ).toBe(true);

  const traveler = (await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0269 R-4 second traveller", locale: "en-GB", timezone: "Asia/Shanghai" },
    })
  ).json()) as { id?: string };
  expect(traveler.id, node("ord0269-r4", "no second traveller -- journey B could not be created")).toBeTruthy();
  const journeyB = (await (
    await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: traveler.id,
        title: "ORD-0269 R-4 unplanned journey",
        city_code: "CN-TIANJIN",
        timezone: "Asia/Shanghai",
        start_at: "2026-11-09T18:00:00+08:00",
        end_at: "2026-11-11T15:00:00+08:00",
        budget: { amount_minor: 120000, currency: "CNY" },
      },
    })
  ).json()) as { id?: string };
  expect(journeyB.id, node("ord0269-r4", "the product API refused to create journey B")).toBeTruthy();
  const otherJourneyId = String(journeyB.id);
  expect(
    otherJourneyId !== journeyId,
    node("ord0269-r4", `journey B came back as the same id as A (${otherJourneyId})`),
  ).toBe(true);

  await page.evaluate((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
  }, otherJourneyId);
  /* installStack's init script re-writes journey A on EVERY navigation, so a one-off setItem above
     is silently undone by the reload -- the v2 baselines caught the node passing for that reason
     ("no card" on a screen that was never journey B is not a control).  Init scripts run in the
     order they were added, so a second one carrying journey B is what actually moves the screen. */
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
  }, otherJourneyId);
  const readsOnB: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(`/journeys/${otherJourneyId}/`)) readsOnB.push(request.url());
  });
  await page.reload({ waitUntil: "load" });
  /* The screen has to be journey B's before "no card" means anything. */
  await expect
    .poll(() => readsOnB.length, {
      timeout: 30_000,
      message: node(
        "ord0269-r4",
        `the app never read journey ${otherJourneyId} -- the node would be asserting on journey A's screen`,
      ),
    })
    .toBeGreaterThan(0);
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByTestId("continuation-pending"),
    node("ord0269-r4", "journey A's interrupted round painted a pending card on journey B"),
  ).toHaveCount(0);
  await expect(
    page.getByTestId("continuation-unconfirmable"),
    node("ord0269-r4", "journey A's marker leaked into journey B as an unconfirmable line"),
  ).toHaveCount(0);

  writeFileSync(
    evidencePath("ord0269-r4-negative.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0269-r4",
        journeyWithMarker: journeyId,
        markerBytesOnA: markerOnA.length,
        unplannedJourney: otherJourneyId,
        bScopedReadsObserved: readsOnB.length,
        pendingCardsOnB: 0,
        unconfirmableLinesOnB: 0,
        note: "strong form: A carries a real marker, B is a separate real journey the app actually read, B shows nothing",
      },
      null,
      1,
    ),
  );
});

