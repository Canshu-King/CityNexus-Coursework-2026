/**
 * ORD-0520 中止 -- the last cell of that order: the sentences were measured as bytes, as keys, as
 * types and as a mount point, but nobody had read them off a screen.  The landing note for the slice
 * carries `NOT_REACHED_BY_CMDR` for exactly this cell, so this file walks it in a real browser.
 *
 * Nothing here is a stand-in for the product.  The backend is the shipped FastAPI app on this lane's
 * own port over its own run-stamped sqlite (proven by the run id it echoes through both doors before
 * any arm runs -- see `review/ord0520/abort_glass_provenance_r1.py`), the writes go through
 * `POST /api/v1/journeys/{id}/abort`, and every judgement below is read from painted text
 * (`innerText` through `_qoderTextScan`'s visual layer), not from a `data-*` mirror.
 *
 * What each arm owes:
 *  G1 the consequence is said BEFORE the press, on the trip's own entry, and the entry is reachable
 *     in the phone shape without scrolling past the plan; no machine token is painted.
 *  G2 pressing it writes: the answer this device received carries the reason and `live_view`, the
 *     reason is then painted from the server's re-read (not from local state), and no second action
 *     appears on a trip that has been aborted.
 *  G3 the trip leaves 实时行程: the notice replaces the live content, and the pair -- a trip the
 *     server still calls live -- keeps the live content and shows no notice.  Both readings carry the
 *     `live_view` field as it crossed the wire, which is what makes this the server's decision.
 *  G4 中止 is not 丢弃: the trip is still in `GET /journeys` and absent from the recycle bin doors.
 *  G5 a refusal is said, not coded: a second window holding a stale form presses after the first
 *     window aborted the trip, and the screen names the condition in a sentence.  The machine token
 *     is only reachable on the 技术详情 face.
 *  G6 an empty reason is refused by the device and by the door, and neither writes.
 *  G7 the judgement is the server's and not the row's mutable status: another door moves
 *     `journeys.status` back to PLANNED (the write `select_plan` performs,
 *     `backend/app/main.py`, done here as a committed UPDATE because this lane's store cannot
 *     produce a plan -- `reason_code=CATALOG_EMPTY`, measured in
 *     `review/ord0520/ABORT_RESURRECT_PROBE.json`), and both the live screen and the trip's own
 *     entry have to keep treating the trip as called off.  This is the arm that separates "reads the
 *     server's field" from "re-derives it from status": with the latter the live screen paints live
 *     content again and the entry grows a second 中止 button.
 */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";
import { abortCopy } from "../src/features/abort/journeyAbortCopy";

const FILE = "e2e/qoder-89f2e2d6-ord0520-abort-on-the-glass-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (value: string | null | undefined) =>
  String(value ?? "").replace(/\s+/g, " ").trim();
const PROVENANCE = {
  run: process.env.CITYNEXUS_E2E_RUN_ID ?? "unset",
  head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unset",
  frontendPort: process.env.CITYNEXUS_ORD0520_PORT ?? "unset",
  backendPort: process.env.CITYNEXUS_BACKEND_PORT ?? "unset",
};
/** Tokens that belong to the details face and nowhere else on a painted screen. */
const MACHINE_TOKENS = [
  "JOURNEY_ALREADY_ABORTED",
  "ABORT_ACTOR_NOT_OWNER",
  "JOURNEY_IN_RECYCLE_BIN",
  "ABORT_WRITE_CONTESTED",
  "live_view",
  "abort.explains",
  "CANCELLED",
];

function evidence(name: string, payload: Record<string, unknown>) {
  const target = evidencePath("ui-ord0520-glass", name);
  writeFileSync(target, JSON.stringify({ ...PROVENANCE, ...payload }, null, 2), "utf-8");
  return target;
}

const stampOf = (tag: string) =>
  `${tag}${String(Math.floor(Math.random() * 1e6)).padStart(6, "0")}`;

async function newTraveler(page: Page, tag: string): Promise<string> {
  const body = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: `ORD-0520 走查 ${tag}`, locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  return String(body.id);
}

/** A trip the traveller could actually abort: the same body the recycle-bin gates use. */
async function newJourney(page: Page, travelerId: string, title: string): Promise<string> {
  const body = await (
    await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: travelerId,
        title,
        city_code: "CN-TIANJIN",
        timezone: "Asia/Shanghai",
        start_at: "2026-10-09T18:00:00+08:00",
        end_at: "2026-10-11T15:00:00+08:00",
        budget: { amount_minor: 120000, currency: "CNY" },
      },
    })
  ).json();
  return String(body.id);
}

