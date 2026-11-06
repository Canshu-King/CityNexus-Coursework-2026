/**
 * ORD-0323 — two Q64 exposure cells, each judged from both directions.
 *
 * S1's full-tree scan (`evidence/cmdr-s1-verify-0306/verify_r1.json`) named exactly two places where a
 * screen still prints words it did not write:
 *   cell 1  `ProductErrorBoundary.tsx` -- the caught failure's own prose and React's component
 *           frames, both straight into a `<pre>`;
 *   cell 2  `JourneyInboxWorkbench.tsx` -- a rejected reply rebuilt as `new Error(<server sentence>)`,
 *           which this panel then renders at :687 / :691 / :731.
 *
 * Q64=① says a panel may show whitelisted fields it measured on its own, so every arm here carries two
 * observables that must not substitute for one another (the commander's own words: 两个观测量不许互替):
 *   NEGATIVE  the text this device is forbidden to repeat is not in the DOM at all -- not merely not
 *             visible, because a collapsed `<details>` is still on the screen for the next reader;
 *   POSITIVE  the allowed readings are still rendered, so a "clean" verdict cannot be bought by deleting
 *             the panel, the fold, or the measurement behind it.
 * Each direction has its own counter-example, run outside this file: `ord0323_run_arms.py` restores the
 * old sink (M_sink_back), strips the device fields while keeping the panel (M_fields_gone), puts the
 * server sentence back into the thrown error (M_throw_back), and strips status/content-type from the
 * device sentence while leaving the throw alone (M_device_text_blind).
 *
 * Cell 1's crash is driven for real: `GET .../inbox` answers with a draft row missing the field the queue
 * renders (`unresolved_fields`), so the render throws and the boundary is the component that has to speak.
 * Nothing here stubs the boundary itself.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0323-two-exposure-cells.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* The three bodies the product must never repeat. Distinctive tokens, one per request leg, so a leak is
   attributable to the sink that carried it rather than to "something on the page". */
const UPSTREAM_MEMORY = "memory_forbidden_token_9f3c the traveller's own words were refused";
const UPSTREAM_INBOX = "inbox_forbidden_token_4b71 the review queue is not ready";
const UPSTREAM_CONFIRM = "confirm_forbidden_token_c258 the commitment was rejected";

/* What the pre-fix screen actually held, measured from the M_sink_back run's own evidence file rather than
   guessed at: the TypeError's prose, the property name inside it, and the component frame React printed. */
const CELL1_LEAKS = ["Cannot read", "unresolved_fields", "JourneyInboxWorkbench"];
const CELL2_LEAKS = [UPSTREAM_MEMORY, UPSTREAM_INBOX, UPSTREAM_CONFIRM];

const jsonBody = (o: unknown) => JSON.stringify(o);

type Readings = Record<string, unknown>;
const readings: Readings = {};

async function domReading(page: Page): Promise<{
  root: string;
  body: string;
  attrs: string;
  attr_pairs: number;
  overlay: string[];
}> {
  return page.evaluate(() => {
    const root = document.getElementById("root");
    const body = document.body;
    /* Q71: data-* / aria-* are part of the screen, and textContent cannot see them, so the prose check
       is run over the attribute values as its own sweep rather than assumed to follow from the text one. */
    const pairs = Array.from(document.querySelectorAll("*")).flatMap((el) =>
      Array.from(el.attributes)
        .filter((a) => a.name.startsWith("data-") || a.name.startsWith("aria-"))
        .map((a) => `${el.tagName.toLowerCase()}|${a.name}|${a.value}`),
    );
    return {
      root: root ? root.textContent ?? "" : "",
      body: body.textContent ?? "",
      attrs: pairs.join("\n"),
      attr_pairs: pairs.length,
      // Anything bolted onto the page outside the product tree (a dev-only overlay would show here).
      overlay: Array.from(body.children).map((el) => {
        const tag = el.tagName.toLowerCase();
        const id = el.id ? `#${el.id}` : "";
        return `${tag}${id}${el.shadowRoot ? "[shadow]" : ""}`;
      }),
    };
  });
}

async function boot(page: Page, journeyId: string): Promise<void> {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
}

async function journeyId(page: Page): Promise<string> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("boot", "the stage named no journey, so there is no inbox to open"));
  return id;
}

/* The arm key is the cell, not the test's prefix: eight legs share the `N4_` prefix and the two Q68 legs
 * share `N6_`, so keying on the first token would have collided two legs onto one evidence file. */
