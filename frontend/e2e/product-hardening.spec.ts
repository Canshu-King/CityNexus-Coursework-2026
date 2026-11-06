import { expect, test } from "@playwright/test";
import { protectCommitment } from "./_qoderFreshJourney";

/* PORT-FIRST (pass-15 B) — autonomous recovery that stays inside its envelope.
 *
 * The legacy case drove the Control Tower: mode/cost/drift inputs, an event
 * simulator, and a policy-decision badge that only existed on that surface.  The
 * capability is not legacy — envelope-bounded autonomous execution is what
 * CityNexus is building — so the behaviour check moves to the current product
 * instead of being archived with the widget that used to show it.
 *
 * What must still be proven here, and what deliberately is NOT proven:
 *   proven: the Agent Gateway decides under the traveller's own policy, executes
 *           only inside the cost and drift envelope, keeps locked commitments,
 *           changes which plan is authoritative, and the current V8 recovery
 *           surface renders that outcome with its evidence.
 *   not proven, and not claimed: that a traveller had to do nothing.  A user
 *           action initiates the chain here on purpose — the zero-touch case is
 *           `live-autopilot-v5`, and merging the two would leave the suite with
 *           two differently-named tests that check the same thing.
 *
 * Setup uses public product endpoints because the low-level policy dials are not
 * a consumer screen any more; the observation is on the surface a traveller
 * reaches.  No legacy control is resurrected, and no assertion is reduced to a
 * 200 or a mode label — a server that nods is not a server that behaved.
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
        ? await request.put(`${API}${url}`, {
            ...options,
            headers: { "Content-Type": "application/json" },
          })
        : await request.post(`${API}${url}`, {
            ...options,
            headers: { "Content-Type": "application/json" },
          });
  if (!response.ok()) {
    throw new Error(
      `${method} ${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 220)}`,
    );
  }
  return (await response.json()) as T;
}

type Proposal = {
  id: string;
  status: string;
  proposed_plan_version_id: string;
  locked_commitments_preserved: boolean;
  trigger_event_id: string | null;
  diff: {
    removed?: string[];
    added?: string[];
    cost_delta_minor?: number;
    travel_delta_min?: number;
  };
};

type ToolResponse = {
  decision: string;
  executed: boolean;
  reason_codes?: string[];
  result?: { journey?: { active_plan_version_id?: string } };
};

const COST_LIMIT_MINOR = 5000;
const DRIFT_LIMIT_MIN = 120;

test("an autonomous recovery executes inside its envelope and is observable on the current recovery surface", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));

  const demo = await api<{ traveler_id: string; journey_id: string }>(
    request,
    "/api/v1/demo/reset",
    "POST",
  );
  const journeyId = demo.journey_id;

  /* The envelope is configured through the product's own policy endpoint, and
     read back: an assertion about "inside the envelope" is worthless if the limit
     was never actually stored. */
  const policy = await api<{
    mode: string;
    auto_accept_replan: boolean;
    max_auto_replan_cost_delta_minor: number;
    max_auto_schedule_drift_min: number;
  }>(request, `/api/v1/travelers/${demo.traveler_id}/agent-policy`, "PUT", {
    mode: "AUTONOMOUS",
    auto_accept_replan: true,
    max_auto_replan_cost_delta_minor: COST_LIMIT_MINOR,
    max_auto_loss_minor: COST_LIMIT_MINOR,
    max_auto_schedule_drift_min: DRIFT_LIMIT_MIN,
    allow_auto_when_provider_degraded: false,
    allow_auto_on_critical_event: false,
  });
  expect(policy.mode).toBe("AUTONOMOUS");
  expect(policy.auto_accept_replan).toBe(true);
  expect(policy.max_auto_replan_cost_delta_minor).toBe(COST_LIMIT_MINOR);
  expect(policy.max_auto_schedule_drift_min).toBe(DRIFT_LIMIT_MIN);

  const plans = await api<{ id: string; activities: { id: string; place_id: string | null }[] }[]>(
    request,
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await api(request, `/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");
  /* The envelope promises it will not spend a protected commitment, so this case
     has to own one: the demo cruise is only `locked` until a booking exists and
     its payment is captured, and `protected` is derived from ACTIVE Commitment
     rows.  Before /start, because booking a travelling activity is refused. */
  const commitment = await protectCommitment(request, journeyId, "place-river-cruise");
  await api(request, `/api/v1/journeys/${journeyId}/start`, "POST");
  const before = await api<{ active_plan: { id: string } }>(
    request,
    `/api/v1/journeys/${journeyId}/experience`,
    "GET",
  );
  const techArt = plans[0].activities.find((a) => a.place_id === "place-tech-art");
  expect(techArt, "the demo plan has no activity to close").toBeTruthy();

  const event = await api<{ id: string }>(
    request,
    `/api/v1/journeys/${journeyId}/events`,
    "POST",
    {
      event_type: "CLOSURE",
      severity: "MAJOR",
      affected_activity_id: techArt!.id,
      occurred_at: "2026-10-10T13:25:00+08:00",
      payload: { reason: "pass-15 envelope-safe recovery" },
    },
  );

  const requestReceipt = await api<ToolResponse>(
    request,
    "/api/v1/agent/tools/request_replan",
    "POST",
    {
      request_id: `p15b-${event.id}`,
      agent_id: "citynexus-local-agent",
      journey_id: journeyId,
      trigger_event_id: event.id,
      candidate_count: 3,
    },
  );
  expect(
    requestReceipt.decision,
    "an AUTONOMOUS policy with auto-accept still had to ask the traveller",
  ).toBe("ALLOW_AUTO");

  const proposals = await api<Proposal[]>(
    request,
    `/api/v1/journeys/${journeyId}/replans`,
    "GET",
  );
  const presented = proposals.filter(
    (p) => p.trigger_event_id === event.id && p.status === "PRESENTED",
  );
  expect(presented.length, "the gateway offered no proposal for this incident").toBeGreaterThan(0);
  const proposal = presented[0];

  const acceptReceipt = await api<ToolResponse>(
    request,
    "/api/v1/agent/tools/accept_replan",
    "POST",
    {
      request_id: `p15b-accept-${proposal.id}`,
      agent_id: "citynexus-local-agent",
      journey_id: journeyId,
      proposal_id: proposal.id,
    },
  );
  expect(acceptReceipt.decision).toBe("ALLOW_AUTO");
  expect(
    acceptReceipt.executed,
    `the gateway allowed the action but did not run it (reasons: ${JSON.stringify(acceptReceipt.reason_codes ?? [])})`,
  ).toBe(true);

  /* The envelope, checked against the numbers the proposal itself carried. */
  expect(
    proposal.locked_commitments_preserved,
    "the recovery spent a commitment it was not allowed to touch",
  ).toBe(true);
  expect(proposal.diff.cost_delta_minor ?? 0).toBeLessThanOrEqual(COST_LIMIT_MINOR);
  expect(proposal.diff.travel_delta_min ?? 0).toBeLessThanOrEqual(DRIFT_LIMIT_MIN);
  expect(proposal.diff.cost_delta_minor ?? 0).toBeGreaterThanOrEqual(-COST_LIMIT_MINOR);
  expect(proposal.diff.travel_delta_min ?? 0).toBeGreaterThanOrEqual(-DRIFT_LIMIT_MIN);

  /* And the plan that is now authoritative really is the child it executed. */
  const after = await api<{
    active_plan: {
      id: string;
      activities?: { title?: string; place_id?: string | null; protected?: boolean }[];
    };
  }>(request, `/api/v1/journeys/${journeyId}/experience`, "GET");
  expect(after.active_plan.id).not.toBe(before.active_plan.id);
  expect(after.active_plan.id).toBe(proposal.proposed_plan_version_id);
  /* Identity, not wording: an executed replan mints new activity ids, so only
     place_id can show the committed place is the same one. */
  const preservedCruise = (after.active_plan.activities ?? []).find(
    (a) => a.place_id === "place-river-cruise",
  );
  expect(
    preservedCruise,
    `the committed River Cruise disappeared from the executed plan (booking ${commitment.bookingId}); ` +
      `it holds ${JSON.stringify((after.active_plan.activities ?? []).map((a) => a.place_id))}`,
  ).toBeTruthy();
  expect(
    preservedCruise?.protected,
    "the commitment survived the envelope but is no longer protected",
  ).toBe(true);

  /* Observation on the surface a traveller can actually reach. */
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/authoritative");
  const panel = page.getByTestId("v14-recovery-truth");
  await expect(panel, "the current recovery surface did not render").toBeVisible();
  await expect(page.getByTestId("v14-recovery-authorization")).toContainText(
    "AUTONOMOUS · envelope auto-accept enabled",
  );
  await expect(page.getByTestId("v14-recovery-protected")).toContainText("C4 commitments preserved");
  await expect(page.getByTestId("v14-recovery-provider")).not.toContainText("—");
  await expect(page.getByTestId("v14-recovery-changed")).toBeVisible();
  const accepted = page.getByTestId("v14-recovery-accepted");
  await expect(accepted, "the executed child plan version is not shown as accepted").toBeVisible();
  await expect(accepted).toContainText(proposal.proposed_plan_version_id);
  console.log(
    `[envelope] executed recovery: cost delta ${proposal.diff.cost_delta_minor ?? 0} of ${COST_LIMIT_MINOR}, ` +
      `drift ${proposal.diff.travel_delta_min ?? 0} of ${DRIFT_LIMIT_MIN}, plan ${before.active_plan.id} -> ${after.active_plan.id}`,
  );
});
