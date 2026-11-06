/* ORD-0553 — a standing ruler for the execution-status wording family.

 * Shape reused from `e2e/qoder-82f55d8a-hub-quality-vocabulary-r1.spec.ts` and from the runtime-identity
 * arm this lane shipped: read source files, start nothing, listen on nothing. The judgement is the one the
 * order states: every status name the contract can answer must have a traveller wording, and a wording that
 * is the enum's own printed form is not a wording.
 *
 * The二分 this ruler encodes: only names the server can answer are policed here. The screen also carries
 * uppercase kickers ("AUTHORITATIVE JOURNEY", "NEXT", "TRIP STUDIO · DRAFT THEN ACCEPT",
 * "V14 · REALITY CHANGED") that no emitter writes -- those are labels the product authored, and抹 them
 * would remove text a traveller can read, so this ruler deliberately does not look at them.
 *
 * What a green does NOT prove: that the card renders (see the screen arms), or that a wording is a *good*
 * sentence -- only that it is not a machine token and that it exists in both languages.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { expect, test } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILES = {
  contract: join(ROOT, "backend", "app", "schemas.py"),
  stateMap: join(ROOT, "frontend", "src", "features", "execution", "V12StatePrimitives.tsx"),
  i18n: join(ROOT, "frontend", "src", "i18n", "LocaleContext.tsx"),
  vocabulary: join(ROOT, "frontend", "src", "lib", "displayVocabulary.ts"),
  panel: join(ROOT, "frontend", "src", "features", "execution", "V12AuthoritativeJourneyPanel.tsx"),
};

const BACKEND_PREREQ = existsSync(FILES.contract)
  ? null
  : `missing file: ${FILES.contract} -- this checkout has no backend/ beside frontend/, so the arms that read the server's own names cannot run here`;
const EVIDENCE = process.env.CITYNEXUS_STATUSQ_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_STATUSQ_RUN ?? "statusq-unset";

const NAME = String.raw`[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*`;
/* The trailing group is `*`, not `+`: this contract enumerates single-word statuses (ACTIVE, COMPLETED,
 READY) as well as underscored ones, and a pattern that demands an underscore counts three of ten names and
 then reports a healthy-looking denominator. That is the way a file-only ruler goes quietly blind. */
const UPPER_SNAKE = new RegExp(`^${NAME}$`);
/* A bare run of four capitals is what the commander's screen sweep flags as painted machine speech, so a
   wording like "EN ROUTE" (the enum with its underscore re-spaced) has to be caught here too. */
const ALLCAPS_WORD = /\b[A-Z]{4,}\b/;

function read(name: keyof typeof FILES): string {
  return readFileSync(FILES[name], "utf8");
}

/** The status names the contract says the server may answer for this field. */
function contractStatuses(): { names: string[]; line: string } {
  const src = read("contract");
  const hit = src.split(/\r?\n/).find((l) => /status: Literal\[/.test(l) && l.includes("EN_ROUTE"));
  if (!hit) {
    throw new Error(
      'schemas.py: no `status: Literal[...]` line naming EN_ROUTE. This ruler is bound to that contract line on ' +
        'purpose; a rename must re-anchor it, not silently empty it.',
    );
  }
  const names = [...hit.matchAll(new RegExp(String.raw`"(${NAME})"`, "g"))].map((m) => m[1]);
  if (names.length < 2) throw new Error(`schemas.py: only ${names.length} status names parsed from that line — blinded.`);
  return { names, line: hit.trim().slice(0, 120) };
}

/** stateKeyMap: which contract names the screen routes to which i18n key. */
function stateKeyMap(): Record<string, string> {
  const src = read("stateMap");
  const at = src.indexOf("const stateKeyMap: Record<string, string> = {");
  if (at < 0) throw new Error("V12StatePrimitives.tsx: stateKeyMap was renamed; this ruler is bound to that name.");
  const end = src.indexOf("};", at);
  const body = src.slice(at, end < 0 ? undefined : end);
  const out: Record<string, string> = {};
  for (const m of body.matchAll(new RegExp(String.raw`^\s*(${NAME}):\s*"([^"]+)"`, "gm"))) out[m[1]] = m[2];
  if (Object.keys(out).length === 0) throw new Error("V12StatePrimitives.tsx: stateKeyMap parsed empty — this ruler would pass on nothing.");
  return out;
}

/** Both language tables of the i18n dictionary, keyed separately so a missing side cannot hide. */
function dictionaries(): Record<"en" | "zhCN", Record<string, string>> {
  const src = read("i18n");
  const blocks: Record<"en" | "zhCN", Record<string, string>> = { en: {}, zhCN: {} };
  for (const tag of ["en", "zhCN"] as const) {
    const at = src.indexOf(tag === "en" ? "const en: Dict = {" : "const zhCN: Dict = {");
    if (at < 0) throw new Error(`LocaleContext.tsx: the ${tag} table marker moved; the ruler cannot tell an absent key from a moved file.`);
    const next = src.indexOf("\n};", at);
    for (const m of src.slice(at, next).matchAll(/^\s*"([^"]+)":\s*"([^"]*)"/gm)) blocks[tag][m[1]] = m[2];
    if (Object.keys(blocks[tag]).length < 100) throw new Error(`LocaleContext.tsx: only ${Object.keys(blocks[tag]).length} keys parsed from the ${tag} block — blinded.`);
  }
  return blocks;
}

/** The fallback family: names the traveller dictionary does not carry. */
function fallbackLabels(): Record<string, { zh: string; en: string }> {
  const src = read("vocabulary");
  const at = src.indexOf("const EXECUTION_STATE: Record<string, Label> = {");
  if (at < 0) throw new Error("displayVocabulary.ts: EXECUTION_STATE was renamed; this ruler is bound to that name.");
  const end = src.indexOf("\n};", at);
  const out: Record<string, { zh: string; en: string }> = {};
  for (const m of src.slice(at, end).matchAll(new RegExp(String.raw`^\s*(${NAME}):\s*\{\s*zh:\s*"([^"]*)"\s*,\s*en:\s*"([^"]*)"\s*,?\s*\}`, "gm"))) {
    out[m[1]] = { zh: m[2], en: m[3] };
  }
  return out;
}

const { names: contract, line: contractLine } = BACKEND_PREREQ
  ? ({ names: [], line: "" } as ReturnType<typeof contractStatuses>)  // never used: the arms skip above
  : contractStatuses();
const keyMap = BACKEND_PREREQ
  ? ({} as ReturnType<typeof stateKeyMap>)  // never used: the arms skip above
  : stateKeyMap();
const dicts = BACKEND_PREREQ
  ? ({} as ReturnType<typeof dictionaries>)  // never used: the arms skip above
  : dictionaries();
const fallback = BACKEND_PREREQ
  ? ({} as ReturnType<typeof fallbackLabels>)  // never used: the arms skip above
  : fallbackLabels();

type Resolved = { name: string; via: string; zh: string; en: string };
function resolve(name: string): Resolved | null {
  const key = keyMap[name];
  if (key) {
    const zh = dicts.zhCN[key], en = dicts.en[key];
    if (zh === undefined || en === undefined) return { name, via: `i18n ${key}`, zh, en };
    return { name, via: `i18n ${key}`, zh, en };
  }
  const fb = fallback[name];
  if (fb) return { name, via: "displayVocabulary EXECUTION_STATE", zh: fb.zh, en: fb.en };
  return null;
}

const resolved = contract.map(resolve);

function writeReading(payload: Record<string, unknown>): void {
  if (!EVIDENCE) return;
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(payload, null, 1), "utf8");
}

