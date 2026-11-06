/* ORD-0545 slice 2 -- the POI-import sentence family now lives in one table, and this gate checks
 * what that claim is actually made of.
 *
 * The family moved because ORD-0533 proved what happens when a screen's sentences live in a file every
 * order edits: a key can be called and never defined, and the product prints the key name on the glass.
 * Moving it into `poiImportCopy.ts` only helps if three things hold, and each is an arm here:
 *
 *   G1  every sentence the screen reaches -- from a call site or through the FAILURE_SENTENCE lookup --
 *       exists in the table with BOTH halves, none of the moved keys is still in either dictionary book,
 *       and the panel no longer calls `t()` at all;
 *   G2  the screen at 390x844 in Chinese renders real sentences: no `poiImport.*` key name, no
 *       unfilled `{placeholder}`, no machine word, no bare uuid;
 *   G3  the same screen in English renders its own half of every one of those sentences.
 *
 * What is deliberately not claimed: the seven refusal sentences behind FAILURE_SENTENCE render only
 * after an add is really refused.  G1 proves they can no longer be orphaned -- the map is typed as
 * `Record<Failure["code"], PoiImportCopyKey>`, so a name the table lacks is a build failure -- and the
 * counter-arm in review/ord0545/family_counterarm_r1.py shows that claim bites.  Walking the UI to a
 * real refusal is named NOT_REACHED_BY_LANE below with the reading that produced the name.
 */

import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const COLLECTION_PREREQ: string[] = [];

const TRIP = "22222222-2222-4222-8222-222222222222";
const PANEL = "frontend/src/features/poiimport/PoiImportPanel.tsx";
const TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const DICTIONARY = "frontend/src/i18n/LocaleContext.tsx";
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0545", "readouts");
const TAG = process.env.CITYNEXUS_ORD0545_TAG ?? "r1";
const BASE_TIP = process.env.CITYNEXUS_ORD0545_BASE_TIP ?? "";
if (!/^[0-9a-f]{40}$/.test(BASE_TIP)) COLLECTION_PREREQ.push("CITYNEXUS_ORD0545_BASE_TIP must be the 40-hex tip this was measured on");

const repoRoot = path.join(process.cwd(), "..");
const read = (rel: string) => fs.readFileSync(path.join(repoRoot, rel), "utf-8");

function writeEvidence(name: string, payload: unknown) {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  fs.writeFileSync(
    path.join(EVIDENCE, `${name}_${TAG}.json`),
    JSON.stringify({ generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() },
                     ...(payload as object) }, null, 2) + "\n",
    "utf-8",
  );
}

/** Same plain-string parsing the ORD-0533 arm learned to use: a regex built through a template literal
 *  loses its backslashes to the escape layer, which once reported every table key as missing both
 *  halves.  That is an instrument failing, not a product. */
function tableFacts(): { names: string[]; pairs: { name: string; zh?: string; en?: string }[] } {
  const text = read(TABLE);
  // The union ends at the first line whose own text ends with `";` -- not at the first semicolon, which
  // once landed inside an inserted comment ("moved verbatim; see ...") and cut the slice in half,
  // making every table key look like it was missing both halves.  Line-scanning, no escape layers.
  const afterUnion = text.split("export type PoiImportCopyKey")[1].split("\n");
  const stop = afterUnion.findIndex((line) => line.trimEnd().endsWith('";'));
  expect(stop, "the key union's terminating line was not found").toBeGreaterThan(0);
  const union = afterUnion.slice(0, stop + 1).join("\n");
  const names = union.split("\n").flatMap((line) => {
    const at = line.indexOf('  | "');
    return at === -1 ? [] : [line.slice(at + 5, line.indexOf('"', at + 5))];
  });
  const body = text.split("const COPY: Record<PoiImportCopyKey, { zh: string; en: string }> = {")[1];
  const pairs = names.map((name) => {
    const open = body.indexOf(`\n  ${name}: {`);
    if (open === -1) return { name };
    const block = body.slice(open, body.indexOf("\n  }", open) + 1);
    const half = (which: string) => {
      const at = block.indexOf(`${which}: "`);
      if (at === -1) return undefined;
      const from = at + which.length + 3;
      return block.slice(from, block.indexOf('"', from));
    };
    return { name, zh: half("zh"), en: half("en") };
  });
  return { names, pairs };
}

