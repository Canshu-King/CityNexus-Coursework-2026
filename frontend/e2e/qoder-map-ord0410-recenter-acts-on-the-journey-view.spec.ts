import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0410 — 「回到行程视野」 has to be an action, not a label.
 *
 * The defect (commander, 2026-09-28 22:31, and re-measured on this lane's own stack against the
 * bundle built from `214ab0ff`): on a journey that has located stops but no leg geometry the press
 * did nothing at all — markers byte-identical to post-drag, `data-viewport-state` still
 * `off-target`, two presses, no exception.  Cause, measured rather than assumed: the panel framed
 * the journey only `if (coordinates.length > 1)`, where `coordinates` is *route* geometry, so for
 * that shape `refitRef.current` was never assigned and the control's `refitRef.current?.()`
 * silently called nothing.  The same reading rules the commander's "stale map instance" guess out:
 * the instance behind the canvas seam never changed identity, was never removed, received zero
 * `fitBounds` calls per press, and moved fine when `fitBounds` was called on it by hand.
 *
 * What this file grades, in one press, on the shape that carried the defect:
 *   cell "moved"  — every marker returns to the position it painted at rest (<=2 px), and the press
 *                   demonstrably moved them off their post-drag positions.
 *   cell "cleared" — `data-viewport-state` is back to `on-target`, and the accessible name changed
 *                   back with it.
 * Both are asserted in the same run after exactly one press: moving the markers without clearing
 * the state fails, and clearing the state without bringing the markers back fails.
 *
 * The arm states its own precondition (`routeLegs === 0`, at least two located stops) instead of
 * trusting the seed to keep producing the broken shape — a gate that quietly starts measuring a
 * journey that has a route is how this defect stayed out of the accepted suite.
 */

const FILE = "e2e/qoder-map-ord0410-recenter-acts-on-the-journey-view.spec.ts";

type Marker = { n: string; cx: number; cy: number };
type Control = { state: string | null; aria: string; legs: string | null };

const readMarkers = (page: Page) =>
  page.evaluate<Marker[], void>(() =>
    Array.from(
      document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"),
    ).map((el) => {
      const b = el.getBoundingClientRect();
      return {
        n: (el.textContent ?? "").trim(),
        cx: Math.round(b.x + b.width / 2),
        cy: Math.round(b.y + b.height / 2),
      };
    }),
  );

const readControl = (page: Page) =>
  page.evaluate<Control, void>(() => {
    const el = document.querySelector<HTMLElement>("[data-testid='map-viewport-reset']");
    const panel = document.querySelector<HTMLElement>(".v8-map-panel");
    return {
      state: el ? el.getAttribute("data-viewport-state") : null,
      aria: el ? el.getAttribute("aria-label") ?? "" : "",
      legs: panel ? panel.getAttribute("data-route-legs") : null,
    };
  });

/** Where the markers' own panel sits, so "back in frame" is a position and not an opinion. */
const readPanelBox = (page: Page) =>
  page.evaluate(() => {
    const el = document.querySelector<HTMLElement>(".v8-map-panel");
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  });

const maxMarkerDelta = (a: Marker[], b: Marker[]) => {
  const byN = new Map(b.map((m) => [m.n, m]));
  let worst = 0;
  for (const m of a) {
    const other = byN.get(m.n);
    if (!other) return Number.POSITIVE_INFINITY;
    worst = Math.max(worst, Math.abs(m.cx - other.cx), Math.abs(m.cy - other.cy));
  }
  return worst;
};

async function dragGlass(page: Page, dx: number, dy: number): Promise<void> {
  const box = await page.locator(".v8-maplibre-canvas").boundingBox();
  expect(box, "the map canvas must be laid out before it can be dragged").not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(1_500);
}

/**
 * Enter the map the way the traveller in the receipt did: the setup screen's own
 * 「使用封存的演示行程」 door, then the map route.  The stored journey pointer is cleared first
 * because the suite's seed project leaves a journey with leg geometry in `localStorage`, and that
 * shape is exactly the one that never carried the defect.
 */
async function enterMapViaSealedDemo(page: Page): Promise<void> {
  await page.goto("/v8", { waitUntil: "load" });
  await page.evaluate(() => localStorage.removeItem("citynexus.v8.journey_id"));
  await page.goto("/v8", { waitUntil: "load" });
  const setup = page.getByTestId("trial-setup");
  await expect(setup, "clearing the journey pointer must bring the product's own setup screen up")
    .toBeVisible({ timeout: 25_000 });
  await page.getByTestId("trial-tianjin-demo").click();
  await page.waitForFunction(
    () => !!localStorage.getItem("citynexus.v8.journey_id"),
    null,
    { timeout: 30_000 },
  );
  await page.goto("/v8/map", { waitUntil: "load" });
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(3_000);
}

