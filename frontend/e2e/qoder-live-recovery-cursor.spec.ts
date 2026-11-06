import { expect, test } from "@playwright/test";

/* The Live recovery card must name the step the projection resolved, not the first one.

   LiveRecovery read `context.execution.current_activity_id`, a key the shared execution
   truth never returns - its keys are now_activity_id / next_activity_id / cursor_status /
   persisted_cursor (the *twin* carries a field by that name one level up, and the two were
   confused).  The read was therefore permanently null, and the `?? activities[0]` that
   followed it pinned the opening stop as "now" forever: two completions later the card
   still said the traveller was at the first activity.  That is CX-SOL-001's failure class
   in a second consumer, which is why it has to be proven on this surface too.

   What this gate can and cannot claim, stated because the CSS makes it matter: the
   recovery card's intro line is deliberately `display: none` inside `.live-shell`
   (design-tokens.css, "Live already shows the single current state; hide LiveRecovery's
   duplicate header, kept in the DOM"), so the wrong step was never *read* by a traveller
   on this page today - it was computed and rendered into the DOM, where a screen reader
   skips it but any other consumer of that markup does not.  So each case asserts two
   things: the identity the card resolved (from the DOM it renders), and that the visible
   Live heading agrees with it.  When the UI lane restakes this fold - which is exactly
   what Amendment C is about - the second half keeps the card from surfacing a lie.

   Case 1 uses the product's own transitions with no interception at all.  Case 2 hands the
   surface the contradictory state, which no endpoint can produce, and asserts it refuses
   to name any step. */

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

type Activity = { id: string; title: string; status: string };
type Execution = {
  now_activity_id: string | null;
  next_activity_id: string | null;
  cursor_status: string | null;
  persisted_cursor: string | null;
};

async function call<T>(
  request: import("@playwright/test").APIRequestContext,
  url: string,
  method: "GET" | "POST" = "GET",
  data?: unknown,
): Promise<T> {
  const response =
    method === "GET"
      ? await request.get(`${API}${url}`)
      : await request.post(`${API}${url}`, { data });
  if (!response.ok()) {
    throw new Error(
      `${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`,
    );
  }
  return (await response.json()) as T;
}

