import { expect, test } from "@playwright/test";
import { protectCommitment } from "./_qoderFreshJourney";

/* golden — backend truth for a closure recovery, and the persistence half of it.
 *
 * The original case drove the legacy Journey Mode surface (`page.goto("/")`,
 * `journey-status`, the demo event simulator).  Pass-8 refused an archive until the
 * capability was shown to exist in the current product, and pass-14 kept it red
 * rather than pretend.  This is that port: same invariant, current surface.
 *
 * What it must prove, in the order the product actually does it:
 *   1. a closure is a real event on the journey, not a UI state;
 *   2. the current V8 Live surface presents the resulting proposal and the
 *      traveller's decision goes through the product's own accept endpoint;
 *   3. the accepted outcome is *backend* truth — the active plan identity
 *      changes server-side, and the twin agrees with the journey;
 *   4. after a reload the same resolved fact is still what the surface renders,
 *      and the backend still reports the same identity;
 *   5. protected commitments survive the change;
 *   6. the browser never held the truth: localStorage carries a journey id and a
 *      language, nothing about plans.
 *
 * A heading that survives a reload proves nothing, so the persistence assertions
 * are on plan identity and on the absence of the accepted proposal, not on text.
 *
 * Setup uses public product endpoints (the demo journey is the fixture that has a
 * real multi-activity plan; a freshly created journey has none, and the stay-aware
 * optimiser returns nothing for a one-day trip).  The decision itself is made by
 * clicking the current Live surface.
 */

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