const armOf = (title: string): string =>
  /^N4_(T\d\d)_/.exec(title)?.[1] ?? /^N6_(Q68[abcd])_/.exec(title)?.[1]
  ?? /^N7_(F\d)_/.exec(title)?.[1] ?? title.split("_")[0];

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const body: Readings = { ...(readings[arm] ?? {}) };
  /* E's signoff criterion: a reading is worth nothing unless it says which test produced it. */
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  /* A red arm has to carry its own readings too, or the counter-example proves nothing about which
     direction flipped. */
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 600);
  }
  writeFileSync(
    evidencePath(`ord0323/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, ...body }, null, 1),
    "utf-8",
  );
});

test("N1_boundary_panel_keeps_its_own_readings_and_drops_the_failure_prose — 边界自己量的字段还在，失败原文与组件帧不在屏上", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "N1";
  const id = await journeyId(page);
  const consoleErrors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  /* A queue row without the field the row renders. The server contract says `unresolved_fields` is always
     present; this is the shape that makes the panel throw, which is the only way the boundary gets to
     speak -- so the arm drives the real component tree, not a stand-in. */
  await page.route("**/api/v1/journeys/*/inbox", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({
      status: 200,
      contentType: "application/json",
      body: jsonBody({
        drafts: [
          {
            id: "ORD0323-CRASH-1",
            artifact_type: "STRUCTURED_JSON",
            extracted: { title: "天津西 → 北京南 高铁" },
            status: "NEEDS_REVIEW",
            extraction_mode: "RULE",
          },
        ],
      }),
    });
  });
  await boot(page, id);
  await page.goto("/v8/bookings", { waitUntil: "load" });

  const panel = page.getByTestId("product-error-boundary");
  await expect(panel, node(arm, "the render never threw, so the boundary had nothing to say and every claim below would be vacuous")).toBeVisible({
    timeout: 30_000,
  });
  /* The fold is opened by structure, not by a testid of my own: the same arm has to be able to speak on
     the pre-fix file, and there it hung for the whole budget waiting for an element the old screen never
     had (measured in the first pass, shape `cell1_baseline_sink`). */
  await panel.locator("details").locator("summary").click();
  const detail = ((await page.getByTestId("error-detail").textContent()) ?? "").trim();
  const dom = await domReading(page);
  readings[arm] = {
    panel_copy: clean(((await panel.textContent()) ?? "")),
    error_detail_text: detail,
    console_error_count: consoleErrors.length,
    failure_text_rendered_field: (detail.match(/failure_text_rendered=(\w+)/) || [null, ""])[1],
    console_error_head: consoleErrors.slice(0, 2).map((s) => s.slice(0, 220)),
    root_text_length: dom.root.length,
    overlay_hosts: dom.overlay,
    leaked_needles_still_on_screen: CELL1_LEAKS.filter((n) => dom.root.includes(n) || dom.body.includes(n)),
    leaked_needles_in_attributes: CELL1_LEAKS.filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
  };

  /* POSITIVE -- readings this device took itself, each one only present if the measurement still runs. */
  expect(detail, node(arm, "技术详情 renders empty, which is how a fold passes a 'nothing leaked' check while telling the reader nothing")).not.toBe("");
  expect(
    /route=\/v8\/bookings/.test(detail),
    node(arm, `the fold no longer states which screen failed; it reads "${detail.slice(0, 200)}"`),
  ).toBe(true);
  expect(
    /caught_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(detail),
    node(arm, `no device timestamp in the fold, so the reading is not the device's own; it reads "${detail.slice(0, 200)}"`),
  ).toBe(true);
  expect(
    /failures_caught_here=[1-9]\d{0,2}/.test(detail),
    node(arm, `the boundary does not count what it caught; it reads "${detail.slice(0, 200)}"`),
  ).toBe(true);
  expect(
    /online_at_this_device=(yes|no)/.test(detail),
    node(arm, `the fold dropped the device's own connectivity reading; it reads "${detail.slice(0, 200)}"`),
  ).toBe(true);
  expect(
    (detail.match(/failure_text_rendered=(\w+)/) || [null, ""])[1],
    node(arm, `the fold no longer states its own boundary as the word no; it reads "${detail.slice(0, 200)}"`),
  ).toBe("no");

  /* NEGATIVE -- the failure prose and the component frames are not in the DOM. Asserted on textContent,
     not innerText: a collapsed fold is still a page a screenshot tool can expand. */
  expect(
    (readings[arm] as Readings).leaked_needles_still_on_screen,
    node(
      arm,
      `the boundary is printing what it was handed again (${JSON.stringify(
        (readings[arm] as Readings).leaked_needles_still_on_screen,
      )}); the screen reads "${clean(dom.root).slice(0, 240)}"`,
    ),
  ).toEqual([]);
  expect(
    (readings[arm] as Readings).leaked_needles_in_attributes,
    node(
      arm,
      `Q71 counts an attribute as the screen: ${JSON.stringify(
        (readings[arm] as Readings).leaked_needles_in_attributes,
      )} is held in a data / aria attribute value (${String((readings[arm] as Readings).attribute_pairs_swept)} pairs swept)`,
    ),
  ).toEqual([]);
});

test("N2_rejected_readings_render_their_own_status_and_content_type_not_the_reply_s_sentence — 顶回来的两条读数各自还在，回复原话不在屏上", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "N2";
  const id = await journeyId(page);
  await page.route("**/api/v1/journeys/*/memory", async (r: Route) => {
    await r.fulfill({
      status: 500,
      contentType: "application/json",
      body: jsonBody({ error: { message: UPSTREAM_MEMORY } }),
    });
  });
  await page.route("**/api/v1/journeys/*/inbox", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({
      status: 503,
      contentType: "application/json",
      body: jsonBody({ detail: UPSTREAM_INBOX }),
    });
  });
  await boot(page, id);
  await page.goto("/v8/bookings", { waitUntil: "load" });

  const message = page.locator(".v10-inbox-message").first();
  await expect(
    message,
    node(arm, "the refused memory read left no sentence at all, so there is nothing to judge -- an empty screen is not an honest screen"),
  ).toBeVisible({ timeout: 30_000 });
  const messageText = clean(await message.textContent());
  const pending = page.getByTestId("inbox-pending-error");
  await expect(
    pending,
    node(arm, "the refused queue read left no warning in the queue's own slot; an unreadable list and an empty list are showing alike"),
  ).toBeVisible({ timeout: 30_000 });
  const pendingText = clean(await pending.textContent());
  const dom = await domReading(page);
  readings[arm] = {
    message_text: messageText,
    pending_text: pendingText,
    leaked_needles_still_on_screen: CELL2_LEAKS.filter((n) => dom.root.includes(n) || dom.body.includes(n)),
    leaked_needles_in_attributes: CELL2_LEAKS.filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
  };

  /* POSITIVE -- each slot carries the reading of ITS OWN request, which is what makes these two
     observables independent rather than one claim written twice. */
  expect(
    messageText.includes("500"),
    node(arm, `the memory slot no longer states the status this device measured (${messageText})`),
  ).toBe(true);
  expect(
    /Cannot read|Unexpected token|JSON|is not valid JSON/.test(messageText),
    node(arm, `the status line is showing parser or engine prose instead of the channel's own sentence (${messageText})`),
  ).toBe(false);
  expect(
    pendingText.includes("503") && pendingText.includes("application/json"),
    node(arm, `the queue slot no longer states the status and content type this device got (${pendingText})`),
  ).toBe(true);
  expect(
    !messageText.includes("503") && !pendingText.includes("500"),
    node(arm, `the two slots swapped readings (memory: "${messageText}" / queue: "${pendingText}") -- one is showing the other's request`),
  ).toBe(true);

  /* NEGATIVE -- no reply sentence, from either leg, reaches the DOM. */
  expect(
    (readings[arm] as Readings).leaked_needles_still_on_screen,
    node(
      arm,
      `the panel is repeating the reply again (${JSON.stringify((readings[arm] as Readings).leaked_needles_still_on_screen)}); memory reads "${messageText}" and the queue reads "${pendingText}"`,
    ),
  ).toEqual([]);
  expect(
    (readings[arm] as Readings).leaked_needles_in_attributes,
    node(
      arm,
      `Q71 counts an attribute as the screen: ${JSON.stringify(
        (readings[arm] as Readings).leaked_needles_in_attributes,
      )} is held in a data / aria attribute value (${String(
        (readings[arm] as Readings).attribute_pairs_swept,
      )} pairs swept)`,
    ),
  ).toEqual([]);
});

