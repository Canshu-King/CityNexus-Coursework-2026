/* ORD-0533 · 字典键名直接上了屏 —— the gate that a traveller never reads a key.
 *
 * The reported defect: on the day9 generation the C2 cell of the phone walkthrough rendered the
 * literal text `poiImport.channelLocal`, because `PoiImportPanel.tsx` calls that key and no
 * dictionary in the tree defines it.  The mechanism is the fallback inside `t()` --
 * `dictionaries.en[key] ?? key` in `frontend/src/i18n/LocaleContext.tsx` -- so an undefined key does
 * not throw, it prints.  No line number is quoted for it: this file is where lines move most, and a
 * citation that rots is worse than none.  The split below is taken from the declarations themselves
 * for the same reason.
 *
 * Two arms, one for each half of what a one-line fix usually misses:
 *
 * B1 is a static arm over the whole screen.  Every key that file passes to `t()` must exist in BOTH
 *    books.  A zh-only insert is the same defect flipped: an English traveller reads the key.  This
 *    is what "逐枚核一遍" means as an executable claim rather than as a promise -- the denominator is
 *    counted from the file on every run, because a sibling order (ORD-0524) moved part of this
 *    screen's vocabulary into `poiImportCopy.ts` and a hand-copied count would have gone stale with it.
 * B2 is the screen itself at 390x844, reading `document.body.innerText`.  Rendered text, not an
 *    attribute: a `data-testid` or a title attribute can hold a key name all day without any
 *    traveller ever seeing it, and the reverse -- words painted by CSS content -- is invisible to an
 *    attribute assertion.
 *
 * The counter-arm lives in `review/ord0533/ord0533_counterarms_r1.py`: it deletes the two lines this
 * order added and requires B2 to go red WITH THE KEY NAME IN THE FAILURE, then restores the file and
 * re-runs green.  Without that, B2 could be green because it never looked at the thing it claims.
 */

import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const TRIP = "22222222-2222-4222-8222-222222222222";
const PANEL = "frontend/src/features/poiimport/PoiImportPanel.tsx";
const DICTIONARY = "frontend/src/i18n/LocaleContext.tsx";
const COPY_TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0533", "readouts");
const TAG = process.env.CITYNEXUS_ORD0533_TAG ?? "r3";
/* Required, not defaulted to a placeholder.  A sibling lane's readings shipped stamped
 * base_tip="unknown", which leaves whoever lands them having to infer the generation from a console
 * log -- so this file refuses to run rather than write a reading with no provenance. */
const BASE_TIP = process.env.CITYNEXUS_ORD0533_BASE_TIP ?? "";
/* ORD-0576: this refused to run without a provenance stamp, which is right, but refusing *at module scope*
 * aborted Playwright's collection for every spec in the directory -- one unset variable silenced the whole
 * gate. The refusal is unchanged in force, it just happens inside this file's tests now, and the reason
 * names the variable. Nothing is defaulted and no assertion was softened. */
const BASE_TIP_PREREQ = /^[0-9a-f]{40}$/.test(BASE_TIP)
  ? null
  : "missing environment variable: CITYNEXUS_ORD0533_BASE_TIP must be the 40-hex tip this candidate was cut from";

/** The words this order added.  zh first, because the reported screen was Chinese. */
const CHANNEL_LOCAL_ZH = "本地解析，不打开链接";
const CHANNEL_LOCAL_EN = "Local parse, no links opened";

const repoRoot = path.join(process.cwd(), "..");
fs.mkdirSync(EVIDENCE, { recursive: true });

function writeEvidence(name: string, payload: unknown) {
  fs.writeFileSync(
    path.join(EVIDENCE, `${name}_${TAG}.json`),
    JSON.stringify(
      { generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() }, ...payload },
      null,
      2,
    ),
    "utf-8",
  );
}