async function call<T>(
  request: import("@playwright/test").APIRequestContext,
  url: string,
  method: "GET" | "POST",
  data?: unknown,
): Promise<T> {
  const response =
    method === "GET"
      ? await request.get(`${API}${url}`, { headers: { "Content-Type": "application/json" } })
      : await request.post(`${API}${url}`, {
          data,
          headers: { "Content-Type": "application/json" },
        });
  if (!response.ok()) {
    throw new Error(`${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

test("an accepted closure recovery is backend truth on V8 Live and survives reload", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));

  const api = <T,>(url: string, method: "GET" | "POST" = "GET", data?: unknown) =>
    call<T>(request, url, method, data);

  /* 1. A journey with a real plan. */
  const demo = await api<{ journey_id: string }>("/api/v1/demo/reset", "POST");
  const journeyId = demo.journey_id;
  const plans = await api<{ id: string; activities: { id: string; place_id: string | null; title: string }[] }[]>(
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  expect(plans.length, "the demo plan generator produced nothing").toBeGreaterThan(0);
  await api(`/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");

  const techArt = plans[0].activities.find((a) => a.place_id === "place-tech-art");
  const cruise = plans[0].activities.find((a) => a.place_id === "place-river-cruise");
  expect(techArt, "the plan has no Tech Art activity to close").toBeTruthy();
  expect(cruise, "the plan has no River Cruise commitment to protect").toBeTruthy();

  /* The commitment this recovery is not allowed to spend has to exist.  On the
     demo plan the cruise is `locked: true` but `protected: false` until a booking
     is created and its payment captured — `protected` is derived from ACTIVE
     Commitment rows — so without this step the case would be asserting that
     nothing was disturbed.  Booking is refused once the journey is travelling, so
     it goes here, before /start, through the public endpoints. */
  const commitment = await protectCommitment(request, journeyId, "place-river-cruise");

  /* Selecting a plan only makes it the current draft; the recovery machinery
     refuses a journey that is not travelling.  /start is the product's own
     transition (the client calls it startJourney), so the fixture goes the way a
     traveller does rather than poking the status column. */
  const twinAfterStart = await api<{ status?: string; active_plan_version_id?: string }>(
    `/api/v1/journeys/${journeyId}/start`,
    "POST",
  );
  expect(twinAfterStart.active_plan_version_id, "starting the journey produced no active plan").toBeTruthy();

  const before = await api<{ active_plan: { id: string } }>(
    `/api/v1/journeys/${journeyId}/experience`,
  );
  expect(before.active_plan?.id, "no active plan was selected").toBeTruthy();

  /* 2. A real closure event, then the current Live surface asks the traveller. */
  const event = await api<{ id: string }>(`/api/v1/journeys/${journeyId}/events`, "POST", {
    event_type: "CLOSURE",
    severity: "MAJOR",
    affected_activity_id: techArt!.id,
    occurred_at: "2026-10-10T13:25:00+08:00",
    payload: { reason: "pass-15 golden port" },
  });
  expect(event.id).toBeTruthy();
  await api("/api/v1/agent/tools/request_replan", "POST", {
    request_id: `golden-${event.id}`,
    agent_id: "frontend-agent",
    journey_id: journeyId,
    trigger_event_id: event.id,
    candidate_count: 3,
  });
  const proposals = await api<{ id: string; status: string; trigger_event_id: string | null }[]>(
    `/api/v1/journeys/${journeyId}/replans`,
  );
  const presented = proposals.filter(
    (p) => p.trigger_event_id === event.id && p.status === "PRESENTED",
  );
  expect(
    presented.length,
    "the closure produced no presented proposal for this event",
  ).toBeGreaterThan(0);

  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/live");
  await expect(page.getByTestId("live-recovery")).toBeVisible();

  const proposal = page.getByTestId("live-proposal").first();
  await expect(proposal, "V8 Live did not render the recovery the backend is offering").toBeVisible();
  /* The product's own promise about the change, before the traveller commits. */
  await expect(proposal.getByTestId("live-unchanged")).toContainText("stay unchanged");

  const accepted = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/replans/") &&
      response.url().endsWith("/accept") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  await proposal.getByTestId("live-accept").click();
  const receipt = await (await accepted).json();
  expect(receipt.journey?.active_plan_version_id, "acceptance did not produce an active plan version").toBeTruthy();
  expect(
    receipt.twin?.active_plan_version_id,
    "the twin and the journey disagree about which plan is active",
  ).toBe(receipt.journey.active_plan_version_id);
  await expect(page.getByTestId("live-message")).toContainText("Your trip has been updated.");

  /* 3. Backend truth, read back from the server rather than from the paint. */
  const after = await api<{ active_plan: { id: string } }>(`/api/v1/journeys/${journeyId}/experience`);
  expect(after.active_plan.id).not.toBe(before.active_plan.id);
  expect(after.active_plan.id).toBe(receipt.journey.active_plan_version_id);

  /* 4. The browser never owned the fact.  An exact key whitelist would be both
     brittle and beside the point — the Copilot dock legitimately keeps a chat
     history per journey in localStorage.  What golden must prove is that the
     *accepted plan* is not a client-side fact: no stored key talks about plans
     or replans, and no stored value contains the accepted plan's identity. */
  const stored = await page.evaluate(() =>
    Object.keys(localStorage).map((key) => [key, localStorage.getItem(key) ?? ""] as const),
  );
  const planShapedKeys = stored
    .map(([key]) => key)
    .filter((key) => /plan|replan|version|proposal/i.test(key));
  expect(planShapedKeys, "localStorage is holding plan-shaped state").toEqual([]);
  const leaked = stored
    .filter(([, value]) => value.includes(after.active_plan.id))
    .map(([key]) => key);
  expect(
    leaked,
    `the accepted plan id ${after.active_plan.id} is cached in ${leaked.join(", ")}, ` +
      `so a reload could be proving the browser rather than the server`,
  ).toEqual([]);

  /* 5. Reload: the same resolved fact is still what renders and what the server says. */
  await page.reload();
  await expect(page.getByTestId("live-recovery")).toBeVisible();
  await expect(
    page.getByTestId("live-accept"),
    "an accepted proposal came back as a decision to make again",
  ).toHaveCount(0);
  await expect(page.getByTestId("live-handled")).toBeVisible();

  const reloaded = await api<{
    active_plan: {
      id: string;
      activities?: {
        title?: string;
        place_id?: string | null;
        locked?: boolean;
        protected?: boolean;
      }[];
    };
  }>(`/api/v1/journeys/${journeyId}/experience`);
  expect(reloaded.active_plan.id, "the accepted plan did not survive a reload")
    .toBe(after.active_plan.id);

  /* 6. The commitment the recovery was not allowed to spend — compared by
     identity, not wording.  `place_id` is the stable key and an accepted replan
     mints new activity ids, so a title match could pass while the place changed
     and fail while nothing did.  Pass-16 fixed the projection that made this
     impossible: /experience used to report place_id as null on the accepted plan,
     which pushed consumers onto titles; GET /plans/{id} and /experience now agree.
     `protected` is only true because step 1 filed a real booking and captured it. */
  const activities = reloaded.active_plan.activities ?? [];
  const missingPlaceIds = activities.filter((item) => !item.place_id).length;
  const preserved = activities.find((item) => item.place_id === "place-river-cruise");
  console.log(
    `[golden] accepted plan ${reloaded.active_plan.id} reports ${activities.length} activities, ` +
      `${missingPlaceIds} without a place_id; commitment booking=${commitment.bookingId} ` +
      `commitment=${commitment.commitmentId ?? "n/a"} ` +
      `cruise(locked=${preserved?.locked},protected=${preserved?.protected})`,
  );
  expect(missingPlaceIds, "the accepted plan is still hiding activity identity")
    .toBe(0);
  expect(
    preserved,
    `the committed River Cruise is not on the accepted plan; it holds ${JSON.stringify(
      activities.map((item) => item.place_id),
    )}`,
  ).toBeTruthy();
  expect(preserved?.protected, "the surviving cruise activity is no longer a protected commitment")
    .toBe(true);
});
