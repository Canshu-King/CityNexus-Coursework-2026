import { expect, test } from "@playwright/test";
import { protectCommitment } from "./_qoderFreshJourney";

/* C0 — the preventive panel must not freeze on a half-finished action.

   A GuardianRun is written as RUNNING / PLANNING before the recomposition lands
   and only later becomes COMPLETED / EXECUTED.  Pass-16 proved that in the API
   layer — and then the panel it owns stopped polling the moment a run appeared,
   which is the opposite conclusion.  A traveller who opened
   /v8/authoritative mid-action would have been left staring at "PLANNING" until
   they reloaded by hand.

   The real window is a race, so it is produced deterministically here: the first
   status response is served as the run still in flight, and every later response
   is the truth.  What the case asserts is that the surface recovers on its own. */

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
      ? await request.get(`${API}${url}`)
      : method === "PUT"
        ? await request.put(`${API}${url}`, options)
        : await request.post(`${API}${url}`, options);
  if (!response.ok()) {
    throw new Error(
      `${method} ${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`,
    );
  }
  return (await response.json()) as T;
}

test("a preventive run that was still planning updates to executed without a reload", async ({
  page,
  request,
}) => {
  test.setTimeout(150_000);

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
  const plans = await api<{ id: string; activities: { place_id: string }[] }[]>(
    request,
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await api(request, `/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");
  await protectCommitment(request, journeyId, "place-river-cruise");
  await api(request, `/api/v1/journeys/${journeyId}/start`, "POST");
  await api(request, `/api/v1/journeys/${journeyId}/guardian/mission`, "PUT", {
    command: "Keep the cruise protected, prefer indoor alternatives.",
    requested_autonomy: "EXECUTE_WITHIN_BOUNDS",
  });
  await api(request, `/api/v1/journeys/${journeyId}/guardian/provider-fixture`, "POST", {
    state: "RISK_FORECAST",
    payload: { risk_score: 88, forecast_horizon_min: 45, expected_route_delay_min: 18 },
  });

  /* Prevention is the autopilot's cycle; `/guardian/scan` is the reactive
     controller and never produces a preventive run.  Enrol, wait for the run to
     finish through the product's own loop, then stop so nothing else moves the
     state underneath the page. */
  await api(request, `/api/v1/journeys/${journeyId}/guardian/autopilot/start`, "POST", {
    interval_seconds: 0.5,
  });
  let runId = "";
  await expect
    .poll(
      async () => {
        const status = await api<{ runs: any[] }>(
          request,
          `/api/v1/journeys/${journeyId}/guardian/status`,
          "GET",
        );
        const finished = (status.runs ?? []).find(
          (item) =>
            item.decision === "PREEMPTIVE_RECOVERY" &&
            item.trigger_kind === "PREDICTIVE" &&
            item.status === "COMPLETED",
        );
        if (finished) runId = finished.id;
        return Boolean(finished) && finished.action_status === "EXECUTED";
      },
      {
        message: "the autopilot never completed an executed preventive run",
        timeout: 60_000,
        intervals: [1000],
      },
    )
    .toBe(true);
  await api(request, `/api/v1/journeys/${journeyId}/guardian/autopilot/stop`, "POST", {});

  let firstResponse = true;
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.locale", "en");
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.route(`**/api/v1/journeys/${journeyId}/guardian/status`, async (route) => {
    if (!firstResponse) {
      await route.fallback();
      return;
    }
    firstResponse = false;
    try {
      const body = await route.fetch().then((response) => response.json());
      /* The same run, one moment earlier in its life. */
      for (const item of body.runs ?? []) {
        if (item.id === runId) {
          item.status = "RUNNING";
          item.action_status = "PLANNING";
        }
      }
      await route.fulfill({ json: body });
    } catch {
      await route.fallback();
    }
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/authoritative");

  const action = page.getByTestId("v14-preventive-action");
  await expect(action).toHaveText("PLANNING");
  await expect(page.getByTestId("v14-preventive-trigger")).toHaveAttribute("data-run-id", runId);

  /* No reload, no click: the panel has to notice the run finished. */
  await expect(action).toHaveText("EXECUTED", { timeout: 20_000 });
  await expect(page.getByTestId("v14-preventive-timing")).toHaveAttribute("data-pre-failure", "true");
  await expect(page.getByTestId("v14-preventive-plan-change")).not.toHaveAttribute(
    "data-plan-after-id",
    "",
  );
});
