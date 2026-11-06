/**
 * ORD-0238 -- three layers of truth, pinned apart: **what she said / what the server holds / what
 * the planner actually took as input.**  The arbiter refused the shape "把字段塞进 preferences 就算修好",
 * so each layer gets its own node and its own reading, and the third one is deliberately *not* a
 * claim that interests shaped the plan.
 *
 * Literal-equality on the review row follows this lane's own ORD-0226 finding: judging rendered
 * Chinese prose by keyword lists survives paraphrase poorly, and a joined label list is exactly the
 * kind of string that changes quietly.  The literals live here, not imported from the product.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-stated-versus-stored-versus-planned.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** What this test clicks, and the exact strings the product owes for it. */
const PICKED_INTERESTS = ["food", "family"] as const;
const FROZEN_REVIEW_INTERESTS_ZH = "美食、亲子";
const FROZEN_REVIEW_PACE_ZH = "紧凑";

async function walkWizardToReview(page: Page) {
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
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("trial-pace-packed").click();
  for (const key of PICKED_INTERESTS) {
    await page.getByTestId(`trial-interest-${key}`).click();
  }
  await page.fill('[data-testid="trial-budget"]', "1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 20_000 });
}

async function createTrip(page: Page): Promise<string> {
  await page.getByTestId("trial-create").click();
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 90_000 });
  return await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id") ?? "");
}

async function travelerOf(page: Page, journeyId: string): Promise<string> {
  const experience = await page.request.get(`/api/v1/journeys/${journeyId}/experience`);
  expect(experience.status(), "the created journey must be readable").toBe(200);
  const body = (await experience.json()) as { journey?: { traveler_id?: string } };
  return String(body.journey?.traveler_id ?? "");
}

test("ord0238-1-the-review-screen-shows-what-she-picked — 第一层：她说了什么", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0238-1-the-review-screen-shows-what-she-picked";
  await walkWizardToReview(page);
  const interests = (await page.getByTestId("review-interests").innerText()).replace(/\s+/g, " ").trim();
  const pace = (await page.getByTestId("review-pace").innerText()).replace(/\s+/g, " ").trim();
  expect(
    interests,
    node(NODE, `the review row is not the frozen join of her two chips: expected "${FROZEN_REVIEW_INTERESTS_ZH}" / got "${interests}"`),
  ).toBe(FROZEN_REVIEW_INTERESTS_ZH);
  expect(
    pace,
    node(NODE, `the pace row is not the chip she pressed: expected "${FROZEN_REVIEW_PACE_ZH}" / got "${pace}"`),
  ).toBe(FROZEN_REVIEW_PACE_ZH);
});

