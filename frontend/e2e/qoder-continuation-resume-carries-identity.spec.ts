/**
 * ORD-0278 -- a continuation press carries the identity of the round it resumes.
 *
 * One positive control, three negative controls, exactly as ordered:
 *
 *  POSITIVE  marker present + the feed can point at the same mission => the resume gate appears, and
 *            the request that actually leaves carries `mission_id` in the BODY and
 *            `Idempotency-Key` in the HEADER -- two keys, never merged, both read off the real
 *            request (never off the source), and the resume key differs from the interrupted run's
 *            key because it is a different intent.
 *  NEGATIVE1 no marker => no resume gate, and no second-leg request at all (proven over repeated
 *            re-entries, not one, so a pass cannot be an accident of navigation).
 *  NEGATIVE2 the marker's mission is off the served page => the named "cannot confirm" state, no
 *            resume gate, no second-leg request.  Timestamps are never read as an association.
 *  NEGATIVE3 the marker is not cleared ahead of the send: while the second leg is refused or is
 *            still in flight, the record survives and the screen says which of the two happened --
 *            with the server's own words when it sent any, and an explicit "this sentence is ours"
 *            when it sent none.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-continuation-resume-carries-identity.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const MISSION_ID = "11111111-2222-4333-8444-555555555555";
const RESUME_LABEL = "继续这次规划";
const UNCONFIRMABLE_TEXT = "上次规划有记录，但现在读不到它的进度，无法确认进行到哪一步。";
const RESUMED_TEXT = "已接着上次把第二步发出去，但这一轮没有产出新方案。";
const REFUSED_TEXT = "服务器拒绝了这一次尝试。没有产生新东西，上次的记录还在，你可以再试一次。";
/** The server's own contract, quoted from `commerce/validators.py::validate_idempotency_key`. */
const KEY_MIN = 8;
const KEY_MAX = 128;

type Sent = { body: Record<string, unknown>; key: string | null };

async function stack(page: Page, options: { includeMissionOnPage: boolean; generate?: (sent: Sent) => Promise<void> | void }) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((id) => {
    if (localStorage.getItem("citynexus.storage-seeded") !== "1") {
      localStorage.clear();
      localStorage.setItem("citynexus.storage-seeded", "1");
    }
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);

  const events = options.includeMissionOnPage
    ? [
        {
          event_type: "agent.mission.created",
          entity_id: MISSION_ID,
          facts: { mission_id: MISSION_ID, event_id: null, requested_autonomy: "EXECUTE_WITHIN_BOUNDS" },
          summary: "Agent.mission.created",
          timestamp: "2026-09-25T00:00:00+00:00",
        },
      ]
    : [];
  await page.route("**/api/v1/journeys/*/agent-feed*", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ journey_id: journeyId, source: "stub", events }),
    }),
  );
  let leg1Key: string | null = null;
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    leg1Key = route.request().headers()["idempotency-key"] ?? null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mission: { id: MISSION_ID, status: "COMPLETED", failure_reason: null, reasoning_summary: null } }),
    });
  });
  await page.route("**/api/v1/journeys/*/plans", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }),
  );
  const sent: Sent[] = [];
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) => {
    const entry: Sent = {
      body: JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>,
      key: route.request().headers()["idempotency-key"] ?? null,
    };
    sent.push(entry);
    if (options.generate) await options.generate(entry);
    else await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify([]) });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  return { journeyId, sent, leg1Key: () => leg1Key };
}

/** Leg 1 through the sheet, so the marker is written by the product and not by the test. */
async function runInterruptedLeg1(page: Page) {
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景，别排太满");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result")).toBeVisible({ timeout: 90_000 });
  await expect.poll(() => page.getByTestId("plan-ai-cancel").count(), { timeout: 30_000 }).toBe(0);
  await page.getByTestId("plan-ai-dismiss").click();
}