function keysCalledBy(file: string): string[] {
  const text = fs.readFileSync(path.join(repoRoot, file), "utf-8");
  return [...text.matchAll(/\bt\(\s*"([a-zA-Z0-9._]+)"/g)].map((match) => match[1]).sort();
}

/**
 * The two books, split by their own declarations rather than by a line count.
 *
 * `dictionaries` is `{ en, "zh-CN": zhCN }`, so the boundary this arm needs is where `zhCN` starts
 * being declared.  A line number would be wrong the moment anyone adds a word above it; a marker
 * taken from the source is wrong only when the structure itself changes, and then it says so.
 */
function books(): { en: Set<string>; zh: Set<string> } {
  const text = fs.readFileSync(path.join(repoRoot, DICTIONARY), "utf-8");
  const marker = "\nconst zhCN";
  const split = text.indexOf(marker);
  expect(split, "the zhCN book is no longer where this arm reads it").toBeGreaterThan(0);
  const entries = (chunk: string) =>
    new Set([...chunk.matchAll(/^\s{2}"([a-zA-Z0-9._]+)":/gm)].map((match) => match[1]));
  return { en: entries(text.slice(0, split)), zh: entries(text.slice(split)) };
}

/**
 * The screen's own sentence table (ORD-0524's `poiImportCopy.ts`), read as key + both halves.
 *
 * Parsing is done by plain string search rather than by a regex built inside a template literal: the
 * first cut did the latter, and the escape layer turned `[\s\S]` into the character class `[sS]`, so
 * every one of the table's keys reported as missing both halves.  That was my instrument failing, not
 * the product -- and a green/red that comes from the tool's own typo is worse than no tool.
 *
 * What this arm is for: `Record<PoiImportCopyKey, { zh, en }>` already refuses a missing half at
 * compile time, so the two things worth checking are what the compiler cannot see -- that the union has
 * no name without an entry, and that a sentence moved here did not stay behind in the dictionary as a
 * second owner of the same words.
 */
function copyTable(): {
  keys: string[];
  entries: { key: string; zh?: string; en?: string }[];
  keysBySecondMethod: string[];
} {
  const text = fs.readFileSync(path.join(repoRoot, COPY_TABLE), "utf-8");
  // The first cut of this parser took `split("export type PoiImportCopyKey")[1].split(";")[0]`.  That
  // `;` was eaten by a semicolon *inside* a comment in the union (ORD-0545's own "moved verbatim; see"
  // note), so the segment stopped at entry 15 while the file held 67 -- an arm that counted 15 pairs,
  // reported "0 missing halves" and stayed green on a table it was not reading.  The union now ends
  // where a line ends with `";`, and that closing line's own name is taken too: an earlier revision of
  // the fix broke before extracting it and read 66, one short of the truth.  The second method is an
  // independent count over the same segment, so a future shape change makes this arm red instead of
  // quietly under-reading it.
  const unionStart = text.indexOf("export type PoiImportCopyKey");
  const after = text.slice(unionStart);
  const keys: string[] = [];
  for (const line of after.split("\n")) {
    const at = line.indexOf('  | "');
    if (at !== -1) keys.push(line.slice(at + 5, line.indexOf('"', at + 5)));
    if (line.trimEnd().endsWith('";')) break;
  }
  const keysBySecondMethod = [
    // Independent denominator: everything between the union's own name and the COPY declaration, which
    // is where the union provably ends.  If the line scan above stops early -- at a comment's semicolon
    // (the 15 it read) or one entry before the closing line (the 66) -- this count stays higher and
    // B4 goes red instead of reporting a clean table it never finished reading.
    ...text.slice(unionStart, text.indexOf("const COPY: Record<PoiImportCopyKey")).matchAll(/\|\s*"([A-Za-z0-9]+)"/g),
  ].map((m) => m[1]);
  const body = text.split("const COPY: Record<PoiImportCopyKey, { zh: string; en: string }> = {")[1];
  const half = (block: string, name: string) => {
    const at = block.indexOf(`${name}: "`);
    if (at === -1) return undefined;
    const from = at + name.length + 3;
    const end = block.indexOf('"', from);
    return end === -1 ? undefined : block.slice(from, end);
  };
  const entries = keys.map((key) => {
    const open = body.indexOf(`\n  ${key}: {`);
    if (open === -1) return { key };
    const close = body.indexOf("\n  }", open);
    const block = body.slice(open + `\n  ${key}: {`.length, close === -1 ? undefined : close);
    return { key, zh: half(block, "zh"), en: half(block, "en") };
  });
  return { keys, entries, keysBySecondMethod };
}

async function bootToPanel(page: Page) {
  // `/v8/import` is an address the shell itself recognises: `import` is a route key in
  // `frontend/src/routes/v8Routes.ts`, and `KNOWN_ROUTES` is built from those keys, so this is the
  // product's own deep link and not an invented one.
  await page.addInitScript(
    ([id]) => {
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", "zh-CN");
    },
    [TRIP],
  );
  await page.goto("/v8/import");
  await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 40_000 });
}