/**
 * Enter the shell with this arm's own trip and wait for the trip to be on screen.  The boot read is
 * `GET /journeys/<id>/experience`, so the payload the screen got is captured here rather than
 * re-requested afterwards: a later read could see a row somebody else moved.
 */
async function enter(page: Page, journeyId: string, route: string) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
  const wire = page.waitForResponse(
    (res) => res.url().includes(`/journeys/${journeyId}/experience`) && res.status() === 200,
    { timeout: 45_000 },
  );
  await page.goto(route, { waitUntil: "load", timeout: 45_000 });
  const answer = await wire;
  return (await answer.json()) as any;
}

async function openItineraryTab(page: Page, why: string) {
  const tab = page.getByTestId("trips-tab-itinerary");
  await expect(tab, node("boot", `${why}: the itinerary tab never appeared`)).toBeVisible({
    timeout: 30_000,
  });
  await tab.click();
  const panel = page.getByTestId("journey-abort");
  await expect(
    panel,
    node("boot", `${why}: the trip's own 中止 entry never rendered on the itinerary tab`),
  ).toBeVisible({ timeout: 30_000 });
  return panel;
}

/**
 * Reachability in the shape the order named (390x844): the entry has to be in the viewport with its
 * centre hitting its own node, because an action a traveller scrolls past the whole plan to reach is
 * not an action, and an element under the bottom bar cannot be pressed.
 */
async function reachability(page: Page, testId: string) {
  const handle = page.getByTestId(testId);
  await handle.scrollIntoViewIfNeeded();
  const box = await handle.boundingBox();
  if (!box) throw new Error(`${testId} has no box`);
  const hits = await page.evaluate(
    ([x, y, id]) => {
      const top = document.elementFromPoint(x, y);
      const own = document.querySelector(`[data-testid="${id}"]`);
      return {
        hitTag: top ? top.tagName : "none",
        hitOwns: !!(top && own && (own === top || own.contains(top))),
        inViewportY: y >= 0 && y <= 844,
      };
    },
    [box.x + box.width / 2, box.y + Math.min(box.height / 2, 20), testId] as [number, number, string],
  );
  return {
    box: { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) },
    ...hits,
  };
}

async function paintedTokens(page: Page) {
  const scan = await scanScreenText(page);
  return {
    visual: scan.visual,
    visualLeaves: scan.visualLeaves,
    hits: MACHINE_TOKENS.filter((token) => scan.visual.includes(token)),
  };
}

test("ord0520g-1-the-consequence-is-said-before-the-press — 中止之前那句后果要真的在屏上", async ({
  browser,
}) => {
  test.setTimeout(240_000);
  const NODE = "G1";
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await context.newPage();
  const stamp = stampOf("g1-");
  const traveler = await newTraveler(page, stamp);
  const journey = await newJourney(page, traveler, `会被读屏的趟 ${stamp}`);
  const wire = await enter(page, journey, "/v8/trips");
  const panel = await openItineraryTab(page, "the trip was opened on the trips screen");

  const sentence = clean(await page.getByTestId("journey-abort-explains").innerText());
  const tableRow = clean(abortCopy("zh-CN", "abort.explains"));
  expect(sentence, node(NODE, `the screen says "${sentence}"`)).toBe(tableRow);
  for (const part of [
    "不再进入「实时行程」",
    "它仍留在你的行程列表里",
    "中止不等于丢弃",
    "你写下的原因",
    "不能回头",
  ]) {
    expect(sentence.includes(part), node(NODE, `the sentence never said "${part}": "${sentence}"`)).toBe(
      true,
    );
  }
  for (const forbidden of ["恢复", "还原", "找回"]) {
    expect(
      sentence.includes(forbidden),
      node(NODE, `the 中止 sentence borrowed the bin's or recovery's word "${forbidden}"`),
    ).toBe(false);
  }

  /* Said before the press means the entry has to answer without opening anything, and the entry has
     to be reachable in the phone shape. */
  const reach = await reachability(page, "journey-abort-explains");
  expect(reach.inViewportY, node(NODE, `the sentence sat outside the viewport: ${JSON.stringify(reach)}`)).toBe(
    true,
  );
  expect(reach.hitOwns, node(NODE, `the sentence's centre hit ${reach.hitTag}, not itself`)).toBe(true);

  const tokens = await paintedTokens(page);
  expect(tokens.hits, node(NODE, `machine tokens reached the painted layer: ${tokens.hits.join(", ")}`)).toEqual(
    [],
  );

  const formClosed = await page.getByTestId("journey-abort-submit").isVisible().catch(() => false);
  expect(formClosed, node(NODE, "the reason box was open before the traveller asked for it")).toBe(false);

  const shot = evidencePath("ui-ord0520-glass", "g1-consequence.png");
  await page.screenshot({ path: shot });
  evidence("g1_consequence_sentence.json", {
    arm: NODE,
    journeyId: journey,
    travelerId: traveler,
    painted_sentence: sentence,
    table_row: tableRow,
    sentence_equals_table_row: sentence === tableRow,
    load_bearing_parts_checked: 5,
    forbidden_words_found: [],
    reachability: reach,
    painted_layer: tokens,
    wire_live_view: wire?.journey?.live_view ?? null,
    screenshot: shot,
    verdict: "GREEN",
  });
  await context.close();
});

