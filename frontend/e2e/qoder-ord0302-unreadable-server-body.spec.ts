/**
 * ORD-0302 — a server answer this screen cannot parse must stay an HTTP answer, not become a network claim.
 *
 * Root cause, read off `dc964086c9d412573ac19d70b24b30e6c3ee6168` before touching anything: the two envelope
 * helpers read `body.detail` with a cast but no optional chaining (`:147`, `:155`), while the leg that owns
 * them sets `body = null` for a non-JSON answer (`:531`).  So a genuine `502 Bad Gateway` from an HTML
 * proxy threw *inside* the `!response.ok` branch (`:539`), the throw was swallowed by the sheet's big catch
 * (`:738`) and that catch wrote `status: "NETWORK", failureReason: null` (`:748`) -- which both blames the
 * network for a server answer and, because 技术详情 is gated on `failureReason || summary` (`:936`), removes
 * the one panel that could have explained it.  The 0280 arms missed it because every one of them answered
 * with JSON: the only quantity that distinguishes these states is whether the body holds a JSON object,
 * which is neither the status code nor the elapsed time (Q25).
 *
 * Three arms, one per claim in the order:
 *   N1  non-JSON 500  -> status stays HTTP_500, the block still renders, and it says this device has no
 *                       server wording (never 「服务器给出的原因」, never 「连不上服务端」).
 *   N2  positive control: a 4xx that DOES carry `error.message` -> still green after the fix: the server's
 *                       sentence is shown verbatim under the attribution label (`==`, not `in`).
 *   N3  a real network failure (the request is aborted) -> NETWORK, and ② is what this measures: the detail
 *                       block is still there, carrying a device-composed line.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0302-unreadable-server-body.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();

const SERVER_LABEL_ZH = "服务器给出的原因";
const SERVER_LABEL_EN = "Reason the server gave";
const NETWORK_COPY = "连不上服务端";
const USER_SENTENCE = "帮我排三天，喜欢美食和特色景点。";
const SERVER_SENTENCE = "该目的地的候选目录尚未就绪，请稍后重试。";

type Force = { status: number; contentType: string; body: string } | { abort: true };
type ArmId = "N1" | "N2" | "N3";
const readings: Record<string, unknown> = {};

async function bootAndAsk(page: Page, force: Force, arm: ArmId) {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const journeyId = String(scene.journey_id ?? "");
  expect(journeyId.length > 0, node(arm, "the stage named no journey")).toBe(true);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });

  const seen: string[] = [];
  await page.route("**/api/v1/journeys/*/plans", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    seen.push("GET /plans(baseline)");
    await r.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    seen.push("POST /agent/commands");
    if ("abort" in force) return r.abort();
    await r.fulfill({ status: force.status, contentType: force.contentType, body: force.body });
  });

  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(arm, "the hub never rendered")).toBeVisible({
    timeout: 30_000,
  });
  await page.getByTestId("hub-plan-with-ai").click();
  await page.getByTestId("plan-ai-input").fill(USER_SENTENCE);
  await page.getByTestId("plan-ai-submit").click();
  const result = page.getByTestId("plan-ai-result");
  await expect(result, node(arm, "the sheet never answered")).toBeVisible({ timeout: 120_000 });

  const cardText = clean(await result.innerText());
  const status = (await result.getAttribute("data-result-status")) ?? "";
  /* `count()` first: on the buggy tree the block is not in the DOM at all, and waiting for it would hang for
     30 s and then read as a timeout instead of the finding it is. */
  const details = page.getByTestId("plan-ai-detail-toggle");
  const blockAttached = (await details.count()) > 0;
  let label = "";
  let origin = "";
  let reason = "";
  if (blockAttached) {
    await details.click();
    label = clean(await page.getByTestId("plan-ai-detail-kind").innerText());
    origin = (await page.getByTestId("plan-ai-detail-kind").getAttribute("data-reason-origin")) ?? "";
    reason = clean(await page.getByTestId("plan-ai-reason").innerText());
  }
  return { arm, status, detail_block_attached: blockAttached, label, origin, reason, card_text: cardText,
    legs: seen };
}

