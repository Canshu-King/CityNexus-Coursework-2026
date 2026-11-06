/**
 * ORD-0472 — the trip poster, walked on a real screen.
 *
 * Four arms, each with its own receipt in `CITYNEXUS_EVIDENCE_DIR`:
 *   A1 reachability (390x844, zh)  -- the entry is scrolled to, its centre is hit-tested, and the
 *                                    sheet's own rendered words are read off the screen.
 *   A2 export (1280x900, en)       -- pressing download produces a file; the bytes and the pixel
 *                                    dimensions the traveller reads on screen are compared with the
 *                                    PNG's own IHDR, so "saved" cannot mean an empty or wrong-sized
 *                                    picture.
 *   A3 persistence                 -- after a reload the entry is still there and still makes a
 *                                    picture; the two files are compared as readings, not asserted.
 *   A4 control (反件)              -- with the 2D context refused, the sheet must NAME the step that
 *                                    failed and hand over no file. Without this arm the success path
 *                                    proves nothing about the failure path.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const DEMO_TITLE = "One Day in Nexus City";
const STOP_COUNT = 6;

const EVIDENCE_DIR = (process.env.CITYNEXUS_ORD0472_EVIDENCE ?? "").trim();
/* ORD-0576: this refusal is unchanged in force -- no reading is written without a named directory -- but it
 * used to happen at module scope, where one unset variable aborted Playwright's collection for every spec in
 * the directory. It is now a named skip inside this file's tests, so the reason reaches the report instead of
 * the collector. */
const EVIDENCE_PREREQ = EVIDENCE_DIR
  ? null
  : "missing environment variable: CITYNEXUS_ORD0472_EVIDENCE must name where evidence is written";

function write(name: string, body: unknown): string {
  const path = `${EVIDENCE_DIR}/${name}.json`;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(body, null, 1), "utf8");
  console.log(`O472 ${name} ${JSON.stringify(body)}`);
  return path;
}

/** Opens the app with the journey the product itself holds, in the requested language. */
async function landOnTrips(page: Page, locale: "zh-CN" | "en"): Promise<void> {
  await page.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    [locale, JOURNEY_ID],
  );
  await page.goto("/v8/trips", { waitUntil: "load", timeout: 90_000 });
  const bar = page.getByTestId("trip-poster-bar");
  try {
    await expect(bar.first()).toBeVisible({ timeout: 20_000 });
  } catch {
    /* The stored journey did not take: fall back to the product's own demo button, which is the
       route every other lane's gate uses. Recorded, because which door opened matters. */
    await page.goto("/v8", { waitUntil: "load" });
    await page.waitForTimeout(800);
    await page.getByTestId("trial-tianjin-demo").click();
    await page
      .waitForFunction(() => !!localStorage.getItem("citynexus.v8.journey_id"), null, { timeout: 40_000 })
      .catch(() => undefined);
    await page.waitForTimeout(1_500);
    await page.goto("/v8/trips", { waitUntil: "load" });
    await expect(bar.first()).toBeVisible({ timeout: 30_000 });
    write("fallback_used", { locale, door: "trial-tianjin-demo" });
  }
  await page.waitForTimeout(800);
}

function pngNumbers(path: string): { bytes: number; width: number; height: number; signature: string } {
  const buffer = readFileSync(path);
  return {
    bytes: buffer.length,
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
    signature: buffer.subarray(1, 4).toString("latin1"),
  };
}

async function hitTest(page: Page, locator: Locator, testId: string) {
  await locator.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  const box = await locator.boundingBox();
  const scrollY = await page.evaluate(() => window.scrollY);
  const centre = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null;
  const owner = centre
    ? await page.evaluate(
        ({ x, y, id }) => {
          const hit = document.elementFromPoint(x, y);
          if (!hit) return { found: false as const };
          return {
            found: true as const,
            tag: hit.tagName.toLowerCase(),
            text: (hit.textContent || "").trim().slice(0, 60),
            isTheButtonOrInsideIt: hit.closest(`[data-testid="${id}"]`) !== null,
          };
        },
        { ...centre, id: testId },
      )
    : { found: false as const };
  return { box, scrollY, centre, owner };
}

test.beforeEach(() => {
  test.skip(EVIDENCE_PREREQ !== null, EVIDENCE_PREREQ ?? undefined);
});

