import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import { mapLabelViolations } from "./_mapLabelRules";

/**
 * ORD-0512 — the map label rule moves from the ruler to the map.
 *
 * `e2e/_mapLabelRules.ts` has said since ORD-0403 which classes of string may never be painted on the
 * glass, and `qoder-ord0415-label-string-origin.spec.ts` measured the consequence nobody had faced: no
 * word on the product path was painted at all, so the whitelist had nothing to whitelist and
 * `qoder-map-ord0403-named-control-and-labels.spec.ts` could only assert that a pin "paints only its
 * stop number". What the corpus this rule judges actually holds is measured here rather than quoted: on
 * the tip's own bytes `.v8-maplibre-canvas` yields 17 characters (none of them a stop's name), and on this
 * candidate 52, of which the three names then on the glass account for 32. The traveller's complaint is
 * the plain one: numbered dots and no way to tell which is which without tapping each.
 *
 * What this file measures, one arm per question:
 *   names-painted-<shape>   — every located stop has a name on the glass, and the name is the word the
 *                             server sent (`active_plan.activities[].title` / `place_name`, read out of
 *                             the payloads this screen received, not out of the DOM I am judging).
 *                             Ink is proven by pixels, not by attributes: the label's own clip differs
 *                             from the identical clip taken with that one label hidden.
 *   crowding-hides          — at a zoom where the stops converge, no two shown names overlap, at least
 *                             one is hidden, the hidden nodes are still in the document (a clip, not a
 *                             deletion — the same rule `.filtered-out` follows), and the stop the
 *                             traveller picked keeps its name.
 *   attribution-never-covered — the compliance sentence loses no pixels to a name, at three shapes.
 *   painted-set-passes-the-rule-list — the strings now on the glass, fed to the shared forbidden list,
 *                             come back clean, and the corpus is not empty (an empty corpus is what
 *                             made the old arm vacuous).
 *   no-machine-word-on-glass — field names, capability codes, uuids and ordinals are not painted.
 *   labels-move-with-camera — the names belong to geography: a real drag moves every shown one.
 *   hidden-reasons-are-told — a name that is not on the glass says which kind of no it is, and the
 *                             per-side attributes are cross-checked against `data-placement` rather
 *                             than merely read out.
 *   third-side-is-tried     — with both flanks of a pin taken away on the live screen, the word comes
 *                             back below the pin instead of disappearing; placement order is right,
 *                             left, under.
 *   pin-paints-only-its-number — ORD-0403's claim survives this order: the text went beside the pin,
 *                             not into it.
 *
 * The rule's own scope is respected: `MAP_LABEL_SCOPE.judged` is canvas label strings, and the basemap
 * attribution is DOM text the ORD-A obligation requires. This file therefore judges the label chips,
 * and separately proves the attribution is not covered by them — it never feeds the attribution to
 * `mapLabelViolations` as a violation.
 */

const FILE = "e2e/qoder-0512-stop-names-on-the-glass.spec.ts";
const RESERVED_SELECTOR = "[data-testid='v8-map-attribution']";
const LABEL_SELECTOR = ".v8-map-panel [data-testid='v8-map-stop-label']";

const node = (arm: string, detail: string) => `${FILE} :: ${arm} — ${detail}`;

async function enterMap(page: Page): Promise<void> {
  /* The address, not the tab: `.v8-mobile-nav` is `display:none` above 680 px, so an arm that clicks
     `nav-map` can only ever run at phone width. `/v8/map` is the same route the tab navigates to
     (`V8Experience.tsx` pushes `/v8/${key}`), which lets the 390 arm and the desktop arm read one
     screen instead of two different ways of reaching it. */
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/map", ["v8-maplibre-panel"]);
  await page.waitForTimeout(1_800);
}

/** Every `active_plan.activities[]` array reachable in a decoded response, with both name fields. */
function collectStops(bodies: unknown[]): { title: string; place_name: string | null }[] {
  const found: { title: string; place_name: string | null }[] = [];
  const walk = (value: unknown) => {
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (value && typeof value === "object") {
      const record = value as Record<string, unknown>;
      const plan = record.active_plan as { activities?: unknown } | undefined;
      if (Array.isArray(plan?.activities)) {
        for (const item of plan!.activities as Record<string, unknown>[]) {
          if (typeof item?.title === "string") {
            found.push({
              title: item.title,
              place_name: typeof item.place_name === "string" ? item.place_name : null,
            });
          }
        }
      }
      Object.values(record).forEach(walk);
    }
  };
  bodies.forEach(walk);
  return found;
}

type LabelReading = {
  ordinal: string;
  text: string;
  source: string | null;
  placement: string | null;
  side: string | null;
  fitsRight: string | null;
  fitsLeft: string | null;
  fitsUnder: string | null;
  blockedBy: string | null;
  visible: boolean;
  box: { x: number; y: number; w: number; h: number };
  boxf: { x: number; y: number; w: number; h: number };
  inCanvas: boolean;
};

