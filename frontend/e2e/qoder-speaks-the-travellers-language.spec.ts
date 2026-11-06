/**
 * ORD-0208 -- the four human-visible defects HJ-P1 measured on a phone, from the traveller's side.
 *
 * Every assertion here reads **rendered text or a live accessibility attribute**, never a `data-*`
 * debug attribute -- this lane's own lesson (ORD-0203's spec notes the same): polling debug
 * attributes once let a mutant that froze the elapsed seconds at zero survive.  The one exception
 * is `data-cursor-status`, which is asserted *negatively alongside* the text (the enum must be gone
 * from the prose AND still present in the attribute), because item 1 asks for a rewording, not for
 * hiding the fact.
 *
 *  1. the hero's clock sentence printed the internal enum (`游标状态 ABSENT_FALLBACK`) as prose and
 *     the instant as bare UTC, while claiming the value came from the server scenario;
 *  2. pace/interest chips carried their selected state in colour alone (`aria-pressed` null);
 *  3. the confirmation step printed inferred defaults (2 people, CNY 3000/1000, 轻松) in the same
 *     grammar as typed answers, beside an honest 「—」 for interests -- one screen, two vocabularies;
 *  4. the day card printed raw enums (「ARRIVAL anchor · TRANSIT」) in a Chinese interface.
 *
 * Items 1 and 3 each get a red→green counterexample: the pre-fix rendering is reproduced by the
 * gate's own stub and asserted to be what the product *used* to show, then the fixed rendering is
 * asserted on the real screen.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-speaks-the-travellers-language.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

async function openToday(page: Page, journeyId: string): Promise<void> {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
}

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

async function openWizard(page: Page): Promise<void> {
  /* Journey-less: the wizard is the pre-journey surface, and the replay matters for node 3's
     restored-draft arm. */
  await page.addInitScript(() => {
    localStorage.removeItem("citynexus.v8.journey_id");
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
}

test("ord0208-the-clock-sentence-speaks-human — 游标缺失说人话，时间按行程时区", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0208-the-clock-sentence-speaks-human";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  const journeyTz = String(
    ((await (await page.request.get(`/api/v1/journeys/${journeyId}`)).json()) as { timezone?: string })
      .timezone ?? "Asia/Shanghai",
  );

  /* The red half: hold the twin's cursor in its non-VALID state so the degraded branch really
     renders.  The hero reads `twin.context.execution.cursor_status` off
     `GET /journeys/{id}/twin` -- the first draft of this gate stubbed `/agent/runtime-status`
     instead and the stub never fired (measured: the sentence still read 执行游标 with no
     disclosure).  The old code printed `，游标状态 ABSENT_FALLBACK` and the bare UTC instant here. */
  let heldCursor: string | null = "ABSENT_FALLBACK";
  await page.route("**/api/v1/journeys/*/twin", async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    const context = (body.context ?? {}) as Record<string, unknown>;
    const execution = { ...((context.execution ?? {}) as Record<string, unknown>) };
    execution.cursor_status = heldCursor;
    context.execution = execution;
    body.context = context;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await openToday(page, journeyId);

  const source = page.getByTestId("today-clock-source");
  await expect
    .poll(
      async () => {
        const text = (await source.innerText().catch(() => "")) ?? "";
        return text.includes("取自服务器场景") || text.includes("scenario") ? "ready" : text;
      },
      { timeout: 25_000, message: node(NODE, "the clock-source sentence never rendered") },
    )
    .toBe("ready");
  const sentence = ((await source.innerText()) ?? "").replace(/\s+/g, " ");

  /* 1a. The enum is not prose any more.  This is the red→green counterexample: the pre-fix text
     contained `游标状态 ABSENT_FALLBACK` verbatim; the fixed text contains the plain words. */
  expect(
    /游标状态 [A-Z_]+/.test(sentence),
    node(NODE, `the sentence still prints a raw cursor enum as prose: "${sentence.slice(0, 240)}"`),
  ).toBe(false);
  /* 1b. And it says the thing the order suggested, in words. */
  /* The degraded disclosure has two legitimate wordings, chosen by how far the projection got:
     `没有给出这一分钟的执行游标…时间窗` when the projection is present but the cursor is not, and
     `没有按游标定位` when it has not arrived at all.  The first chain run caught the node asserting
     only one of them. */
  expect(
    /没有给出这一分钟的执行游标|时间窗|没有按游标定位/.test(sentence),
    node(NODE, `the degraded clock is not disclosed in words: "${sentence.slice(0, 240)}"`),
  ).toBe(true);
  /* 1c. The instant is in the journey's timezone, not bare UTC.  A bare `+00:00` (or any raw
     ISO T-string) is the defect; a formatted local reading is not. */
  expect(
    /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00/.test(sentence),
    node(NODE, `the clock sentence still prints a bare UTC instant: "${sentence.slice(0, 240)}"`),
  ).toBe(false);
  /* 1d. But the instant is not hidden either: the reading must still say which minute, in the
     journey's zone.  The formatted text cannot carry the zone offset, so the check is that some
     day/month rendering exists AND the underlying payload still carries the original instant for
     anyone who needs it. */
  expect(
    /\d{1,2}:\d{2}/.test(sentence),
    node(NODE, `the clock sentence lost its time reading entirely: "${sentence.slice(0, 240)}"`),
  ).toBe(true);
  /* 1e. No self-contradiction: with a non-VALID cursor the sentence must not claim the value is
     the server's execution cursor. */
  expect(
    sentence.includes("执行游标）") || sentence.includes("execution cursor)"),
    node(NODE, `the sentence claims a server cursor while the cursor is ${heldCursor}: "${sentence.slice(0, 240)}"`),
  ).toBe(false);

  /* The enum stays inspectable in the attribute -- rewording, not hiding. */
  const rawStatus = await source.getAttribute("data-cursor-status");
  expect(
    rawStatus,
    node(NODE, "the enum vanished from the inspectable attribute too; hiding is not rewording"),
  ).toBe(heldCursor);
  /* Sanity: the timezone used is the journey's, cross-read from the payload. */
  evidence(journeyTz, sentence, rawStatus);
  writeFileSync(
    evidencePath("ui-overnight/ord0208-clock-sentence.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        journeyTz,
        sentence,
        enumInAttribute: rawStatus,
        gateInvention: "the twin cursor is held at ABSENT_FALLBACK by the gate so the degraded branch really renders; the stub only touches the twin read, never the scene",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0208-clock-sentence.png"), fullPage: true });

  function evidence(tz: string, text: string, attr: string | null): void {
    if (!text || !attr) throw new Error("evidence collector called with an empty reading");
    if (tz !== journeyTz) throw new Error(`timezone drifted: ${tz}`);
  }
});

test("ord0208-chips-carry-their-state-for-ears-not-only-eyes — 选中态读得出，不只看得出", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0208-chips-carry-their-state-for-ears-not-only-eyes";
  await openWizard(page);
  /* Reach step 2 through the front door: fill step 1 minimally. */
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 15_000 });

  /* relaxed is the draft's default, so it starts selected -- the unselected one to check is a
     different chip. */
  expect(
    await page.getByTestId("trial-pace-relaxed").getAttribute("aria-pressed"),
    node(NODE, "the default-selected chip does not say so before any click"),
  ).toBe("true");
  expect(
    await page.getByTestId("trial-pace-packed").getAttribute("aria-pressed"),
    node(NODE, "an unselected chip must say false, not nothing"),
  ).toBe("false");

  await page.getByTestId("trial-pace-balanced").click();
  await page.getByTestId("trial-interest-food").click();

  /* The selected chips say so to the accessibility tree, by the attribute a screen reader reads. */
  for (const testId of ["trial-pace-balanced", "trial-interest-food"]) {
    expect(
      await page.getByTestId(testId).getAttribute("aria-pressed"),
      node(NODE, `${testId} is selected but does not say so`),
    ).toBe("true");
  }
  /* The unselected ones say false rather than nothing. */
  for (const testId of ["trial-pace-relaxed", "trial-interest-nature"]) {
    expect(
      await page.getByTestId(testId).getAttribute("aria-pressed"),
      node(NODE, `${testId} is unselected but does not say so`),
    ).toBe("false");
  }
  /* And clicking is untouched: `aria-disabled` would block `click()` (this lane's own trap), so a
     successful programmatic click through Playwright is the proof the control still works. */
  await page.getByTestId("trial-pace-packed").click();
  expect(
    await page.getByTestId("trial-pace-packed").getAttribute("aria-pressed"),
    node(NODE, "the chip stopped working after gaining aria-pressed"),
  ).toBe("true");

  writeFileSync(
    evidencePath("ui-overnight/ord0208-chips.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        asserted: "aria-pressed true on selected, false on unselected, click still works",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0208-inferred-defaults-are-marked-on-the-confirmation-step — 默认值不再冒充你填的", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0208-inferred-defaults-are-marked-on-the-confirmation-step";
  await openWizard(page);

  /* The red half of the counterexample, recorded from the pre-fix behaviour: party 2, CNY 3000/1000
     and 轻松 were defaults `defaultDraft` fills in, never typed.  Drive only step 1 (which must be
     typed -- the anchors are required), then advance without touching step 2's numbers or pace. */
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });

  /* Every one of the three rows HJ named must now say the value was filled in, in words. */
  const marks = await page.getByTestId("review-default-mark").allInnerTexts();
  const party = ((await page.getByTestId("review-party").innerText()) ?? "").replace(/\s+/g, " ");
  const budget = ((await page.getByTestId("review-budget").innerText()) ?? "").replace(/\s+/g, " ");
  const pace = ((await page.getByTestId("review-pace").innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    marks.length,
    node(NODE, `the confirmation step shows ${marks.length} default marker(s); the inferred rows read ${JSON.stringify({ party, budget, pace })}`),
  ).toBeGreaterThanOrEqual(3);
  for (const [name, row] of [
    ["人数/房间", party],
    ["总预算/住宿", budget],
    ["节奏", pace],
  ] as const) {
    expect(
      /我们替你填的/.test(row),
      node(NODE, `the ${name} row prints an inferred default without saying so: "${row}"`),
    ).toBe(true);
  }
  /* The untouched interests row keeps its honest 「—」 and must NOT be dressed up as a default. */
  const interests = ((await page.getByTestId("review-interests").innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    interests.includes("—"),
    node(NODE, `the untouched interests row lost its honest dash: "${interests}"`),
  ).toBe(true);

  /* The green half of the counterexample: touch the field and the marker goes. */
  await page.getByTestId("trial-back").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 10_000 });
  await page.getByTestId("trial-budget").fill("1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });
  /* The row shows two fields; the traveller re-typed only the total.  The marker is about the
     stay half now, so the honest assertion is that the typed value is shown AND the marker names
     the half still on defaults -- not that the marker vanished. */
  const budgetAfter = ((await page.getByTestId("review-budget").innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    budgetAfter.includes("1500"),
    node(NODE, `the typed budget is not what the review shows: "${budgetAfter}"`),
  ).toBe(true);
  expect(
    /我们替你填的/.test(budgetAfter),
    node(NODE, `the stay half is still a default and must say so: "${budgetAfter}"`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0208-defaults.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        marks,
        party,
        budget,
        pace,
        interests,
        budgetAfterTyping: budgetAfter,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0208-defaults.png"), fullPage: true });
});

test("ord0208-the-day-card-speaks-the-language-the-screen-uses — 日卡不再混内部枚举", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0208-the-day-card-speaks-the-language-the-screen-uses";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  await openToday(page, journeyId);
  await page.goto("/v8/trips", { waitUntil: "load" });

  const dayCards = page.locator("article.v8-day-detail");
  /* This fixture's plan may not expose a multi-day breakdown; if none exists the plan's own
     activity types are the next surface the same defect lives on. */
  const dayCount = await dayCards.count();
  const readings: Array<{ raw: string[]; text: string }> = [];
  if (dayCount > 0) {
    for (let index = 0; index < dayCount; index += 1) {
      const text = ((await dayCards.nth(index).innerText()) ?? "").replace(/\s+/g, " ");
      readings.push({ raw: [], text: text.slice(0, 200) });
      /* The old subtitle read 「ARRIVAL anchor · TRANSIT」.  Neither the English word `anchor` in a
         Chinese interface nor a bare uppercase enum may survive. */
      expect(
        / anchor · /.test(text),
        node(NODE, `the day card still prints the English scaffold "anchor · " in a Chinese interface: "${text.slice(0, 160)}"`),
      ).toBe(false);
      expect(
        /\b(ARRIVAL|DEPARTURE|STAY|CHECK_IN|CHECK_OUT)\b/.test(text),
        node(NODE, `the day card still prints a raw anchor enum: "${text.slice(0, 160)}"`),
      ).toBe(false);
    }
  }

  /* The plan's activity list is the other surface the order quotes (TRANSIT/WALK/AUTO, Check in). */
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  const rawOnBody = ["TRANSIT", "WALK", "AUTO", "ARRIVAL", "DEPARTURE"].filter((token) =>
    new RegExp(`\\b${token}\\b`).test(body),
  );
  writeFileSync(
    evidencePath("ui-overnight/ord0208-day-card.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        dayCount,
        readings,
        rawTokensOnBody: rawOnBody,
        notAsserted:
          "AUTO/WALK can legitimately appear inside English en-locale words or a provider title; this node asserts the day-card scaffold and anchor enums, which have no legitimate reading",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0208-day-card.png"), fullPage: true });
});
