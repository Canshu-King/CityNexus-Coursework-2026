import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0453 — the stop list's third state on a small screen.
 *
 * Shape this gate is written against (measured, not assumed — see the two accepted row-count arms that
 * forced it): the pressed-down list is a HEIGHT CLIP, not a deletion. Every located stop keeps its row
 * in the document, so the third state is admitted by the fade, the named total and the capture control,
 * not by hiding rows from the traveller's keyboard or from `qoder-map-ord0390-selection-both-ways`.
 *
 * Every claim is a hit or a count read off the running screen, in both states:
 *   pressed    — the box is shorter than its own content (`scrollHeight > clientHeight`), the first
 *                `COLLAPSED_STOP_ROWS` chips are inside the visible band and the rest are outside it,
 *                the capture control names the whole list, and a centre hit-test on it returns the
 *                control itself (`elementFromPoint`, not a presence check); the mask over the cut is
 *                paint with `pointer-events: none`, so it can never be the thing that answers.
 *   expanded   — the same box now contains its whole content, every chip is in view, the same control
 *                offers the way back and is still centre-hittable.
 *   rows       — the DOM row count is identical in both states and equals the number of pins, so
 *                pressing cannot make a pinned stop unselectable.
 *   glass      — a tap on the map and a drag across it both leave the list pressed down. The order
 *                offered "tap the map to expand"; I took it back out after measuring what it cost
 *                (see `glass-tap-does-not-open`), so the arm now pins the property I actually shipped:
 *                the capture control is the only opener.
 *   camera     — the map panel's own box is identical between the two states, because the bottom
 *                overlay band is what `fitBounds` measures and this control must not move it.
 * Copy is read verbatim in both language arms; evidence names are ASCII, one file per moment.
 */

const FILE = "e2e/qoder-map-ord0453-list-third-state.spec.ts";
const WIDTHS = [320, 360, 390, 414];
const COLLAPSED_ROWS = 4;
const TOTAL_STOPS = 9;