const readLabels = (page: Page) =>
  page.evaluate((selector: string) => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const frame = canvas?.getBoundingClientRect();
    const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
    return {
      labelNodeCount: nodes.length,
      canvasInnerTextChars: canvas ? (canvas as HTMLElement).innerText.length : -1,
      /* The surface and the pins' own centres, kept unrounded: the third check in
         `hidden-reasons-are-told` rebuilds each side's box from the screen rather than trusting the
         attribute that claims it, and a rounded box would disagree with the product at the boundary. */
      canvasRect: frame ? { x: frame.x, y: frame.y, w: frame.width, h: frame.height } : null,
      pinCentres: Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker")).map((el) => {
        const b = el.getBoundingClientRect();
        return { ordinal: (el.textContent ?? "").trim(), cx: b.x + b.width / 2, cy: b.y + b.height / 2 };
      }),
      pinTexts: Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker")).map(
        (el) => (el.textContent ?? "").trim(),
      ),
      /* How many pins the camera is actually showing: without this, "four names hidden as `off_map`"
         reads as a placement rule losing names rather than as stops the journey's view left outside
         the surface -- two different facts that look identical in the label list alone. */
      pins_inside_canvas: Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"))
        .filter((el) => {
          if (!frame) return false;
          const b = el.getBoundingClientRect();
          return b.x >= frame.x - 1 && b.y >= frame.y - 1
            && b.x + b.width <= frame.x + frame.width + 1
            && b.y + b.height <= frame.y + frame.height + 1;
        }).length,
      labels: nodes.map((el) => {
        const box = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return {
          ordinal: el.getAttribute("data-stop-ordinal") ?? "",
          text: (el.textContent ?? "").trim(),
          source: el.getAttribute("data-name-source"),
          placement: el.getAttribute("data-placement"),
          side: el.getAttribute("data-side"),
          fitsRight: el.getAttribute("data-fits-right"),
          fitsLeft: el.getAttribute("data-fits-left"),
          fitsUnder: el.getAttribute("data-fits-under"),
          blockedBy: el.getAttribute("data-blocked-by"),
          visible: style.visibility === "visible" && style.display !== "none" && box.width > 0,
          box: {
            x: Math.round(box.x),
            y: Math.round(box.y),
            w: Math.round(box.width),
            h: Math.round(box.height),
          },
          boxf: { x: box.x, y: box.y, w: box.width, h: box.height },
          inCanvas: !!frame
            && box.x >= frame.x - 1
            && box.y >= frame.y - 1
            && box.x + box.width <= frame.x + frame.width + 1
            && box.y + box.height <= frame.y + frame.height + 1,
        };
      }),
    };
  }, LABEL_SELECTOR);

/** Overlap area in px^2 between two client rects, as the accepted map gates measure it. */
function overlapArea(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? Math.round(w * h) : 0;
}

const readAttributionBox = (page: Page) =>
  page.evaluate((selector: string) => {
    const el = document.querySelector<HTMLElement>(selector);
    if (!el) return null;
    const box = el.getBoundingClientRect();
    return {
      x: Math.round(box.x),
      y: Math.round(box.y),
      w: Math.round(box.width),
      h: Math.round(box.height),
      text: (el.innerText ?? "").trim(),
    };
  }, RESERVED_SELECTOR);

/**
 * Paint, or not: the same clip with this one label visible and with it hidden.
 *
 * The mutation is the instrument, not the product — the inline override is removed in the same call,
 * and the only thing the arm concludes from it is whether those pixels came from that node.
 */
async function inkDelta(
  page: Page,
  box: { x: number; y: number; w: number; h: number },
  ordinal: string,
): Promise<boolean> {
  const view = page.viewportSize();
  const clip = {
    x: Math.max(0, Math.round(box.x)),
    y: Math.max(0, Math.round(box.y)),
    width: Math.max(1, Math.round(Math.min(box.w, (view?.width ?? box.x + box.w) - Math.max(0, box.x)))),
    height: Math.max(1, Math.round(Math.min(box.h, (view?.height ?? box.y + box.h) - Math.max(0, box.y)))),
  };
  const shown = await page.screenshot({ clip });
  const hide = async (hidden: boolean) => {
    await page.evaluate(
      ({ selector, ordinal: n, on }: { selector: string; ordinal: string; on: boolean }) => {
        const el = Array.from(document.querySelectorAll<HTMLElement>(selector)).find(
          (candidate) => (candidate.getAttribute("data-stop-ordinal") ?? "") === n,
        );
        if (!el) return;
        if (on) el.style.setProperty("visibility", "hidden", "important");
        else el.style.removeProperty("visibility");
      },
      { selector: LABEL_SELECTOR, ordinal, on: hidden },
    );
  };
  await hide(true);
  const notPainted = await page.screenshot({ clip });
  await hide(false);
  return !shown.equals(notPainted);
}

/** Hide every name at once, run a read, put them back. Used to prove whose pixels are whose. */
async function withAllNamesHidden<T>(page: Page, body: () => Promise<T>): Promise<T> {
  await page.evaluate((selector: string) => {
    const style = document.createElement("style");
    style.id = "qoder-0512-hide-names";
    style.textContent = `${selector} { visibility: hidden !important; }`;
    document.head.appendChild(style);
  }, LABEL_SELECTOR);
  try {
    return await body();
  } finally {
    await page.evaluate(() => document.getElementById("qoder-0512-hide-names")?.remove());
  }
}

