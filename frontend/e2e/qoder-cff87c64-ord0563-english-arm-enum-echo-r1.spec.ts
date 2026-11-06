/* ORD-0563 standing ruler (lane cff87c64) — the English arm must not answer with a shouted token
 * where the Chinese arm answers with a human word.
 *
 * Why this exists: on 2026-10-04 a real-browser read of the English arm found screen values that were
 * the server's enum or an all-caps shout (`ADVISORY`, `LEARNED_SOFT`, `ARRIVAL`, `CAPTURED REPLAY`) while
 * the Chinese arm said the human thing. The fix is eleven dictionary values; this ruler is the door that
 * keeps them fixed and forces any *new* all-caps English value to be judged and named rather than waved
 * through.
 *
 * It reads one source file and decides nothing about a running product, so it needs no backend, no front
 * and no store. Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 *
 * The predicate is deliberately broader than the commander's first scan (`_cmdr_enum_echo_scan_r1.py`,
 * which matches a single token): a space inside the value does not make `SELECTED STAY` less of a shout,
 * so multi-word values are flagged too. That is why this ruler sees more entries than that scan did.
 */
import { expect, test } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const DICTIONARY = join(ROOT, "frontend", "src", "i18n", "LocaleContext.tsx");
const EVIDENCE = process.env.CITYNEXUS_ENARM_EVIDENCE ?? "";
const RUN = process.env.CITYNEXUS_ENARM_RUN ?? "enarm-unset";

const CJK = /[\u4E00-\u9FFF]/; // CJK Unified Ideographs, spelled as escapes so no glyph has to survive an editor
const ENTRY = /^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$/;
// A shout: every letter is capitalised and nothing but letters, digits, underscores and spaces appear.
const SHOUT = /^[A-Z][A-Z0-9 _]*$/;
// Honestly abbreviated product words, not echoed enums.
const ACRONYMS = new Set(["UTC", "GMT", "PDF", "ID", "URL", "API", "AI", "GPS", "OSM", "OK", "FAQ",
  "COPILOT", "HTML", "JSON", "HTTP", "HTTPS", "CSRF", "MCP", "CN", "EN", "QR", "SMS", "SLA", "ETA",
  "ETD", "CSV", "UI", "UX", "DB", "POI", "OD"]);

/** The eleven values ORD-0563 brought down to human words. Each one's Chinese sibling is untouched. */
const FIXED: Record<string, string> = {
  "hub.arrival": "Arrival",
  "hub.departure": "Departure",
  "stay.capturedReplay": "Captured replay",
  "twin.explicit": "Explicit",
  "twin.learned": "Learned soft signal",
  "v14.mode.ADVISORY": "Advisory",
  "v14.mode.ASSISTED": "Assisted",
  "v14.mode.AUTONOMOUS": "Autonomous",
  "intel.advisory": "Advisory",
  "discovery.sealed": "Sealed replay",
  "push.checkingState": "Checking",
};

/**
 * Shouted English values that stay, because the slot they fill is a heading, a kicker or a field label --
 * the shout is the typography, and the Chinese arm says the same short thing. Each reason names the slot
 * this lane read at the render site on generation 0413e55634e9c6953ea88a4b01a17b8afd7ac88a.
 */
const INTENTIONAL: Record<string, string> = {
  "nav.groupToday": "sidebar group heading",
  "nav.groupPlan": "sidebar group heading",
  "nav.groupTravel": "sidebar group heading",
  "nav.groupSystem": "sidebar group heading",
  "nav.groupOther": "sidebar group heading",
  "studio.selectedStay": "v8-aside-heading",
  "studio.noStaySelected": "v8-aside-heading",
  "live.now": "v8-live-label beside the status dot",
  "live.awaitingPlanUpper": "the key names its own uppercase variant; a Title Case sibling serves the sentence",
  "live.risk": "risk block heading",
  "live.evidence": "drawer summary heading",
  "live.routeEvidence": "drawer summary label",
  "bookings.stayEvidence": "v8-selected-label",
  "bookings.stayPreview": "v8-selected-label",
  "agent.simulationOnly": "v15-simulation-only chip",
  "discovery.demoReplay": "status-dot label in the page header",
  "discovery.liveProviderMode": "status-dot label in the page header",
  "discovery.provenance": "intel-facts field label (the value sits in the <b> after it)",
  "discovery.mobility": "intel-facts field label",
  "discovery.grounding": "chain step label",
  "discovery.fullyGrounded": "chain step label",
  "discovery.feasibility": "intel-facts field label",
  "discovery.planversion": "intel-facts field label",
  "exec.liveStatus": "v8-kicker",
  "exec.clock": "v10-metric label above the value",
  "exec.currentStep": "v10-metric label above the value",
  "exec.offline": "v10-metric label above the value",
  "exec.authoritativeJourney": "v8-kicker on the authoritative panel",
  "exec.now": "label of the now-card",
  "exec.next": "label of the next-card",
  "inbox.multimodal": "v8-kicker on the inbox workbench",
  "inbox.authoritativeMemory": "v8-kicker on the memory panel",
  "intel.policy": "intel-facts field label",
  "intel.commitment": "intel-facts field label",
  "stay.rateEvidence": "definition-list term",
  "push.kicker": "v8-kicker on the capability card",
  "v14.kickerChanged": "v12-state-kicker",
  "v14.kickerProtected": "v12-state-kicker",
};

