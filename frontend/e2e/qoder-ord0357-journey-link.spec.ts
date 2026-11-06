/**
 * ORD-0357 — the traveller can get the journey's own link out of the phone.
 *
 * ORD-0351 made `/v8/<uuid>` a real entry point (`dbe6d285cb9dbe7654cf43caeb12940b2d5437c8`), and the
 * tip this gate was written against still held exactly one writer of that address:
 * `frontend/src/app/V8Experience.tsx` `window.history.pushState({}, "", `/v8/${value}`)`. A phone
 * hides the address bar, so the entry existed and the traveller still could not hand it to anyone.
 *
 * Cells:
 *   A1  the URL is absolute, its path is the route, its tail is the uuid -- and the same predicate is
 *       re-run on a hollowed uuid, where it must report a reason. R1's "no pseudo link".
 *   A2  the three refusals each name their own fact, in both directions, including which one wins when
 *       two hold at once. R2's "the clipboard refusing is not the journey being gone".
 *   B1  on the real screen, one press puts the absolute link in the real clipboard.
 *   B2  a device that will not accept a write says so, and does not claim the journey vanished.
 *   B3  a non-secure context says so, and does not claim the journey vanished.
 *   B4  a refusal costs no second screen and leaks no prose into an attribute (Q64=① / Q71).
 *
 * The counter-example that lives outside this file: `review/n3_mutation_hollow_r1.py` deletes the
 * empty-id guard at `frontend/src/lib/journeyLink.ts`, re-runs A1 and B1, and requires them red.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { buildJourneyLinkUrl, copyJourneyLink, journeyLinkFailure } from "../src/lib/journeyLink";

const FILE = "e2e/qoder-ord0357-journey-link.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
/* The origin the browser arms will really be running on: the suite's own vite port, not a base the
   product knows about. A link that only matches a hard-coded origin is the defect R1 forbids. */
const FE_PORT = process.env.CITYNEXUS_E2E_PORT ?? "0";
const ORIGIN = `http://127.0.0.1:${FE_PORT}`;
const UUID = "0f6f47d4-5a3e-4a72-9d3c-4c9f2b7e1a55";

const COPY = {
  button: "复制行程链接",
  copied: "行程链接已复制到这台设备的剪贴板",
  noJourney: "这里没有打开的行程",
  insecure: "不是安全上下文",
  clipboard: "拒绝了剪贴板写入",
};

type Readings = Record<string, unknown>;
const readings: Readings = {};

/**
 * One predicate, used by both the pure arms and the browser arm, so "green on the page" and "red on a
 * hollowed uuid" are the same measurement rather than two similar-looking ones.
 * Returns null when the shape holds, and the reason it does not when it does not.
 */
function whyTheLinkShapeFails(url: string | null, journeyId: string): string | null {
  if (url === null) return "url is null, so there is nothing to hand over";
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) return `not absolute: "${url}"`;
  const parsed = new URL(url);
  if (parsed.hostname === "") return "no host";
  if (parsed.pathname !== `/v8/${journeyId}`) return `path is "${parsed.pathname}"`;
  if (!url.endsWith(journeyId)) return `tail is not the uuid: "${url.slice(-8)}"`;
  return null;
}

async function domSweep(page: Page) {
  return page.evaluate(() => {
    const pairs = Array.from(document.querySelectorAll("*")).flatMap((el) =>
      Array.from(el.attributes)
        .filter((a) => a.name.startsWith("data-") || a.name.startsWith("aria-"))
        .map((a) => `${el.tagName.toLowerCase()}|${a.name}|${a.value}`),
    );
    return {
      body: document.body.textContent ?? "",
      attrs: pairs.join("\n"),
      attr_pairs: pairs.length,
    };
  });
}

