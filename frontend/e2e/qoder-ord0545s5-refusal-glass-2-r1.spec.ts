/* ORD-0545 slice 5 -- the two refusal sentences the backend never emits have to be read off real glass.
 *
 * Slice 2 moved the family into `poiImportCopy.ts`; slice 3 read `errNoActivePlan` on glass; slice 4
 * walked the whole screen cell by cell.  `review/ord0545/readouts/refusal_reachability_probe_r1.json`
 * measured what is left: of the seven keys only reachable through FAILURE_SENTENCE,
 *
 *   JOURNEY_NOT_FOUND / IMPORT_DAY_REQUIRED / IMPORT_PLACE_UNKNOWN / IMPORT_PLACE_NOT_VISITABLE
 *       -- NOT reachable by a traveller's own next step on this build; each is named in this slice's
 *          evidence with the door that blocks it, and this file does not pretend to have read them.
 *   UNNAMED_REFUSAL / NO_RESPONSE_FROM_ORIGIN
 *       -- decided by THIS device, 0 emit points in backend/app, so only glass can settle them.
 *
 * The two this spec reads, and the antecedent for each, measured rather than assumed:
 *
 *   U  the paste door caps `text` at 20000 characters (backend/app/schemas.py, PoiImportParseRequest).
 *      A longer paste is a thing a traveller really does -- 粘一整篇攻略 -- and the door answers 422 with
 *      `error.code = "VALIDATION_ERROR"` and NO `reason_code` field at all, which is exactly the case
 *      `describeRejection` answers with UNNAMED_REFUSAL (frontend/src/features/poiimport/PoiImportPanel.tsx).
 *      So the screen must say the table's own `errUnnamed` half and the measured status, nothing else.
 *   N  a refusal that arrives as "the question was never answered" -- the device goes offline after the
 *      panel is mounted (the traveller's own network, not a mocked provider) -- must read `errNoResponse`,
 *      and its technical line is the SAME sentence, because that is what the component does at :461.
 *
 * Every arm reads rendered text, never an attribute, and every expected sentence is taken out of
 * `poiImportCopy.ts` at run time rather than retyped here, so a wording change moves the expectation with
 * it instead of turning this gate red for the wrong reason.  Reads are capped per call: an uncapped
 * `innerText()` on a cell that never appeared is the stall this lane measured at 02:3x.
 */
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const CANARY_TRIP = "22222222-2222-4222-8222-222222222222";
const TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const BACKEND = `http://127.0.0.1:${process.env.CITYNEXUS_ORD0545_BACKEND_PORT ?? "4921"}`;
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0545", "readouts");
const TAG = process.env.CITYNEXUS_ORD0545_TAG ?? "s5r1";
const BASE_TIP = process.env.CITYNEXUS_ORD0545_BASE_TIP ?? "";
const OVER_LONG = "北京一日".repeat(5_001); // 4 characters each -> 20_004, four past the door's own cap

const READ_CAP = { timeout: 5_000 };

function tablePair(key: string): { zh: string; en: string } {
  const src = fs.readFileSync(path.join(process.cwd(), "..", TABLE), "utf8");
  const start = src.indexOf(`\n  ${key}: {`);
  expect(start, `the table has no line for ${key}`).toBeGreaterThan(-1);
  const body = src.slice(start, src.indexOf("}", start));
  const zh = /zh:\s*"([^"]*)"/.exec(body);
  const en = /en:\s*"([^"]*)"/.exec(body);
  expect(zh, `${key} has no readable zh half`).not.toBeNull();
  expect(en, `${key} has no readable en half`).not.toBeNull();
  return { zh: zh[1], en: en[1] };
}

async function text(locator: any): Promise<string> {
  return ((await locator.innerText(READ_CAP)) ?? "").replace(/\s+/g, " ").trim();
}

/** Direct text nodes of one cell, so a nested technical line is not folded into the sentence it explains. */
async function ownText(locator: any): Promise<string> {
  return (
    (await locator.evaluate(
      (el: Element) =>
        Array.from(el.childNodes)
          .filter((n) => n.nodeType === 3)
          .map((n) => (n.textContent ?? "").replace(/\s+/g, " "))
          .join("")
          .trim(),
      READ_CAP,
    )) ?? ""
  );
}

