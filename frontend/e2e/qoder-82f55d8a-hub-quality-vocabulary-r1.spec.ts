/* Hub evidence wording — a standing ruler for the one defect class neither the landing gate nor a screen
 * sweep catches: a server enum name travelling into a human sentence.
 *
 * The exposure on `/v8/hub` was state-dependent (a plan with legs has to exist before the sentence is painted
 * at all), but the *hole* is not: the moment the backend can write a quality name the screen's vocabulary
 * does not carry, that name goes on screen whatever the store holds — `pick` falls through to the labelled-raw
 * channel and prints the server's own token. So this arm reads two files and nothing else: no front, no back,
 * no database, no seed. It is runnable on any checkout, and a red here always means the vocabulary and the
 * emitters disagree.
 *
 * Two things it deliberately cannot do, so nobody mistakes a green for coverage: it does not prove the
 * sentence renders (that needs a plan with legs — see the screen arms), and it does not prove the label is a
 * *good* sentence, only that it is not a machine word.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { expect, test } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILES = {
  qualities: join(ROOT, "backend", "app", "replanning", "provider_snapshots.py"),
  vocabulary: join(ROOT, "frontend", "src", "lib", "displayVocabulary.ts"),
  panel: join(ROOT, "frontend", "src", "features", "planning", "PlanRankingPanel.tsx"),
  copy: join(ROOT, "frontend", "src", "features", "planning", "planRankingCopy.ts"),
};
const EVIDENCE = process.env.CITYNEXUS_HUBQ_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_HUBQ_RUN ?? "hubq-unset";

const NAME = String.raw`[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+`;
const MACHINE_WORD = new RegExp(NAME);
const DECLARATION = "const MOBILITY_FACT_QUALITY: Record<string, Label> = {";
const REGION_START = "# Mobility fact quality.";
const REGION_END = "HARD_FEASIBLE_QUALITIES";

function read(name: keyof typeof FILES): string {
  return readFileSync(FILES[name], "utf8");
}

/** The quality names the backend can write, taken from the region that defines them. */
function emittedQualities(): string[] {
  const src = read("qualities");
  const from = src.indexOf(REGION_START);
  if (from < 0) {
    throw new Error(
      `provider_snapshots.py: the "${REGION_START}" marker moved or was renamed. This ruler is bound to that ` +
        `region on purpose; a rename must re-anchor it, not silently empty it.`,
    );
  }
  const to = src.indexOf(REGION_END, from);
  if (to < 0) {
    throw new Error("provider_snapshots.py: HARD_FEASIBLE_QUALITIES is gone, so the region has no end to parse.");
  }
  const names: string[] = [];
  for (const line of src.slice(from, to).split(/\r?\n/)) {
    const m = new RegExp(String.raw`^(${NAME})\s*=\s*"${m1()}"$`).exec(line.trim());
    if (m) names.push(m[1]);
  }
  if (names.length === 0) {
    throw new Error("provider_snapshots.py: no NAME = \"NAME\" quality constant parsed — a shape change blinds this ruler.");
  }
  return names;
}

/** A backreference has to be written after the name group exists; kept in one place so the regex stays honest. */
function m1(): string {
  return String.raw`\1`;
}

/** The labels the screen owns for those names. */
function labelledQualities(): Record<string, { zh: string; en: string }> {
  const src = read("vocabulary");
  const at = src.indexOf(DECLARATION);
  if (at < 0) {
    throw new Error(`displayVocabulary.ts: the "${DECLARATION}" declaration was renamed. This ruler is bound to that name.`);
  }
  const end = src.indexOf("\n};", at);
  if (end < 0) throw new Error("displayVocabulary.ts: the quality table has no closing brace where this ruler expects one.");
  const body = src.slice(at, end);
  const out: Record<string, { zh: string; en: string }> = {};
  const row = new RegExp(String.raw`^\s*(${NAME}|\w+)\s*:\s*\{\s*zh:\s*"([^"]*)"\s*,\s*en:\s*"([^"]*)"\s*,?\s*\},?\s*$`, "gm");
  for (const m of body.matchAll(row)) out[m[1]] = { zh: m[2], en: m[3] };
  if (Object.keys(out).length === 0) {
    throw new Error("displayVocabulary.ts: the quality table parsed empty — this ruler would otherwise pass on nothing.");
  }
  return out;
}

