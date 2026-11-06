import { expect, test, type Page } from "@playwright/test";
import { testIds } from "../../src/contracts/testIds";

/* ------------------------------------------------------------------ *
 * ARCHIVED legacy-surface tests — non-gating.
 *
 * These four cases used to sit in the current-product acceptance suite while
 * driving a surface that is no longer the product: `src/main.tsx` renders
 * `V8Experience` for every route and only falls back to `LegacyApp` under
 * `/legacy`, deliberately, so that a first-time traveller never lands there.
 * They failed on `page.goto("/")` for that reason alone, and pass-11 proved
 * the failure was not shared state.
 *
 * The arbiter's ruling (pass-14) is that archiving must be an actual removal
 * from the acceptance suite, not a rename: the authoritative config
 * (`playwright.config.ts`) ignores `e2e/legacy/**` entirely, and these run
 * only when `playwright.legacy.config.ts` is invoked explicitly.  Their result
 * cannot sign a gate.  Nothing here is skipped, and no assertion was weakened
 * to make the move look clean — where a case still fails, the reason is
 * recorded in `docs/LEGACY_E2E_MIGRATION.md` next to the capability it guards.
 *
 * Entry point changed from `/` to `/legacy`, which is the whole point of the
 * move: the surface exists, and this is where it lives.
 * ------------------------------------------------------------------ */

async function openLegacyJourney(page: Page) {
  await page.goto("/legacy");
  await expect(page.getByTestId(testIds.journeyStatus)).toHaveAttribute("data-source", "backend");
  await expect(page.getByTestId(testIds.currentActivity)).toContainText("Tech Art Center");
}

test("TravelerTwin Control Tower renders persisted evidence and authority boundary", async ({ page }) => {
  await openLegacyJourney(page);

  const panel = page.getByTestId(testIds.travelerTwinPanel);
  await expect(panel).toContainText("Inspectable cross-Journey learning");
  await expect(panel).toContainText("Explicit preferences");
  await expect(panel).toContainText("Conflicts and resolution");
  await expect(panel).toContainText("soft planning input only");
  await expect(page.getByTestId(testIds.travelerTwinClear)).toBeDisabled();
});

test("Control Tower compiles and reloads a persisted natural-language mission", async ({ page }) => {
  await openLegacyJourney(page);

  const commandResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/agent/commands") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  await page.getByTestId("agent-command").fill(
    "下午海河沿线有交通管制，我不想淋雨，游船和晚餐别动，最多多花50元。你自己重新安排。",
  );
  await page.getByTestId("run-agent-command").click();
  const body = await (await commandResponse).json();
  expect(body.mission.objective_codes).toContain("PREFER_INDOOR");
  expect(body.mission.protected_activity_ids.length).toBeGreaterThan(0);
  expect(body.run.status).toMatch(/COMPLETED|SKIPPED_EXTERNAL_DEPENDENCY|FAILED_EXTERNAL/);
  await expect(page.getByTestId("agent-mission-result")).toContainText(body.run.status);
  await expect(page.getByTestId("agent-mission-result")).toContainText(/Persisted tool trace/i);

  await page.reload();
  await expect(page.getByTestId("agent-mission-result")).toContainText(body.mission.id.slice(0, 8));
  await expect(page.getByTestId("agent-command-center")).toContainText(/Persisted tool trace/i);
});