/**
 * Dictionary entries this lane could not find a single reader for, by literal key search across src plus a
 * check that no composed `t(`...${}`)` prefix could reach them (the only composed families are
 * `studio.gate.*`, `twin.dim.*` and `twin.cap.*`). A dead key is not a screen defect -- the value cannot
 * reach a pixel -- but it is not cleared either, so it is named here rather than folded into the design.
 */
const NO_READER: Record<string, string> = {
  "bookings.importKicker": "0 reader files in src",
  "me.currentMode": "0 reader files in src",
  "me.tripContext": "0 reader files in src",
  "discovery.sealedReplayReady": "0 reader files in src",
  "discovery.liveProvider": "0 reader files in src",
  "discovery.sealedReal": "0 reader files in src; the live ternary reads discovery.sealedRealReplay instead",
  "exec.notStarted": "0 reader files in src",
};

function entries(): { line: number; key: string; value: string }[] {
  const out: { line: number; key: string; value: string }[] = [];
  const lines = readFileSync(DICTIONARY, "utf8").split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    const m = ENTRY.exec(lines[i]);
    if (m) out.push({ line: i + 1, key: m[1], value: m[2] });
  }
  return out;
}

const all = entries();
const english = all.filter((e) => !CJK.test(e.value) && e.value.trim() !== "");
const shouted = english.filter((e) => SHOUT.test(e.value) && !ACRONYMS.has(e.value));
/* Every key appears twice in this file -- once per arm -- so a map keyed by name alone keeps whichever
   came last, which is the Chinese entry. The arms below measure the English arm, so they look the key up
   there; the Chinese side is checked separately, for presence rather than for wording. */
const byKey = new Map(english.map((e) => [e.key, e]));

test("the eleven fixed English values say a human word, and their Chinese siblings still say Chinese", () => {
  const offenders: string[] = [];
  for (const [key, expected] of Object.entries(FIXED)) {
    const hit = byKey.get(key);
    if (!hit) { offenders.push(`${key}: the entry disappeared`); continue; }
    if (hit.value !== expected) offenders.push(`${key}:${hit.line} paints "${hit.value}"`);
    /* Compared as spelled, not case-folded: `Arrival` is the human word and `ARRIVAL` is the token, and a
       case-insensitive comparison would condemn the fix alongside the defect. */
    const tail = key.split(".").pop()!.toUpperCase();
    if (hit.value === tail || hit.value.replace(/ /g, "_").toUpperCase() === tail && hit.value === hit.value.toUpperCase()) {
      offenders.push(`${key}:${hit.line} is the enum token echoed back`);
    }
  }
  expect(offenders, "an English-arm value went back to being a shout or the server's own token").toEqual([]);
  const zh = Object.keys(FIXED).map((k) => all.filter((e) => e.key === k && CJK.test(e.value)));
  expect(zh.filter((list) => list.length === 0).map((_, i) => Object.keys(FIXED)[i]),
    "the Chinese arm lost one of the fixed keys; this order never touches Chinese copy").toEqual([]);
});

test("every remaining shouted English value is judged and named, not waved through", () => {
  const unclassified = shouted
    .filter((e) => !(e.key in FIXED) && !(e.key in INTENTIONAL) && !(e.key in NO_READER))
    .map((e) => `${e.key}:${e.line} = ${e.value}`);
  expect(unclassified, "a new all-caps English value appeared; judge it (heading / dead key / fix it) and name it in this ruler before it lands").toEqual([]);
  const stale = Object.keys(INTENTIONAL).filter((k) => !shouted.some((e) => e.key === k) && !(k in FIXED));
  expect(stale, "the design list names a key this ruler no longer flags -- the dictionary moved, drop the stale name").toEqual([]);
});

test("the policy-mode family agrees with the vocabulary table that already words it", () => {
  /* POLICY_MODE in displayVocabulary.ts is the product's own wording for these three states, so the
     dictionary fallback has to agree with it or one screen says "Advisory" and the next says "ADVISORY". */
  const vocabulary = readFileSync(join(ROOT, "frontend", "src", "lib", "displayVocabulary.ts"), "utf8");
  for (const [key, word] of [["v14.mode.ADVISORY", "Advisory"], ["v14.mode.ASSISTED", "Assisted"],
    ["v14.mode.AUTONOMOUS", "Autonomous"]] as const) {
    expect(vocabulary, `displayVocabulary no longer words ${word}, so the two surfaces can drift apart`)
      .toContain(`"${word}"`);
    expect(byKey.get(key)?.value, `${key} drifted away from the shared wording`).toBe(word);
  }
});

test("the ruler read what it claims to have read", () => {
  expect(all.length, "the dictionary parse is empty; this ruler would otherwise pass on nothing").toBeGreaterThan(1000);
  expect(english.length, "no English-side entries parsed").toBeGreaterThan(400);
  if (EVIDENCE) {
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, JSON.stringify({
      generatedBy: "cff87c64",
      ruler: "ord0563-english-arm-enum-echo-r1",
      run: RUN,
      dictionary_lines: readFileSync(DICTIONARY, "utf8").split("\n").length,
      entries_parsed: all.length,
      english_entries: english.length,
      shouted_entries: shouted.length,
      fixed: FIXED,
      intentional: INTENTIONAL,
      no_reader: NO_READER,
      shouted_keys: shouted.map((e) => e.key).sort(),
    }, null, 1), "utf8");
  }
});
