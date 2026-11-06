/* ORD-0545 slice 4 -- CMDR 01:24 put this in my lane: 「POI 导入屏走查：新文案在屏上逐格读」.
 *
 * Slice 2 proved the moved family is complete in its own table and slice 3 put one refusal on the
 * glass.  Neither proves the screen paints every one of those sentences cell by cell in the states a
 * traveller actually reaches.  A gate that reads the table cannot see: a cell whose copy call was
 * dropped in a later edit, an interpolation that lost its parameter, a state earlier walks never
 * entered, or a cell that stopped going through the table and prints a server field instead.
 *
 * So this arm walks the product's own path on the day24 line and reads the glass cell by cell, twice:
 * once in zh-CN, once in en-GB.  Each cell is checked from two directions --
 *   1. the SHIPPED SOURCE must route that cell through the table key this gate claims, otherwise the
 *      expectation could have rotted without the arm noticing (a stale map is a failure, not a guess);
 *   2. the RENDERED TEXT must equal that key's half of the table, with dynamic slots filled from what
 *      the server actually answered on the wire -- never typed in here.
 *
 * States walked (per locale):
 *   A  fresh screen, nothing pasted      -- kicker / title / lede / the channel badge / the agent-card
 *                                          head (brand, modes title, its sub, the boundary pair) /
 *                                          both mode chips / the field label / the scope hint / the
 *                                          parse button / the character count at zero / the day
 *                                          disclosure.
 *   A2 the screenshot chip               -- the "not supported" sentence, then back to text.
 *   B  pasted ".." (the door extracts no line) -- the empty-results sentence.
 *   C  pasted a line the city cannot match -- the counts line with unresolved = 1, the candidate row's
 *                                          own dictionary-served state word and its "why" line (that
 *                                          nine-key family stayed in the shared dictionary; the walk
 *                                          reads it where it renders).
 *   D  pasted a name the catalogue holds -- counts line from the parse response, phase pill, retry
 *                                          button, the permanent verify warning, the select-all row,
 *                                          the exits block (title, lead, selection count, three exit
 *                                          labels, the store-only reason line), the channel facts cell,
 *                                          the day-result cell.
 *   E  the row picked                    -- the select-all row's other half and the selection count
 *                                          with n moved.
 *   F  tapped 加进计划 on a planned journey -- the read-back success sentence carrying the wire's own
 *                                          n and day.
 *   G  tapped it again (a replay)        -- the "already there" sentence, and NO success sentence:
 *                                          ORD-0524's rule that a replay may not mint a second one.
 *
 * Whole-screen census at the end of each locale: `poiImport.*` key names, table key names, unfilled
 * `{placeholder}`, bare uuids, and the upper-case census over both channels (rendered text and DOM text
 * nodes) with the testid that painted each hit.  Cells that print a server field are reported there,
 * not folded into the green.
 */
import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const COLLECTION_PREREQ: string[] = [];

const TRIP = "22222222-2222-4222-8222-222222222222";
const PANEL = "frontend/src/features/poiimport/PoiImportPanel.tsx";
const CARD = "frontend/src/components/PoiCandidateCard.tsx";
const TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const DICTIONARY = "frontend/src/i18n/LocaleContext.tsx";
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0545", "readouts");
const TAG = process.env.CITYNEXUS_ORD0545_TAG ?? "r1";
const BASE_TIP = process.env.CITYNEXUS_ORD0545_BASE_TIP ?? "";
if (!/^[0-9a-f]{40}$/.test(BASE_TIP)) COLLECTION_PREREQ.push("CITYNEXUS_ORD0545_BASE_TIP must be the 40-hex tip this was measured on");

const UNMATCHED = "zzz-not-a-place-anywhere zzz";
/* Both locale arms share ONE copy of the store, so the two legs consume two different catalogue rows:
 * the first leg's write is still on the plan when the second leg boots.  A re-run against a store that
 * already carries the place is reported as such by the arm rather than being passed off as a success
 * sentence -- see the write_leg field in the evidence. */
