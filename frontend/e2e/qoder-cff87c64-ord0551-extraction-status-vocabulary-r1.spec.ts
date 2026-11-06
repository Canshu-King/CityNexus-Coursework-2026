/* ORD-0551 extraction-status wording ruler (lane cff87c64) — one family the hub-quality ruler does not
 * cover: the top-level `status` that the two extraction routes answer with.
 *
 * The defect this exists for was on a real screen: a supplier failure reached a Chinese booking panel as
 * 「提取状态： FAILED_EXTERNAL」, because the workbench's fallback branch interpolated the reply's machine
 * status straight into a sentence. Like the mobility-quality case, the hole is not state-dependent: the
 * moment the backend can answer a status the vocabulary does not carry, that token goes on screen whatever
 * the store holds, because `pick` falls through to the labelled-raw channel.
 *
 * So this arm reads three source files and nothing else: no front, no back, no database, no seed. A red
 * here always means the emitters and the vocabulary disagree.
 *
 * Two things it deliberately cannot do, so nobody mistakes a green for coverage: it does not prove the
 * sentence renders on a phone (that needs the click leg — see the ORD-0551 acceptance arms), and it does
 * not prove the wording is a *good* sentence, only that it is not a machine word and is filled in both
 * languages.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { expect, test } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILES = {
  routes: join(ROOT, "backend", "app", "main.py"),
  vocabulary: join(ROOT, "frontend", "src", "lib", "displayVocabulary.ts"),
  workbench: join(ROOT, "frontend", "src", "features", "inbox", "JourneyInboxWorkbench.tsx"),
};

const BACKEND_PREREQ = existsSync(FILES.routes)
  ? null
  : `missing file: ${FILES.routes} -- this checkout has no backend/ beside frontend/, so the arms that read the server's own names cannot run here`;
const EVIDENCE = process.env.CITYNEXUS_EXSTAT_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_EXSTAT_RUN ?? "exstat-unset";

/* An enum name the backend can answer: `READY` (one word) and `FAILED_EXTERNAL` (several) are both
   machine statuses, so this pattern must not require an underscore -- requiring one silently dropped
   READY out of the denominator and let the ruler pass on two of three values. */
const ENUM_NAME = String.raw`[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*`;
/* A "machine word" inside a human sentence: a run of four or more capital letters, optionally
   underscore-joined. Four is the floor because a traveller sentence can legitimately contain short
   capitals (`CNY`, `AI`), but `READY`, `FAILED_EXTERNAL` and `UNKNOWN` are not traveller words. */
const MACHINE_WORD = /[A-Z][A-Z0-9_]{3,}/;
/* Bound to the declaration *name*, not to its type annotation: the r3 table is declared
   `Record<ExtractionStatus, Label>` so that a status without a wording fails the build, and an anchor that
   carried the old annotation would have gone red over a shape this ruler does not measure. What the arm
   guards is the table and its rows, and that is what is parsed below. */
const DECLARATION = "const EXTRACTION_STATUS";
const ROUTES = ["/api/v1/journeys/{journey_id}/inbox/text-extract", "/api/v1/journeys/{journey_id}/inbox/vision"];

function read(name: keyof typeof FILES): string {
  return readFileSync(FILES[name], "utf8");
}

/** One route's own body: from its decorator line to the next decorator, so a status literal elsewhere in
 *  the 15k-line file cannot be mistaken for this route's answer. */
function routeBody(src: string, path: string): string {
  const at = src.indexOf(`"${path}"`);
  if (at < 0) {
    throw new Error(`main.py: the route "${path}" is gone or renamed. This ruler is bound to that path; a rename must re-anchor it, not silently empty it.`);
  }
  const next = src.indexOf("\n@app.", at + path.length);
  const body = next < 0 ? src.slice(at) : src.slice(at, next);
  if (!body.includes("def ")) throw new Error(`main.py: no handler found after "${path}" — the region has no body to parse.`);
  return body;
}

/** The status names those routes can answer. */
function emittedStatuses(): string[] {
  const src = read("routes");
  const names = new Set<string>();
  for (const path of ROUTES) {
    const body = routeBody(src, path);
    const found = [...body.matchAll(new RegExp(String.raw`"status":\s*"(${ENUM_NAME})"`, "g"))].map((m) => m[1]);
    if (found.length === 0) {
      throw new Error(`main.py: no "status": "<ENUM_NAME>" literal parsed inside ${path} — a shape change blinds this ruler.`);
    }
    for (const name of found) names.add(name);
  }
  return [...names].sort();
}

/** The labels the screen owns for those names. */
function labelledStatuses(): Record<string, { zh: string; en: string }> {
  const src = read("vocabulary");
  const at = src.indexOf(DECLARATION);
  if (at < 0) throw new Error(`displayVocabulary.ts: the "${DECLARATION}" declaration was renamed. This ruler is bound to that name.`);
  const end = src.indexOf("\n};", at);
  if (end < 0) throw new Error("displayVocabulary.ts: the extraction-status table has no closing brace where this ruler expects one.");
  const body = src.slice(at, end);
  const out: Record<string, { zh: string; en: string }> = {};
  const row = new RegExp(String.raw`^\s*(${ENUM_NAME}|\w+)\s*:\s*\{\s*zh:\s*"([^"]*)"\s*,\s*en:\s*"([^"]*)"\s*,?\s*\},?\s*$`, "gm");
  for (const m of body.matchAll(row)) out[m[1]] = { zh: m[2], en: m[3] };
  if (Object.keys(out).length === 0) throw new Error("displayVocabulary.ts: the extraction-status table parsed empty — this ruler would otherwise pass on nothing.");
  return out;
}

