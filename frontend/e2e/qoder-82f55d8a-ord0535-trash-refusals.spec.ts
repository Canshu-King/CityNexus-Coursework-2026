/**
 * ORD-0535 residual (lane 82f55d8a, self-declared) -- the recycle-bin screen's three refusals have
 * never been read off a screen.
 *
 * What is on the line today: the three refusal states are pinned by backend pytest
 * (`tests/test_qoder_ord0399_journey_undelete.py`, `test_qoder_ord0383_undelete_write_lock.py`), and
 * the only front-end file that touches this surface at all is ORD-0509's contrast enumeration
 * (`qoder-ord0509-contrast-screens.spec.ts`), which lists test ids without asserting a sentence.
 * So "the traveller is *told* which condition refused" had no evidence on the painted layer:
 *   - a stale row tapped after somebody else brought the trip back -> 409 `JOURNEY_NOT_DELETED`;
 *   - a row whose trip is no longer on this server -> 404 `JOURNEY_NOT_FOUND`;
 *   - an empty bin versus a bin the server did not answer -- ORD-0402 slice 2's own rule that these
 *     must not look alike.
 *
 * Arms read rendered text (never a `data-*` mirror), and each one names its own lever:
 *   T1 a real stale-list tap: the trip is brought back through the product API while the row is on
 *      screen, then the row's own 找回 button is pressed. Nothing is stubbed.
 *   T2 the 404 sentence, replaying the envelope this server itself answered -- captured by asking
 *      the real door for a journey id that does not exist, then handing that exact body back for the
 *      row's request. The wording is judged; the body is not invented here.
 *   T3 empty versus unreadable, both on the same screen, plus the row's action count (there is no
 *      permanent-delete door, so a row may not grow a second action).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-82f55d8a-ord0535-trash-refusals.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "dev";
const clean = (value: string | null | undefined) => String(value ?? "").replace(/\s+/g, " ").trim();

async function makeJourney(page: Page, travelerId: string, title: string): Promise<string> {
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
        stay_budget: { amount_minor: 70000, currency: "CNY" },
        party_size: 2,
        occupancy: { adults: 2, rooms: 1 },
        arrival_anchor: { at: "2026-10-09T18:00:00+08:00", location: "Tianjin West Station" },
        departure_anchor: { at: "2026-10-11T15:00:00+08:00", location: "Tianjin Binhai Airport" },
      },
    })
  ).json();
  return String(body.id);
}

async function openTrash(page: Page, sessionJourneyId: string) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, sessionJourneyId);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/trash", { waitUntil: "load", timeout: 30_000 });
  await expect(
    page.getByTestId("journey-trash-panel"),
    node("boot", "the recycle-bin panel never rendered"),
  ).toBeVisible({ timeout: 30_000 });
}

async function newTraveler(page: Page, tag: string): Promise<string> {
  const body = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0535 refusals " + tag, locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  return String(body.id);
}

function rowOf(page: Page, journeyId: string) {
  return page.locator(`[data-testid="trash-row"][data-cnx-journey="${journeyId}"]`);
}

async function outcomeOf(page: Page): Promise<{ kind: string | null; text: string }> {
  const box = page.getByTestId("trash-outcome");
  await expect(
    box,
    node("outcome", "no outcome box appeared, so the screen answered the refusal with silence"),
  ).toBeVisible({ timeout: 20_000 });
  await expect
    .poll(async () => clean(await box.getAttribute("data-cnx-outcome")), { timeout: 20_000 })
    .not.toBeNull();
  return {
    kind: await box.getAttribute("data-cnx-outcome"),
    text: clean(await page.getByTestId("trash-outcome-text").innerText()),
  };
}

test("ord0535t-1-a-stale-row-says-it-was-already-brought-back — 陈旧那一行的拒绝要说得出条件", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535t-1";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await newTraveler(page, stamp);
  const session = await makeJourney(page, traveler, `在场 ${stamp}`);
  const target = await makeJourney(page, traveler, `会被别人先找回 ${stamp}`);
  await page.request.delete(`/api/v1/journeys/${target}`);

  await openTrash(page, session);
  const row = rowOf(page, target);
  await expect(row, node(NODE, "the binned trip drew no row to tap")).toBeVisible({ timeout: 20_000 });

  /* Somebody else brings it back while this row is on the screen -- the stale-list state ORD-0402
     named, reached with no stubbing at all. */
  const raced = await page.request.post(`/api/v1/journeys/${target}/undelete`, { data: {} });
  expect(
    raced.status(),
    node(NODE, "the race's own 找回 did not answer 200, so no stale row was produced"),
  ).toBe(200);

  await row.getByTestId("trash-bring-back").click();
  const outcome = await outcomeOf(page);
  expect(
    outcome.kind,
    node(NODE, `the row claimed "${String(outcome.kind)}" for a trip the server had already brought back`),
  ).toBe("already");
  expect(
    outcome.text,
    node(NODE, `the refusal was said as "${outcome.text}"`),
  ).toContain("这条已经在你的行程列表里了");
  expect(
    outcome.text.includes("已找回"),
    node(NODE, `the screen reported a success it did not have: "${outcome.text}"`),
  ).toBe(false);

  /* The machine token stays on the details face: absent while closed, quotable once opened. */
  const closedScan = await scanScreenText(page);
  expect(
    closedScan.visual.includes("JOURNEY_NOT_DELETED"),
    node(NODE, "the server's token reached the painted layer without the traveller opening anything"),
  ).toBe(false);
  /* The outcome box carries two folds on purpose -- the condition's own token, and the measured-facts
     instrument inside it -- so this is scoped to the direct one rather than "any details descendant". */
  const fold = page.getByTestId("trash-outcome").locator("details.cn-trash-details");
  await expect(fold, node(NODE, "the refusal has no details face to quote the condition from")).toHaveCount(1);
  await fold.locator("> summary").click();
  const openedText = clean(await fold.innerText());
  expect(
    openedText.includes("JOURNEY_NOT_DELETED"),
    node(NODE, `opened, the fold reads "${openedText.slice(0, 160)}" and names no code`),
  ).toBe(true);

  writeFileSync(
    evidencePath(`ord0535-refusals/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535 residual",
        node: NODE,
        run: RUN,
        journey: target,
        race_status: raced.status(),
        outcome_kind: outcome.kind,
        outcome_text: outcome.text,
        code_painted_while_closed: closedScan.visual.includes("JOURNEY_NOT_DELETED"),
        fold_text_when_open: openedText.slice(0, 240),
        paintedLeaves: closedScan.visualLeaves,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});

test("ord0535t-2-a-row-whose-trip-is-gone-says-so — 已经不在这台服务器上的那一行要说找不到", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535t-2";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await newTraveler(page, stamp);
  const session = await makeJourney(page, traveler, `在场 ${stamp}`);
  const target = await makeJourney(page, traveler, `行将消失 ${stamp}`);
  await page.request.delete(`/api/v1/journeys/${target}`);

  /* Capture the envelope this door really answers for a journey it does not have, and replay THAT
     body for the row's write: the sentence under test is the screen's, the server's answer is not
     written by this file. */
  const probe = await page.request.post("/api/v1/journeys/00000000-0000-4000-8000-000000000000/undelete", {
    data: {},
  });
  const capturedStatus = probe.status();
  const capturedBody = await probe.text();
  expect(
    capturedStatus,
    node(NODE, `the not-found probe answered ${capturedStatus}, not the 404 this arm is about`),
  ).toBe(404);
  expect(
    capturedBody.includes("JOURNEY_NOT_FOUND"),
    node(NODE, "the real 404 carries no reason code, so the replay would be inventing one"),
  ).toBe(true);

  let replays = 0;
  await page.route(`**/api/v1/journeys/${target}/undelete`, (route) => {
    replays += 1;
    route.fulfill({ status: capturedStatus, contentType: "application/json", body: capturedBody });
  });

  await openTrash(page, session);
  const row = rowOf(page, target);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.getByTestId("trash-bring-back").click();
  const outcome = await outcomeOf(page);
  expect(replays, node(NODE, "the row's write never reached the replayed door")).toBeGreaterThan(0);
  expect(
    outcome.kind,
    node(NODE, `a gone trip was reported as "${String(outcome.kind)}"`),
  ).toBe("gone");
  expect(
    outcome.text,
    node(NODE, `the gone refusal was said as "${outcome.text}"`),
  ).toContain("不在这台服务器上");
  expect(
    outcome.text.includes("已找回"),
    node(NODE, `the screen still claimed a bring-back for a trip that is gone: "${outcome.text}"`),
  ).toBe(false);

  const scan = await scanScreenText(page);
  expect(
    scan.visualLeaves,
    node(NODE, "nothing painted, so the sentence above would be read off an empty screen"),
  ).toBeGreaterThan(20);
  expect(
    scan.visual.includes("JOURNEY_NOT_FOUND"),
    node(NODE, "the 404's token was painted without the fold being opened"),
  ).toBe(false);

  writeFileSync(
    evidencePath(`ord0535-refusals/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535 residual",
        node: NODE,
        run: RUN,
        journey: target,
        captured_status: capturedStatus,
        captured_body_bytes: Buffer.byteLength(capturedBody),
        replays,
        outcome_kind: outcome.kind,
        outcome_text: outcome.text,
        paintedLeaves: scan.visualLeaves,
        token_painted: scan.visual.includes("JOURNEY_NOT_FOUND"),
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});

