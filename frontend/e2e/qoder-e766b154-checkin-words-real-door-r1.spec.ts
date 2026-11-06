/**
 * lane e766b154 -- four refusal sentences read off the glass where the ANSWER came from the real door.
 *
 * The order (CMDR 19:46): of the eleven words, four still had no screen reading of this kind --
 * `activityNotInJourney`, `activityUnknown`, `journeyUnknown`, `placeUnknown`. The day24 hand painted
 * them by answering the POST from inside the instrument; that proves the mapping table renders, and it
 * proves nothing about the server. Here nothing is invented: the browser's own POST is forwarded to this
 * lane's backend with a target that the store really rejects, and the sentence is painted from whatever
 * the door answered. Each arm records the status and `error.reason_code` the server itself returned, so
 * "the door said it" and "the screen said it" are two separate readings.
 *
 * The targets come from `real_door_targets_r1.json`, which was read out of the same sqlite file this api
 * is serving, with `?mode=ro`, and which proves each "unknown" id is absent by a count.
 *
 * Judgements read RENDERED TEXT. `data-testid` locates structure only. Every reading is written before
 * its assertion, so a red arm still leaves its reading behind.
 */
import { expect, test } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import { scanScreenText } from "./_qoderTextScan";
import { CHECKIN_COPY } from "../src/features/checkin/checkinCopy";

const FILE = "e2e/qoder-e766b154-checkin-words-real-door-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const OUT = process.env.CITYNEXUS_E766_REAL_OUT || "";
const BASE = process.env.CITYNEXUS_E766_REAL_BASE || "";
const TARGETS_PATH = process.env.CITYNEXUS_E766_REAL_TARGETS || "";
/* ORD-0576: this read used to happen at module scope, so an unset CITYNEXUS_E766_REAL_TARGETS aborted
 * Playwright's collection for the whole directory. The read is lazy now, and the file still refuses to run
 * its arms without a real target list -- see the named skip below. */
const TARGETS_MISSING = TARGETS_PATH
  ? null
  : "missing environment variable: CITYNEXUS_E766_REAL_TARGETS must name the targets file";
const TARGETS = (TARGETS_MISSING
  ? { targets: null, journey_used: { id: "", activities: 0 } }
  : JSON.parse(fs.readFileSync(TARGETS_PATH, "utf8"))) as {
    targets: Record<string, { post_journey_id: string; post_activity_id?: string;
                              post_place_id?: string; why: string }> | null;
    journey_used: { id: string; activities: number };
  };

const STAMPED = { "zh-CN": "已打卡", en: "Checked in" } as Record<string, string>;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const CASES = [
  { code: "CHECKIN_JOURNEY_UNKNOWN", copy: "journeyUnknown", status: 404 },
  { code: "CHECKIN_ACTIVITY_UNKNOWN", copy: "activityUnknown", status: 404 },
  { code: "CHECKIN_ACTIVITY_NOT_IN_JOURNEY", copy: "activityNotInJourney", status: 409 },
  { code: "CHECKIN_PLACE_UNKNOWN", copy: "placeUnknown", status: 404 },
];

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

function writeReading(name: string, body: Record<string, unknown>) {
  if (!OUT) return;
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, `${name}.json`), JSON.stringify(body, null, 1), "utf8");
}

