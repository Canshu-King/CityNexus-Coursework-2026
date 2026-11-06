/**
 * ORD-0501 — the multi-day poster has to hold its layout, and the arm has to be able to tell.
 *
 * Five readings, all on one generation and one bundle:
 *   S1  two days, same run: the block count on screen, the PNG's own IHDR, and a pixel audit of that
 *       same file -- one block per day, a DRAWN seam between blocks, nothing clipped at the bottom.
 *  C1   the counterexample S1 needs: with the stroke stubbed away the picture keeps the whitespace
 *       but loses the drawn seam, and the audit must see fewer hairlines. If it did not, S1 would be
 *       grading luck.
 *   S2  English arm on the same two-day journey: the title, the date rows and the source line are
 *       dictionary words, and no key reaches the screen (`t()` echoes a missing key verbatim, which
 *       is the family this arm counts).
 *   S3  the locale inventory, measured rather than assumed: how many arms the dictionary really has.
 *  C2   the other counterexample: the server's day count is lied to in flight, and the comparison
 *       between the headline number and the projection must go red.
 *
 * The picture is audited by `CITYNEXUS_ORD0501_AUDIT` (a Python instrument in this order's review
 * directory) rather than by a second decoder written here, so there is one answer to "what is in
 * these pixels".
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const UNWRITTEN = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0501_UNWRITTEN_PORT)}`;
const WRITTEN = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0501_WRITTEN_PORT)}`;
const WRITTEN_API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0501_WRITTEN_API)}`;
const AUDIT = (process.env.CITYNEXUS_ORD0501_AUDIT ?? "").trim();
const PYTHON = (process.env.CITYNEXUS_ORD0501_PY ?? "").trim();
const EVIDENCE = (process.env.CITYNEXUS_ORD0501_EVIDENCE ?? "").trim();
for (const [name, value] of [
  ["CITYNEXUS_ORD0501_AUDIT", AUDIT],
  ["CITYNEXUS_ORD0501_PY", PYTHON],
  ["CITYNEXUS_ORD0501_EVIDENCE", EVIDENCE],
] as const) {
  if (!value) COLLECTION_PREREQ.push(`[ord0501] ${name} must be named by the runner`);
}
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

function write(name: string, body: unknown) {
  const path = `${EVIDENCE}/${name}.json`;
  writeFileSync(path, JSON.stringify(body, null, 1), "utf8");
  console.log(`O501 ${name} ${JSON.stringify(body)}`);
}

function pngGeometry(path: string) {
  const buffer = readFileSync(path);
  return { bytes: buffer.length, width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

/** Runs this order's own pixel instrument over a downloaded poster. */
function audit(path: string): any {
  const out = execFileSync(PYTHON, [AUDIT, path], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(out.trim().split("\n")[0]);
}

async function openPoster(page: Page, origin: string, locale: "zh-CN" | "en") {
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

async function downloadPoster(page: Page, name: string) {
  const downloadPromise = page.waitForEvent("download", { timeout: 60_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE}/${name}_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  return { saved, screen_line: (await page.getByTestId("poster-result").innerText()).trim() };
}

const statedNumbers = (line: string) => {
  /* The Chinese line separates with a full-width comma, the English one with an ASCII comma: a
     pattern that accepts only one of them reads the other as "the screen stated nothing", which is
     what this arm's first run did. */
  const m = /([\d,]+)\s*(?:字节|bytes)\s*[,，]?\s*([\d,]+)\s*[×x]\s*([\d,]+)/.exec(line.replace(/,/g, ""));
  if (!m) return null;
  return { bytes: Number(m[1]), width: Number(m[2]), height: Number(m[3]) };
};

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("ORD-0501 S1 two days draw two blocks with a drawn seam, and nothing is clipped", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
  await openPoster(page, WRITTEN, "zh-CN");

  const dayCards = page.getByTestId(/^poster-day-\d+$/);
  const dayRows = await dayCards.count();
  const labels: string[] = [];
  for (let i = 0; i < dayRows; i += 1) {
    labels.push((await dayCards.nth(i).locator("b").first().innerText()).trim());
  }
  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  const { saved, screen_line } = await downloadPoster(page, "S1");
  const png = pngGeometry(saved);
  const stated = statedNumbers(screen_line);
  const pixels = audit(saved);

  // one block per day
  expect(dayRows, "the second day has to be a block on the screen").toBe(2);
  expect(pixels.height, "the picture is the height the screen states").toBe(stated!.height);
  expect(pixels.width, "and the width the screen states").toBe(stated!.width);
  expect(png.bytes, "and the bytes the screen states").toBe(stated!.bytes);

  /* A drawn seam, not just a gap: every block contributes two card edges and every break between
     two blocks contributes one more hairline. Two blocks therefore read 5, not 4. */
  const expectedHairlines = 3 * dayRows - 1;
  expect(pixels.hairline_groups, `blocks=${dayRows} must draw ${expectedHairlines} hairlines (2 per block + 1 drawn seam per break)`)
    .toBe(expectedHairlines);

  // the height grew because a block was added, not because something was cut
  expect(pixels.bottom_padding_rows, "a clipped bottom would fake a compact picture").toBeGreaterThan(8);
  expect(pixels.last_ink_row).toBeLessThan(pixels.height);
  expect(labels[0]).not.toBe(labels[1]);

  write("S1_two_day_layout", {
    day_rows: dayRows,
    day_labels: labels,
    subtitle,
    screen_line,
    stated,
    png,
    pixels,
    expected_hairlines: expectedHairlines,
    pageerrors: errors,
  });
  await context.close();
});

test("ORD-0501 C1 stub the stroke away and the same audit must stop seeing the seam", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  await context.addInitScript(() => {
    /* The mutation has to be narrow. Stubbing every stroke also erases the two card edges, which
       is a different picture and not the one under test -- the first run of this arm read zero
       hairlines for exactly that reason. So: only a path made of straight segments is left
       unstroked, which is what the day divider is and what a rounded card never is. */
    const proto = CanvasRenderingContext2D.prototype as unknown as {
      stroke: () => void;
      quadraticCurveTo: (...a: number[]) => void;
      __curved?: boolean;
    };
    const realStroke = proto.stroke;
    const realCurve = proto.quadraticCurveTo;
    proto.quadraticCurveTo = function (this: CanvasRenderingContext2D, ...a: number[]) {
      (this as unknown as { __curved?: boolean }).__curved = true;
      return realCurve.apply(this, a as [number, number, number, number]);
    };
    proto.stroke = function (this: CanvasRenderingContext2D) {
      const curved = (this as unknown as { __curved?: boolean }).__curved === true;
      (this as unknown as { __curved?: boolean }).__curved = false;
      if (!curved) return;
      return realStroke.call(this);
    };
  });
  const page = await context.newPage();
  await openPoster(page, WRITTEN, "zh-CN");
  const dayRows = await page.getByTestId(/^poster-day-\d+$/).count();
  const { saved, screen_line } = await downloadPoster(page, "C1");
  const pixels = audit(saved);

  expect(dayRows).toBe(2);
  expect(pixels.hairline_groups, "with the seam unstroked only the two card edges per block remain")
    .toBe(2 * dayRows);
  expect(pixels.hairline_groups, "and that is strictly fewer than S1 demanded")
    .toBeLessThan(3 * dayRows - 1);
  /* The picture is still a valid, uncut file: what disappeared is the drawn separation, which is
     the whole point -- the counterexample fails the judgement, not the encoder. */
  const stated = statedNumbers(screen_line);
  expect(pixels.height).toBe(stated!.height);
  expect(pixels.bottom_padding_rows).toBeGreaterThan(8);
  write("C1_no_drawn_seam", { day_rows: dayRows, screen_line, stated, pixels, expected_without_seam: 2 * dayRows });
  await context.close();
});

test("ORD-0501 S2 the English arm reads dictionary words, never a key", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en" });
  const page = await context.newPage();
  await openPoster(page, WRITTEN, "en");

  const title = (await page.getByTestId("poster-title").innerText()).trim();
  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  const progress = (await page.getByTestId("poster-progress").innerText()).trim();
  const attribution = (await page.getByTestId("poster-attribution").innerText()).trim();
  const cards = page.getByTestId(/^poster-day-\d+$/);
  const dayRows = await cards.count();
  const labels: string[] = [];
  for (let i = 0; i < dayRows; i += 1) {
    labels.push((await cards.nth(i).locator("b").first().innerText()).trim());
  }
  const { saved, screen_line } = await downloadPoster(page, "S2");
  const pixels = audit(saved);

  const rendered = [title, subtitle, progress, attribution, ...labels].join(" | ");
  /* `t()` falls back to the key itself, so a missing English word would print `poster.kicker`.
     Counted over everything the picture and the sheet show. */
  for (const prefix of ["poster.", "mapComp.", "fmt.", "{", "}"]) {
    expect(rendered, `a machine word reached the English screen: ${rendered}`).not.toContain(prefix);
  }
  expect(subtitle).toContain("2 days");
  expect(attribution).toContain("OpenStreetMap");
  expect(labels[0]).toMatch(/Oct/);
  expect(labels[1]).toMatch(/Oct/);
  expect(pixels.hairline_groups, "the English picture carries the same drawn seam").toBe(3 * dayRows - 1);
  write("S2_english_arm", { title, subtitle, progress, attribution, day_rows: dayRows, day_labels: labels, screen_line, pixels });
  await context.close();
});