test("ORD-0472 A1 the poster entry is reachable and its words are on the screen", async ({ browser }) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    locale: "zh-CN",
  });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error).slice(0, 200)));
  await landOnTrips(page, "zh-CN");

  const open = page.getByTestId("poster-open");
  const reach = await hitTest(page, open, "poster-open");
  expect(reach.box, "the entry has to have a box before it can be hit").not.toBeNull();
  expect(reach.owner.isTheButtonOrInsideIt, "the centre of the entry must land on the entry").toBe(true);
  /* Mobile/touch contract: 44px floor (design-tokens.css:309 and :859). */
  expect(reach.box!.height, "a touch control under 44px is not the contract this project holds")
    .toBeGreaterThanOrEqual(44);

  await open.click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });

  const title = (await page.getByTestId("poster-title").innerText()).trim();
  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  const progress = (await page.getByTestId("poster-progress").innerText()).trim();
  const attribution = (await page.getByTestId("poster-attribution").innerText()).trim();
  const dayRows = await page.getByTestId(/^poster-day-\d+$/).count();
  const stopRows = await page.getByTestId("poster-stop").count();
  const stopTexts = await page.getByTestId("poster-stop").allInnerTexts();

  expect(title, "the picture's headline is the journey's own title").toBe(DEMO_TITLE);
  expect(subtitle).toContain("演示城市");
  expect(subtitle).toContain("1 天");
  expect(dayRows, "every day group the itinerary draws gets a block").toBe(1);
  expect(stopRows).toBe(STOP_COUNT);
  expect(attribution).toContain("OpenStreetMap");
  /* No machine words: the copy comes from the dictionary, and a missing key would print its own name. */
  for (const rendered of [title, subtitle, progress, attribution]) {
    expect(rendered, "a dictionary key must never reach the traveller's screen").not.toContain("poster.");
  }
  expect(stopTexts[0]).toMatch(/^\d{2}:\d{2}\s*–\s*\d{2}:\d{2}/);

  write("A1_reachability", {
    viewport: "390x844",
    locale: "zh-CN",
    entry_box: reach.box,
    entry_centre: reach.centre,
    hit: reach.owner,
    scroll_y: reach.scrollY,
    title,
    subtitle,
    progress,
    attribution,
    day_rows: dayRows,
    stop_rows: stopRows,
    first_stop: stopTexts[0],
    pageerrors: errors,
  });
  await context.close();
});

test("ORD-0472 A2 download produces a PNG whose own bytes match what the screen says", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "en" });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error).slice(0, 200)));
  await landOnTrips(page, "en");
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const suggested = download.suggestedFilename();
  const saved = `${EVIDENCE_DIR}/A2_${suggested}`;
  await download.saveAs(saved);

  const onScreen = (await page.getByTestId("poster-result").innerText()).trim();
  /* The counted noun has to follow the count. The first drawn picture said "Demo city · 1 days",
     because `{plural:day}` reads the parameter named `n` and this call names its count `days`
     (`lib/plural.ts:40`); nothing in these arms would have caught it, so it is asserted now. */
  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  expect(subtitle, "one day must read as one day").toBe("Demo city · 1 day");
  const file = pngNumbers(saved);
  /* Matched against the rendered line as the traveller sees it: the thousands separators are turned
     into numbers afterwards, never stripped from the line first (that made the line and the pattern
     disagree, and read as a product red when the screen was already stating both numbers). */
  const match = /([\d,]+)\s+bytes,\s*([\d,]+)\s*×\s*([\d,]+)\s*px/.exec(onScreen);
  expect(match, `the screen must state bytes and pixel size, saw: ${onScreen}`).not.toBeNull();
  const number = (digits: string) => Number(digits.replace(/,/g, ""));
  const stated = { bytes: number(match![1]), width: number(match![2]), height: number(match![3]) };
  expect(stated.bytes, "the bytes the traveller reads are the bytes of the file").toBe(file.bytes);
  expect(stated.width, "the width the traveller reads is the PNG's own IHDR width").toBe(file.width);
  expect(stated.height, "the height the traveller reads is the PNG's own IHDR height").toBe(file.height);
  expect(file.signature).toBe("PNG");
  expect(file.bytes, "a poster of nothing is not a delivered file").toBeGreaterThan(10_000);
  expect(onScreen.includes("poster.fail"), "a failure word must not be on a success screen").toBe(false);

  const sheetBox = await page.getByTestId("trip-poster").boundingBox();
  let elementShot = "NOT_CAPTURED";
  try {
    await page.getByTestId("trip-poster").screenshot({
      path: `${EVIDENCE_DIR}/A2_sheet.png`,
      timeout: 20_000,
    });
    elementShot = `review/ord0472/A2_sheet.png ${readFileSync(`${EVIDENCE_DIR}/A2_sheet.png`).length} B`;
  } catch (error) {
    elementShot = `NOT_CAPTURED_BY_INSTRUMENT ${String(error).slice(0, 90)}`;
  }

  write("A2_export", {
    viewport: "1280x900",
    locale: "en",
    suggested_filename: suggested,
    subtitle_on_screen: subtitle,
    screen_line: onScreen,
    stated,
    file: { ...file, sha1: createHash("sha1").update(readFileSync(saved)).digest("hex") },
    sheet_box: sheetBox,
    element_shot: elementShot,
    pageerrors: errors,
  });
  await context.close();
});

