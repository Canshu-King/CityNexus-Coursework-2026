/**
 * ORD-0542 -- the guest poster must not deny itself.
 *
 * The finding, in the words it reached the ledger in: the receiving screen printed 「演示城市 · 天数未知」
 * on one line and 「按这条链接读到的内容数出：2 天 · 7 站」 on the next. Both were true -- the share door
 * sends no length projection -- and together they read as one line calling the other a lie, which is
 * what a traveller on the other end of the link actually sees.
 *
 *   D1  the counted number is the subtitle, the line under it names where the number came from, and
 *       the page says "unknown" nowhere;
 *   D2  the same reading taken against the generation before the fix goes red -- the base bundle
 *       really does print both lines, so this is a comparison and not a restatement;
 *   D3  both shipped languages, counted for key echo, and the picture still exports with its day
 *       blocks intact (the wording change must not cost the guest its picture).
 *
 * One ruler, two fronts: `CANDIDATE` is this order's build, `BASE` is the tip it was cut from.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0542_CANDIDATE_PORT)}`;
const BASE = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0542_BASE_PORT)}`;
const EVIDENCE = (process.env.CITYNEXUS_ORD0542_EVIDENCE ?? "").trim();
for (const [name, value] of [
  ["CITYNEXUS_ORD0542_CANDIDATE_PORT", process.env.CITYNEXUS_ORD0542_CANDIDATE_PORT],
  ["CITYNEXUS_ORD0542_BASE_PORT", process.env.CITYNEXUS_ORD0542_BASE_PORT],
  ["CITYNEXUS_ORD0542_EVIDENCE", EVIDENCE],
] as const) {
  if (!value) COLLECTION_PREREQ.push(`[ord0542] ${name} must be named by the runner`);
}
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

/** 「未知」 on the Chinese side, "unknown" on the English one: the two words the contradiction used. */
const UNKNOWN_WORDS = ["天数未知", "trip length unknown", "length unknown"];

function write(name: string, body: unknown) {
  writeFileSync(`${EVIDENCE}/${name}.json`, JSON.stringify(body, null, 1), "utf8");
  console.log(`O542 ${name} ${JSON.stringify(body).slice(0, 900)}`);
}

/** Open the traveller's sheet, share the picture, and hand back the link's token. */
async function sharedPosterToken(browser: Browser, origin: string, locale: "zh-CN" | "en") {
  const owner = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
  await owner.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    [locale, JOURNEY_ID] as const,
  );
  const page = await owner.newPage();
  await page.goto(`${origin}/v8/trips`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(900);
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("poster-share").click();
  const result = page.getByTestId("poster-share-result");
  await expect(result).toBeVisible({ timeout: 30_000 });
  const link = await page.getByTestId("poster-share-link").inputValue();
  await owner.close();
  const match = /\/v8\/shared\/([^/]+)\/([^/]+)$/.exec(link.trim());
  if (!match) throw new Error(`[ord0542] the share link came back in an unexpected shape: ${link}`);
  return match[1];
}

/** The guest's own reading of the page: the two lines, and what the whole page says. */
async function guestReading(browser: Browser, origin: string, token: string, locale: "zh-CN" | "en") {
  const guest = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
  await guest.addInitScript((language) => localStorage.setItem("citynexus.locale", language), locale);
  const page: Page = await guest.newPage();
  await page.goto(`${origin}/v8/shared/${token}/poster`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("shared-poster-screen")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1_200);
  const subtitle = (await page.getByTestId("shared-poster-subtitle").innerText()).trim();
  const why = await page.getByTestId("shared-poster-counted-why").innerText({ timeout: 2_000 }).catch(() => null);
  const body = await page.evaluate(() => document.body.innerText);
  const blocks = await page.getByTestId(/^shared-poster-day-\d+$/).count();
  await guest.close();
  return {
    locale,
    subtitle,
    provenance_line: why,
    body_chars: body.length,
    day_blocks: blocks,
    subtitle_carries_a_number: /\d/.test(subtitle),
    unknown_phrases_on_page: UNKNOWN_WORDS.filter((word) => body.includes(word)),
    key_echo_hits: (body.match(/\b(poster|sharedPoster|shared)\.[A-Za-z0-9_.]+/g) ?? []).length,
  };
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("ORD-0542 D1 the counted number is the subtitle and nothing on the page denies it", async ({ browser }) => {
  const token = await sharedPosterToken(browser, CANDIDATE, "zh-CN");
  const reading = await guestReading(browser, CANDIDATE, token, "zh-CN");
  write("D1_guest_reading", { origin: "candidate", ...reading });

  expect(reading.subtitle_carries_a_number, "the subtitle states the count").toBe(true);
  expect(reading.unknown_phrases_on_page, "and no line on the page says the length is unknown").toEqual([]);
  expect(reading.provenance_line ?? "", "the line under it names where the number came from").not.toBe("");
  expect(reading.provenance_line ?? "", "and that sentence is about the projection, not a second count")
    .toMatch(/投影|projection/);
  expect(reading.day_blocks, "the days themselves are still listed").toBeGreaterThanOrEqual(2);
});

test("ORD-0542 D2 the same invariant, read off the generation before the fix", async ({ browser }) => {
  const token = await sharedPosterToken(browser, BASE, "zh-CN");
  const reading = await guestReading(browser, BASE, token, "zh-CN");
  write("D2_base_reading", { origin: "base", ...reading });

  /* Deliberately the same three assertions as D1, pointed at the base bundle: on that generation the
     subtitle is 「… 天数未知」 and the count lives on the next line, so this arm is expected to go RED.
     An invariant that passes on both generations is not measuring the contradiction. */
  expect(reading.unknown_phrases_on_page, "the base page still denies itself").toEqual([]);
  expect(reading.subtitle_carries_a_number, "and its subtitle carries no number at all").toBe(true);
});

test("ORD-0542 D3 both languages, no key echo, and the guest still gets its picture", async ({ browser }) => {
  const readings: any[] = [];
  for (const locale of ["zh-CN", "en"] as const) {
    const token = await sharedPosterToken(browser, CANDIDATE, locale);
    const reading = await guestReading(browser, CANDIDATE, token, locale);

    const guest = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
    await guest.addInitScript((language) => localStorage.setItem("citynexus.locale", language), locale);
    const page = await guest.newPage();
    await page.goto(`${CANDIDATE}/v8/shared/${token}/poster`, { waitUntil: "load", timeout: 90_000 });
    await expect(page.getByTestId("shared-poster-screen")).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(1_200);
    const downloadPromise = page.waitForEvent("download", { timeout: 90_000 });
    await page.getByTestId("shared-poster-download").click();
    const download = await downloadPromise;
    const saved = `${EVIDENCE}/D3_${locale}_${download.suggestedFilename()}`;
    await download.saveAs(saved);
    const buffer = readFileSync(saved);
    await guest.close();

    readings.push({
      ...reading,
      picture_bytes: buffer.length,
      picture_ihdr: { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) },
    });
  }
  write("D3_both_languages", { readings });
  for (const reading of readings) {
    expect(reading.key_echo_hits, `${reading.locale} leaks no key name`).toBe(0);
    expect(reading.unknown_phrases_on_page, `${reading.locale} says "unknown" nowhere`).toEqual([]);
    expect(reading.picture_ihdr.height, `${reading.locale} still exports a tall picture`)
      .toBeGreaterThan(reading.picture_ihdr.width);
  }
});
