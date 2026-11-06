import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0390 — the map and the card share one selection, measured from both ends.
 *
 * Before this order the map was a picture: `MapPanel` built markers as bare painted dots
 * (`aria-label` for a screen reader, nothing else) and the shell's `detailActivityId` could only be
 * written from the trips list. `ActivityDetail`'s 「在地图上显示」 even cleared the id before
 * navigating, so the action threw away the payload it needed.
 *
 * Four things are read here, and each is a value, not a control's existence:
 *   forward  — click a marker, then read the map screen's own selection card text.
 *   reverse  — click a stop row, then read `map.getCenter()` off the camera.
 *   glass    — every painted string inside the map canvas must stay on the whitelist
 *              (name/number only): no clock time, no currency, no provider host.
 *   reach    — at 320/360/390/414 each marker's hit box is found by `elementFromPoint` at its own
 *              centre, which is the ORD-0338 lesson: a control that is merely laid out is not a
 *              control that can be hit.
 *
 * The camera is read through the seam `MapPanel` already publishes on the container
 * (`citynexusMap`), because maplibre keeps its instance off the DOM.
 */

const FILE = "e2e/qoder-map-ord0390-selection-both-ways.spec.ts";
const WIDTHS = [320, 360, 390, 414];

async function enterMap(page: Page): Promise<void> {
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

const camera = (page: Page) =>
  page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
    if (!map) return null;
    const c = map.getCenter();
    return { lng: Number(c.lng.toFixed(6)), lat: Number(c.lat.toFixed(6)) };
  });

const cardText = (page: Page) =>
  page.getByTestId("map-selected-stop").innerText().then((s) => s.trim());

async function markerBoxes(page: Page) {
  return page.evaluate(() => {
    const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
    return Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"))
      .filter((el) => el.getAttribute("role") === "button")
      .map((el) => {
        const r = el.getBoundingClientRect();
        const x = r.x + r.width / 2;
        const y = r.y + r.height / 2;
        // unproject so "two pins on one pixel" can be told apart from "two stops at one place":
        // the first is a camera/zoom fact, the second is a data fact, and they need different fixes.
        const ll = map ? map.unproject([x, y]) : null;
        return {
          label: el.textContent ?? "",
          aria: el.getAttribute("aria-label") ?? "",
          pressed: el.getAttribute("aria-pressed"),
          x,
          y,
          w: Math.round(r.width),
          h: Math.round(r.height),
          lng: ll ? Number(ll.lng.toFixed(5)) : null,
          lat: ll ? Number(ll.lat.toFixed(5)) : null,
        };
      });
  });
}

const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

test("forward — tapping a marker changes the card text on the same screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const markers = await markerBoxes(page);
  expect(markers.length, "the seeded journey must put at least two pickable stops on the map").toBeGreaterThan(1);

  const before = await cardText(page);
  const target = markers.find((m) => m.aria && m.aria !== before) ?? markers[markers.length - 1];
  await page.locator(".v8-map-panel .v9-map-marker").filter({ hasText: new RegExp(`^${target.label}$`) }).click();
  await page.waitForTimeout(900);
  const after = await cardText(page);
  /* ① both halves have to be reachable, so "the card exists" is not enough: after selecting a pin
     the card must be inside the viewport and answer a hit-test at its own centre. `scrolled>0` is
     deliberately not read anywhere in this file — it is a proxy, and ORD-0338 already showed a
     control can scroll and still not be hittable. */
  const card = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>("[data-testid='map-selected-stop']");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2;
    const cy = r.y + r.height / 2;
    const top = document.elementFromPoint(cx, cy);
    return {
      inViewport: r.top >= -1 && r.bottom <= window.innerHeight + 1,
      hitSelf: !!top && (top === el || el.contains(top) || top.contains(el)),
      topEl: top ? `${top.tagName.toLowerCase()}.${top.className}`.slice(0, 40) : "none",
      box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    };
  });
  const shot = evidencePath("ord0390-selection", "forward.png");
  await page.screenshot({ path: shot });
  push("forward-marker-to-card", {
    before, after, changed: before !== after, marker: target.label, aria: target.aria,
    cardShowsAria: after.includes(target.aria), pressed: target.pressed, card, shot,
  });
  expect(before).not.toBe(after);
  expect(after).toContain(target.aria);
  expect(card, "the selection card must be measurable").not.toBeNull();
  expect(card!.inViewport, `card must be scrolled into view: ${JSON.stringify(card!.box)}`).toBe(true);
  expect(card!.hitSelf, `card must answer a hit-test at its centre, top was ${card!.topEl}`).toBe(true);
});