function decide(arm: ArmId, read: Awaited<ReturnType<typeof bootAndAsk>>): { verdict: string; rows: unknown[] } {
  const rows: { claim: string; ok: boolean; got: unknown }[] = [];
  const add = (claim: string, ok: boolean, got: unknown) => rows.push({ claim, ok, got });
  if (arm === "N1") {
    add("an unreadable answer stays HTTP_500 and never becomes NETWORK", read.status === "HTTP_500",
      { status: read.status });
    add("the technical-detail block is still in the DOM", read.detail_block_attached,
      { detail_block_attached: read.detail_block_attached });
    add("the reading is labelled as this device's, not the server's",
      read.origin === "device" && read.label !== SERVER_LABEL_ZH && read.label !== SERVER_LABEL_EN,
      { label: read.label, origin: read.origin });
    add("the card does not blame the network", !read.card_text.includes(NETWORK_COPY),
      { card_head: read.card_text.slice(0, 40) });
    add("a device-composed line is still shown (this is not a mute-the-error fix)",
      read.reason.length > 0, { reason: read.reason });
  } else if (arm === "N2") {
    add("a refusal that carries the server's sentence still renders under the attribution label",
      read.label === SERVER_LABEL_ZH && read.origin === "server", { label: read.label, origin: read.origin });
    add("the server's words are passed through verbatim (equality, not containment)",
      read.reason === clean(SERVER_SENTENCE), { reason: read.reason, expected: clean(SERVER_SENTENCE) });
    add("status is the HTTP answer it is", read.status === "HTTP_422", { status: read.status });
  } else {
    add("a request that really never got an answer is NETWORK", read.status === "NETWORK",
      { status: read.status });
    add("ORD-0302 ②: even then the detail block is still there", read.detail_block_attached,
      { detail_block_attached: read.detail_block_attached });
    add("and it carries a device line rather than nothing",
      read.reason.length > 0 && read.origin === "device", { reason: read.reason, origin: read.origin });
  }
  const bad = rows.filter((r) => !r.ok);
  return { verdict: bad.length === 0 ? "MET" : "NOT_MET", rows: rows.map((r) => ({ ...r, ok: r.ok })) ,
    ...(bad.length ? { failed: bad.map((b) => b.claim) } : {}) } as { verdict: string; rows: unknown[] };
}

const ARMS: { id: ArmId; force: Force }[] = [
  { id: "N1", force: { status: 500, contentType: "text/html; content-type=text/plain",
                       body: "<html><body>502 Bad Gateway</body></html>" } },
  { id: "N2", force: { status: 422, contentType: "application/json",
                       body: JSON.stringify({ error: { code: "request.invalid", message: SERVER_SENTENCE,
                         message_key: "request.invalid", details: null } }) } },
  { id: "N3", force: { abort: true } },
];

for (const { id, force } of ARMS) {
  test(`ord0302-${id} ${id === "N1" ? "a non-JSON 5xx stays an HTTP answer with a visible device reading"
    : id === "N2" ? "a 4xx with the server's sentence still quotes it verbatim (positive control)"
      : "a genuine network failure keeps NETWORK and still renders the detail block"}`, async ({ page }) => {
    const read = await bootAndAsk(page, force, id);
    const { verdict, rows } = decide(id, read);
    readings[id] = { want: id === "N2" ? "server" : "device", force_shape: force, read, verdict, rows };
    expect(rows.filter((r: { ok: boolean }) => !r.ok), node(id, JSON.stringify({ verdict, read }))).toEqual([]);
  });
}

/* -------------------------------------------------------------------------------------------- X-5 arms */
/*
 * ORD-0302's second named cell (commander 16:0x): the `NO_PLANS_PRODUCED` card text sends the traveller to
 * 技术详情, and on the deployed generation that block was not in the DOM at all -- a promise pointing at
 * nothing.  Q53=① chose the fix "render the raw reading", not "soften the sentence", so the gate here is the
 * pairing itself, read both ways:
 *
 *   promise in the card's own sentence  <=>  <details> count >= 1
 *
 * X1 drives the shape that used to break (a run that compiled, stored no reason, and left this screen with
 * nothing usable) and requires the block to exist AND to carry a reading.  X2 is the other side: a status
 * whose copy makes no promise (the baseline could not be read) may keep the block absent without lying.
 * Both read RENDERED text and DOM counts, never a `data-*`; the promise is read out of the card's `<p>` and
 * not out of the whole card, because the block's own summary says 技术详情 and would hide a deleted promise.
 */
