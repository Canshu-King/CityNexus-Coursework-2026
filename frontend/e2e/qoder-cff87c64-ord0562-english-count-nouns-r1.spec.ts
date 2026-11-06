/* ORD-0562 standing ruler (lane cff87c64) — a counted English noun says one or says several.
 *
 * Why: the hub's own line read `1 days` on a one-day trip while the traveller count beside it read
 * `1 traveler` correctly, because the traveller count goes through the dictionary's `{plural:...}`
 * marker and the day count was an inline template. Seven dictionary lines softened the same problem into
 * `item(s)` / `leg(s)` / `row(s)` / `refusal(s)` / `need(s)`, which `src/lib/plural.ts` names explicitly
 * as not a feature ("a screen either says one refusal or says several").
 *
 * This ruler reads three source files and decides nothing about a running product, so it needs no
 * backend, no front and no store. Not the acceptance gate: `playwright.config.ts` stays the only thing
 * that can sign one. The screen proof for `1 day` and `2 days` is a separate real-browser read, recorded
 * in review/ord0562/.
 */
import { expect, test } from "@playwright/test";
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { CityNexusApiError } from "../src/api/citynexus";
import { describeFailure } from "../src/features/simulation/simulationAdapter";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILES = {
  dictionary: join(ROOT, "frontend", "src", "i18n", "LocaleContext.tsx"),
  plural: join(ROOT, "frontend", "src", "lib", "plural.ts"),
  hub: join(ROOT, "frontend", "src", "app", "V8Experience.tsx"),
};
const EVIDENCE = process.env.CITYNEXUS_COUNT_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_COUNT_RUN ?? "count-unset";

const ENTRY = /^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$/;
const CJK = /[\u4E00-\u9FFF]/; // CJK Unified Ideographs, as escapes so no glyph has to survive an editor
const MARKER = /\{plural:([A-Za-z][A-Za-z_ ]*)(?::([A-Za-z_]+))?\}/g;

/** The three keys ORD-0562 introduced, and the parameters each one counts on. */
const COUNTED_KEYS: Record<string, string[]> = {
  "hub.todayDays": ["n"],
  "hub.budgetLine": ["days", "people"],
  "hub.todayDaysUnknown": [],
};

function read(name: keyof typeof FILES): string {
  return readFileSync(FILES[name], "utf8");
}

function dictionaryEntries() {
  const lines = read("dictionary").split("\n");
  const out: { key: string; value: string; english: boolean; line: number }[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    const m = ENTRY.exec(lines[i]);
    if (m) out.push({ key: m[1], value: m[2], english: !CJK.test(m[2]), line: i + 1 });
  }
  return out;
}

const entries = dictionaryEntries();
const englishEntries = entries.filter((e) => e.english);

test("no English line softens a count into `(s)` any more", () => {
  const offenders = englishEntries
    .filter((e) => /\(s\)/.test(e.value))
    .map((e) => `${e.key}:${e.line} = ${e.value}`);
  expect(offenders, "a counted English noun went back to the `1 or more` dodge that plural.ts rejects").toEqual([]);
  expect(englishEntries.length, "the English side parsed empty; this ruler would pass on nothing").toBeGreaterThan(400);
});

test("the counted keys carry the marker on the English side only, and the Chinese side stays plain", () => {
  for (const [key, countParams] of Object.entries(COUNTED_KEYS)) {
    const en = englishEntries.filter((e) => e.key === key);
    const zh = entries.filter((e) => e.key === key && !e.english);
    expect(en.length, `${key}: no English entry`).toBe(1);
    expect(zh.length, `${key}: the Chinese entry disappeared`).toBe(1);
    expect(zh[0].value, `${key}: the Chinese arm must not carry a plural marker`).not.toContain("{plural:");
    for (const param of countParams) {
      expect(en[0].value, `${key}: the English line no longer counts {${param}}`).toContain(`{${param}}`);
    }
  }
  const markers = englishEntries
    .filter((e) => Object.keys(COUNTED_KEYS).includes(e.key))
    .flatMap((e) => [...e.value.matchAll(MARKER)].map((m) => ({ key: e.key, stem: m[1], countKey: m[2] || "n" })));
  expect(markers.length, "the counted lines carry no marker, so nothing expands them").toBeGreaterThan(2);
  for (const marker of markers) {
    const stem = marker.stem.trim();
    expect(stem, `${marker.key}: a marker stem must be the singular noun itself`).not.toContain("(s)");
    expect(stem.split(" ").length, `${marker.key}: marker stem "${stem}" is more than a noun plus an optional qualifier`)
      .toBeLessThanOrEqual(2);
    const declared = Object.keys(COUNTED_KEYS).includes(marker.key);
    expect(declared, `${marker.key}: not one of the counted keys this arm reads`).toBe(true);
  }
});

test("the hub stopped building counted nouns inline", () => {
  const hub = read("hub");
  expect(hub, "the day count is an inline template again, which is what printed `1 days`")
    .not.toMatch(/\$\{[A-Za-z_.]+\}\s+days/);
  expect(hub, "the people count is an inline template again, which is what printed `1 people`")
    .not.toMatch(/\$\{[A-Za-z_.]+\}\s+people/);
  expect(hub, "the day count no longer asks the dictionary").toContain('t("hub.todayDays", { n: tripDayCount })');
  expect(hub, "the budget line no longer asks the dictionary").toContain('t("hub.budgetLine", { days, people, amount })');
});

