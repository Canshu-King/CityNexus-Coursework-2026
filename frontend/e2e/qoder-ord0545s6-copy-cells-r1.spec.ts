/* ORD-0545 slice six (CMDR 01:37, option 乙) -- two cells on the import screen, retaken as product bytes.
 *
 * Cell 1 (was `PoiImportPanel.tsx:489`): the channel slot printed the answer's machine token
 *   (`LOCAL_TEXT_PARSER`) straight onto the main screen. It now prints this family's own words, and the
 *   token is only available inside a fold a human opens on purpose.
 * Cell 2 (was `:463`): when the device measured no status, the technical cell repeated the refusal
 *   sentence above it verbatim. It now says the measured fact: no status.
 *
 * Every expectation is read out of `poiImportCopy.ts` at run time (never retyped here), so a wording
 * change moves the expectation with it. The two locale arms are separate tests, so one locale cannot
 * cover for the other.
 *
 * Arms:
 *   Azh / Aen -- a parse that succeeds: the channel cell's words equal the table, the folded token is
 *                NOT rendered while closed, and becomes readable only after the summary is clicked.
 *   Bzh / Ben  -- the device's own network dropped: the technical line equals the table's no-status
 *                line, is NOT the same sentence as the refusal above it, and the refusal sentence
 *                itself is still there (positive control: this slice removed a duplicate, not a message).
 */
import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const CANARY_TRIP = "22222222-2222-4222-8222-222222222222";
const TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const PANEL = "frontend/src/features/poiimport/PoiImportPanel.tsx";
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0545", "readouts");
const TAG = process.env.CITYNEXUS_ORD0545_TAG ?? "s6r1";
const BASE_TIP = process.env.CITYNEXUS_ORD0545_BASE_TIP ?? "";
if (!/^[0-9a-f]{40}$/.test(BASE_TIP)) COLLECTION_PREREQ.push("CITYNEXUS_ORD0545_BASE_TIP must be the 40-hex tip this was measured on");

const MATCHED_BY_LOCALE: Record<string, string> = { "zh-CN": "Nexus City Park", "en-GB": "Design Library" };
const READ_CAP = { timeout: 5_000 };
const HALF = (locale: string) => (locale === "zh-CN" ? "zh" : "en");

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