/** Tokens that must never reach the glass, whatever the screen decides to say.
 *  Note this is a NAMED list, not "any upper-case word": the shipped dictionary itself capitalises the
 *  nav labels (`nav.groupToday`, `nav.groupTravel` and `nav.groupSystem` in frontend/src/i18n/LocaleContext.tsx answer "TODAY", "TRAVEL" and "SYSTEM"), so a
 *  blanket whole-screen assertion would be red on a screen that is exposing nothing. */
const FORBIDDEN_ON_SCREEN = [
  "VALIDATION_ERROR",
  "UNNAMED_REFUSAL",
  "NO_RESPONSE_FROM_ORIGIN",
  "IMPORT_DAY_REQUIRED",
  "IMPORT_PLACE_UNKNOWN",
  "IMPORT_PLACE_NOT_VISITABLE",
  "JOURNEY_NOT_FOUND",
  "NO_ACTIVE_PLAN",
  "request.invalid",
  "errUnnamed",
  "errNoResponse",
  "errNoActivePlan",
];

async function screenCensus(page: any) {
  const { rendered, raw } = await page.evaluate(() => ({
    rendered: document.body.innerText,
    raw: document.body.textContent ?? "",
  }));
  return {
    screen_characters: rendered.length,
    whole_screen_text_sha256: createHash("sha256").update(rendered, "utf8").digest("hex"),
    dictionary_key_names_on_screen: [...new Set(rendered.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? [])].sort(),
    table_key_names_on_screen: [...new Set(rendered.match(/\berr[A-Za-z]+/g) ?? [])].sort(),
    machine_words_by_rendered_text: [...new Set(rendered.match(/\b[A-Z][A-Z_]{4,}\b/g) ?? [])].sort(),
    forbidden_tokens_on_screen: FORBIDDEN_ON_SCREEN.filter((t) => raw.includes(t)),
    uuids_on_screen: [
      ...new Set(rendered.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g) ?? []),
    ].sort(),
  };
}

async function mountPanel(page: any, locale: string) {
  await page.addInitScript(
    ([id, which]) => {
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", which);
    },
    [CANARY_TRIP, locale],
  );
  await page.goto("/v8/import");
  await expect(page.getByTestId("poi-import"), "the import screen itself must be on the glass").toBeVisible({
    timeout: 40_000,
  });
}

/** How many stops the ACTIVE plan really has, read back from the plan door.
 *  GET /plans answers a LIST of versions (measured at this tip: 7 rows, one of them ACTIVE with 6 stops),
 *  so this reads that shape rather than inventing an envelope, and says so when it cannot find one.
 */
async function activePlanStopCount(request: any): Promise<number | "NOT_READ"> {
  const answer = await request.get(`${BACKEND}/api/v1/journeys/${CANARY_TRIP}/plans`);
  if (answer.status() !== 200) return "NOT_READ";
  const body = await answer.json().catch(() => null);
  const rows = Array.isArray(body) ? body : (body?.plans ?? body?.versions);
  if (!Array.isArray(rows)) return "NOT_READ";
  const active = rows.filter((p: any) => p?.status === "ACTIVE");
  if (active.length !== 1) return "NOT_READ";
  const activities = active[0]?.activities;
  return Array.isArray(activities) ? activities.length : "NOT_READ";
}