async function boot(page: Page, journeyId: string, breakEnv?: "clipboard" | "secure") {
  await page.addInitScript(
    ({ id, which, origin }) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", "zh-CN");
      if (which === "clipboard") {
        Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
      }
      if (which === "secure") {
        Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
      }
      /* The arm must show it landed on the page it claims to be judging. */
      (window as unknown as Record<string, unknown>).__n3Env = {
        clipboard_present: Boolean(navigator.clipboard?.writeText),
        is_secure: window.isSecureContext === true,
        origin,
      };
    },
    { id: journeyId, which: breakEnv ?? "", origin: ORIGIN },
  );
  page.setDefaultTimeout(30_000);
  await page.setViewportSize({ width: 390, height: 844 });
}

async function stageJourneyId(page: Page): Promise<string> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("boot", "the stage named no journey, so the work bench has nothing to link to"));
  return id;
}

function armOf(title: string): string {
  return /^([AB]\d)/.exec(title)?.[1] ?? title.slice(0, 2);
}

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const body: Readings = { ...(readings[arm] ?? {}) };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 600);
  }
  writeFileSync(
    evidencePath(`ord0357/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

/* ------------------------------------------------------------------ A1: the link, and its hollow twin */

test("A1_the_link_is_absolute_and_its_tail_is_the_uuid — 有行程：链接是绝对的、路径是 /v8/、尾段就是那枚 uuid，挖空同一格要红", async () => {
  const built = buildJourneyLinkUrl(UUID, ORIGIN);
  const reason = whyTheLinkShapeFails(built, UUID);
  /* The counter-example in the same cell: the identical predicate over a uuid hollowed the way a
     cleared journey_id hollows it (empty, and whitespace-only). */
  const hollow = buildJourneyLinkUrl("", ORIGIN);
  const blank = buildJourneyLinkUrl("   ", ORIGIN);
  const hollowReasonEmpty = whyTheLinkShapeFails(hollow, "");
  const hollowReasonBlank = whyTheLinkShapeFails(blank, "   ");
  readings.A1 = {
    origin: ORIGIN,
    built_url: built,
    built_url_length: String(built ?? "").length,
    shape_reason: reason,
    hollow_url: hollow,
    blank_url: blank,
    hollow_reason_empty: hollowReasonEmpty,
    hollow_reason_blank: hollowReasonBlank,
  };
  expect(reason, node("A1", `the link the product builds is not the link R1 asks for (${JSON.stringify(built)})`)).toBeNull();
  expect(built, node("A1", `the link does not start from the origin it was built on (${built})`)).toBe(`${ORIGIN}/v8/${UUID}`);
  expect(hollow, node("A1", "an absent uuid produced a link anyway -- this is the pseudo link R1 forbids")).toBeNull();
  expect(blank, node("A1", "a whitespace-only uuid produced a link anyway")).toBeNull();
  expect(hollowReasonEmpty, node("A1", "the predicate accepted a hollowed uuid, so every A1/B1 green reading is vacuous")).not.toBeNull();
  expect(hollowReasonBlank, node("A1", "the predicate accepted a whitespace-only uuid, so it is not really reading the tail")).not.toBeNull();
});

/* ------------------------------------------------------------------ A2: three refusals, three facts */

test("A2_each_refusal_names_its_own_fact_in_both_directions — 三种失败各自具名，且反向必须不再报那一种", async () => {
  const ok = { journeyId: UUID, origin: ORIGIN, clipboardAvailable: true, isSecureContext: true };
  const cases = [
    { cell: "NO_JOURNEY", broken: { ...ok, journeyId: "" }, clean: ok },
    { cell: "CLIPBOARD_UNAVAILABLE", broken: { ...ok, clipboardAvailable: false }, clean: ok },
    { cell: "INSECURE_CONTEXT", broken: { ...ok, isSecureContext: false }, clean: ok },
  ] as const;
  const rows: Readings = {};
  for (const one of cases) {
    rows[`${one.cell}_broken`] = journeyLinkFailure(one.broken);
    rows[`${one.cell}_clean`] = journeyLinkFailure(one.clean);
  }
  /* Which fact speaks when two hold at once. Both orders are claims R2 makes: a traveller with no
     journey must not be told about permissions, and a traveller in a blocked context must hear the
     cause (the context) rather than the symptom (the clipboard that context refuses). */
  const noJourneyAndNoClipboard = journeyLinkFailure({ ...ok, journeyId: "", clipboardAvailable: false });
  const insecureAndNoClipboard = journeyLinkFailure({ ...ok, isSecureContext: false, clipboardAvailable: false });
  /* The write that was accepted by the API and then refused by policy -- the leg `clipboardAvailable`
     cannot see, so it is measured through the writer itself. */
  const refusedWrite = await copyJourneyLink(ok, () => Promise.reject(new Error("NotAllowedError")));
  const acceptedWrite = await copyJourneyLink(ok, () => Promise.resolve());
  readings.A2 = {
    ...rows,
    no_journey_and_no_clipboard: noJourneyAndNoClipboard,
    insecure_and_no_clipboard: insecureAndNoClipboard,
    refused_write_failure: refusedWrite.failure,
    refused_write_url_kept: refusedWrite.url,
    accepted_write_ok: acceptedWrite.ok,
  };
  for (const one of cases) {
    expect(rows[`${one.cell}_broken`], node("A2", `${one.cell}: the refusal is not named (${JSON.stringify(rows[`${one.cell}_broken`])})`)).toBe(one.cell);
    expect(rows[`${one.cell}_clean`], node("A2", `${one.cell}: reported on a healthy environment, so a green run cannot be bought by never failing`)).toBeNull();
  }
  expect(noJourneyAndNoClipboard, node("A2", "with no journey at all the device was blamed instead")).toBe("NO_JOURNEY");
  expect(insecureAndNoClipboard, node("A2", "the symptom spoke instead of the cause the traveller can act on")).toBe("INSECURE_CONTEXT");
  expect(refusedWrite.failure, node("A2", `a rejected write was filed as ${JSON.stringify(refusedWrite.failure)} instead of the clipboard`)).toBe("CLIPBOARD_UNAVAILABLE");
  expect(refusedWrite.url, node("A2", "a refused copy lost the link it was about, which is how a permission problem reads as a lost journey")).toBe(`${ORIGIN}/v8/${UUID}`);
  expect(acceptedWrite.ok, node("A2", "the accepted write did not report success -- this arm has no green direction")).toBe(true);
});

/* ------------------------------------------------------------------ B1: the real press, the real clipboard */

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

test("B1_one_press_puts_the_absolute_link_in_the_clipboard — 真点一次：剪贴板里就是那条绝对链接，尾段是那枚行程 id", async ({ page }) => {
  test.setTimeout(300_000);
  const id = await stageJourneyId(page);
  await boot(page, id);
  await page.goto("/v8/bookings", { waitUntil: "load" });
  const button = page.getByTestId("journey-link-copy");
  await expect(button, node("B1", "the named action never reached the screen, so there is nothing to press")).toBeVisible();
  const buttonLabel = (await button.textContent() ?? "").trim();
  const before = await page.evaluate(() => navigator.clipboard.readText().catch(() => "__unreadable__"));
  await button.click();
  await expect(
    page.getByTestId("journey-link-message"),
    node("B1", "the press produced no feedback in the message slot -- a silent copy is not a copied link"),
  ).toBeVisible({ timeout: 15_000 });
  const message = (await page.getByTestId("journey-link-message").textContent() ?? "").trim();
  const after = await page.evaluate(async () => {
    try {
      return await navigator.clipboard.readText();
    } catch {
      return "__unreadable__";
    }
  });
  const env = await page.evaluate(() => (window as unknown as Record<string, unknown>).__n3Env);
  const shapeReason = whyTheLinkShapeFails(String(after), id);
  readings.B1 = {
    journey_id: id,
    page_origin: env,
    button_label: buttonLabel,
    clipboard_before: before,
    clipboard_after: after,
    clipboard_after_length: String(after).length,
    message_text: message,
    shape_reason: shapeReason,
    tail_equals_journey_id: String(after).endsWith(id),
  };
  expect(buttonLabel.includes(COPY.button), node("B1", `the action is not named 「${COPY.button}」 (${buttonLabel})`)).toBe(true);
  expect(after, node("B1", "the clipboard still holds what the test put there -- nothing was copied")).not.toBe(before);
  expect(shapeReason, node("B1", `the copied text is not a journey link (${JSON.stringify(after)}): ${shapeReason}`)).toBeNull();
  expect(String(after).endsWith(id), node("B1", `the tail is not THIS journey's id (${after})`)).toBe(true);
  expect(message.includes(COPY.copied), node("B1", `it copied but the screen does not say so (${message})`)).toBe(true);
});

