/**
 * ORD-0222 -- the raw-enum leak was only half closed, and the gate that missed it was mine.
 *
 * The commander read four print points off `25165d9` and I had fixed one (`:2768-2769`).  HJ-P1's
 * two human readings of 「Check in · TRANSIT ARRIVAL锚点」 were not stale bundles: they were the
 * **other** day-card path.  The arbitration then refused to let a single whole-screen `innerText`
 * scan be the judge ("不能一边把这个量具判有罪，一边下一张单又让它当法官"), so the gate is two
 * layers, each with its own verdict:
 *
 *  - `VISUAL_TEXT_SCAN` -- text nodes that are actually on screen.  A node is excluded when it sits
 *    in `.v8-sr-only`, under `aria-hidden`, or computes to `display:none` / `visibility:hidden` /
 *    `opacity:0` / a zero box.  This is what a sighted traveller reads.
 *  - `ACCESSIBLE_TEXT_SCAN` -- everything an announced surface can carry: the same visible text
 *    **plus** the screen-reader-only regions and `aria-label` / `title` / `alt` values, minus
 *    `aria-hidden` and non-rendered nodes.  A raw enum here is a defect even though nobody sees it.
 *
 * Four nodes:
 *  1. zh visual scan over hub / live / map: no internal enum token anywhere on screen, and the rows
 *     this order touched (day-card mode+anchor, the ETA row, both route rows) carry **no Latin
 *     letters at all** -- "mixed fixed-English on these touched rows" is the failure the arbitration
 *     named, and a Chinese row with an English word in it fails that rule even when the word is not
 *     an enum;
 *  2. the accessible scan, same three screens, same enum list;
 *  3. a synthetic unknown (`SCOOTER_SHARE` / `PICK_UP_POINT`) must fail closed in Chinese to
 *     「未识别的出行方式」/「未识别的行程锚点」 instead of humanising a token into the screen;
 *  4. the side-by-side receipt the arbitration asked for before any "Check in"/"Overnight" fix:
 *     the raw `options[].days` payload next to the visible card text, with the two words traced to
 *     the field that carries them.  They are **server-authored activity titles**, not client enums,
 *     so they are recorded and handed off rather than stuffed into a mapping table -- and this file
 *     says so in the artefact, not only in the ledger.
 *
 * Mutation `killAA1` puts `:2677` back to raw `day.mode`; node 1 must go red.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-raw-enums-never-reach-the-traveller.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** Internal enum tokens: the product's own vocabulary, in any language setting. */
const ENUM_TOKENS = [
  "ARRIVAL",
  "DEPARTURE",
  "STAY",
  "CHECK_IN",
  "CHECK_OUT",
  "ANCHOR",
  "TRANSIT",
  "BICYCLE",
  "RIDE_HAIL",
  "WALK",
  "AUTO",
  "SCOOTER_SHARE",
  "PICK_UP_POINT",
];

/** Rows this order touched: they must be entirely in the traveller's language. */
const TOUCHED_ROWS = [
  { selector: ".v8-day-card small", name: "day-card mode + anchor", screens: ["hub"] },
  { selector: ".v8-eta span", name: "live ETA row", screens: ["live"] },
  { selector: '[data-testid="live-leave-by"]', name: "live leave-by slack", screens: ["live"] },
  { selector: ".v8-route-row", name: "route-evidence row", screens: ["live", "map"] },
  { selector: '[data-testid="studio-walking"]', name: "studio walking figure", screens: ["studio"] },
];

type Scan = { visual: string; accessible: string; visualNodes: number; accessibleNodes: number };

/** Read the screen twice: what is rendered, and what would be announced. */
async function scan(page: Page): Promise<Scan> {
  return await page.evaluate(() => {
    const rendered = (el: Element): boolean => {
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (el.closest('[aria-hidden="true"]')) return false;
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (Number(style.opacity) === 0) return false;
      return box.width > 0 && box.height > 0;
    };
    const srOnly = (el: Element): boolean => !!el.closest(".v8-sr-only");
    const visual: string[] = [];
    const accessible: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let next = walker.nextNode(); next; next = walker.nextNode()) {
      const el = (next as Text).parentElement;
      const text = (next.textContent ?? "").trim();
      if (!el || !text) continue;
      if (el.tagName === "SCRIPT" || el.tagName === "STYLE") continue;
      const isRendered = rendered(el);
      if (isRendered && !srOnly(el)) visual.push(text);
      if (isRendered) accessible.push(text);
    }
    for (const el of Array.from(document.querySelectorAll("[aria-label], [title], [alt]"))) {
      if (!rendered(el)) continue;
      for (const name of ["aria-label", "title", "alt"]) {
        const value = (el.getAttribute(name) ?? "").trim();
        if (value) accessible.push(value);
      }
    }
    const visualText = visual.join(" ");
    return {
      visual: visualText,
      accessible: accessible.join(" "),
      visualNodes: visual.length,
      accessibleNodes: accessible.length,
    };
  });
}

function enumHits(text: string): string[] {
  return ENUM_TOKENS.filter((token) =>
    new RegExp(`(^|[^A-Za-z0-9_])${token}([^A-Za-z0-9_]|$)`).test(text),
  );
}