const push = (arm: string, body: Record<string, unknown>) => {
  writeFileSync(
    evidencePath("ord0512-glass", `reading_${arm}.json`),
    JSON.stringify({ file: FILE, arm, run_id: process.env.CITYNEXUS_E2E_RUN_ID ?? null, ...body }, null, 1),
  );
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(body)}`);
};

test("names-painted-390 — every located stop has its word on the glass, and the word is the server's", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const bodies: unknown[] = [];
  page.on("response", async (response) => {
    if (!response.url().includes("/api/v1/")) return;
    try {
      bodies.push(await response.json());
    } catch {
      /* a tile or a non-JSON answer carries no activity list */
    }
  });
  await enterMap(page);
  const stops = collectStops(bodies);
  const read = await readLabels(page);
  const shown = read.labels.filter((label) => label.visible);
  /* Which word should be painted, computed the way the product says it is: the place's own name when
     the door sent one, the plan's title otherwise. Deduplicated because the same payload is seen on
     more than one response, and a name the glass carries twice is not a missing name. */
  const expected = Array.from(
    new Set(
      stops.map((stop) =>
        (stop.place_name && stop.place_name.trim() ? stop.place_name.trim() : stop.title.trim()),
      ),
    ),
  ).filter((word) => word.length > 0);
  const paintedAnywhere = read.labels.map((label) => label.text);
  const missing = expected.filter((word) => !paintedAnywhere.includes(word));
  const placeNameKeysInPayloads = stops.filter((stop) => stop.place_name !== null).length;
  const inked = shown.length ? await inkDelta(page, shown[0].box, shown[0].ordinal) : false;
  /* A second read after the camera has had longer to settle: "3 names, then 5" is a timing fact about
     the placement pass, and it has to be told apart from "the map really has room for three". */
  await page.waitForTimeout(2_000);
  const settled = await readLabels(page);
  const settledShown = settled.labels.filter((label) => label.visible);
  push("names-painted-390", {
    viewport: "390x844",
    server_stop_rows_in_payloads: stops.length,
    server_distinct_names: expected.length,
    pin_count: read.pinTexts.length,
    label_node_count: read.labelNodeCount,
    shown_count: shown.length,
    shown_texts: shown.map((label) => label.text),
    placements_at_first_read: read.labels.map((label) => [label.placement, label.side]),
    settled_shown_count: settledShown.length,
    placements_after_settle: settled.labels.map((label) => [label.placement, label.side]),
    missing_from_glass: missing,
    canvasInnerTextChars: read.canvasInnerTextChars,
    /* which branch of `stopLabel()` this screen actually fed: the owner's v8 read sends no
       `place_name` at all, and saying so is the honest half of "priority implemented". */
    place_name_keys_in_payloads: placeNameKeysInPayloads,
    sources: shown.map((label) => label.source),
    first_label_ink_changed_it: inked,
  });
  expect(stops.length, node("A1", "the screen received no activity list, so no word was ever comparable")).toBeGreaterThan(0);
  expect(expected.length, node("A1", `every server row arrived nameless: ${JSON.stringify(stops)}`)).toBeGreaterThan(0);
  expect(missing, node("A1", `names the server sent but the map does not carry: ${JSON.stringify(missing)}`)).toEqual([]);
  expect(read.labelNodeCount, node("A1", `a located stop has no name beside it: ${JSON.stringify(read)}`)).toBe(read.pinTexts.length);
  expect(shown.length, node("A1", "no stop name reached the glass at all")).toBeGreaterThan(0);
  expect(inked, node("A1", `the first shown label paints no pixels of its own: ${JSON.stringify(shown[0])}`)).toBe(true);
});

test("names-painted-desktop — the same words at 1280x900", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const bodies: unknown[] = [];
  page.on("response", async (response) => {
    if (!response.url().includes("/api/v1/")) return;
    try {
      bodies.push(await response.json());
    } catch {
      /* ignore */
    }
  });
  await enterMap(page);
  const stops = collectStops(bodies);
  const read = await readLabels(page);
  const shown = read.labels.filter((label) => label.visible);
  const allInside = read.labels.every((label) => label.inCanvas);
  push("names-painted-desktop", {
    viewport: "1280x900",
    server_stop_count: stops.length,
    label_node_count: read.labelNodeCount,
    shown_count: shown.length,
    placements: read.labels.map((label) => label.placement),
    all_label_boxes_inside_canvas: allInside,
  });
  expect(shown.length, node("A2", `no name on the glass at desktop either: ${JSON.stringify(read)}`)).toBeGreaterThan(0);
  expect(allInside, node("A2", `a name sits outside the map surface: ${JSON.stringify(read.labels.filter((l) => !l.inCanvas))}`)).toBe(true);
});

test("crowding-hides — where the stops converge the names take turns, and the picked one keeps its word", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  /* Pick a stop first: priority is the half of the rule that makes "hide some of them" a decision
     rather than a loss. */
  const pins = page.locator(".v8-map-panel .v9-map-marker");
  expect(await pins.count(), node("B1", "no pin to pick, so the priority claim could not be tested")).toBeGreaterThan(1);
  const picked = pins.nth(0);
  await picked.click();
  await page.waitForTimeout(1_200);
  /* Which pin the tap actually landed on, read back rather than assumed: this seed puts a check-in and
     a check-out on the same bed, so a click at one centre can answer for the other (the same reason
     `qoder-ord0417-tap-names-the-stop.spec.ts` taps a pin that proves it was hit). Priority is then
     judged against the stop the screen really selected. */
  const pressedOrdinal = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker")).find(
      (marker) => marker.getAttribute("aria-pressed") === "true",
    );
    return el ? (el.textContent ?? "").trim() : null;
  });
  expect(pressedOrdinal, node("B1", "the tap selected no pin, so priority had nothing to be about")).not.toBeNull();

  const before = await readLabels(page);
  const shownBefore = before.labels.filter((label) => label.visible).length;
  await page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: { setZoom: (z: number) => void; getZoom: () => number } }) | null)?.citynexusMap;
    map?.setZoom(6);
  });
  await page.waitForTimeout(1_500);
  const after = await readLabels(page);
  const shownAfter = after.labels.filter((label) => label.visible);
  const pairs: { a: string; b: string; area: number }[] = [];
  for (let i = 0; i < shownAfter.length; i += 1) {
    for (let j = i + 1; j < shownAfter.length; j += 1) {
      const area = overlapArea(shownAfter[i].box, shownAfter[j].box);
      if (area > 0) pairs.push({ a: shownAfter[i].text, b: shownAfter[j].text, area });
    }
  }
  const hiddenStillInDocument = after.labelNodeCount === before.labelNodeCount;
  const reasons = after.labels.filter((label) => !label.visible).map((label) => label.placement);
  const pickedKept = shownAfter.some((label) => label.ordinal === pressedOrdinal);
  push("crowding-hides", {
    zoom_fit_shown: shownBefore,
    zoom_6_shown: shownAfter.length,
    pins_inside_canvas_at_fit: before.pins_inside_canvas,
    pins_inside_canvas_at_zoom_6: after.pins_inside_canvas,
    label_node_count: after.labelNodeCount,
    hidden_still_in_document: hiddenStillInDocument,
    hidden_reasons: reasons,
    overlapping_shown_pairs: pairs,
    picked_pin_ordinal: pressedOrdinal,
    picked_stop_kept_its_name: pickedKept,
  });
  /* The floor the two-sided placement was written for: r1 reached 3 names on this same post-pick view
     at 390x844, and four of the misses were chips that only ever reached to the right of their pin. With
     the left side allowed, the same view, the same seed and the same camera carry 5. */
  expect(shownBefore, node("B1", `only ${shownBefore} names after the pick: the placement rule is back to one side`)).toBeGreaterThanOrEqual(5);
  expect(shownAfter.length, node("B1", `nothing was painted at all when the stops converged: ${JSON.stringify(after)}`)).toBeGreaterThan(0);
  expect(shownAfter.length, node("B1", `a crowded view hid no name, so the rule is not being applied: ${JSON.stringify(after)}`)).toBeLessThan(before.labelNodeCount);
  expect(hiddenStillInDocument, node("B1", "hiding removed the node instead of clipping it — pins and rows are counted as proof a stop is still pickable")).toBe(true);
  expect(pairs, node("B1", `two shown names overlap: ${JSON.stringify(pairs)}`)).toEqual([]);
  for (const reason of reasons) {
    expect(["off_map", "over_a_name", "over_a_control", "no_box"], node("B1", `an unnamed hide reason reached the glass: ${reason}`)).toContain(reason);
  }
  expect(pickedKept, node("B1", "the stop the traveller picked lost its name to a neighbour")).toBe(true);
});

test("attribution-never-covered — a name never paints over the licence sentence", async ({ page }) => {
  /* The shapes ORD-0412 already answers for: the phone band this screen ships on, the narrowest width
     the product supports, and the desktop shell. `page.screenshot({clip})` refuses a box that leaves
     the viewport, so each crop is clamped and an empty clamp is reported as the layout fact it is. */
  for (const shape of [{ width: 390, height: 844 }, { width: 320, height: 844 }, { width: 1280, height: 900 }] as const) {
    await page.setViewportSize(shape);
    await enterMap(page);
    const read = await readLabels(page);
    const shown = read.labels.filter((label) => label.visible);
    const attribution = await readAttributionBox(page);
    expect(attribution, node("C1", "the basemap attribution is not on this screen at all")).not.toBeNull();
    const overlaps = shown
      .map((label) => ({ text: label.text, area: overlapArea(label.box, attribution!) }))
      .filter((entry) => entry.area > 0);
    const clip = {
      x: Math.max(0, attribution!.x),
      y: Math.max(0, attribution!.y),
      width: Math.min(attribution!.w, shape.width - Math.max(0, attribution!.x)),
      height: Math.min(attribution!.h, shape.height - Math.max(0, attribution!.y)),
    };
    const fullyInViewport = attribution!.y + attribution!.h <= shape.height && clip.width >= 4 && clip.height >= 4;
    if (!fullyInViewport) {
      /* Measured on this very screen, and reported rather than asserted away: at 1280x900 the licence
         band sits below the fold, so no crop of it fits in the image Playwright can take. The geometry
         claim still runs; the pixel claim is named NOT_MEASURED at this shape instead of being quietly
         dropped. Whether the shell put it there before this order's labels exist is answered by the
         base reading in the lane report, not by this comment. */
      push(`attribution-never-covered-${shape.width}x${shape.height}`, {
        shown_count: shown.length,
        attribution_box: attribution,
        licence_box_inside_viewport: fullyInViewport,
        overlapping_shown_labels: overlaps,
        pixel_claim: "NOT_MEASURED: the licence box does not fit inside the viewport at this shape",
      });
      expect(overlaps, node("C1", `${shape.width}x${shape.height}: a name covers the attribution: ${JSON.stringify(overlaps)}`)).toEqual([]);
      continue;
    }
    /* Recorded, not asserted: the whole overlay band is `pointer-events:none` on purpose
       (`map-attribution.css`, and the same rule the stamp badge follows), so a hit-test at its centre
       answers with the canvas underneath. That is why "is a name painted over the licence" cannot be
       asked of `elementFromPoint` here, and why the pixel half below carries the claim. */
    const onTop = await page.evaluate((selector: string) => {
      const el = document.querySelector<HTMLElement>(selector);
      if (!el) return "no-element";
      const box = el.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return hit === el || el.contains(hit) ? "attribution" : (hit?.className ?? "other");
    }, RESERVED_SELECTOR);
    /* The pixel half: crop the attribution once with the names on the glass, then take the same crop
       inside a frame where every name is hidden — through a stylesheet this arm owns and removes, so no
       later shape in this loop inherits the mutation. Identical bytes mean no name contributed a single
       pixel inside that box. */
    const attributionClip = { x: clip.x, y: clip.y, width: clip.width, height: clip.height };
    const withNames = await page.screenshot({ clip: attributionClip });
    const withoutNames = await withAllNamesHidden(page, () => page.screenshot({ clip: attributionClip }));
    /* ...and the same box with only the sentence itself taken away: proof that the crop the previous
       two frames were compared on carries the licence text at all. */
    await page.evaluate(
      (selector: string) => {
        const el = document.querySelector<HTMLElement>(selector);
        if (el) el.style.setProperty("visibility", "hidden", "important");
      },
      RESERVED_SELECTOR,
    );
    const withoutSentence = await page.screenshot({ clip: attributionClip });
    await page.evaluate((selector: string) => {
      const el = document.querySelector<HTMLElement>(selector);
      if (el) el.style.removeProperty("visibility");
    }, RESERVED_SELECTOR);
    const attributionOwnsItsPixels = !withNames.equals(withoutSentence);
    push(`attribution-never-covered-${shape.width}x${shape.height}`, {
      shown_count: shown.length,
      attribution_text: attribution!.text,
      overlapping_shown_labels: overlaps,
      centre_hit_test_recorded_only: onTop,
      attribution_pixels_identical_with_and_without_names: withNames.equals(withoutNames),
      attribution_owns_its_own_pixels: attributionOwnsItsPixels,
    });
    expect(overlaps, node("C1", `${shape.width}x${shape.height}: a name covers the attribution: ${JSON.stringify(overlaps)}`)).toEqual([]);
    /* The negative control this arm needs: if the sentence painted no pixels of its own, "hiding the
       names changed nothing" would be vacuous. */
    expect(attributionOwnsItsPixels, node("C1", `${shape.width}x${shape.height}: the attribution box itself paints no pixels — nothing for a name to cover`)).toBe(true);
    expect(withNames.equals(withoutNames), node("C1", `${shape.width}x${shape.height}: the attribution's own pixels changed when names were hidden`)).toBe(true);
  }
});

