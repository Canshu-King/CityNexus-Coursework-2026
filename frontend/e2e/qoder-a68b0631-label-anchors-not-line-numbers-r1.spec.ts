/*
 * Anchor-drift guard (rel lane a68b0631, 19:53 order).
 *
 * Three shipped arms pinned facts about `src/i18n/LocaleContext.tsx` by *line number*.  Measured at
 * this head, every one of those numbers already pointed at a different entry -- `:18` was
 * `nav.agent`, `:835` was `studio.previewImpact`, `:863` was `twin.explicitFrom`, `:2064` was
 * `twin.cap.MAX_DAILY_WALKING_M`.  A drifted line anchor does not fail: it quietly starts
 * describing whatever line landed there, which is worse than a red test.
 *
 * Those three comments now name their subject by key/symbol.  This file is what keeps that honest:
 *  A1  no citation of `LocaleContext.tsx:<digits>` survives anywhere under e2e -- re-introducing one
 *      reddens this arm instead of rotting silently;
 *  A2  every label the replacement cites resolves, in the right dictionary arm, saying what the
 *      comment now claims it says;
 *  A3  the three files actually carry the new anchors (a deleted citation is not a converted one).
 *
 * All three arms read source text; none of them needs a browser, and none of them asserts a number
 * it did not just parse out of a file.
 */
import { expect, test } from "@playwright/test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const FILE = "e2e/qoder-a68b0631-label-anchors-not-line-numbers-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const NODE_CITATION = "label-anchor-citation-absent";
const NODE_RESOLVES = "label-anchor-resolves-and-says-what-it-claims";
const NODE_REPLACED = "the-three-former-citations-now-name-their-subject";

const ROOT = process.cwd();
if (!/[\\/]frontend$/.test(ROOT)) {
  throw new Error(node(NODE_RESOLVES, `root is not the frontend project (cwd=${ROOT}); these arms read source text and must run from frontend/`));
}

const dict = () => readFileSync(join(ROOT, "src/i18n/LocaleContext.tsx"), "utf8");
const spec = (name: string) => readFileSync(join(ROOT, "e2e", name), "utf8");
const defineCount = (text: string, key: string) =>
  (text.match(new RegExp(`"${key.replace(/\./g, "\\.")}"\\s*:`, "g")) ?? []).length;
const valueOf = (text: string, key: string, arm: "first" | "second") => {
  const re = new RegExp(`"${key.replace(/\./g, "\\.")}"\\s*:\\s*"([^"]*)"`, "g");
  const hits = [...text.matchAll(re)].map((m) => m[1]);
  return arm === "first" ? hits[0] : hits[1];
};

const SPECS = [
  "qoder-ord0344b-sentence-move-keeps-attribution.spec.ts",
  "qoder-ord0386-plan-version-history.spec.ts",
  "qoder-ord0477-journey-list-sort.spec.ts",
];

