/**
 * ORD-0325 · Q64=① 的三格落地：服务器原话不上屏，这台设备量到的字段仍在屏
 *
 * 三格（要件点名的三处）：
 *   T-A `frontend/src/features/stay/HotelsPanel.tsx`      —— 422/404 的 `detail` 句子直接进 `setError`
 *   T-B `frontend/src/features/planning/PlanComparison.tsx` —— `body?.error?.message ?? body?.detail`
 *   T-C `frontend/src/app/V8Experience.tsx`               —— banner 印 `error.message`，而 message 由
 *       `rejectionReason` 把服务器的 `detail` / `error.message` / 每条 `loc: msg` 原样拼进去
 *
 * 每格两问，缺一不可：
 *   负向 —— 上游那句话（哨兵 LEAK）与一句形似凭据的假 token（JWT，跟着正文一起进来）不得出现在
 *           `document.body.innerText` 里。读的是渲染文字，不是 data-* 属性。
 *   正向 —— 这台设备量到的三行 `http_status=` / `content-type=` / `bytes=` 必须在屏，并且 `bytes` 要
 *           等于本节点 fulfill 出去的那段正文的 UTF-8 字节数：读数是真的量出来的，不是写死的。
 *
 * 还有一枚正控（A2）：422 的 `detail` 以 `STAY_ITINERARY_CONFLICT` 打头时，屏幕上必须仍是那句"与行程
 * 冲突"的话。拆掉冻结码表就会红 —— 这一枚证明我没有把"不许上屏"修成"分支也不认了"。
 *
 * 三格各自另测两种语言（A1 中文 / A3 英文 / B1 中文），因为两半文案是两条独立的路。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0325-upstream-prose-not-on-screen.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();

/** 服务器"原话"的替身。改前它会出现在屏上，改后它一个字符都不许出现。 */
const LEAK = "ORD0325-the-servers-own-sentence-请别把我画上屏";
/** 形似凭据的一段（跟着正文进来，屏上不该看得见它）。 */
const JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.ORD0325.not-a-real-token";

const refusalBody = (extra: Record<string, unknown> = {}) =>
  JSON.stringify({
    detail: `${LEAK} ${JWT}`,
    error: {
      code: "VALIDATION_ERROR",
      message: LEAK,
      details: { errors: [{ loc: ["body", "budget"], msg: `${LEAK}（字段级原文）`, type: "value_error" }] },
      ...extra,
    },
  });

const bytesOf = (text: string) => new TextEncoder().encode(text).length;

/** The three lines this device owes on screen, recomputed from what the test itself sent. */
const expectedFacts = (status: number, text: string) => [
  `http_status=${status}`,
  "content-type=application/json",
  `bytes=${bytesOf(text)}`,
];

async function refuse(route: Route, status: number, text: string) {
  await route.fulfill({ status, contentType: "application/json", body: text });
}

/** Read the screen the way a traveller's screenshot would: rendered text of the whole page. */
async function visibleText(page: Page) {
  return clean(await page.evaluate(() => document.body.innerText));
}

