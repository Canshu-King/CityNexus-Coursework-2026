import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0037 — the map behaved like a pasted picture.
 *
 * `interactive: false` in MapPanel.tsx switched off drag, zoom, rotate and pitch in one
 * line, so every gesture the traveller made was discarded.  This gate does not assert on
 * that flag; it performs the gestures and reads the camera, which is the thing the user
 * actually experiences.  Restoring the flag therefore still turns `drag-moves-camera`
 * and `ctrl-wheel-zooms` red, and that is the kill evidence the order asked for.
 *
 * The route half exists for the second complaint ("一根大线"): more than one feature in
 * the source, a style that varies by mode, and a width that follows zoom.
 *
 * An earlier revision counted through `GeoJSONSource.getData()` and
 * `map.queryRenderedFeatures({layers})`.  Measured on this same screen both returned 0
 * while the caption — which prints `route.length` straight out of React — said 8, so
 * neither is a usable denominator here (getData hands back maplibre's internal tile
 * structure, not the FeatureCollection that was passed in; rendered-feature queries are
 * worker-tile and viewport dependent).  What is read instead is the contract the
 * component itself publishes (`data-route-legs` / `data-route-modes`) together with the
 * layer's declared paint properties, which are synchronous and not tile-scheduled.
 */

interface Camera {
  lng: number;
  lat: number;
  zoom: number;
}

async function readCamera(page: Page): Promise<Camera | null> {
  return page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
    if (!map) return null;
    const c = map.getCenter();
    return { lng: c.lng, lat: c.lat, zoom: map.getZoom() };
  });
}

interface RouteShape {
  legs: number;
  modes: string[];
  hasModeColourExpression: boolean;
  hasZoomExpression: boolean;
  casingLayer: boolean;
  captionText: string | null;
}

async function readRouteShape(page: Page): Promise<RouteShape | null> {
  return page.evaluate(() => {
    const panel = document.querySelector<HTMLElement>("[data-route-legs]");
    if (!panel) return null;
    const host = panel.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
    const width = map?.getPaintProperty?.("journey-provider-route", "line-width");
    const colour = map?.getPaintProperty?.("journey-provider-route", "line-color");
    return {
      legs: Number(panel.dataset.routeLegs ?? "0"),
      modes: (panel.dataset.routeModes ?? "").split(" ").filter(Boolean),
      hasModeColourExpression: Array.isArray(colour) && JSON.stringify(colour).includes('"get"'),
      hasZoomExpression: Array.isArray(width) && width[0] === "interpolate",
      casingLayer: !!map?.getLayer?.("journey-provider-route-casing"),
      captionText: panel.querySelector(".v8-map-caption")?.textContent?.trim() ?? null,
    };
  });
}

test.describe("ORD-0037 the journey map takes gestures and reads as a map", () => {
  test.beforeEach(async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 412, height: 915 });
    await ensureDemoJourney(page);
    /* /v8/hub, not /v8/live: the shipped demo plan carries no legs, so Live's map
       legitimately has no route geometry and would give the leg-count assertion a zero
       denominator.  Hub renders the same MapPanel against the selected option. */
    await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
    const canvas = page.locator(".maplibregl-canvas").first();
    await expect(canvas).toBeVisible({ timeout: 25_000 });
    await expect
      .poll(() => readCamera(page), { timeout: 25_000, message: "map instance never appeared" })
      .not.toBeNull();
    // Let fitBounds' 650ms animation settle before anything reads a baseline.
    await page.waitForTimeout(1200);
  });

  test("drag-moves-camera + ctrl-wheel-zooms — gestures reach the camera", async ({
    page,
  }) => {
    test.setTimeout(180_000);

    const before = (await readCamera(page))!;
    const box = (await page.locator(".maplibregl-canvas").first().boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;

    await page.mouse.move(cx, cy);
    await page.mouse.down();
    for (let i = 1; i <= 6; i += 1) {
      await page.mouse.move(cx - i * 18, cy - i * 12);
    }
    await page.mouse.up();
    await page.waitForTimeout(700);
    const afterDrag = (await readCamera(page))!;

    // Cooperative gestures make a plain wheel scroll the page, so zoom is Ctrl+wheel.
    await page.keyboard.down("Control");
    await page.mouse.move(cx, cy);
    await page.mouse.wheel(0, -400);
    await page.keyboard.up("Control");
    await page.waitForTimeout(700);
    const afterZoom = (await readCamera(page))!;

    const evidence = {
      generatedBy: "e345b9d6",
      order: "ORD-0037",
      nodeIds: ["drag-moves-camera", "ctrl-wheel-zooms"],
      before,
      afterDrag,
      afterZoom,
      centreMoved: afterDrag.lng !== before.lng || afterDrag.lat !== before.lat,
      zoomChanged: afterZoom.zoom !== afterDrag.zoom,
    };
    writeFileSync(
      evidencePath("ui-overnight/map-camera.json"),
      JSON.stringify(evidence, null, 2) + "\n",
      "utf-8",
    );

    expect(
      evidence.centreMoved,
      "e2e/qoder-map-interaction.spec.ts :: drag-moves-camera — " +
        `centre unchanged after a real pointer drag (${before.lng},${before.lat} -> ` +
        `${afterDrag.lng},${afterDrag.lat}); gestures are switched off`,
    ).toBe(true);
    expect(
      evidence.zoomChanged,
      "e2e/qoder-map-interaction.spec.ts :: ctrl-wheel-zooms — " +
        `zoom unchanged after Ctrl+wheel (${afterDrag.zoom} -> ${afterZoom.zoom})`,
    ).toBe(true);
  });

  test("route-feature-count + route-width-follows-zoom + route-casing-layer — not one big line", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    const route = await readRouteShape(page);
    const evidence = {
      generatedBy: "e345b9d6",
      order: "ORD-0037",
      nodeIds: [
        "route-feature-count",
        "route-mode-colour-expression",
        "route-width-follows-zoom",
        "route-casing-layer",
      ],
      route,
    };
    writeFileSync(
      evidencePath("ui-overnight/map-route.json"),
      JSON.stringify(evidence, null, 2) + "\n",
      "utf-8",
    );

    expect(route, "no route panel to measure").not.toBeNull();
    expect(
      route!.legs,
      `e2e/qoder-map-interaction.spec.ts :: route-feature-count — the route source carries ` +
        `${route!.legs} feature(s) (caption: ${JSON.stringify(route!.captionText)}); one line ` +
        `for the whole journey is the "一根大线" report`,
    ).toBeGreaterThan(1);
    expect(
      route!.modes.length,
      `e2e/qoder-map-interaction.spec.ts :: route-mode-colour-expression — the features carry ` +
        `no mode grouping keys (modes: ${route!.modes.join(",") || "none"}), so the layer ` +
        `cannot colour one leg differently from another`,
    ).toBeGreaterThan(1);
    expect(
      route!.hasModeColourExpression,
      "e2e/qoder-map-interaction.spec.ts :: route-mode-colour-expression — line-color is a " +
        "constant, so every leg paints the same colour",
    ).toBe(true);
    expect(
      route!.hasZoomExpression,
      "e2e/qoder-map-interaction.spec.ts :: route-width-follows-zoom — line-width is a " +
        "constant, so the line cannot stay legible across zoom levels",
    ).toBe(true);
    expect(
      route!.casingLayer,
      "e2e/qoder-map-interaction.spec.ts :: route-casing-layer — no casing under the route",
    ).toBe(true);
  });

  test("the gesture split is stated on the panel, not left to be discovered", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await expect(page.getByTestId("v8-map-gesture-hint")).toBeVisible({ timeout: 25_000 });
  });
});
