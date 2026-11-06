/**
 * T4 — budget evidence coverage (docs/BUDGET_EVIDENCE_CONTRACT.md; V2-T4 / B4).
 * LAYER: integration (RENDERED component), not unit.
 *
 * Unlike e2e/qoder-budget-evidence.spec.ts (which drives the pure functions via
 * `page.evaluate(import .../budgetEvidence.ts)` and is labelled unit), this spec
 * renders the real budget surface: it serves deterministic fixtures through
 * `page.route` for every API the application actually fetches, boots the real
 * V8Experience shell on the real `/v8/studio` route, and lets the real
 * `Studio` feasibility block (data-testid="studio-feasibility") paint from the
 * authoritative plan's activity cost evidence.
 *
 * The Studio feasibility block reads `budgetEvidenceCoverage(plan.activities)`
 * (cost_status / estimated_cost live on the authoritative PlanVersion), and maps
 * the coverage state to i18n copy:
 *   FULL    -> studio.feasibilityPassed  ("行程约束检查已通过")
 *            + studio.feasibilitySub     ("...所有站点的活动费用已完整；整趟行程
 *                                          预算（含住宿与交通）未在此处验证。")
 *   PARTIAL -> studio.feasibilityPassedPartial ("行程约束检查通过，费用证据不完整")
 *            + studio.feasibilityPartialSub    ("...预算未验证：还有 {n} 站没有
 *                                               供应商价格...")
 *   NONE    -> studio.feasibilityPassedPartial + studio.feasibilityNoPrice
 *
 * What this pins, from the rendered DOM:
 *   1. B4: everything priced EXCEPT an unknown stay/transport item -> the block
 *      must NOT carry the FULL verdict wording; it must show the partial
 *      "预算未验证" wording with the unknown count.
 *   2. Only when every budget-relevant item is known does the FULL wording appear.
 *   3. A KNOWN_FREE zero-amount stop counts as *known*, so it does not knock the
 *      block back out of FULL (a 0 price is real evidence, not "unknown").
 *
 * Endpoints mocked: same set the shell boot + Studio consume; the cost evidence
 * that drives the block is the /api/v1/journeys/{id}/plans response.
 */

import { expect, test, type Page } from "@playwright/test";

const JOURNEY_ID = "e2e-render-journey";
const TRAVELER_ID = "e2e-render-traveler";

async function seed(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("citynexus.v8.journey_id", "e2e-render-journey");
    localStorage.setItem("citynexus.locale", "zh-CN");
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (navigator as any).serviceWorker.register = () => Promise.resolve({});
    } catch {
      /* no-op */
    }
  });
}

/** One plan activity carrying the cost evidence Studio aggregates. */
function priced(id: string, title: string, cost_status: string, amount_minor: number) {
  return {
    id,
    sequence_no: 1,
    type: "visit",
    title,
    start_at: "2026-10-10T09:00:00+08:00",
    end_at: "2026-10-10T11:00:00+08:00",
    locked: false,
    protected: false,
    metadata: {},
    cost_status,
    estimated_cost: { amount_minor, currency: "CNY" },
  };
}

function experienceFixture() {
  return {
    journey: {
      id: JOURNEY_ID,
      traveler_id: TRAVELER_ID,
      title: "厦门周末",
      city_code: "CN-XIAMEN",
      timezone: "Asia/Shanghai",
      start_at: "2026-10-09T18:00:00+08:00",
      end_at: "2026-10-11T15:00:00+08:00",
      budget: { amount_minor: 120000, currency: "CNY" },
    },
    trip: {
      arrival_anchor: { at: "2026-10-09T18:00:00+08:00", location: "厦门站" },
      departure_anchor: { at: "2026-10-11T15:00:00+08:00", location: "厦门高崎机场" },
      party_size: 2,
      occupancy: { adults: 2, rooms: 1 },
      stay_budget: { amount_minor: 70000, currency: "CNY" },
    },
    offers: [],
    options: [],
    selected_option_id: null,
    mobility_facts: [],
    active_plan: {
      id: "plan-1",
      version_no: 1,
      status: "ACTIVE",
      source: "E2E_RENDER",
      current_activity_id: null,
      activities: [],
      legs: [],
      user_location: null,
      disruptions: [],
    },
    recovery: null,
    experience_flags: {
      needs_review: false,
      recovery_required: false,
      staleness: false,
      feasibility_failures: [],
    },
  };
}