test("painted-set-passes-the-rule-list — the words now on the glass are judged by the shared list", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const read = await readLabels(page);
  const shown = read.labels.filter((label) => label.visible);
  const judged = shown.map((label) => ({ text: label.text, violations: mapLabelViolations(label.text) }));
  const offenders = judged.filter((entry) => entry.violations.length > 0);
  push("painted-set-passes-the-rule-list", {
    /* ORD-0415's finding, kept as the before-picture: this is what the existing gate's corpus was made
       of, in characters of actual canvas text. */
    canvasInnerTextChars: read.canvasInnerTextChars,
    judged_surface: "canvas-label-strings",
    corpus_strings: shown.length,
    corpus_chars: shown.reduce((total, label) => total + label.text.length, 0),
    offenders,
    forbidden_classes_checked: ["clock-time", "duration", "money", "source"],
  });
  expect(shown.length, node("D1", "no painted string to judge — the corpus would be empty again")).toBeGreaterThan(0);
  expect(offenders, node("D1", `forbidden class painted on the glass: ${JSON.stringify(offenders)}`)).toEqual([]);
});

test("no-machine-word-on-glass — the field name is not the place name", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const read = await readLabels(page);
  const shown = read.labels.filter((label) => label.visible);
  const machine = /(^|\W)(place_name|sequence_no|coordinate_system|NOT_ANSWERED|RECENT_UPDATE|journeys\.)/;
  const uuid = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
  const hits = shown
    .map((label) => ({
      text: label.text,
      ordinal: label.ordinal,
      machine: machine.test(label.text),
      uuid: uuid.test(label.text),
      is_its_own_number: label.text === label.ordinal,
    }))
    .filter((entry) => entry.machine || entry.uuid || entry.is_its_own_number);
  push("no-machine-word-on-glass", { shown_texts: shown.map((label) => label.text), offenders: hits });
  expect(shown.length, node("D2", "nothing painted, so this arm would pass vacuously")).toBeGreaterThan(0);
  expect(hits, node("D2", `a machine word is painted: ${JSON.stringify(hits)}`)).toEqual([]);
});