async function enterMap(page: Page): Promise<void> {
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

const readList = (page: Page) =>
  page.evaluate(() => {
    const wrap = document.querySelector<HTMLElement>("[data-testid='map-stops']");
    const box = document.querySelector<HTMLElement>("[data-testid='map-stops-box']");
    const cap = document.querySelector<HTMLElement>("[data-testid='map-stops-capture']");
    const mask = document.querySelector<HTMLElement>(".v8-map-stops-mask");
    const rows = Array.from(document.querySelectorAll<HTMLElement>("[data-testid^='map-stop-']"));
    if (!wrap || !box || !cap) return null;
    const r = cap.getBoundingClientRect();
    /* The centre hit has to be taken where the eye can reach it: this control sits under the map on a
       phone, so a raw `elementFromPoint` on a below-the-fold coordinate returns null and a red here
       would be my instrument, not the product — the ORD-0390 lesson. The scroll position is recorded
       instead of hidden, so the reading still says whether the control was on the first screen. */
    const belowFold = r.bottom > window.innerHeight || r.top < 0;
    cap.scrollIntoView({ block: "center" });
    const r2 = cap.getBoundingClientRect();
    const top = document.elementFromPoint(r2.x + r2.width / 2, r2.y + r2.height / 2);
    const sy = window.scrollY;
    const bb = box.getBoundingClientRect();
    /* "in view" is the clip's own visible band, read off the laid-out chip box: a chip is in view when
       both its edges fall inside the box's content area. This is the quantity the traveller's eye has,
       and it is what separates a height clip from a deletion. */
    const inView = rows.filter((el) => {
      const q = el.getBoundingClientRect();
      return q.top >= bb.top - 1 && q.bottom <= bb.bottom + 1;
    }).length;
    const panel = document.querySelector<HTMLElement>(".v8-map-panel");
    const pb = panel?.getBoundingClientRect();
    const markers = document.querySelectorAll(".v9-map-marker:not(.user)").length;
    return {
      expanded: wrap.getAttribute("data-stops-expanded"),
      pressed: wrap.getAttribute("data-stops-pressed"),
      ariaExpanded: cap.getAttribute("aria-expanded"),
      rowCount: rows.length,
      markers,
      rowsInBefore: inView,
      boxClientH: Math.round(bb.height),
      boxScrollH: Math.round(box.scrollHeight),
      pressHeight: box.getAttribute("data-press-height"),
      rowTexts: rows.map((el) => (el.textContent ?? "").trim()),
      captureText: (cap.textContent ?? "").trim(),
      captureBox: { x: Math.round(r2.x), y: Math.round(r2.y), w: Math.round(r2.width), h: Math.round(r2.height) },
      captureBelowFoldBeforeScrolling: belowFold,
      documentYOfCapture: Math.round(r.y + sy),
      scrollY: Math.round(sy),
      pageScrollHeight: document.documentElement.scrollHeight,
      viewportH: window.innerHeight,
      captureHitsItself: !!top && (top === cap || cap.contains(top)),
      hitTarget: top
        ? (top.getAttribute("data-testid") ?? top.className?.toString().slice(0, 40) ?? top.tagName)
        : "none",
      maskPointerEvents: mask ? getComputedStyle(mask).pointerEvents : "no-mask",
      maskVisible: !!mask && getComputedStyle(mask).display !== "none",
      /* Document-space, so the two states are compared without a scroll position sneaking in. What the
         camera was promised is the panel's own box and its bottom overlay band, not where the page
         happened to be scrolled. */
      panelBox: pb ? { x: Math.round(pb.x), y: Math.round(pb.y + sy), w: Math.round(pb.width), h: Math.round(pb.height) } : null,
      docScrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
    };
  });

const push = (arm: string, v: Record<string, unknown>) => {
  writeFileSync(evidencePath("ord0453-list", `reading_${arm}.json`),
    JSON.stringify({ file: FILE, arm, ...v }, null, 1));
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

test("pressed-then-expanded — the capture control answers in both states", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const pressed = await readList(page);
  expect(pressed, "the stop list must exist").not.toBeNull();
  const shotPressed = evidencePath("ord0453-list", "list_pressed.png");
  await page.getByTestId("map-stops").screenshot({ path: shotPressed });

  await page.getByTestId("map-stops-capture").click();
  await page.waitForTimeout(500);
  const expanded = await readList(page);
  const shotExpanded = evidencePath("ord0453-list", "list_expanded.png");
  await page.getByTestId("map-stops").screenshot({ path: shotExpanded });

  push("pressed-then-expanded", { pressed, expanded, shotPressed, shotExpanded });

  expect(pressed!.expanded, "the list starts pressed down").toBe("false");
  expect(pressed!.pressed, "and says so in the DOM").toBe("true");
  expect(pressed!.rowCount, "every located stop keeps its row in the document").toBe(TOTAL_STOPS);
  expect(pressed!.rowCount, "rows are not borrowed from the pins").toBe(pressed!.markers);
  expect(pressed!.boxScrollH, "the box holds more than it shows").toBeGreaterThan(pressed!.boxClientH);
  expect(pressed!.rowsInBefore, "at least the first four chips stay whole inside the clip")
    .toBeGreaterThanOrEqual(COLLAPSED_ROWS);
  expect(pressed!.rowsInBefore, "and the clip really leaves rows outside the band")
    .toBeLessThan(pressed!.rowCount);
  expect(pressed!.captureText, "the control must name the whole list").toBe("Show all 9 places");
  expect(pressed!.captureHitsItself, "a centre hit on the control must return the control").toBe(true);
  push("capture-visibility-pressed", { documentYOfCapture: pressed!.documentYOfCapture,
    viewportH: pressed!.viewportH, belowFold: pressed!.captureBelowFoldBeforeScrolling,
    pageScrollHeightPressed: pressed!.pageScrollHeight, pageScrollHeightExpanded: expanded!.pageScrollHeight,
    boxClientHPressed: pressed!.boxClientH, boxScrollHPressed: pressed!.boxScrollH,
    pressHeightAttr: pressed!.pressHeight });
  expect(pressed!.maskPointerEvents, "the mask is paint, not a target").toBe("none");
  expect(pressed!.maskVisible, "and it is visible while pressed").toBe(true);

  expect(expanded!.expanded, "pressing it shows the whole list").toBe("true");
  expect(expanded!.pressed, "and the DOM stops claiming a clip").toBe("false");
  expect(expanded!.rowCount, "the same rows, not more of them").toBe(pressed!.rowCount);
  expect(expanded!.boxScrollH, "open, the box shows all of its content")
    .toBeLessThanOrEqual(expanded!.boxClientH + 1);
  expect(expanded!.rowsInBefore, "every chip is in view").toBe(TOTAL_STOPS);
  expect(expanded!.ariaExpanded).toBe("true");
  expect(expanded!.captureText, "the same control must now offer the way back").toBe("Collapse list");
  expect(expanded!.captureHitsItself, "and still answer a centre hit").toBe(true);
  expect(expanded!.maskVisible, "the fade goes away when nothing is clipped").toBe(false);
  expect(expanded!.panelBox, "the map panel keeps its box across both states").toEqual(pressed!.panelBox);
  expect(expanded!.docScrollW, "no horizontal page scroll").toBeLessThanOrEqual(expanded!.innerW + 1);
});

test("glass-tap-does-not-open — the capture control is the only opener", async ({ page }) => {
  /* Measured cost of the alternative. Binding maplibre's `click` to the expand setter made the
     geometry arm of `e2e/qoder-ord0417-tap-names-the-stop.spec.ts` read "26 controls -> 31": that
     arm taps the glass on purpose, and the tap changed what it was counting in the middle of its own
     measurement (regression run `ord0453reg1`, 8 failures on that spec). A tap on the map keeps
     meaning only what the map says it means -- selecting a pin -- so this arm pins the opposite
     property: the glass must never open the list, and neither must a drag. */
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const before = await readList(page);

  /* `readList` scrolls the capture control into view, and `page.mouse.click` takes raw viewport
     coordinates — clicking where the canvas used to be would hit whatever is on screen after that
     scroll. Go back to the top and prove the point is inside the viewport before tapping it. */
  const canvas = page.locator(".v8-maplibre-canvas");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(300);
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  const tapPoint = { x: Math.round(box!.x + box!.width / 2), y: Math.round(box!.y + box!.height / 4) };
  expect(tapPoint.y > 0 && tapPoint.y < 844, `the tap point must be on screen: ${JSON.stringify(tapPoint)}`)
    .toBe(true);
  await page.mouse.click(tapPoint.x, tapPoint.y);
  await page.waitForTimeout(600);
  const afterTap = await readList(page);

  await page.reload({ waitUntil: "networkidle" });
  await enterMap(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(300);
  const b2 = await canvas.boundingBox();
  expect(b2).not.toBeNull();
  await page.mouse.move(b2!.x + b2!.width / 2, b2!.y + b2!.height / 2);
  await page.mouse.down();
  await page.mouse.move(b2!.x + b2!.width / 2 - 70, b2!.y + b2!.height / 2 - 40, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(700);
  const afterDrag = await readList(page);
  const shot = evidencePath("ord0453-list", "after_glass_tap_stays_collapsed.png");
  await page.getByTestId("map-stops").screenshot({ path: shot });

  push("glass-tap-and-drag", { beforeExpanded: before!.expanded, tapPoint,
    afterTapExpanded: afterTap!.expanded, afterDragExpanded: afterDrag!.expanded,
    afterTapRowsIn: afterTap!.rowsInBefore, afterTapScrollY: afterTap!.scrollY, shot });
  expect(before!.expanded).toBe("false");
  expect(afterTap!.expanded, "a tap on the glass must not open the list").toBe("false");
  expect(afterTap!.rowsInBefore, "and must not change how many chips are in view")
    .toBe(before!.rowsInBefore);
  expect(afterDrag!.expanded, "a drag is not a tap and must not open it either").toBe("false");
});

for (const width of WIDTHS) {
  test(`reach-and-hit @${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await enterMap(page);
    const pressed = await readList(page);
    await page.getByTestId("map-stops-capture").click();
    await page.waitForTimeout(500);
    const expanded = await readList(page);
    const shot = evidencePath("ord0453-list", `reach_${width}.png`);
    await page.getByTestId("map-stops").screenshot({ path: shot });
    push(`reach-${width}`, { width, pressed, expanded, shot });
    expect(pressed!.captureHitsItself, `pressed centre hit @${width}`).toBe(true);
    expect(expanded!.captureHitsItself, `expanded centre hit @${width}`).toBe(true);
    expect(pressed!.captureBox.w * pressed!.captureBox.h, `the control keeps a real hit box @${width}`)
      .toBeGreaterThanOrEqual(32 * 32);
    expect(pressed!.docScrollW, `no horizontal page scroll @${width}`).toBeLessThanOrEqual(pressed!.innerW + 1);
    expect(pressed!.rowCount, `every stop keeps its row @${width}`).toBe(pressed!.markers);
    expect(pressed!.boxScrollH, `the list is really clipped @${width}`).toBeGreaterThan(pressed!.boxClientH);
    expect(pressed!.rowsInBefore, `four chips stay whole @${width}`).toBe(COLLAPSED_ROWS);
    expect(expanded!.rowsInBefore, `and all of them open @${width}`).toBe(TOTAL_STOPS);
    expect(expanded!.panelBox, `the map panel does not move @${width}`).toEqual(pressed!.panelBox);
  });
}

for (const arm of [{ locale: "zh", toggle: true }, { locale: "en", toggle: false }] as const) {
  test(`copy-${arm.locale} — the third state reads verbatim`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page);
    if (arm.toggle) {
      await page.getByTestId("topbar-language").click();
      await page.waitForTimeout(1_200);
    }
    const pressed = await readList(page);
    await page.getByTestId("map-stops-capture").click();
    await page.waitForTimeout(500);
    const expanded = await readList(page);
    const shot = evidencePath("ord0453-list", `copy_${arm.locale}.png`);
    await page.getByTestId("map-stops").screenshot({ path: shot });
    push(`copy-${arm.locale}`, { locale: arm.locale, pressed, expanded, shot });
    if (arm.locale === "zh") {
      expect(pressed!.captureText).toBe("查看全部 9 个地点");
      expect(expanded!.captureText).toBe("收起清单");
    } else {
      expect(pressed!.captureText).toBe("Show all 9 places");
      expect(expanded!.captureText).toBe("Collapse list");
    }
  });
}

test("rows-still-select — expanding does not break the shared selection", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  /* While pressed the fifth chip is still in the document (its row is counted, not deleted); once the
     list is open it must select the same card as a chip that was never clipped. */
  const pressed = await readList(page);
  await page.getByTestId("map-stops-capture").click();
  await page.waitForTimeout(400);
  const card = page.getByTestId("map-selected-stop");
  const before = (await card.innerText()).trim();
  const fifth = page.locator("[data-testid^='map-stop-']").nth(4);
  const name = (await fifth.innerText()).trim();
  await fifth.click();
  await page.waitForTimeout(900);
  const after = (await card.innerText()).trim();
  const list = await readList(page);
  push("rows-still-select", { before, after, picked: name,
    rowCountPressed: pressed!.rowCount, rowCountAfter: list!.rowCount,
    rowsInPressed: pressed!.rowsInBefore, rowsInAfterOpen: list!.rowsInBefore });
  expect(pressed!.rowCount, "the fifth chip is in the document while pressed").toBeGreaterThan(COLLAPSED_ROWS);
  expect(after, "a row below the clip must select the same card as one above it").toContain(name);
  expect(after).not.toBe(before);
});