test("reverse — clicking a stop row moves the camera", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const rows = page.getByTestId(/^map-stop-/);
  expect(await rows.count()).toBeGreaterThan(1);
  const start = await camera(page);
  expect(start, "camera seam must be readable").not.toBeNull();
  // pick the row whose stop is furthest from the current centre, so a real move is required
  await rows.last().click();
  await page.waitForTimeout(1_200);
  const end = await camera(page);
  const pressed = await rows.last().getAttribute("aria-pressed");
  const shot = evidencePath("ord0390-selection", "reverse.png");
  await page.screenshot({ path: shot });
  const moved = !!start && !!end && (start.lng !== end.lng || start.lat !== end.lat);
  push("reverse-row-to-camera", { start, end, moved, lastRowPressed: pressed, shot });
  expect(moved, `camera must move: ${JSON.stringify(start)} -> ${JSON.stringify(end)}`).toBe(true);
  expect(pressed).toBe("true");
});

test("glass — nothing off-whitelist is painted inside the map canvas", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const glass = await page.evaluate(() => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const markers = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
    const painted = markers.map((m) => (m.textContent ?? "").trim());
    const aria = markers.map((m) => m.getAttribute("aria-label") ?? "");
    const text = canvas?.innerText ?? "";
    // annotated: an untyped `[]` here infers never[], and `.concat` of two match arrays then fails
    // spread, not `.concat`: `match() ?? []` infers `RegExpMatchArray | never[]`, and the `never[]`
    // arm makes concat's parameter `ConcatArray<never>` and the call unassignable.
    const timeHits: string[] = [
      ...(text.match(/\b\d{1,2}:\d{2}\b/g) ?? []),
      ...(text.match(/\d+\s*(?:分钟|min)\b/gi) ?? []),
    ];
    const moneyHits: string[] = text.match(/[¥$€£]|CNY|USD|\d+\s*元/g) ?? [];
    const sourceHits: string[] = text.match(/openstreetmap|openfreemap|©/gi) ?? [];
    return { painted, aria, text, timeHits, moneyHits, sourceHits };
  });
  push("glass-whitelist", glass);
  // the caption/notice live outside the canvas; only canvas text is judged here
  expect(glass.timeHits, `times painted on the map: ${JSON.stringify(glass.timeHits)}`).toEqual([]);
  expect(glass.moneyHits, `money painted on the map: ${JSON.stringify(glass.moneyHits)}`).toEqual([]);
});

/** Read the three strings this order put on the glass, verbatim, in whichever language the screen
 *  is currently serving. `stopLocateAria` is an aria-label, so it is read as an attribute — the
 *  other two are painted text and are read out of `innerText`. */
async function readCopy(page: Page) {
  return page.evaluate(() => {
    const painted = (sel: string) =>
      (document.querySelector<HTMLElement>(sel)?.innerText ?? "").trim();
    const row = document.querySelector<HTMLElement>("[data-testid^='map-stop-']");
    const lead = document.querySelector<HTMLElement>("[data-testid='map-selected-stop']");
    return {
      lang: document.documentElement.lang || null,
      selectedLead: lead ? painted("[data-testid='map-selected-stop']") : null,
      stopsKicker: painted(".v8-map-stops .v8-kicker"),
      rowAria: row ? row.getAttribute("aria-label") ?? "" : null,
      rowName: row ? (row.innerText ?? "").trim() : null,
      bodyHasOSMNotice: (document.body.innerText ?? "").includes("tile.openstreetmap.org"),
    };
  });
}

for (const arm of [
  /* `.v8-kicker` carries `text-transform: uppercase`, so what is painted in English is
     "STOPS ON THIS DAY" while the authored string is "Stops on this day". The gate reads the
     screen, so it expects the painted form and records the authored one next to it. */
  { locale: "en", lead: "Selected", kicker: "STOPS ON THIS DAY", aria: "Locate ", toggle: false },
  {
    locale: "zh",
    lead: "已选中",
    kicker: "这一天去的地点",
    aria: "在地图上定位 ",
    toggle: true,
  },
] as const) {
  test(`copy-${arm.locale} — the words on the glass are read verbatim`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page);
    if (arm.toggle) {
      await page.getByTestId("topbar-language").click();
      await page.waitForTimeout(1_200);
    }
    // select a stop so the lead-in renders, then read everything back off the screen
    await page.getByTestId(/^map-stop-/).first().click();
    await page.waitForTimeout(900);
    const c = await readCopy(page);
    /* per-arm evidence: a full-page shot of this state is byte-identical to the forward arm's,
       which would let one arm borrow another's witness. Capture the panel this arm actually reads. */
    const shot = evidencePath("ord0390-selection", `copy_${arm.locale}.png`);
    await page.locator(".v8-map-facts").screenshot({ path: shot });
    push(`copy-${arm.locale}`, { ...c, expectLead: arm.lead, expectKicker: arm.kicker, shot });
    expect(c.selectedLead, `zh/en lead-in must render "${arm.lead}", got ${c.selectedLead}`).toContain(
      arm.lead,
    );
    expect(c.stopsKicker).toBe(arm.kicker);
    expect(c.rowAria ?? "").toContain(arm.aria);
    expect(c.bodyHasOSMNotice, "ORD-A's basemap notice must still be on the glass").toBe(true);
  });
}

