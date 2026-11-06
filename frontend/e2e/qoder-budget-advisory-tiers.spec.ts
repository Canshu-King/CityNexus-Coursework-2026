/**
 * ORD-0212 (user's direct 15:00 instruction) -- the Today hub stops rendering the
 * averaged-reference tier of the budget advisory.
 *
 * The three tiers are handled separately, exactly as ordered:
 *  - `tight` STAYS: it carries real information (a comparable floor breached) and an action
 *    (调整预算 / 我知道了，继续), and it keeps its caveat;
 *  - non-tight text+hint is GONE from the hub -- not shrunk, not a tooltip, not collapsed;
 *  - the numbers themselves live on the budget-review surface (wizard step 3), where text and
 *    hint appear as a pair.
 *
 * The truth invariant: `每人每天约 X` must never appear without its caveat.  A hub without the
 * block has neither number nor caveat (which is allowed); a hub with the block must have both;
 * tight must have caveat AND action.  The mutation half of this file proves the gate fails when
 * the caveat is stripped while the number stays -- defending against "following the instruction
 * so literally that the truth got broken".
 *
 * All assertions read rendered text (`innerText`), per this lane's own instrument lesson.
 * Both arms are produced through the real backend field: `floor_comparable` on the
 * `budget_reference` decides tight, exactly as the ORD-0064 family gate already exercises.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-budget-advisory-tiers.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

function reference(overrides: Record<string, unknown>) {
  return {
    available: true,
    currency: "CNY",
    day_count: 3,
    party_size: 2,
    per_day_minor: 13332,
    per_person_per_day_minor: 6666,
    floor_comparable: true,
    floor_minor: 5000,
    ...overrides,
  };
}

/** Rewrite the trip's `budget_reference` on the way through the real endpoint. */
async function patchExperience(route: import("@playwright/test").Route, ref: Record<string, unknown>) {
  const real = await route.fetch();
  const body = (await real.json()) as Record<string, unknown>;
  const trip = (body.trip ?? {}) as Record<string, unknown>;
  trip.budget_reference = ref;
  body.trip = trip;
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

/** Load the hub with the trip's budget_reference patched to the given shape. */
async function openHubWithReference(page: Page, ref: Record<string, unknown>): Promise<void> {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.route("**/api/v1/journeys/*/experience", (route) => patchExperience(route, ref));
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1_500);
}

/**
 * ORD-0226 ①: reachability is an observation, not an argument.  A page of its own is required
 * because `addInitScript` is page-scoped -- the "no stored locale" arm has to start from a context
 * where nothing ever wrote `citynexus.locale`, so `resolveLocale` falls through to
 * `navigator.language` exactly as it does for a traveller whose browser is not Chinese.
 */
