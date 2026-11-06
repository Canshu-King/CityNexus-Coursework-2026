/**
 * ORD-0515 — the poster's size bound, its pagination, and the two arms that prove neither is decoration.
 *
 *   P1  the short trip: one page, no pagination sentence, and the exported bytes/pixels measured
 *       (this is the reading the bound is expressed against).
 *   P2  the seven-day trip the product's own write doors built: the sheet says it paged, offers one
 *       button per page, every page exports, and no page's own IHDR exceeds the named bound.
 *   P3  the day row read in both shipped languages, counted for key echo.
 *   X1  the same seven-day journey against the bundle that predates the bound: one over-tall picture
 *       and no sentence about it -- which is the silent case P2 exists to catch.
 *   X2  page count and block count taken in the same breath, and re-taken after a day disappears.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const SHORT = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0515_SHORT_PORT)}`;
const LONG = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0515_LONG_PORT)}`;
const UNBOUNDED = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0515_UNBOUNDED_PORT)}`;
const AUDIT = (process.env.CITYNEXUS_ORD0515_AUDIT ?? "").trim();
const PYTHON = (process.env.CITYNEXUS_ORD0515_PY ?? "").trim();
const EVIDENCE = (process.env.CITYNEXUS_ORD0515_EVIDENCE ?? "").trim();
/** Read back out of the product source, never retyped here: a bound the test restates by hand can
 *  drift away from the bound the product enforces, and both would still be green. */
const SOURCE_BOUND = readFileSync("src/features/poster/posterCanvas.ts", "utf8");
const LIMIT_RASTER_PX = Number(/POSTER_MAX_RASTER_HEIGHT_PX = (\d+)/.exec(SOURCE_BOUND)?.[1] ?? "0");
for (const [name, value] of [
  ["CITYNEXUS_ORD0515_AUDIT", AUDIT],
  ["CITYNEXUS_ORD0515_PY", PYTHON],
  ["CITYNEXUS_ORD0515_EVIDENCE", EVIDENCE],
] as const) {
  if (!value) COLLECTION_PREREQ.push(`[ord0515] ${name} must be named by the runner`);
}
if (!(LIMIT_RASTER_PX > 0)) throw new Error("[ord0515] the named bound could not be read from source");
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

function write(name: string, body: unknown) {
  const path = `${EVIDENCE}/${name}.json`;
  writeFileSync(path, JSON.stringify(body, null, 1), "utf8");
  console.log(`O515 ${name} ${JSON.stringify(body).slice(0, 700)}`);
}

function audit(path: string): any {
  const out = execFileSync(PYTHON, [AUDIT, path], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(out.trim().split("\n")[0]);
}

const statedNumbers = (line: string) => {
  const m = /([\d,]+)\s*(?:字节|bytes)\s*[,，]?\s*([\d,]+)\s*[×x]\s*([\d,]+)/.exec(line.replace(/,/g, ""));
  return m ? { bytes: Number(m[1]), width: Number(m[2]), height: Number(m[3]) } : null;
};

async function openSheet(page: Page, origin: string, locale: "zh-CN" | "en") {
  await page.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    [locale, JOURNEY_ID] as const,
  );
  await page.goto(`${origin}/v8/trips`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(900);
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });
}

async function readSheet(page: Page) {
  /* One atomic read. Counting and then indexing by testid raced with the sheet's own first
     pagination: the count said seven while the panel had already collapsed to page one, and the
     arm waited four hundred seconds for an eighth card that was never going to exist. */
  const listed = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-testid^="poster-day-"]')).map((node) => ({
      label: node.querySelector("b")?.textContent?.trim() ?? "",
      count: node.querySelector("small")?.textContent?.trim() ?? "",
      stops: Array.from(node.querySelectorAll('[data-testid="poster-stop"]')).map((s) =>
        (s.textContent ?? "").trim(),
      ),
    })),
  );
  const dayRows = listed.length;
  const labels = listed.map((row) => row.label);
  const stops = listed.reduce((total, row) => total + row.stops.length, 0);
  return {
    day_rows: dayRows,
    day_labels: labels,
    stop_rows: stops,
    subtitle: (await page.getByTestId("poster-subtitle").innerText()).trim(),
    paged_note: await page.getByTestId("poster-paged-note").innerText({ timeout: 1_000 }).catch(() => null),
    page_buttons: await page.getByTestId(/^poster-page-\d+$/).count(),
  };
}