/** Assert both halves for one cell, and hand back what was actually on screen. */
async function assertTwoWay(
  page: Page,
  arm: string,
  sentenceTestId: string,
  factsTestId: string,
  status: number,
  sentBody: string,
  sentence: string,
) {
  const pageText = await visibleText(page);
  for (const forbidden of [LEAK, JWT]) {
    expect(
      pageText.includes(forbidden),
      node(arm, `上游正文仍在了屏上（哨兵 ${forbidden.slice(0, 24)}…）：${pageText.slice(0, 400)}`),
    ).toBe(false);
  }
  const facts = page.getByTestId(factsTestId);
  await expect(facts, node(arm, "白名单读数块没有出现在屏上")).toBeVisible();
  const factsText = clean(await facts.innerText());
  for (const line of expectedFacts(status, sentBody)) {
    expect(factsText.includes(line), node(arm, `读数块缺了「${line}」，实读=${factsText}`)).toBe(true);
  }
  /* The attribution label is what keeps the block from being read as the server's own words, so it is
     asserted as rendered text -- a data-* attribute would prove nobody can see it. */
  const label = clean(await page.getByTestId(`${factsTestId}-label`).innerText());
  expect(
    label.includes("这台设备自己量到的回读字段") || label.includes("Fields this device measured"),
    node(arm, `归因标签没有说清这是谁的读数，实读=${label}`),
  ).toBe(true);
  const said = clean(await page.getByTestId(sentenceTestId).innerText());
  expect(
    said.includes(sentence),
    node(arm, `屏幕没说自己那句人话，实读=${said.slice(0, 300)}`),
  ).toBe(true);
  return {
    page_text: pageText,
    facts_text: factsText,
    attribution_label: label,
    screen_sentence: said,
    expected_lines: expectedFacts(status, sentBody),
    sent_bytes: bytesOf(sentBody),
  };
}

async function openWithJourney(page: Page, path: string, anchorTestId: string, locale = "zh-CN") {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length > 0, node("stage", "舞台没有已点名的旅程")).toBe(true);
  await page.addInitScript(
    ([id, loc]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", loc as string);
    },
    [journeyId, locale],
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(path, { waitUntil: "load" });
  /* The anchor is the page's own surface, not Today's heading -- `today-heading` exists only on the
     hub route, and waiting for it on /v8/trips is how the first round of this node reported an
     absent shell when the shell was simply on another route. */
  await expect(page.getByTestId(anchorTestId), node("stage", `${path} 没有渲染出 ${anchorTestId}`)).toBeVisible({
    timeout: 30_000,
  });
  return journeyId;
}

/**
 * Add one stay option only when the trip genuinely has none.  The read stays the real endpoint's; this
 * never removes or rewrites a row the server did send (same `patchExperience` idiom as
 * `qoder-budget-advisory-tiers.spec.ts`).
 */
function stayOptionsRoute(arm: string) {
  return async (route: Route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    const options = Array.isArray(body.options) ? (body.options as unknown[]) : [];
    if (options.length === 0) {
      body.options = [
        {
          id: "ord0325-stay-1",
          rank: 1,
          status: "AVAILABLE",
          stay_total_minor: 64000,
          walking_min: 7,
          protected_margin_min: 45,
          arrival_fit: 88,
          departure_fit: 91,
          cancellation_score: 2,
          hard_constraints_passed: true,
          stay: {
            property_name: "ORD0325 测试酒店",
            room_type: "twin",
            total_amount_minor: 64000,
            nightly_amount_minor: 32000,
            currency: "CNY",
            refundable: true,
            location: { latitude: 39.1, longitude: 117.2 },
          },
        },
      ];
      body.selected_option_id = null;
    }
    await route.fulfill({
      status: real.status(),
      contentType: real.headers()["content-type"] ?? "application/json",
      body: JSON.stringify(body),
    });
    void arm;
  };
}

const readings: Record<string, unknown> = {};

/** Land on Trips -> the stay tab, with the experience read patched so a hotel card exists. */
async function openHotels(page: Page, arm: string, locale = "zh-CN") {
  await openWithJourney(page, "/v8/trips", "trips-tabs", locale);
  await page.route(/\/api\/v1\/journeys\/[^/]+\/experience(\?|$)/, stayOptionsRoute(arm));
  await page.reload({ waitUntil: "load" });
  await page.getByTestId("trips-tab-stay").click();
  /* The stage's own trip already holds stay options, and one of them is the option this trip
     selected -- that card's button renders `disabled` with 「已选择这家」. Clicking a disabled button is
     how the first round of this node burned its whole timeout, so the target is an enabled card. */
  const choose = page.locator('[data-testid="hotel-choose"]:not([disabled])').first();
  await expect(choose, node(arm, "没有一张住宿卡片带着可点的按钮")).toBeVisible({ timeout: 40_000 });
  await choose.scrollIntoViewIfNeeded();
  const cards = await page.getByTestId("hotel-card").count();
  const alreadyChosen = await page.locator('[data-testid="hotel-choose"][disabled]').count();
  return { choose, cards, disabled_cards: alreadyChosen };
}