type Stub = { url: string; method?: string; status?: number; body?: unknown; html?: string;
                  raw?: string; contentType?: string; abort?: boolean; delay?: number };

async function bootWithStubs(page: Page, stubs: Stub[], arm: string, cancelMidFlight = false) {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const journeyId = String(scene.journey_id ?? "");
  expect(journeyId.length > 0, node(arm, "the stage named no journey")).toBe(true);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const s of stubs) {
    await page.route(s.url, async (r: Route) => {
      if (s.method && r.request().method() !== s.method) return r.fallback();
      if (s.abort) return r.abort();
      if (s.html !== undefined) {
        return r.fulfill({ status: s.status ?? 200, contentType: "text/html", body: s.html });
      }
      if (s.raw !== undefined) {
        /* The body is handed over verbatim, including "" -- a JSON parse of an empty answer is exactly what
           these shapes are about, so the stub must not quietly replace it with `{}`. */
        return r.fulfill({ status: s.status ?? 200, contentType: s.contentType ?? "application/json",
                           body: s.raw, delay: s.delay });
      }
      return r.fulfill({ status: s.status ?? 200, contentType: "application/json",
                         body: JSON.stringify(s.body) });
    });
  }
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(arm, "the hub never rendered")).toBeVisible({
    timeout: 30_000,
  });
  await page.getByTestId("hub-plan-with-ai").click();
  await page.getByTestId("plan-ai-input").fill(USER_SENTENCE);
  await page.getByTestId("plan-ai-submit").click();
  if (cancelMidFlight) {
    /* The other direction of the pairing needs a screen whose block really is absent, and every answered leg
       now renders a raw reading. A request the traveller cancels is the shape with no answer to show, so the
       absence is measured here rather than assumed. */
    const cancel = page.getByTestId("plan-ai-cancel");
    await expect(cancel, node(arm, "the cancel control never appeared")).toBeVisible({ timeout: 30_000 });
    await cancel.click();
  }
  const result = page.getByTestId("plan-ai-result");
  await expect(result, node(arm, "the sheet never answered")).toBeVisible({ timeout: 120_000 });
  const cardText = clean(await result.innerText());
  /* The promise lives in the sentence, not in the card: `innerText()` of the whole card also returns the
     block's own 「技术详情」 summary label, so a run that deleted the promise from the wording would still
     read as if it made one.  Reading the card's `<p>` is what makes the soft-promise mutant actually bite. */
  const sentence = clean(await result.locator("p").first().innerText());
  const details = page.getByTestId("plan-ai-detail-toggle");
  const count = await details.count();
  let reason = "";
  let label = "";
  let origin = "";
  let rawText = "";
  if (count > 0) {
    await details.first().click();
    const kind = page.getByTestId("plan-ai-detail-kind");
    if (await kind.count() > 0) {
      label = clean(await kind.innerText());
      origin = (await kind.getAttribute("data-reason-origin")) ?? "";
      reason = clean(await page.getByTestId("plan-ai-reason").innerText());
    }
    /* Q53=①: what has to be under the block is the answer's own bytes. Reading them is a separate probe
       because a shape can carry a raw reading and no sentence at all. */
    const raw = page.getByTestId("plan-ai-raw-reading");
    if (await raw.count() > 0) rawText = String(await raw.textContent());
  }
  const rawFields: Record<string, string> = {};
  for (const line of rawText.split("\n")) {
    const at = line.indexOf("=");
    if (at > 0) rawFields[line.slice(0, at)] = line.slice(at + 1);
  }
  return { arm, status: (await result.getAttribute("data-result-status")) ?? "", card_text: cardText,
           sentence, details_count: count, promise_in_copy: sentence.includes("技术详情"), reason, label,
           origin, raw_text: rawText, raw_fields: rawFields };
}