async function exportPage(page: Page, tag: string) {
  const downloadPromise = page.waitForEvent("download", { timeout: 90_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE}/${tag}_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  const screen_line = (await page.getByTestId("poster-result").innerText()).trim();
  const page_line = await page.getByTestId("poster-page-result").innerText({ timeout: 1_000 }).catch(() => null);
  const buffer = readFileSync(saved);
  return {
    file: saved.split("/").pop(),
    suggested_filename: download.suggestedFilename(),
    screen_line,
    page_line,
    stated: statedNumbers(screen_line),
    ihdr: { bytes: buffer.length, width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) },
    pixels: audit(saved),
  };
}

/**
 * Walks the page strip and collects what the sheet lists on each page.
 *
 * The panel shows one page at a time -- that is the point of paging -- so a day count has to be
 * summed across pages. An earlier version of these arms read the panel and expected every day of the
 * trip to be on screen at once, which is the behaviour this order removed.
 */
async function readAllPages(page: Page) {
  const first = await readSheet(page);
  const buttons = first.page_buttons;
  const perPage: any[] = [];
  for (let index = 1; index <= Math.max(buttons, 1); index += 1) {
    if (index > 1) {
      await page.getByTestId(`poster-page-${index}`).click();
      await page.waitForTimeout(250);
    }
    perPage.push(await readSheet(page));
  }
  return {
    buttons,
    paged_note: first.paged_note,
    subtitle: first.subtitle,
    per_page: perPage,
    blocks_total: perPage.reduce((sum, sheet) => sum + sheet.day_rows, 0),
    stops_total: perPage.reduce((sum, sheet) => sum + sheet.stop_rows, 0),
    labels: perPage.flatMap((sheet) => sheet.day_labels),
  };
}

/** Hairline rows to day blocks. `seamsDrawn` is the difference between this build and the boundless one. */
function blocksFromHairlines(groups: number, seamsDrawn: boolean) {
  const blocks = seamsDrawn ? (groups + 1) / 3 : groups / 2;
  expect(Number.isInteger(blocks), `hairline rows ${groups} do not describe whole blocks`).toBe(true);
  return blocks;
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("ORD-0515 P1 the short trip still exports one picture, and its size is measured", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  await openSheet(page, SHORT, "zh-CN");
  const sheet = await readSheet(page);
  const exported = await exportPage(page, "P1");

  expect(sheet.day_rows, "the sealed journey is one day").toBe(1);
  expect(sheet.page_buttons, "one day needs no page strip").toBe(0);
  expect(sheet.paged_note, "and no sentence about paging").toBeNull();
  expect(exported.ihdr.height, "the single picture sits under the bound").toBeLessThanOrEqual(LIMIT_RASTER_PX);
  expect(exported.stated!.height, "the screen states the PNG's own height").toBe(exported.ihdr.height);
  expect(exported.stated!.bytes, "and its own byte count").toBe(exported.ihdr.bytes);
  write("P1_short_trip", { limit_raster_px: LIMIT_RASTER_PX, sheet, exported });
  await context.close();
});

test("ORD-0515 P2 the seven-day trip pages, and every page exports inside the bound", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  await openSheet(page, LONG, "zh-CN");
  const walked = await readAllPages(page);

  expect(walked.buttons, "the strip offers one button per page").toBeGreaterThan(1);
  expect(walked.blocks_total, "the seven days the write doors built are all there, across pages").toBe(7);
  expect(new Set(walked.labels).size, "and no day appears twice").toBe(7);
  expect(walked.paged_note, "the sheet says so in words").toContain("分页");

  const pages: any[] = [];
  for (let index = 1; index <= walked.buttons; index += 1) {
    await page.getByTestId(`poster-page-${index}`).click();
    await page.waitForTimeout(250);
    const onScreen = (await readSheet(page)).day_rows;
    const exported = await exportPage(page, `P2_page${index}`);
    expect(exported.ihdr.height, `page ${index} must fit inside the named bound`).toBeLessThanOrEqual(LIMIT_RASTER_PX);
    expect(exported.suggested_filename, `page ${index} names itself in the file name`)
      .toContain(`-p${index}.png`);
    /* n blocks draw n card tops + n card bottoms + (n-1) drawn day seams = 3n-1 hairlines; the
       pre-bound build draws no seam, so it is 2n. Dividing by two was my own arithmetic error and it
       read 8.5 blocks for a six-block page. */
    expect(
      blocksFromHairlines(exported.pixels.hairline_groups, true),
      `page ${index} exports exactly the blocks it lists`,
    ).toBe(onScreen);
    pages.push({ page: index, blocks_on_screen: onScreen, ...exported });
  }
  write("P2_seven_day_pagination", {
    limit_raster_px: LIMIT_RASTER_PX,
    walked,
    pages,
  });
  await context.close();
});