test("Assisted acceptance is policy-driven and route/feed evidence survive reload", async ({ page }) => {
  await openLegacyJourney(page);

  const policyResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/agent-policy") &&
      response.request().method() === "PUT" &&
      response.status() === 200,
  );
  await page.getByTestId("agent-mode").selectOption("ASSISTED");
  await policyResponse;

  await page.getByTestId(testIds.eventSimulator).click();
  const eventResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/events") &&
      response.request().method() === "POST" &&
      response.status() === 201,
  );
  const agentRequestResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/agent/tools/request_replan") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  const routeEvidenceResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/route-evidence") &&
      response.request().method() === "GET" &&
      response.status() === 200,
  );
  await page.getByTestId(testIds.eventClosureButton).click();
  const event = await (await eventResponse).json();
  const requestReceipt = await (await agentRequestResponse).json();
  const routeEvidence = await (await routeEvidenceResponse).json();
  expect(event.event_type).toBe("CLOSURE");
  expect(requestReceipt.decision).toBe("ALLOW_AUTO");
  expect(routeEvidence.source).toBe("frozen_mobility_snapshot");
  expect(routeEvidence.old_route.geometry.coordinates.length).toBeGreaterThan(2);
  expect(routeEvidence.new_route.geometry.coordinates.length).toBeGreaterThan(2);
  expect(routeEvidence.old_route.properties.provider_backed).toBe(true);
  expect(routeEvidence.new_route.properties.provider_backed).toBe(true);

  await page.waitForFunction(() => {
    const host = document.querySelector(".maplibre-container") as
      | (HTMLDivElement & { __cityNexusMap?: any })
      | null;
    const map = host?.__cityNexusMap;
    return Boolean(map?.isStyleLoaded?.() && map.getSource("citynexus-old-route") && map.getSource("citynexus-new-route"));
  });
  const mapState = await page.locator(".maplibre-container").evaluate(async (element) => {
    const map = (element as HTMLDivElement & { __cityNexusMap?: any }).__cityNexusMap;
    const oldData = await map.getSource("citynexus-old-route").getData();
    const newData = await map.getSource("citynexus-new-route").getData();
    return {
      layers: map.getStyle().layers.map((layer: { id: string }) => layer.id),
      oldFeatures: oldData.features.length,
      newFeatures: newData.features.length,
      oldCoordinates: oldData.features[0].geometry.coordinates.length,
      newCoordinates: newData.features[0].geometry.coordinates.length,
      source: element.getAttribute("data-route-source"),
      provider: element.getAttribute("data-route-provider"),
    };
  });
  expect(mapState.layers).toEqual(expect.arrayContaining(["citynexus-old-route", "citynexus-new-route", "citynexus-marker-points"]));
  expect(mapState.oldFeatures).toBe(1);
  expect(mapState.newFeatures).toBe(1);
  expect(mapState.oldCoordinates).toBe(routeEvidence.old_route.geometry.coordinates.length);
  expect(mapState.newCoordinates).toBe(routeEvidence.new_route.geometry.coordinates.length);
  expect(mapState.source).toBe("frozen_mobility_snapshot");
  expect(mapState.provider).toBe("mock-mobility");

  const firstAccept = page.waitForResponse(
    (response) =>
      response.url().endsWith("/agent/tools/accept_replan") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  await page.getByTestId(testIds.acceptReplan).click();
  const firstReceipt = await (await firstAccept).json();
  expect(firstReceipt.decision).toBe("REQUIRE_CONFIRMATION");
  expect(firstReceipt.executed).toBe(false);
  await expect(page.getByTestId("agent-policy-decision")).toContainText("REQUIRE_CONFIRMATION");
  await expect(page.getByTestId(testIds.acceptReplan)).toContainText("Confirm");

  const confirmedAccept = page.waitForResponse(
    (response) =>
      response.url().endsWith("/agent/tools/accept_replan") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  const approvalReceipt = page.waitForResponse(
    (response) =>
      response.url().includes("/agent/approvals/") &&
      response.request().method() === "POST" &&
      response.status() === 200,
  );
  await page.getByTestId(testIds.acceptReplan).click();
  const approvedReceipt = await (await approvalReceipt).json();
  expect(approvedReceipt.status).toBe("APPROVED");
  const confirmedReceipt = await (await confirmedAccept).json();
  expect(confirmedReceipt.decision).toBe("REQUIRE_CONFIRMATION");
  expect(confirmedReceipt.executed).toBe(true);
  expect(confirmedReceipt.user_confirmed).toBe(false);
  expect(confirmedReceipt.reason_codes).toContain("USER_APPROVAL_RECEIPT_CONSUMED");
  await expect(page.getByTestId(testIds.protectedCommitment)).toContainText("River Cruise");

  const feedItems = await page.getByTestId("agent-feed").locator("li").evaluateAll((items) =>
    items.map((item) => ({
      type: item.getAttribute("data-event-type"),
      actor: item.getAttribute("data-actor-type"),
      tool: item.getAttribute("data-tool"),
    })),
  );
  expect(feedItems.some((item) => item.type === "agent_tool_requested" && item.actor === "AI_AGENT")).toBe(true);
  expect(feedItems.some((item) => item.type === "replan_confirmation_required")).toBe(true);

  await page.reload();
  await expect(page.getByTestId("agent-feed")).toContainText(/agent tool requested/i);
  await expect(page.getByTestId("agent-policy-decision")).toContainText("REQUIRE_CONFIRMATION");
  await expect(page.locator(".maplibre-container")).toHaveAttribute(
    "data-route-source",
    "frozen_mobility_snapshot",
  );
});