/** What the block must show for a shape we forced: only the fields this device measured (Q64=①). */
function expectedRaw(sh: X5Shape): Record<string, string> {
  return {
    http_status: String(sh.status),
    "content-type": sh.contentType,
    bytes: String(new TextEncoder().encode(sh.body).length),
  };
}
/** The other direction: nothing that was forwarded from the answer's body may be on screen. */
function panelLeaksBody(read: { raw_text: string }, sh: X5Shape): boolean {
  const frag = sh.body.slice(0, 24);
  return frag.length > 0 && read.raw_text.includes(frag);
}

const MISSION_CLEAN = { mission: { id: "ord0302-mission", status: "COMPLETED", failure_reason: null,
                                   reasoning_summary: null } };
/* Two place rows: below two, the sheet stops at its own `<2 places` guard and never reaches the mission
   branch X-5 is about -- the first attempt at this arm proved that by landing on BLOCKED:CATALOG_EMPTY. */
const TWO_PLACES = [{ id: "ord0302-p1", title: "A" }, { id: "ord0302-p2", title: "B" }];
/* The five shapes the commander named (ledger `CMDR-0925-1625`, readings in bus issuecomment-5829345648):
   all five answer HTTP 200 on both write legs and differ only in the body. Each one reaches the branch whose
   card sends the traveller to 技术详情, so the pairing is judged once per shape -- inside ONE cell, which is
   what keeps the two killer mutants reddening exactly this cell. */
/* Q64=① witness: a body that looks like a credential.  Under b4 exactly this string was sliced into the
   panel; under b5 nothing of it may appear, while `bytes` still has to equal its real length. */
const FAKE_TOKEN_BODY =
  "upstream said: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.supersecret-not-for-the-screen";
type X5Shape = { row: string; status: number; contentType: string; body: string };
const X5_SHAPES: X5Shape[] = [
  { row: "R19_④_200_empty_body", status: 200, contentType: "application/json", body: "" },
  { row: "R20_④_200_json_empty_object", status: 200, contentType: "application/json", body: "{}" },
  { row: "R21_④_200_json_array", status: 200, contentType: "application/json", body: "[]" },
  { row: "R22_④_200_text_plain_payload", status: 200, contentType: "text/plain; charset=utf-8",
    body: "plans are not available right now" },
  { row: "R23_④_200_mission_unusable_status", status: 200, contentType: "application/json",
    body: JSON.stringify({ mission: { id: "ord0302-mission", status: "FAILED_UPSTREAM",
                                      failure_reason: null, reasoning_summary: null } }) },
  { row: "R24_Q64_200_credential_shaped_text", status: 200, contentType: "text/plain; charset=utf-8",
    body: FAKE_TOKEN_BODY },
];

function stubsForShape(sh: X5Shape): Stub[] {
  return [
    { url: "**/api/v1/journeys/*/agent/commands", method: "POST", status: sh.status,
      contentType: sh.contentType, raw: sh.body },
    { url: "**/api/v1/journeys/*/plans/generate", method: "POST", status: sh.status,
      contentType: sh.contentType, raw: sh.body },
    { url: "**/api/v1/journeys/*/plans", method: "GET", body: [] },
    { url: "**/api/v1/places**", body: TWO_PLACES },
  ];
}