test("N3_confirm_fold_carries_the_attempt_reading_not_the_attempt_s_words — 确认那一折带着这台设备的读数，不带回复的原话", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "N3";
  const id = await journeyId(page);
  const draft = {
    id: "ORD0323-D3",
    artifact_type: "STRUCTURED_JSON",
    extracted: { title: "天津西 → 北京南 高铁", reservation_type: "RAIL" },
    status: "NEEDS_REVIEW",
    unresolved_fields: [],
    extraction_mode: "RULE",
    interpretation: { anchor_role: "MID_TRIP", anchor_role_confidence: 0.8, payment_status: null, reasoning_summary: null, semantic_target: null },
    smart_intake: { decision: "REQUIRES_CONFIRMATION", reasons: [], conflicts: [], resolved_target: null, role_confidence: 0.5 },
  };
  await page.route("**/api/v1/journeys/*/inbox", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({ status: 200, contentType: "application/json", body: jsonBody({ drafts: [draft] }) });
  });
  await page.route("**/api/v1/journeys/*/inbox/*/confirm", async (r: Route) => {
    await r.fulfill({
      status: 500,
      contentType: "application/json",
      body: jsonBody({ error: { message: UPSTREAM_CONFIRM } }),
    });
  });
  await boot(page, id);
  await page.goto("/v8/bookings", { waitUntil: "load" });

  await expect(
    page.getByTestId("inbox-pending-row").first(),
    node(arm, "the queue never rendered the draft row, so there is no review to open and the fold below would prove nothing"),
  ).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("inbox-open-review").first().click();
  await expect(
    page.getByTestId("v13-2-confirm"),
    node(arm, "the review panel never opened for a draft the server itself reports as reviewable"),
  ).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("v13-2-confirm").click();

  const fold = page.getByTestId("inbox-confirm-detail");
  await expect(
    fold,
    node(arm, "a refused confirmation with no technical fold at all -- the pairing ORD-0302 X-5 settled: a promise needs its block"),
  ).toBeVisible({ timeout: 30_000 });
  const summaryText = clean(await fold.locator("summary").innerText());
  await fold.locator("summary").click();
  const detailText = clean(await fold.textContent());
  const codeText = clean(await fold.locator("code").innerText());
  const dom = await domReading(page);
  readings[arm] = {
    fold_summary: summaryText,
    fold_text: detailText,
    fold_code_visible: codeText,
    status_line: clean((await page.locator(".v10-inbox-message").first().textContent().catch(() => "")) ?? ""),
    leaked_needles_still_on_screen: CELL2_LEAKS.filter((n) => dom.root.includes(n) || dom.body.includes(n)),
    leaked_needles_in_attributes: CELL2_LEAKS.filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
  };

  expect(
    summaryText.includes("技术详情"),
    node(arm, `the fold promised something different from what it holds now (${summaryText})`),
  ).toBe(true);
  expect(
    detailText.includes("500") && detailText.includes("application/json"),
    node(arm, `the confirmation fold no longer states what this device measured about the attempt (${detailText})`),
  ).toBe(true);
  expect(
    codeText.includes("500"),
    node(arm, `the reading is in the fold but not on the screen once it is opened (${codeText})`),
  ).toBe(true);

  expect(
    (readings[arm] as Readings).leaked_needles_still_on_screen,
    node(
      arm,
      `the confirmation fold is repeating the reply again (${JSON.stringify((readings[arm] as Readings).leaked_needles_still_on_screen)}); it reads "${detailText}"`,
    ),
  ).toEqual([]);
  expect(
    (readings[arm] as Readings).leaked_needles_in_attributes,
    node(
      arm,
      `Q71 counts an attribute as the screen: ${JSON.stringify(
        (readings[arm] as Readings).leaked_needles_in_attributes,
      )} is held in a data / aria attribute value (${String(
        (readings[arm] as Readings).attribute_pairs_swept,
      )} pairs swept)`,
    ),
  ).toEqual([]);
});

function clean(value: string): string {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

/* ------------------------------------------------------------------ N4: the eight legs D numbered.
 * T05-T13 of `evidence/ord0320-triage-d/ord0320_triage_r2.json` are eight catch sites plus the technical
 * fold, and all nine carried the same reply's sentence to the screen through `error.message`. One test
 * walks the legs with a route plan that changes per leg: every leg asserts its own needle is nowhere in
 * the DOM, and its own measured status is in the slot that leg writes to -- which is what makes the
 * reading attributable to the leg instead of to the page.
 */
const NEEDLES: Record<string, string> = {
  T06: "import_forbidden_leg_t06",
  T07: "memory_forbidden_token_9f3c the traveller's own words were refused",
  T08: "inbox_forbidden_token_4b71 the review queue is not ready",
  T09: "text_extract_forbidden_leg_t09",
  T10: "vision_forbidden_leg_t10",
  T11: "smart_apply_forbidden_leg_t11",
  T12: "confirm_forbidden_token_c258 the commitment was rejected",
  T13: "patch_forbidden_leg_t13",
};

type Leg = {
  cell: string;
  match: string;
  method: string;
  status: number;
  slot: "message" | "queue" | "fold";
  trigger: "none" | "manual" | "paste" | "vision" | "review_smart" | "review_confirm" | "review_patch";
};

const LEGS: Leg[] = [
  {cell: "T06", match: "/inbox/import", method: "POST", status: 502, slot: "message", trigger: "manual"},
  {cell: "T07", match: "/memory", method: "GET", status: 500, slot: "message", trigger: "none"},
  {cell: "T08", match: "/inbox", method: "GET", status: 503, slot: "queue", trigger: "none"},
  {cell: "T09", match: "/inbox/text-extract", method: "POST", status: 504, slot: "message", trigger: "paste"},
  {cell: "T10", match: "/inbox/vision", method: "POST", status: 507, slot: "message", trigger: "vision"},
  {cell: "T11", match: "/smart-apply", method: "POST", status: 506, slot: "message", trigger: "review_smart"},
  {cell: "T12", match: "/confirm", method: "POST", status: 509, slot: "fold", trigger: "review_confirm"},
  {cell: "T13", match: "PATCH_INBOX", method: "PATCH", status: 511, slot: "message", trigger: "review_patch"},
];

const draftRow = {
  id: "ORD0323-D3",
  artifact_type: "STRUCTURED_JSON",
  extracted: {title: "天津西 → 北京南 高铁", reservation_type: "RAIL", anchor_role: "MID_TRIP"},
  status: "NEEDS_REVIEW",
  unresolved_fields: [],
  extraction_mode: "RULE",
  interpretation: {anchor_role: "MID_TRIP", anchor_role_confidence: 0.8, payment_status: null, reasoning_summary: null, semantic_target: null},
  smart_intake: {decision: "AUTO_APPLY", reasons: [], conflicts: [], resolved_target: null, role_confidence: 0.5},
};

async function drive(page: Page, trigger: Leg["trigger"]): Promise<void> {
  if (trigger === "manual") {
    await page.getByTestId("inbox-tab-manual").click();
    await page.getByTestId("manual-title").fill("天津西 → 北京南 高铁");
    await page.getByTestId("manual-from").fill("天津");
    await page.getByTestId("manual-to").fill("北京");
    await page.getByTestId("manual-submit").click();
    return;
  }
  if (trigger === "vision") {
    await page.getByTestId("inbox-tab-paste").click();
    await page.locator("input[type=file]").first().setInputFiles({
      name: "ticket.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c636000000200" +
        "0100ffff03000006000557bfabd40000000049454e44ae426082", "hex"),
    });
    /* A file <input> is itself exposed with role=button and carries the whole label in its accessible
       name, so the substring match resolved to two controls. */
    await page.getByRole("button", {name: "提取图片草稿", exact: true}).click();
    return;
  }
  await page.getByTestId("inbox-open-review").first().click();
  if (trigger === "review_smart") await page.getByTestId("v13-2-smart-apply").click();
  if (trigger === "review_confirm") await page.getByTestId("v13-2-confirm").click();
  if (trigger === "review_patch") await page.getByTestId("v13-2-save-corrections").click();
}