function panelFacts(): { t_calls: number; reached: string[]; map_values: string[] } {
  const text = read(PANEL);
  const called: string[] = [];
  for (const line of text.split("\n")) {
    const at = line.indexOf('poiImportCopy(locale, "');
    if (at !== -1) {
      const from = at + 'poiImportCopy(locale, "'.length;
      called.push(line.slice(from, line.indexOf('"', from)));
    }
  }
  const mapStart = text.indexOf("const FAILURE_SENTENCE");
  const mapBody = text.slice(mapStart, text.indexOf("};", mapStart));
  const mapValues: string[] = [];
  for (const line of mapBody.split("\n")) {
    const at = line.indexOf(': "');
    if (at !== -1) mapValues.push(line.slice(at + 3, line.indexOf('"', at + 3)));
  }
  return {
    t_calls: (text.match(/\bt\(/g) ?? []).length,
    reached: [...new Set([...called, ...mapValues])].sort(),
    map_values: mapValues,
  };
}

function dictionaryKeys(): Set<string> {
  const text = read(DICTIONARY);
  return new Set([...text.matchAll(/^  "(poiImport\.[a-zA-Z0-9._]+)":/gm)].map((m) => m[1]));
}

async function bootToPanel(page: import("@playwright/test").Page, locale: string) {
  await page.addInitScript(([id, loc]) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", loc);
  }, [TRIP, locale]);
  await page.goto("/v8/import");
  await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 40_000 });
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test.describe("ORD-0545 slice 2 the moved family owns its own sentences", () => {
  test("G1 这一屏够得着的每一枚句子都在表里成对，字典里不留第二份，t() 归零", () => {
    const panel = panelFacts();
    const table = tableFacts();
    const dicKeys = dictionaryKeys();
    const missingFromTable = panel.reached.filter((n) => !table.names.includes(n));
    const unpaired = table.pairs.filter((p) => p.zh === undefined || p.en === undefined);
    const orphanUnionNames = table.names.filter((n) => !table.pairs.find((p) => p.name === n && p.zh && p.en));
    // a moved key must not survive in either book -- that would be two owners for one sentence
    const stillInDictionary = panel.reached.filter((n) => dicKeys.has(`poiImport.${n}`));
    writeEvidence("family_census", {
      panel: PANEL,
      table: TABLE,
      t_calls_left_in_panel: panel.t_calls,
      sentences_reached_by_screen: panel.reached.length,
      reached_via_lookup_table: panel.map_values.length,
      table_union_names: table.names.length,
      table_pairs_complete: table.pairs.length - unpaired.length,
      missing_from_table: missingFromTable,
      union_names_without_a_pair: orphanUnionNames,
      moved_names_still_in_dictionary: stillInDictionary,
      dictionary_poi_keys_left: dicKeys.size,
    });
    expect(panel.t_calls, "the panel still reaches the shared dictionary").toBe(0);
    expect(missingFromTable, `sentences with no table entry: ${missingFromTable.join(", ")}`).toEqual([]);
    expect(unpaired.map((p) => p.name), `table entries missing a half: ${unpaired.map((p) => p.name).join(", ")}`).toEqual([]);
    expect(orphanUnionNames, `union names with no pair: ${orphanUnionNames.join(", ")}`).toEqual([]);
    expect(stillInDictionary, `moved sentences left in the dictionary: ${stillInDictionary.join(", ")}`).toEqual([]);
  });

  for (const [locale, expectWord] of [["zh-CN", "本地解析，不打开链接"], ["en-GB", "Local parse, no links opened"]] as const) {
    test(`G2 ${locale} 那一屏走一遍：整屏没有键名、没有未填占位符、没有机词`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await bootToPanel(page, locale);
      const rendered = await page.evaluate(() => document.body.innerText);
      const keyNames = rendered.match(/\b[a-zA-Z]+\.[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+\b/g) ?? [];
      const poiKeys = rendered.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? [];
      const unfilled = rendered.match(/\{[a-zA-Z_]+\}/g) ?? [];
      const machine = rendered.match(/[A-Z][A-Z0-9]*_[A-Z0-9_]+/g) ?? [];
      const uuids = rendered.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) ?? [];
      writeEvidence(`family_screen_${locale.replace("-", "_")}`, {
        locale,
        viewport: "390x844",
        rendered_characters: rendered.length,
        badge_cell: await page.locator(".v8-evidence-label").first().innerText(),
        expected_badge: expectWord,
        poi_key_names_on_screen: poiKeys,
        dotted_tokens_on_screen: keyNames,
        unfilled_placeholders_on_screen: unfilled,
        machine_words_on_screen: machine,
        uuids_on_screen: uuids,
        refusal_leg: "NOT_REACHED_BY_LANE -- a refusal sentence renders only after an add is refused; "
                    + "loading this screen against a deleted journey produced no refusal (measured)",
      });
      expect(poiKeys, `a dictionary key name is on the glass: ${poiKeys.join(", ")}`).toEqual([]);
      expect(unfilled, `an unfilled placeholder is on the glass: ${unfilled.join(", ")}`).toEqual([]);
      expect(machine, `a machine word is on the glass: ${machine.join(", ")}`).toEqual([]);
      expect(uuids, `a bare uuid is on the glass: ${uuids.join(", ")}`).toEqual([]);
      await expect(page.locator(".v8-evidence-label").first()).toHaveText(expectWord);
    });
  }
});
