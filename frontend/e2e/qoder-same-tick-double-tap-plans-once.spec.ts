/**
 * ORD-0246 -- the traveller can tap 开始规划 twice before React has re-rendered, and a
 * `disabled={busy}` button cannot refuse that: the render is not the guard.  Measured by lane
 * `aad2d1f2` on the same product: two clicks inside one tick produced 2 POSTs and 2 mission rows,
 * with `b.disabled` false before, during and after both clicks.
 *
 * The fix is a synchronous in-flight ref taken ahead of every await in `submit()`.  This file
 * judges it on the real chain (its own throwaway run DB, no stubbed mission route) because the
 * claim is about rows the server writes, not about text this screen paints:
 *
 *   1. a same-tick pair of clicks  => exactly 1 POST and exactly 1 new `agent.mission.created` row
 *   2. the next real 60 ms double tap still plans  => the guard is one-shot, not sticky
 *   3. the refused click stays silent            => it must not sabotage the run that survived
 *
 * Row counting goes through the server's own read path (`GET /journeys/{id}/agent-feed`) rather
 * than a second sqlite handle, so the number quoted is the number the product itself admits.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-same-tick-double-tap-plans-once.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const COMMAND_PATH = "/agent/commands";
/** `main.py` writes exactly one of these per accepted command, with the mission id in `facts`. */
const MISSION_EVENT = "agent.mission.created";
/** Outcomes that would mean the refused second click damaged the run that survived. */
const SABOTAGE_STATUSES = ["CANCELLED", "CANCELLED_AFTER_SEND", "NETWORK"];

type FeedEvent = { event_type?: string; facts?: { mission_id?: string }; id?: string };
type Reading = Record<string, unknown>;
const readings: Reading[] = [];

async function missions(page: Page, journeyId: string) {
  const response = await page.request.get(
    `/api/v1/journeys/${journeyId}/agent-feed?limit=200`,
  );
  expect(
    response.status(),
    node("feed", `GET agent-feed answered ${response.status()}, so no row count exists`),
  ).toBe(200);
  const body = (await response.json()) as { events?: FeedEvent[] };
  const events = Array.isArray(body?.events) ? body.events : [];
  const created = events.filter((item) => item?.event_type === MISSION_EVENT);
  const ids = created.map((item) => String(item?.facts?.mission_id ?? ""));
  return {
    rows: created.length,
    ids,
    distinct: [...new Set(ids)].filter((id) => id.length > 0),
  };
}

/** Open the sheet on the seeded journey with a request long enough to pass the length gate. */
async function openSheet(page: Page) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(
    8,
  );
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes(COMMAND_PATH)) posts += 1;
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天时间慢慢逛，晚上看夜景，不要赶行程");
  return { journeyId, postCount: () => posts };
}

/**
 * Two click events dispatched in the same task, which is the shape the order names.  `disabled` is
 * sampled after both, because whether the button had already been re-rendered is the whole point.
 */
async function sameTickPair(page: Page) {
  const button = page.getByTestId("plan-ai-submit");
  await expect(button, node("pair", "the 开始规划 button never became clickable")).toBeVisible({
    timeout: 15_000,
  });
  return await button.evaluate((el) => {
    const target = el as HTMLButtonElement;
    const before = target.disabled;
    target.click();
    const during = target.disabled;
    target.click();
    return { before, during, after: target.disabled };
  });
}

/** Wait for this run to be over: the result block exists only once `busy` is cleared. */
async function settle(page: Page, who: string) {
  await expect(page.getByTestId("plan-ai-result"), node(who, "the run never answered")).toBeVisible({
    timeout: 180_000,
  });
  await expect
    .poll(() => page.getByTestId("plan-ai-cancel").count(), {
      timeout: 60_000,
      message: node(who, "the sheet stayed busy, so no row count below is final"),
    })
    .toBe(0);
  return await page.getByTestId("plan-ai-result").getAttribute("data-result-status");
}

/**
 * Row counts must be read once nothing is still arriving: the result block can be re-shown by the
 * *previous* run, so "the UI answered" is not enough to call a count final.
 */
async function settledMissions(page: Page, journeyId: string, who: string) {
  let previous = (await missions(page, journeyId)).rows;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 400));
    const current = await missions(page, journeyId);
    if (current.rows === previous) return current;
    previous = current.rows;
  }
  throw new Error(node(who, "mission rows kept growing for 4 s, so the run never settled"));
}

test("ord0246-1-a-same-tick-double-tap-creates-one-run-and-one-row — 同 tick 两击只留一次任务", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const { journeyId, postCount } = await openSheet(page);
  const before = await missions(page, journeyId);
  const disabledStates = await sameTickPair(page);
  const status = await settle(page, "ord0246-1");
  const after = await settledMissions(page, journeyId, "ord0246-1");

  // Positive control on the counter itself: a tap that never reached the network would make
  // "exactly one POST" true for the wrong reason.
  expect(postCount(), node("ord0246-1", "0 POST reached the network, so nothing was measured")).toBe(
    1,
  );
  expect(
    after.rows - before.rows,
    node(
      "ord0246-1",
      `the same-tick pair wrote ${after.rows - before.rows} mission rows (${after.ids
        .slice(before.rows)
        .join(",")}) — one tap pair must create exactly one`,
    ),
  ).toBe(1);
  expect(
    new Set(after.ids.slice(before.rows)).size,
    node("ord0246-1", "the new rows do not carry distinct mission ids in facts.mission_id"),
  ).toBe(1);
  expect(after.ids.slice(before.rows)[0].length, node("ord0246-1", "facts.mission_id was empty")).toBeGreaterThan(8);

  readings.push({
    node: "ord0246-1",
    journeyId,
    action: "same_tick_pair",
    posts: postCount(),
    rowsBefore: before.rows,
    rowsAfter: after.rows,
    newRowIds: after.ids.slice(before.rows),
    buttonDisabled: disabledStates,
    resultStatus: status,
    note: "before/during/after are the rendered `disabled` flag sampled around the two synchronous clicks; all false is what makes the render useless as a guard",
  });
});