async function refuseByOverLongPaste(page: any, request: any, locale: string) {
  const pair = tablePair("errUnnamed");
  const before = await activePlanStopCount(request);

  await page.getByTestId("poi-import-text").fill(OVER_LONG, { timeout: 30_000 });
  const answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
  await page.getByTestId("poi-import-parse").click();
  const http = await answered;
  const envelope = await http.json().catch(() => null);
  // read the antecedent first, so a door that changed its mind is named as that instead of as a missing cell
  expect(http.status(), `the paste door answered ${http.status()}, so this walk is not a refusal at all`).toBeGreaterThanOrEqual(400);
  expect(
    envelope?.error?.reason_code === undefined,
    `the door now names its own reason (${JSON.stringify(envelope?.error?.reason_code)}), which is a different sentence`,
  ).toBe(true);

  const why = page.getByTestId("poi-import-error-why");
  await expect(why, "the unnamed refusal has to be read off the glass, not inferred").toBeVisible({
    timeout: 20_000,
  });
  const sentence = await text(why);
  const detail = await text(page.getByTestId("poi-import-error-detail"));
  const loadCell = await ownText(page.getByTestId("poi-import-error"));
  const after = await activePlanStopCount(request);

  return {
    arm: "U",
    locale,
    pasted_characters: OVER_LONG.length,
    parse_http_status: http.status(),
    // the antecedent this slice claims: the door refused WITHOUT naming a reason_code
    door_answered_without_a_reason_code: envelope?.error?.reason_code === undefined,
    door_error_code: envelope?.error?.code ?? null,
    message_key: envelope?.error?.message_key ?? null,
    sentence_on_glass: sentence,
    expected_from_the_table: pair[locale === "zh-CN" ? "zh" : "en"],
    sentence_equals_the_table_half: sentence === pair[locale === "zh-CN" ? "zh" : "en"],
    technical_line: detail,
    leading_cell_text: loadCell,
    refusal_cell_text: await text(page.getByTestId("poi-import-error")),
    machine_words_in_refusal_cell: (
      ((await text(page.getByTestId("poi-import-error"))) ?? "").match(/[A-Z][A-Z_]{4,}/g) ?? []
    ).sort(),
    plan_stops_before: before,
    plan_stops_after: after,
    wrote_nothing: before === "NOT_READ" || after === "NOT_READ" ? "NOT_READ" : before === after,
    ...(await screenCensus(page)),
  };
}

async function refuseByOfflineDevice(page: any, context: any, request: any, locale: string) {
  const pair = tablePair("errNoResponse");
  const before = await activePlanStopCount(request);

  await page.getByTestId("poi-import-text").fill("Central Food Hall", { timeout: 30_000 });
  await context.setOffline(true);
  let refused: string | null = null;
  try {
    await page.getByTestId("poi-import-parse").click();
    await expect(page.getByTestId("poi-import-error-why")).toBeVisible({ timeout: 20_000 });
    refused = "the sentence appeared";
  } finally {
    await context.setOffline(false);
  }
  const sentence = await text(page.getByTestId("poi-import-error-why"));
  const detail = await text(page.getByTestId("poi-import-error-detail"));
  const after = await activePlanStopCount(request);

  return {
    arm: "N",
    locale,
    mechanism: "the device's own network was dropped after the panel mounted (context.setOffline), " +
      "so no answer arrived at all -- this is not a mocked provider response",
    click_outcome: refused,
    sentence_on_glass: sentence,
    expected_from_the_table: pair[locale === "zh-CN" ? "zh" : "en"],
    sentence_equals_the_table_half: sentence === pair[locale === "zh-CN" ? "zh" : "en"],
    // measured: with no status to show, does the technical line repeat the refusal sentence?
    technical_line_is_the_same_sentence: detail === sentence,
    technical_line: detail,
    refusal_cell_text: await text(page.getByTestId("poi-import-error")),
    machine_words_in_refusal_cell: (
      ((await text(page.getByTestId("poi-import-error"))) ?? "").match(/[A-Z][A-Z_]{4,}/g) ?? []
    ).sort(),
    plan_stops_before: before,
    plan_stops_after: after,
    wrote_nothing: before === "NOT_READ" || after === "NOT_READ" ? "NOT_READ" : before === after,
    ...(await screenCensus(page)),
  };
}