/* ------------------------------------------------------------------ B2 / B3: refusals keep their own names */

async function pressAndRead(page: Page, id: string, breakEnv: "clipboard" | "secure") {
  await boot(page, id, breakEnv);
  await page.goto("/v8/bookings", { waitUntil: "load" });
  const button = page.getByTestId("journey-link-copy");
  await expect(button, node(breakEnv, "the named action is not on the screen to be refused")).toBeVisible();
  await button.click();
  const message = page.getByTestId("journey-link-message");
  await expect(message, node(breakEnv, "the refusal left no sentence at all -- an empty screen is not an honest screen")).toBeVisible({
    timeout: 15_000,
  });
  const text = (await message.textContent() ?? "").trim();
  const dom = await domSweep(page);
  const env = await page.evaluate(() => (window as unknown as Record<string, unknown>).__n3Env);
  return { text, dom, env, buttonStillThere: await button.isVisible() };
}

test("B2_a_refused_clipboard_is_the_device_s_no_one_s_journey — 剪贴板被拒：说设备不让写，不说行程没了，也不加第二屏", async ({ page }) => {
  test.setTimeout(300_000);
  const id = await stageJourneyId(page);
  const { text, dom, env, buttonStillThere } = await pressAndRead(page, id, "clipboard");
  readings.B2 = {
    journey_id: id,
    page_origin: env,
    message_text: text,
    names_the_clipboard: text.includes(COPY.clipboard),
    claims_journey_lost: text.includes(COPY.noJourney),
    claims_insecure: text.includes(COPY.insecure),
    button_still_visible: buttonStillThere,
    sentence_in_attributes: [COPY.clipboard, COPY.noJourney, COPY.insecure].filter((s) => dom.attrs.includes(s)),
    attribute_pairs_swept: dom.attr_pairs,
  };
  /* Without this the arm could be green because the page never lost its clipboard at all. */
  expect((env as Record<string, unknown>).clipboard_present, node("B2", "the page still has a clipboard, so nothing was refused and this arm judged nothing")).toBe(false);
  expect((env as Record<string, unknown>).is_secure, node("B2", "the context also went insecure, so the sentence below is not attributable to the clipboard")).toBe(true);
  expect(text.includes(COPY.clipboard), node("B2", `the clipboard refusal is not named (${text})`)).toBe(true);
  expect(text.includes(COPY.noJourney), node("B2", `a permission the device withheld is being shown as a journey that vanished (${text}) -- R2 names this`)).toBe(false);
  expect(text.includes(COPY.insecure), node("B2", `the clipboard refusal borrowed the insecure-context sentence (${text})`)).toBe(false);
  expect(buttonStillThere, node("B2", "the refusal replaced the row instead of answering in it")).toBe(true);
  expect(readings.B2.sentence_in_attributes, node("B2", `Q71: a sentence sits in a data / aria value (${String(dom.attr_pairs)} pairs swept)`)).toEqual([]);
});