async function attr(locator: any, name: string): Promise<string | null> {
  return locator.getAttribute(name, READ_CAP);
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

/** Machine tokens rendered where the channel cell is supposed to speak. Rendered text is the test, not the
 *  DOM string: the token deliberately lives in the DOM inside the fold, and a collapsed <details> child is
 *  not rendered -- that difference is the whole point of this slice, so both are recorded separately. */
function renderedMachineWords(s: string) {
  return [...new Set(s.match(/\b[A-Z][A-Z_]{4,}\b/g) ?? [])].sort();
}

async function channelArm(page: any, locale: string) {
  const pair = tablePair("channelValueLocal");
  const toggle = tablePair("technicalToggle");
  await page.getByTestId("poi-import-text").fill(MATCHED_BY_LOCALE[locale], { timeout: 30_000 });
  const answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
  await page.getByTestId("poi-import-parse").click();
  const body = await (await answered).json();
  await expect(page.getByTestId("poi-import-counts"), "the success cell must be on the glass").toBeVisible({
    timeout: 20_000,
  });

  const nameCell = page.getByTestId("poi-import-channel-name");
  const painted = await text(nameCell);
  const folded = page.getByTestId("poi-import-channel-token");
  const card = page.getByTestId("poi-import-card");
  const closedRendered = await folded.isVisible().catch(() => false);
  const closedRenderedText = closedRendered ? await text(folded) : "";
  const foldOpen = await attr(page.getByTestId("poi-import-channel-fold"), "open");
  const domWhileClosed = await card.evaluate((el: Element) => el.textContent ?? "");
  const cardRenderedWhileClosed = ((await card.innerText(READ_CAP)) ?? "").replace(/\s+/g, " ").trim();

  // open the fold the way a traveller would
  await page.getByTestId("poi-import-channel-fold").locator("summary").click();
  await expect(folded, "the token must be readable once the fold is opened").toBeVisible({ timeout: 10_000 });
  const openedText = await text(folded);
  const cardRenderedWhileOpen = ((await card.innerText(READ_CAP)) ?? "").replace(/\s+/g, " ").trim();

  const cellWords = await text(page.getByTestId("poi-import-channel-cell"));
  // Rendered text and DOM text disagree on purpose here: a closed <details> keeps its child in the DOM
  // without painting it. Which of the two a census counts decides whether this fold clears a screen or
  // only a viewport, so both are measured rather than argued.
  const noteWords = await text(page.getByTestId("poi-import-channel-name").locator("xpath=../small"));
  const summaryWords = await text(page.getByTestId("poi-import-channel-fold").locator("summary"));
  return {
    arm: "A",
    locale,
    parse_counts: body.counts ?? null,
    channel_cell_words: painted,
    expected_from_the_table: pair[HALF(locale)],
    channel_words_equal_the_table: painted === pair[HALF(locale)],
    toggle_words: summaryWords,
    toggle_equals_the_table: summaryWords === toggle[HALF(locale)],
    folded_attribute_before_click: foldOpen === null ? "closed" : "open",
    token_rendered_while_closed: closedRendered,
    token_text_while_closed: closedRenderedText,
    token_in_the_cards_dom_while_closed: domWhileClosed.includes("LOCAL_TEXT_PARSER"),
    token_in_the_cards_rendered_text_while_closed: cardRenderedWhileClosed.includes("LOCAL_TEXT_PARSER"),
    token_in_the_cards_rendered_text_while_open: cardRenderedWhileOpen.includes("LOCAL_TEXT_PARSER"),
    machine_words_rendered_in_card_while_closed: renderedMachineWords(cardRenderedWhileClosed),
    token_text_after_click: openedText,
    token_is_the_servers_name: openedText === "LOCAL_TEXT_PARSER",
    machine_words_in_channel_cell_after_open: renderedMachineWords(cellWords),
    channel_cell_words_after_open: cellWords,
    note_words: noteWords,
    note_equals_the_table: noteWords === tablePair("channelNote")[HALF(locale)],
    dom_text_length_while_closed: domWhileClosed.length,
  };
}

async function noStatusArm(page: any, context: any, locale: string) {
  const refusal = tablePair("errNoResponse");
  const measured = tablePair("noStatusMeasured");
  await page.getByTestId("poi-import-text").fill(MATCHED_BY_LOCALE[locale], { timeout: 30_000 });
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
  const wholeCell = await text(page.getByTestId("poi-import-error"));
  return {
    arm: "B",
    locale,
    click_outcome: refused,
    sentence_on_glass: sentence,
    expected_refusal_from_the_table: refusal[HALF(locale)],
    sentence_equals_the_table_half: sentence === refusal[HALF(locale)],
    technical_line: detail,
    expected_technical_from_the_table: measured[HALF(locale)],
    technical_line_equals_the_table_no_status_line: detail === measured[HALF(locale)],
    technical_line_is_the_same_sentence: detail === sentence,
    refusal_cell_text: wholeCell,
    machine_words_in_refusal_cell: renderedMachineWords(wholeCell),
  };
}

const collected: any[] = [];

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("Azh zh-CN 渠道格说的是词表的人话，机词只在折叠里", async ({ page }) => {
  await mountPanel(page, "zh-CN");
  const r = await channelArm(page, "zh-CN");
  expect(r.channel_words_equal_the_table, `the cell painted "${r.channel_cell_words}"`).toBe(true);
  expect(r.toggle_equals_the_table, `the summary read "${r.toggle_words}"`).toBe(true);
  expect(r.folded_attribute_before_click, "the fold must start closed").toBe("closed");
  expect(r.token_rendered_while_closed, "no token may be rendered while the fold is closed").toBe(false);
  expect(r.token_text_after_click, "opening the fold must reveal the server's own name").toBe("LOCAL_TEXT_PARSER");
  expect(r.machine_words_in_channel_cell_after_open, "once opened on purpose the token is the only machine word there").toEqual(["LOCAL_TEXT_PARSER"]);
  expect(r.machine_words_rendered_in_card_while_closed, "while the fold is shut the card renders no machine word").toEqual([]);
  collected.push(r);
});

test("Aen en-GB the same fold walk reads the table's en half", async ({ page }) => {
  await mountPanel(page, "en-GB");
  const r = await channelArm(page, "en-GB");
  expect(r.channel_words_equal_the_table, `the cell painted "${r.channel_cell_words}"`).toBe(true);
  expect(r.toggle_equals_the_table).toBe(true);
  expect(r.folded_attribute_before_click).toBe("closed");
  expect(r.token_rendered_while_closed).toBe(false);
  expect(r.token_text_after_click).toBe("LOCAL_TEXT_PARSER");
  expect(r.machine_words_in_channel_cell_after_open).toEqual(["LOCAL_TEXT_PARSER"]);
  expect(r.machine_words_rendered_in_card_while_closed).toEqual([]);
  collected.push(r);
});

test("Bzh zh-CN 无量到状态时技术格写「没有状态」，不再重述那句", async ({ page, context }) => {
  await mountPanel(page, "zh-CN");
  const r = await noStatusArm(page, context, "zh-CN");
  expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
  expect(r.technical_line_equals_the_table_no_status_line, `the technical line read "${r.technical_line}"`).toBe(true);
  expect(r.technical_line_is_the_same_sentence, "slice six removed the verbatim repeat -- this is the fix").toBe(false);
  expect(r.machine_words_in_refusal_cell).toEqual([]);
  collected.push(r);
});

test("Ben en-GB the same no-status walk reads the table's en half", async ({ page, context }) => {
  await mountPanel(page, "en-GB");
  const r = await noStatusArm(page, context, "en-GB");
  expect(r.sentence_equals_the_table_half, `glass said "${r.sentence_on_glass}"`).toBe(true);
  expect(r.technical_line_equals_the_table_no_status_line).toBe(true);
  expect(r.technical_line_is_the_same_sentence).toBe(false);
  expect(r.machine_words_in_refusal_cell).toEqual([]);
  collected.push(r);
});

test.afterAll(async () => {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  const panelSource = fs.readFileSync(path.join(process.cwd(), "..", PANEL), "utf8");
  fs.writeFileSync(
    path.join(EVIDENCE, `copy_cells_s6_${TAG}.json`),
    JSON.stringify(
      {
        order: "ORD-0545 slice six (CMDR 01:37 option 乙)",
        base_tip: BASE_TIP,
        written_at: new Date().toISOString(),
        cells: {
          channel_main_slot_reads_the_table: panelSource.includes('poiImportCopy(locale, CHANNEL_NAME['),
          token_only_inside_the_fold: /<details[\s\S]{0,240}<code data-testid="poi-import-channel-token">/.test(panelSource),
          no_status_line_is_a_table_key: panelSource.includes('poiImportCopy(locale, "noStatusMeasured")'),
        },
        arms: collected,
      },
      null,
      1,
    ),
    { encoding: "utf8" },
  );
});