test("ORD-0472 A3 after a reload the entry is still there and still makes a picture", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  await landOnTrips(page, "zh-CN");
  await page.getByTestId("poster-open").click();
  const firstDownload = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const first = await firstDownload;
  const firstPath = `${EVIDENCE_DIR}/A3_first.png`;
  await first.saveAs(firstPath);

  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("poster-title")).toBeVisible({ timeout: 20_000 });
  const secondDownload = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const second = await secondDownload;
  const secondPath = `${EVIDENCE_DIR}/A3_second.png`;
  await second.saveAs(secondPath);

  const a = pngNumbers(firstPath);
  const b = pngNumbers(secondPath);
  const line = (await page.getByTestId("poster-result").innerText()).trim();
  write("A3_reload", {
    before: a,
    after: b,
    same_dimensions: a.width === b.width && a.height === b.height,
    same_bytes: a.bytes === b.bytes,
    sha1_before: createHash("sha1").update(readFileSync(firstPath)).digest("hex"),
    sha1_after: createHash("sha1").update(readFileSync(secondPath)).digest("hex"),
    screen_line_after_reload: line,
  });
  expect(a.width, "a reload that loses the picture's size is not the same feature").toBe(b.width);
  expect(b.bytes).toBeGreaterThan(10_000);
  expect(line).toContain("已存为 PNG");
  await context.close();
});

test("ORD-0472 A5 the poster follows the dark tokens and still clears contrast", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  await context.addInitScript(() => localStorage.setItem("citynexus.theme", "dark"));
  const page = await context.newPage();
  await landOnTrips(page, "zh-CN");
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });

  /* The picture is painted from `getComputedStyle(document.documentElement)`, so the reading that
     matters here is what those tokens resolve to on this page. Measured off the rendered elements
     rather than the token text: a token is `#1d1d1f` while a computed colour is `rgb(...)`, and
     comparing the two forms with one parser is how this arm first reported a ratio of exactly 1 --
     both sides decoded to black because neither parsed, not because the theme was flat. */
  const colours = await page.evaluate(() => {
    const css = getComputedStyle(document.documentElement);
    const title = document.querySelector('[data-testid="poster-title"]')!;
    const card = document.querySelector(".cn-poster-day")!;
    const asRgb = (value: string) => {
      const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim());
      if (hex) {
        const digits =
          hex[1].length === 3 ? hex[1].split("").map((c) => c + c).join("") : hex[1];
        const packed = Number.parseInt(digits, 16);
        return `rgb(${(packed >> 16) & 255}, ${(packed >> 8) & 255}, ${packed & 255})`;
      }
      return value.trim();
    };
    return {
      theme: document.documentElement.getAttribute("data-theme"),
      token_ink: css.getPropertyValue("--cn-ink").trim(),
      token_surface: css.getPropertyValue("--cn-surface").trim(),
      token_canvas: css.getPropertyValue("--cn-canvas").trim(),
      rendered_title: getComputedStyle(title).color,
      rendered_card_background: getComputedStyle(card).backgroundColor,
      /* What the canvas will actually be handed for the two colours it fills with. */
      poster_ink: asRgb(css.getPropertyValue("--cn-ink")),
      poster_surface: asRgb(css.getPropertyValue("--cn-surface")),
    };
  });
  const ratio = (a: string, b: string) => {
    const lum = (value: string) => {
      const m = /rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(value);
      if (!m) return Number.NaN;
      const [r, g, bl] = [Number(m[1]), Number(m[2]), Number(m[3])];
      const channel = (c: number) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(bl);
    };
    const x = lum(a);
    const y = lum(b);
    expect(Number.isNaN(x) || Number.isNaN(y), `both sides must produce a reading: ${a} vs ${b}`).toBe(false);
    return Number(((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)).toFixed(2));
  };
  const inkOnSurface = ratio(colours.poster_ink, colours.poster_surface);
  const renderedInkOnCard = ratio(colours.rendered_title, colours.rendered_card_background);
  expect(colours.theme, "the theme choice has to have landed before this reading means anything")
    .toBe("dark");
  expect(inkOnSurface, "the poster's ink against its own card surface, dark theme").toBeGreaterThanOrEqual(4.5);
  expect(renderedInkOnCard, "the same pair as the screen renders it").toBeGreaterThanOrEqual(4.5);

  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE_DIR}/A5_dark_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  const file = pngNumbers(saved);
  const line = (await page.getByTestId("poster-result").innerText()).trim();
  expect(file.bytes, "a dark-theme poster is still a real file").toBeGreaterThan(10_000);
  expect(file.signature).toBe("PNG");
  expect(line).toContain("已存为 PNG");
  write("A5_dark_tokens", {
    ...colours,
    poster_ink_on_surface_ratio: inkOnSurface,
    rendered_ink_on_card_ratio: renderedInkOnCard,
    file_bytes: file.bytes,
    width: file.width,
    height: file.height,
    screen_line: line,
  });
  await context.close();
});