function writeReading(payload: Record<string, unknown>): void {
  if (!EVIDENCE) return;
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(payload, null, 1), "utf8");
}

/**
 * ORD-0576: this ruler compares the screen's vocabulary against the names the backend can write, so two of
 * its arms need `backend/` next to `frontend/`. Reading it at module scope made a frontend-only export abort
 * collection for *every* spec in the directory -- one missing file silenced the whole gate. The read is now
 * lazy and those two arms skip themselves with the absent path named in the reason; the other two arms,
 * which only need frontend files, still run in either shape.
 */
const BACKEND_PREREQ = existsSync(FILES.qualities)
  ? null
  : `missing file: ${FILES.qualities} -- this checkout has no backend/ beside frontend/, so the arms that compare against the names the server can write cannot run here`;

test("the names the server can write and the names the screen can say are one set", () => {
  test.skip(BACKEND_PREREQ !== null, BACKEND_PREREQ ?? undefined);
  const emitted = emittedQualities();
  const labelled = labelledQualities();
  expect(emitted.length, "a single quality name would not be worth a ruler").toBeGreaterThan(1);
  expect(
    Object.keys(labelled).sort(),
    "the screen carries a quality word the server cannot write, or dropped one it can — either way the sentence falls to the labelled-raw channel",
  ).toEqual([...emitted].sort());
});

test("every quality is worded in both languages, and no wording is a machine token", () => {
  test.skip(BACKEND_PREREQ !== null, BACKEND_PREREQ ?? undefined);
  const emitted = emittedQualities();
  const labelled = labelledQualities();
  const offenders: string[] = [];
  for (const [name, label] of Object.entries(labelled)) {
    if (!label.zh.trim() || !label.en.trim()) offenders.push(`${name}: an empty side`);
    for (const side of ["zh", "en"] as const) {
      const hit = MACHINE_WORD.exec(label[side]);
      if (hit) offenders.push(`${name}.${side} paints "${hit[0]}"`);
    }
  }
  expect(offenders, "a quality label that still carries a server enum is the exact defect this ruler exists for").toEqual([]);
  writeReading({
    generatedBy: "82f55d8a",
    ruler: "hub-quality-vocabulary-r1",
    run: RUN,
    emitted_qualities: emitted,
    labelled_qualities: labelled,
    panel_imports_labelled: read("panel").includes("mobilityFactQualityLabel"),
    file_bytes: Object.fromEntries(
      (Object.keys(FILES) as (keyof typeof FILES)[]).map((k) => [k, read(k).length]),
    ),
  });
});

test("the ranking panel asks the vocabulary for the quality it prints", () => {
  const panel = read("panel");
  expect(panel, "the panel stopped importing the vocabulary channel").toContain("mobilityFactQualityLabel");
  expect(
    panel.replace(/\s+/g, " "),
    "the quality slot hands the server's own value to the sentence again — this is the line that painted SYNTHETIC_DEFAULT",
  ).not.toMatch(/quality:\s*row\.evidence\.worstQuality\s*\?\?/);
});

test("the sentence still has a quality slot in both languages", () => {
  const copy = read("copy");
  const filled = [...copy.matchAll(/"rank\.evidenceNote": "([^"]*\{quality\}[^"]*)"/g)].map((m) => m[1]);
  expect(
    filled.length,
    "the evidence note lost its {quality} slot in at least one language: this ruler must not pass by reading nothing",
  ).toBe(2);
  for (const sentence of filled) {
    expect(
      sentence,
      "the note no longer says which leg-count the quality belongs to, so a label here would not be a sentence about evidence",
    ).toMatch(/\{hard\}[\s\S]*\{total\}|(\{total\}[\s\S]*\{hard\})/);
  }
});