test("labels-move-with-camera — the names belong to the geography, not to the panel", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const before = (await readLabels(page)).labels.filter((label) => label.visible);
  expect(before.length, node("E1", "no shown name to follow the camera")).toBeGreaterThan(0);
  const canvasBox = await page.locator(".v8-maplibre-canvas").boundingBox();
  expect(canvasBox, node("E1", "the map canvas has no box to drag on")).not.toBeNull();
  await page.mouse.move(canvasBox!.x + canvasBox!.width / 2, canvasBox!.y + canvasBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(canvasBox!.x + canvasBox!.width / 2 - 70, canvasBox!.y + canvasBox!.height / 2 - 45, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(1_500);
  const after = (await readLabels(page)).labels.filter((label) => label.visible);
  /* Only a name that was on the glass in both frames can follow the camera. Which names appeared and
     which fell off is a placement result of the new view, recorded separately — folding them into
     "did it move" would let a re-fit look like a stuck label. */
  const common = after.filter((label) => before.some((other) => other.ordinal === label.ordinal));
  const moved = common.map((label) => {
    const was = before.find((other) => other.ordinal === label.ordinal)!;
    return { text: label.text, dx: Math.abs(label.box.x - was.box.x), dy: Math.abs(label.box.y - was.box.y) };
  });
  const stuck = moved.filter((entry) => entry.dx <= 5 && entry.dy <= 5);
  const appeared = after.filter((label) => !before.some((other) => other.ordinal === label.ordinal)).map((l) => l.text);
  const vanished = before.filter((label) => !after.some((other) => other.ordinal === label.ordinal)).map((l) => l.text);
  const inked = after.length ? await inkDelta(page, after[0].box, after[0].ordinal) : false;
  push("labels-move-with-camera", {
    before: before.length,
    after: after.length,
    common_compared: moved.length,
    moved,
    stuck,
    appeared_after_the_drag: appeared,
    vanished_after_the_drag: vanished,
    first_still_inked: inked,
  });
  expect(moved.length, node("E1", `no name was on the glass both before and after the drag: ${JSON.stringify({ before: before.length, after: after.length })}`)).toBeGreaterThan(0);
  expect(stuck, node("E1", `a name did not move with the camera: ${JSON.stringify(stuck)}`)).toEqual([]);
  expect(inked, node("E1", "after the pan the followed name paints no pixels")).toBe(true);
});

test("hidden-reasons-are-told — a name that is not on the glass says which kind of no it is", async ({ page }) => {
  /* The second generation's own claim: `off_map` used to cover two different facts -- no side of the pin
     can hold this word, and a side can hold it but somebody else owns that ground. Both came back as a
     name that simply is not there. The element now carries the per-side verdict and the blocker, and this
     arm checks those three attributes against `data-placement` until they contradict each other: a name
     cannot be `off_map` while a side fits, and cannot be `over_a_name` while nothing stands on it. */
  const check = (rows: Awaited<ReturnType<typeof readLabels>>["labels"]) => rows
    .map((label) => {
      const fits = [label.fitsRight === "true", label.fitsLeft === "true", label.fitsUnder === "true"];
      if (label.placement === null || label.fitsRight === null || label.fitsLeft === null
        || label.fitsUnder === null || label.blockedBy === null) return `${label.text}: an attribute is missing`;
      if (!label.visible && label.placement === "shown") return `${label.text}: shown yet hidden`;
      if (label.placement === "shown") {
        return !fits.some(Boolean) || label.blockedBy !== "nothing"
          ? `${label.text}: shown but not free (fits ${JSON.stringify(fits)}, blocked by ${label.blockedBy})` : "";
      }
      if (label.placement === "off_map") {
        return fits.some(Boolean) || label.blockedBy !== "nothing"
          ? `${label.text}: off_map while ${JSON.stringify(fits)} says a side fits / ${label.blockedBy} stands on it` : "";
      }
      if (label.placement === "over_a_name" || label.placement === "over_a_control") {
        const wanted = label.placement === "over_a_name" ? "name" : "control";
        return !fits.some(Boolean) || label.blockedBy !== wanted
          ? `${label.text}: ${label.placement} contradicted by fits ${JSON.stringify(fits)} / blocked by ${label.blockedBy}` : "";
      }
      if (label.placement === "no_box") {
        return fits.some(Boolean) ? `${label.text}: no_box yet a side fits` : "";
      }
      return `${label.text}: unknown placement ${label.placement}`;
    })
    .filter(Boolean);

  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const fitted = await readLabels(page);
  const fittedViolations = check(fitted.labels);
  await page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: { setZoom: (z: number) => void } }) | null)?.citynexusMap;
    map?.setZoom(6);
  });
  await page.waitForTimeout(1_500);
  const crowded = await readLabels(page);
  const crowdedViolations = check(crowded.labels);
  const tally = (rows: typeof crowded["labels"]) => rows.reduce<Record<string, number>>((acc, label) => {
    const key = label.visible ? "shown" : String(label.placement);
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  /* The attributes can be mutually consistent and still describe a screen that does not exist: with
     three sides the product has fewer `off_map` names left, and a chip that simply always claimed a
     flank fitted would no longer contradict any placement. So the claims are measured here rather than
     cross-read -- each side's box is rebuilt from the pin's own centre on this screen, the chip's own
     size, and the surface, and compared with what the element says. The gap off the pin is derived from
     where the names actually landed, never copied from the source, so changing that constant moves both
     sides of this check together. */
  const measure = (read: Awaited<ReturnType<typeof readLabels>>) => {
    const canvas = read.canvasRect;
    if (!canvas) return { problems: ["the map surface is not on this screen, so no side can be judged"], gap: null };
    const centres = new Map(read.pinCentres.map((pin) => [pin.ordinal, pin]));
    const EPS = 1;
    const inside = (x: number, y: number, w: number, h: number) => x >= canvas.x - EPS && y >= canvas.y - EPS
      && x + w <= canvas.x + canvas.w + EPS && y + h <= canvas.y + canvas.h + EPS;
    const gaps: number[] = [];
    for (const label of read.labels) {
      const pin = centres.get(label.ordinal);
      if (!pin || label.placement !== "shown") continue;
      if (label.side === "right") gaps.push(label.boxf.x - pin.cx);
      else if (label.side === "left") gaps.push(pin.cx - label.boxf.x - label.boxf.w);
      else if (label.side === "under") gaps.push(label.boxf.y - pin.cy);
    }
    const problems: string[] = [];
    if (!gaps.length) return { problems: ["no shown name to derive the gap from, so every fits claim is unverified"], gap: null };
    const gap = gaps.reduce((sum, value) => sum + value, 0) / gaps.length;
    if (Math.max(...gaps) - Math.min(...gaps) > 2) {
      problems.push(`the flanks do not share one gap off the pin: ${JSON.stringify(gaps.map((v) => Math.round(v)))}`);
    }
    for (const label of read.labels) {
      const pin = centres.get(label.ordinal);
      if (!pin) { problems.push(`${label.text}: no pin on this screen to measure its sides from`); continue; }
      const real = [
        inside(pin.cx + gap, pin.cy - label.boxf.h / 2, label.boxf.w, label.boxf.h),
        inside(pin.cx - gap - label.boxf.w, pin.cy - label.boxf.h / 2, label.boxf.w, label.boxf.h),
        inside(pin.cx - label.boxf.w / 2, pin.cy + gap, label.boxf.w, label.boxf.h),
      ];
      const told = [label.fitsRight === "true", label.fitsLeft === "true", label.fitsUnder === "true"];
      real.forEach((ok, index) => {
        if (ok !== told[index]) {
          problems.push(`${label.text}: says ${["right", "left", "under"][index]} fits=${told[index]} measured from the screen it is ${ok}`);
        }
      });
    }
    return { problems, gap: Math.round(gap * 10) / 10 };
  };
  const fittedMeasured = measure(fitted);
  const crowdedMeasured = measure(crowded);
  push("hidden-reasons-are-told", {
    fitted_tally: tally(fitted.labels),
    crowded_tally: tally(crowded.labels),
    gap_off_pin_px_fitted: fittedMeasured.gap,
    gap_off_pin_px_crowded: crowdedMeasured.gap,
    measured_against_screen_fitted: fittedMeasured.problems,
    measured_against_screen_crowded: crowdedMeasured.problems,
    fitted_rows: fitted.labels.map((label) => [label.text, label.placement, label.side, label.fitsRight, label.fitsLeft, label.fitsUnder, label.blockedBy]),
    crowded_rows: crowded.labels.map((label) => [label.placement, label.side, label.fitsRight, label.fitsLeft, label.fitsUnder, label.blockedBy]),
    contradictions: [...fittedViolations, ...crowdedViolations],
  });
  expect(crowded.labelNodeCount, node("F1", "no name nodes at all, so every verdict below would be vacuous")).toBeGreaterThan(0);
  expect(fittedViolations, node("F1", `at the journey's own view: ${JSON.stringify(fittedViolations)}`)).toEqual([]);
  expect(crowdedViolations, node("F1", `at a crowded zoom: ${JSON.stringify(crowdedViolations)}`)).toEqual([]);
  expect(fittedMeasured.problems, node("F2", `the fits attributes disagree with the screen at the journey's own view: ${JSON.stringify(fittedMeasured.problems)}`)).toEqual([]);
  expect(crowdedMeasured.problems, node("F2", `the fits attributes disagree with the screen at a crowded zoom: ${JSON.stringify(crowdedMeasured.problems)}`)).toEqual([]);
});

