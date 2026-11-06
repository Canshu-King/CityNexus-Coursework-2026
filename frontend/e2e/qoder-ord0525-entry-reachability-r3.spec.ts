/* ORD-0525 · the cell the commander's own instrument counts: how many calendar words reach the glass
 *
 * Why this file exists separately from the five arms that landed with ORD-0525: the dispatch at 01:48
 * (`CITYNEXUS_AGENT_CONTROL.md:16604`) still lists 屏上零命中 as owed, and that wording comes from the
 * commander's own phone walk of 23:35 -- a five-cell probe at viewport 390x844 whose readings are
 * booleans and hit counts, not test names.  A green Playwright suite does not answer a
 * "zero hits on screen" reading; only the same measurement does.  So this arm reproduces his shape:
 *
 *   - walk the traveller's own route: 今天 -> the drawer's 行程 entry -> the day-by-day screen,
 *     clicking by test identity, no deep link and no invented address;
 *   - count the words 「日历」 and "calendar" in the rendered text of that screen (case-insensitive),
 *     which is exactly the quantity that read 0 before ORD-0525 landed;
 *   - take the entry's box, scroll it into view, and prove the point at its own centre is the entry;
 *   - press it and report what actually arrives: a download, its bytes, and the VEVENT count inside.
 *
 * What is deliberately NOT claimed here: the stored journey reference is seeded, because the shell's
 * own gate is that reference (`frontend/src/app/V8Experience.tsx`); walking the onboarding wizard to
 * get one is the real-device cell, ruled NOT_REACHED_BY_LANE and still NOT-RUN in headless.
 */

import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const COLLECTION_PREREQ: string[] = [];

const TRIP = "22222222-2222-4222-8222-222222222222";
const TAG = process.env.CITYNEXUS_ORD0525_TAG ?? "r3";
const BASE_TIP = process.env.CITYNEXUS_ORD0525_BASE_TIP ?? "";
if (!/^[0-9a-f]{40}$/.test(BASE_TIP)) COLLECTION_PREREQ.push("CITYNEXUS_ORD0525_BASE_TIP must be the 40-hex tip this was measured on");
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0525", "readouts");

function writeEvidence(name: string, payload: unknown) {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  fs.writeFileSync(
    path.join(EVIDENCE, `${name}_${TAG}.json`),
    JSON.stringify({ generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() },
                     ...(payload as object) }, null, 2) + "\n",
    "utf-8",
  );
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test.describe("ORD-0525 the calendar entry, counted the way the commander counts it", () => {
  test("R3 手机竖屏走一遍：日历字命中数、入口中心命中、点下去真拿到文件", async ({ page }) => {
    // The traveller's device already has a trip; that is the shell's gate, seeded rather than earned
    // through the wizard -- the wizard cell is the real-device one and stays named.
    await page.addInitScript(([id]) => {
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", "zh-CN");
    }, [TRIP]);
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto("/");
    await expect(page.getByRole("main")).toBeVisible();
    const trips = page.getByTestId("nav-trips");
    await expect(trips).toHaveCount(1);
    await trips.scrollIntoViewIfNeeded();
    await trips.click();
    await expect(page.getByTestId("trip-itinerary")).toBeVisible({ timeout: 40_000 });

    const rendered = await page.evaluate(() => document.body.innerText);
    const zhHits = rendered.split("日历").length - 1;
    const enHits = (rendered.match(/calendar/gi) ?? []).length;

    const entry = page.getByTestId("calendar-export");
    await expect(entry).toHaveCount(1);
    await entry.scrollIntoViewIfNeeded();
    const box = await entry.boundingBox();
    expect(box, "the entry has no box to aim at").not.toBeNull();
    const centre = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null;
    const atCentre = centre
      ? await page.evaluate(([x, y]) => {
          const found = document.elementFromPoint(x, y);
          const self = document.querySelector('[data-testid="calendar-export"]');
          return {
            tag: found?.tagName ?? null,
            testId: found instanceof HTMLElement ? found.dataset.testid ?? null : null,
            isEntryOrChild: !!(found && self && (found === self || self.contains(found))),
            disabled: found instanceof HTMLButtonElement ? found.disabled : null,
          };
        }, [centre.x, centre.y])
      : null;

    // Arm the listener before the press and await it, so no reading depends on a sleep.
    const announced = page.waitForEvent("download", { timeout: 30_000 });
    await entry.click();
    const arrived = await announced;
    const parked = await arrived.path();
    const body = parked ? fs.readFileSync(parked) : Buffer.alloc(0);
    const download = {
      seen: true,
      bytes: body.length,
      vevents: (body.toString("utf-8").match(/BEGIN:VEVENT/g) ?? []).length,
      suggested: arrived.suggestedFilename(),
    };
    const notice = page.getByTestId("calendar-export-notice");
    await expect(notice).toBeVisible({ timeout: 30_000 });
    const noticeText = await notice.innerText();

    writeEvidence("phone_calendar_probe", {
      walked: ["今天", "nav-trips", "trip-itinerary", "calendar-export"],
      viewport: "390x844",
      base_tip_at_record_time: BASE_TIP,
      calendar_word_hits: { zh: zhHits, en: enHits, total: zhHits + enHits },
      rendered_characters: rendered.length,
      entry_box: box,
      centre_hit_test: atCentre,
      download_seen: download.seen,
      download_bytes: download.bytes,
      download_vevents: download.vevents,
      download_suggested_name: download.suggested,
      notice_on_screen: noticeText,
      wizard_cell: "NOT_REACHED_BY_LANE (real device, ruled at 15:30)",
    });

    expect(zhHits + enHits, "the calendar entry still does not reach the glass").toBeGreaterThan(0);
    expect(atCentre?.isEntryOrChild, "the centre of the entry is not the entry").toBe(true);
    expect(download.seen, "pressing the entry produced no download").toBe(true);
    expect(download.vevents, "the downloaded file holds no VEVENT").toBeGreaterThan(0);
  });
});
