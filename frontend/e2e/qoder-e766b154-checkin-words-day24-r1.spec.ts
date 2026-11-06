/**
 * lane e766b154 -- the footprint door's day22-new sentences, read off the painted glass at 390x844.
 *
 * The order (ledger CMDR 01:24): "手机打卡门新句的屏读 —— day22 起的新句在手机竖屏视口亲读一遍".
 * ORD-0500's own spec on the line reads ONE of those eleven rows (the zh duplicate refusal). This file
 * reads every row the door can answer with, in both locales, and says which of them actually reach the
 * screen -- which is the half that stays unproven when a table of words lands without anyone painting it.
 *
 * Two modes, one store each, chosen by env so nothing shares state:
 *   CITYNEXUS_E766_WORDS_MODE=fake  -- the POST is answered from this file (route.fulfill / route.abort),
 *                                      the GET is never touched, so the marks on screen stay the door's
 *                                      own rows and no stamp is written;
 *   CITYNEXUS_E766_WORDS_MODE=real  -- nothing is intercepted: the control is pressed twice for real and
 *                                      the door itself answers CHECKIN_DUPLICATE_STAMP. Locale comes from
 *                                      CITYNEXUS_E766_WORDS_LOCALE (zh-CN | en).
 *
 * Judgements read RENDERED TEXT (the notice's own innerText plus the painted-leaf scan), never a
 * data-testid value; the testid only locates structure. Every reading is written to
 * CITYNEXUS_E766_WORDS_OUT before its assertion runs, so a red arm still leaves its reading behind.
 */
import { expect, test } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import { scanScreenText } from "./_qoderTextScan";
import {
  CHECKIN_COPY,
  CHECKIN_REFUSAL_COPY,
  formatStampedMoment,
  type CheckinCopyKey,
} from "../src/features/checkin/checkinCopy";

const FILE = "e2e/qoder-e766b154-checkin-words-day24-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const OUT = process.env.CITYNEXUS_E766_WORDS_OUT || "";
const MODE = process.env.CITYNEXUS_E766_WORDS_MODE || "";
const REAL_LOCALE = process.env.CITYNEXUS_E766_WORDS_LOCALE || "zh-CN";
const BASE = process.env.CITYNEXUS_E766_WORDS_BASE || "";

const MOMENT_ZH = /\d{1,2}月\d{1,2}日/;
const MOMENT_EN = /\d{1,2} [A-Za-z]{3},? \d{2}:\d{2}/;
/* en-GB puts a comma between the day and the time ("3 Oct, 01:58"); the first cut of this ruler
   did not allow it, so an arm that had the moment on the glass read as if it did not. */
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const STAMPED = { "zh-CN": "已打卡", en: "Checked in" } as Record<string, string>;
const BUTTON = { "zh-CN": "打卡", en: "Check in" } as Record<string, string>;
const WON_MOMENT = "2026-10-02T04:07:00Z";
const OTHER_UUID = "5ee55555-5555-4555-8555-555555555555";

type FakeState = {
  key: CheckinCopyKey;
  reasonCode: string;
  status: number;
  details?: Record<string, unknown>;
  abort?: boolean;
};

/** The eleven words, each driven by the answer shape the door actually uses for it. */
const STATES: FakeState[] = [
  { key: "duplicateAt", reasonCode: "CHECKIN_DUPLICATE_STAMP", status: 409,
    details: { existing_checkin_id: OTHER_UUID, existing_stamped_at: WON_MOMENT } },
  { key: "duplicateNoMoment", reasonCode: "CHECKIN_DUPLICATE_STAMP", status: 409,
    details: { existing_checkin_id: OTHER_UUID } },
  { key: "activityUnknown", reasonCode: "CHECKIN_ACTIVITY_UNKNOWN", status: 404 },
  { key: "activityNotInJourney", reasonCode: "CHECKIN_ACTIVITY_NOT_IN_JOURNEY", status: 409 },
  { key: "placeUnknown", reasonCode: "CHECKIN_PLACE_UNKNOWN", status: 404 },
  { key: "targetRequired", reasonCode: "CHECKIN_TARGET_REQUIRED", status: 422 },
  { key: "targetAmbiguous", reasonCode: "CHECKIN_TARGET_AMBIGUOUS", status: 422 },
  { key: "journeyInBin", reasonCode: "CHECKIN_JOURNEY_IN_RECYCLE_BIN", status: 410 },
  { key: "journeyUnknown", reasonCode: "CHECKIN_JOURNEY_UNKNOWN", status: 404 },
  { key: "unexpected", reasonCode: "CHECKIN_A_NAME_Nobody_Has_A_Doctor_For", status: 418 },
  { key: "unreachable", reasonCode: "TRANSPORT_DID_NOT_ANSWER", status: 0, abort: true },
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
  const seeded = await (await page.request.get(`${BASE}/api/v1/demo/scene`)).json() as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "the scene door answered no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((arg) => {
    window.localStorage.clear();
    window.localStorage.setItem("citynexus.v8.journey_id", arg.id);
    window.localStorage.setItem("citynexus.locale", arg.locale);
  }, { id: journeyId, locale });
  await page.goto(`${BASE}/v8/trips`, { waitUntil: "load" });
  await expect(
    page.locator("[data-testid='stop-checkin']").first(),
    node("settle", "no 打卡 control ever mounted on this screen"),
  ).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(2_500);
  return journeyId;
}