test("ord0535t-3-empty-and-unanswered-are-not-the-same-sentence — 空站与没答上各说各的", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535t-3";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await newTraveler(page, stamp);
  const session = await makeJourney(page, traveler, `在场 ${stamp}`);

  await openTrash(page, session);
  /* This lane's own journeys may still be binned from an earlier arm, so emptiness is established by
     bringing back everything this traveler's trips left in the bin -- through the product's own door.
     Since ORD-0531 the screen asks the door scoped to a seat, so that is the roster this arm empties. */
  const listed = (await (
    await page.request.get(`/api/v1/travelers/${traveler}/recycle-bin/journeys`)
  ).json()) as Array<{ id: string }>;
  for (const row of listed) await page.request.post(`/api/v1/journeys/${row.id}/undelete`, { data: {} });
  await page.reload({ waitUntil: "load" });
  const emptyLine = page.getByTestId("trash-empty");
  await expect(emptyLine, node(NODE, "the bin did not answer an empty station")).toBeVisible({ timeout: 20_000 });
  const emptyText = clean(await emptyLine.innerText());
  const countWhenEmpty = await page.getByTestId("trash-count").count();
  const rowCountWhenEmpty = await page.getByTestId("trash-row").count();

  /* Now the door itself is refused, and the screen must say *that* instead. The pattern covers both
     bin doors on purpose: which one this screen asks is the product's business, and an arm that
     intercepts only one of them would silently watch nothing at all -- which is how this arm went red
     after ORD-0531 moved the read to the scoped door. `asked` names the door that was actually asked. */
  let refused = 0;
  const asked: string[] = [];
  const BIN_DOOR = /\/api\/v1\/(?:travelers\/[^/]+\/)?recycle-bin\/journeys$/;
  await page.route(BIN_DOOR, (route) => {
    refused += 1;
    asked.push(new URL(route.request().url()).pathname);
    route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "gone" }) });
  });
  await page.reload({ waitUntil: "load" });
  /* The panel asks after the document loads, so this is polled rather than read once: a single read
     here would be a race this arm would lose against its own page. The plain assertion after it keeps
     the sentence this arm prints when a reader changes which door the screen asks. */
  await expect
    .poll(() => refused, { timeout: 20_000, message: node(NODE, "no bin door was asked at all, so the sentence below proves nothing") })
    .toBeGreaterThan(0);
  expect(refused, node(NODE, "no bin door was asked at all, so the sentence below proves nothing")).toBeGreaterThan(0);
  const failedLine = page.getByTestId("trash-read-failed");
  await expect(failedLine, node(NODE, `an unanswered bin did not say so; the doors asked were ${asked.join(", ")}`)).toBeVisible({ timeout: 20_000 });
  const failedText = clean(await failedLine.innerText());
  expect(
    failedText,
    node(NODE, `the unanswered bin said "${failedText}"`),
  ).toContain("没有答出");
  expect(
    failedText === emptyText,
    node(NODE, `empty and unanswered share one sentence: "${failedText}"`),
  ).toBe(false);
  expect(
    /空的|没有报出/.test(failedText),
    node(NODE, `the unanswered state borrowed the empty station's words: "${failedText}"`),
  ).toBe(false);

  /* A row may not offer what this server cannot do: on a rendered row there is exactly one action. */
  const binned = await makeJourney(page, traveler, `只有一枚动作 ${stamp}`);
  await page.unroute(BIN_DOOR);
  await page.request.delete(`/api/v1/journeys/${binned}`);
  await page.reload({ waitUntil: "load" });
  const row = rowOf(page, binned);
  await expect(row, node(NODE, "the binned trip drew no row to count actions on")).toBeVisible({ timeout: 20_000 });
  const actions = await row.locator("button").count();
  expect(
    actions,
    node(NODE, `a recycle-bin row offers ${actions} actions; there is no permanent-delete door on this server`),
  ).toBe(1);
  const scan = await scanScreenText(page);
  expect(
    scan.visualLeaves,
    node(NODE, "empty denominator: nothing was painted on the row arm"),
  ).toBeGreaterThan(20);

  writeFileSync(
    evidencePath(`ord0535-refusals/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535 residual",
        node: NODE,
        run: RUN,
        emptied_rows_before: listed.length,
        empty_text: emptyText,
        count_line_when_empty: countWhenEmpty,
        rows_when_empty: rowCountWhenEmpty,
        refused_reads: refused,
        doors_asked: asked,
        unanswered_text: failedText,
        actions_on_row: actions,
        paintedLeaves: scan.visualLeaves,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});