test.beforeEach(() => {
  test.skip(BACKEND_PREREQ !== null, BACKEND_PREREQ ?? undefined);
});

test("every status the contract can answer has one wording, in both languages", () => {
  const unworded = contract.filter((n, i) => !resolved[i]).map((n) => n);
  const halfSided = resolved.filter((r) => r && (!r.zh || !r.en)).map((r) => `${r.name} via ${r.via}`);
  expect(
    unworded,
    "a status the server can answer with no wording falls through to the labelled-raw channel and paints the enum (or the key name) on the traveller's screen",
  ).toEqual([]);
  expect(halfSided, "one language side missing is the same defect with a different reader").toEqual([]);
});

test("no status wording is the machine token it is supposed to translate", () => {
  const offenders: string[] = [];
  for (const r of resolved) {
    if (!r) continue;
    for (const side of ["zh", "en"] as const) {
      const text = r[side] ?? "";
      if (UPPER_SNAKE.test(text.trim())) offenders.push(`${r.name}.${side} prints the enum "${text}"`);
      const caps = ALLCAPS_WORD.exec(text);
      if (caps) offenders.push(`${r.name}.${side} prints the all-caps word "${caps[0]}" in "${text}"`);
      /* Verbatim means verbatim: the value that is the contract's own string character for character. A
         deliberate case-fold ("Completed" for COMPLETED) is not policed here, because the same word in
         ordinary casing is what a zh reader already gets (已完成) and a ruler that bans it would drag
         every unrelated screen's labels into this order. */
      if (text.trim() === r.name) offenders.push(`${r.name}.${side} is the enum verbatim ("${text}")`);
    }
  }
  expect(offenders, "an execution-status wording that is the server's own printed form is the exact defect this order is about").toEqual([]);
});

test("the panel asks for a wording instead of putting the payload field on screen", () => {
  const panel = read("panel");
  expect(panel, "the panel stopped routing through the state label helper").toMatch(/label\(/);
  expect(panel, "the panel imports the fallback family").toContain("executionStateLabel");
  const raw = /<span[^>]*>\{(?:item|now|next)\??\.status\}<\/span>/.exec(panel.replace(/\s+/g, " "));
  expect(raw, "a status field is interpolated straight into the markup").toBe(null);
});

test("the ruler is reading a live surface, not an empty one", () => {
  expect(contract.length, "the contract line should still enumerate the states this screen renders").toBeGreaterThanOrEqual(5);
  expect(Object.keys(keyMap).length, "stateKeyMap went empty — the ruler would otherwise pass on nothing").toBeGreaterThan(1);
  expect(Object.keys(dicts.en).length, "the en dictionary looks truncated").toBeGreaterThan(100);
  expect(Object.keys(dicts.zhCN).length, "the zh dictionary looks truncated").toBeGreaterThan(100);
  writeReading({
    generatedBy: "45d25bfb",
    ruler: "authoritative-status-vocabulary-r1",
    run: RUN,
    contract_line: contractLine,
    contract_statuses: contract,
    state_key_map: keyMap,
    resolved_wordings: Object.fromEntries(resolved.filter(Boolean).map((r) => [r!.name, { via: r!.via, zh: r!.zh, en: r!.en }])),
    fallback_labels: fallback,
    file_bytes: Object.fromEntries((Object.keys(FILES) as (keyof typeof FILES)[]).map((k) => [k, read(k).length])),
  });
});