test("ord0520g-2-pressing-it-writes-and-the-reason-reads-back — 按下去要真写，原因要真读回", async ({
  browser,
}) => {
  test.setTimeout(240_000);
  const NODE = "G2";
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await context.newPage();
  const stamp = stampOf("g2-");
  const traveler = await newTraveler(page, stamp);
  const journey = await newJourney(page, traveler, `要被中止的趟 ${stamp}`);
  await enter(page, journey, "/v8/trips");
  await openItineraryTab(page, "the trip to abort was opened");

  const reason = `膝盖不舒服，这一趟先停下来（走查 ${stamp}）`;
  await page.getByTestId("journey-abort-open").click();
  await page.getByTestId("journey-abort-reason").fill(reason);
  const answer = page.waitForResponse(
    (res) => res.url().includes(`/journeys/${journey}/abort`) && res.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.getByTestId("journey-abort-submit").click();
  const response = await answer;
  expect(response.status(), node(NODE, `the abort door answered ${response.status()}`)).toBe(200);
  const body = await response.json();
  expect(String(body.status), node(NODE, `the door returned status="${body.status}"`)).toBe("CANCELLED");
  expect(String(body.abort_reason), node(NODE, `the door returned abort_reason="${body.abort_reason}"`)).toBe(
    reason,
  );
  expect(body.live_view?.included, node(NODE, `the door said live_view=${JSON.stringify(body.live_view)}`)).toBe(
    false,
  );
  expect(String(body.deleted_at ?? ""), node(NODE, "aborting also set deleted_at -- that is the bin's door")).toBe(
    "",
  );

  const paintedNow = clean(await page.getByTestId("journey-abort-readback-reason").innerText());
  expect(
    paintedNow.includes(reason),
    node(NODE, `the read-back box said "${paintedNow}" instead of the reason I wrote`),
  ).toBe(true);
  const paintedWhen = clean(await page.getByTestId("journey-abort-readback-at").innerText());
  expect(paintedWhen.length > 0, node(NODE, "the read-back box carried no moment")).toBe(true);
  await expect(
    page.getByTestId("journey-abort-done"),
    node(NODE, "the done box never appeared"),
  ).toBeVisible();

  /* The shell re-reads after the write; this is that read, so the state on screen came off the row. */
  const storedReason = page.getByTestId("journey-abort-stored-reason");
  await expect(
    storedReason,
    node(NODE, "after the re-read the entry never showed the stored reason"),
  ).toBeVisible({ timeout: 30_000 });
  const storedText = clean(await storedReason.innerText());
  expect(storedText.includes(reason), node(NODE, `the stored line said "${storedText}"`)).toBe(true);
  expect(
    await page.getByTestId("journey-abort-badge").innerText().then(clean),
    node(NODE, "the state line did not say 已中止"),
  ).toBe(clean(abortCopy("zh-CN", "abort.stateOnScreen")));
  expect(
    await page.getByTestId("journey-abort-submit").count(),
    node(NODE, "a second 中止 action appeared on a trip that is already aborted"),
  ).toBe(0);

  const tokens = await paintedTokens(page);
  expect(tokens.hits, node(NODE, `machine tokens on the painted layer: ${tokens.hits.join(", ")}`)).toEqual([]);

  const shot = evidencePath("ui-ord0520-glass", "g2-after-press.png");
  await page.screenshot({ path: shot });
  evidence("g2_press_writes_and_reads_back.json", {
    arm: NODE,
    journeyId: journey,
    travelerId: traveler,
    reason_typed: reason,
    http_status: response.status(),
    door_answer: { status: body.status, abort_reason: body.abort_reason, aborted_at: body.aborted_at, live_view: body.live_view, deleted_at: body.deleted_at ?? null },
    painted_readback_reason: paintedNow,
    painted_readback_when: paintedWhen,
    painted_stored_reason: storedText,
    painted_state_badge: clean(await page.getByTestId("journey-abort-badge").innerText()),
    second_action_count_after_abort: 0,
    painted_layer: tokens,
    screenshot: shot,
    verdict: "GREEN",
  });
  await context.close();
});

test("ord0520g-3-the-live-screen-loses-the-trip-and-keeps-another — 中止后离开实时行程，这一判来自服务器", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const NODE = "G3";
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });

  /* Two trips, one walk each, so the pair is on the same screen in the same run: the aborted row must
     lose the live content, the row the server still calls live must keep it. */
  const setup = await context.newPage();
  const stamp = stampOf("g3-");
  const traveler = await newTraveler(setup, stamp);
  const abortedTrip = await newJourney(setup, traveler, `中止过的趟 ${stamp}`);
  const liveTrip = await newJourney(setup, traveler, `还在走的趟 ${stamp}`);
  const write = await setup.request.post(`/api/v1/journeys/${abortedTrip}/abort`, {
    data: { reason: `中止之后再读实时屏（${stamp}）`, actor_traveler_id: traveler },
  });
  expect(write.status(), node(NODE, `the setup write answered ${write.status()}`)).toBe(200);
  await setup.close();

  const pageA = await context.newPage();
  const wireA = await enter(pageA, abortedTrip, "/v8/live");
  await expect(
    pageA.getByTestId("journey-aborted-notice"),
    node(NODE, "the live screen kept showing live content for a trip the server excluded"),
  ).toBeVisible({ timeout: 30_000 });
  const whyA = clean(await pageA.getByTestId("journey-aborted-why").innerText());
  expect(whyA, node(NODE, `the live screen said "${whyA}"`)).toBe(clean(abortCopy("zh-CN", "abort.liveExcluded")));
  const factsA = clean(await pageA.getByTestId("journey-aborted-facts-reason").innerText());
  expect(
    factsA.includes("中止之后再读实时屏"),
    node(NODE, `the live screen did not read the reason back: "${factsA}"`),
  ).toBe(true);
  expect(
    await pageA.getByTestId("live-recovery").count(),
    node(NODE, "the live content was still mounted beside the notice -- two answers on one screen"),
  ).toBe(0);
  expect(wireA?.journey?.live_view?.included, node(NODE, `the wire said ${JSON.stringify(wireA?.journey?.live_view)}`)).toBe(
    false,
  );
  const tokensA = await paintedTokens(pageA);
  expect(tokensA.hits, node(NODE, `machine tokens on the live screen: ${tokensA.hits.join(", ")}`)).toEqual([]);
  const reachA = await reachability(pageA, "journey-aborted-why");
  expect(reachA.hitOwns, node(NODE, `the notice's centre hit ${reachA.hitTag}`)).toBe(true);

  const pageB = await context.newPage();
  const wireB = await enter(pageB, liveTrip, "/v8/live");
  expect(
    await pageB.getByTestId("journey-aborted-notice").count(),
    node(NODE, "a trip the server still calls live got the aborted notice anyway"),
  ).toBe(0);
  await expect(
    pageB.getByTestId("live-recovery"),
    node(NODE, "the control arm lost its live content, so the pair measures nothing"),
  ).toBeVisible({ timeout: 30_000 });
  expect(wireB?.journey?.live_view?.included, node(NODE, `the wire said ${JSON.stringify(wireB?.journey?.live_view)}`)).toBe(
    true,
  );

  const shotA = evidencePath("ui-ord0520-glass", "g3-live-excluded.png");
  await pageA.screenshot({ path: shotA });
  const shotB = evidencePath("ui-ord0520-glass", "g3-live-included.png");
  await pageB.screenshot({ path: shotB });
  evidence("g3_live_screen_decision.json", {
    arm: NODE,
    aborted_trip: { journeyId: abortedTrip, wire_live_view: wireA?.journey?.live_view ?? null, painted_notice: whyA, painted_facts: factsA, live_content_nodes: 0, reachability: reachA },
    live_trip: { journeyId: liveTrip, wire_live_view: wireB?.journey?.live_view ?? null, notice_nodes: 0 },
    decision_source: "the notice renders from the live_view field on the wire; neither screen filters status",
    painted_layer_excluded: tokensA,
    screenshots: [shotA, shotB],
    verdict: "GREEN",
  });
  await context.close();
});

