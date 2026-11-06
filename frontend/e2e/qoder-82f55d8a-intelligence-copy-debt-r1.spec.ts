import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/* ORD-0567 second batch: a named-debt ruler for the four English sentences on /v8/intelligence that I reported
 * as 待裁 and the commander has not ruled on yet.
 *
 * It does not decide them. It freezes what each one is now -- the exact string, the machine word or the lone
 * noun that made it suspicious, and the reason it is suspicious -- so that:
 *   * if someone changes the string, the row stops being true and this goes red with the row's name;
 *   * if a NEW sentence of the same shape appears in that dictionary region and nobody judged it, this goes red.
 *
 * Shape follows the two rulers already landed for this lane: named debt, a non-empty-denominator arm, and the
 * obligation to delete a row when it is paid. Source-only: it reads two files, starts nothing, opens no port.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const LOCALE = join(FRONTEND, "src", "i18n", "LocaleContext.tsx");
const VOCAB = join(FRONTEND, "src", "lib", "displayVocabulary.ts");

/** Every row: the exact English string, the machine word / lone noun that made it a candidate, and why. */
const DEBT: Array<{
  file: string;
  key: string;
  en: string;
  suspect: RegExp;
  why: string;
}> = [
  {
    file: "LocaleContext.tsx",
    key: "intel.gateSub",
    en: "Hard constraints verified after mode change",
    suspect: /\bmode\b/,
    why: "\"mode\" appears once on the screen and only in this sentence; the control it names is labelled POLICY, and mode is the server field name policy.mode",
  },
  {
    file: "LocaleContext.tsx",
    key: "intel.commitmentSub",
    en: "Durable moments are not silently broken",
    suspect: /\b(durable|moments?)\b/i,
    why: "\"durable\" is this product's storage word (backend comment calls a fingerprint durable) and \"moments\" has no other reader on the screen",
  },
  {
    file: "LocaleContext.tsx",
    key: "intel.nativeMcpNotProven",
    en: "Native Hermes MCP is not claimed without a verified proof run",
    suspect: /\b(Hermes|MCP)\b/,
    why: "two runtime names in a sentence the traveller reads, and the sentence is about what the service claims rather than what it can do",
  },
  {
    file: "LocaleContext.tsx",
    key: "intel.nativeMcpProven",
    en: "Native Hermes MCP proof recorded",
    suspect: /\b(Hermes|MCP)\b/,
    why: "the sibling of the row above, found by this ruler's neighbour arm on the first run: same two runtime names in the same claim family, and I reported only the NotProven half. Whether one of the two should survive is the commander's call, not mine.",
  },
  {
    file: "displayVocabulary.ts",
    key: "runtime source (en row of the shared table)",
    en: "this service's own loop straight to the model provider",
    suspect: /\bloop\b/,
    why: "a noun phrase with no verb, and \"loop straight to\" is not an English collocation; the Chinese twin of this row is already a filed candidate",
  },
];

function read(p: string): string {
  return readFileSync(p, "utf8");
}

/** The exact English row, read the way each file actually spells it: the dictionary uses `"key": "en"`, the
 *  shared vocabulary table uses one `en: "..."` row. The first version of this arm searched for the
 *  dictionary shape in both files and went red on the vocabulary row for that reason alone. */
function enRow(src: string, key: string, en: string): boolean {
  if (key.startsWith("runtime source")) return src.includes(`en: "${en}"`);
  return src.includes(`"${key}": "${en}"`);
}

/**
 * Any OTHER English sentence in this dictionary that carries the same machine word AND sits next to one of
 * the reported keys -- an unjudged neighbour, i.e. someone added a fifth sentence of the same shape without
 * ruling on it.
 *
 * The first version scanned the whole dictionary for the word alone and cried wolf on `shell.autopilotRecovered`
 * (which carries the placeholder `{mode}`, not the word) and on a hub sentence. Scanning by proximity instead
 * of by word-alone is what makes the arm about the neighbourhood rather than about the dictionary.
 */
function neighbours(src: string, debt: typeof DEBT): string[] {
  const out: string[] = [];
  const rows = [...src.matchAll(/"([A-Za-z0-9_.]+)":\s*"((?:[^"\\]|\\.)*)"/g)];
  for (const d of debt) {
    const at = rows.findIndex((m) => m[1] === d.key);
    if (at < 0) continue;
    for (let i = Math.max(0, at - 3); i <= Math.min(rows.length - 1, at + 3); i += 1) {
      const [key, value] = [rows[i][1], rows[i][2]];
      if (key === d.key) continue;
      if (/\{[a-z_]+\}/.test(value)) continue;                 // a placeholder such as {mode} is not the word
      if (/[^\x00-\x7f]/.test(value)) continue;               // the Chinese half of the dictionary
      if (!d.suspect.test(value)) continue;
      if (debt.some((o) => o.en === value)) continue;         // one of the reported sentences
      out.push(`${key} (next to ${d.key}): ${value.slice(0, 60)}`);
    }
  }
  return [...new Set(out)];
}

test("arm 1 -- both files are present and each row has a reason (a debt table with no reasons is a smell)", () => {
  expect(read(LOCALE).length).toBeGreaterThan(1000);
  expect(read(VOCAB).length).toBeGreaterThan(100);
  expect(DEBT.length).toBeGreaterThanOrEqual(4);
  for (const d of DEBT) expect(d.why.length, `${d.key} has no reason`).toBeGreaterThan(20);
});

test("arm 2 -- every debt row is still the exact string I reported, so a change cannot pass unnoticed", () => {
  const drifted: string[] = [];
  for (const d of DEBT) {
    const src = read(d.file === "displayVocabulary.ts" ? VOCAB : LOCALE);
    if (!enRow(src, d.key, d.en)) drifted.push(`${d.key} (${d.file})`);
  }
  expect(drifted, `these rows are no longer the strings I reported -- either re-measure or delete the row: ${drifted.join(", ")}`).toEqual([]);
});

test("arm 3 -- each reported sentence still carries the word that made it a candidate", () => {
  const clean: string[] = [];
  for (const d of DEBT) {
    const src = read(d.file === "displayVocabulary.ts" ? VOCAB : LOCALE);
    if (!d.suspect.test(d.en)) clean.push(`${d.key}: the suspect word is gone from the string I reported`);
  }
  expect(clean).toEqual([]);
});

test("arm 4 -- no unjudged neighbour: a new English sentence carrying the same machine word is red until judged", () => {
  const loc = read(LOCALE);
  const found = neighbours(loc, DEBT.filter((d) => d.file === "LocaleContext.tsx"));
  expect(found, `new English sentences carrying a reported machine word, nobody has judged them: ${found.join(" | ")}`).toEqual([]);
});