/** Per-stop rows as painted: the owner's title, the control's own word, the notice's own words. */
async function paintedRows(page: import("@playwright/test").Page) {
  return await page.evaluate(() => {
    const strip = (x: string | null | undefined) => String(x || "").replace(/\s+/g, " ").trim();
    return Array.from(document.querySelectorAll("[data-testid='trip-stop']")).map((row) => {
      const title = row.querySelector(".trip-stop-body b");
      const control = row.querySelector("[data-testid='stop-checkin']");
      const notice = row.querySelector("[data-testid='stop-checkin-notice']");
      return {
        stop: title ? strip(title.textContent) : "OWNER_UNRESOLVED",
        control: control ? strip(control.textContent) : null,
        notice: notice ? strip(notice.textContent) : null,
      };
    });
  });
}

async function tapFirstUnstamped(page: import("@playwright/test").Page, locale: string) {
  const rows = await paintedRows(page);
  const want = BUTTON[locale];
  const done = STAMPED[locale];
  const fresh = rows.find((r) => r.control === want);
  expect(fresh, node("tap", `no control painted exactly "${want}" (and not "${done}") on this `
    + `screen -- the row words read: ${rows.map((r) => r.control).join(" | ")}`)).toBeTruthy();
  const control = page.locator("[data-testid='trip-stop']").filter({ hasText: fresh!.stop })
    .locator("[data-testid='stop-checkin']");
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(3_500);
  return fresh!;
}

if (MODE === "fake") {
  for (const locale of ["zh-CN", "en"]) {
    for (const state of STATES) {
      test(`w-${locale}-${state.key} — 服务端那样答，屏上就说这句`, async ({ page }) => {
        test.setTimeout(120_000);
        await openScreen(page, locale);
        await page.route(`**/api/v1/journeys/**/checkins`, async (route) => {
          if (route.request().method() !== "POST") return route.continue();
          if (state.abort) return route.abort();
          return route.fulfill({
            status: state.status,
            contentType: "application/json",
            body: JSON.stringify({ error: { reason_code: state.reasonCode,
                                            details: state.details ?? {},
                                            message: "instrumented refusal" } }),
          });
        });
        const tapped = await tapFirstUnstamped(page, locale);
        const rows = await paintedRows(page);
        const withNotice = rows.filter((r) => r.notice);
        const scan = await scanScreenText(page);
        const zh = locale === "zh-CN";
        const params: Record<string, string> = {};
        if (state.key === "duplicateAt") {
          params.moment = formatStampedMoment(WON_MOMENT, zh);
        }
        if (state.key === "targetRequired") {
          params.button = String(tapped.control || "");
        }
        if (state.key === "unexpected") params.status = String(state.status);
        const entry = CHECKIN_COPY[state.key];
        const raw = zh ? entry.zh : entry.en;
        const expected = raw.replace(/\{(\w+)\}/g, (_all, name) =>
          Object.prototype.hasOwnProperty.call(params, name) ? params[name] : "{" + name + "}");
        const painted = withNotice.length === 1 ? String(withNotice[0].notice) : null;
        const checks = {
          exactly_one_notice_on_the_whole_screen: withNotice.length === 1,
          notice_owner_is_the_stop_that_was_tapped: withNotice.length === 1 &&
            withNotice[0].stop === tapped.stop,
          painted_equals_the_tables_own_row: painted === expected,
          no_brace_placeholder_left_on_the_glass: painted !== null && painted.indexOf("{") < 0,
          no_uuid_reached_the_travellers_sentence: painted !== null && !UUID.test(painted),
          no_server_reason_code_on_the_glass: painted !== null &&
            painted.indexOf(state.reasonCode) < 0,
          notice_is_on_the_painted_layer: painted !== null && scan.visual.indexOf(painted) >= 0,
          moment_shape_painted: state.key === "duplicateAt"
            ? painted !== null && (zh ? MOMENT_ZH : MOMENT_EN).test(painted) : "NOT_APPLICABLE",
          server_status_painted: state.key === "unexpected"
            ? painted !== null && painted.indexOf("418") >= 0 : "NOT_APPLICABLE",
        };
        writeReading(`w-${locale}-${state.key}`, {
          file: FILE, mode: MODE, locale, state: state.key, reason_code_sent: state.reasonCode,
          status_sent: state.status, details_sent: state.details ?? null, aborted: !!state.abort,
          base: BASE, viewport: "390x844", has_touch: true,
          stop_tapped: tapped.stop, control_word_painted: tapped.control,
          notices_count: withNotice.length, notice_owner: withNotice[0] ? withNotice[0].stop : null,
          painted_notice: painted, expected_from_table: expected, params_used: params,
          checks, scan_visual_leaves: scan.visualLeaves,
          accessible_layer_has_it: scan.accessible.indexOf(String(painted)) >= 0,
          taken_at: new Date().toISOString(),
        });
        await writeShot(page, `w-${locale}-${state.key}`);
        const failed = Object.entries(checks).filter(([, v]) => v === false).map(([k]) => k);
        expect(failed, node(`w-${locale}-${state.key}`,
          `the sentence did not land clean: ${failed.join(", ")} :: painted=${String(painted)}`))
          .toEqual([]);
        expect(scan.visualLeaves, node(`w-${locale}-${state.key}`,
          "the painted-text scan read no leaves -- nothing was looked at")).toBeGreaterThan(10);
      });
    }
  }
}