test("Proactive Guardian launches the flagship recovery without a new chat prompt", async ({ page }) => {
  await openLegacyJourney(page);

  for (const [selector, value] of [
    ["#agent-cost-limit", "5000"],
    ["#agent-drift-limit", "120"],
  ] as const) {
    const response = page.waitForResponse(
      (item) => item.url().includes("/agent-policy") && item.request().method() === "PUT" && item.status() === 200,
    );
    await page.locator(selector).fill(value);
    await response;
  }
  let response = page.waitForResponse(
    (item) => item.url().includes("/agent-policy") && item.request().method() === "PUT" && item.status() === 200,
  );
  await page.getByTestId("agent-mode").selectOption("AUTONOMOUS");
  await response;
  response = page.waitForResponse(
    (item) => item.url().includes("/agent-policy") && item.request().method() === "PUT" && item.status() === 200,
  );
  await page.getByTestId("agent-auto-accept").check();
  await response;

  await page.getByTestId("agent-command").fill(
    "游船和晚餐别动，优先室内、少走路，最多多花五十元，允许自动调整。",
  );
  const missionResponse = page.waitForResponse(
    (item) => item.url().endsWith("/guardian/mission") && item.request().method() === "PUT" && item.status() === 200,
  );
  await page.getByTestId("activate-guardian-mission").click();
  const standingMission = await (await missionResponse).json();
  expect(standingMission.max_cost_delta_minor).toBe(5000);
  expect(standingMission.protected_activity_ids.length).toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId("guardian-standing-mission")).toContainText("ACTIVE");

  const scanResponse = page.waitForResponse(
    (item) => item.url().endsWith("/guardian/scan") && item.request().method() === "POST" && item.status() === 200,
  );
  await page.getByTestId("guardian-flagship-scan").click();
  const run = await (await scanResponse).json();
  expect(run.decision).toBe("RECOVERY_REQUIRED");
  expect(run.action_status).toBe("EXECUTED");
  expect(run.evidence.observation.actionable_event_ids).toHaveLength(2);
  expect(run.evidence.solver.kind).toBe("ORTOOLS_CP_SAT_V2");
  expect(run.tool_steps.map((step: { tool_name: string }) => step.tool_name)).toEqual([
    "inspect_twin",
    "request_replan",
    "accept_replan",
  ]);
  expect(run.active_plan_after_id).not.toBe(run.active_plan_before_id);
  await expect(page.getByTestId("guardian-decision")).toContainText("RECOVERY_REQUIRED");
  await expect(page.getByTestId("guardian-provenance")).toContainText("ORTOOLS_CP_SAT_V2");
  await expect(page.getByTestId(testIds.protectedCommitment)).toContainText("River Cruise");
  await expect(page.getByTestId(testIds.journeyTimeline)).toContainText("Dinner");
  await expect(page.getByTestId(testIds.mapOldRoute)).toBeVisible();
  await expect(page.getByTestId(testIds.mapNewRoute)).toBeVisible();

  await page.reload();
  await expect(page.getByTestId("guardian-decision")).toContainText("RECOVERY_REQUIRED");
  await expect(page.getByTestId("guardian-trace")).toContainText("accept_replan");
  await expect(page.getByTestId(testIds.protectedCommitment)).toContainText("River Cruise");
});
