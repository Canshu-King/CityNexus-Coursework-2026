import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import { MAP_LABEL_FORBIDDEN } from "./_mapLabelRules";

/**
 * ORD-0403 — one named map control, and the labels that may be painted on the glass.
 *
 * What is measured, and why each reading is a value rather than a presence check:
 *   control-state  — the button's `data-viewport-state` must follow the camera: on-target at rest,
 *                    off-target after a real drag, on-target again after pressing it. A control
 *                    whose label is set at click time and never re-derived would pass a presence
 *                    check and lie to the traveller.
 *   control-reset  — pressing it must actually reframe: the camera centre is read before and after.
 *   marker-to-card — the sentence carried over from ORD-0390's acceptance: tapping a marker on the
 *                    map has to change the card's selected state, and it is read out of the card's
 *                    painted text.
 *   labels         — every string painted inside the canvas is tested against
 *                    `MAP_LABEL_FORBIDDEN` from `e2e/_mapLabelRules.ts` (ORD-0415 moved the rule out
 *                    of `src/lib/`), so the gate and the labels check read the same list rather than
 *                    the gate keeping its own copy.
 *   reach-<width>  — the control is found by `elementFromPoint` at its own centre at 320/360/390/414
 *                    and must not overlap the tile notice or the gesture bubble. No arm in this file
 *                    reads `scrolled>0`.
 *
 * The `interactive:false` question is answered by running this file twice — once on the shipped
 * build, once on a build where that flag is switched back off — and the control-state and
 * marker-to-card arms are what separate the two.
 */

const FILE = "e2e/qoder-map-ord0403-named-control-and-labels.spec.ts";
const WIDTHS = [320, 360, 390, 414];

async function enterMap(page: Page): Promise<void> {
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

const readControl = (page: Page) =>
  page.evaluate(() => {
    const el = document.querySelector<HTMLElement>("[data-testid='map-viewport-reset']");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return {
      state: el.getAttribute("data-viewport-state"),
      label: (el.textContent ?? "").trim(),
      aria: el.getAttribute("aria-label") ?? "",
      box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      hitSelf: !!top && (top === el || el.contains(top) || (top.closest?.("[data-testid='map-viewport-reset']") as HTMLElement | null) !== null),
    };
  });

const readCamera = (page: Page) =>
  page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
    if (!map) return null;
    const c = map.getCenter();
    return { lng: Number(c.lng.toFixed(5)), lat: Number(c.lat.toFixed(5)), zoom: Number(map.getZoom().toFixed(3)) };
  });

async function dragCanvas(page: Page, dx: number, dy: number): Promise<void> {
  const box = await page.locator(".v8-maplibre-canvas").boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(900);
}

const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

test("control-state — the button follows the camera, and pressing it reframes", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  /* one artefact per moment, and the artefact is the control itself: a full-page shot at rest was
     byte-identical to the reach@390 arm's, which lets one arm borrow another's witness. These three
     also carry the glyph, so the state is visible in the evidence and not only in the attribute. */
  const shots: string[] = [];
  const shootControl = async (tag: string) => {
    const f = evidencePath("ord0403-control", `control_${tag}.png`);
    await page.getByTestId("map-viewport-reset").screenshot({ path: f });
    shots.push(f);
  };
  const atRest = await readControl(page);
  const camAtRest = await readCamera(page);
  await shootControl("rest");
  expect(atRest, "the named control must be on the map screen").not.toBeNull();
  expect(atRest!.state, `control should start on-target, got ${atRest!.state}`).toBe("on-target");

  await dragCanvas(page, -80, -50);
  const afterDrag = await readControl(page);
  const camAfterDrag = await readCamera(page);
  await shootControl("after_drag");

  await page.getByTestId("map-viewport-reset").click();
  await page.waitForTimeout(1_200);
  const afterReset = await readControl(page);
  const camAfterReset = await readCamera(page);
  await shootControl("after_reset");
  push("control-state", {
    atRest: atRest!.state, afterDrag: afterDrag?.state, afterReset: afterReset?.state,
    labels: [atRest!.label, afterDrag?.label, afterReset?.label],
    ariaLabels: [atRest!.aria, afterDrag?.aria, afterReset?.aria],
    ariaChangesWithState: atRest!.aria !== afterDrag?.aria,
    camAtRest, camAfterDrag, camAfterReset,
    dragMovedCamera: camAtRest!.lng !== camAfterDrag!.lng || camAtRest!.lat !== camAfterDrag!.lat,
    resetReturned: camAfterReset!.lng === camAtRest!.lng && camAfterReset!.lat === camAtRest!.lat,
    shots,
  });
  /* This is the arm that answers "两态不得靠 interactive:false 蒙过门禁" with a measurement:
     if the map is not interactive the drag cannot move the camera, so `afterDrag` stays on-target
     and this fails — it is not satisfiable by deleting or hiding the control. */
  expect(
    afterDrag!.aria,
    `the accessible name must state the new condition, not only the action: was ${atRest!.aria}`,
  ).not.toBe(atRest!.aria);
  expect(afterDrag?.state, "a real drag must take the view off target").toBe("off-target");
  expect(afterReset?.state, "pressing the control must bring the view back on target").toBe("on-target");
  expect(camAfterReset!.lng, "the reset must restore the framed centre").toBe(camAtRest!.lng);
  expect(camAfterReset!.lat, "the reset must restore the framed centre").toBe(camAtRest!.lat);
});

