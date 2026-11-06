/**
 * ORD-0498 — the poster's second day, read off the screen.
 *
 * The second day was made with the product's own write doors and nothing else:
 *   PATCH /api/v1/journeys/{id}                    -> http 200, `temporal_shape.day_count` 1 -> 2
 *   POST  /api/v1/journeys/{id}/poi-import/add     -> http 201, `day_source` TRAVELLER_CHOSEN
 * No SQL, no `demo/reset`, no seed file. This spec then compares two journeys that differ only by
 * those two calls, served by the same bundle, so the only thing a reading can attribute the height
 * difference to is the second day.
 */
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const UNWRITTEN = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0498_UNWRITTEN_PORT)}`;
const WRITTEN = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0498_WRITTEN_PORT)}`;
/* The API behind each front, named by the runner rather than derived from the front's port: the
   second reading asks the server what it projects, and that question has to go to the same
   database the screen was drawn from. */
const UNWRITTEN_API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0498_UNWRITTEN_API)}`;
const WRITTEN_API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0498_WRITTEN_API)}`;
const EVIDENCE = (process.env.CITYNEXUS_ORD0498_EVIDENCE ?? "").trim();
const EVIDENCE_PREREQ = EVIDENCE ? null : "[ord0498] CITYNEXUS_ORD0498_EVIDENCE must name where evidence is written";
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

function write(name: string, body: unknown) {
  const path = `${EVIDENCE}/${name}.json`;
  writeFileSync(path, JSON.stringify(body, null, 1), "utf8");
  console.log(`O498 ${name} ${JSON.stringify(body)}`);
}

function pngNumbers(path: string) {
  const buffer = readFileSync(path);
  return {
    bytes: buffer.length,
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
    sha1: createHash("sha1").update(buffer).digest("hex"),
  };
}

/** Opens the trips screen, opens the poster sheet and downloads the picture. */
async function posterOn(browser: Browser, origin: string, apiOrigin: string, caseName: string) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page: Page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error).slice(0, 200)));
  await page.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    ["zh-CN", JOURNEY_ID] as const,
  );
  await page.goto(`${origin}/v8/trips`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(900);

  const bar = page.getByTestId("poster-open");
  await bar.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const box = await bar.boundingBox();
  const centre = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null;
  const hit = centre
    ? await page.evaluate(({ x, y }) => {
        const found = document.elementFromPoint(x, y);
        return found ? { tag: found.tagName.toLowerCase(), owns_it: found.closest('[data-testid="poster-open"]') !== null } : null;
      }, centre)
    : null;

  await bar.click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });

  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  const dayCards = page.getByTestId(/^poster-day-\d+$/);
  const dayRows = await dayCards.count();
  const days: Array<{ label: string; count: string; stops: string[] }> = [];
  for (let index = 0; index < dayRows; index += 1) {
    const card = dayCards.nth(index);
    days.push({
      label: (await card.locator("b").first().innerText()).trim(),
      count: (await card.locator("small").first().innerText()).trim(),
      stops: (await card.getByTestId("poster-stop").allInnerTexts()).map((t) => t.trim()),
    });
  }
  const attribution = (await page.getByTestId("poster-attribution").innerText()).trim();

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE}/${caseName}_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  const screenLine = (await page.getByTestId("poster-result").innerText()).trim();
  const file = pngNumbers(saved);

  await context.close();
  return {
    origin,
    api_origin: apiOrigin,
    entry_box: box,
    entry_centre: centre,
    hit,
    subtitle,
    day_rows: dayRows,
    days,
    attribution,
    screen_line: screenLine,
    file,
    pageerrors: errors,
  };
}

const stated = (line: string) => {
  const m = /已存为 PNG：([\d,]+) 字节，([\d,]+) × ([\d,]+) 像素/.exec(line);
  if (!m) return null;
  const n = (v: string) => Number(v.replace(/,/g, ""));
  return { bytes: n(m[1]), width: n(m[2]), height: n(m[3]) };
};