test("ORD-0515 P3 the day row in both shipped languages, counted for key echo", async ({ browser }) => {
  const rows: Record<string, any> = {};
  for (const locale of ["zh-CN", "en"] as const) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
    const page = await context.newPage();
    await openSheet(page, LONG, locale);
    const sheet = await readAllPages(page);
    const joined = [sheet.subtitle, ...(sheet.paged_note ? [sheet.paged_note] : []), ...sheet.labels].join(" | ");
    for (const needle of ["poster.", "mapComp.", "fmt.", "{", "}"]) {
      expect(joined, `a dictionary key reached the ${locale} screen: ${joined}`).not.toContain(needle);
    }
    expect(sheet.blocks_total, "every day of the trip is listed, across the pages").toBe(7);
    rows[locale] = { subtitle: sheet.subtitle, paged_note: sheet.paged_note, day_labels: sheet.labels, blocks_total: sheet.blocks_total };
    await context.close();
  }
  const source = readFileSync("src/i18n/LocaleContext.tsx", "utf8");
  const union = /type TripLocale = ([^;]+);/.exec(source)?.[1]?.trim() ?? "";
  const arms = [...union.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  write("P3_languages", { arms, ...rows, german_arm_present: arms.includes("de") || arms.includes("de-DE") });
  expect(arms).toEqual(["en", "zh-CN"]);
});

test("ORD-0515 X1 the bundle without the bound ships one over-tall picture and says nothing", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  await openSheet(page, UNBOUNDED, "zh-CN");
  const sheet = await readSheet(page);
  const exported = await exportPage(page, "X1");

  expect(sheet.day_rows, "same journey, same seven days").toBe(7);
  expect(sheet.page_buttons, "the pre-bound build offers no pages").toBe(0);
  expect(sheet.paged_note, "and never tells the traveller the picture is over the bound").toBeNull();
  expect(exported.ihdr.height, "this is the silent case: one file taller than the named bound")
    .toBeGreaterThan(LIMIT_RASTER_PX);
  expect(exported.pixels.bottom_padding_rows, "the file still encodes, so nothing looks broken on screen")
    .toBeGreaterThan(8);
  write("X1_unbounded_bundle", { limit_raster_px: LIMIT_RASTER_PX, sheet, exported });
  await context.close();
});

test("ORD-0515 X2 page count and block count are taken in the same breath", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  await openSheet(page, LONG, "zh-CN");
  const before = await readAllPages(page);
  const buttonsBefore = before.buttons;
  const blocksBefore = before.blocks_total;

  /* Take the last day away in flight -- no write, no SQL -- and require both numbers to move
     together. A page count cached from an earlier render would keep the old strip. */
  await page.route(`**/api/v1/journeys/${JOURNEY_ID}/experience`, async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const acts = payload?.active_plan?.activities ?? [];
    const lastDay = acts.map((a: any) => (a.start_at ?? "").slice(0, 10)).sort().pop();
    payload.active_plan.activities = acts.filter((a: any) => (a.start_at ?? "").slice(0, 10) !== lastDay);
    await route.fulfill({ response, body: JSON.stringify(payload) });
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });
  const after = await readAllPages(page);

  expect(after.blocks_total, "one day less across the pages").toBe(blocksBefore - 1);
  expect(after.buttons, "the strip is recomputed, not carried over").toBeLessThanOrEqual(buttonsBefore);
  expect(after.buttons, "and it is never more pages than there are blocks").toBeLessThanOrEqual(after.blocks_total);
  write("X2_pages_follow_blocks", {
    before: { blocks: blocksBefore, buttons: buttonsBefore },
    after: { blocks: after.blocks_total, buttons: after.buttons, subtitle: after.subtitle },
  });
  await context.close();
});