/* ------------------------------------------------------------------ Q68: the 「原因编号」 slot holds a code or nothing.
 * CMDR 04:45 ruled the slot's format is a promise: `serverReasonCode` used to fall back on
 * `detail.slice(0, 60).toUpperCase()`, which put the reply's sentence into a place the card labels a
 * machine code. Two legs, because both halves of that ruling are observable: a real code must still be
 * shown (the fix may not be "show nothing"), and prose must be shown only where Q67=① put it -- the
 * 技术详情 fold, under the attribution label.
 */
const Q68_CODE = "ZZ_Q68_SERVER_CODE";
/* CMDR 04:14 names `{"error":{"code":"BUDGET_INFEASIBLE"}}` as the shape a traveller must be able to see,
   and `serverReasonCode` accepts three machine keys, so each key gets its own arm and its own code. */
const Q68_CODE_KEYS: Array<[string, string]> = [
  ["reason_code", Q68_CODE],
  ["code", "BUDGET_INFEASIBLE"],
  ["message_key", "ZZ_Q68_MESSAGE_KEY"],
];
const Q68_PROSE = "q68_forbidden_prose_行程过长无法在三日预算内排布";

async function askTheSheet(page: Page, generateBody: string): Promise<void> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("Q68", "the stage named no journey"));
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  await page.setViewportSize({width: 390, height: 844});
  /* The mission has to read as completed: the sheet decides `planCount > 0` -> COMPLETED, then an
     incomplete mission -> PLANS_WITHOUT_AI, and only after those does it consult `blockedCode`. A stub
     that stops the mission short therefore never reaches the 原因编号 slot, and both Q68 arms would be
     judging a copy that does not contain it (measured: under `cell3_code_slot_back` the card still said
     "没有给出具体原因编号" -- the arm was vacuous, not green). */
  const mission = {mission: {id: "ord0323-q68-mission", status: "SUCCEEDED", failure_reason: null,
                             reasoning_summary: null}};
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    await r.fulfill({status: 200, contentType: "application/json", body: jsonBody(mission)});
  });
  await page.route("**/api/v1/journeys/*/plans/generate", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    await r.fulfill({status: 503, contentType: "application/json", body: generateBody});
  });
  await page.route("**/api/v1/journeys/*/plans", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({status: 200, contentType: "application/json", body: "[]"});
  });
  await page.route("**/api/v1/places**", async (r: Route) => {
    await r.fulfill({status: 200, contentType: "application/json",
                     body: jsonBody([{id: "p1", name: "天津西站"}, {id: "p2", name: "北京南站"}])});
  });
  await page.goto("/v8/hub", {waitUntil: "load"});
  await expect(page.getByTestId("today-hero")).toBeVisible({timeout: 30_000});
  await page.getByTestId("hub-plan-with-ai").click();
  await page.getByTestId("plan-ai-input").fill("帮我排三天，喜欢美食和特色景点。");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result")).toBeVisible({timeout: 60_000});
}

test("N6_Q68a_a_server_code_still_reaches_the_code_slot — 编号那格：服务器真给了编号时，编号还在屏上", async ({page}) => {
  test.setTimeout(300_000);
  const arm = "Q68a";
  await askTheSheet(page, jsonBody({error: {reason_code: Q68_CODE}, detail: Q68_PROSE}));
  const card = clean(await page.getByTestId("plan-ai-result").textContent());
  const dom = await domReading(page);
  readings[arm] = {card_text: card, code_in_card: card.includes(Q68_CODE),
                   sweep_sees_the_tested_card: dom.attrs.includes("|data-testid|plan-ai-result"),
                   attribute_pairs_swept: dom.attr_pairs,
                   prose_outside_the_fold: clean(card.replace(clean(await page.getByTestId("plan-ai-reason").textContent().catch(() => "")), "")),
                   needle_in_dom: dom.body.includes(Q68_PROSE)};
  expect(card.includes(Q68_CODE),
    node(arm, `the code slot lost a code the server really gave (${card.slice(0, 200)}) -- "do not show prose" is not "show nothing"`)).toBe(true);
  expect(card.includes("BLOCKED") || card.includes("服务器回的原因编号"),
    node(arm, `the card no longer names this as a server-coded block (${card.slice(0, 200)})`)).toBe(true);
  /* The control that keeps the attribute leg from being vacuous: the card under test carries its own
     data-testid, so if the sweep cannot name that pair, every "no sentence in the attributes" reading
     on this page measures nothing. */
  expect(dom.attrs.includes("|data-testid|plan-ai-result"),
    node(arm, `the attribute sweep found ${String(dom.attr_pairs)} pairs but not the tested card among them (${card.slice(0, 160)})`)).toBe(true);
});