function writeReading(payload: Record<string, unknown>): void {
  if (!EVIDENCE) return;
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(payload, null, 1), "utf8");
}

const emitted = BACKEND_PREREQ
  ? ({} as ReturnType<typeof emittedStatuses>)  // never used: the arms skip above
  : emittedStatuses();
const labelled = BACKEND_PREREQ
  ? ({} as ReturnType<typeof labelledStatuses>)  // never used: the arms skip above
  : labelledStatuses();
const routes = BACKEND_PREREQ
  ? ({} as ReturnType<typeof read>)  // never used: the arms skip above
  : read("routes");
const workbench = BACKEND_PREREQ
  ? ({} as ReturnType<typeof read>)  // never used: the arms skip above
  : read("workbench");

test.beforeEach(() => {
  test.skip(BACKEND_PREREQ !== null, BACKEND_PREREQ ?? undefined);
});

test("the statuses the extraction routes can answer and the statuses the screen can say are one set", () => {
  expect(emitted.length, "a single status value would not be worth a ruler").toBeGreaterThan(1);
  expect(
    Object.keys(labelled).sort(),
    "the screen carries a status word the routes cannot answer, or dropped one they can — a dropped one is answered by the gap sentence, which is a silence where the contract has a fact",
  ).toEqual(emitted);
});

test("every extraction status is worded in both languages, and no wording is a machine token", () => {
  const offenders: string[] = [];
  for (const [name, label] of Object.entries(labelled)) {
    if (!label.zh.trim() || !label.en.trim()) offenders.push(`${name}: an empty side`);
    for (const side of ["zh", "en"] as const) {
      const hit = MACHINE_WORD.exec(label[side]);
      if (hit) offenders.push(`${name}.${side} paints "${hit[0]}"`);
    }
  }
  expect(offenders, "an extraction-status label that still carries a server enum is the exact defect this ruler exists for").toEqual([]);
});

test("the workbench asks the vocabulary for the status it prints, and no longer interpolates the token", () => {
  expect(workbench, "the workbench stopped importing the vocabulary channel").toContain("extractionStatusLabel");
  expect(
    workbench.replace(/\s+/g, " "),
    "the status slot hands the server's own value to the sentence again — this is the line that painted FAILED_EXTERNAL",
  ).not.toMatch(/\$\{t\("inbox\.extractionStatus"\)\} \$\{result\.status\}/);
});

test("a rejection that still names a status is translated too, not reduced to an HTTP number", () => {
  /* ORD-0551 moved the supplier failure onto 5xx, so the reply body arrives through the rejection path.
     If either half of that disappears, the screen goes silent exactly when the provider failed. */
  expect(workbench, "the rejected reply no longer carries its body, so the status cannot be translated").toMatch(/class InboxRequestRejected[\s\S]*?payload/);
  expect(workbench, "the extraction catch sites no longer route a rejected status through the vocabulary").toContain("extractionFailureCopy");
  expect(workbench.replace(/\s+/g, " ")).toContain("extractionStatusLabel(named, zh)");
});

test("a supplier failure answers 5xx on both routes, and the answer that creates a draft still answers 201", () => {
  for (const path of ROUTES) {
    const body = routeBody(routes, path);
    expect(body, `${path}: the FAILED_EXTERNAL branch no longer sets a 5xx status code — 201 claims a resource that was never created`).toMatch(/response\.status_code\s*=\s*502/);
    expect(body, `${path}: only the supplier-failure branch may leave 201 behind` ).toMatch(/"status": "READY"/);
  }
});

test("the extraction lookup is closed: an unnamed status is never answered with its own word", () => {
  /* ORD-0551 r3, from the landing gate's reading. This family is called with the reply's own `status`, and a
     lookup that can return its input connects an upstream read to the shared `pick()`. The module graph
     answers a helper's return for *all* of its callers, so that one call site condemned five panels this
     order never opened. The arm below is the cheap local door; the gate's ruler is the authority. */
  const src = read("vocabulary");
  const at = src.indexOf("export function extractionStatusLabel");
  if (at < 0) throw new Error("displayVocabulary.ts: extractionStatusLabel is gone or renamed — this arm would otherwise pass on nothing.");
  const end = src.indexOf("\n}", at);
  if (end < 0) throw new Error("displayVocabulary.ts: extractionStatusLabel has no closing brace this arm can bound.");
  const body = src.slice(at, end);
  expect(body, "the status lookup is routed through the shared pick()/rawChannel() fallback again").not.toMatch(/\bpick\(|\brawChannel\(/);
  expect(body, "the lookup returns the value it was handed, so the reply's own word reaches the screen").not.toMatch(/return\s+(?:String\()?(?:value|raw|named)\b/);
  expect(body, "the closed table is what answers").toContain("EXTRACTION_STATUS[");
});

test("the ruler read what it claims to have read", () => {
  writeReading({
    generatedBy: "cff87c64",
    ruler: "ord0551-extraction-status-vocabulary-r1",
    run: RUN,
    emitted_statuses: emitted,
    labelled_statuses: labelled,
    routes_checked: ROUTES,
    file_bytes: Object.fromEntries((Object.keys(FILES) as (keyof typeof FILES)[]).map((k) => [k, read(k).length])),
  });
  expect(emitted.length, "the reading must not be written from an empty denominator").toBeGreaterThan(1);
});