test("ord0520g-4-aborted-is-not-discarded — 中止不等于丢弃：列表门还有它，回收站两扇门没有", async ({
  browser,
}) => {
  test.setTimeout(240_000);
  const NODE = "G4";
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await context.newPage();
  const stamp = stampOf("g4-");
  const traveler = await newTraveler(page, stamp);
  const journey = await newJourney(page, traveler, `中止之后还在列表的趟 ${stamp}`);
  const binned = await newJourney(page, traveler, `被丢进回收站的趟 ${stamp}`);
  await enter(page, journey, "/v8/trips");

  const abortAnswer = await page.request.post(`/api/v1/journeys/${journey}/abort`, {
    data: { reason: `列表门与回收站门各读一次（${stamp}）`, actor_traveler_id: traveler },
  });
  expect(abortAnswer.status(), node(NODE, `abort answered ${abortAnswer.status()}`)).toBe(200);
  const discard = await page.request.delete(`/api/v1/journeys/${binned}`);
  expect(discard.status(), node(NODE, `the bin door answered ${discard.status()}, so the bin arm is empty`)).toBe(
    204,
  );

  const listed = await page.request.get("/api/v1/journeys");
  expect(listed.status(), node(NODE, `GET /journeys answered ${listed.status()}`)).toBe(200);
  const rows = (await listed.json()) as any[];
  const mine = rows.find((row) => String(row.id) === journey) ?? null;
  expect(mine, node(NODE, "the aborted trip disappeared from GET /journeys -- abort discarded it")).not.toBeNull();
  expect(String(mine.status), node(NODE, `the row in the list carries status="${mine.status}"`)).toBe("CANCELLED");
  expect(mine.live_view?.included, node(NODE, `the list row carries live_view=${JSON.stringify(mine.live_view)}`)).toBe(
    false,
  );
  const inList = rows.filter((row) => String(row.traveler_id) === traveler).map((row) => String(row.id));
  expect(
    inList.includes(binned),
    node(NODE, "the discarded trip is still in GET /journeys -- the two doors agree on nothing"),
  ).toBe(false);

  const binDoors: Record<string, { status: number; ids: string[] }> = {};
  for (const [name, path] of [
    ["scoped", `/api/v1/travelers/${traveler}/recycle-bin/journeys`],
    ["global", "/api/v1/recycle-bin/journeys"],
  ] as const) {
    const answer = await page.request.get(path);
    const body = answer.ok() ? ((await answer.json()) as any[]) : [];
    binDoors[name] = { status: answer.status(), ids: Array.isArray(body) ? body.map((row) => String(row.id)) : [] };
  }
  expect(
    binDoors.scoped.ids.includes(journey),
    node(NODE, "the recycle bin named the aborted trip -- 中止 was read as 丢弃"),
  ).toBe(false);
  expect(
    binDoors.scoped.ids.includes(binned),
    node(NODE, `the scoped bin did not hold the trip I discarded (statuses ${JSON.stringify(binDoors)})`),
  ).toBe(true);

  evidence("g4_abort_is_not_discard.json", {
    arm: NODE,
    travelerId: traveler,
    aborted_trip: journey,
    discarded_trip: binned,
    list_door: { status: listed.status(), rows_for_this_traveller: inList, aborted_row: { status: mine.status, live_view: mine.live_view, deleted_at: mine.deleted_at ?? null } },
    recycle_bin_doors: binDoors,
    verdict: "GREEN",
  });
  await context.close();
});

