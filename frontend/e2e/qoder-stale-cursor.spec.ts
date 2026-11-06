import { expect, test } from "@playwright/test";

/* The Live surface must not invent an execution position.

   CX-SOL-001 is a backend contract, but the failure the traveller sees is a
   frontend one: Live used to render `activities[0]` as "now" and
   `activities[1]` as "next" regardless of the persisted cursor, so a cursor that
   named a step the active plan no longer contains still produced a confident,
   wrong "You are at the museum".

   The stale state is not reachable through any public endpoint — writing the
   cursor is the product's own business — so this case intercepts the projection
   and hands the surface the state it must survive. That is a UI guarantee about
   what the surface does with a contradiction, not a fake of product behaviour. */

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

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
    throw new Error(`${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 180)}`);
  }
  return (await response.json()) as T;
}

test("a cursor the plan cannot resolve is reported as unresolved, not as a plausible step", async ({
  page,
  request,
}) => {
  const demo = await call<{ journey_id: string }>(request, "/api/v1/demo/reset", "POST");
  const journeyId = demo.journey_id;
  const plans = await call<{ id: string; activities: { id: string; place_id: string }[] }[]>(
    request,
    `/api/v1/journeys/${journeyId}/plans/generate`,
    "POST",
    { candidate_count: 1 },
  );
  await call(request, `/api/v1/journeys/${journeyId}/plans/${plans[0].id}/select`, "POST");
  await call(request, `/api/v1/journeys/${journeyId}/start`, "POST");

  // A well-formed id that this plan does not contain: the shape the guard exists
  // for, and the one no endpoint can produce on its own.
  const ghost = "00000000-0000-4000-8000-000000000000";
  const titles = plans[0].activities.map((item) => item.id);

  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.locale", "en");
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);

  // One handler for the whole case: the control pass changes what it serves
  // rather than tearing the route down mid-navigation, which raced and aborted.
  let mode: "stale" | "real" = "stale";
  let served = 0;
  await page.route(`**/api/v1/journeys/${journeyId}/experience`, async (route) => {
    let body: any;
    try {
      body = await route.fetch().then((response) => response.json());
      if (mode === "stale") {
        body.active_plan.current_activity_id = ghost;
        body.active_plan.cursor_status = "STALE";
      }
      served += 1;
      await route.fulfill({ json: body });
    } catch {
      // The page abandoned this request by navigating away; nothing to assert on.
      return;
    }
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/live");

  const current = page.getByTestId("live-journey-current");
  await expect(current).toBeVisible();
  await expect(current).toHaveAttribute("data-cursor-status", "STALE");
  await expect(current).toHaveText("Execution position unresolved");
  await expect(current).not.toHaveText("Awaiting plan");

  // The claim this case exists to kill: no plan step may be presented as "now".
  for (const activity of plans[0].activities) {
    await expect(current).not.toHaveText(activity.id);
  }
  const nextLine = page.locator(".v8-live-now p").first();
  await expect(nextLine).toContainText("no current or next step");

  // And the honest control: the same surface, same code path, real cursor.
  mode = "real";
  await page.reload();
  await expect(page.getByTestId("live-journey-current")).toHaveAttribute(
    "data-cursor-status",
    "VALID",
  );
  await expect(page.getByTestId("live-journey-current")).not.toHaveText(
    "Execution position unresolved",
  );
  expect(titles.length).toBeGreaterThan(1);
  // The catch above is only for requests the page abandoned.  If the handler
  // never served both passes, the "stale" render was not produced by this test.
  expect(served, `the projection was intercepted ${served} time(s), expected at least 2`).toBeGreaterThanOrEqual(2);
});