test("ord0238-2-the-server-holds-what-she-said-after-the-trip-exists — 第二层：系统保存了什么", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0238-2-the-server-holds-what-she-said-after-the-trip-exists";
  await walkWizardToReview(page);
  const journeyId = await createTrip(page);
  const travelerId = await travelerOf(page, journeyId);
  expect(travelerId, node(NODE, "the new journey carries no traveler id, so nothing could be read back")).toBeTruthy();

  const read = await page.request.get(`/api/v1/travelers/${travelerId}/preferences`);
  expect(read.status(), node(NODE, "the preferences contract did not answer")).toBe(200);
  const rows = (await read.json()) as Array<Record<string, unknown>>;
  const interests = rows
    .filter((row) => String(row.category ?? "") === "interest")
    .map((row) => String(row.key ?? ""))
    .sort();
  expect(
    interests,
    node(NODE, `stored interest keys are not what she clicked: got ${JSON.stringify(rows.map((r) => [r.category, r.key]))}`),
  ).toEqual([...PICKED_INTERESTS].sort());
  /* `PUT` replaces the whole list, so a weight here is the product's own statement, not her ranking:
     the wizard never asked how strongly she holds an interest.  There is no `source` in the read-back
     projection (`{id, traveler_id, category, key, value, weight}`), so this row cannot claim more
     than "she said it, we did not rank it". */
  for (const row of rows.filter((item) => String(item.category ?? "") === "interest")) {
    expect(
      `${row.weight}/${row.value}`,
      node(NODE, `an interest row says more than she said: ${JSON.stringify(row)}`),
    ).toBe("0.5/true");
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0238-2-stored-rows.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        journeyId,
        travelerId,
        rows,
        rule: "第二层只问服务器存了什么行；权重是产品自述的\"说过但未排序\"",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0238-3-the-plan-headline-claims-only-inputs-the-planner-takes — 第三层：系统真的按什么规划", async ({
  page,
}) => {
  test.setTimeout(420_000);
  const NODE = "ord0238-3-the-plan-headline-claims-only-inputs-the-planner-takes";
  let chooserReads = 0;
  /* The chooser renders only when a CANDIDATE plan exists (`PlanComparison` returns null otherwise),
     so a trip whose generate returned 422 has no headline to judge.  Registered before any navigation:
     installed mid-session it measured 0 firings, which is how an unexercised arm reads as an absence. */
  await page.route(/\/api\/v1\/journeys\/[^/]+\/plans(\?|$)/, async (route) => {
    chooserReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { id: "ord0238-candidate-1", version_no: 1, status: "CANDIDATE", source: "planner", stale: false },
        { id: "ord0238-candidate-2", version_no: 2, status: "CANDIDATE", source: "planner", stale: false },
      ]),
    });
  });
  await walkWizardToReview(page);
  const journeyId = await createTrip(page);
  const travelerId = await travelerOf(page, journeyId);

  /* Black-box reading, taken over `page.request` -- which route interception does NOT touch, so the
     stub below cannot flatter it.  Does a stored interest row change what comes back? */
  const setRows = async (rows: unknown[]) => {
    const put = await page.request.put(`/api/v1/travelers/${travelerId}/preferences`, { data: rows });
    expect(put.status(), node(NODE, "the preferences contract refused the probe payload")).toBe(200);
  };
  const fingerprint = async (): Promise<{ plans: string; code: string; status: number }> => {
    const generated = await page.request.post(`/api/v1/journeys/${journeyId}/plans/generate`, { data: {} });
    const status = generated.status();
    const body = (await generated.json().catch(() => null)) as Record<string, unknown> | null;
    const error = (body?.error ?? {}) as Record<string, unknown>;
    const code = String(error?.code ?? body?.detail ?? "");
    const plans = Array.isArray(body) ? (body as Array<Record<string, unknown>>) : [];
    return {
      plans: plans
        .map((plan) => `${plan.id ?? plan.version_no ?? "?"}:${JSON.stringify(plan.activity_ids ?? plan.activities ?? "").slice(0, 160)}`)
        .join("|"),
      code,
      status,
    };
  };
  await setRows([{ category: "interest", key: "food", value: true, weight: 0.5 }]);
  const withInterest = await fingerprint();
  await setRows([]);
  const withoutInterest = await fingerprint();
  /* "Both sides errored the same way" is not evidence that the stored row was ignored, so the verdict
     is derived from what the two calls actually were, never from a bare string comparison. */
  const probeVerdict =
    withInterest.status === 201 && withoutInterest.status === 201
      ? withInterest.plans === withoutInterest.plans
        ? "NO_EFFECT_ON_OUTPUT"
        : "PREFERENCES_CHANGED_THE_OUTPUT"
      : `INCONCLUSIVE(refused=${withInterest.code || withInterest.status}/${
          withoutInterest.code || withoutInterest.status
        })`;

  await expect
    .poll(() => chooserReads, {
      timeout: 20_000,
      message: node(
        NODE,
        "the plan-list read never reached the stub, so the chooser state was never presented and the headline judgement below would be vacuous",
      ),
    })
    .toBeGreaterThan(0);
  const head = page.locator('[data-testid="plan-comparison"] p');
  const onScreen = await page.evaluate(() => ({
    contentTestIds: Array.from(document.querySelectorAll('.v8-content [data-testid]'))
      .slice(0, 24)
      .map((el) => el.getAttribute("data-testid")),
    sheetOpen: Boolean(document.querySelector('[data-testid="plan-ai-sheet"]')),
    setupOpen: Boolean(document.querySelector('[data-testid="trial-setup"]')),
    path: window.location.pathname,
  }));
  await expect(
    head,
    node(
      NODE,
      `no plan-chooser headline is on screen, so the claim could not be read; chooser stub fired ${chooserReads} time(s), generate probe verdict "${probeVerdict}", on screen=${JSON.stringify(onScreen)}`,
    ),
  ).toBeVisible({ timeout: 30_000 });
  const said = (await head.innerText()).replace(/\s+/g, " ").trim();
  for (const claim of ["节奏", "兴趣", "pace", "interest"]) {
    expect(
      said,
      node(NODE, `the headline claims the plan followed "${claim}"; measured inputs are places + budget only: "${said}"`),
    ).not.toContain(claim);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0238-3-planner-input-probe.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        headline: said,
        measured_inputs: "generate_plans(session, journey, planning_mode, candidate_count, place_ids)",
        probe: {
          verdict: probeVerdict,
          with_interest_row: { status: withInterest.status, code: withInterest.code, plans: withInterest.plans.slice(0, 300) },
          without_any_preference_row: {
            status: withoutInterest.status,
            code: withoutInterest.code,
            plans: withoutInterest.plans.slice(0, 300),
          },
        },
        rule: "第三层读规划器的输入与产出；只有两边都 201 且方案不同才说\"被用了\"，两边同错只记 INCONCLUSIVE",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
