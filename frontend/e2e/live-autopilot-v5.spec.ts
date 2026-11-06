import { expect, test } from "@playwright/test";
import { protectCommitment } from "./_qoderFreshJourney";

/* PORT-FIRST (pass-15 C) — zero-touch means nobody presses anything.
 *
 * The legacy case watched the old Control Tower recover on its own after a
 * provider pulse.  Its value is *not* the same as the envelope case in
 * `product-hardening.spec.ts`: there, a decision is initiated and the point is
 * that it stays inside the traveller's limits; here, the point is that the
 * traveller never initiates anything at all.  Merging the two would leave the
 * suite with two differently-named tests that check one thing, so this file keeps
 * the distinction structural: after the trigger below, the test performs no
 * click, no fill and no accept — the only later browser work is reading a page.
 *
 * Chain that must be observed:
 *   provider/event pulse -> guardian autopilot -> solver/recovery -> active plan
 *   changes -> protected commitments intact -> the current V8 Live surface shows
 *   an automatic recovery -> reload keeps the result.
 *
 * Standing policy and the autopilot subscription are setup, which the ruling
 * allows explicitly: a system cannot be expected to recover for a traveller who
 * never asked it to watch.
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

test("the guardian autopilot recovers a live journey from a provider event with no traveller action", async ({
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

  /* --- setup: the same policy payload the product's own autonomy switch sends --- */
  await api(request, `/api/v1/travelers/${demo.traveler_id}/agent-policy`, "PUT", {
    mode: "AUTONOMOUS",
    auto_accept_replan: true,
    max_auto_replan_cost_delta_minor: 5000,
    max_auto_loss_minor: 0,
    max_auto_schedule_drift_min: 20,
    allowed_activity_types: ["transport", "visit"],
    allowed_booking_types: [],
    protected_commitment_levels: ["C3_Guaranteed", "C4_Anchored"],
    allow_auto_commitment_management: false,
    allow_auto_when_provider_degraded: false,
    allow_auto_on_critical_event: false,
  });

  const plans = await api<{ id: string; activities: { id: string; place_id: string | null }[] }[]>(
    request,
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await api(request, `/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");
  /* The autopilot is being trusted with a commitment, so there has to be one.
     The demo cruise is `locked` but not `protected` until a booking is filed and
     its payment captured, so without this step `locked_commitments_preserved`
     would be true for the wrong reason.  Before /start, because booking a
     travelling activity is refused with 422. */
  const commitment = await protectCommitment(request, journeyId, "place-river-cruise");
  await api(request, `/api/v1/journeys/${journeyId}/start`, "POST");

  const mission = await api<{ id: string; status: string }>(
    request,
    `/api/v1/journeys/${journeyId}/guardian/mission`,
    "PUT",
    {
      command: "Keep the evening afloat: prefer indoor, protect the cruise and dinner, spend up to 50 more.",
      requested_autonomy: "EXECUTE_WITHIN_BOUNDS",
    },
  );
  expect(mission.status, "the standing mission is not active").toBe("ACTIVE");

  const autopilot = await api<{ journey_id: string; enrollment?: { enabled: boolean } }>(
    request,
    `/api/v1/journeys/${journeyId}/guardian/autopilot/start`,
    "POST",
    { interval_seconds: 0.5 },
  );
  expect(autopilot.enrollment?.enabled, "the autopilot subscription did not enable").toBe(true);

  const before = await api<{ active_plan: { id: string } }>(
    request,
    `/api/v1/journeys/${journeyId}/experience`,
    "GET",
  );

  /* --- trigger: one provider-shaped event, and then nothing from a human --- */
  const techArt = plans[0].activities.find((a) => a.place_id === "place-tech-art");
  expect(techArt, "the plan has no activity for the incident to hit").toBeTruthy();
  const event = await api<{ id: string }>(
    request,
    `/api/v1/journeys/${journeyId}/events`,
    "POST",
    {
      event_type: "CLOSURE",
      severity: "MAJOR",
      affected_activity_id: techArt!.id,
      occurred_at: "2026-10-10T13:25:00+08:00",
      payload: { reason: "pass-15 zero-touch port" },
    },
  );

  let recoveredPlanId = "";
  await expect
    .poll(
      async () => {
        const now = await api<{ active_plan: { id: string } }>(
          request,
          `/api/v1/journeys/${journeyId}/experience`,
          "GET",
        );
        if (now.active_plan.id !== before.active_plan.id) {
          recoveredPlanId = now.active_plan.id;
          return true;
        }
        return false;
      },
      {
        message:
          "the autopilot never changed the authoritative plan on its own — no autonomous recovery happened " +
          "within the budget, which is a product gap, not a slow assertion",
        timeout: 60_000,
        intervals: [1000],
      },
    )
    .toBe(true);

  /* The record has to say it was the system, not the traveller. */
  const proposals = await api<
    {
      id: string;
      status: string;
      trigger_event_id: string | null;
      proposed_plan_version_id: string;
      locked_commitments_preserved: boolean;
    }[]
  >(request, `/api/v1/journeys/${journeyId}/replans`, "GET");
  const linked = proposals.filter((p) => p.trigger_event_id === event.id);
  expect(linked.length, "no proposal is linked to the incident event").toBeGreaterThan(0);
  const accepted = linked.find((p) => p.status === "ACCEPTED");
  expect(
    accepted,
    `the incident produced proposals but none was accepted autonomously (${JSON.stringify(
      linked.map((p) => p.status),
    )})`,
  ).toBeTruthy();
  expect(accepted!.proposed_plan_version_id).toBe(recoveredPlanId);
  expect(
    accepted!.locked_commitments_preserved,
    "the automatic recovery spent a locked commitment",
  ).toBe(true);

  /* And the flag has to mean something on the plan the autopilot installed:
     this journey owns a real captured booking now, so the cruise must still be
     on the recovered plan by place id, still flagged protected. */
  const recovered = await api<{
    active_plan: { id: string; activities?: { place_id?: string | null; protected?: boolean }[] };
  }>(request, `/api/v1/journeys/${journeyId}/experience`, "GET");
  const preservedCruise = (recovered.active_plan.activities ?? []).find(
    (a) => a.place_id === "place-river-cruise",
  );
  expect(
    preservedCruise,
    `the autopilot's recovered plan has no committed cruise (booking ${commitment.bookingId}); ` +
      `it holds ${JSON.stringify((recovered.active_plan.activities ?? []).map((a) => a.place_id))}`,
  ).toBeTruthy();
  expect(
    preservedCruise?.protected,
    "the recovered plan carries the cruise activity but no longer treats it as protected",
  ).toBe(true);

  /* The record has to say it was the system, not the traveller.  No .catch here:
     a swallowed 404 would arrive as an empty list and quietly turn the proof into
     nothing, which is the failure mode this whole pass exists to remove. */
  const feed = await api<{ source: string; events: { event_type: string; actor_type: string }[] }>(
    request,
    `/api/v1/journeys/${journeyId}/agent-feed?limit=100`,
    "GET",
  );
  const acceptances = feed.events.filter((item) =>
    /replan_auto_accepted|replan_user_approved|replan_user_confirmed|accept_replan/.test(
      String(item.event_type),
    ),
  );
  console.log(
    `[zero-touch] plan ${before.active_plan.id} -> ${recoveredPlanId} without a traveller action; ` +
      `feed source=${feed.source}, ${feed.events.length} entries, acceptances ` +
      `${JSON.stringify(acceptances.map((i) => `${i.event_type}/${i.actor_type}`))}`,
  );
  expect(
    acceptances.some((item) => item.actor_type === "AI_AGENT"),
    `the acceptance is not attributed to the agent, so nothing here proves it was automatic ` +
      `(acceptances: ${JSON.stringify(acceptances.map((i) => `${i.event_type}/${i.actor_type}`))})`,
  ).toBe(true);

  /* The current surface must show it, not just the database. */
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/authoritative");
  await expect(page.getByTestId("v14-recovery-truth")).toBeVisible();
  await expect(page.getByTestId("v14-recovery-accepted")).toContainText(recoveredPlanId);
  await expect(page.getByTestId("v14-recovery-protected")).toContainText("C4 commitments preserved");

  await page.goto("/v8/live");
  await expect(page.getByTestId("live-recovery")).toBeVisible();
  await expect(
    page.getByTestId("live-accept"),
    "Live is still asking the traveller to accept a recovery that already happened",
  ).toHaveCount(0);

  /* Reload keeps the result, so it is a fact and not a render. */
  await page.reload();
  await expect(page.getByTestId("live-recovery")).toBeVisible();
  const after = await api<{ active_plan: { id: string } }>(
    request,
    `/api/v1/journeys/${journeyId}/experience`,
    "GET",
  );
  expect(after.active_plan.id).toBe(recoveredPlanId);
});