/* ------------------------------------------------------------------ T-A 住宿 */

test("A1 住宿被拒（未映射的 422）：原话不上屏、三行读数在屏 — 中文半边", async ({ page }) => {
  test.setTimeout(180_000);
  const arm = "T-A1";
  const { choose, cards, disabled_cards } = await openHotels(page, arm);
  const body = refusalBody();
  await page.route(/\/api\/v1\/journeys\/[^/]+\/stay-options\/[^/]+\/select(\?|$)/, (route) =>
    refuse(route, 422, body),
  );
  await choose.click();
  await expect(page.getByTestId("hotel-error"), node(arm, "拒绝句没有上屏")).toBeVisible({ timeout: 20_000 });
  readings[arm] = await assertTwoWay(
    page,
    arm,
    "hotel-error",
    "hotel-raw-facts",
    422,
    body,
    "暂时无法选择这家住宿。",
  );
  readings[arm].reached_by = {
    hotel_cards_on_screen: cards,
    cards_already_chosen: disabled_cards,
    note: "舞台上这趟自己就有住宿卡片，补位分支没被走到",
  };
});

test("A2 住宿冲突码仍由 token 决定（正控）：换成行程冲突句，且原话仍不上屏", async ({ page }) => {
  test.setTimeout(180_000);
  const arm = "T-A2";
  const { choose } = await openHotels(page, arm);
  const body = JSON.stringify({
    detail: `STAY_ITINERARY_CONFLICT: STAY_PINNED_CONFLICT:PINNED_AFTER_DEPARTURE ${LEAK}`,
    error: { code: "VALIDATION_ERROR", message: `STAY_ITINERARY_CONFLICT ${LEAK}` },
  });
  await page.route(/\/api\/v1\/journeys\/[^/]+\/stay-options\/[^/]+\/select(\?|$)/, (route) =>
    refuse(route, 422, body),
  );
  await choose.click();
  await expect(page.getByTestId("hotel-error"), node(arm, "拒绝句没有上屏")).toBeVisible({ timeout: 20_000 });
  readings[arm] = await assertTwoWay(
    page,
    arm,
    "hotel-error",
    "hotel-raw-facts",
    422,
    body,
    "这家住宿的入住/退房时间和你已确认的行程冲突",
  );
});

test("A3 住宿被拒：英文那半边同样两问", async ({ page }) => {
  test.setTimeout(180_000);
  const arm = "T-A3";
  const { choose } = await openHotels(page, arm, "en-GB");
  const body = refusalBody();
  await page.route(/\/api\/v1\/journeys\/[^/]+\/stay-options\/[^/]+\/select(\?|$)/, (route) =>
    refuse(route, 404, body),
  );
  await choose.click();
  await expect(page.getByTestId("hotel-error"), node(arm, "拒绝句没有上屏")).toBeVisible({ timeout: 20_000 });
  const pageText = await visibleText(page);
  expect(pageText.includes(LEAK), node(arm, "上游原话仍在了屏上")).toBe(false);
  const factsText = clean(await page.getByTestId("hotel-raw-facts").innerText());
  for (const line of expectedFacts(404, body)) {
    expect(factsText.includes(line), node(arm, `读数块缺「${line}」，实读=${factsText}`)).toBe(true);
  }
  const sentence = clean(await page.getByTestId("hotel-error").innerText());
  expect(
    sentence.includes("Could not choose this stay."),
    node(arm, `英文半边没说自己那句，实读=${sentence}`),
  ).toBe(true);
  readings[arm] = { facts_text: factsText, sentence, expected_lines: expectedFacts(404, body) };
});

/* ------------------------------------------------------------- T-B 方案选择 */