test("ord0520g-5-a-stale-window-press-is-said-in-words — 第二个窗口按下去，屏要说得出条件", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const NODE = "G5";
  const stamp = stampOf("g5-");
  const firstReason = `第一窗口的原因 ${stamp}`;
  const secondReason = `第二窗口想覆盖的原因 ${stamp}`;

  const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const pageA = await contextA.newPage();
  const traveler = await newTraveler(pageA, stamp);
  const journey = await newJourney(pageA, traveler, `两个窗口抢着中止的趟 ${stamp}`);

  const contextB = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const pageB = await contextB.newPage();
  await enter(pageB, journey, "/v8/trips");
  await openItineraryTab(pageB, "the stale window opened the trip");
  await pageB.getByTestId("journey-abort-open").click();
  await pageB.getByTestId("journey-abort-reason").fill(secondReason);

  await enter(pageA, journey, "/v8/trips");
  await openItineraryTab(pageA, "the first window opened the same trip");
  await pageA.getByTestId("journey-abort-open").click();
  await pageA.getByTestId("journey-abort-reason").fill(firstReason);
  const firstAnswer = pageA.waitForResponse(
    (res) => res.url().includes(`/journeys/${journey}/abort`) && res.request().method() === "POST",
    { timeout: 30_000 },
  );
  await pageA.getByTestId("journey-abort-submit").click();
  const first = await firstAnswer;
  expect(first.status(), node(NODE, `the first window's press answered ${first.status()}`)).toBe(200);

  /* Window B still holds the form it opened before the write landed.  It is not a fixture: it is the
     same page one second ago, and the trip underneath it really did move. */
  const staleAnswer = pageB.waitForResponse(
    (res) => res.url().includes(`/journeys/${journey}/abort`) && res.request().method() === "POST",
    { timeout: 30_000 },
  );
  await pageB.getByTestId("journey-abort-submit").click();
  const second = await staleAnswer;
  expect(second.status(), node(NODE, `the stale window's press answered ${second.status()}`)).toBe(409);
  const envelope = await second.json();
  expect(String(envelope?.error?.reason_code), node(NODE, `envelope ${JSON.stringify(envelope)}`)).toBe(
    "JOURNEY_ALREADY_ABORTED",
  );

  const refusal = clean(await pageB.getByTestId("journey-abort-refusal-copy").innerText());
  expect(refusal.includes("这趟已经被中止"), node(NODE, `the screen said "${refusal}"`)).toBe(true);
  expect(
    refusal.includes(firstReason),
    node(NODE, `the refusal did not name the reason already stored: "${refusal}"`),
  ).toBe(true);
  expect(
    refusal.includes("服务器没有把它再写一次"),
    node(NODE, `the refusal promised nothing about the write: "${refusal}"`),
  ).toBe(true);

  const beforeDetails = await paintedTokens(pageB);
  expect(
    beforeDetails.hits,
    node(NODE, `the token ${beforeDetails.hits.join(", ")} was painted before anyone opened 技术详情`),
  ).toEqual([]);
  await pageB.getByTestId("journey-abort-details-toggle").click();
  const token = clean(await pageB.getByTestId("journey-abort-reason-code").innerText());
  expect(token, node(NODE, `the details face named "${token}"`)).toBe("JOURNEY_ALREADY_ABORTED");

  /* Nothing was overwritten by the refused tap. */
  const stored = await pageA.request.get(`/api/v1/journeys/${journey}/experience`);
  const storedBody = await stored.json();
  expect(
    String(storedBody?.journey?.abort_reason),
    node(NODE, `the refused tap changed the stored reason to "${storedBody?.journey?.abort_reason}"`),
  ).toBe(firstReason);

  const shot = evidencePath("ui-ord0520-glass", "g5-stale-window-refusal.png");
  await pageB.screenshot({ path: shot });
  evidence("g5_refusal_said_on_the_glass.json", {
    arm: NODE,
    journeyId: journey,
    first_window: { http_status: first.status(), reason: firstReason },
    stale_window: { http_status: second.status(), reason_tried: secondReason, envelope },
    painted_refusal: refusal,
    painted_layer_before_details: beforeDetails,
    token_after_opening_details: token,
    stored_reason_after_the_refused_tap: String(storedBody?.journey?.abort_reason),
    screenshot: shot,
    verdict: "GREEN",
  });
  await contextA.close();
  await contextB.close();
});