test("ord0278-positive-the-resume-request-carries-both-keys — 续发那一发真的带着两枚键", async ({ page }) => {
  test.setTimeout(300_000);
  const { journeyId, sent, leg1Key } = await stack(page, { includeMissionOnPage: true });
  await runInterruptedLeg1(page);

  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  const gate = page.getByTestId("continuation-resume");
  await expect(gate, node("ord0278-positive", "the resume gate never appeared with a marker and a matching round")).toBeVisible({ timeout: 15_000 });
  expect((await gate.innerText()).trim(), node("ord0278-positive", "the resume gate has no label")).toBe(RESUME_LABEL);

  await gate.click();
  await expect
    .poll(() => sent.length, { timeout: 20_000, message: node("ord0278-positive", "pressing resume sent no second-leg request") })
    .toBeGreaterThan(0);
  const request = sent[0];

  // Business identity: whose batch this is, in the body.
  expect(
    typeof request.body.mission_id === "string" && request.body.mission_id === MISSION_ID,
    node("ord0278-positive", `the real body carries mission_id=${JSON.stringify(request.body.mission_id)} -- expected the recorded ${MISSION_ID}`),
  ).toBe(true);
  expect(request.body.candidate_count, node("ord0278-positive", `a resumed round changed the request shape (candidate_count=${request.body.candidate_count})`)).toBe(3);

  // Transport identity: is this the same send, in the header.
  expect(request.key, node("ord0278-positive", "the real request carried no Idempotency-Key header")).not.toBeNull();
  const key = String(request.key);
  expect(
    key.length >= KEY_MIN && key.length <= KEY_MAX,
    node("ord0278-positive", `the resume key is ${key.length} chars -- outside the server's ${KEY_MIN}..${KEY_MAX} rule`),
  ).toBe(true);

  // Two keys, never merged: the resume is a NEW intent, so it cannot reuse the interrupted run's key,
  // and it must not be the mission id wearing a different name.
  expect(
    key !== MISSION_ID,
    node("ord0278-positive", "the transport key is just the mission id re-labelled -- the two keys were merged into one name"),
  ).toBe(true);
  expect(
    key !== leg1Key(),
    node("ord0278-positive", `the resume reused the interrupted run's key (${key}) -- a new press is a new intent`),
  ).toBe(true);

  // A visible outcome, and only then does the record leave.
  const line = page.getByTestId("continuation-resume-outcome");
  await expect(line, node("ord0278-positive", "the resume produced no painted outcome")).toBeVisible({ timeout: 20_000 });
  expect(
    (await line.innerText()).replace(/\s+/g, " ").trim(),
    node("ord0278-positive", "the painted resume outcome is not the accepted-empty sentence"),
  ).toBe(RESUMED_TEXT);
  await expect
    .poll(
      async () =>
        await page.evaluate((jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`) === null, journeyId),
      { timeout: 15_000, message: node("ord0278-positive", "the marker survived an accepted outcome") },
    )
    .toBe(true);
  await expect(page.getByTestId("continuation-pending"), node("ord0278-positive", "the card is still up after its own round was resumed")).toHaveCount(0);

  writeFileSync(
    evidencePath("ord0278-positive-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0278-positive",
        journeyId,
        bodyKeys: Object.keys(request.body).sort(),
        bodyMissionId: request.body.mission_id,
        headerKeyLength: key.length,
        headerKeyIsNotTheMissionId: key !== MISSION_ID,
        differsFromInterruptedRunKey: key !== leg1Key(),
        paintedOutcome: (await line.innerText()).replace(/\s+/g, " ").trim(),
        markerClearedAfterAcceptedOutcome: true,
        readFrom: "the request object Playwright actually observed -- not the source file",
      },
      null,
      1,
    ),
  );
});

test("ord0278-negative-no-marker-sends-nothing — 没有 marker 就没有第二腿", async ({ page }) => {
  test.setTimeout(300_000);
  const { sent } = await stack(page, { includeMissionOnPage: true });
  // No leg 1 at all: nothing was ever interrupted on this device.
  for (let round = 0; round < 3; round += 1) {
    await page.reload({ waitUntil: "load" });
    await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("continuation-pending")).toHaveCount(0);
    await expect(page.getByTestId("continuation-resume")).toHaveCount(0);
  }
  expect(sent.length, node("ord0278-negative-no-marker", `${sent.length} second-leg requests were sent without a marker`)).toBe(0);

  writeFileSync(
    evidencePath("ord0278-negative-no-marker-reading.json"),
    JSON.stringify(
      { generatedBy: "e345b9d6", node: "ord0278-negative-no-marker", reentries: 3, resumeGatesSeen: 0, secondLegPosts: sent.length },
      null,
      1,
    ),
  );
});

test("ord0278-negative-off-page-sends-nothing — 翻不到那一轮就不许猜", async ({ page }) => {
  test.setTimeout(300_000);
  const { journeyId, sent } = await stack(page, { includeMissionOnPage: false });
  await runInterruptedLeg1(page);

  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  const line = page.getByTestId("continuation-unconfirmable");
  await expect(line, node("ord0278-negative-off-page", "an off-page round stayed silent instead of naming itself")).toBeVisible({ timeout: 15_000 });
  expect(
    (await line.innerText()).replace(/\s+/g, " ").trim(),
    node("ord0278-negative-off-page", "the off-page shape painted a different sentence than the named one"),
  ).toBe(UNCONFIRMABLE_TEXT);
  await expect(page.getByTestId("continuation-pending")).toHaveCount(0);
  await expect(page.getByTestId("continuation-resume")).toHaveCount(0);
  expect(sent.length, node("ord0278-negative-off-page", "the screen guessed a resumable round it could not see")).toBe(0);
  const marker = await page.evaluate((jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`), journeyId);
  expect(marker, node("ord0278-negative-off-page", "an unreadable join erased the durable record -- that is not this screen's to do")).not.toBeNull();

  writeFileSync(
    evidencePath("ord0278-negative-off-page-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0278-negative-off-page",
        journeyId,
        paintedLine: (await line.innerText()).replace(/\s+/g, " ").trim(),
        resumeGatesSeen: 0,
        secondLegPosts: sent.length,
        markerStillPersisted: String(marker).length,
      },
      null,
      1,
    ),
  );
});