test("ORD-0501 S3 the locale inventory, measured", async () => {
  const source = readFileSync("src/i18n/LocaleContext.tsx", "utf8");
  const union = /type TripLocale = ([^;]+);/.exec(source)?.[1] ?? "";
  const arms = [...union.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  const dictionary = /const dictionaries[^=]*=\s*\{([^}]*)\}/.exec(source)?.[1] ?? "";
  write("S3_locale_inventory", {
    trip_locale_union: union.trim(),
    locale_arms: arms,
    dictionary_arms: dictionary.trim(),
    german_arm_present: arms.includes("de") || arms.includes("de-DE"),
  });
  expect(arms.length, "this is the measured locale set, not an assumed one").toBe(2);
  expect(arms).toContain("en");
  expect(arms).toContain("zh-CN");
});

test("ORD-0501 C2 a lied-to day count must break the comparison, not pass it", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh-CN" });
  const page = await context.newPage();
  /* Lie in flight only: the database and the server keep their own answer, which is read back
     afterwards and compared. No write, no SQL -- this is a transport mutation. */
  await page.route(`**/api/v1/journeys/${JOURNEY_ID}/experience`, async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    if (payload?.journey?.temporal_shape) {
      payload.journey.temporal_shape.day_count = 9;
      payload.journey.temporal_shape.night_count = 8;
    }
    await route.fulfill({ response, body: JSON.stringify(payload) });
  });
  await openPoster(page, WRITTEN, "zh-CN");
  const subtitle = (await page.getByTestId("poster-subtitle").innerText()).trim();
  const dayRows = await page.getByTestId(/^poster-day-\d+$/).count();

  const direct = await fetch(`${WRITTEN_API}/api/v1/journeys/${JOURNEY_ID}/experience`);
  const truth: any = await direct.json();
  const realDayCount = truth?.journey?.temporal_shape?.day_count ?? null;

  expect(subtitle, "the headline follows the projection the screen was handed").toContain("9 天");
  expect(realDayCount, "and that is where the lie shows: the server still answers 2").toBe(2);
  expect(dayRows, "two blocks were drawn while the headline claimed nine").toBe(2);
  write("C2_day_count_lie", {
    subtitle_with_lie: subtitle,
    server_day_count_read_directly: realDayCount,
    day_rows_drawn: dayRows,
    mismatch_detected: subtitle.includes("9 天") && realDayCount === 2,
  });
  await context.close();
});