const MATCHED_BY_LOCALE: Record<string, string> = { "zh-CN": "Nexus City Park", "en-GB": "Design Library" };
const NOTHING = "..";

const repoRoot = path.join(process.cwd(), "..");
const read = (rel: string) => fs.readFileSync(path.join(repoRoot, rel), "utf-8");
const norm = (s: string) => s.replace(/\s+/g, " ").trim();
/** CSS `text-transform` repaints Latin cells in capitals, so equality here is about the word sequence. */
const fold = (s: string) => norm(s).toLocaleLowerCase();

function writeEvidence(name: string, payload: unknown) {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  fs.writeFileSync(
    path.join(EVIDENCE, `${name}_${TAG}.json`),
    JSON.stringify(
      { generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() }, ...(payload as object) },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
}

/** One half of a sentence out of the shipped table, with {slots} filled from the wire. */
function tableHalf(key: string, half: "zh" | "en", slots: Record<string, string | number> = {}) {
  const text = read(TABLE);
  const open = text.indexOf(`  ${key}: {`);
  if (open === -1) throw new Error(`${key} is not in the copy table`);
  const block = text.slice(open, text.indexOf("\n  }", open));
  const at = block.indexOf(`${half}: "`);
  if (at === -1) throw new Error(`${key} has no ${half} half`);
  const from = at + half.length + 3;
  let raw = block.slice(from, block.indexOf('"', from));
  if (raw.includes("{plural:")) throw new Error(`${key} uses a plural marker this arm does not expand`);
  for (const [slot, value] of Object.entries(slots)) raw = raw.split(`{${slot}}`).join(String(value));
  const left = raw.match(/\{[a-z_]+\}/);
  if (left) throw new Error(`${key}/${half} left ${left[0]} unfilled -- the slot list here is stale`);
  return raw;
}

/** The nine card keys never came into the table; read the value the dictionary really serves. */
function dictionaryValue(dottedKey: string, locale: "zh-CN" | "en-GB") {
  const text = read(DICTIONARY);
  const split = text.indexOf("\nconst zhCN");
  expect(split, "the zhCN book is no longer where this arm reads it").toBeGreaterThan(0);
  const book = locale === "zh-CN" ? text.slice(split) : text.slice(0, split);
  const at = book.indexOf(`"${dottedKey}": "`);
  if (at === -1) throw new Error(`${dottedKey} is not in the ${locale} book`);
  const from = at + dottedKey.length + 5;
  return book.slice(from, book.indexOf('"', from));
}

/** The shipped source must still route this element through this key -- a stale map fails the arm. */
function sourceRoutes(marker: string, key: string, file = PANEL) {
  const text = read(file);
  const at = text.indexOf(marker);
  if (at === -1) throw new Error(`marker ${marker} is not in the shipped ${file}`);
  const window = text.slice(at, at + 1_400);
  return (
    window.includes(`poiImportCopy(locale, "${key}"`) ||
    window.includes(`copyFor("${key}"`) ||
    window.includes(`t("${key}"`)
  );
}

type Row = {
  state: string; cell: string; source: string; key: string;
  expected: string; rendered: string; equal: boolean; byte_equal: boolean; css_uppercased: boolean;
};

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test.describe("ORD-0545 slice 4 the POI import screen, cell by cell", () => {
  for (const locale of ["zh-CN", "en-GB"] as const) {
    const half: "zh" | "en" = locale === "zh-CN" ? "zh" : "en";

    test(`W${half} ${locale} 从头屏到重放，逐格 == 表里那一半`, async ({ page }) => {
      const MATCHED = MATCHED_BY_LOCALE[locale];
      const rows: Row[] = [];
      const wire: Record<string, unknown> = {};
      const seen = new Set<string>();

      const check = async (
        state: string,
        cell: string,
        key: string,
        rendered: string,
        expected: string,
        source: "table" | "dictionary",
        marker?: string,
        file?: string,
      ) => {
        if (marker) {
          expect(sourceRoutes(marker, key, file), `${cell}: the shipped source no longer routes it through ${key}`).toBe(
            true,
          );
        }
        seen.add(`${source}:${key}`);
        // CSS `text-transform` repaints Latin cells in capitals, so the claim tested here is the word
        // sequence compared case-folded; whether the painted bytes differ from the table's own is
        // recorded beside it (byte_equal / css_uppercased) rather than hidden inside a pass.
        const foldEqual = norm(rendered).toLocaleLowerCase() === norm(expected).toLocaleLowerCase();
        const byteEqual = norm(rendered) === norm(expected);
        rows.push({
          state, cell, source, key, expected, rendered,
          equal: foldEqual, byte_equal: byteEqual, css_uppercased: foldEqual && !byteEqual,
        });
        expect(foldEqual, `${state} / ${cell} must paint ${key} (${source})`).toBe(true);
      };

      const text = async (locator: any) => norm(await locator.innerText({ timeout: 5_000 }));
      /** A cell that nests a technical line (an HTTP status, a server enum) is read by its own text
       *  nodes, so the sentence is compared without the nested widget's contents. */
      const ownText = async (locator: any) =>
        norm(
          await locator.evaluate((el: Element) =>
            Array.from(el.childNodes)
              .filter((n) => n.nodeType === 3)
              .map((n) => n.textContent ?? "")
              .join(" "),
          ),
        );

      await page.addInitScript(
        ([id, which]) => {
          localStorage.setItem("citynexus.v8.journey_id", id);
          localStorage.setItem("citynexus.locale", which);
        },
        [TRIP, locale],
      );
      await page.goto("/v8/import");
      await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 40_000 });

      // ---------- A  head and composer, before anything is pasted
      const head: [string, any, string, string][] = [
        ["kicker", page.locator('[data-testid="poi-import"] .v8-kicker'), "kicker", 'v8-kicker">{poiImportCopy(locale, "kicker")'],
        ["title", page.locator('[data-testid="poi-import"] h1').first(), "title", 'h1>{poiImportCopy(locale, "title")'],
        ["lede", page.locator('[data-testid="poi-import"] .v8-lede'), "lede", 'v8-lede">{poiImportCopy(locale, "lede")'],
        ["channel_badge", page.locator(".v8-evidence-label").first(), "channelLocal", "v8-evidence-label"],
        ["brand", page.locator(".v8-agent-mark"), "brand", 'v8-agent-mark">{poiImportCopy(locale, "brand")'],
        ["modes_title", page.locator(".v8-agent-composer-card h2").first(), "modesTitle", 'h2>{poiImportCopy(locale, "modesTitle")'],
        ["modes_sub", page.locator(".v8-agent-copy p").first(), "modesSub", 'v8-agent-copy'],
        ["boundary", page.locator(".v8-agent-boundary b"), "boundary", 'b>{poiImportCopy(locale, "boundary")'],
        ["boundary_sub", page.locator(".v8-agent-boundary span"), "boundarySub", 'span>{poiImportCopy(locale, "boundarySub")'],
        ["mode_text", page.getByTestId("poi-import-mode-text"), "modeText", 'data-testid="poi-import-mode-text"'],
        ["mode_screenshot", page.getByTestId("poi-import-mode-screenshot"), "modeScreenshot", 'data-testid="poi-import-mode-screenshot"'],
        ["field_label", page.locator('label[for="poi-import-text"]'), "intent", 'htmlFor="poi-import-text"'],
        ["scope_hint", page.getByTestId("poi-import-scope-hint"), "scopeHint", 'data-testid="poi-import-scope-hint"'],
        ["parse_button", page.getByTestId("poi-import-parse"), "parseAction", 'data-testid="poi-import-parse"'],
      ];
      for (const [cell, locator, key, marker] of head) {
        await check("A", cell, key, await text(locator), tableHalf(key, half), "table", marker);
      }

      // the ghost text in the box is copy too, but its channel is the placeholder attribute -- named as such
      const placeholderAttr = await page.getByTestId("poi-import-text").getAttribute("placeholder");
      await check(
        "A(placeholder attribute)", "textarea_placeholder", "placeholder", placeholderAttr ?? "",
        tableHalf("placeholder", half), "table", 'data-testid="poi-import-text"',
      );
      await check(
        "A", "char_count_zero", "charCount", await text(page.getByTestId("poi-import-char-count")),
        tableHalf("charCount", half, { n: 0 }), "table", 'data-testid="poi-import-char-count"',
      );

      // one cell, three parts: label + the day an import would pick + the note
      const disclosure = await text(page.getByTestId("poi-import-day-disclosure"));
      const label = tableHalf("autoDayLabel", half);
      const note = tableHalf("autoDayNote", half);
      const dayOnGlass = norm(
        norm(disclosure.slice(label.length)).replace(new RegExp(`\\s*${note.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`), ""),
      ).replace(/^[\s—-]+|[\s—-]+$/g, "");
      wire.day_disclosure = { rendered: disclosure, label, note, middle: dayOnGlass };
      expect(disclosure.startsWith(label), `A / day disclosure opens with autoDayLabel, got ${disclosure}`).toBe(true);
      expect(disclosure.endsWith(note), "A / day disclosure closes with autoDayNote").toBe(true);
      // Before anything is parsed the middle slot is either a real date the plan already has, or the
      // table's own "no day yet" sentence -- both are honest answers, and an invented third is not.
      const isDate = /^\d{4}-\d{2}-\d{2}$/.test(dayOnGlass);
      const isUnknownSentence = fold(dayOnGlass) === fold(tableHalf("autoDayUnknown", half));
      expect(isDate || isUnknownSentence, `A / middle slot must be a date or autoDayUnknown, got ${dayOnGlass}`).toBe(
        true,
      );
      rows.push({
        state: "A", cell: "day_disclosure", source: "table", key: "autoDayLabel+autoDayNote",
        expected: `${label} <${dayOnGlass}> ${note}`, rendered: disclosure,
        equal: disclosure.startsWith(label) && disclosure.endsWith(note),
        byte_equal: true,
        css_uppercased: false,
      });

      // ---------- A2 the screenshot chip's own sentence, then back to text
      await page.getByTestId("poi-import-mode-screenshot").click();
      await check(
        "A2", "screenshot_unsupported", "screenshotNotSupported",
        await text(page.getByTestId("poi-import-mode-unsupported")),
        tableHalf("screenshotNotSupported", half), "table", 'data-testid="poi-import-mode-unsupported"',
      );
      await page.getByTestId("poi-import-mode-text").click();

      // ---------- B the door extracts no line at all -> the empty-results sentence
      await page.getByTestId("poi-import-text").fill(NOTHING);
      let answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
      await page.getByTestId("poi-import-parse").click();
      let body = await (await answered).json();
      wire.empty_parse = { extracted: body.counts?.extracted, items: (body.items ?? []).length };
      await expect(page.getByTestId("poi-import-empty")).toBeVisible({ timeout: 30_000 });
      await check(
        "B", "empty_results", "emptyExtracted", await text(page.getByTestId("poi-import-empty")),
        tableHalf("emptyExtracted", half), "table", 'data-testid="poi-import-empty"',
      );

      // ---------- C a line the city cannot match -> counts say so, and the card's own family speaks
      await page.getByTestId("poi-import-text").fill(UNMATCHED);
      answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
      await page.getByTestId("poi-import-parse").click();
      body = await (await answered).json();
      const unmatchedCounts = body.counts ?? {};
      await check(
        "C", "counts_unmatched", "counts", await text(page.getByTestId("poi-import-counts")),
        tableHalf("counts", half, {
          extracted: unmatchedCounts.extracted ?? 0, resolved: unmatchedCounts.resolved ?? 0,
          ambiguous: unmatchedCounts.ambiguous ?? 0, unresolved: unmatchedCounts.unresolved ?? 0,
        }),
        "table", 'data-testid="poi-import-counts"',
      );
      await check(
        "C", "char_count_typed", "charCount", await text(page.getByTestId("poi-import-char-count")),
        tableHalf("charCount", half, { n: UNMATCHED.length }), "table", 'data-testid="poi-import-char-count"',
      );
      await check(
        "C", "card_state_unresolved", "poiImport.cardUnresolved",
        await text(page.getByTestId("poi-candidate-state").first()),
        dictionaryValue("poiImport.cardUnresolved", locale), "dictionary", 't("poiImport.cardUnresolved")', CARD,
      );
      await check(
        "C", "card_why_unresolved", "poiImport.cardUnresolvedWhy",
        await text(page.getByTestId("poi-candidate-reason").first()),
        dictionaryValue("poiImport.cardUnresolvedWhy", locale), "dictionary", 't("poiImport.cardUnresolvedWhy")', CARD,
      );

      // ---------- D a name the catalogue really holds
      await page.getByTestId("poi-import-text").fill(MATCHED_BY_LOCALE[locale]);
      answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
      await page.getByTestId("poi-import-parse").click();
      body = await (await answered).json();
      const counts = body.counts ?? {};
      wire.matched_parse = counts;
      await check(
        "D", "counts_matched", "counts", await text(page.getByTestId("poi-import-counts")),
        tableHalf("counts", half, {
          extracted: counts.extracted ?? 0, resolved: counts.resolved ?? 0,
          ambiguous: counts.ambiguous ?? 0, unresolved: counts.unresolved ?? 0,
        }),
        "table", 'data-testid="poi-import-counts"',
      );
      await check(
        "D", "phase_pill", "phaseSuccess", await text(page.getByTestId("poi-import-phase")),
        tableHalf("phaseSuccess", half), "table", 'data-testid="poi-import-phase"',
      );
      await check(
        "D", "retry_button", "retry", await text(page.getByTestId("poi-import-retry")),
        tableHalf("retry", half), "table", 'data-testid="poi-import-retry"',
      );
      await check(
        "D", "verify_warning", "verifyWarning", await text(page.getByTestId("poi-import-verify-warning")),
        tableHalf("verifyWarning", half), "table", 'data-testid="poi-import-verify-warning"',
      );
      await check(
        "D", "select_all", "selectAll",
        await text(page.locator('[data-testid="poi-import-select-all"] .v8-pick-box-text')),
        tableHalf("selectAll", half), "table", 'data-testid="poi-import-select-all"',
      );
      await check(
        "D", "selection_count_zero", "selectedCount", await text(page.getByTestId("poi-import-selection-count")),
        tableHalf("selectedCount", half, { n: 0, total: counts.addable ?? 1 }), "table",
        'data-testid="poi-import-selection-count"',
      );
      await check(
        "D", "exits_lead", "exitsLead", await text(page.getByTestId("poi-import-exits-lead")),
        tableHalf("exitsLead", half), "table", 'data-testid="poi-import-exits-lead"',
      );
      await check(
        "D", "exits_title", "exitsTitle", await text(page.locator(".v8-boundary-card > b").first()),
        tableHalf("exitsTitle", half), "table", 'b>{poiImportCopy(locale, "exitsTitle")',
      );
      for (const [cell, testid, key] of [
        ["exit_plan", "poi-import-exit-plan", "addToPlan"],
        ["exit_shelf", "poi-import-exit-shelf", "favorite"],
        ["exit_store_only", "poi-import-exit-store-only", "exitStoreOnly"],
      ] as [string, string, string][]) {
        await check("D", cell, key, await text(page.getByTestId(testid)), tableHalf(key, half), "table", `data-testid="${testid}"`);
      }
      await check(
        "D", "store_only_why", "storeOnlyRefusal", await text(page.getByTestId("poi-import-store-only-why")),
        tableHalf("storeOnlyRefusal", half), "table", 'data-testid="poi-import-store-only-why"',
      );
      await check(
        "D", "card_state_matched", "poiImport.cardMatched",
        await text(page.getByTestId("poi-candidate-state").first()),
        dictionaryValue("poiImport.cardMatched", locale), "dictionary", 't("poiImport.cardMatched")', CARD,
      );
      await check(
        "D", "card_phrase", "poiImport.cardPhrase",
        await text(page.getByTestId("poi-candidate-phrase")),
        dictionaryValue("poiImport.cardPhrase", locale).replace("{phrase}", MATCHED_BY_LOCALE[locale]), "dictionary",
        't("poiImport.cardPhrase"', CARD,
      );

      // the facts cell: label and note through the table, and the middle slot prints the server field
      const facts = await page.getByTestId("poi-import-card").innerText();
      expect(facts.includes(tableHalf("channelLabel", half)), "D / the channel label must be the table's").toBe(true);
      expect(facts.includes(tableHalf("channelNote", half)), "D / the channel note must be the table's").toBe(true);
      wire.channel_facts_cell = {
        renders_channel_label: facts.includes(tableHalf("channelLabel", half)),
        renders_channel_note: facts.includes(tableHalf("channelNote", half)),
        prints_server_field_verbatim: /LOCAL_TEXT_PARSER/.test(facts),
      };
      const dayResult = await text(page.getByTestId("poi-import-day-result"));
      expect(dayResult.includes(tableHalf("autoDayLabel", half)), "D / day-result carries autoDayLabel").toBe(true);
      expect(dayResult.includes(tableHalf("autoDayNote", half)), "D / day-result carries autoDayNote").toBe(true);
      expect(/\d{4}-\d{2}-\d{2}/.test(dayResult), `D / day-result must name a real day, got ${dayResult}`).toBe(true);

      // ---------- E pick the row
      await page.getByTestId("poi-card-select").first().click();
      await check(
        "E", "select_all_on", "selectAllOn",
        await text(page.locator('[data-testid="poi-import-select-all"] .v8-pick-box-text')),
        tableHalf("selectAllOn", half), "table", 'data-testid="poi-import-select-all"',
      );
      await check(
        "E", "selection_count_one", "selectedCount", await text(page.getByTestId("poi-import-selection-count")),
        tableHalf("selectedCount", half, { n: 1, total: counts.addable ?? 1 }), "table",
        'data-testid="poi-import-selection-count"',
      );

      // ---------- F a real write on a planned journey
      const addAnswered = page.waitForResponse((r: any) => r.url().includes("/poi-import/add"), { timeout: 30_000 });
      await page.getByTestId("poi-import-add").click();
      const added = await (await addAnswered).json();
      wire.add = { n: (added.added ?? []).length, day: added.day, day_source: added.day_source };
      const successSentence = tableHalf("addedCount", half, {
        n: (added.added ?? []).length, day: added.day,
      });
      if ((added.added ?? []).length > 0) {
        await expect(page.getByTestId("poi-import-added")).toBeVisible({ timeout: 20_000 });
        const successRendered = await ownText(page.getByTestId("poi-import-added"));
        await check("F", "added_read_back", "addedCount", successRendered, successSentence, "table", 'data-testid="poi-import-added"');
        wire.write_leg = "fresh write -- the success sentence was read off the glass";
        // The widget nested in that same paragraph prints the server's own enum.  It only exists on this
        // branch, so it is read here: an earlier cut reached for it unconditionally and `innerText` then
        // auto-waited out the whole test timeout on a cell that was never going to appear.
        const addedSourceCell = await text(page.getByTestId("poi-import-added-source"));
        wire.day_source_cell = {
          rendered: addedSourceCell,
          server_value: added.day_source,
          painted_verbatim: addedSourceCell === added.day_source,
          goes_through_the_table: false,
        };
      } else {
        // The store this run started from already carried the row, so the honest sentence is the replay's.
        await expect(page.getByTestId("poi-import-already-present")).toBeVisible({ timeout: 20_000 });
        const alreadyRendered = await text(page.getByTestId("poi-import-already-present"));
        await check(
          "F", "already_present_instead", "alreadyPresent", alreadyRendered,
          tableHalf("alreadyPresent", half, { n: (added.already_present ?? []).length }), "table",
          'data-testid="poi-import-already-present"',
        );
        expect(await page.getByTestId("poi-import-added").count(), "F / a replay may not wear a success sentence").toBe(0);
        wire.write_leg = "store already had this place -- the already-there sentence was read instead";
        // the enum widget belongs to the success paragraph, which is absent on this branch: named, not guessed
        wire.day_source_cell = { rendered: null, server_value: added.day_source, painted_verbatim: null };
      }

      // ---------- G the same tap again: a replay may not mint a second success sentence
      const replayAnswered = page.waitForResponse((r: any) => r.url().includes("/poi-import/add"), { timeout: 30_000 });
      await page.getByTestId("poi-import-add").click();
      const replay = await (await replayAnswered).json();
      wire.replay = { already_present: (replay.already_present ?? []).length, added: (replay.added ?? []).length };
      await expect(page.getByTestId("poi-import-already-present")).toBeVisible({ timeout: 20_000 });
      await check(
        "G", "already_present", "alreadyPresent", await text(page.getByTestId("poi-import-already-present")),
        tableHalf("alreadyPresent", half, { n: (replay.already_present ?? []).length }), "table",
        'data-testid="poi-import-already-present"',
      );
      expect(await page.getByTestId("poi-import-added").count(), "G / a replay leaves no success sentence on the glass").toBe(0);

      // ---------- census over the whole glass
      const screen = await page.evaluate(() => document.body.innerText);
      const census = await page.evaluate(() => {
        const re = /\b[A-Z][A-Z_]{4,}\b/g;
        const carriers: Record<string, string[]> = {};
        const walk = (el: Element) => {
          for (const node of Array.from(el.childNodes)) {
            if (node.nodeType === 3) {
              for (const m of (node.textContent ?? "").matchAll(re)) {
                let box: Element | null = el;
                let named = "";
                while (box && !named) {
                  named = box.getAttribute?.("data-testid") ?? "";
                  box = box.parentElement;
                }
                (carriers[m[0]] ??= []).push(named || el.tagName.toLowerCase());
              }
            } else if (node.nodeType === 1) {
              walk(node as Element);
            }
          }
        };
        walk(document.body);
        return Object.fromEntries(
          Object.entries(carriers).map(([word, boxes]) => [word, [...new Set(boxes)].sort()]),
        );
      });

      writeEvidence(`walkthrough_${locale}`, {
        trip: TRIP,
        cells_compared: rows.length,
        cells_equal: rows.filter((r) => r.equal).length,
        cells_by_source: {
          table: rows.filter((r) => r.source === "table").length,
          dictionary: rows.filter((r) => r.source === "dictionary").length,
        },
        distinct_keys_reached: [...seen].sort(),
        rows,
        wire,
        screen_characters: screen.length,
        poi_import_key_names_on_screen: (screen.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? []).sort(),
        table_key_names_on_screen: [...new Set(screen.match(/\b(?:err|card|exit|landing|channel|select|added|already|mode|phase|verify|store|empty|retry|parse|counts|intent|lede|kicker)[A-Za-z]+\b/g) ?? [])].sort(),
        unfilled_placeholders_on_screen: (screen.match(/\{[a-z_]+\}/g) ?? []).sort(),
        uuids_on_screen: (screen.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g) ?? []).sort(),
        machine_words_by_dom_text_nodes: Object.keys(census).sort(),
        machine_words_by_rendered_text: [...new Set(screen.match(/\b[A-Z][A-Z_]{4,}\b/g) ?? [])].sort(),
        machine_word_carriers: census,
      });

      expect(rows.filter((r) => !r.equal).map((r) => `${r.state}/${r.cell}`), "every cell must equal its sentence").toEqual([]);
      expect(rows.filter((r) => r.source === "table").length, "the table must really be what paints these cells").toBeGreaterThanOrEqual(25);
    });
  }
});