test("ord0520g-6-an-empty-reason-is-refused-by-both-doors — 空原因：设备先拦，门的 422 还在", async ({
  browser,
}) => {
  test.setTimeout(240_000);
  const NODE = "G6";
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await context.newPage();
  const stamp = stampOf("g6-");
  const traveler = await newTraveler(page, stamp);
  const journey = await newJourney(page, traveler, `空原因按不下去的趟 ${stamp}`);
  await enter(page, journey, "/v8/trips");
  await openItineraryTab(page, "the empty-reason walk opened the trip");

  await page.getByTestId("journey-abort-open").click();
  const requests: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes("/abort")) requests.push(req.url());
  });
  await page.getByTestId("journey-abort-submit").click();
  const empty = page.getByTestId("journey-abort-empty-reason");
  await expect(
    empty,
    node(NODE, "the device sent the empty box at the server instead of refusing it here"),
  ).toBeVisible({ timeout: 20_000 });
  expect(requests.length, node(NODE, `the empty box still asked the door: ${requests.join(", ")}`)).toBe(0);
  const paintedEmpty = clean(await empty.innerText());
  expect(paintedEmpty.length > 0, node(NODE, "the empty-reason refusal painted nothing")).toBe(true);

  /* The door is still the authority: the same request, sent honestly, is refused with a name. */
  const answer = await page.request.post(`/api/v1/journeys/${journey}/abort`, {
    data: { reason: "", actor_traveler_id: traveler },
  });
  expect(answer.status(), node(NODE, `the door answered ${answer.status()} to an empty reason`)).toBe(422);
  const envelope = await answer.json();
  expect(String(envelope?.error?.code), node(NODE, `envelope ${JSON.stringify(envelope)}`)).toBe(
    "VALIDATION_ERROR",
  );

  const after = await (await page.request.get(`/api/v1/journeys/${journey}/experience`)).json();
  expect(
    String(after?.journey?.status),
    node(NODE, `the refused write moved the row to ${after?.journey?.status}`),
  ).not.toBe("CANCELLED");
  expect(
    String(after?.journey?.aborted_at ?? ""),
    node(NODE, "the refused write set aborted_at"),
  ).toBe("");

  const shot = evidencePath("ui-ord0520-glass", "g6-empty-reason.png");
  await page.screenshot({ path: shot });
  evidence("g6_empty_reason_refused.json", {
    arm: NODE,
    journeyId: journey,
    device_side_refusal_sentence: paintedEmpty,
    post_requests_from_the_empty_box: requests.length,
    door_answer: { http_status: answer.status(), envelope },
    row_after: { status: String(after?.journey?.status), aborted_at: after?.journey?.aborted_at ?? null, abort_reason: after?.journey?.abort_reason ?? null },
    screenshot: shot,
    verdict: "GREEN",
  });
  await context.close();
});

