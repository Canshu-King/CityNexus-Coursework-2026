/**
 * ORD-0216 ④ — the sealed-demo entry must not name a city the server has not chosen.
 *
 * HJ-P1 read 「使用封存的天津演示」 on a stage whose sealed journey is the Guangzhou one
 * (commander's 亲测 block: `HUMAN_MISLEADING`).  The entry is not Tianjin-specific: which
 * journey it hands over is decided by `GET /api/v1/demo/scene` from server data -- the journey
 * an ACTIVE plan points at, else the most recently seeded one (`backend/app/main.py`).
 * `review/probe-ord0216-scene-city.py` measured that rule on a private stage: with a Tianjin
 * journey holding an active plan plus a later Guangzhou DRAFT, the scene still names Tianjin, so
 * the choice is data, not a constant this file may copy into a label.
 *
 * Fix: the copy is city-neutral ("使用封存的演示行程" / "Use the sealed demo trip"), which is
 * option 1 of the two the order allowed.  Option 2 -- pinning the entry to the Tianjin journey --
 * would make the front end overrule the server's own choice, and renaming the label to
 * "Guangzhou" was ruled out up front as moving the inconsistency rather than removing it.
 *
 * Arms, and what each one can actually see:
 *  A/B (zh, en)  no stub, real stage: every traveller-visible sentence on the wizard screen is
 *                read as rendered `innerText` and must contain **no city name at all**.  A
 *                placeholder ("例如：天津、东京、新加坡") is not rendered text and stays allowed --
 *                it is an input example, not a claim about the sealed demo.
 *  C            the landed city must come from data: a Guangzhou journey is created through the
 *                product API (a real row, DRAFT, so the stage's own scene is untouched), and only
 *                the *choice* in `/demo/scene` is re-aimed at it inside this node.  The hub then
 *                has to render 广州 from `journey.city_code`, and the entry copy the traveller
 *                tapped must not have named a different city.  The fixture is the pointer; the
 *                landing page, the city label and the copy are all product truth.
 *
 * Mutation (killW1, see `review/ord0082_0087_kill_drivers.py`): restoring 「使用封存的天津演示」
 * turns arm A red.  That is the whole point of the arm -- it is not satisfied by deleting text.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-demo-entry-copy-matches-the-stage.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** The cities this product ships names for (`CITY_NAME_BY_SLUG`, V8Experience.tsx). */
const CITY_NAMES = [
  "天津",
  "Tianjin",
  "广州",
  "Guangzhou",
  "厦门",
  "Xiamen",
  "北京",
  "Beijing",
  "上海",
  "Shanghai",
  "深圳",
  "Shenzhen",
  "成都",
  "Chengdu",
  "杭州",
  "Hangzhou",
  "福州",
  "Fuzhou",
  "东京",
  "Tokyo",
  "新加坡",
  "Singapore",
];

/** Cities named inside a piece of rendered text: CJK matches as a substring, Latin on word ends. */
function citiesIn(text: string): string[] {
  return CITY_NAMES.filter((name) =>
    /[一-鿿]/.test(name) ? text.includes(name) : new RegExp(`(^|[^a-zA-Z])${name}([^a-zA-Z]|$)`, "i").test(text),
  );
}

async function openWizard(page: Page, locale: string): Promise<void> {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
}

async function wizardText(page: Page): Promise<string> {
  return (await page.getByTestId("trial-setup").innerText()) ?? "";
}

async function entryCopy(page: Page): Promise<string> {
  return (await page.getByTestId("trial-tianjin-demo").innerText()) ?? "";
}

