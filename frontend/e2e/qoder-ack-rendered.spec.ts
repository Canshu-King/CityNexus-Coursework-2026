/**
 * T3 — acknowledgement vs resolution (docs/ACK_RESOLUTION_CONTRACT.md).
 * LAYER: integration (RENDERED component), not unit.
 *
 * Unlike e2e/qoder-ack-resolution.spec.ts (which drives the pure predicate via
 * `page.evaluate(import .../disruptionState.ts)` and is labelled unit), this
 * spec renders the real React surface: it serves deterministic fixtures through
 * `page.route` for every API the application actually fetches, boots the real
 * V8Experience shell on the real `/v8/live` route, and lets the real
 * `LiveRecovery` component (`src/features/execution/LiveRecovery.tsx`) paint.
 * The assertions are on rendered DOM (`getByTestId` / `getByText`), never on an
 * imported function's return value.
 *
 * What this pins (the V2-T3 fix — the old `acknowledgedOnly > 0 && openDisruptions
 * === 0` guard made the "已知晓，仍待处理" notice unreachable, because an
 * acknowledged-only event is by construction a subset of the open set):
 *   1. An ACKNOWLEDGED disruption with NO linked ACCEPTED replan stays open, so
 *      BOTH "发生了什么" (live-disruption) AND "已知晓，仍待处理"
 *      (live-acknowledged) render at the same time.
 *   2. An ACCEPTED proposal whose trigger_event_id matches the event closes it:
 *      neither the open notice nor the acknowledged notice is shown; the event
 *      surfaces as handled instead.
 *   3. An ACCEPTED proposal with an UNRELATED trigger_event_id does NOT close the
 *      event; it stays open (both notices still render).
 *
 * Endpoints mocked (the exact URLs LiveRecovery + its V8Experience shell fetch):
 *   /api/v1/journeys/{id}/experience      (shell `load()` + LiveRecovery disruption
 *                                          merge of active_plan.disruptions)
 *   /api/v1/journeys/{id}/replans         (LiveRecovery proposals -> acceptedTriggerIds)
 *   /api/v1/journeys/{id}/plans           (authoritative plans via useAuthoritativePlans)
 *   /api/v1/journeys/{id}/twin            (authoritative twin via useAuthoritativeTwin)
 *   /api/v1/travelers/{id}/agent-policy   (shell boot)
 *   /api/v1/journeys/{id}/wallet          (shell boot)
 *   /api/v1/journeys/{id}/offline-snapshot(shell boot; written to Dexie, needs journey_id)
 *   /api/v1/agent/runtime-status          (shell boot)
 *   everything else under /api/v1/        (sibling Live / V10 panels) -> {} neutral state
 */

import { expect, test, type Page } from "@playwright/test";

const JOURNEY_ID = "e2e-render-journey";
const TRAVELER_ID = "e2e-render-traveler";

/**
 * Boot the real app in a deterministic, isolated way:
 *  - seed the journey id the shell reads from localStorage on boot, and force
 *    the zh-CN locale so the live copy ("发生了什么" / "已知晓，仍待处理") is the
 *    literal string the review asked to see on screen;
 *  - neuter the PWA service worker registration so it can never serve a cached
 *    shell and our route fixtures stay authoritative.
 */
async function seed(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("citynexus.v8.journey_id", "e2e-render-journey");
    localStorage.setItem("citynexus.locale", "zh-CN");
    // Prevent the dev service worker from registering / claiming clients.
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (navigator as any).serviceWorker.register = () => Promise.resolve({});
    } catch {
      /* no-op */
    }
  });
}

type Disruption = {
  id: string;
  type: string;
  severity: string;
  status?: string | null;
  affected_activity_id: string | null;
  occurred_at: string;
  resolved_at?: string | null;
};

type Proposal = {
  id: string;
  status: string;
  trigger_event_id?: string | null;
  hard_constraints_passed: boolean;
  locked_commitments_preserved: boolean;
  diff: { removed?: string[]; added?: string[]; moved?: string[] };
};

function experienceFixture(disruptions: Disruption[]) {
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
      // Deliberately no located activities / legs: keeps MapPanel in its honest
      // empty state so maplibre never loads external tiles.
      activities: [],
      legs: [],
      user_location: null,
      disruptions,
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

/** One dispatcher on /api/v1/ so route-registration order can never bite. */
async function mockApi(page: Page, fx: { experience: unknown; replans: unknown }) {
  await page.route(/\/api\/v1\//, (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const send = (json: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(json),
      });
    if (pathname.endsWith("/experience")) return send(fx.experience);
    if (pathname.endsWith("/replans")) return send(fx.replans);
    if (pathname.endsWith("/plans")) return send([]);
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
    // The /v8/live route also mounts V10LiveExecutionPanel, which reads
    // `web.data.observations.length` (not optional beyond web.data) and
    // `new Date(clock.data.now)`, so these must be well-formed.
    if (pathname.endsWith("/execution-clock"))
      return send({ now: "2026-10-09T10:00:00+08:00", timezone: "Asia/Shanghai", current_activity_id: null });
    if (pathname.endsWith("/web-observations")) return send({ observations: [] });
    return send({});
  });
}

const WEATHER_EVENT: Disruption = {
  id: "evt-1",
  type: "WEATHER",
  severity: "MEDIUM",
  status: "ACKNOWLEDGED",
  affected_activity_id: null,
  occurred_at: "2026-10-09T20:00:00+08:00",
};