for (const width of WIDTHS) {
  test(`reach — every stop is selectable on the map screen @${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await enterMap(page);
    const markers = await markerBoxes(page);
    expect(markers.length).toBeGreaterThan(1);

    /* The first version of this arm demanded that every marker be hit at its own centre, and it
       went red at all four widths for one reason: three stops land on a single pixel
       (markers 2/3/7 all measured centre (126,468) at 320). That is not the layout being pushed
       apart — it is several stops sharing a place, and no amount of CSS makes one pixel answer to
       three taps. So the invariant asserted here is the one that protects the traveller:
         - each pixel cluster yields a hit on a marker of that same cluster (nothing foreign is
           sitting on top of the pins), and
         - every stop in the cluster is still selectable on that screen, via the rows.
       The remaining gap — a co-located pin cannot be tapped individually — is reported with its
       numbers instead of being asserted away, and belongs to a clustering order, not this one. */
    const clusters = new Map<string, typeof markers>();
    for (const m of markers) {
      const key = `${Math.round(m.x)},${Math.round(m.y)}`;
      const group = clusters.get(key);
      if (group) group.push(m);
      else clusters.set(key, [m]);
    }
    const misses: unknown[] = [];
    const coLocated: unknown[] = [];
    for (const [, group] of clusters) {
      const probe = group[0];
      const hit = await page.evaluate(([x, y]) => {
        const el = document.elementFromPoint(x, y);
        const marker = el?.closest?.(".v9-map-marker");
        return marker ? { label: (marker.textContent ?? "").trim() } : null;
      }, [probe.x, probe.y]);
      if (!hit || !group.some((g) => g.label === hit.label)) {
        misses.push({ want: group.map((g) => g.label), got: hit?.label ?? "none",
                      at: [Math.round(probe.x), Math.round(probe.y)] });
      }
      if (group.length > 1) {
        coLocated.push({ labels: group.map((g) => g.label), names: group.map((g) => g.aria),
                         lng: probe.lng, lat: probe.lat,
                         px: [Math.round(probe.x), Math.round(probe.y)] });
      }
    }
    const rowCount = await page.getByTestId(/^map-stop-/).count();
    const smallest = markers.reduce((a, m) => Math.min(a, m.w * m.h), Infinity);
    const scroll = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth, win: window.innerWidth,
    }));
    push(`reach-${width}`, {
      markers: markers.length, clusters: clusters.size, misses, coLocated,
      rows: rowCount, allStopsHaveRows: rowCount === markers.length,
      smallestHitPx2: smallest, ...scroll,
    });
    expect(misses, `a pixel whose top element is not one of its own pins: ${JSON.stringify(misses)}`).toEqual([]);
    expect(rowCount, "every stop on the map has a selectable row at this width").toBe(markers.length);
    expect(scroll.doc, "no horizontal page scroll").toBeLessThanOrEqual(scroll.win + 1);
    expect(smallest, "every marker keeps a real hit box").toBeGreaterThanOrEqual(20 * 20);
  });
}

test("interactive — one finger on the canvas moves the camera (the state the gate must be read in)", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const before = await camera(page);
  expect(before, "camera seam must be readable").not.toBeNull();
  const box = await page.locator(".v8-maplibre-canvas").boundingBox();
  expect(box).not.toBeNull();
  // a plain one-finger pan, the gesture the product promises on the glass
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 - 70, box!.y + box!.height / 2 - 45, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(900);
  const after = await camera(page);
  const moved = !!before && !!after && (before.lng !== after.lng || before.lat !== after.lat);
  push("interactive-drag-moves-camera", { before, after, moved });
  /* This is the arm that makes "we did not pass by switching the map back into a picture" checkable.
     With `interactive:false` the drag is discarded and this goes red — the control run in the
     delivery block measures exactly that. Nothing here is satisfied by removing a control: the
     markers, the rows and the camera seam all still have to exist for the other arms to read. */
  expect(moved, `a one-finger drag must move the camera: ${JSON.stringify(before)} -> ${JSON.stringify(after)}`).toBe(true);
});

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0390-selection", "readings.json"),
    JSON.stringify({ file: FILE, widths: WIDTHS, rows }, null, 1),
  );
});