test("marker-to-card — tapping a marker changes the card's selection", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const card = page.getByTestId("map-selected-stop");
  const before = (await card.innerText()).trim();
  const markers = await page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"))
      .filter((el) => el.getAttribute("role") === "button")
      .map((el) => ({ n: (el.textContent ?? "").trim(), aria: el.getAttribute("aria-label") ?? "" })),
  );
  expect(markers.length, "the seeded journey must put pickable stops on the map").toBeGreaterThan(1);
  const target = markers.find((m) => m.aria && !before.includes(m.aria)) ?? markers[markers.length - 1];
  await page.locator(".v8-map-panel .v9-map-marker").filter({ hasText: new RegExp(`^${target.n}$`) }).click();
  await page.waitForTimeout(1_000);
  const after = (await card.innerText()).trim();
  const pressed = await page.evaluate((n: string) => {
    const el = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"))
      .find((m) => (m.textContent ?? "").trim() === n);
    return el ? el.getAttribute("aria-pressed") : null;
  }, target.n);
  const shot = evidencePath("ord0403-control", "marker_to_card.png");
  await page.screenshot({ path: shot });
  push("marker-to-card", {
    before, after, changed: before !== after, marker: target.n, aria: target.aria,
    cardNamesStop: after.includes(target.aria), markerPressed: pressed, shot,
  });
  expect(before).not.toBe(after);
  expect(after).toContain(target.aria);
  expect(pressed, "the tapped marker must read as pressed").toBe("true");
});

test("labels — nothing off-whitelist is painted on the glass", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const painted = await page.evaluate(() => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const markers = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
    return {
      markerText: markers.map((m) => (m.textContent ?? "").trim()),
      markerAria: markers.map((m) => m.getAttribute("aria-label") ?? ""),
      canvasText: canvas?.innerText ?? "",
    };
  });
  const onlyDigitsPainted = painted.markerText.every((label) => /^\d+$/.test(label));
  const corpus = [painted.canvasText, ...painted.markerText, ...painted.markerAria].join("\n");
  const hits = MAP_LABEL_FORBIDDEN
    .map((rule) => ({ rule: rule.name, source: rule.source, found: rule.pattern.test(corpus) }))
    .filter((x) => x.found);
  push("labels", {
    forbiddenListLength: MAP_LABEL_FORBIDDEN.length,
    markerCount: painted.markerText.length,
    markerTextSample: painted.markerText.slice(0, 12),
    onlyDigitsPainted,
    hits,
  });
  expect(hits, `off-whitelist text painted on the map: ${JSON.stringify(hits)}`).toEqual([]);
  expect(onlyDigitsPainted, "only the stop number is painted on a marker").toBe(true);
});

for (const width of WIDTHS) {
  test(`reach — the control is hittable and does not collide @${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await enterMap(page);
    const c = await readControl(page);
    expect(c, "the named control must exist").not.toBeNull();
    const overlap = await page.evaluate(() => {
      const el = document.querySelector<HTMLElement>("[data-testid='map-viewport-reset']");
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const area = (sel: string) => {
        const o = document.querySelector<HTMLElement>(sel);
        if (!o) return 0;
        const b = o.getBoundingClientRect();
        const w = Math.min(r.right, b.right) - Math.max(r.left, b.left);
        const h = Math.min(r.bottom, b.bottom) - Math.max(r.top, b.top);
        return w > 0 && h > 0 ? Math.round(w * h) : 0;
      };
      return {
        withHint: area("[data-testid='v8-map-gesture-hint']"),
        withNotice: area(".v8-map-tile-notice"),
        withStack: area(".v8-map-overlay-stack"),
      };
    });
    const scroll = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth, win: window.innerWidth,
    }));
    const shot = evidencePath("ord0403-control", `reach_${width}.png`);
    await page.screenshot({ path: shot });
    push(`reach-${width}`, { ...c, hitSelf: c!.hitSelf, overlap, ...scroll, shot });
    expect(c!.hitSelf, `control centre must hit the control itself: ${JSON.stringify(c!.box)}`).toBe(true);
    expect(c!.box.w * c!.box.h, "control keeps a real hit box").toBeGreaterThanOrEqual(32 * 32);
    expect(overlap!.withHint, "must not sit under the gesture bubble").toBe(0);
    expect(overlap!.withNotice, "must not sit under the tile notice").toBe(0);
    expect(overlap!.withStack, "must not sit under the overlay stack").toBe(0);
    expect(scroll.doc, "no horizontal page scroll").toBeLessThanOrEqual(scroll.win + 1);
  });
}

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0403-control", "readings.json"),
    JSON.stringify({ file: FILE, widths: WIDTHS, rows }, null, 1),
  );
});