if (MODE === "real") {
  test(`w-real-${REAL_LOCALE} — 真按两下，门自己答的那句在屏上`, async ({ page }) => {
    test.setTimeout(150_000);
    const journeyId = await openScreen(page, REAL_LOCALE);
    const tapped = await tapFirstUnstamped(page, REAL_LOCALE);
    await page.waitForTimeout(1_500);
    const rowsAfterFirst = (await paintedRows(page)).filter((r) => r.notice);
    const control = page.locator("[data-testid='trip-stop']").filter({ hasText: tapped.stop })
      .locator("[data-testid='stop-checkin']");
    await control.scrollIntoViewIfNeeded();
    await control.click();
    await page.waitForTimeout(4_000);
    await control.scrollIntoViewIfNeeded();
    const rows = await paintedRows(page);
    const withNotice = rows.filter((r) => r.notice);
    const scan = await scanScreenText(page);
    const zh = REAL_LOCALE === "zh-CN";
    const painted = withNotice.length === 1 ? String(withNotice[0].notice) : null;
    const doorRows = await (await page.request.get(
      `${BASE}/api/v1/journeys/${journeyId}/checkins`)).json() as { target_key: string }[];
    const reading = {
      file: FILE, mode: MODE, locale: REAL_LOCALE, state: "real_duplicate", base: BASE,
      viewport: "390x844", has_touch: true, journey_id_len: journeyId.length,
      stop_tapped: tapped.stop, control_word_painted_after_first_press: rowsAfterFirst.length,
      notices_count: withNotice.length, notice_owner: withNotice[0] ? withNotice[0].stop : null,
      painted_notice: painted, expected_from_table: zh ? CHECKIN_COPY.duplicateAt.zh
        : CHECKIN_COPY.duplicateAt.en,
      door_rows_after_the_two_presses: doorRows.length,
      stamped_word_on_glass: scan.visual.includes(zh ? STAMPED["zh-CN"] : STAMPED.en),
      moment_shape_painted: painted !== null && (zh ? MOMENT_ZH : MOMENT_EN).test(painted),
      uuid_on_glass: painted !== null && UUID.test(painted),
      scan_visual_leaves: scan.visualLeaves,
      taken_at: new Date().toISOString(),
    };
    writeReading(`w-real-${REAL_LOCALE}`, reading);
    await writeShot(page, `w-real-${REAL_LOCALE}`);
    expect(withNotice.length, node(`w-real-${REAL_LOCALE}`,
      `the refusal was painted under ${withNotice.length} stops after one double tap`)).toBe(1);
    expect(painted !== null && (zh ? MOMENT_ZH : MOMENT_EN).test(painted),
      node(`w-real-${REAL_LOCALE}`, `the door named a moment but the screen did not paint it: ${painted}`))
      .toBe(true);
    expect(painted !== null && !UUID.test(painted),
      node(`w-real-${REAL_LOCALE}`, "an identifier reached the traveller's sentence")).toBe(true);
    expect(scan.visual.includes(zh ? STAMPED["zh-CN"] : STAMPED.en),
      node(`w-real-${REAL_LOCALE}`, "no 已打卡/Stamped on the glass, so the sentence is not about a "
        + "stamp the screen can see")).toBe(true);
  });
}