test("ord0246-2-the-guard-is-one-shot-a-settled-run-does-not-block-the-next-one — 门是一次性的", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const { journeyId, postCount } = await openSheet(page);
  const first = await missions(page, journeyId);
  /* Deliberately two single, deliberate clicks with a finished run between them -- NOT the
     same-tick pair, which ORD-0246-1 owns.  If this node also tapped twice in one tick, the
     "guard missing" mutant would redden it too and the two claims would stop having separate
     killers. */
  await page.getByTestId("plan-ai-submit").click();
  const startedAt = Date.now();
  await settle(page, "ord0246-2/first");
  const runLatencyMs = Date.now() - startedAt;
  const between = await settledMissions(page, journeyId, "ord0246-2/first");

  await page.getByTestId("plan-ai-submit").click();
  await settle(page, "ord0246-2/second");
  const after = await settledMissions(page, journeyId, "ord0246-2/second");

  expect(
    between.rows - first.rows,
    node("ord0246-2", `the first click created ${between.rows - first.rows} rows`),
  ).toBe(1);
  expect(
    after.rows - between.rows,
    node(
      "ord0246-2",
      `the click after a settled run created ${after.rows - between.rows} rows — a guard that never releases turns the traveller's next request into a silent dead button`,
    ),
  ).toBe(1);
  expect(postCount(), node("ord0246-2", "expected two commands in total across the two clicks")).toBe(
    2,
  );

  /* DIAGNOSTIC, deliberately not asserted: a real 60 ms double tap is only covered by this guard
     while a run is still in flight.  With the AI leg off (`CITYNEXUS_AI_ENABLED=false`, which is how
     this stack runs) a run settles in about `runLatencyMs` below -- sometimes inside the gap -- so
     the second click of that pair can be, to the code, a fresh request after a completed one.
     Recording the number is the honest half; the residual is named in the ledger rather than hidden
     behind an assertion that would pass only because this machine is slow today. */
  await page.getByTestId("plan-ai-submit").dblclick({ delay: 60 });
  const fastGapStatus = await settle(page, "ord0246-2/60ms-bracket").catch(() => "UNSETTLED");
  const bracket = await settledMissions(page, journeyId, "ord0246-2/60ms-bracket");
  readings.push({
    node: "ord0246-2",
    journeyId,
    action: "click, settle, click, settle, then dblclick({delay:60})",
    posts: postCount(),
    rows: { first: first.rows, between: between.rows, after: after.rows, bracket: bracket.rows },
    runLatencyMs,
    realDoubleTapRows: bracket.rows - after.rows,
    residual:
      "RESIDUAL_FAST_RUN_DOUBLE_TAP: a 60 ms real double tap created realDoubleTapRows run(s); a re-entry guard only covers clicks that arrive while a run is in flight, so this needs a coalescing window or a per-command idempotency key, which ORD-0246 does not ask for.",
    secondActionStatus: fastGapStatus,
  });
});

test("ord0246-3-the-refused-click-is-silent-and-does-not-hurt-the-run — 被拒的那一下不许伤到活着的那次", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const { journeyId, postCount } = await openSheet(page);
  const before = await missions(page, journeyId);
  await sameTickPair(page);
  const status = await settle(page, "ord0246-3");
  const after = await settledMissions(page, journeyId, "ord0246-3");

  /* Refusing the second click must be a no-op, not an interruption: if the guard aborts the
     controller it owns, the surviving run is reported as cancelled and the traveller loses work
     they never asked to lose. */
  expect(
    SABOTAGE_STATUSES.includes(String(status)),
    node(
      "ord0246-3",
      `the surviving run ended as "${status}" — the refused click damaged it (statuses that mean damage: ${SABOTAGE_STATUSES.join("/")})`,
    ),
  ).toBe(false);
  const announced = await page.evaluate(() => {
    const result = document.querySelector('[data-testid="plan-ai-result"]');
    const error = document.querySelector('[data-testid="plan-ai-result"][data-result-tone="danger"]');
    return { text: (result?.textContent ?? "").replace(/\s+/g, " ").slice(0, 160), danger: !!error };
  });
  expect(
    announced.danger,
    node("ord0246-3", `the refused click painted a danger result: "${announced.text}"`),
  ).toBe(false);

  readings.push({
    node: "ord0246-3",
    journeyId,
    posts: postCount(),
    rowsCreated: after.rows - before.rows,
    resultStatus: status,
    resultText: announced.text,
    dangerPainted: announced.danger,
  });
});

test.afterAll(async () => {
  writeFileSync(
    evidencePath("ord0246-same-tick-double-tap.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0246",
        at: new Date().toISOString(),
        judgingCriterion:
          "同一 tick 两击 ⇒ 1 POST / 1 行（行数由服务器自己的 GET agent-feed 数出，不是我另开一个 sqlite 句柄）",
        missionEvent: MISSION_EVENT,
        sabotageStatuses: SABOTAGE_STATUSES,
        readings,
      },
      null,
      1,
    ),
  );
});