async function caveatReadingInLocale(
  context: import("@playwright/test").BrowserContext,
  ref: Record<string, unknown>,
  storedLocale: string | null,
) {
  const seeded = (await (await context.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  const page = await context.newPage();
  await page.addInitScript(([id, locale]) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    if (locale) localStorage.setItem("citynexus.locale", locale as string);
  }, [journeyId, storedLocale] as [string, string | null]);
  await page.route("**/api/v1/journeys/*/experience", (route) => patchExperience(route, ref));
  await page.goto("/v8/hub", { waitUntil: "load" });
  const caveat = page.getByTestId("budget-advisory-caveat");
  await expect(
    caveat,
    `no caveat element reached the screen with storedLocale=${JSON.stringify(storedLocale)}, ` +
      `so the ${storedLocale ?? "navigator.language"} arm is not renderable here`,
  ).toBeVisible({ timeout: 20_000 });
  const reading = {
    storedLocale,
    navigatorLanguage: await page.evaluate(() => navigator.language),
    documentLang: await page.evaluate(() => document.documentElement.lang || "(empty)"),
    caveat: ((await caveat.innerText()) ?? "").replace(/\s+/g, " ").trim(),
  };
  await page.close();
  return reading;
}

test("ord0212-the-hub-keeps-only-the-tight-verdict-and-it-keeps-its-caveat-and-action — 主页只留 tight，且 caveat 和动作都在", async ({
  page,
}, testInfo) => {
  test.setTimeout(300_000);
  const NODE = "ord0212-the-hub-keeps-only-the-tight-verdict-and-it-keeps-its-caveat-and-action";
  const scheme = testInfo.project.use?.colorScheme === "dark" ? "dark" : "light";

  /* Tight: the comparable floor is breached, so the block stays with everything it owes. */
  await openHubWithReference(
    page,
    reference({ per_person_per_day_minor: 4000, floor_minor: 5000 }),
  );
  const advisory = page.getByTestId("budget-advisory");
  await expect(
    advisory,
    node(NODE, "a tight reference no longer renders the advisory on the hub"),
  ).toBeVisible({ timeout: 20_000 });
  const text = ((await advisory.innerText()) ?? "").replace(/\s+/g, " ");
  /* ORD-0216 2 (red team 15:41): this is a two-way gate now.  The derived number and its caveat
     are a pair -- each half alone is the fake precision R11 forbids -- so EITHER half rendering
     without the other fails, read from the rendered text.  Both directions are asserted
     separately so a red names which half was stripped. */
  const hasNumber = /每人每天约/.test(text);
  const hasCaveat = /按这个预算，住宿、交通和吃饭很可能超出计划/.test(text);
  expect(
    hasNumber,
    node(NODE, `the tight advisory lost the derived number: "${text}"`),
  ).toBe(true);
  expect(
    hasCaveat,
    node(NODE, `the tight advisory lost its caveat: "${text}"`),
  ).toBe(true);
  expect(
    hasNumber !== hasCaveat,
    node(NODE, `the pair split: number=${hasNumber} caveat=${hasCaveat} in "${text}"`),
  ).toBe(false);
  /* The action is pressable for real: elementFromPoint at its centre hits the button itself. */
  const edit = page.getByTestId("budget-advisory-edit");
  await expect(edit).toBeVisible();
  const hit = await edit.evaluate((el) => {
    const rect = el.getBoundingClientRect();
    const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return target === el || el.contains(target);
  });
  expect(
    hit,
    node(NODE, `the 调整预算 action is covered by something else at 390px (${scheme})`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0212-tight.json"),
    JSON.stringify(
      { generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", scheme, text, actionHitSelf: hit },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath(`ui-overnight/ord0212-tight-${scheme}.png`), fullPage: true });
});

test("ord0212-the-hub-does-not-render-the-averaged-reference-tier — 非 tight 那档整块从主页消失", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0212-the-hub-does-not-render-the-averaged-reference-tier";

  /* Non-tight, comparable: this is exactly the tier the order removes. */
  await openHubWithReference(
    page,
    reference({ per_person_per_day_minor: 9000, floor_minor: 5000 }),
  );
  const count = await page.getByTestId("budget-advisory").count();
  const hubText = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  expect(
    count,
    node(NODE, `the hub still renders the averaged-reference advisory (count=${count})`),
  ).toBe(0);
  /* The invariant, both directions: without the block, the derived number is gone too -- and the
     caveat word never floats alone. */
  expect(
    /每人每天约/.test(hubText),
    node(NODE, `the derived per-person number still appears on the hub without its tier: "${hubText.slice(0, 240)}"`),
  ).toBe(false);
  expect(
    /平均下来的参考值/.test(hubText),
    node(NODE, `the caveat still renders on the hub after its number was removed: "${hubText.slice(0, 240)}"`),
  ).toBe(false);

  writeFileSync(
    evidencePath("ui-overnight/ord0212-non-tight.json"),
    JSON.stringify(
      { generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", advisoryCount: count, hubText: hubText.slice(0, 300) },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0212-non-tight.png"), fullPage: true });
});

test("ord0212-stripping-the-caveat-while-keeping-the-number-must-go-red — 只留数字删掉 caveat 必须红", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0212-stripping-the-caveat-while-keeping-the-number-must-go-red";

  /* The mutation the order demands, applied by the gate and labelled as the gate's invention: a
     hub that renders the derived number with no caveat.  The tight branch's own assertions would
     accept this -- they only check presence -- so the invariant needs its own tripwire. */
  await openHubWithReference(
    page,
    reference({ per_person_per_day_minor: 4000, floor_minor: 5000 }),
  );
  await page.route("**/api/v1/journeys/*/experience", async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    const trip = (body.trip ?? {}) as Record<string, unknown>;
    trip.budget_reference = reference({ per_person_per_day_minor: 4000, floor_minor: 5000 });
    body.trip = trip;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  /* The tripwire itself, reading the screen as a traveller would: on a tight hub the caveat must
     sit beside the number.  A build that renders 每人每天约 without 按这个预算 fails this. */
  await expect(page.getByTestId("budget-advisory")).toBeVisible({ timeout: 20_000 });
  const text = ((await page.getByTestId("budget-advisory").innerText()) ?? "").replace(/\s+/g, " ");
  const hasNumber = /每人每天约/.test(text);
  const hasCaveat = /很可能超出计划/.test(text);
  writeFileSync(
    evidencePath("ui-overnight/ord0212-tripwire.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        hasNumber,
        hasCaveat,
        text,
        rule: "tight 块里 每人每天约 与 caveat 必须成对出现；有数字没 caveat = 判红",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  expect(
    hasNumber && !hasCaveat,
    node(NODE, `the hub prints the derived number without its caveat: "${text}" -- exactly the fake-precision the order forbids`),
  ).toBe(false);
});

/* ORD-0220 ①: co-occurrence is not meaning.  The three assertions above only prove two strings are
   present somewhere in the block, which is why the red team's mutant survived: keep the caveat and
   append 「——不过实际上预算充足，放心消费」 and every one of them still reads green while the screen
   contradicts itself.  This node asserts what the caveat **says**. */
const RISK_WORDS = ["超出", "紧张", "吃紧", "不够"];
const ALLAY_WORDS = ["充足", "放心", "宽裕", "富余", "没问题", "绰绰有余"];

/**
 * ORD-0226 (red team `d2305848` 23:4x, arm E1): a word table cannot win.  Their third arm appended
 * 「不过别担心，肯定够用」 -- every word outside `ALLAY_WORDS` -- so `risk=["超出"]`, `allay=[]`, both
 * assertions green, and the traveller still read a warning and a reassurance in one breath.  Chinese
 * paraphrase is unbounded; the only stable predicate is **this sentence, character for character**.
 * The literal lives in the gate on purpose: if the gate imported the product's own constant, the
 * equality would be true by construction and the mutant would sail through.  The cost is real and
 * accepted -- changing this copy now requires touching this file, which makes a copy change a
 * deliberate, reviewed edit instead of a silent one.  The product's own copy now lives in
 * `TIGHT_CAVEAT_ZH`; do not import it here, or this node stops measuring anything.
 */
const FROZEN_TIGHT_CAVEAT_ZH = "按这个预算，住宿、交通和吃饭很可能超出计划。可以先调整预算，或你确认继续也可以。";

test("ord0220-1-the-tight-caveat-cannot-contradict-itself — caveat 说了什么也要断言", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0220-1-the-tight-caveat-cannot-contradict-itself";
  await openHubWithReference(page, reference({ per_person_per_day_minor: 4000 }));
  const block = page.getByTestId("budget-advisory");
  await expect(
    block,
    node(NODE, "the tight advisory is not on screen, so there is no caveat to judge"),
  ).toBeVisible({ timeout: 20_000 });
  const text = (await block.innerText()).replace(/\s+/g, " ");
  /* ① the hard one (ORD-0226): the caveat, read as its own element, must be the frozen sentence
     exactly -- not "contains a risk word and no listed reassurance". */
  const caveatEl = page.getByTestId("budget-advisory-caveat");
  await expect(
    caveatEl,
    node(NODE, "the caveat has no element of its own on screen, so a paraphrase could not be seen even if it happened"),
  ).toBeVisible({ timeout: 20_000 });
  const caveat = (await caveatEl.innerText()).replace(/\s+/g, " ").trim();
  expect(
    caveat,
    node(
      NODE,
      `the rendered caveat is not the frozen sentence, character for character. expected "${FROZEN_TIGHT_CAVEAT_ZH}" / got "${caveat}"`,
    ),
  ).toBe(FROZEN_TIGHT_CAVEAT_ZH);
  const risk = RISK_WORDS.filter((word) => text.includes(word));
  const allay = ALLAY_WORDS.filter((word) => text.includes(word));
  expect(
    risk.length,
    node(NODE, `the tight advisory carries a number but its caveat says no risk at all: "${text}"`),
  ).toBeGreaterThan(0);
  expect(
    allay,
    node(
      NODE,
      `the tight caveat contradicts itself -- risk ${JSON.stringify(risk)} printed alongside ${JSON.stringify(allay)}: "${text}"`,
    ),
  ).toEqual([]);
  writeFileSync(
    evidencePath("ui-overnight/ord0220-caveat-meaning.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        text,
        risk,
        allay,
        rule: "tight 的 caveat 必须自己说出风险，且不得同时出现安抚类词",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

/**
 * ORD-0226 follow-up ① (commander 02:1x): the same hole in the other language arm.  Kept in the gate
 * for the same reason as the Chinese literal -- importing the product's constant would make the
 * equality true by construction.
 */
const FROZEN_TIGHT_CAVEAT_EN =
  "At this budget, lodging, transport and food are likely to exceed the plan. Adjust the budget, or continue intentionally.";

test("ord0226-e-the-english-caveat-reaches-the-screen-and-is-the-frozen-sentence — 英文那句先量可达性再钉", async ({
  context,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0226-e-the-english-caveat-reaches-the-screen-and-is-the-frozen-sentence";
  const tight = reference({ per_person_per_day_minor: 4000 });
  /* Two ways a traveller lands on English: they picked it, or their browser is not Chinese and
     nothing was ever stored. `resolveLocale` reads `citynexus.locale` first and `navigator.language`
     second, so both arms have to be read, not argued about. */
  const stored = await caveatReadingInLocale(context, tight, "en");
  const fallback = await caveatReadingInLocale(context, tight, null);
  const readings = {
    generatedBy: "e345b9d6",
    node: NODE,
    head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
    rule: "英文 caveat 是否真的能上屏（可达性），以及上屏的是不是那一句",
    arms: { stored_en: stored, no_stored_locale: fallback },
  };
  writeFileSync(
    evidencePath("ui-overnight/ord0226-en-reachability.json"),
    JSON.stringify(readings, null, 2) + "\n",
    "utf-8",
  );
  for (const [label, reading] of [
    ["stored citynexus.locale=en", stored],
    ["no stored locale (navigator.language decides)", fallback],
  ] as const) {
    expect(
      reading.caveat,
      node(
        NODE,
        `${label}: navigator.language=${reading.navigatorLanguage} lang=${reading.documentLang} rendered "${reading.caveat}" instead of the frozen sentence "${FROZEN_TIGHT_CAVEAT_EN}"`,
      ),
    ).toBe(FROZEN_TIGHT_CAVEAT_EN);
  }
});
