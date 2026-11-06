import { expect, test } from "@playwright/test";
import { protectCommitment } from "./_qoderFreshJourney";

/* PORT-FIRST (pass-15 D, real evidence chain in pass-16 A/B/D) — prevention, not
 * detection, and not recovery-after-the-fact.
 *
 * `v13-5-fully-grounded` already proves grounded discovery and that a weakness can
 * be *seen*.  What this case must still prove is the half nothing else covers:
 * CityNexus acts before the failure happens, and the traveller can see why.
 *
 * So the assertions are deliberately shaped as:
 *   - the alternative it would move to is a real, replay-provenanced city POI, so
 *     prevention is grounded in the city twin rather than in demo placeholders;
 *   - a genuine protected commitment exists *before* the forecast — created with
 *     the public booking + CAPTURE path, because `locked` alone is not a promise;
 *   - no hard failure exists at the moment of action (checked, not assumed);
 *   - a forecast pulse with a real payload alone produces a preventive run, whose
 *     evidence carries the predictive status, the solver that recomposed the plan
 *     and `forecast_before_hard_failure`;
 *   - the plan really changes, and the commitment is still on it by place id;
 *   - nobody clicked anything (a scan request would make this reactive, not predictive);
 *   - a current V8 surface owns the result, asserted field by field.
 *
 * The last bullet is what was missing in pass-15: the run existed and the plan
 * changed, but only `src/main.tsx` (i.e. `/legacy`) rendered it.  Pass-16 added
 * `V14PreventivePanel` to `/v8/authoritative`, so the assertions below bind to
 * structured testids and to the guardian run id rather than to the body-text
 * sentinel this file used while the gap was open.
 */

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

async function api<T>(
  request: import("@playwright/test").APIRequestContext,
  url: string,
  method: "GET" | "POST" | "PUT",
  data?: unknown,
): Promise<T> {
  const options = data === undefined ? {} : { data };
  const response =
    method === "GET"
      ? await request.get(`${API}${url}`, { headers: { "Content-Type": "application/json" } })
      : method === "PUT"
        ? await request.put(`${API}${url}`, { ...options, headers: { "Content-Type": "application/json" } })
        : await request.post(`${API}${url}`, { ...options, headers: { "Content-Type": "application/json" } });
  if (!response.ok()) {
    throw new Error(
      `${method} ${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 220)}`,
    );
  }
  return (await response.json()) as T;
}

type Activity = {
  id: string;
  place_id: string | null;
  title?: string | null;
  start_at?: string | null;
  locked?: boolean;
  protected?: boolean;
};

type GuardianRun = {
  id: string;
  decision: string;
  status: string;
  action_status: string;
  trigger_kind: string | null;
  reasoning_summary?: string | null;
  active_plan_before_id: string | null;
  active_plan_after_id: string | null;
  evidence?: {
    observation?: { trigger_kind?: string; forecast_before_hard_failure?: boolean };
    prediction?: {
      status?: string | null;
      risk_score?: number | null;
      evidence_provenance?: { provider_key: string; provenance: string }[];
    } & Record<string, unknown>;
    inference?: { decision?: string; reason?: string };
    solver?: { kind?: string } | null;
  };
};