async function writeShot(page: import("@playwright/test").Page, name: string) {
  if (!OUT) return;
  fs.mkdirSync(OUT, { recursive: true });
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

async function openScreen(page: import("@playwright/test").Page, locale: string) {
  await page.addInitScript((arg) => {
    window.localStorage.clear();
    window.localStorage.setItem("citynexus.v8.journey_id", arg.id);
    window.localStorage.setItem("citynexus.locale", arg.locale);
  }, { id: TARGETS.journey_used.id, locale });
  await page.goto(`${BASE}/v8/trips`, { waitUntil: "load" });
  await expect(page.locator("[data-testid='stop-checkin']").first(),
    node("settle", "no 打卡 control mounted")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(2_500);
}

async function paintedRows(page: import("@playwright/test").Page) {
  return await page.evaluate(() => {
    const strip = (x: string | null | undefined) => String(x || "").replace(/\s+/g, " ").trim();
    return Array.from(document.querySelectorAll("[data-testid='trip-stop']")).map((row) => ({
      stop: strip(row.querySelector(".trip-stop-body b")?.textContent),
      control: strip(row.querySelector("[data-testid='stop-checkin']")?.textContent),
      notice: strip(row.querySelector("[data-testid='stop-checkin-notice']")?.textContent) || null,
    }));
  });
}

async function doorRows(page: import("@playwright/test").Page) {
  const answer = await page.request.get(
    `${BASE}/api/v1/journeys/${TARGETS.journey_used.id}/checkins`);
  expect(answer.status(), node("door-read", "the footprint read door did not answer 200")).toBe(200);
  return await answer.json() as unknown[];
}

async function tapFirstUnstamped(page: import("@playwright/test").Page, locale: string) {
  const rows = await paintedRows(page);
  const fresh = rows.find((r) => r.control === (locale === "zh-CN" ? "打卡" : "Check in"));
  expect(fresh, node("tap", `no unstamped control on this screen: ${rows
    .map((r) => r.control).join(" | ")}`)).toBeTruthy();
  const control = page.locator("[data-testid='trip-stop']").filter({ hasText: fresh!.stop })
    .locator("[data-testid='stop-checkin']");
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(3_500);
  return fresh!;
}

/** Forward the page's own POST at a target the store really rejects, and keep the door's real answer. */
function realRefusalRoute(target: { post_journey_id: string; post_activity_id?: string;
                                    post_place_id?: string }, seen: Record<string, unknown>) {
  return async (route: import("@playwright/test").Route) => {
    if (route.request().method() !== "POST") return route.continue();
    const data = target.post_place_id ? JSON.stringify({ place_id: target.post_place_id })
      : JSON.stringify({ activity_id: target.post_activity_id });
    const response = await route.fetch({
      url: `${BASE}/api/v1/journeys/${target.post_journey_id}/checkins`,
      method: "POST", data, headers: { "content-type": "application/json" },
    });
    const text = await response.text();
    let parsed: { error?: { reason_code?: string; message?: string; details?: object } } = {};
    try { parsed = JSON.parse(text); } catch { parsed = { error: { message: text.slice(0, 120) } }; }
    seen.status = response.status();
    seen.reason_code = parsed.error?.reason_code ?? null;
    seen.server_message = parsed.error?.message ?? null;
    seen.server_details_keys = Object.keys(parsed.error?.details ?? {}).sort();
    seen.answered_by = `real backend behind ${BASE}`;
    return route.fulfill({
      status: response.status(), body: text,
      headers: { "content-type": response.headers()["content-type"] || "application/json" },
    });
  };
}

test.beforeAll(async () => {
  /* ORD-0576: this was `throw new Error("ABORT missing env")`. The refusal is the same -- nothing runs
   * without a target list and a door to point at -- but it is a named skip now, so the report says which
   * variable is unset instead of the whole directory failing to collect. */
  const missing = [
    !BASE && "CITYNEXUS_E766_REAL_BASE",
    !OUT && "CITYNEXUS_E766_REAL_OUT",
    !TARGETS.targets && "CITYNEXUS_E766_REAL_TARGETS",
  ].filter(Boolean) as string[];
  test.skip(missing.length > 0, `missing prerequisite(s): ${missing.join(", ")}`);
});

for (const locale of ["zh-CN", "en"]) {
  test(`r-${locale}-control-a-real-stamp-is-not-a-refusal — 真打卡落一枚，屏上不出现拒绝句`, async ({
    page }) => {
    test.setTimeout(150_000);
    await openScreen(page, locale);
    const before = await doorRows(page);
    const tapped = await tapFirstUnstamped(page, locale);
    const after = await doorRows(page);
    const rows = await paintedRows(page);
    const scan = await scanScreenText(page);
    const withNotice = rows.filter((r) => r.notice);
    const reading = {
      file: FILE, kind: "control", locale, stop_tapped: tapped.stop, base: BASE,
      door_rows_before: before.length, door_rows_after: after.length,
      notices_count: withNotice.length, painted_notice: withNotice[0]?.notice ?? null,
      stamped_word_on_glass: scan.visual.includes(STAMPED[locale]),
      scan_visual_leaves: scan.visualLeaves, taken_at: new Date().toISOString(),
    };
    writeReading(`r-${locale}-control`, reading);
    await writeShot(page, `r-${locale}-control`);
    expect(after.length, node("control", "a real stamp did not add a row to the door")).toBe(
      before.length + 1);
    expect(withNotice.length, node("control", "a success painted a refusal sentence")).toBe(0);
    expect(scan.visual.includes(STAMPED[locale]), node("control",
      "the stop does not show the stamped word after a 201")).toBe(true);
  });

  for (const item of CASES) {
    test(`r-${locale}-${item.copy} — 真门答的这句才上屏`, async ({ page }) => {
      test.setTimeout(150_000);
      const seen: Record<string, unknown> = {};
      const target = TARGETS.targets[item.code];
      await openScreen(page, locale);
      const before = await doorRows(page);
      await page.route("**/api/v1/journeys/**/checkins", realRefusalRoute(target, seen));
      const tapped = await tapFirstUnstamped(page, locale);
      const rows = await paintedRows(page);
      const withNotice = rows.filter((r) => r.notice);
      const after = await doorRows(page);
      const scan = await scanScreenText(page);
      const painted = withNotice.length === 1 ? String(withNotice[0].notice) : null;
      const expected = locale === "zh-CN" ? CHECKIN_COPY[item.copy as keyof typeof CHECKIN_COPY].zh
        : CHECKIN_COPY[item.copy as keyof typeof CHECKIN_COPY].en;
      const checks = {
        door_answered_the_named_code: seen.reason_code === item.code,
        door_answered_the_expected_status: seen.status === item.status,
        refusal_added_no_footprint_row: after.length === before.length,
        exactly_one_notice_on_the_whole_screen: withNotice.length === 1,
        notice_owner_is_the_stop_that_was_tapped: withNotice.length === 1
          && withNotice[0].stop === tapped.stop,
        painted_equals_the_tables_own_row: painted === expected,
        no_uuid_reached_the_travellers_sentence: painted !== null && !UUID.test(painted),
        no_id_of_any_kind_in_the_sentence: painted !== null
          && !UUID.test(painted) && !/journey|activity_id|place_id/i.test(painted),
        servers_own_prose_stays_off_the_screen: painted !== null
          && !!seen.server_message && !scan.visual.includes(String(seen.server_message)),
        notice_is_on_the_painted_layer: painted !== null && scan.visual.indexOf(painted) >= 0,
      };
      writeReading(`r-${locale}-${item.copy}`, {
        file: FILE, kind: "real_door", locale, copy_key: item.copy, code_expected: item.code,
        target_sent: target, base: BASE, viewport: "390x844",
        door_status: seen.status, door_reason_code: seen.reason_code,
        door_message: seen.server_message, door_details_keys: seen.server_details_keys,
        answered_by: seen.answered_by, stop_tapped: tapped.stop,
        notices_count: withNotice.length, painted_notice: painted,
        expected_from_table: expected, door_rows_before: before.length,
        door_rows_after: after.length, checks, scan_visual_leaves: scan.visualLeaves,
        taken_at: new Date().toISOString(),
      });
      await writeShot(page, `r-${locale}-${item.copy}`);
      const failed = Object.entries(checks).filter(([, v]) => v === false).map(([k]) => k);
      expect(failed, node(`r-${locale}-${item.copy}`,
        `did not land clean: ${failed.join(", ")} :: door=${String(seen.reason_code)}/`
        + `${String(seen.status)} painted=${String(painted)}`)).toEqual([]);
    });
  }
}