test("N6_Q68b_a_sentence_only_refusal_is_not_presentation_as_a_code — 编号那格：只有句子时，编号位上不许出现句子，句子归技术详情", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "Q68b";
  /* `serverReasonCode` returns early when the envelope holds no `error` object at all, so the shape that
     really reached the slot is the one S4 measured: an envelope with an `error` and no code in it, plus
     a `detail`. Measured under `cell3_code_slot_back`: with a bare `{detail}` both legs stayed green,
     which was the arm being vacuous, not the fix being good. */
  await askTheSheet(page, jsonBody({error: {hint: "no machine code in this envelope"}, detail: Q68_PROSE}));
  const reasonEl = page.getByTestId("plan-ai-reason");
  const reasonText = clean(await reasonEl.textContent().catch(() => ""));
  const reasonRaw = (await reasonEl.textContent().catch(() => "")) ?? "";
  /* the page as one surface, with the sanctioned attributed copy taken back out of it */
  const pageButFold = ((await domReading(page)).body || "").replace(reasonRaw, "");
  const cardAll = clean(await page.getByTestId("plan-ai-result").textContent());
  const cardWithoutReason = cardAll.replace(reasonText, "");
  /* both copies the product is allowed to use for "the server gave no machine code" */
  const noCodeAnyWording = /（没给编号）|[(]none given[)]|没有给出具体原因编号/.test(cardAll);
  const dom = await domReading(page);
  readings[arm] = {
    card_text: cardAll,
    reason_fold_text: reasonText,
    uppercased_prose_in_code_slot: cardWithoutReason.toUpperCase().includes(Q68_PROSE.toUpperCase().slice(0, 18)),
    uppercased_prose_in_attributes: dom.attrs.toUpperCase().includes(Q68_PROSE.toUpperCase().slice(0, 18)),
    attribute_pairs_swept: dom.attr_pairs,
    attributed_sentence_in_fold: reasonText.includes(Q68_PROSE),
    fold_text_equals_the_sentence: reasonText === Q68_PROSE,
    server_label_present: /服务器给出的原因|Reason the server gave/.test(cardAll),
    needle_in_dom: dom.body.includes(Q68_PROSE),
    no_code_branch_on_screen: noCodeAnyWording,
    no_code_copy_in_the_code_slot: /（没给编号）|[(]none given[)]/.test(cardAll),
    no_code_copy_in_the_no_plan_state: /没有给出具体原因编号/.test(cardAll),
    which_no_code_branch: /（没给编号）|[(]none given[)]/.test(cardAll)
      ? "code_slot"
      : /没有给出具体原因编号/.test(cardAll)
        ? "no_plan_state"
        : "neither",
    uppercased_prose_anywhere_but_the_fold: pageButFold
      .toUpperCase()
      .includes(Q68_PROSE.toUpperCase().slice(0, 60)),
    prose_anywhere_but_the_fold: pageButFold.includes(Q68_PROSE),
    page_text_length: pageButFold.length,
  };
  /* the code slot's own text may not hold the sentence, in any case or truncation */
  expect(
    cardWithoutReason.toUpperCase().includes(Q68_PROSE.toUpperCase().slice(0, 18)),
    node(arm, `the 原因编号 slot is showing the reply's sentence again (${cardWithoutReason.slice(0, 220)})`),
  ).toBe(false);
  expect(
    dom.attrs.toUpperCase().includes(Q68_PROSE.toUpperCase().slice(0, 18)),
    node(arm, `Q71: the sentence is in a data / aria attribute value (${String((readings[arm] as Readings).attribute_pairs_swept)} pairs swept) even though the visible slot is clean`),
  ).toBe(false);
  /* and Q67=①'s home for it is still there: the attributed sentence inside 技术详情 */
  expect(reasonText,
    node(arm, `技术详情 holds something other than the reply's own sentence verbatim (${reasonText.slice(0, 200)})`)).toBe(Q68_PROSE);
  expect(reasonText.includes(Q68_PROSE),
    node(arm, `the sentence vanished from 技术详情 too (${reasonText.slice(0, 160)}) -- Q68 says the code slot is not its place, not that it may be deleted`)).toBe(true);
  expect(/服务器给出的原因|Reason the server gave/.test(cardAll),
    node(arm, `the attribution label is gone, so whatever sentence shows is unattributed (${cardAll.slice(0, 220)})`)).toBe(true);
  /* CMDR 04:14 B, first half: no machine code means the screen says it does not know. Deleting this
     branch would also answer "no prose in the slot", so it is asserted as a positive, not assumed. */
  expect(
    noCodeAnyWording,
    node(arm, `the screen names no absence of a machine code in either sanctioned wording (${cardAll.slice(0, 240)}}) -- an invented code and a silent slot are both wrong`),
  ).toBe(true);
  /* CMDR 0323 B, second half: the old fallback rendered the sentence truncated and uppercased. That form
     may not survive anywhere on the page except the attributed fold that holds the original. */
  expect(
    pageButFold.toUpperCase().includes(Q68_PROSE.toUpperCase().slice(0, 60)),
    node(arm, `the reply's sentence is on the page outside 技术详情 in the screaming form (${pageButFold.slice(0, 240)})`),
  ).toBe(false);
  expect(
    pageButFold.includes(Q68_PROSE),
    node(arm, `the reply's sentence is on the page outside 技术详情 even in its own case (${pageButFold.slice(0, 240)})`),
  ).toBe(false);
});

/* One arm per machine key: `serverReasonCode` reads them in a loop, but a loop is not evidence that each
   spelling reaches the traveller, and Q68's other half is "a real code still shows". */
for (const [key, code] of Q68_CODE_KEYS) {
  if (key === "reason_code") continue; // already the Q68a arm, kept as it was measured
  test(`N6_Q68${key === "code" ? "c" : "d"}_a_machine_code_in_${key}_reaches_the_code_slot — 编号那格：${key} 这一形给了真编号时，编号还在屏上`, async ({page}) => {
    test.setTimeout(300_000);
    const arm = key === "code" ? "Q68c" : "Q68d";
    await askTheSheet(page, jsonBody({error: {[key]: code}, detail: Q68_PROSE}));
    const card = clean(await page.getByTestId("plan-ai-result").textContent());
    const reason = clean(await page.getByTestId("plan-ai-reason").textContent().catch(() => ""));
    const dom = await domReading(page);
    readings[arm] = {asked_key: key, asked_code: code, card_text: card,
                     code_in_card: card.includes(code),
                     prose_outside_the_fold: clean(card.replace(reason, "")),
                     needle_in_dom: dom.body.includes(Q68_PROSE),
                     sweep_sees_the_tested_card: dom.attrs.includes("|data-testid|plan-ai-result"),
                     attribute_pairs_swept: dom.attr_pairs};
    expect(card.includes(code),
      node(arm, `the 原因编号 slot lost the code the server really sent in ${key} (${card.slice(0, 200)})`)).toBe(true);
    expect(/服务器回的原因编号|服务器给的原因编号/.test(card),
      node(arm, `the card no longer names this as a server-coded block for ${key} (${card.slice(0, 200)})`)).toBe(true);
    expect(dom.attrs.includes("|data-testid|plan-ai-result"),
      node(arm, `the attribute sweep found ${String(dom.attr_pairs)} pairs but not the tested card (${key})`)).toBe(true);
  });
}