test("a forecast is prevented before the failure happens, and the traveller can see why", async ({
  page,
  request,
}) => {
  test.setTimeout(150_000);
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));

  const demo = await api<{ traveler_id: string; journey_id: string }>(
    request,
    "/api/v1/demo/reset",
    "POST",
  );
  const journeyId = demo.journey_id;

  await api(request, `/api/v1/travelers/${demo.traveler_id}/agent-policy`, "PUT", {
    mode: "AUTONOMOUS",
    auto_accept_replan: true,
    max_auto_replan_cost_delta_minor: 5000,
    max_auto_loss_minor: 0,
    max_auto_schedule_drift_min: 120,
    allowed_activity_types: ["transport", "visit"],
    allowed_booking_types: [],
    protected_commitment_levels: ["C3_Guaranteed", "C4_Anchored"],
    allow_auto_commitment_management: false,
    allow_auto_when_provider_degraded: false,
    allow_auto_on_critical_event: false,
  });

  const plans = await api<{ id: string; activities: Activity[] }[]>(
    request,
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await api(request, `/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");

  /* A commitment has to exist before prevention can be credited with keeping it.
     Measured in pass-16: the demo cruise is `locked: true` but `protected: false`
     until a booking is created and its payment captured, so asserting on
     `protected` without this step asserts on a fixture with nothing to protect.
     `/bookings` refuses an activity whose journey is already travelling, so this
     is deliberately before `/start`. */
  const commitment = await protectCommitment(request, journeyId, "place-river-cruise");

  await api(request, `/api/v1/journeys/${journeyId}/start`, "POST");
  const mission = await api<{ status: string }>(
    request,
    `/api/v1/journeys/${journeyId}/guardian/mission`,
    "PUT",
    {
      command:
        "Keep the cruise and dinner protected, prefer indoor alternatives, allow autonomous reversible recovery.",
      requested_autonomy: "EXECUTE_WITHIN_BOUNDS",
    },
  );
  expect(mission.status).toBe("ACTIVE");
  await api(
    request,
    `/api/v1/journeys/${journeyId}/guardian/autopilot/start`,
    "POST",
    { interval_seconds: 0.5 },
  );

  /* Ground the prevention in the real city twin first: the alternative the
     guardian will move to must exist as a replay-provenanced POI, otherwise a
     green preventive run would only prove the mock could edit the mock. */
  const discovered = await api<{
    provenance: string;
    mode: string;
    candidates: { name: string; provider_key?: string }[];
  }>(request, `/api/v1/journeys/${journeyId}/real-city/discover`, "POST", {
    query: "indoor",
    mode: "replay",
    limit: 3,
  });
  expect(discovered.provenance, "real-city discovery was not replay-provenanced").toBe("REPLAY");
  expect(
    discovered.candidates.some((item) => /Tianjin|Haihe/i.test(item.name)),
    `no real-city POI in ${JSON.stringify(discovered.candidates.map((c) => c.name))}`,
  ).toBe(true);

  const before = await api<{
    active_plan: { id: string; activities?: Activity[] };
  }>(request, `/api/v1/journeys/${journeyId}/experience`, "GET");

  /* "Before the failure" has to be a measured fact.  A hard failure here means a
     CLOSURE or CRITICAL event on the journey; none is injected in this test, and
     the assertion proves the preventive action was not a reaction to something
     that had already gone wrong. */
  const eventsBefore = await api<{ event_type: string; severity: string }[]>(
    request,
    `/api/v1/journeys/${journeyId}/events`,
    "GET",
  );
  const hardFailures = eventsBefore.filter((e) =>
    /CLOSURE/.test(e.event_type) || e.severity === "CRITICAL",
  );
  expect(
    hardFailures.length,
    "a hard failure already exists on this journey, so anything after it is recovery, not prevention",
  ).toBe(0);

  /* A scan request would mean the test asked for the decision, which is exactly
     what makes a case reactive. */
  let scanRequests = 0;
  page.on("request", (entry) => {
    if (entry.url().endsWith("/guardian/scan")) scanRequests += 1;
  });

  /* A real forecast, not an empty pulse: these numbers are what the resilience
     snapshot has to reproduce, so the run cannot pass on a default. */
  const fixture = await api<{ state: string }>(
    request,
    `/api/v1/journeys/${journeyId}/guardian/provider-fixture`,
    "POST",
    {
      state: "RISK_FORECAST",
      payload: {
        risk_score: 88,
        forecast_horizon_min: 45,
        expected_route_delay_min: 18,
      },
    },
  );
  expect(fixture.state).toBe("RISK_FORECAST");

  let preventive: GuardianRun | null = null;
  await expect
    .poll(
      async () => {
        const status = await api<{ runs: GuardianRun[] }>(
          request,
          `/api/v1/journeys/${journeyId}/guardian/status`,
          "GET",
        );
        preventive =
          status.runs.find(
            (run) => run.decision === "PREEMPTIVE_RECOVERY" && run.trigger_kind === "PREDICTIVE",
          ) ?? null;
        return preventive !== null;
      },
      {
        message:
          "no PREEMPTIVE_RECOVERY run appeared from a forecast alone — the backend does not perform " +
          "autonomous prevention, which is a capability gap rather than a surface gap",
        timeout: 60_000,
        intervals: [1000],
      },
    )
    .toBe(true);

  const appeared = preventive as unknown as GuardianRun;
  /* The row is written before the work finishes: measured, it appears with
     status=RUNNING / action_status=PLANNING and only later carries EXECUTED and
     the solver evidence.  Waiting for the same run id to reach a terminal state
     is not a retry — reading it mid-flight would just assert on a half-truth. */
  await expect
    .poll(
      async () => {
        const status = await api<{ runs: GuardianRun[] }>(
          request,
          `/api/v1/journeys/${journeyId}/guardian/status`,
          "GET",
        );
        const fresh = status.runs.find((run) => run.id === appeared.id) ?? null;
        if (fresh) preventive = fresh;
        return fresh?.status ?? "MISSING";
      },
      {
        message:
          "the preventive run never left PLANNING — prevention started and did not reach a terminal " +
          "state, so nothing was actually done about the forecast",
        timeout: 60_000,
        intervals: [1000],
      },
    )
    .toBe("COMPLETED");

  const run = preventive as unknown as GuardianRun;
  expect(run.action_status, "the preventive run never executed").toBe("EXECUTED");
  expect(run.evidence?.observation?.forecast_before_hard_failure, "the run does not claim to be pre-failure")
    .toBe(true);
  expect(
    run.evidence?.prediction?.status,
    "the forecast pulse did not produce a predictive weakness",
  ).toBe("PREDICTIVE_WEAKNESS");
  expect(
    run.evidence?.solver?.kind,
    "the preventive plan did not come from the CP-SAT solver the twin validates",
  ).toBe("ORTOOLS_CP_SAT_V2");
  expect(run.active_plan_after_id, "the preventive action left no plan identity behind").toBeTruthy();
  expect(
    run.active_plan_before_id,
    "the preventive run did not record which plan it moved away from",
  ).toBe(before.active_plan.id);

  const after = await api<{
    active_plan: { id: string; current_activity_id?: string | null; activities?: Activity[] };
  }>(request, `/api/v1/journeys/${journeyId}/experience`, "GET");
  expect(after.active_plan.id, "the authoritative plan did not change after prevention")
    .not.toBe(before.active_plan.id);

  /* Identity, not wording: `place_id` is the stable key and an accepted replan
     mints new activity ids, so this is the only comparison that can fail. */
  const activities = after.active_plan.activities ?? [];
  const cruise = activities.find((item) => item.place_id === "place-river-cruise");
  expect(
    cruise,
    `the committed River Cruise is not on the prevented plan; it holds ${JSON.stringify(
      activities.map((item) => item.place_id),
    )}`,
  ).toBeTruthy();
  expect(cruise?.protected, "the commitment survived prevention but is no longer protected").toBe(true);
  expect(
    activities.some((item) => item.place_id),
    "the prevented plan exposes no activity identity at all",
  ).toBe(true);

  /* What the recomposition actually did, printed rather than assumed.  This is
     the honest record of a claim pass-16 tried to make and could not: the
     preventive plan does NOT gain a `real-place-*` activity in this environment.
     The v6 pytest case only gets one because it seeds specific legs as explicit
     replay facts, which changes which alternatives the solver finds affordable;
     with live mobility the recomposition keeps the demo places and buys slack
     instead.  Asserting otherwise here would test the fixture, not the product. */
  const signature = (items: Activity[]) =>
    JSON.stringify(items.map((item) => [item.place_id, item.start_at ?? "?"]));
  console.log(
    `[prevention] recomposition changed: ${
      signature(before.active_plan.activities ?? []) !== signature(activities)
    }\n` +
      `  before ${signature(before.active_plan.activities ?? [])}\n` +
      `  after  ${signature(activities)}`,
  );

  /* The real-city half that IS provable here: the providers the guardian reasons
     over are replay-provenanced OpenStreetMap candidates for this journey, not a
     demo-only namespace. */
  const mesh = await api<{
    real_place_candidates: { provider_key: string; name: string; status: string }[];
  }>(request, `/api/v1/journeys/${journeyId}/provider-mesh`, "GET");
  expect(
    mesh.real_place_candidates.some((item) => item.provider_key === "openstreetmap-replay"),
    `no replay-provenanced real-city candidate in the mesh: ${JSON.stringify(
      mesh.real_place_candidates.map((item) => [item.provider_key, item.name]),
    )}`,
  ).toBe(true);

  /* `trigger_kind=PREDICTIVE` is not proof of a grounded prediction, so the run's
     own evidence is checked: it must name recorded provider observations, and every
     one of them must be REPLAY rather than a mock default that got laundered into a
     hard fact.  Measured on this journey: open-meteo-replay (CITY_SIGNAL) and
     city-signal-fixture (MOBILITY), both provenance REPLAY. */
  const evidenceProvenance = run.evidence?.prediction?.evidence_provenance ?? [];
  expect(
    evidenceProvenance.length,
    "the preventive run acted on no recorded provider evidence at all",
  ).toBeGreaterThan(0);
  expect(
    evidenceProvenance.every((item) => item.provenance === "REPLAY"),
    `the prediction leaned on non-replay evidence: ${JSON.stringify(
      evidenceProvenance.map((item) => [item.provider_key, item.provenance]),
    )}`,
  ).toBe(true);
  expect(
    evidenceProvenance.some((item) => /replay$/.test(item.provider_key)),
    "no replay provider backs the prediction the prevention acted on",
  ).toBe(true);

  /* The forecast the run acted on has to be the forecast that was filed. */
  const resilience = await api<{ status: string; components: Record<string, number> }>(
    request,
    `/api/v1/journeys/${journeyId}/resilience`,
    "GET",
  );
  expect(resilience.status).toBe("PREDICTIVE_WEAKNESS");
  expect(resilience.components.forecast_risk, "the snapshot ignored the supplied forecast payload")
    .toBe(88);

  expect(scanRequests, "the test itself asked for the decision, so this is not zero-touch").toBe(0);
  /* The ruling refuses `trigger_kind=PREDICTIVE` alone as proof of a grounded
     real-city prediction, so the run record carries what the evidence actually
     names rather than what the case's title claims. */
  console.log(
    `[prevention] prediction provenance ${JSON.stringify(
      ((run.evidence?.prediction ?? {}) as { evidence_provenance?: unknown }).evidence_provenance ?? [],
    )} · mesh candidates ${mesh.real_place_candidates.length}`,
  );
  console.log(
    `[prevention] run=${run.id} decision=${run.decision} trigger=${run.trigger_kind} ` +
      `action=${run.action_status} solver=${run.evidence?.solver?.kind} ` +
      `booking=${commitment.bookingId} commitment=${commitment.commitmentId ?? "n/a"} ` +
      `plan ${before.active_plan.id} -> ${after.active_plan.id} with no hard failure on the journey ` +
      `and ${scanRequests} scan requests`,
  );

  /* A current surface has to own it.  Pass-15 left this red with a body-text
     sentinel; pass-16 added V14PreventivePanel on /v8/authoritative, so the
     assertions below are structured and bound to the guardian run id. */
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/authoritative");
  await expect(page.getByTestId("v14-recovery-truth")).toBeVisible();
  await expect(page.getByTestId("v14-preventive")).toBeVisible();
  await expect(page.getByTestId("v14-preventive-decision")).toHaveText("PREEMPTIVE_RECOVERY");
  await expect(page.getByTestId("v14-preventive-trigger")).toHaveAttribute("data-run-id", run.id);
  await expect(page.getByTestId("v14-preventive-action")).toHaveText("EXECUTED");
  await expect(page.getByTestId("v14-preventive-timing")).toHaveAttribute("data-pre-failure", "true");
  await expect(page.getByTestId("v14-preventive-resilience")).toContainText("PREDICTIVE_WEAKNESS");
  /* The component, not the aggregate: measured in this run, a filed forecast of 88
     produces an aggregate score of 100 once the other components stack, so only
     forecast_risk can show the panel is rendering this journey's forecast. */
  await expect(page.getByTestId("v14-preventive-resilience")).toContainText("forecast 88");
  await expect(page.getByTestId("v14-preventive-evidence")).toContainText("ORTOOLS_CP_SAT_V2");
  await expect(page.getByTestId("v14-preventive-reason")).not.toHaveText("—");
  await expect(page.getByTestId("v14-preventive-plan-change")).toHaveAttribute(
    "data-plan-before-id",
    before.active_plan.id,
  );
  await expect(page.getByTestId("v14-preventive-plan-change")).toHaveAttribute(
    "data-plan-after-id",
    after.active_plan.id,
  );
  await expect(page.getByTestId("v14-preventive-protected")).toContainText("place-river-cruise");

  /* The traveller's own view of the same fact.  Live has no plan id on screen, so
     the strongest thing it can prove is that it is rendering the prevented plan:
     its current-activity heading comes from the same /experience payload, matched
     by activity id rather than by wording invented in this test. */
  const current = activities.find((item) => item.id === after.active_plan.current_activity_id);
  await page.goto("/v8/live");
  if (current?.title) {
    await expect(page.getByTestId("live-journey-current")).toHaveText(current.title);
  } else {
    await expect(page.getByTestId("live-journey-current")).toBeVisible();
  }
  /* Prevention is owned by the authoritative surface; Live must not grow a second,
     drifting copy of the same claim. */
  await expect(page.getByTestId("v14-preventive")).toHaveCount(0);
});