/** A journey the traveller is genuinely inside, through the product's own endpoints. */
async function startedJourney(
  request: import("@playwright/test").APIRequestContext,
): Promise<{ journeyId: string; activities: Activity[] }> {
  const demo = await call<{ journey_id: string }>(request, "/api/v1/demo/reset", "POST");
  const plans = await call<{ id: string; activities: Activity[] }[]>(
    request,
    `/api/v1/journeys/${demo.journey_id}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await call(
    request,
    `/api/v1/journeys/${demo.journey_id}/plans/${plans[0].id}/select`,
    "POST",
  );
  await call(request, `/api/v1/journeys/${demo.journey_id}/start`, "POST");
  return { journeyId: demo.journey_id, activities: plans[0].activities };
}

/** The two Live surfaces: the visible heading, and the recovery card's resolved line. */
async function openLive(page: import("@playwright/test").Page, journeyId: string) {
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.locale", "en");
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/live");
  const card = page.getByTestId("live-next");
  // Attached, not visible: `.live-shell` suppresses the card's duplicate header by
  // design, and asserting visibility here would test the stylesheet instead of the
  // resolution.  The visible surface below is the one travellers read.
  await expect(card).toBeAttached();
  const heading = page.getByTestId("live-journey-current");
  await expect(heading).toBeVisible();
  return { card, heading };
}

test("the recovery card follows the cursor the product itself advanced", async ({
  page,
  request,
}) => {
  const { journeyId, activities } = await startedJourney(request);
  expect(activities.length).toBeGreaterThanOrEqual(3);

  // Real transitions: complete the step the journey is on, twice.  Each completion
  // moves the persisted cursor to the next non-terminal activity, so the card's Now
  // has to be the third step - and demonstrably not the first.
  await call(
    request,
    `/api/v1/journeys/${journeyId}/activities/${activities[0].id}/complete`,
    "POST",
  );
  await call(
    request,
    `/api/v1/journeys/${journeyId}/activities/${activities[1].id}/complete`,
    "POST",
  );

  const twin = await call<{ context: { execution: Execution } }>(
    request,
    `/api/v1/journeys/${journeyId}/twin`,
  );
  expect(twin.context.execution.cursor_status).toBe("VALID");
  expect(twin.context.execution.now_activity_id).toBe(activities[2].id);

  const { card, heading } = await openLive(page, journeyId);
  await expect(card).toHaveAttribute("data-cursor-status", "VALID");
  await expect(card).toHaveAttribute("data-now-activity-id", activities[2].id);
  await expect(card).not.toHaveAttribute("data-now-activity-id", activities[0].id);
  await expect(card).toContainText(activities[2].title);
  // The visible Live heading must not disagree about which step is now.
  await expect(heading).toHaveAttribute("data-cursor-status", "VALID");
  await expect(heading).toHaveText(activities[2].title);
  // Neither may the V10 panel, which used to decide this from the raw cursor.
  const step = page.getByTestId("v10-current-step");
  await expect(step).toHaveAttribute("data-cursor-status", "VALID");
  await expect(step).toHaveText("in progress");
});

test("a plan that holds no activities invents no current step", async ({
  page,
  request,
}) => {
  // The third named state in ORD-0011 (2): nothing to resolve from at all.  All three
  // surfaces that could fill the gap - the twin's execution truth, the projection's plan
  // window, and the card's own activity list - are handed the same empty plan, and none of
  // them may produce a step out of nothing.
  const { journeyId } = await startedJourney(request);

  // The card's list comes from /plans, not from the projection, so emptiness has to be
  // applied there as well - otherwise this case measures a page whose list is full and
  // proves nothing about an empty plan.
  await page.route(`**/api/v1/journeys/${journeyId}/plans`, async (route) => {
    let body: { activities?: Activity[] }[];
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return;
    }
    if (Array.isArray(body)) {
      for (const plan of body) {
        if (Array.isArray(plan?.activities)) plan.activities = [];
      }
    }
    await route.fulfill({ json: body });
  });

  await page.route(`**/api/v1/journeys/${journeyId}/experience`, async (route) => {
    let body: { active_plan?: { current_activity_id?: string; activities: Activity[] } };
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return;
    }
    if (body.active_plan) {
      body.active_plan.current_activity_id = null;
      body.active_plan.activities = [];
    }
    await route.fulfill({ json: body });
  });

  await page.route(`**/api/v1/journeys/${journeyId}/twin`, async (route) => {
    let body: { context?: { execution?: Record<string, unknown> } };
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return;
    }
    if (body.context?.execution) {
      body.context.execution = {
        ...body.context.execution,
        persisted_cursor: null,
        now_activity_id: null,
        now: null,
        next_activity_id: null,
        next: null,
        next_leg_id: null,
        next_mode: "NONE",
        next_eta_min: 0,
        cursor_status: "ABSENT_FALLBACK",
      };
    }
    await route.fulfill({ json: body });
  });

  const { card, heading } = await openLive(page, journeyId);
  await expect(card).toHaveAttribute("data-now-activity-id", "");
  await expect(card).not.toContainText("Next:");
  // No cursor at all is the backend's own ABSENT_FALLBACK, not a fourth state.
  await expect(heading).toHaveAttribute("data-cursor-status", "ABSENT_FALLBACK");
  // No interception counters anywhere in this case, and no bookkeeping either: every
  // assertion above can only hold once the injected state has reached the surface, and a
  // handler the service worker answers from cache would otherwise fail a counter line
  // instead of the real one.
});

test("a cursor that resolves to a finished step yields no Now on the Live heading", async ({
  page,
  request,
}) => {
  // The saved position names a real activity, and that activity is COMPLETED: server-side
  // that is exactly the shape ORD-0007 measured (cursor VALID, Now empty, Next present),
  // and the frontend keeps its own guard for it inside liveActivityWindow.  Presenting a
  // finished step as "now" tells the traveller to do something they already did, and until
  // this case existed no observable behaviour depended on that guard at all.
  const { journeyId, activities } = await startedJourney(request);
  const finished = activities[1];
  await call(
    request,
    `/api/v1/journeys/${journeyId}/activities/${activities[0].id}/complete`,
    "POST",
  );

  let served = 0;
  await page.route(`**/api/v1/journeys/${journeyId}/experience`, async (route) => {
    let body: { active_plan?: { current_activity_id?: string; activities: Activity[] } };
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return;
    }
    if (body.active_plan) {
      body.active_plan.current_activity_id = finished.id;
      body.active_plan.activities = body.active_plan.activities.map((item) =>
        item.id === finished.id ? { ...item, status: "COMPLETED" } : item,
      );
    }
    served += 1;
    await route.fulfill({ json: body });
  });

  const { heading } = await openLive(page, journeyId);
  // The cursor resolves - it names a real activity that happens to be finished - so the
  // state is VALID with no Now, which is precisely what an "ABSENT" value used to hide.
  await expect(heading).toHaveAttribute("data-cursor-status", "VALID");
  await expect(heading).not.toHaveText(finished.title);
  await expect(heading).toHaveText("Nothing is due right now");
  // The discriminator for ORD-0026: the cursor exists and names a real activity,
  // so an existence test answers "in progress" where the resolution answers
  // "nothing is due".
  const step = page.getByTestId("v10-current-step");
  await expect(step).toHaveText("Nothing is due right now");
  await expect(step).not.toHaveText("in progress");
  // No interception counters here on purpose: every assertion above can only hold once the
  // injected state has actually reached the surface, and a handler the service worker bypasses
  // would otherwise fail this line instead of the real one.
});

test("an unresolvable cursor makes the recovery card name nothing", async ({
  page,
  request,
}) => {
  const { journeyId, activities } = await startedJourney(request);

  // Both surfaces are handed the same impossible record.  The card reads /twin and the
  // visible Live heading reads /experience; patching only one of them would make the
  // *test* invent a disagreement between the two surfaces and prove nothing about either.
  const ghost = "00000000-0000-4000-8000-000000000000";
  const served = { twin: 0, experience: 0 };

  await page.route(`**/api/v1/journeys/${journeyId}/twin`, async (route) => {
    let body: {
      current_activity_id?: string | null;
      context?: { execution?: Record<string, unknown> };
    };
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return; // the page abandoned this request by navigating away
    }
    body.current_activity_id = ghost;
    if (body.context?.execution) {
      body.context.execution = {
        ...body.context.execution,
        persisted_cursor: ghost,
        now_activity_id: null,
        next_activity_id: null,
        cursor_status: "STALE",
        now_state: "STALE_CURSOR",
      };
    }
    served.twin += 1;
    await route.fulfill({ json: body });
  });

  await page.route(`**/api/v1/journeys/${journeyId}/experience`, async (route) => {
    let body: { active_plan?: { current_activity_id?: string; cursor_status?: string } };
    try {
      body = await route.fetch().then((response) => response.json());
    } catch {
      return;
    }
    if (body.active_plan) {
      body.active_plan.current_activity_id = ghost;
      body.active_plan.cursor_status = "STALE";
    }
    served.experience += 1;
    await route.fulfill({ json: body });
  });

  const { card, heading } = await openLive(page, journeyId);
  await expect(card).toHaveAttribute("data-cursor-status", "STALE");
  await expect(card).toHaveText("Execution position unresolved");
  await expect(card).toHaveAttribute("data-now-activity-id", "");
  // The pre-fix render resolved to nothing and then named the first activity.
  for (const activity of activities.slice(0, 3)) {
    await expect(card).not.toContainText(activity.title);
  }
  await expect(heading).toHaveAttribute("data-cursor-status", "STALE");
  await expect(heading).toHaveText("Execution position unresolved");
  // A stale pointer used to read as "in progress": the field exists, so the panel
  // assumed the traveller was mid-step.
  const step = page.getByTestId("v10-current-step");
  await expect(step).toHaveAttribute("data-cursor-status", "STALE");
  await expect(step).toHaveText("Execution position unresolved");
  await expect(step).not.toHaveText("in progress");
});