test.describe("ORD-0545 slice 5 the two refusals only a device can decide, read off glass", () => {
  const collected: any[] = [];

  test("Uzh zh-CN 超长粘贴之后屏上那句逐字 == 表里 errUnnamed 的 zh 半", async ({ page, request }) => {
    await mountPanel(page, "zh-CN");
    const r = await refuseByOverLongPaste(page, request, "zh-CN");
    expect(r.pasted_characters, "the paste must really be over the door's cap").toBeGreaterThan(20_000);
    expect(r.parse_http_status, "the paste door's own cap answers 422").toBe(422);
    expect(r.door_answered_without_a_reason_code, "the antecedent moved: the door now names a reason").toBe(true);
    expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
    expect(r.technical_line, "the technical line is this device's own measurement of the status").toBe("HTTP 422");
    expect(r.machine_words_in_refusal_cell, `machine words inside the refusal cell: ${JSON.stringify(r.machine_words_in_refusal_cell)}`).toEqual([]);
    expect(r.forbidden_tokens_on_screen, `the door's own vocabulary reached the glass: ${JSON.stringify(r.forbidden_tokens_on_screen)}`).toEqual([]);
    expect(r.table_key_names_on_screen, `a table key name reached the screen: ${JSON.stringify(r.table_key_names_on_screen)}`).toEqual([]);
    expect(r.dictionary_key_names_on_screen).toEqual([]);
    expect(r.uuids_on_screen).toEqual([]);
    expect(r.wrote_nothing, "a refusal may not have moved the plan").toBe(true);
    collected.push(r);
  });

  test("Wen en-GB the same over-long paste reads the table's en half", async ({ page, request }) => {
    await mountPanel(page, "en-GB");
    const r = await refuseByOverLongPaste(page, request, "en-GB");
    expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
    expect(r.forbidden_tokens_on_screen).toEqual([]);
    expect(r.machine_words_in_refusal_cell).toEqual([]);
    expect(r.wrote_nothing).toBe(true);
    collected.push(r);
  });

  test("Nzh zh-CN 设备断网之后屏上那句逐字 == 表里 errNoResponse 的 zh 半", async ({ page, context, request }) => {
    await mountPanel(page, "zh-CN");
    const r = await refuseByOfflineDevice(page, context, request, "zh-CN");
    expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
    expect(r.forbidden_tokens_on_screen).toEqual([]);
    expect(r.machine_words_in_refusal_cell).toEqual([]);
    expect(r.technical_line_is_the_same_sentence, "ORD-0545 slice six re-cast this criterion: the technical line must no longer repeat the refusal sentence (CMDR 01:37 定点). The criterion changed on purpose -- the reading still has to be a real one, so the sentence itself is asserted above.").toBe(false);
    expect(r.wrote_nothing).toBe(true);
    collected.push(r);
  });

  test("Ren en-GB the same offline walk reads the table's en half", async ({ page, context, request }) => {
    await mountPanel(page, "en-GB");
    const r = await refuseByOfflineDevice(page, context, request, "en-GB");
    expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
    expect(r.forbidden_tokens_on_screen).toEqual([]);
    expect(r.machine_words_in_refusal_cell).toEqual([]);
    expect(r.technical_line_is_the_same_sentence).toBe(false);
    expect(r.wrote_nothing).toBe(true);
    collected.push(r);
  });

  test.afterAll(async () => {
    fs.mkdirSync(EVIDENCE, { recursive: true });
    const file = path.join(EVIDENCE, `refusal_glass2_${TAG}.json`);
    fs.writeFileSync(
      file,
      JSON.stringify(
        {
          generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() },
          over_long_paste_characters: OVER_LONG.length,
          arms: collected,
          // four sentences this slice measured and did NOT read -- the probe names the blocking door
          // which door blocks which sentence, taken from THIS generation's probe, named the same way
          not_reached_by_lane: JSON.parse(
            fs.readFileSync(
              path.join(EVIDENCE, process.env.CITYNEXUS_ORD0545S5_PROBE_NAME ?? "refusal_reachability_probe_r1.json"),
              "utf8",
            ),
          ).walks,
          probe_generation: process.env.CITYNEXUS_ORD0545S5_PROBE_NAME ?? "refusal_reachability_probe_r1.json",
        },
        null,
        1,
      ),
      "utf8",
    );
    console.log("evidence written", file, collected.length, "arms");
  });
});