async function openScreen(page: Page, route: "hub" | "live" | "map" | "studio"): Promise<void> {
  if (route === "hub") {
    await page.goto("/v8/hub", { waitUntil: "load" });
  } else {
    await page.evaluate((value) => window.history.pushState({}, "", `/v8/${value}`), route);
    await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  }
  await page.waitForTimeout(1_200);
  /* Expand what a traveller would expand.  The live route-evidence rows live inside a
     `<details>` and measured 0 visible until opened (`review/run-run078539.log`), so a scan that
     skipped collapsed disclosures would silently skip the rows this order is about. */
  await page.evaluate(() => {
    document.querySelectorAll("details").forEach((disclosure) => {
      disclosure.open = true;
    });
  });
  await page.waitForTimeout(300);
}

async function seedLocale(page: Page, locale: string): Promise<string> {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length > 0, node("seed", "the stage named no journey to read")).toBe(true);
  await page.addInitScript(
    ([id, value]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", value as string);
    },
    [journeyId, locale],
  );
  return journeyId;
}

test("ord0222-1-visual-scan-no-raw-enums-and-no-mixed-english — 旅客看得见的字里不许有内部枚举，被改的行不许中英混排", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-1-visual-scan-no-raw-enums-and-no-mixed-english";
  const journeyId = await seedLocale(page, "zh-CN");
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".v8-day-card").first()).toBeVisible({ timeout: 20_000 });

  const readings: Record<string, { visual: string; hits: string[]; mixed: { row: string; text: string }[]; seen: Record<string, number> }> = {};
  for (const route of ["hub", "live", "map", "studio"] as const) {
    await openScreen(page, route);
    const scanResult = await scan(page);
    expect(
      scanResult.visualNodes,
      node(NODE, `the visual scan found no rendered text on /v8/${route}, so its verdict is vacuous`),
    ).toBeGreaterThan(20);
    const hits = enumHits(scanResult.visual);
    const mixed: { row: string; text: string }[] = [];
    const seen: Record<string, number> = {};
    for (const row of TOUCHED_ROWS) {
      if (!row.screens.includes(route)) continue;
      seen[row.name] = 0;
      for (const locator of await page.locator(row.selector).all()) {
        if (!(await locator.isVisible())) continue;
        const text = (await locator.innerText()).replace(/\s+/g, " ").trim();
        if (!text) continue;
        seen[row.name] += 1;
        // A touched row in Chinese must not carry a Latin word: that is the "mixed fixed-English"
        // failure, and it catches the leak whether or not the word happens to be on the enum list.
        if (/[A-Za-z]{3,}/.test(text)) mixed.push({ row: row.name, text });
      }
    }
    expect(
      Object.entries(seen).filter(([, count]) => count === 0).map(([name]) => name),
      node(NODE, `/v8/${route} rendered none of these touched rows, so their verdict would be vacuous: ${JSON.stringify(seen)}`),
    ).toEqual([]);
    readings[route] = { visual: scanResult.visual.slice(0, 1200), hits, mixed, seen };
    expect(
      hits,
      node(NODE, `/v8/${route} renders internal enum tokens to the traveller: ${JSON.stringify(hits)} in "${scanResult.visual.slice(0, 300)}"`),
    ).toEqual([]);
    expect(
      mixed,
      node(NODE, `/v8/${route} still mixes fixed English into the rows this order touched: ${JSON.stringify(mixed)}`),
    ).toEqual([]);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0222-visual-scan.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0222 1", journeyId, enumList: ENUM_TOKENS, readings }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0222-2-accessible-scan-no-raw-enums — 读屏输出里也不许有内部枚举", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-2-accessible-scan-no-raw-enums";
  await seedLocale(page, "zh-CN");
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 30_000 });

  const perScreen: Record<string, { accessible: string; hits: string[]; nodes: number }> = {};
  for (const route of ["hub", "live", "map"] as const) {
    await openScreen(page, route);
    const scanResult = await scan(page);
    expect(
      scanResult.accessibleNodes,
      node(NODE, `the accessible scan found nothing announceable on /v8/${route}`),
    ).toBeGreaterThan(20);
    const hits = enumHits(scanResult.accessible);
    perScreen[route] = { accessible: scanResult.accessible.slice(0, 1200), hits, nodes: scanResult.accessibleNodes };
    expect(
      hits,
      node(NODE, `/v8/${route} announces internal enum tokens: ${JSON.stringify(hits)}`),
    ).toEqual([]);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0222-accessible-scan.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0222 2", perScreen }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0222-3-an-unknown-value-says-unknown-in-chinese — 新枚举进来时中文要 fail closed", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-3-an-unknown-value-says-unknown-in-chinese";
  const journeyId = await seedLocale(page, "zh-CN");
  /* Synthetic values the tables have never heard of.  The old fallback humanised the token and
     printed it anyway, which is the same leak one enum later. */
  await page.route("**/api/v1/journeys/*/experience", async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    const options = (body.options ?? []) as Array<Record<string, unknown>>;
    for (const option of options) {
      option.days = ((option.days ?? []) as Array<Record<string, unknown>>).map((day) => ({
        ...day,
        mode: "SCOOTER_SHARE",
        anchor: "PICK_UP_POINT",
      }));
    }
    body.options = options;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  const card = page.locator(".v8-day-card").first();
  await expect(card, node(NODE, "no day card rendered, so the unknown-value reading has no subject")).toBeVisible({
    timeout: 30_000,
  });
  const text = (await card.innerText()).replace(/\s+/g, " ");
  expect(text, node(NODE, `an unknown mode did not fail closed in Chinese: "${text}"`)).toContain("未识别的出行方式");
  expect(text, node(NODE, `an unknown anchor did not fail closed in Chinese: "${text}"`)).toContain("未识别锚点");
  expect(
    text,
    node(NODE, `the unknown anchor doubled the noun the template already supplies: "${text}"`),
  ).not.toContain("锚点锚点");
  expect(
    enumHits(text),
    node(NODE, `the synthetic internal token reached the screen instead of a named unknown: ${JSON.stringify(enumHits(text))} in "${text}"`),
  ).toEqual([]);
  writeFileSync(
    evidencePath("ui-overnight/ord0222-unknown-value.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0222 ③", journeyId, synthetic: { mode: "SCOOTER_SHARE", anchor: "PICK_UP_POINT" }, cardText: text }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0222-5-dark-scheme-carries-the-same-words — 同一批屏在深色下再扫一遍", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-5-dark-scheme-carries-the-same-words";
  await seedLocale(page, "zh-CN");
  await page.emulateMedia({ colorScheme: "dark", media: "screen" });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 30_000 });
  const dark: Record<string, string[]> = {};
  for (const route of ["hub", "live", "map"] as const) {
    await openScreen(page, route);
    const scanResult = await scan(page);
    const hits = enumHits(scanResult.visual);
    dark[route] = hits;
    expect(
      scanResult.visualNodes,
      node(NODE, `/v8/${route} in dark rendered no text, so the sweep is vacuous`),
    ).toBeGreaterThan(20);
    expect(
      hits,
      node(NODE, `/v8/${route} in dark renders internal enum tokens: ${JSON.stringify(hits)}`),
    ).toEqual([]);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0222-dark-scan.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0222", scheme: "dark", perScreen: dark }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0222-4-day-payload-versus-visible-card — Check in / Overnight 的来源先交并排收据", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-4-day-payload-versus-visible-card";
  const journeyId = await seedLocale(page, "zh-CN");
  const experience = (await (await page.request.get(`/api/v1/journeys/${journeyId}/experience`)).json()) as {
    options?: Array<{ id: string; days?: Array<Record<string, unknown>> }>;
  };
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.locator(".v8-day-card").first()).toBeVisible({ timeout: 30_000 });
  const cards = [];
  for (const card of await page.locator(".v8-day-card").all()) {
    cards.push({
      heading: ((await card.locator("strong").innerText()) ?? "").trim(),
      activities: ((await card.locator("p").innerText()) ?? "").replace(/\s+/g, " ").trim(),
      modeAnchorRow: ((await card.locator("small").innerText()) ?? "").replace(/\s+/g, " ").trim(),
    });
  }
  const rawDays = (experience.options?.[0]?.days ?? []).map((day) => ({
    label: day.label,
    date: day.date,
    anchor: day.anchor,
    mode: day.mode,
    activities: day.activities,
  }));
  // Where do the two words live?  Read the fields, do not guess.
  const carries = (needle: string) =>
    rawDays
      .map((day, index) => ({
        day: index + 1,
        inLabel: String(day.label ?? "").includes(needle),
        inActivities: (day.activities as string[] | undefined)?.some((item) => item.includes(needle)) ?? false,
      }))
      .filter((row) => row.inLabel || row.inActivities);
  const receipt = {
    generatedBy: "e345b9d6",
    order: "ORD-0222 ②",
    journeyId,
    rawDays,
    visibleCards: cards,
    sourceOf: { "Check in": carries("Check in"), Overnight: carries("Overnight") },
    boundary:
      "mode/anchor are client enums and are mapped here; `Check in`/`Overnight` arrive inside `options[].days[].activities[]` as server-authored display strings, and the weekday arrived in `days[].label`. The weekday is a form and is now rendered from `days[].date`; the activity titles are content and are handed to the plan projection rather than rewritten in the client.",
  };
  expect(
    cards.length,
    node(NODE, "no day card rendered, so there is nothing to compare against the payload"),
  ).toBeGreaterThan(0);
  expect(
    JSON.stringify(receipt.sourceOf["Check in"]).length > 2 && JSON.stringify(receipt.sourceOf.Overnight).length > 2,
    node(NODE, `the receipt could not locate the two words in any payload field: ${JSON.stringify(receipt.sourceOf)}`),
  ).toBe(true);
  writeFileSync(
    evidencePath("ui-overnight/ord0222-days-side-by-side.json"),
    JSON.stringify(receipt, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0222-days-side-by-side.png"), fullPage: true });
});