test("B3_an_insecure_context_names_the_context — 非安全上下文：说清是地址不安全，不说行程没了", async ({ page }) => {
  test.setTimeout(300_000);
  const id = await stageJourneyId(page);
  const { text, dom, env, buttonStillThere } = await pressAndRead(page, id, "secure");
  readings.B3 = {
    journey_id: id,
    page_origin: env,
    message_text: text,
    names_the_context: text.includes(COPY.insecure),
    claims_journey_lost: text.includes(COPY.noJourney),
    claims_clipboard: text.includes(COPY.clipboard),
    button_still_visible: buttonStillThere,
    sentence_in_attributes: [COPY.clipboard, COPY.noJourney, COPY.insecure].filter((s) => dom.attrs.includes(s)),
    attribute_pairs_swept: dom.attr_pairs,
  };
  /* The arm only means something if the page really was told it is not secure -- otherwise this is B1 again. */
  expect((env as Record<string, unknown>).is_secure, node("B3", "the page still reads as a secure context, so this arm judged nothing")).toBe(false);
  expect(text.includes(COPY.insecure), node("B3", `the context refusal is not named (${text})`)).toBe(true);
  expect(text.includes(COPY.noJourney), node("B3", `an unusable address is being shown as a lost journey (${text})`)).toBe(false);
  expect(text.includes(COPY.clipboard), node("B3", `the context refusal borrowed the clipboard sentence (${text})`)).toBe(false);
  expect(buttonStillThere, node("B3", "the refusal replaced the row instead of answering in it")).toBe(true);
  expect(readings.B3.sentence_in_attributes, node("B3", `Q71: a sentence sits in a data / aria value (${String(dom.attr_pairs)} pairs swept)`)).toEqual([]);
});

