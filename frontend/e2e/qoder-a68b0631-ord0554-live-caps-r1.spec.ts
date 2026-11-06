import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/**
 * ORD-0554 · lane a68b0631 · `/v8/live` caps census, judged by source.
 *
 * The order offers two verdicts per painted all-caps word: a word the *server* answers with is
 * dirty and belongs behind `src/lib/displayVocabulary.ts`; a word the product authored is the
 * traveller-facing style and must not be touched.  Grepping the word in `backend/app/` cannot
 * decide that -- it matches letters, not sources, and it matched `NEXT` (an edge action) and
 * `LIVE` (a provider provenance) here while both screen words come from the dictionary.
 *
 * So these arms judge by source, on the built bundle against the product's own seeded journey:
 *
 *   A1  census + attribution: every painted caps token is either a value of the shipped English
 *       dictionary (authored, with the key and the `t("<key>")` request site named) or a string
 *       the transport actually answered with (data or enum).  Positive control: the panel's own
 *       words must paint, so a blank screen cannot pass.
 *   A2  the same screen in zh-CN paints none of the English label words, and paints their Chinese
 *       counterparts.
 *   A3  the decisive dirty test: one distinct sentinel per enum-shaped field of the real
 *       `/experience` answer.  A sentinel that reaches the screen names a server enum painting
 *       raw and needing a vocabulary family.  Fields the answer does not carry are named
 *       NOT_PROBED rather than counted as clean.
 */

const ROOT = process.cwd();
const LOCALE_FILE = join(ROOT, "src/i18n/LocaleContext.tsx");
const OUT_DIR = join(ROOT, "review/ord0554");

/** Words the order names; the four panel words paint unconditionally, the card words are
 *  state-dependent and are recorded rather than demanded. */
const ALWAYS_PAINT = ["LIVE", "CLOCK", "CURRENT", "OFFLINE"];
const STATE_DEPENDENT = ["NEXT", "NAVIGATE", "RISK"];

const CAPS = /\b[A-Z][A-Z0-9_]{2,}\b/g;

/** One sentinel per enum-shaped field the live screen reads. */
const SENTINEL_PATHS: { field: string; set: (plan: Record<string, unknown>, v: string) => boolean }[] = [
  { field: "active_plan.status", set: (p, v) => setValue(p, ["status"], v) },
  { field: "active_plan.source", set: (p, v) => setValue(p, ["source"], v) },
  { field: "active_plan.cursor_status", set: (p, v) => setValue(p, ["cursor_status"], v) },
  { field: "active_plan.current_activity_id", set: (p, v) => setValue(p, ["current_activity_id"], v) },
  {
    field: "active_plan.activities[].type",
    set: (p, v) => everyItem(p, "activities", (item) => setValue(item, ["type"], v)),
  },
  {
    field: "active_plan.activities[].status",
    set: (p, v) => everyItem(p, "activities", (item) => setValue(item, ["status"], v)),
  },
  {
    field: "active_plan.activities[].cost_status",
    set: (p, v) => everyItem(p, "activities", (item) => setValue(item, ["cost_status"], v)),
  },
  {
    field: "active_plan.legs[].mode",
    set: (p, v) => everyItem(p, "legs", (item) => setValue(item, ["mode"], v)),
  },
  {
    field: "active_plan.legs[].provider_key",
    set: (p, v) => everyItem(p, "legs", (item) => setValue(item, ["provider_key"], v)),
  },
  {
    field: "active_plan.legs[].provenance",
    set: (p, v) => everyItem(p, "legs", (item) => setValue(item, ["provenance"], v)),
  },
  {
    field: "active_plan.legs[].crs",
    set: (p, v) => everyItem(p, "legs", (item) => setValue(item, ["crs"], v)),
  },
  {
    field: "active_plan.disruptions[].type",
    set: (p, v) => everyItem(p, "disruptions", (item) => setValue(item, ["type"], v)),
  },
  {
    field: "active_plan.disruptions[].severity",
    set: (p, v) => everyItem(p, "disruptions", (item) => setValue(item, ["severity"], v)),
  },
  {
    field: "active_plan.disruptions[].status",
    set: (p, v) => everyItem(p, "disruptions", (item) => setValue(item, ["status"], v)),
  },
  { field: "journey.timezone", set: (p, v) => setValue(p, ["__journey", "timezone"], v) },
  { field: "journey.city_code", set: (p, v) => setValue(p, ["__journey", "city_code"], v) },
  { field: "supply.state", set: (p, v) => setValue(p, ["__supply", "state"], v) },
];