function offlineFixture() {
  return {
    journey_id: JOURNEY_ID,
    sequence: 1,
    valid_until: "2026-12-31T00:00:00Z",
    snapshot: {
      active_plan_version_id: null,
      current_activity_id: null,
      next_24h: [],
      legs: [],
      hotel: null,
      reservations: [],
      read_only: true,
      financial_actions_allowed: false,
    },
    provider_freshness: { latest_provider_capture_at: null, transit_realtime: "UNKNOWN" },
    updated_at: "2026-10-09T10:00:00Z",
  };
}

/** Authoritative plan list whose activities carry the cost evidence. */
function plansFixture(activities: unknown[]) {
  return [{ id: "plan-1", version_no: 1, status: "ACTIVE", activities }];
}

async function mockApi(page: Page, activities: unknown[]) {
  await page.route(/\/api\/v1\//, (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const send = (json: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(json),
      });
    if (pathname.endsWith("/experience")) return send(experienceFixture());
    if (pathname.endsWith("/plans")) return send(plansFixture(activities));
    if (pathname.endsWith("/replans")) return send([]);
    if (pathname.endsWith("/twin")) return send({ id: "twin-1", context: {} });
    if (pathname.endsWith("/agent-policy"))
      return send({
        mode: "ADVISORY",
        auto_accept_replan: false,
        version: 1,
        max_auto_replan_cost_delta_minor: 5000,
        max_auto_loss_minor: 0,
        max_auto_schedule_drift_min: 20,
      });
    if (pathname.endsWith("/wallet"))
      return send({ reservations: [], commitments: [], offline_ready: true });
    if (pathname.endsWith("/offline-snapshot")) return send(offlineFixture());
    if (pathname.endsWith("/runtime-status")) return send({});
    return send({});
  });
}

test.describe("T4 budget evidence coverage (rendered integration)", () => {
  test("B4: all stops priced but an unknown stay/transport -> the block refuses a verified-budget verdict and shows PARTIAL wording", async ({
    page,
  }) => {
    await seed(page);
    await mockApi(page, [
      priced("a1", "日光岩", "KNOWN_PAID", 3500),
      priced("a2", "曾厝垵", "KNOWN_PAID", 1200),
      priced("a3", "南普陀", "KNOWN_PAID", 800),
      // The hotel and the airport transfer have no provider price.
      priced("stay", "住宿 · 酒店", "UNKNOWN", 0),
      priced("transport", "机场往返交通", "UNKNOWN", 0),
    ]);
    await page.goto("/v8/studio", { waitUntil: "domcontentloaded" });

    const block = page.getByTestId("studio-feasibility");
    await expect(block).toBeVisible();
    // PARTIAL wording (positive assertion first, so it waits for the async plan
    // query to resolve, then the negatives are stable).
    await expect(block).toContainText("行程约束检查通过，费用证据不完整");
    await expect(block).toContainText("还有 2 项缺少已验证的供应商价格");
    // The FULL verdict wording must NOT be shown while stay/transport are unknown.
    await expect(block).not.toContainText("行程约束检查已通过");
  });

  test("only when every budget-relevant item is known does the FULL verdict wording render", async ({
    page,
  }) => {
    await seed(page);
    await mockApi(page, [
      priced("a1", "日光岩", "KNOWN_PAID", 1000),
      priced("a2", "环岛路骑行", "KNOWN_PAID", 2000),
      priced("a3", "厦大参观", "KNOWN_PAID", 3000),
    ]);
    await page.goto("/v8/studio", { waitUntil: "domcontentloaded" });

    const block = page.getByTestId("studio-feasibility");
    await expect(block).toBeVisible();
    await expect(block).toContainText("行程约束检查已通过");
    await expect(block).toContainText("所有站点的活动费用已完整");
    // None of the partial "budget not verified" wording may leak in.
    await expect(block).not.toContainText("行程约束检查通过，费用证据不完整");
    await expect(block).not.toContainText("站没有供应商价格");
  });

  test("a KNOWN_FREE (0 amount) stop counts as known and keeps the block at FULL", async ({
    page,
  }) => {
    await seed(page);
    await mockApi(page, [
      priced("a1", "日光岩", "KNOWN_PAID", 1000),
      priced("free", "南普陀（免费开放）", "KNOWN_FREE", 0),
      priced("a2", "酒店住宿", "KNOWN_PAID", 5000),
    ]);
    await page.goto("/v8/studio", { waitUntil: "domcontentloaded" });

    const block = page.getByTestId("studio-feasibility");
    await expect(block).toBeVisible();
    // If a zero-amount KNOWN_FREE fell back to "unknown", coverage would be PARTIAL
    // and the heading would read "预算未验证". Reaching FULL proves the 0 is evidence.
    await expect(block).toContainText("行程约束检查已通过");
    await expect(block).not.toContainText("预算未验证");
  });
});