test.describe("T3 acknowledgement vs resolution (rendered integration)", () => {
  test("an ACKNOWLEDGED disruption with no linked acceptance stays open: both '发生了什么' and '已知晓，仍待处理' render", async ({
    page,
  }) => {
    await seed(page);
    // No ACCEPTED replan behind the event at all.
    await mockApi(page, { experience: experienceFixture([WEATHER_EVENT]), replans: [] });
    await page.goto("/v8/live", { waitUntil: "domcontentloaded" });

    // The real LiveRecovery section mounted.
    await expect(page.getByTestId("live-recovery")).toBeVisible();

    // V2-T3 fix: the acknowledged-only notice is now reachable WHILE the event is
    // still open — the two co-render (the old dead-condition could not).
    const disruption = page.getByTestId("live-disruption");
    await expect(disruption).toBeVisible();
    await expect(disruption).toContainText("发生了什么");

    const acknowledged = page.getByTestId("live-acknowledged");
    await expect(acknowledged).toBeVisible();
    await expect(acknowledged).toContainText("已知晓，仍待处理");

    // The traveller has only seen it; nothing is handled yet.
    await expect(page.getByTestId("live-handled")).toHaveCount(0);
  });

  test("an ACCEPTED proposal linked to the event by trigger_event_id closes it: no open/acknowledged notice, shown as handled", async ({
    page,
  }) => {
    await seed(page);
    const accepted: Proposal = {
      id: "r1",
      status: "ACCEPTED",
      trigger_event_id: "evt-1",
      hard_constraints_passed: true,
      locked_commitments_preserved: true,
      diff: { removed: [], added: [], moved: [] },
    };
    await mockApi(page, { experience: experienceFixture([WEATHER_EVENT]), replans: [accepted] });
    await page.goto("/v8/live", { waitUntil: "domcontentloaded" });

    await expect(page.getByTestId("live-recovery")).toBeVisible();
    // Exact linkage resolves the event -> it is no longer in the open set, so the
    // "发生了什么" block and the "已知晓，仍待处理" notice both disappear.
    await expect(page.getByTestId("live-disruption")).toHaveCount(0);
    await expect(page.getByTestId("live-acknowledged")).toHaveCount(0);
    await expect(page.getByTestId("live-handled")).toBeVisible();
  });

  test("an ACCEPTED proposal with an UNRELATED trigger_event_id leaves the event open", async ({
    page,
  }) => {
    await seed(page);
    const acceptedUnrelated: Proposal = {
      id: "r2",
      status: "ACCEPTED",
      trigger_event_id: "evt-999",
      hard_constraints_passed: true,
      locked_commitments_preserved: true,
      diff: { removed: [], added: [], moved: [] },
    };
    await mockApi(page, {
      experience: experienceFixture([WEATHER_EVENT]),
      replans: [acceptedUnrelated],
    });
    await page.goto("/v8/live", { waitUntil: "domcontentloaded" });

    await expect(page.getByTestId("live-recovery")).toBeVisible();
    // Accepting a proposal for a DIFFERENT event must not silently resolve this one.
    await expect(page.getByTestId("live-disruption")).toBeVisible();
    await expect(page.getByTestId("live-disruption")).toContainText("发生了什么");
    await expect(page.getByTestId("live-acknowledged")).toBeVisible();
    await expect(page.getByTestId("live-handled")).toHaveCount(0);
  });
});

/**
 * V3-5: the SAME rule must hold on the Today surface, not only Live.  The
 * `today-attention` cell in V8Experience counts OPEN disruptions via the shared
 * `isDisruptionOpen`, so an acknowledged-only event is a countable unresolved
 * change there too, and a matching accepted replan drops it from the count.
 */
test.describe("T3 ack vs resolution — Today surface (rendered integration)", () => {
  test("an ACKNOWLEDGED event with no acceptance stays an unresolved change on Today", async ({
    page,
  }) => {
    await seed(page);
    await mockApi(page, { experience: experienceFixture([WEATHER_EVENT]), replans: [] });
    await page.goto("/v8/today", { waitUntil: "domcontentloaded" });

    const attention = page.getByTestId("today-attention");
    await expect(attention).toBeVisible();
    await expect(attention).toContainText("1 项变化");
  });

  test("a matching ACCEPTED replan clears the change count on Today", async ({ page }) => {
    await seed(page);
    const accepted: Proposal = {
      id: "r1",
      status: "ACCEPTED",
      trigger_event_id: "evt-1",
      hard_constraints_passed: true,
      locked_commitments_preserved: true,
      diff: { removed: [], added: [], moved: [] },
    };
    await mockApi(page, { experience: experienceFixture([WEATHER_EVENT]), replans: [accepted] });
    await page.goto("/v8/today", { waitUntil: "domcontentloaded" });

    const attention = page.getByTestId("today-attention");
    await expect(attention).toBeVisible();
    // No longer an open/unresolved change once the linked recovery is accepted.
    await expect(attention).not.toContainText("1 项变化");
  });

  test("an UNRELATED accepted proposal still leaves the change unresolved on Today", async ({
    page,
  }) => {
    await seed(page);
    const acceptedUnrelated: Proposal = {
      id: "r2",
      status: "ACCEPTED",
      trigger_event_id: "evt-999",
      hard_constraints_passed: true,
      locked_commitments_preserved: true,
      diff: { removed: [], added: [], moved: [] },
    };
    await mockApi(page, {
      experience: experienceFixture([WEATHER_EVENT]),
      replans: [acceptedUnrelated],
    });
    await page.goto("/v8/today", { waitUntil: "domcontentloaded" });

    const attention = page.getByTestId("today-attention");
    await expect(attention).toBeVisible();
    // Accepting a proposal for a DIFFERENT event must not clear this one on Today.
    await expect(attention).toContainText("1 项变化");
  });
});