test("A1 no LocaleContext line-number citation is left under e2e", async () => {
  test.setTimeout(60_000);
  const found: Record<string, string[]> = {};
  for (const name of readdirSync(join(ROOT, "e2e"))) {
    if (!/\.(ts|tsx)$/.test(name)) continue;
    const text = readFileSync(join(ROOT, "e2e", name), "utf8");
    const hits = [...text.matchAll(/[^\s`]*LocaleContext\.tsx:\d+/g)].map((m) => m[0]);
    if (hits.length) found[name] = hits;
  }
  expect(
    Object.keys(found),
    node(NODE_CITATION, `line-number citations of the dictionary file are back (they rot every generation and then describe the wrong entry): ${JSON.stringify(found)}`),
  ).toEqual([]);
});

test("A2 every label the replacements cite resolves and says what they claim", async () => {
  test.setTimeout(60_000);
  const text = dict();
  const problems: string[] = [];

  /* the refusal sentence ord0344b holds verbatim */
  if (defineCount(text, "hub.continuationResumeRefused") !== 2)
    problems.push(`hub.continuationResumeRefused definitions=${defineCount(text, "hub.continuationResumeRefused")} (want one per dictionary arm)`);
  if (defineCount(text, "nav.authoritative") !== 2)
    problems.push(`nav.authoritative definitions=${defineCount(text, "nav.authoritative")} (want one per dictionary arm)`);
  if (valueOf(text, "nav.authoritative", "first") !== "Recovery")
    problems.push(`nav.authoritative English arm reads ${JSON.stringify(valueOf(text, "nav.authoritative", "first"))} (claim: "Recovery")`);
  if (valueOf(text, "nav.authoritative", "second") !== "恢复与执行")
    problems.push(`nav.authoritative zh-CN arm reads ${JSON.stringify(valueOf(text, "nav.authoritative", "second"))} (claim: 「恢复与执行」)`);

  /* the sentence the fixture copies must still be the dictionary's own, not a fixture invention */
  const held = (spec(SPECS[0]).match(/const OUR_SENTENCE = "([^"]+)"/) ?? [])[1];
  if (!held) problems.push("the refusal sentence is no longer held as a literal in ord0344b, so this comparison has no subject");
  else if (!text.includes(`"hub.continuationResumeRefused": "${held}"`))
    problems.push(`ord0344b's literal is not the zh-CN value of hub.continuationResumeRefused: literal=${JSON.stringify(held)}`);

  /* the locale guard ord0477 now names by symbol */
  const union = (text.match(/export type TripLocale = ([^;]+);/) ?? [])[1];
  if (!union || !union.includes('"en"') || !union.includes('"zh-CN"'))
    problems.push(`TripLocale is not the two-value union the arm claims: ${JSON.stringify(union)}`);
  const storedGuard = /localStorage\.getItem\("citynexus\.locale"\)[\s\S]{0,120}stored === "zh-CN" \|\| stored === "en"/;
  if (!storedGuard.test(text))
    problems.push("the `citynexus.locale` read is no longer followed by the `stored === \"zh-CN\" || stored === \"en\"` guard inside LocaleProvider");

  expect(problems, node(NODE_RESOLVES, problems.join(" ; "))).toEqual([]);
});

test("A3 the three former citations now name their subject instead of a line", async () => {
  test.setTimeout(60_000);
  const expected: Record<string, string> = {
    [SPECS[0]]: "hub.continuationResumeRefused",
    [SPECS[1]]: "nav.authoritative",
    [SPECS[2]]: "LocaleProvider",
  };
  const missing: string[] = [];
  for (const [name, label] of Object.entries(expected)) {
    const text = spec(name);
    if (!text.includes(label)) missing.push(`${name} does not name ${label}`);
    if (/LocaleContext\.tsx:\d+/.test(text)) missing.push(`${name} still carries a line number`);
  }
  expect(missing, node(NODE_REPLACED, missing.join(" ; "))).toEqual([]);

  /* reported, not asserted: the same hazard pointed at other spec files is out of this order's scope
     and the commander should have the number rather than my impression of it */
  const crossFile = SPECS.reduce((sum, name) => {
    const hits = [...spec(name).matchAll(/[\w.-]+\.(ts|tsx):\d+/g)];
    return sum + hits.length;
  }, 0);
  console.log(JSON.stringify({ node: NODE_REPLACED, line_number_citations_left_pointing_at_other_specs: crossFile, files: SPECS.length }));
});

/*
 * A4 and A5 are the second half of this order: the same hazard stood in five shipped product
 * sources, each citing the dictionary by line number, and every one of those numbers already
 * described a different entry at this head.  They now name `LocaleProvider`'s fallback arm or the
 * dictionary key itself, and the two arms below are what keep that from rotting back.
 */
const PRODUCT_SITES = [
  "src/app/v8PanelRegistry.tsx",
  "src/features/checkin/footprintCopy.ts",
  "src/features/discovery/RealCityDiscoveryPanel.tsx",
  "src/features/studio/planVersionHistoryCopy.ts",
  "src/features/traveler-twin/TwinPanel.tsx",
];

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

test("A4 no shipped source cites the dictionary by line number", async () => {
  test.setTimeout(60_000);
  const found: string[] = [];
  for (const rel of sourceFiles("src")) {
    const text = readFileSync(join(ROOT, rel), "utf8");
    for (const m of text.matchAll(/LocaleContext\.tsx:\d+/g)) found.push(`${rel} :: ${m[0]}`);
  }
  expect(
    found,
    node("product-source-line-citation-absent", `a line-number citation of the dictionary is back in shipped source, where it silently describes the wrong entry once lines move: ${JSON.stringify(found)}`),
  ).toEqual([]);
});

test("A5 the product-source anchors resolve to what their comments now claim", async () => {
  test.setTimeout(60_000);
  const text = dict();
  const problems: string[] = [];
  /* the arm all four fallback claims now name */
  const fallback = /const t = useCallback[\s\S]{0,400}dictionaries\.en\[key\] \?\? key/;
  if (!fallback.test(text))
    problems.push("`t()` no longer falls back through `dictionaries.en[key] ?? key` inside `const t = useCallback`, so four comments name an arm that is not there");
  /* the key RealCityDiscoveryPanel now names instead of two line numbers */
  if ((text.match(/"discovery\.defaultGoal"\s*:/g) ?? []).length !== 2)
    problems.push(`discovery.defaultGoal is not defined exactly once per language arm: ${(text.match(/"discovery\.defaultGoal"\s*:/g) ?? []).length}`);
  /* planVersionHistoryCopy's claim: the lookup only ever consults this file's own two dictionaries */
  const body = text.slice(text.indexOf("const t = useCallback"), text.indexOf("const t = useCallback") + 700);
  if (!/dictionaries\[locale\]/.test(body) || !/dictionaries\.en\[key\]/.test(body) || /extraDictionary|fallbackTable/.test(body))
    problems.push("the `t` implementation is no longer the two-dictionaries-only lookup that planVersionHistoryCopy cites");
  for (const rel of PRODUCT_SITES) {
    const site = readFileSync(join(ROOT, rel), "utf8");
    if (/LocaleContext\.tsx:\d+/.test(site)) problems.push(`${rel} still carries a line number`);
  }
  if (!readFileSync(join(ROOT, PRODUCT_SITES[2]), "utf8").includes("discovery.defaultGoal"))
    problems.push("the discovery panel no longer names the key it means");
  if (!readFileSync(join(ROOT, PRODUCT_SITES[0]), "utf8").includes("LocaleProvider"))
    problems.push("the panel registry no longer names LocaleProvider as the fallback's home");
  expect(problems, node("product-source-anchor-resolves", problems.join(" ; "))).toEqual([]);
});