function setValue(obj: Record<string, unknown>, path: string[], value: string): boolean {
  let node: Record<string, unknown> | undefined = obj;
  for (const key of path.slice(0, -1)) {
    node = node?.[key] as Record<string, unknown> | undefined;
  }
  const leaf = path[path.length - 1];
  if (!node || !(leaf in node)) return false;
  node[leaf] = value;
  return true;
}

function everyItem(
  plan: Record<string, unknown>,
  listKey: string,
  fn: (item: Record<string, unknown>) => boolean,
): boolean {
  const list = plan[listKey];
  if (!Array.isArray(list) || list.length === 0) return false;
  return list.every((item) => fn(item as Record<string, unknown>));
}

function dictionary(locale: "en" | "zh"): { key: string; value: string; line: number }[] {
  const text = readFileSync(LOCALE_FILE, "utf8");
  const start = text.indexOf(locale === "en" ? "const en" : "const zhCN");
  if (start < 0) throw new Error(`dictionary block ${locale} not found in the locale module`);
  const stop = locale === "en" ? text.indexOf("const zhCN") : text.length;
  const body = text.slice(start, stop > start ? stop : text.length);
  const out: { key: string; value: string; line: number }[] = [];
  /* The key class carries camelCase: a lowercase-only class reads 260 of the 938 English keys and
     silently calls the other 678 "not authored here", which is a one-sided ruler -- it can only
     invent dirty words, never clear them. */
  for (const m of body.matchAll(/^\s*"([A-Za-z0-9_.\-]+)"\s*:\s*"((?:[^"\\]|\\.)*)"/gm)) {
    out.push({
      key: m[1],
      value: m[2].replace(/\\"/g, '"'),
      line: text.slice(0, start + (m.index ?? 0)).split("\n").length,
    });
  }
  if (out.length === 0) throw new Error(`dictionary ${locale} parsed to zero entries`);
  return out;
}

const LIVE_MODULES = [
  "src/app/V8Experience.tsx",
  "src/features/execution/V10LiveExecutionPanel.tsx",
  "src/features/execution/V12StatePrimitives.tsx",
  "src/features/execution/V12AuthoritativeJourneyPanel.tsx",
];

function requestSites(key: string): string[] {
  const sites: string[] = [];
  for (const rel of LIVE_MODULES) {
    readFileSync(join(ROOT, rel), "utf8")
      .split("\n")
      .forEach((line, i) => {
        if (line.includes(`t("${key}")`) || line.includes(`t("${key}",`)) sites.push(`${rel}:${i + 1}`);
      });
  }
  return sites;
}

/** Responses the screen answers with, kept so attribution is measured rather than assumed. */
function captureTransport(page: Page, bag: { url: string; body: string }[]) {
  page.on("response", async (response) => {
    const url = response.url();
    if (!url.includes("/api/v1/")) return;
    try {
      bag.push({ url, body: await response.text() });
    } catch {
      bag.push({ url, body: "" });
    }
  });
}

async function openLive(page: Page, locale: string) {
  await page.setViewportSize({ width: 412, height: 915 });
  await page.addInitScript((value) => {
    localStorage.setItem("citynexus.locale", value);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (navigator as any).serviceWorker.register = () => Promise.resolve({});
  }, locale);
  await page.goto("/v8/live", { waitUntil: "domcontentloaded" });
  const panel = page.locator("[data-testid='v10-live-execution-panel']");
  await expect(
    panel,
    "the live execution panel never painted, so a caps census below would be reading an empty stage",
  ).toBeVisible({ timeout: 45_000 });
  return panel;
}