/** Land on the map with whatever journey the suite's seed established (leg geometry included). */
async function enterMapOnSeededJourney(page: Page): Promise<void> {
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-heading").first()).toBeVisible({ timeout: 25_000 });
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(3_000);
}

const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

/** The two cells, asserted together after exactly one press. */
async function assertOnePressFramesTheJourney(
  page: Page,
  arm: string,
  atRest: Marker[],
  controlAtRest: Control,
  shotPrefix: string,
): Promise<void> {
  await dragGlass(page, -130, -90);
  const afterDrag = await readMarkers(page);
  const controlAfterDrag = await readControl(page);
  await page.screenshot({ path: evidencePath(shotPrefix, "after_drag.png") });

  expect(
    controlAfterDrag.state,
    `${arm}: precondition — a real drag must take the view off target, got ${controlAfterDrag.state}`,
  ).toBe("off-target");
  const dragMoved = maxMarkerDelta(atRest, afterDrag);
  expect(dragMoved, `${arm}: precondition — the drag must move markers, worst delta ${dragMoved} px`)
    .toBeGreaterThan(24);

  await page.getByTestId("map-viewport-reset").click();
  await page.waitForTimeout(2_000);
  const afterPress = await readMarkers(page);
  const controlAfterPress = await readControl(page);
  const panel = await readPanelBox(page);
  await page.screenshot({ path: evidencePath(shotPrefix, "after_press.png") });

  const backToRest = maxMarkerDelta(atRest, afterPress);
  const movedByPress = maxMarkerDelta(afterDrag, afterPress);
  const allInside =
    !!panel &&
    afterPress.every((m) => m.cx >= panel.x && m.cx <= panel.x + panel.w && m.cy >= panel.y && m.cy <= panel.y + panel.h);
  const state = panel ? controlAfterPress.state : null;

  push(`${arm}-one-press`, {
    routeLegs: controlAtRest.legs,
    markers: atRest.length,
    stateAtRest: controlAtRest.state,
    ariaAtRest: controlAtRest.aria,
    stateAfterDrag: controlAfterDrag.state,
    ariaAfterDrag: controlAfterDrag.aria,
    dragWorstDeltaPx: dragMoved,
    stateAfterPress: state,
    ariaAfterPress: controlAfterPress.aria,
    pressWorstDeltaFromDragPx: movedByPress,
    pressWorstDeltaFromRestPx: backToRest,
    allMarkersInsidePanelAfterPress: allInside,
    cellMoved: movedByPress > 2 && backToRest <= 2 && allInside === true,
    cellCleared: state === "on-target" && controlAfterPress.aria !== controlAfterDrag.aria,
  });

  // cell "moved": the press has to have acted, and it has to have landed on the journey's own frame.
  expect(
    movedByPress,
    `${arm}: the press must move the markers off their post-drag positions, worst delta ${movedByPress} px`,
  ).toBeGreaterThan(2);
  expect(
    backToRest,
    `${arm}: one press must bring every marker back to where the journey framed it, worst delta ${backToRest} px`,
  ).toBeLessThanOrEqual(2);
  expect(allInside, `${arm}: after one press every stop must be inside the panel again`).toBe(true);
  // cell "cleared": the control's own two-state reading has to come back with the camera.
  expect(controlAfterPress.state, `${arm}: one press must clear off-target`).toBe("on-target");
  expect(
    controlAfterPress.aria,
    `${arm}: the accessible name must change back with the state`,
  ).not.toBe(controlAfterDrag.aria);
}

test("sealed-demo-shape — no leg geometry, and the press still frames the journey", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMapViaSealedDemo(page);
  const control = await readControl(page);
  const markers = await readMarkers(page);
  // The precondition is the defect's condition. Name it, so this arm cannot silently start
  // measuring a journey that has a route and therefore never carried the bug.
  expect(
    control.legs,
    "this arm measures the route-less shape; a non-zero leg count means it is measuring something else",
  ).toBe("0");
  expect(markers.length, "the shape needs located stops to frame").toBeGreaterThanOrEqual(2);
  expect(control.state, "the view starts on target").toBe("on-target");
  await page.screenshot({ path: evidencePath("ord0410-recenter-demo", "at_rest.png") });
  await assertOnePressFramesTheJourney(page, "sealed-demo-shape", markers, control, "ord0410-recenter-demo");
});

test("seeded-shape — the press works on a journey that does have route geometry", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMapOnSeededJourney(page);
  const control = await readControl(page);
  const markers = await readMarkers(page);
  expect(markers.length, "the seeded journey must put stops on the map").toBeGreaterThanOrEqual(2);
  expect(control.state, "the view starts on target").toBe("on-target");
  await page.screenshot({ path: evidencePath("ord0410-recenter-seeded", "at_rest.png") });
  await assertOnePressFramesTheJourney(page, "seeded-shape", markers, control, "ord0410-recenter-seeded");
});

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0410-recenter", "readings.json"),
    JSON.stringify({ file: FILE, rows }, null, 1),
  );
});