test("B1 采用方案被拒（409）：原话不上屏、三行读数在屏", async ({ page }) => {
  test.setTimeout(180_000);
  const arm = "T-B1";
  await openWithJourney(page, "/v8/hub", "today-heading");
  /* The chooser renders only while a CANDIDATE plan exists, so the read is patched the same way
     ord0238-3 patches it: real endpoint first, one candidate appended when the trip has none. */
  await page.route(/\/api\/v1\/journeys\/[^/]+\/plans(\?|$)/, async (route) => {
    const real = await route.fetch();
    let rows: unknown[] = [];
    try {
      const parsed = await real.json();
      rows = Array.isArray(parsed) ? (parsed as unknown[]) : [];
    } catch {
      rows = [];
    }
    const candidates = rows.filter(
      (item) => (item as { status?: string }).status === "CANDIDATE",
    );
    const list = candidates.length
      ? candidates
      : [
          {
            id: "ord0325-candidate-1",
            version_no: 1,
            status: "CANDIDATE",
            source: "planner",
            stale: false,
          },
        ];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(list),
    });
  });
  await page.reload({ waitUntil: "load" });
  const choose = page.getByTestId("plan-cmp-choose").first();
  const chooseAlt = page.locator('[data-testid="plan-comparison"] button').last();
  const target = (await choose.count()) > 0 ? choose : chooseAlt;
  await expect(target, node(arm, "方案选择器没有渲染出来")).toBeVisible({ timeout: 40_000 });
  const body = refusalBody();
  await page.route(/\/api\/v1\/journeys\/[^/]+\/plans\/[^/]+\/select(\?|$)/, (route) =>
    refuse(route, 409, body),
  );
  await target.scrollIntoViewIfNeeded();
  await target.click();
  await expect(page.getByTestId("plan-cmp-error"), node(arm, "拒绝句没有上屏")).toBeVisible({ timeout: 20_000 });
  readings[arm] = await assertTwoWay(
    page,
    arm,
    "plan-cmp-error",
    "plan-cmp-raw-facts",
    409,
    body,
    "这个方案暂时不能采用。",
  );
});

/* ------------------------------------------------------------ T-C 外壳横幅 */

/** Walk the trial wizard to its review step (no trip created yet). */
async function walkToReview(page: Page, arm: string) {
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.waitForSelector('[data-testid="trial-setup"]', { timeout: 30_000 });
  await page.fill('[data-testid="trial-city"]', "天津");
  await page.fill('[data-testid="trial-arrival"]', "天津站");
  await page.fill('[data-testid="trial-departure"]', "北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body"), node(arm, "第二步没有出来")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("trial-pace-packed").click();
  await page.getByTestId("trial-interest-food").click();
  await page.fill('[data-testid="trial-budget"]', "1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body"), node(arm, "确认页没有出来")).toBeVisible({ timeout: 20_000 });
}