test("ord0216d-zh-the-wizard-never-says-a-city-name — 中文向导整屏渲染文字里不许出现任何城市名", async ({
  page,
}) => {
  await openWizard(page, "zh-CN");
  const text = await wizardText(page);
  const label = await entryCopy(page);
  const named = citiesIn(text);
  expect(
    named,
    node(
      "ord0216d-zh",
      `the wizard screen names cities in traveller-visible copy (${JSON.stringify(named)}); which sealed journey the entry opens is the server's choice, so any city here can be wrong on another stage. Entry label: "${label}"`,
    ),
  ).toEqual([]);
  // Substance check: neutrality must not be achieved by removing the entry's meaning.
  expect(
    /封存|演示/.test(label),
    node("ord0216d-zh-substance", `the entry stopped saying what it is: "${label}"`),
  ).toBe(true);
  writeFileSync(
    evidencePath("ui-overnight/ord0216-entry-copy-zh.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0216 ④", locale: "zh-CN", label, named, screen: text.slice(0, 400) }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0216-entry-copy-zh.png"), fullPage: true });
});

test("ord0216d-en-the-wizard-never-says-a-city-name — 英文向导同一屏也不能出现城市名", async ({
  page,
}) => {
  await openWizard(page, "en-GB");
  const text = await wizardText(page);
  const label = await entryCopy(page);
  const named = citiesIn(text);
  expect(
    named,
    node(
      "ord0216d-en",
      `the English wizard screen names cities in traveller-visible copy (${JSON.stringify(named)}). Entry label: "${label}"`,
    ),
  ).toEqual([]);
  expect(
    /sealed|demo/i.test(label),
    node("ord0216d-en-substance", `the entry stopped saying what it is: "${label}"`),
  ).toBe(true);
});

test("ord0216d-the-city-the-traveller-lands-on-comes-from-data — 入口不说城市，落地页必须把服务器那趟的城市给出来", async ({
  page,
}) => {
  /* A real Guangzhou journey, created through the product API.  It stays DRAFT with no plan
     version, so the stage's own `/demo/scene` still resolves to the seeded journey for every
     other spec in this chain; only this node re-aims the pointer at it. */
  const traveler = await page.request.post("/api/v1/travelers", {
    data: { display_name: "ORD-0216 ④ copy probe", locale: "zh-CN", timezone: "Asia/Shanghai" },
  });
  expect(traveler.ok(), node("ord0216d-lands", `POST /travelers -> ${traveler.status()}`)).toBe(true);
  const created = await page.request.post("/api/v1/journeys", {
    data: {
      traveler_id: (await traveler.json()).id,
      title: "Guangzhou probe journey",
      city_code: "CN-GUANGZHOU",
      timezone: "Asia/Shanghai",
      start_at: "2026-11-06T18:00:00+08:00",
      end_at: "2026-11-08T15:00:00+08:00",
      budget: { amount_minor: 120000, currency: "CNY" },
    },
  });
  expect(created.ok(), node("ord0216d-lands", `POST /journeys -> ${created.status()}: ${(await created.text()).slice(0, 160)}`)).toBe(true);
  const probeId: string = (await created.json()).id;

  const sceneResponse = await page.request.get("/api/v1/demo/scene");
  expect(sceneResponse.ok(), node("ord0216d-lands", `GET /demo/scene -> ${sceneResponse.status()}`)).toBe(true);
  const realScene = await sceneResponse.json();
  expect(
    realScene.demo_mode,
    node("ord0216d-lands", `this chain must run a demo stage for the entry to answer at all (demo_mode=${realScene.demo_mode})`),
  ).toBe(true);
  const stageJourneyId: string | null = realScene.journey_id ?? null;

  // Re-aim only the pointer; every other field is whatever the server actually answered.
  await page.route("**/api/v1/demo/scene", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...realScene, journey_id: probeId }),
    });
  });

  await openWizard(page, "zh-CN");
  const label = await entryCopy(page);
  const claimed = citiesIn(label);
  await page.getByTestId("trial-tianjin-demo").click();
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 30_000 });
  const heading = (await page.getByTestId("today-heading").innerText()) ?? "";

  // The hub's city is projected from `journey.city_code`, so this reads as 广州 without any of it
  // being written by the test.
  expect(
    heading,
    node(
      "ord0216d-lands",
      `the entry handed over a journey whose city is not on screen: heading "${heading.trim()}" for ${probeId} (the stage's own sealed journey is ${stageJourneyId})`,
    ),
  ).toContain("广州");
  // The inconsistency HJ-P1 saw, stated as a rule: any city the entry claims must be the city the
  // traveller actually arrives at.  With the fix the claim set is empty, so nothing can contradict.
  const wrong = claimed.filter((city) => !heading.includes(city));
  expect(
    wrong,
    node(
      "ord0216d-copy-matches-landing",
      `the entry says ${JSON.stringify(claimed)} but the traveller lands on "${heading.trim()}" (${JSON.stringify(wrong)} are not what the server opened)`,
    ),
  ).toEqual([]);
  writeFileSync(
    evidencePath("ui-overnight/ord0216-entry-copy-landing.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0216 ④",
        fixture: "only /demo/scene's journey_id is re-aimed; the journey row, its city_code and the hub render are product truth",
        stageJourneyId,
        probeJourneyId: probeId,
        label,
        claimedCitiesInLabel: claimed,
        landingHeading: heading.trim(),
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0216-entry-copy-landing.png"), fullPage: true });
});