test("the irregular noun the budget line counts is declared once, in plural.ts", () => {
  const plural = read("plural");
  expect(plural, "`persons` would reach the screen").toMatch(/person:\s*"people"/);
  const line = englishEntries.find((e) => e.key === "hub.budgetLine");
  expect(line, "the budget line disappeared").toBeTruthy();
  const stems = [...(line as { value: string }).value.matchAll(MARKER)].map((m) => m[1].trim());
  expect(stems, "the budget line no longer counts days and people through the marker").toEqual(["day", "person"]);
});

test("the ruler read what it claims to have read", () => {
  if (EVIDENCE) {
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, JSON.stringify({
      generatedBy: "cff87c64",
      ruler: "ord0562-english-count-nouns-r1",
      run: RUN,
      dictionary_lines: read("dictionary").split("\n").length,
      entries_parsed: entries.length,
      english_entries: englishEntries.length,
      softened_still_present: englishEntries.filter((e) => /\(s\)/.test(e.value)).length,
      counted_keys: COUNTED_KEYS,
      counted_key_values: Object.fromEntries(englishEntries
        .filter((e) => Object.keys(COUNTED_KEYS).includes(e.key))
        .map((e) => [e.key, e.value])),
      chinese_counterparts: Object.fromEntries(entries
        .filter((e) => !e.english && Object.keys(COUNTED_KEYS).includes(e.key))
        .map((e) => [e.key, e.value])),
    }, null, 1), "utf8");
  }
  expect(entries.length, "the parse is empty").toBeGreaterThan(1000);
});

/* The hub was only the first screen. This arm walks every .ts/.tsx under frontend/src so a counted
   English noun cannot soften itself into `(s)` on some other screen while the ruler only guards three
   files. The decision is per occurrence, not per line: a line can hold both an arrow parameter named `s`
   and a real screen string, and a per-line test produced a false positive on the first run. Comment lines
   are skipped, because plural.ts's own header names the dodge it rejects. */
function sources(dir: string, into: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) sources(full, into);
    else if (/\.(ts|tsx)$/.test(entry)) into.push(full);
  }
  return into;
}

function softenedInStrings(line: string): number[] {
  const found: number[] = [];
  let quote = "";
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      const rest = line.slice(i);
      if (ch === "(" && /^\(s\)/.test(rest) && !/^\(s\)\s*=>/.test(rest)) found.push(i);
      if (ch === quote && line[i - 1] !== "\\") quote = "";
      continue;
    }
    if (ch === "`" || ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch !== "(") continue;
    const rest = line.slice(i);
    if (!/^\(s\)/.test(rest)) continue;
    if (/^\(s\)\s*=>/.test(rest)) continue;
    found.push(i);
  }
  return found;
}

test("no screen under frontend/src softens a counted English noun into `(s)`", () => {
  const files = sources(join(ROOT, "frontend", "src"));
  const hits: string[] = [];
  const posix = (p: string) => p.split("\\").join("/");
  for (const file of files) {
    const relative = posix(file).slice(posix(ROOT).length + 1);
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      const text = line.trim();
      if (text.startsWith("*") || text.startsWith("//") || text.startsWith("/*")) return;
      if (!softenedInStrings(text).length) return;
      hits.push(relative + ":" + (index + 1) + "  " + text.slice(0, 100));
    });
  }
  expect(hits, "a counted English noun on some screen went back to the `1 or more` dodge").toEqual([]);
  expect(files.length, "the walk found almost no sources, so this arm would pass on nothing").toBeGreaterThan(100);
});

test("the mobility refusal agrees with its own leg count", () => {
  /* Behavioural, not source-level: build the server error the mobility branch matches and read the
     sentence it composes. The old shape was `these${legsEn} have`, so one leg read "these 1 leg have
     facts" and no leg read "these have facts". */
  const mobility = (gaps: unknown[]) =>
    new CityNexusApiError(
      422,
      { error: { message: "MOBILITY_DATA_UNAVAILABLE", code: "MOBILITY_DATA_UNAVAILABLE", details: { gaps } } },
      "mobility",
    );
  const one = describeFailure(mobility([{ evidence_state: "STALE" }])).en;
  const two = describeFailure(mobility([{ evidence_state: "STALE" }, { evidence_state: "MISSING" }])).en;
  const none = describeFailure(mobility([])).en;
  expect(one, "one leg must read 'this 1 leg has'").toContain("this 1 leg has");
  expect(two, "two legs must read 'these 2 legs have'").toContain("these 2 legs have");
  expect(none, "no legs must not claim a leg count").not.toMatch(/these\s+\d+\s+leg|these\s+have/);
  for (const sentence of [one, two, none]) {
    expect(sentence, "the broken shape survived").not.toContain("these 1 leg have");
    expect(sentence, "the sentence must not end a clause with a bare 'these'").not.toMatch(/these\s+facts/);
  }
});