/* ------------------------------------------------------------------ B4: no second screen, no raw prose */

test("B4_a_refusal_costs_no_second_screen_and_carries_no_raw_prose — 拒绝之后：工造区还在原处，屏上没有设备与网络原文", async ({ page }) => {
  test.setTimeout(300_000);
  const id = await stageJourneyId(page);
  const { text, dom } = await pressAndRead(page, id, "clipboard");
  const panel = page.locator(".v10-inbox-workbench");
  await expect(panel, node("B4", "the work bench itself disappeared behind the refusal -- that is the second failure screen")).toBeVisible();
  await expect(
    page.getByTestId("inbox-tab-paste"),
    node("B4", "the intake tabs are gone, so the refusal took the panel over rather than answering in it"),
  ).toBeVisible();
  await expect(
    page.getByTestId("inbox-tab-manual"),
    node("B4", "the manual tab is gone with them"),
  ).toBeVisible();
  const body = dom.body;
  const q64_fields = ["http_status", "content_type", "body_bytes", "response_received", "elapsed_ms", "request_id"];
  readings.B4 = {
    journey_id: id,
    message_text: text,
    message_length: text.length,
    workbench_visible: await panel.isVisible(),
    intake_tabs_visible: true,
    q64_field_names_on_screen: q64_fields.filter((f) => body.includes(f)),
    q64_field_names_in_attributes: q64_fields.filter((f) => dom.attrs.includes(f)),
    attribute_pairs_swept: dom.attr_pairs,
    error_prototypes: ["Error:", "TypeError", "NotAllowedError", "at http", "undefined"].filter((p) => body.includes(p)),
  };
  expect(readings.B4.q64_field_names_on_screen, node("B4", `a copy refusal put network readings on the screen (${JSON.stringify(readings.B4.q64_field_names_on_screen)})`)).toEqual([]);
  expect(readings.B4.q64_field_names_in_attributes, node("B4", `Q71: network field names are carried in attributes (${JSON.stringify(readings.B4.q64_field_names_in_attributes)} of ${String(dom.attr_pairs)} pairs))`)).toEqual([]);
  expect(readings.B4.error_prototypes, node("B4", `raw engine or permission prose reached the traveller (${JSON.stringify(readings.B4.error_prototypes)})`)).toEqual([]);
  expect(text.length > 0, node("B4", "the message slot is empty, which is how a deleted screen passes a 'no second screen' check")).toBe(true);
});