test("ord0278-negative-refused-keeps-the-marker-and-says-whose-words-these-are — 被拒不清 marker，句子分得清是谁说的", async ({ page }) => {
  test.setTimeout(300_000);
  const { journeyId, sent } = await stack(page, { includeMissionOnPage: true });
  // This node installs its own refusal route for each read below, so the shared stack's default
  // answer is never used -- it only has to exist for leg 1 to complete.
  await runInterruptedLeg1(page);

  /* Read A -- the server refuses AND gives a sentence: the label "服务器给出的原因" may only ever hold
     text that came from the server. */
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) => {
    sent.push({ body: JSON.parse(route.request().postData() ?? "{}"), key: route.request().headers()["idempotency-key"] ?? null });
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "another request is still writing this journey" }),
    });
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  const postsBeforeA = sent.length;
  await page.getByTestId("continuation-resume").click();
  const outcome = page.getByTestId("continuation-resume-outcome");
  await expect(outcome, node("ord0278-negative-refused", "a refused resume painted no outcome")).toBeVisible({ timeout: 20_000 });
  const postsAfterA = sent.length;
  const refusedText = (await outcome.innerText()).replace(/\s+/g, " ").trim();
  expect(refusedText, node("ord0278-negative-refused", `the refusal sentence reads "${refusedText}"`)).toContain(REFUSED_TEXT);
  /* Q76, carried by ORD-0341: ORD-0344-b moved the server's own sentence into the technical-detail
     fold, and the fold stays closed -- a gate may not ask the product to leave it open just so the
     gate reads green.  So this reads the two shapes a traveller actually meets, in order: closed
     (the label is not on screen, and the `<details>` really is closed), then opened by clicking the
     real `<summary>`, where the label must be holding the server's words.  Operating the UI is not
     rewriting the assertion; both readings are written to the evidence file. */
  const fold = page.getByTestId("continuation-technical-details");
  const serverLine = page.getByTestId("continuation-resume-server-reason");
  const collapsedOpen = await fold.evaluate((el) => (el as HTMLDetailsElement).open);
  const collapsedVisible = await serverLine.isVisible();
  expect(
    collapsedOpen,
    node("ord0278-negative-refused", "the technical fold is open before anything touched it -- Q76 says it stays closed"),
  ).toBe(false);
  expect(
    collapsedVisible,
    node("ord0278-negative-refused", "the server's sentence sits on the traveller's line while the fold is closed"),
  ).toBe(false);
  await page.getByTestId("continuation-technical-toggle").click();
  const openedFold = await fold.evaluate((el) => (el as HTMLDetailsElement).open);
  expect(
    openedFold,
    node("ord0278-negative-refused", "clicking 「技术详情」 did not open the fold -- the label below has no witness"),
  ).toBe(true);
  await expect(
    serverLine,
    node("ord0278-negative-refused", "the fold is open and still no reason from the server is shown"),
  ).toBeVisible();
  /* Captured NOW, into a value: by the time the reading block at the bottom runs, read B has replaced
     this outcome and re-querying the locator would wait the whole test timeout on a vanished node. */
  const serverReasonShown = (await serverLine.innerText()).replace(/\s+/g, " ").trim();
  expect(
    serverReasonShown.includes("another request is still writing this journey"),
    node("ord0278-negative-refused", "the label is not holding the server's own words"),
  ).toBe(true);
  expect(
    /HTTP\s?\d\d\d/.test(refusedText),
    node("ord0278-negative-refused", `a client-written HTTP status appears under the server's own label: "${refusedText}"`),
  ).toBe(false);
  expect(
    await page.evaluate((jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`) !== null, journeyId),
    node("ord0278-negative-refused", "a refused attempt erased the record -- clause 5 says a refusal is not an outcome"),
  ).toBe(true);
  await expect(page.getByTestId("continuation-pending"), node("ord0278-negative-refused", "the card vanished although nothing was produced")).toBeVisible();

  /* Read B -- the server refuses with no sentence at all: the screen must say that plainly, in its
     own words, instead of inventing a status string under the server's label. */
  await page.unroute("**/api/v1/journeys/*/plans/generate");
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) => {
    sent.push({ body: JSON.parse(route.request().postData() ?? "{}"), key: route.request().headers()["idempotency-key"] ?? null });
    await route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" });
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  const postsBeforeB = sent.length;
  await page.getByTestId("continuation-resume").click();
  await expect(outcome, node("ord0278-negative-refused", "the no-sentence refusal painted nothing")).toBeVisible({ timeout: 20_000 });
  const bareLine = page.getByTestId("continuation-resume-no-server-reason");
  await expect(bareLine, node("ord0278-negative-refused", "with no server sentence the screen still claimed one")).toBeVisible();
  expect(
    sent.length - postsBeforeB,
    node("ord0278-negative-refused", `read B sent ${sent.length - postsBeforeB} second-leg requests, expected exactly 1`),
  ).toBe(1);
  const bare = (await outcome.innerText()).replace(/\s+/g, " ").trim();
  expect(
    /HTTP\s?\d\d\d/.test(bare),
    node("ord0278-negative-refused", `the screen wrote its own status into the failure sentence: "${bare}"`),
  ).toBe(false);
  expect(
    await page.evaluate((jid) => localStorage.getItem(`citynexus.v8.continuation.${jid}`) !== null, journeyId),
    node("ord0278-negative-refused", "a 500 with no body erased the record"),
  ).toBe(true);

  expect(
    postsAfterA - postsBeforeA,
    node("ord0278-negative-refused", `read A sent ${postsAfterA - postsBeforeA} second-leg requests, expected exactly 1`),
  ).toBe(1);

  writeFileSync(
    evidencePath("ord0278-negative-refused-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0278-negative-refused",
        journeyId,
        refusedSentence: refusedText,
        serverReasonShown,
        fold_collapsed: { details_open: collapsedOpen, reason_visible: collapsedVisible },
        fold_opened_by_click: { details_open: openedFold, reason_visible: true, reason_text: serverReasonShown },
        noSentenceOutcome: bare,
        secondLegPosts: sent.length,
        markerAfterEachRefusal: "present",
      },
      null,
      1,
    ),
  );
});