async function paintedCaps(page: Page): Promise<{ token: string; tag: string; cls: string; textHead: string }[]> {
  const nodes = await page.evaluate(() => {
    const out: { text: string; tag: string; cls: string }[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = (n as Text).parentElement;
      if (!el || el.closest("script,style")) continue;
      const rect = el.getBoundingClientRect();
      const text = (n as Text).nodeValue ?? "";
      if (rect.width > 0 && rect.height > 0 && text.trim().length > 0)
        out.push({ text, tag: el.tagName.toLowerCase(), cls: el.className || "" });
    }
    return out;
  });
  const readings: { token: string; tag: string; cls: string; textHead: string }[] = [];
  for (const node of nodes) {
    for (const m of node.text.matchAll(CAPS))
      readings.push({ token: m[0], tag: node.tag, cls: node.cls, textHead: node.text.trim().slice(0, 90) });
  }
  return readings;
}

function writeEvidence(name: string, payload: unknown) {
  /* One file per arm *per run mode*: the counter-arms re-run the same arm against mutated product
     bytes, and a shared filename would leave the mutant's reading filed as though it were the
     guard's.  `QODER_ARM_TAG` names which world produced the file. */
  const tag = process.env.QODER_ARM_TAG ?? "guard";
  mkdirSync(OUT_DIR, { recursive: true });
  writeFileSync(join(OUT_DIR, name.replace(/_r1\.json$/, "") + `_${tag}_r1.json`), JSON.stringify(payload, null, 2), "utf8");
}

/** Rewrite the real /experience answer, one distinct sentinel per enum-shaped field.
 *  The shell refetches `/experience` on its own timer, so a field can be served several different
 *  sentinels in one walk; every sentinel ever served is kept, because checking only the last one
 *  would miss a word that painted from an earlier pass. */
async function sentinelize(page: Page, served: Record<string, string[]>) {
  let counter = 0;
  await page.route("**/api/v1/**/experience", async (route) => {
    let res;
    try {
      res = await route.fetch();
    } catch (error) {
      /* The shell refreshes `/experience` on its own timer, and that tick can land after this
         test's last assertion.  The rewrite has nothing left to report to, so it lets the request
         go by rather than failing a run whose measurements are already written. */
      return route.fallback();
    }
    const json = (await res.json()) as Record<string, unknown>;
    const plan = (json.active_plan ?? {}) as Record<string, unknown>;
    const journey = (json.journey ?? {}) as Record<string, unknown>;
    const supply = (json.supply ?? {}) as Record<string, unknown>;
    plan.__journey = journey;
    plan.__supply = supply;
    for (const entry of SENTINEL_PATHS) {
      counter += 1;
      const sentinel = `QODERSENTINEL${counter}${entry.field.replace(/[^a-z]/gi, "").toUpperCase().slice(0, 12)}`;
      if (entry.set(plan, sentinel)) served[entry.field] = (served[entry.field] ?? []).concat(sentinel);
    }
    delete plan.__journey;
    delete plan.__supply;
    return route.fulfill({ response: res, body: JSON.stringify(json) });
  });
}

test("A1 [en] every caps word on /v8/live is attributed to a source", async ({ page }) => {
  test.setTimeout(240_000);
  const transport: { url: string; body: string }[] = [];
  captureTransport(page, transport);
  await openLive(page, "en");
  const readings = await paintedCaps(page);
  const tokens = [...new Set(readings.map((r) => r.token))];

  /* Positive control first: the live panel's own words must be on the screen. */
  for (const word of ALWAYS_PAINT) {
    expect(
      tokens.includes(word),
      `the live panel should paint ${word}; the census reads ${JSON.stringify(tokens)}`,
    ).toBe(true);
  }
  expect(tokens.length, "an empty denominator is not a pass").toBeGreaterThan(0);

  const en = dictionary("en");
  /* Instrument self-check, named to the key that hid from the first version of this parser: a
     dictionary reader that drops camelCase keys can only invent dirty words, never clear them. */
  expect(
    en.filter((entry) => entry.key === "exec.liveStatus").length,
    "the dictionary parser is not reading the exec.* keys, so every attribution below is suspect",
  ).toBeGreaterThan(0);

  const table = tokens.map((token) => {
    const owners = en.filter((entry) => entry.value.includes(token));
    const inTransport = transport.some((item) => item.body.includes(token));
    return {
      token,
      authored_by: owners.map((entry) => ({
        key: entry.key,
        definition: `src/i18n/LocaleContext.tsx:${entry.line}`,
        value: entry.value,
      })),
      requested_at: [...new Set(owners.flatMap((entry) => requestSites(entry.key)))],
      in_transport_answer: inTransport,
    };
  });
  const unattributed = table.filter((row) => row.authored_by.length === 0 && !row.in_transport_answer);
  writeEvidence("a1_en_census_sourced_r1.json", {
    channel: "built bundle on vite preview, product's own seeded journey, no response patching",
    painted_tokens: tokens,
    state_dependent_words: STATE_DEPENDENT.map((word) => ({ word, painted: tokens.includes(word) })),
    readings,
    table,
    unattributed,
  });
  expect(
    unattributed.map((row) => row.token),
    "these words are on the screen and neither the dictionary nor the transport carries them",
  ).toEqual([]);
});

