/* Runtime-identity wording — the same ruler shape lane 82f55d8a shipped for the hub quality slot,
 * pointed at the family the hub ruler does not cover: `RUNTIME_IDENTITY`, whose table carries one of
 * the four names the backend can write, so the other three travel to the screen through the
 * labelled-raw channel and print the server's own token (`这台服务器写的：NATIVE_DEEPSEEK_TOOL_LOOP`,
 * `as this server wrote it: ...`).
 *
 * Like that ruler, this one reads source files and nothing else: no front, no back, no database, no
 * seed. It is runnable on a bare checkout, and a red here always means the vocabulary and the emitters
 * disagree. The names are taken from the region that defines them, so adding a fifth identity without
 * wording it turns this arm red by itself.
 *
 * Two things it deliberately cannot do, so nobody mistakes a green for coverage: it does not prove the
 * runtime card renders (that needs a backend answering `/api/v1/agent/runtime-status` — see the screen
 * arms), and it does not prove a label is a *good* sentence, only that it is not a machine word.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { expect, test } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILES = {
  identities: join(ROOT, "backend", "app", "agent", "runtime_identity.py"),
  vocabulary: join(ROOT, "frontend", "src", "lib", "displayVocabulary.ts"),
  panel: join(ROOT, "frontend", "src", "features", "intelligence", "ControlTowerPanel.tsx"),
  i18n: join(ROOT, "frontend", "src", "i18n", "LocaleContext.tsx"),
};

const BACKEND_PREREQ = existsSync(FILES.identities)
  ? null
  : `missing file: ${FILES.identities} -- this checkout has no backend/ beside frontend/, so the arms that read the server's own names cannot run here`;
const EVIDENCE = process.env.CITYNEXUS_RUNTIMEQ_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_RUNTIMEQ_RUN ?? "runtimeq-unset";

const NAME = String.raw`[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+`;
const MACHINE_WORD = new RegExp(NAME);
/* The sweep that found the hub leak also flags a bare run of four capitals, so a label like
   "MCP-backed" would slip past UPPER_SNAKE alone and still be machine speech on the screen. */
const ALLCAPS_WORD = /\b[A-Z]{4,}\b/;
const DECLARATION = "const RUNTIME_IDENTITY: Record<string, Label> = {";
const REGION_START = '"""Non-secret identity for the agent runtime exposed by CityNexus."""';
const REGION_END = "def runtime_identity()";

function read(name: keyof typeof FILES): string {
  return readFileSync(FILES[name], "utf8");
}

/** The identity names the backend can write, from the region that defines them. */
function emittedIdentities(): string[] {
  const src = read("identities");
  const from = src.indexOf(REGION_START);
  if (from < 0) {
    throw new Error(
      `runtime_identity.py: the "${REGION_START}" marker moved or was renamed. This ruler is bound to that ` +
        `region on purpose; a rename must re-anchor it, not silently empty it.`,
    );
  }
  const to = src.indexOf(REGION_END, from);
  if (to < 0) {
    throw new Error("runtime_identity.py: runtime_identity() is gone, so the region has no end to parse.");
  }
  const names: string[] = [];
  for (const line of src.slice(from, to).split(/\r?\n/)) {
    const m = new RegExp(String.raw`^(${NAME})\s*=\s*"${m1()}"$`).exec(line.trim());
    if (m) names.push(m[1]);
  }
  if (names.length === 0) {
    throw new Error('runtime_identity.py: no NAME = "NAME" identity constant parsed — a shape change blinds this ruler.');
  }
  return names;
}

/** A backreference has to be written after the name group exists; kept in one place so the regex stays honest. */
function m1(): string {
  return String.raw`\1`;
}