test.beforeEach(() => {
  const missing = [EVIDENCE_PREREQ]
    .filter((v): v is string => v !== null);
  test.skip(missing.length > 0, missing.join(" | "));
});

test("ORD-0498 M1 the same bundle shows one day before the writes and two days after", async ({ browser }) => {
  const before = await posterOn(browser, UNWRITTEN, UNWRITTEN_API, "M1_unwritten");
  const after = await posterOn(browser, WRITTEN, WRITTEN_API, "M1_written");

  expect(before.pageerrors, "the unwritten journey must render without a page error").toEqual([]);
  expect(after.pageerrors, "the written journey must render without a page error").toEqual([]);

  expect(before.day_rows, "the sealed one-day journey still draws a single day card").toBe(1);
  expect(after.day_rows, "the second day made by the write doors draws its own card").toBe(2);

  /* The picture has to say so too: a taller raster, and the numbers the traveller reads are the
     PNG's own IHDR, on both sides of the comparison. */
  const beforeStated = stated(before.screen_line);
  const afterStated = stated(after.screen_line);
  expect(beforeStated, `unwritten screen line: ${before.screen_line}`).not.toBeNull();
  expect(afterStated, `written screen line: ${after.screen_line}`).not.toBeNull();
  expect(afterStated!.height, "the second day must make the picture taller").toBeGreaterThan(beforeStated!.height);
  expect(after.file.height, "the screen's height is the PNG's own IHDR height, written case")
    .toBe(afterStated!.height);
  expect(after.file.width, "the screen's width is the PNG's own IHDR width, written case").toBe(afterStated!.width);
  expect(after.file.bytes, "the screen's byte count is the file's own size, written case").toBe(afterStated!.bytes);
  expect(before.file.height, "the same holds for the unwritten case").toBe(beforeStated!.height);
  expect(after.file.width, "the width is the measured sheet, so it does not change with the day count")
    .toBe(before.file.width);

  write("M1_one_vs_two_days", { before, after, delta_height: after.file.height - before.file.height });
});

test("ORD-0498 M2 the second card names its own day and its own stop, and the count is the server's", async ({ browser }) => {
  const after = await posterOn(browser, WRITTEN, WRITTEN_API, "M2_written");

  expect(after.day_rows).toBe(2);
  const [first, second] = after.days;
  expect(first.stops.length, "the sealed day keeps its six stops").toBe(6);
  expect(second.stops.length, "the second day holds the stop the write route added").toBe(1);
  /* The place the write route named, read off the picture's own source rather than assumed:
     `poi-import/add` answered with this title for day 2026-10-11. */
  expect(second.stops[0]).toContain("Nexus City Park");
  expect(second.stops[0]).toMatch(/^\d{2}:\d{2}\s*–\s*\d{2}:\d{2}/);
  expect(second.label, "the day label is the trip zone's own calendar day, formatted by the panel")
    .not.toBe(first.label);
  expect(second.label).toMatch(/10|11/);

  /* The headline number is the server's projection, not a recount and never a filled-in 1. */
  expect(after.subtitle).toContain("2 天");
  expect(after.subtitle).toContain("演示城市");
  expect(after.attribution).toContain("OpenStreetMap");

  const response = await fetch(`${WRITTEN_API}/api/v1/journeys/${JOURNEY_ID}/experience`);
  const payload: any = await response.json();
  const serverDayCount = payload?.journey?.temporal_shape?.day_count ?? null;
  const serverDistinctDays = [
    ...new Set(((payload?.active_plan?.activities ?? []).map((a: any) => (a.start_at ?? "").slice(0, 10)) as string[])),
  ].sort();
  write("M2_second_card", { ...after, server_day_count_read_again: serverDayCount, server_distinct_activity_days: serverDistinctDays });
  expect(serverDayCount, "the count on the screen is the count the server projects").toBe(2);
});