test.describe("ORD-0533 dictionary keys must never reach the screen", () => {
  test.beforeEach(() => {
    test.skip(BASE_TIP_PREREQ !== null, BASE_TIP_PREREQ ?? undefined);
  });

  test("B1 这一屏每一枚 t() 键在两本字典里都有定义", () => {
    const called = keysCalledBy(PANEL);
    const distinct = [...new Set(called)];
    const { en, zh } = books();
    const missingEn = called.filter((key) => !en.has(key));
    const missingZh = called.filter((key) => !zh.has(key));
    /* The first cut of this arm printed one number under the name `distinct_keys_called` while the
       list behind it was call sites, not keys -- so the delivery block said "this screen uses N keys"
       when the honest reading was "N call sites over M keys".  Both are recorded now, each named for
       what it counts, because "逐枚核一遍" is about the call sites while "缺哪枚" is about the keys. */
    writeEvidence("b1_key_census", {
      panel: PANEL,
      t_call_sites: called.length,
      distinct_keys: distinct.length,
      distinct_defined_in_en: distinct.filter((key) => en.has(key)).length,
      distinct_defined_in_zh: distinct.filter((key) => zh.has(key)).length,
      missing_from_en: [...new Set(missingEn)],
      missing_from_zh: [...new Set(missingZh)],
      /* ORD-0545 moved this badge's words out of the two books and into the screen's own table, so the
         r3 claim "the reported key is now defined in both books" is no longer the right invariant -- it
         would have failed on a green product.  The owner is asserted in B4 instead; here it is recorded
         as absent, which is what proves the move left no second copy behind. */
      reported_key_still_called_via_t: called.includes("poiImport.channelLocal"),
      reported_key_still_in_dictionary: [en.has("poiImport.channelLocal"), zh.has("poiImport.channelLocal")],
    });
    expect(missingEn, `keys missing from the English book: ${missingEn.join(", ")}`).toEqual([]);
    expect(missingZh, `keys missing from the Chinese book: ${missingZh.join(", ")}`).toEqual([]);
    expect(called).not.toContain("poiImport.channelLocal");
    expect(en.has("poiImport.channelLocal"), "the dictionary still holds a copy of a moved sentence").toBe(false);
    expect(zh.has("poiImport.channelLocal"), "the dictionary still holds a copy of a moved sentence").toBe(false);
  });

  test("B2 390 宽那一档读渲染文字：新词上屏，键名不上屏", async ({ page }) => {
    await bootToPanel(page);
    const rendered = await page.evaluate(() => document.body.innerText);
    const label = page.locator(".v8-evidence-label");
    await expect(label).toHaveText(CHANNEL_LOCAL_ZH);
    // Nothing else the screen paints may look like a key of this family.
    const keyLike = rendered.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? [];
    const machine = rendered.match(/[A-Z][A-Z0-9]*_[A-Z0-9_]+/g) ?? [];
    const uuids = rendered.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) ?? [];
    writeEvidence("b2_screen_reading", {
      viewport: `${page.viewportSize()?.width}x${page.viewportSize()?.height}`,
      rendered_characters: rendered.length,
      evidence_label_text: await label.innerText(),
      expected_zh_copy: CHANNEL_LOCAL_ZH,
      expected_en_copy: CHANNEL_LOCAL_EN,
      poi_import_key_names_on_screen: keyLike,
      machine_words_on_screen: machine,
      uuids_on_screen: uuids,
    });
    expect(keyLike, `a dictionary key name is on screen: ${keyLike.join(", ")}`).toEqual([]);
    expect(machine, `a machine word is on screen: ${machine.join(", ")}`).toEqual([]);
    expect(uuids, `a bare uuid is on screen: ${uuids.join(", ")}`).toEqual([]);
    expect(rendered).toContain(CHANNEL_LOCAL_ZH);
  });

  test("B3 英文那一侧也要给同样的词，缺哪侧就会露出键名或空串", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.addInitScript(
      ([id]) => {
        localStorage.setItem("citynexus.v8.journey_id", id);
        localStorage.setItem("citynexus.locale", "en-GB");
      },
      [TRIP],
    );
    await page.goto("/v8/import");
    await expect(page.getByTestId("poi-import")).toBeVisible();
    const rendered = await page.evaluate(() => document.body.innerText);
    await expect(page.locator(".v8-evidence-label")).toHaveText(CHANNEL_LOCAL_EN);
    expect(rendered.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? []).toEqual([]);
    writeEvidence("b3_english_book_reading", {
      locale: "en-GB",
      evidence_label_text: await page.locator(".v8-evidence-label").innerText(),
      poi_import_key_names_on_screen: rendered.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? [],
    });
    await context.close();
  });

  test("B4 ORD-0545：表里每一枚句子都成对，且字典里没有第二份（含搬进来的整族）", () => {
    const { keys, entries, keysBySecondMethod } = copyTable();
    const { en, zh } = books();
    const withoutPair = entries.filter((entry) => entry.zh === undefined || entry.en === undefined);
    // A table key that is also in either book is the "fourth way of saying the same thing" ORD-0524's
    // own header forbids; after a move it is the residue that makes two owners of one sentence.
    const alsoInDictionary = keys.filter((key) => en.has(`poiImport.${key}`) || zh.has(`poiImport.${key}`));
    const badge = entries.find((entry) => entry.key === "channelLocal");
    writeEvidence("b4_copy_table_pairing", {
      table: COPY_TABLE,
      union_keys: keys.length,
      union_keys_by_second_method: keysBySecondMethod.length,
      entries_with_both_halves: entries.length - withoutPair.length,
      entries_missing_a_half: withoutPair.map((entry) => entry.key),
      table_keys_also_in_dictionary: alsoInDictionary,
      badge_zh: badge?.zh,
      badge_en: badge?.en,
      badge_matches_shipped_words: [badge?.zh === CHANNEL_LOCAL_ZH, badge?.en === CHANNEL_LOCAL_EN],
    });
    // The arm's own reader is part of what it proves: a truncated scan can only ever find pairs.
    expect(keys, "the line parser and the region regex disagree about the union").toEqual(keysBySecondMethod);
    expect(withoutPair.map((entry) => entry.key), `halves missing: ${withoutPair.map((e) => e.key).join(", ")}`).toEqual([]);
    expect(alsoInDictionary, `moved sentences left a copy in the dictionary: ${alsoInDictionary.join(", ")}`).toEqual([]);
    expect(badge?.zh, "the badge's Chinese half is not the sentence ORD-0533 shipped").toBe(CHANNEL_LOCAL_ZH);
    expect(badge?.en, "the badge's English half is not the sentence ORD-0533 shipped").toBe(CHANNEL_LOCAL_EN);
  });
});