test("ord0520g-7-the-live-screen-keeps-the-trip-out-after-another-door-moved-status — 别的门把 status 挪走，实时行程也不该把它请回来", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const NODE = "G7";
  const python = process.env.CITYNEXUS_PYTHON ?? "";
  const database = process.env.CITYNEXUS_GLASS_DB ?? "";
  const helper = process.env.CITYNEXUS_STATUS_MOVER ?? "";
  expect(python.length > 0, node(NODE, "CITYNEXUS_PYTHON must name the interpreter that moves the row")).toBe(true);
  expect(database.length > 0, node(NODE, "CITYNEXUS_GLASS_DB must name the sqlite file this run created")).toBe(true);
  expect(helper.length > 0, node(NODE, "CITYNEXUS_STATUS_MOVER must name the helper")).toBe(true);

  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await context.newPage();
  const stamp = stampOf("g7-");
  const traveler = await newTraveler(page, stamp);
  const journey = await newJourney(page, traveler, `被别的门挪走状态的趟 ${stamp}`);
  const first = await enter(page, journey, "/v8/live");
  expect(first?.journey?.live_view?.included, node(NODE, `a fresh trip is not live: ${JSON.stringify(first?.journey?.live_view)}`)).toBe(true);

  const abortAnswer = page.waitForResponse(
    (res) => res.url().includes(`/journeys/${journey}/abort`) && res.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.goto("/v8/trips", { waitUntil: "load", timeout: 45_000 });
  await openItineraryTab(page, "the trip to abort was reopened");
  await page.getByTestId("journey-abort-open").click();
  await page.getByTestId("journey-abort-reason").fill(`挪走状态之前先中止（${stamp}）`);
  await page.getByTestId("journey-abort-submit").click();
  const response = await abortAnswer;
  expect(response.status(), node(NODE, `the abort door answered ${response.status()}`)).toBe(200);

  await page.goto("/v8/live", { waitUntil: "load", timeout: 45_000 });
  await expect(
    page.getByTestId("journey-aborted-notice"),
    node(NODE, "right after the press the live screen did not exclude the trip"),
  ).toBeVisible({ timeout: 30_000 });

  /* The state `select_plan` writes for a journey that holds a plan (backend/app/main.py):
     status moves, the two abort columns stay.  The screen has to keep the trip out anyway. */
  const moved = JSON.parse(
    execFileSync(
      python,
      [helper, "--db", database, "--journey", journey, "--status", "PLANNED"],
      { encoding: "utf-8" },
    ),
  ) as { rowid: number; before: Record<string, unknown>; after: Record<string, unknown> };
  expect(String(moved.after?.status), node(NODE, `the row did not move: ${JSON.stringify(moved.after)}`)).toBe("PLANNED");
  expect(String(moved.after?.aborted_at ?? ""), node(NODE, "the abort moment was not on the row")).not.toBe("");

  const wire = page.waitForResponse(
    (res) => res.url().includes(`/journeys/${journey}/experience`) && res.status() === 200,
    { timeout: 45_000 },
  );
  await page.goto("/v8/live", { waitUntil: "load", timeout: 45_000 });
  const wireAnswer = await wire;
  const payload = (await wireAnswer.json()) as any;
  expect(String(payload?.journey?.status), node(NODE, `the wire says status=${payload?.journey?.status}`)).toBe("PLANNED");
  expect(
    payload?.journey?.live_view?.included,
    node(NODE, `the server put a called-off trip back on the live screen: ${JSON.stringify(payload?.journey?.live_view)}`),
  ).toBe(false);
  await expect(
    page.getByTestId("journey-aborted-notice"),
    node(
      NODE,
      `status was moved to PLANNED and the live screen showed live content again -- the judgement followed a column other doors own (${JSON.stringify(moved.after)})`,
    ),
  ).toBeVisible({ timeout: 30_000 });
  expect(
    await page.getByTestId("live-recovery").count(),
    node(NODE, "live content was mounted beside the notice"),
  ).toBe(0);
  const why = clean(await page.getByTestId("journey-aborted-why").innerText());
  expect(why, node(NODE, `the live screen said "${why}"`)).toBe(clean(abortCopy("zh-CN", "abort.liveExcluded")));
  const facts = clean(await page.getByTestId("journey-aborted-facts-reason").innerText());
  expect(facts.includes("挪走状态之前先中止"), node(NODE, `the reason did not read back: "${facts}"`)).toBe(true);

  /* The trip's own entry may not offer the action again either: it reads the same field, and a
     second 中止 button on a trip the server says is called off is a door that was already closed.
     Read on the screen that carries it, not on the live one. */
  await page.goto("/v8/trips", { waitUntil: "load", timeout: 45_000 });
  const panel = await openItineraryTab(page, "the trip whose status was moved was reopened");
  const storedAfterMove = clean(await page.getByTestId("journey-abort-stored-reason").innerText());
  expect(
    storedAfterMove.includes("挪走状态之前先中止"),
    node(NODE, `the entry lost the stored reason once status moved: "${storedAfterMove}"`),
  ).toBe(true);
  expect(
    await panel.getByTestId("journey-abort-submit").count(),
    node(NODE, "the entry offered 中止 again after the row's status was moved -- it re-derived the state from status"),
  ).toBe(0);
  const stateBadge = clean(await page.getByTestId("journey-abort-badge").innerText());
  expect(stateBadge, node(NODE, `the entry said "${stateBadge}" for a trip that was called off`)).toBe(
    clean(abortCopy("zh-CN", "abort.stateOnScreen")),
  );

  const shot = evidencePath("ui-ord0520-glass", "g7-status-moved-still-excluded.png");
  await page.screenshot({ path: shot });
  evidence("g7_status_move_keeps_it_out.json", {
    arm: NODE,
    journeyId: journey,
    rowid: moved.rowid,
    database_basename: database.split(/[\\/]/).pop(),
    row_before_move: moved.before,
    row_after_move: moved.after,
    wire_after_move: { status: payload?.journey?.status, live_view: payload?.journey?.live_view ?? null },
    painted_notice: why,
    painted_facts: facts,
    live_content_nodes: 0,
    trips_entry_after_move: {
      painted_stored_reason: storedAfterMove,
      painted_state_badge: stateBadge,
      second_action_count: 0,
    },
    screenshot: shot,
    verdict: "GREEN",
  });
  await context.close();
});