test(`ord0302-X1 ${"every 200 shape whose card points at 技术详情 renders a block that carries a reading"}`,
  async ({ page }) => {
    test.setTimeout(900_000);
    const per: Record<string, unknown> = {};
    const rows: { claim: string; ok: boolean; got: unknown }[] = [];
    for (const sh of X5_SHAPES) {
      await page.unrouteAll({ behavior: "ignoreErrors" });
      const read = await bootWithStubs(page, stubsForShape(sh), "X1");
      per[sh.row] = { status: read.status, promise_in_copy: read.promise_in_copy,
                      details_count: read.details_count, reason: read.reason, origin: read.origin,
                      raw_rendered: read.raw_fields, raw_expected: expectedRaw(sh) };
      rows.push({ claim: `${sh.row}: the card's promise and the DOM agree (promise => block exists)`,
        ok: !read.promise_in_copy || read.details_count >= 1,
        got: { promise_in_copy: read.promise_in_copy, details_count: read.details_count } });
      rows.push({ claim: `${sh.row}: this is the shape X-5 named`,
        ok: read.status === "NO_PLANS_PRODUCED", got: { status: read.status } });
      const want = expectedRaw(sh);
      const got = read.raw_fields;
      rows.push({ claim: `${sh.row}: its block carries the whitelist fields, value for value`,
        ok: Object.entries(want).every(([k, v]) => (got[k] ?? null) === v),
        got: { expected: want, rendered: got } });
      rows.push({ claim: `${sh.row}: no fragment of the answer's body reaches the DOM (Q64=①)`,
        ok: !panelLeaksBody(read, sh),
        got: { fed_head: sh.body.slice(0, 24), leaked: panelLeaksBody(read, sh),
               panel: read.raw_text } });
      rows.push({ claim: `${sh.row}: an empty answer still shows its byte count, so the check above is not vacuous`,
        ok: sh.body !== "" || (read.raw_text ?? "").includes("bytes=0"),
        got: { fed_bytes: new TextEncoder().encode(sh.body).length, panel: read.raw_text } });
      rows.push({ claim: `${sh.row}: the copy still points there (Q53=①: render it, keep the pointer)`,
        ok: read.promise_in_copy === true, got: { promise_in_copy: read.promise_in_copy } });
    }
    const head = per[X5_SHAPES[2].row] as Record<string, unknown>;
    readings["X1"] = { want: "raw", force_shape: "HTTP 200 bodies on both write legs (R19-R23, plus the Q64=① credential-shaped witness R24)",
                       shape_count: X5_SHAPES.length, per_shape: per,
                       read: { ...head, shape_count: X5_SHAPES.length },
                       verdict: rows.every((r) => r.ok) ? "MET" : "NOT_MET", rows };
    expect(rows.filter((r) => !r.ok), node("X1", JSON.stringify(per))).toEqual([]);
  });


test(`ord0302-X2 ${"a promise-free card stays honest, and the empty-block direction is named as unmeasured"}`,
  async ({ page }) => {
    /* Requirement 2 wants a reading in each direction. The forward one is X1, over all five shapes. This arm
       drives a status whose sentence makes no promise and checks the pairing there -- and it states plainly
       that the reverse direction has no witness on this candidate: the block is now gated on the raw reading,
       so every shape this screen can be driven into either answered (a reading exists, block present) or was
       refused before the request (the card names a code and still shows the reading). An unreachable
       antecedent is reported, not manufactured. */
    const stubs: Stub[] = [
      { url: "**/api/v1/journeys/*/plans", method: "GET", abort: true },
      { url: "**/api/v1/journeys/*/agent/commands", method: "POST", body: MISSION_CLEAN },
      { url: "**/api/v1/places**", body: [] },
    ];
    const read = await bootWithStubs(page, stubs, "X2");
    const rows = [
      { claim: "no promise is made when the block is absent",
        ok: read.details_count >= 1 || !read.promise_in_copy,
        got: { promise_in_copy: read.promise_in_copy, details_count: read.details_count } },
      { claim: "the shape is a real one on screen, not a half-booted page", ok: read.status.length > 0,
        got: { status: read.status } },
    ];
    readings["X2"] = { want: "either", force_shape: "baseline read aborted, no places, no promise", read,
                       absent_block_direction: read.details_count === 0 ? "OBSERVED" : "NOT_MEASURED",
                       why_absent_direction_unreachable:
                         "the block is gated on the raw reading, so no drivable shape of this screen leaves it "
                         + "out of the DOM",
                       verdict: rows.every((r) => r.ok) ? "MET" : "NOT_MET", rows };
    expect(rows.filter((r) => !r.ok), node("X2", JSON.stringify(read))).toEqual([]);
  });
test.afterEach(async ({}, testInfo) => {
  const arm = String(testInfo.title).match(/ord0302-(N[123]|X[12])/)?.[1];
  if (!arm) return;
  writeFileSync(
    evidencePath("ord0302", `arm_${arm}.json`),
    JSON.stringify({
      file: FILE, arm, runId: process.env.CITYNEXUS_E2E_RUN_ID ?? "",
      status: testInfo.status, expectation: testInfo.expectedStatus,
      reading: readings[arm] ?? null,
    }, null, 1),
  );
});