test("C1 建行程后的住宿搜索被拒：banner 不印 error.message，改印本机读数", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "T-C1";
  /* The generating call is answered empty on the way in, so the trip is created and Today shows the
     block with its own retry; the retry is then refused. That retry's catch (`retryStaySearch`) is
     one of the two sinks that printed `error.message`; the other is `createJourney`'s own catch, and
     C1 cannot reach it -- an arm aimed at that line stayed green here until C2 was added. */
  let generateMode = "empty";
  const emptyBody = JSON.stringify({ top_k: [], generation: { outcome: "CATALOG_EMPTY" } });
  const refuseBody = refusalBody();
  await page.route(/\/api\/v1\/journeys\/[^/]+\/stay-options\/generate(\?|$)/, (route) =>
    generateMode === "empty"
      ? route.fulfill({ status: 200, contentType: "application/json", body: emptyBody })
      : refuse(route, 422, refuseBody),
  );
  await walkToReview(page, arm);
  await page.getByTestId("trial-create").click();
  await expect(page.getByTestId("today-heading"), node(arm, "行程没有建成，横幅无从出现")).toBeVisible({
    timeout: 180_000,
  });
  const retry = page.getByTestId("hub-stay-retry");
  await expect(retry, node(arm, "Today 上没有「再查一次」，这一格走不到横幅")).toBeVisible({ timeout: 40_000 });
  generateMode = "refuse";
  await retry.scrollIntoViewIfNeeded();
  await retry.click();
  const banner = page.locator(".v8-message");
  await expect(banner, node(arm, "被拒之后外壳横幅没有出现")).toBeVisible({ timeout: 30_000 });
  const bannerText = clean(await banner.innerText());
  expect(bannerText.includes(LEAK), node(arm, `横幅印出了上游原话：${bannerText}`)).toBe(false);
  expect(
    bannerText.includes("服务器拒绝了这一步（HTTP 422）"),
    node(arm, `横幅没有说这台设备量到的那次拒绝，实读=${bannerText}`),
  ).toBe(true);
  const facts = page.getByTestId("shell-message-raw-facts");
  await expect(facts, node(arm, "横幅里没有本机读数块")).toBeVisible();
  const factsText = clean(await facts.innerText());
  for (const line of expectedFacts(422, refuseBody)) {
    expect(factsText.includes(line), node(arm, `读数块缺「${line}」，实读=${factsText}`)).toBe(true);
  }
  readings[arm] = {
    banner_text: bannerText,
    facts_text: factsText,
    expected_lines: expectedFacts(422, refuseBody),
    page_text: await visibleText(page),
  };
  expect(readings[arm].page_text.includes(JWT), node(arm, "假凭据形状出现在屏上")).toBe(false);
});

test("C2 建行程这一步本身被拒：向导里的拒绝句也不印上游原话", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "T-C2";
  const createBody = JSON.stringify({
    detail: LEAK,
    error: {
      code: "VALIDATION_ERROR",
      message: "Request validation failed",
      details: { errors: [{ loc: ["body", "budget"], msg: `${LEAK}（字段级原文）`, type: "value_error" }] },
    },
  });
  /* The journey POST itself is refused, so the wizard stays on screen and `createJourney`'s catch
     feeds the message into the block above the confirm button. This is the sink whose `error.message`
     carried every `loc: msg` pair verbatim. */
  await page.route(/\/api\/v1\/journeys(\?|$)/, (route) =>
    route.request().method() === "POST"
      ? refuse(route, 422, createBody)
      : route.fallback(),
  );
  await walkToReview(page, arm);
  await page.getByTestId("trial-create").click();
  const refusal = page.getByTestId("trial-create-error");
  await expect(refusal, node(arm, "创建被拒后向导没有给出任何拒绝块")).toBeVisible({ timeout: 60_000 });
  const refusalText = clean(await refusal.innerText());
  const pageText = await visibleText(page);
  for (const forbidden of [LEAK, JWT]) {
    expect(
      pageText.includes(forbidden),
      node(arm, `上游正文（或假凭据形状）仍在了屏上：${forbidden.slice(0, 20)}… ${pageText.slice(0, 300)}`),
    ).toBe(false);
  }
  expect(
    refusalText.includes("服务器拒绝了这一步（HTTP 422）"),
    node(arm, `向导块没说自己那句人话，实读=${refusalText}`),
  ).toBe(true);
  readings[arm] = {
    refusal_block: refusalText,
    page_text: pageText,
    raw_facts_block_on_this_surface: await page.getByTestId("shell-message-raw-facts").count(),
    note: "向导遮罩这一屏只有外壳横幅才渲染读数块；此处主张的是『原话不上屏 + 句子是本机的』，"
          + "读数块仍在 C1 那枚外壳横幅上主张",
  };
});

test.afterAll(async () => {
  writeFileSync(
    evidencePath("ui-overnight/ord0325-readings.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        file: FILE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        sentinels: { leak: LEAK, credential_shaped: JWT },
        readings,
        rule: "每格两问：上游原话（含哨兵与假凭据）不在 body.innerText；这台设备量到的三行在屏，bytes 由本节点 fulfill 的正文重算",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