test("ORD-0472 A4 when the canvas is refused the sheet names the step and hands over no file", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  await context.addInitScript(() => {
    /* The one mutation this arm needs: no 2D context at all. Everything else stays the product's. */
    (HTMLCanvasElement.prototype as unknown as { getContext: () => null }).getContext = () => null;
  });
  const page = await context.newPage();
  await landOnTrips(page, "zh-CN");
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });

  let downloaded = false;
  page.on("download", () => {
    downloaded = true;
  });
  await page.getByTestId("poster-download").click();
  await expect(page.getByTestId("poster-result")).toBeVisible({ timeout: 20_000 });
  const line = (await page.getByTestId("poster-result").innerText()).trim();
  await page.waitForTimeout(1_500);

  /* Exact equality, not "contains": after Q64 the line must be the dictionary sentence with nothing
     appended, so an arm that only looks for a substring would still pass on ` … · 0px`. */
  expect(
    line,
    "the failure line must be the dictionary sentence, nothing appended",
  ).toBe("长图没有生成：这台设备的画布没有给出可用的绘制上下文。");
  expect(line.includes("poster.fail"), `a raw key reached the screen: ${line}`).toBe(false);
  expect(line.includes("已存为 PNG"), "a refused canvas is not a saved file").toBe(false);
  expect(downloaded, "no file may be handed over when nothing could be drawn").toBe(false);
  write("A4_control_no_context", { screen_line: line, downloaded });
  await context.close();
});

test("ORD-0472 A6 the failure line carries dictionary words only", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  await context.addInitScript(() => {
    /* Encoding is refused rather than the context, so the drawing runs to the last step and the
       sheet has to say something. This is the arm that looks for a non-dictionary tail on screen. */
    HTMLCanvasElement.prototype.toBlob = function (cb: any) {
      cb(null);
    };
  });
  const page = await context.newPage();
  await landOnTrips(page, "zh-CN");
  await page.getByTestId("poster-open").click();
  await page.getByTestId("poster-download").click();
  await expect(page.getByTestId("poster-result")).toBeVisible({ timeout: 20_000 });
  const line = (await page.getByTestId("poster-result").innerText()).trim();
  const dictionarySentence = await page.evaluate(() => "长图没有生成：画布没有交出 PNG 字节。");
  write("A6_failure_line", { screen_line: line, dictionary_sentence_only: line === dictionarySentence });
  expect(line, "the failure line must be the dictionary sentence, nothing appended").toBe(dictionarySentence);
  await context.close();
});