test("third-side-is-tried — when both flanks are owned the word goes under its pin instead of vanishing", async ({ page }) => {
  /* The third generation's own claim, and the reason the ruling bought it: two sides leave a name with
     nowhere to go on a phone-width map, and hiding it is the same silence as having no name. This arm
     does not read the source and admire the array -- it takes the two flanks away on the live screen and
     requires the word to come back below its pin. `applyLabels` measures reserved ground off the DOM, so
     a band carrying one of the selectors the panel already reserves is enough; the arm adds it, asks the
     camera for a re-fit, and removes it again. */
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const before = await readLabels(page);
  const keptRight = before.labels.filter((label) => label.visible && label.side === "right");
  expect(keptRight.length, node("F3", `no name held its right flank to begin with, so this arm would prove nothing: ${JSON.stringify(before.labels.map((l) => [l.text, l.placement, l.side]))}`)).toBeGreaterThan(0);

  const bandTop = Math.round(keptRight[0].box.y + keptRight[0].box.h / 2) - 12;
  const band = await page.evaluate((top: number) => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const frame = canvas?.getBoundingClientRect();
    if (!frame) return null;
    const el = document.createElement("div");
    el.className = "v8-map-tile-notice";
    el.setAttribute("data-ord0512-band", "1");
    el.style.cssText = [
      "position:fixed", `left:${Math.round(frame.x)}px`, `top:${top}px`,
      `width:${Math.round(frame.width)}px`, "height:24px", "padding:0", "margin:0",
      "border:0", "background:transparent", "pointer-events:none", "box-sizing:border-box",
    ].join(";");
    document.querySelector(".v8-map-panel")?.appendChild(el);
    const host = canvas as (HTMLElement & { citynexusMap?: { getZoom: () => number; setZoom: (z: number) => void } }) | null;
    const map = host?.citynexusMap;
    if (map) map.setZoom(map.getZoom() + 0.001);
    return { top, width: Math.round(frame.width) };
  }, bandTop);
  expect(band, node("F3", "the panel has no map surface to band across, so this arm cannot run")).not.toBeNull();
  await page.waitForTimeout(1_500);

  const under = await readLabels(page);
  const underShown = under.labels.filter((label) => label.visible && label.side === "under");
  const flanksTaken = under.labels.filter((label) => label.visible && label.side !== "under"
    && label.blockedBy === "nothing");
  push("third-side-is-tried", {
    band,
    names_before: keptRight.map((label) => [label.text, label.side]),
    names_after: under.labels.map((label) => [label.text, label.placement, label.side, label.fitsRight, label.fitsLeft, label.fitsUnder, label.blockedBy]),
    under_shown: underShown.map((label) => label.text),
    still_on_a_flank: flanksTaken.map((label) => [label.text, label.side]),
  });
  expect(underShown.length, node("F3", `both flanks of every name are owned, yet nothing moved under its pin: ${JSON.stringify(under.labels.map((l) => [l.text, l.placement, l.side, l.fitsUnder]))}`)).toBeGreaterThan(0);
  expect(underShown.every((label) => label.placement === "shown" && label.blockedBy === "nothing"),
    node("F3", `an under-placed name came back with a contradiction: ${JSON.stringify(underShown.map((l) => [l.text, l.placement, l.blockedBy]))}`)).toBe(true);

  await page.evaluate(() => {
    Array.from(document.querySelectorAll<HTMLElement>("[data-ord0512-band]")).forEach((el) => el.remove());
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const host = canvas as (HTMLElement & { citynexusMap?: { getZoom: () => number; setZoom: (z: number) => void } }) | null;
    const map = host?.citynexusMap;
    if (map) map.setZoom(map.getZoom() - 0.001);
  });
  await page.waitForTimeout(900);
  const after = await readLabels(page);
  /* What has to be gone is the ground this arm borrowed, not the placement it caused: the rule re-runs
     from scratch on every fit, but a name that won the ground under its pin while both flanks were
     blocked can legitimately keep it once they are free, because the stop that outranks it may still be
     sitting on the flank. Asserting the sides snap back would judge the arm's own camera nudge. */
  push("third-side-is-tried-cleanup", {
    band_nodes_left: await page.evaluate(() => document.querySelectorAll("[data-ord0512-band]").length),
    sides_after: after.labels.map((label) => [label.text, label.side, label.visible]),
  });
  expect(await page.evaluate(() => document.querySelectorAll("[data-ord0512-band]").length),
    node("F3", "the borrowed reserved ground is still in the document, so a later reading on this page would be polluted")).toBe(0);
});

test("pin-paints-only-its-number — ORD-0403's claim survives: the word went beside the pin, not into it", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const read = await readLabels(page);
  const notDigits = read.pinTexts.filter((text) => !/^\d+$/.test(text));
  push("pin-paints-only-its-number", { pin_count: read.pinTexts.length, pin_texts: read.pinTexts, not_digits: notDigits, label_nodes: read.labelNodeCount });
  expect(read.pinTexts.length, node("E2", "the map painted no pins, so this arm proves nothing about them")).toBeGreaterThan(0);
  expect(notDigits, node("E2", `a pin now paints more than its stop number: ${JSON.stringify(notDigits)}`)).toEqual([]);
});