/* One test per leg: a shared test spends one timeout across eight chances and hides which leg stuck
 * (measured -- the loop version died at `running_leg = T09` with three legs done). Each leg gets its own
 * route plan, its own 120 s budget, and its own evidence file named by D's cell id. */
for (const leg of LEGS) {
  test(`N4_${leg.cell}_a_refused_${leg.method}_leaves_no_reply_words_and_keeps_its_own_reading — ${leg.cell}：自己那句原话不在屏上，自己那次读数还在屏上`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const arm = leg.cell;
    const id = await journeyId(page);
    await page.route("**/api/v1/journeys/**", async (r: Route) => {
      const path = new URL(r.request().url()).pathname;
      const method = r.request().method();
      const mine =
        leg.match === "PATCH_INBOX"
          ? method === "PATCH" && /\/inbox\/[A-Za-z0-9-]+$/.test(path)
          : method === leg.method && path.endsWith(leg.match);
      if (mine) {
        await r.fulfill({
          status: leg.status,
          contentType: "application/json",
          body: jsonBody({error: {message: NEEDLES[leg.cell]}, detail: NEEDLES[leg.cell]}),
        });
        return;
      }
      if (path.endsWith("/inbox") && method === "GET") {
        await r.fulfill({status: 200, contentType: "application/json", body: jsonBody({drafts: [draftRow]})});
        return;
      }
      return r.fallback();
    });
    await boot(page, id);
    await page.goto("/v8/bookings", {waitUntil: "load"});
    await expect(
      page.getByTestId("v13-2-memory"),
      node(arm, `${leg.cell}: the wallet panel never rendered, so this leg has no surface to be judged on`),
    ).toBeVisible({timeout: 30_000});
    if (leg.trigger === "paste") {
      await page.getByTestId("inbox-tab-paste").click();
      /* This page holds a second textarea inside the closed structured-JSON aside; only the visible one is
         the paste box. */
      await page.locator("textarea").filter({visible: true}).first().fill("9月26日 天津西→北京南 G8901 二等座 07:30");
      await page.getByRole("button", {name: "提取文本草稿", exact: true}).click();
    } else if (leg.trigger !== "none") {
      await drive(page, leg.trigger);
    }
    const slot =
      leg.slot === "message"
        ? page.locator(".v10-inbox-message").first()
        : leg.slot === "queue"
          ? page.getByTestId("inbox-pending-error")
          : page.getByTestId("inbox-confirm-detail");
    await expect(
      slot,
      node(arm, `${leg.cell}: the leg was refused but its own slot shows nothing -- an empty screen is not an honest screen`),
    ).toBeVisible({timeout: 30_000});
    if (leg.slot === "fold") await slot.locator("summary").click();
    const text = clean(await slot.textContent());
    const dom = await domReading(page);
    const found = Object.entries(NEEDLES)
      .filter(([, n]) => dom.root.includes(n) || dom.body.includes(n))
      .map(([c]) => c);
    const foundInAttrs = Object.entries(NEEDLES)
      .filter(([, n]) => dom.attrs.includes(n))
      .map(([c]) => c);
    const foreign = LEGS.filter((o) => o.cell !== leg.cell).map((o) => String(o.status)).filter((x) => text.includes(x));
    readings[arm] = {
      cell: leg.cell,
      route: leg.match,
      method: leg.method,
      status_asked: leg.status,
      slot_text: text,
      status_in_slot: text.includes(String(leg.status)),
      status_field: (text.match(/http_status=(\d+)/) || text.match(/HTTP (\d+)/) || [null, ""])[1],
      status_field_equals_asked: (text.match(/http_status=(\d+)/) || text.match(/HTTP (\d+)/) || [null, ""])[1] === String(leg.status),
      needles_found: found,
      needles_found_in_attributes: foundInAttrs,
      attribute_pairs_swept: dom.attr_pairs,
      foreign_status_found: foreign,
      internal_token_on_screen: /inbox request rejected/i.test(text),
    };
    expect(found, node(arm, `${leg.cell}: a reply's own words are on the screen (${JSON.stringify(found)}); the slot reads "${text}"`)).toEqual([]);
    expect(foundInAttrs, node(arm, `${leg.cell}: Q71 counts an attribute as the screen -- a reply's words sit in a data / aria value (${JSON.stringify(foundInAttrs)} of ${String(dom.attr_pairs)} pairs swept)`)).toEqual([]);
    expect(text.includes(String(leg.status)),
      node(arm, `${leg.cell}: the slot no longer states the status this device measured for THIS request (${text})`)).toBe(true);
    /* equality on the parsed field, so a substring of another leg's status cannot pass for this one */
    expect((text.match(/http_status=(\d+)/) || text.match(/HTTP (\d+)/) || [null, "(absent)"])[1],
      node(arm, `${leg.cell}: the status read off this slot is "${(text.match(/http_status=(\d+)/) || text.match(/HTTP (\d+)/) || [null, "(absent)"])[1]}", this leg asked ${leg.status} (${text})`)).toBe(String(leg.status));
    expect(foreign,
      node(arm, `${leg.cell}: the slot shows another leg's status ${JSON.stringify(foreign)} instead of its own (${text})`)).toEqual([]);
    if (leg.slot !== "message") {
      expect(text.includes("application/json"),
        node(arm, `${leg.cell}: the machine slot lost the content type this device measured (${text})`)).toBe(true);
    }
    expect(/inbox request rejected/i.test(text),
      node(arm, `${leg.cell}: the carrier's internal token reached the traveller (${text})`)).toBe(false);
  });
}

/* ---------------- ORD-0323 甲 follow-on (CMDR 04:53): four more forms in this panel ----------------
 * F1 an answered-but-unusable body, F2 the reply's `reason` as the status line, F3/F4 the model's
 * `reasoning_summary` rendered as this panel's own words.  Each form is judged in both directions: the
 * upstream sentence must be nowhere on the page (Q71: attributes count), and this device's own fields
 * must still be in the slot that form writes to.
 */