/** The labels the screen owns for those names. */
function labelledIdentities(): Record<string, { zh: string; en: string }> {
  const src = read("vocabulary");
  const at = src.indexOf(DECLARATION);
  if (at < 0) {
    throw new Error(`displayVocabulary.ts: the "${DECLARATION}" declaration was renamed. This ruler is bound to that name.`);
  }
  const end = src.indexOf("\n};", at);
  if (end < 0) throw new Error("displayVocabulary.ts: the identity table has no closing brace where this ruler expects one.");
  const body = src.slice(at, end);
  const out: Record<string, { zh: string; en: string }> = {};
  const row = new RegExp(String.raw`^\s*(${NAME}|\w+)\s*:\s*\{\s*zh:\s*"([^"]*)"\s*,\s*en:\s*"([^"]*)"\s*,?\s*\},?\s*$`, "gm");
  for (const m of body.matchAll(row)) out[m[1]] = { zh: m[2], en: m[3] };
  if (Object.keys(out).length === 0) {
    throw new Error("displayVocabulary.ts: the identity table parsed empty — this ruler would otherwise pass on nothing.");
  }
  return out;
}

function writeReading(payload: Record<string, unknown>): void {
  if (!EVIDENCE) return;
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(payload, null, 1), "utf8");
}

const emitted = BACKEND_PREREQ
  ? ({} as ReturnType<typeof emittedIdentities>)  // never used: the arms skip above
  : emittedIdentities();
const labelled = BACKEND_PREREQ
  ? ({} as ReturnType<typeof labelledIdentities>)  // never used: the arms skip above
  : labelledIdentities();

test.beforeEach(() => {
  test.skip(BACKEND_PREREQ !== null, BACKEND_PREREQ ?? undefined);
});

test("the names the server can write and the names the screen can say are one set", () => {
  expect(emitted.length, "a single identity name would not be worth a ruler").toBeGreaterThan(1);
  expect(
    Object.keys(labelled).sort(),
    "the screen carries an identity word the server cannot write, or dropped one it can — either way the card falls to the labelled-raw channel",
  ).toEqual([...emitted].sort());
});

test("every identity is worded in both languages, and no wording is a machine token", () => {
  const offenders: string[] = [];
  for (const [name, label] of Object.entries(labelled)) {
    if (!label.zh.trim() || !label.en.trim()) offenders.push(`${name}: an empty side`);
    for (const side of ["zh", "en"] as const) {
      const snake = MACHINE_WORD.exec(label[side]);
      if (snake) offenders.push(`${name}.${side} paints "${snake[0]}"`);
      const caps = ALLCAPS_WORD.exec(label[side]);
      if (caps) offenders.push(`${name}.${side} paints the all-caps word "${caps[0]}"`);
    }
  }
  expect(offenders, "an identity label that still carries a server enum is the exact defect this ruler exists for").toEqual([]);
  writeReading({
    generatedBy: "cmdra",
    ruler: "runtime-identity-vocabulary-r1",
    run: RUN,
    emitted_identities: emitted,
    labelled_identities: labelled,
    panel_imports_labelled: read("panel").includes("runtimeIdentityLabel"),
    file_bytes: Object.fromEntries(
      (Object.keys(FILES) as (keyof typeof FILES)[]).map((k) => [k, read(k).length]),
    ),
  });
});

test("the intelligence panel asks the vocabulary for the identity it prints", () => {
  const panel = read("panel");
  expect(panel, "the panel stopped importing the vocabulary channel").toContain("runtimeIdentityLabel");
  expect(
    panel.replace(/\s+/g, " "),
    "the runtime card prints the server's own identity value — this is the line that painted NATIVE_DEEPSEEK_TOOL_LOOP",
  ).not.toMatch(/<b>\{runtimeStatus\?\.runtime_identity\}/);
});

test("the runtime card still has a labelled slot in both languages", () => {
  const i18n = read("i18n");
  const filled = [...i18n.matchAll(/"intel\.runtimeSource": "([^"]*)"/g)].map((m) => m[1]);
  expect(
    filled.length,
    "the runtime card lost its label key in at least one language: this ruler must not pass by reading nothing",
  ).toBe(2);
  for (const sentence of filled) {
    expect(sentence, "the card label itself became machine speech").not.toMatch(MACHINE_WORD);
  }
});