test("A2 [zh-CN] the same screen speaks one language", async ({ page }) => {
  test.setTimeout(240_000);
  await openLive(page, "zh-CN");
  const readings = await paintedCaps(page);
  const tokens = [...new Set(readings.map((r) => r.token))];
  const en = dictionary("en");
  /* A label word is a caps word carried by a dictionary key that one of the live modules asks for.
     Comparing against the English dictionary as a whole would let a Chinese screen pass simply
     because a zh value happens to repeat the Latin letters, which is the opposite of a guard. */
  const labelWords = new Set(
    en
      .filter((entry) => requestSites(entry.key).length > 0)
      .flatMap((entry) => entry.value.match(CAPS) ?? []),
  );
  const englishLabels = tokens.filter((token) => labelWords.has(token));
  const body = await page.evaluate(() => document.body.innerText);
  const zh = dictionary("zh");
  const expectedKeys = ["exec.liveStatus", "exec.clock", "exec.currentStep", "exec.offline"];
  const zhLabels = expectedKeys.map((key) => {
    const entry = zh.find((item) => item.key === key);
    expect(entry, `the Chinese dictionary has no ${key}`).toBeTruthy();
    return { key, word: entry!.value };
  });
  const missing = zhLabels.filter((label) => !body.includes(label.word)).map((label) => label.key);
  writeEvidence("a2_zh_census_r1.json", {
    channel: "built bundle, seeded journey",
    tokens,
    label_word_allowlist_size: labelWords.size,
    englishLabels,
    zh_labels_painted: zhLabels.filter((label) => body.includes(label.word)),
    zh_labels_missing: missing,
  });
  expect(
    missing,
    `these Chinese labels did not paint, so the empty leak list above would be a blank screen: ${JSON.stringify(
      missing,
    )}`,
  ).toEqual([]);
  expect(
    englishLabels,
    `English label words reached the Chinese screen: ${JSON.stringify(englishLabels)}`,
  ).toEqual([]);
});

test("A3 no enum-shaped field of the answer reaches the traveller raw", async ({ page }) => {
  test.setTimeout(240_000);
  const served: Record<string, string[]> = {};
  await sentinelize(page, served);
  await openLive(page, "en");
  const body = await page.evaluate(() => document.body.innerText);
  const rows = SENTINEL_PATHS.map((entry) => ({
    field: entry.field,
    sentinels_served: served[entry.field] ?? [],
    probed: (served[entry.field] ?? []).length > 0,
    painted: (served[entry.field] ?? []).some((sentinel) => body.includes(sentinel)),
  }));
  const leaked = rows.filter((row) => row.painted).map((row) => row.field);
  const notProbed = rows.filter((row) => !row.probed).map((row) => row.field);
  writeEvidence("a3_sentinel_walk_r1.json", {
    channel: "built bundle; the product's real answer rewritten in flight, one distinct sentinel per field",
    fields_declared: SENTINEL_PATHS.length,
    fields_probed: rows.filter((row) => row.probed).length,
    fields_not_probed: notProbed,
    answer_passes: Math.max(...rows.map((row) => row.sentinels_served.length), 0),
    rows,
    leaked,
  });
  expect(
    leaked,
    `these server fields painted raw and need a displayVocabulary family: ${JSON.stringify(leaked)}`,
  ).toEqual([]);
  expect(
    rows.filter((row) => row.probed).length,
    "the answer carried none of the enum-shaped fields, so this arm measured nothing",
  ).toBeGreaterThan(0);
});