const F_SENTINELS: Record<string, string> = {
  F1: "f1_forbidden_body_that_is_not_json",
  F2: "f2_forbidden_server_reason_sentence",
  F3: "f3_forbidden_reasoning_summary_memory_row",
  F4: "f4_forbidden_reasoning_summary_draft_row",
};

const memoryRow = {
  id: "ORD0323-M1",
  title: "天津西 → 北京南 高铁",
  role: "MID_TRIP",
  label: "ORD0323-M1-LABEL",
  at: "2026-09-26T08:00:00Z",
  protected: false,
  confidence: 0.62,
  reasoning_summary: F_SENTINELS.F3,
};

const interpretationDraft = {
  ...draftRow,
  id: "ORD0323-D4",
  interpretation: {...draftRow.interpretation, reasoning_summary: F_SENTINELS.F4, payment_status: "PAID",
                   anchor_role_confidence: 0.8},
  extracted: {...draftRow.extracted, anchor_role: "MID_TRIP"},
};

async function openWallet(page: Page, id: string): Promise<void> {
  await boot(page, id);
  await page.goto("/v8/bookings", {waitUntil: "load"});
  await expect(
    page.getByTestId("v13-2-memory"),
    node("boot", "the wallet panel never rendered, so these arms have no surface to be judged on"),
  ).toBeVisible({timeout: 30_000});
}

async function runPasteExtraction(page: Page, arm: string): Promise<string> {
  await page.getByTestId("inbox-tab-paste").click();
  await page.locator("textarea").filter({visible: true}).first().fill("9月26日 天津西→北京南 G8901 二等座 07:30");
  await page.getByRole("button", {name: "提取文本草稿", exact: true}).click();
  const line = page.locator(".v10-inbox-message").first();
  await expect(line, node(arm, `${arm}: the extraction ran but this panel left no status line at all`)).toBeVisible({
    timeout: 30_000,
  });
  return clean(await line.textContent());
}

test("N7_F1_an_answered_body_that_is_not_json_is_not_filed_as_a_transport_failure — F1：回答到了但正文不能用，不许说成网络断了，也不许把正文带上屏", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "F1";
  const id = await journeyId(page);
  await page.route("**/api/v1/journeys/**", async (r: Route) => {
    const path = new URL(r.request().url()).pathname;
    if (path.endsWith("/memory") && r.request().method() === "GET") {
      await r.fulfill({
        status: 200,
        contentType: "text/html;charset=UTF-8",
        body: `<!doctype html><html><body>${F_SENTINELS.F1} 这不是这台设备的读数</body></html>`,
      });
      return;
    }
    return r.fallback();
  });
  await openWallet(page, id);
  const line = page.locator(".v10-inbox-message").first();
  await expect(line, node(arm, "F1: 服务器答了，这一屏却什么也没说")).toBeVisible({timeout: 30_000});
  const text = clean(await line.textContent());
  const dom = await domReading(page);
  readings[arm] = {
    asked_status: 200, asked_content_type: "text/html", message_text: text,
    sentinel_on_screen: dom.root.includes(F_SENTINELS.F1) || dom.body.includes(F_SENTINELS.F1),
    sentinel_in_attributes: [F_SENTINELS.F1].filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
    machine_half: (text.split(" · ").slice(-1)[0] ?? "").trim(),
    machine_half_equals: (text.split(" · ").slice(-1)[0] ?? "").trim()
      === "http_status=200 content_type=text/html;charset=UTF-8 evidence=HTTP_RESPONSE",
    fields_in_line: /http_status=200/.test(text) && /content_type=text\/html/.test(text),
    answered_evidence: /evidence=HTTP_RESPONSE/.test(text),
    filed_as_transport: /evidence=TRANSPORT/.test(text),
  };
  expect(readings[arm].sentinel_on_screen, node(arm, `F1: 正文的碎片被当成这句话带上屏了（${text.slice(0, 220)}）`)).toBe(false);
  expect(readings[arm].sentinel_in_attributes, node(arm, `F1: Q71 口径下正文进了属性也是上屏（${String(dom.attr_pairs)} 对）`)).toEqual([]);
  expect(readings[arm].filed_as_transport, node(arm, `F1: 明明答了 200，这一行把它说成网络问题（${text.slice(0, 220)}）`)).toBe(false);
  expect(readings[arm].answered_evidence, node(arm, `F1: 没有把「这是回答，不是断线」写进读数（${text.slice(0, 220)}）`)).toBe(true);
  expect(readings[arm].fields_in_line, node(arm, `F1: 这一行没有这台设备自己量的状态与媒体类型（${text.slice(0, 220)}）`)).toBe(true);
  expect((text.split(" · ").slice(-1)[0] ?? "").trim(),
    node(arm, `F1: 机器那一半与这台设备答的不逐字相同（${(text.split(" · ").slice(-1)[0] ?? "").trim()}）`),
  ).toBe("http_status=200 content_type=text/html;charset=UTF-8 evidence=HTTP_RESPONSE");
});

test("N7_F2_a_rejected_extraction_shows_its_machine_status_not_the_reply_s_sentence — F2：服务器那句 reason 不再当状态行", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "F2";
  const id = await journeyId(page);
  await page.route("**/api/v1/journeys/**", async (r: Route) => {
    const path = new URL(r.request().url()).pathname;
    if (path.endsWith("/inbox/text-extract") && r.request().method() === "POST") {
      await r.fulfill({status: 200, contentType: "application/json",
        body: jsonBody({status: "REJECTED", reason: F_SENTINELS.F2})});
      return;
    }
    return r.fallback();
  });
  await openWallet(page, id);
  const text = await runPasteExtraction(page, arm);
  const dom = await domReading(page);
  readings[arm] = {
    message_text: text,
    sentinel_on_screen: dom.root.includes(F_SENTINELS.F2) || dom.body.includes(F_SENTINELS.F2),
    sentinel_in_attributes: [F_SENTINELS.F2].filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
    machine_status_in_line: text.includes("REJECTED"),
    last_token_equals: (text.trim().split(/\s+/).slice(-1)[0] ?? "") === "REJECTED",
  };
  expect(readings[arm].sentinel_on_screen, node(arm, `F2: 服务器那句原话又成了这一屏的状态行（${text.slice(0, 220)}）`)).toBe(false);
  expect(readings[arm].sentinel_in_attributes, node(arm, `F2: Q71 口径下原话进了属性也是上屏（${String(dom.attr_pairs)} 对）`)).toEqual([]);
  expect(readings[arm].machine_status_in_line, node(arm, `F2: 状态行不再说明这台答复自己的机器状态（${text.slice(0, 220)}）`)).toBe(true);
  expect((text.trim().split(/\s+/).slice(-1)[0] ?? ""),
    node(arm, `F2: 这一行的最后一个记号不是那枚机器枚举，而是 "${(text.trim().split(/\s+/).slice(-1)[0] ?? "")}"（${text.slice(0, 220)}）`),
  ).toBe("REJECTED");
});

test("N7_F3_a_memory_row_shows_what_it_measured_not_the_model_s_sentence — F3：纪念册那一行的推理摘要不再原样上屏", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "F3";
  const id = await journeyId(page);
  await page.route("**/api/v1/journeys/*/memory", async (r: Route) => {
    await r.fulfill({status: 200, contentType: "application/json",
      body: jsonBody({items: [memoryRow]})});
  });
  await openWallet(page, id);
  const row = page.getByTestId("v13-2-memory").locator("li").first();
  await expect(row, node(arm, "F3: 这台设备的记忆行根本没渲染，摘要在不在屏上就没有可判的对象")).toBeVisible({timeout: 10_000});
  const rowText = clean(await row.textContent());
  const dom = await domReading(page);
  readings[arm] = {
    row_text: rowText,
    sentinel_on_screen: dom.root.includes(F_SENTINELS.F3) || dom.body.includes(F_SENTINELS.F3),
    sentinel_in_attributes: [F_SENTINELS.F3].filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
    held_token_present: (await page.getByTestId("v13-2-memory-summary-held").count()) > 0,
    held_text: (await page.getByTestId("v13-2-memory-summary-held").count())
      ? clean(await page.getByTestId("v13-2-memory-summary-held").textContent().catch(() => "")) ?? ""
      : "",
    own_fields_present: rowText.includes("ORD0323-M1-LABEL") && rowText.includes("62"),
  };
  expect(readings[arm].sentinel_on_screen, node(arm, `F3: 模型的推理摘要被这一屏当成自己的话说出来了（${rowText.slice(0, 200)}）`)).toBe(false);
  expect(readings[arm].sentinel_in_attributes, node(arm, `F3: Q71 口径下摘要进了属性也是上屏（${String(dom.attr_pairs)} 对）`)).toEqual([]);
  expect(readings[arm].held_token_present, node(arm, "F3: 这一行不再说明摘要存在但不在这里展示")).toBe(true);
  expect(readings[arm].held_text,
    node(arm, `F3: 那一格写的不是这句设备自己的话（"${String(readings[arm].held_text)}"）`)).toBe("reasoning_summary=held 原文不在这一屏");
  expect(readings[arm].own_fields_present, node(arm, `F3: 这一行自己的读数不在了（${rowText.slice(0, 200)}）`)).toBe(true);
});

test("N7_F4_the_draft_review_shows_its_own_fields_not_the_model_s_sentence — F4：待审那一屏的推理摘要不再原样上屏", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "F4";
  const id = await journeyId(page);
  await page.route("**/api/v1/journeys/**", async (r: Route) => {
    const path = new URL(r.request().url()).pathname;
    const method = r.request().method();
    if (path.endsWith("/inbox/text-extract") && method === "POST") {
      await r.fulfill({status: 200, contentType: "application/json",
        body: jsonBody({status: "READY", draft: interpretationDraft})});
      return;
    }
    if (path.endsWith("/inbox") && method === "GET") {
      await r.fulfill({status: 200, contentType: "application/json", body: jsonBody({drafts: [interpretationDraft]})});
      return;
    }
    return r.fallback();
  });
  await openWallet(page, id);
  await runPasteExtraction(page, arm);
  await expect(
    page.getByTestId("v13-2-review"),
    node(arm, "F4: 待审那一屏根本没开，摘要在不在就没有可判的对象"),
  ).toBeVisible({timeout: 30_000});
  const dom = await domReading(page);
  const review = clean(await page.getByTestId("v13-2-review").textContent());
  const interp = (await page.getByTestId("v13-2-interpretation").count())
    ? clean(await page.getByTestId("v13-2-interpretation").textContent().catch(() => "")) ?? ""
    : "";
  readings[arm] = {
    review_text: review,
    sentinel_on_screen: dom.root.includes(F_SENTINELS.F4) || dom.body.includes(F_SENTINELS.F4),
    sentinel_in_attributes: [F_SENTINELS.F4].filter((n) => dom.attrs.includes(n)),
    attribute_pairs_swept: dom.attr_pairs,
    held_token_present: (await page.getByTestId("v13-2-draft-summary-held").count()) > 0,
    held_text: (await page.getByTestId("v13-2-draft-summary-held").count())
      ? clean(await page.getByTestId("v13-2-draft-summary-held").textContent().catch(() => "")) ?? ""
      : "",
    interpretation_text: interp,
    interpretation_percent_field: (interp.match(/(\d+)\s*%/) || [null, ""])[1],
    /* the held notice sits against the payment value with no separator, so it is taken out before the
       field is parsed -- otherwise the "equality" would compare a glued string */
    payment_field: ((interp.replace("reasoning_summary=held 原文不在这一屏", "")
      .match(/支付\s*·\s*([^\s·]+)/)) || [null, ""])[1],
    own_fields_present: review.includes("到站") || review.includes("MID_TRIP") || /0\.8|80%/.test(review),
  };
  expect(readings[arm].sentinel_on_screen, node(arm, `F4: 模型的推理摘要被待审那一屏当成自己的话说出来了（${review.slice(0, 200)}）`)).toBe(false);
  expect(readings[arm].sentinel_in_attributes, node(arm, `F4: Q71 口径下摘要进了属性也是上屏（${String(dom.attr_pairs)} 对）`)).toEqual([]);
  expect(readings[arm].held_token_present, node(arm, "F4: 待审那一屏不再说明摘要存在但不在这里展示")).toBe(true);
  expect(readings[arm].held_text,
    node(arm, `F4: 那一格写的不是这句设备自己的话（"${String(readings[arm].held_text)}"）`)).toBe("reasoning_summary=held 原文不在这一屏");
  /* the two directions of F4 read different fields, so each is compared whole */
  expect(readings[arm].payment_field,
    node(arm, `F4: 支付那一格不再是这台设备从这份答复里读到的值（"${String(readings[arm].payment_field)}" / ${interp.slice(0, 200)}）`)).toBe("已支付");
  expect(readings[arm].interpretation_percent_field,
    node(arm, `F4: 置信度那一格不是 80%（"${String(readings[arm].interpretation_percent_field)}"）`)).toBe("80");
});
