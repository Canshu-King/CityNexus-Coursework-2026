/**
 * ORD-0537 -- the 「只看打过」 switch on the map screen, measured where a thumb has to reach it.
 *
 * ORD-0497 put the switch on `/v8/map` and proved what it does. Nobody measured whether a traveller
 * on a narrow screen can actually land a tap on it: at 390 the shell puts a `position: fixed`
 * bottom bar over the last rows of the page (`styles.css` `.v8-mobile-nav`), and the switch sits
 * in the facts column under the map, which is exactly where such a band eats a hit test.
 *
 * Three readings, in the shape the order names:
 *   1. `reach_at_390` -- where the switch is before anything is scrolled (its centre, the bottom
 *      bar's own top edge, and whether the centre falls under that bar), then after
 *      `scrollIntoViewIfNeeded()` the centre `document.elementFromPoint` has to come back with the
 *      switch itself. A null or a foreign element here is the defect, and the reading says which.
 *   2. `two_states` -- off and on, one pass each, reading RENDERED TEXT (the switch's own label, the
 *      sentence beside it) and the marker census (pins, solid, hollow, hidden). The two states must
 *      be visibly different numbers, not just a different `aria-pressed`.
 *   3. the counterexample lives in `ord0537_mutate_r1.py`: the filter condition is masked out, and
 *      the marker census must then stop following the switch -- which is what makes arm 2 a claim
 *      about the switch rather than about the map having pins.
 *
 * No response body is rewritten and nothing is skipped. The trip is the one the setup screen creates
 * and the stamp is made through the traveller's own 打卡 control.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";

const FILE = "e2e/qoder-bac10f40-ord0537-map-toggle-reach.spec.ts";
const PHONE = { width: 390, height: 844 };
const SWITCH = "[data-testid='v8-map-footprint-filter']";
const BAND = "[data-testid='v8-map-footprint-state']";
const NAV = ".v8-mobile-nav";
const LABEL_OFF = "只看打过";
const LABEL_ON = "看全部站";

const rows: Record<string, unknown>[] = [];
const runId = process.env.CITYNEXUS_E2E_RUN_ID || "dev";
const push = (arm: string, v: Record<string, unknown>) => {
  const payload = { file: FILE, arm, run_id: runId, viewport: PHONE, ...v };
  writeFileSync(evidencePath(`qoder-bac10f40-ord0537-${arm}--${runId}.json`), JSON.stringify(payload, null, 1), "utf8");
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

const SEL = { SWITCH, BAND, NAV, PHONE };

/**
 * The whole reading in one page pass: the bottom bar's geometry, the switch's geometry before and
 * after scrolling, the hit at its centre, and the marker census in whatever state the switch is in.
 *
 * `before` is deliberately taken first and from the same evaluate, so the two numbers cannot come
 * from different frames of the same scroll.
 */
const witness = (sel: typeof SEL) => {
  const doc = document;
  const box = (el: Element | null) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      /* `top` is its own field on purpose: the first draft of this file read `navBox.top` off a box
         that only carried `y`, so the "is the centre under the fixed bar" comparison was `number >=
         undefined` -- quietly false forever. The assertion that wants the number failed loudly, which
         is the only reason this was caught. */
      top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom), left: Math.round(r.left),
    };
  };
  const centreOf = (b: { x: number; y: number; w: number; h: number } | null) =>
    b ? { x: b.x + Math.round(b.w / 2), y: b.y + Math.round(b.h / 2) } : null;
  const hitAt = (c: { x: number; y: number } | null) => {
    if (!c) return { hit: "no-box", hit_is_self: false };
    const at = doc.elementFromPoint(c.x, c.y);
    const sw = doc.querySelector(sel.SWITCH);
    return {
      hit: at ? at.tagName.toLowerCase() + "." + String(at.className || "").slice(0, 26) : "none",
      hit_is_self: !!(at && sw && (at === sw || sw.contains(at))),
    };
  };
  const sw = doc.querySelector<HTMLElement>(sel.SWITCH);
  const nav = doc.querySelector<HTMLElement>(sel.NAV);
  const navBox = box(nav);
  const preBox = box(sw);
  const preCentre = centreOf(preBox);
  const pre = { box: preBox, centre: preCentre, ...hitAt(preCentre) };
  const underNavBefore = !!(preBox && navBox && preCentre && preCentre.y >= navBox.top);
  const inViewportBefore = !!(preCentre && preCentre.y >= 0 && preCentre.y < window.innerHeight);
  sw?.scrollIntoView({ block: "center" });
  const postBox = box(sw);
  const postCentre = centreOf(postBox);
  const post = { box: postBox, centre: postCentre, ...hitAt(postCentre) };
  const pins = Array.from(doc.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
  const visible = (el: HTMLElement) => getComputedStyle(el).visibility !== "hidden";
  return {
    scroll_y_before: Math.round(window.scrollY),
    window: { w: window.innerWidth, h: window.innerHeight },
    nav_box: navBox,
    nav_top: navBox ? navBox.top : null,
    switch_label_before: (sw?.innerText ?? "").trim(),
    band_text: (doc.querySelector(sel.BAND)?.textContent ?? "").replace(/\s+/g, " ").trim(),
    aria_pressed: sw?.getAttribute("aria-pressed") ?? null,
    switch_min_height: sw ? Math.round(parseFloat(getComputedStyle(sw).minHeight)) : null,
    pre,
    in_viewport_before: inViewportBefore,
    under_nav_before: underNavBefore,
    post,
    scroll_y_after: Math.round(window.scrollY),
    pins_total: pins.length,
    pins_solid: pins.filter((el) => el.classList.contains("stamped")).length,
    pins_hollow: pins.filter((el) => el.classList.contains("un-stamped")).length,
    pins_claim: pins.filter((el) => el.classList.contains("stamped") || el.classList.contains("un-stamped")).length,
    pins_visible: pins.filter(visible).length,
    pins_hidden: pins.filter((el) => !visible(el)).length,
    date_badges: doc.querySelectorAll(".v8-map-panel .v8-map-stamp-date").length,
    badges_visible: Array.from(doc.querySelectorAll<HTMLElement>(".v8-map-panel .v8-map-stamp-date")).filter(visible).length,
  };
};

async function readMap(page: Page) {
  return page.evaluate(witness, SEL);
}

/** The trip, one real stamp, and the map screen at 390x844 with the switch on the glass. */
async function mapWithOneStamp(page: Page) {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.setViewportSize(PHONE);
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/trips", ["trip-itinerary"]);
  const control = page.locator("[data-testid='stop-checkin']").first();
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(1500);
  await openScreen(page, "/v8/map", ["v8-maplibre-panel"]);
  await expect(page.locator(BAND)).toBeVisible({ timeout: 30000 });
  await expect(page.locator(SWITCH)).toBeVisible({ timeout: 30000 });
  await page.waitForFunction(
    () => document.querySelectorAll(".v8-map-panel .v9-map-marker").length > 1,
    null,
    { timeout: 30000 },
  );
  await page.waitForTimeout(600);
}

test("ORD-0537 zh 390x844 arm=reach the switch takes its own tap once it is on screen", async ({ page }) => {
  await mapWithOneStamp(page);
  const w = await readMap(page);
  expect(w.nav_box, "the fixed bottom bar exists at this width").not.toBeNull();
  expect(w.pre.box, "the switch has a box").not.toBeNull();
  expect(w.switch_min_height, "the switch keeps the 44 px touch floor").toBeGreaterThanOrEqual(44);
  expect(w.post.hit_is_self, `the centre hit must land on the switch itself, got "${w.post.hit}"`).toBe(true);
  expect(w.post.centre!.y, "and that centre is inside the viewport").toBeLessThan(w.window.h);
  expect(w.post.centre!.y, "and above the fixed bottom bar's own top edge").toBeLessThan(w.nav_top!);
  push("reach", {
    nav_top: w.nav_top,
    nav_box: w.nav_box,
    window: w.window,
    before: { ...w.pre, in_viewport: w.in_viewport_before, under_nav: w.under_nav_before },
    after: w.post,
    scroll_y_before: w.scroll_y_before,
    scroll_y_after: w.scroll_y_after,
    switch_min_height: w.switch_min_height,
    switch_label: w.switch_label_before,
    aria_pressed: w.aria_pressed,
  });
  await page.screenshot({ path: evidencePath(`qoder-bac10f40-ord0537-reach--${runId}.png`) });
});

test("ORD-0537 zh 390x844 arm=two_states the census and the words both move", async ({ page }) => {
  await mapWithOneStamp(page);
  const off = await readMap(page);
  expect(off.aria_pressed, "the switch starts off").toBe("false");
  expect(off.switch_label_before, "and says what the press will do").toBe(LABEL_OFF);
  expect(off.pins_hidden, "off hides nothing").toBe(0);
  expect(off.pins_solid, "one stop is stamped").toBe(1);
  expect(off.pins_hollow, "the rest are hollow").toBeGreaterThan(0);
  await page.screenshot({ path: evidencePath(`qoder-bac10f40-ord0537-state_off--${runId}.png`) });

  const sw = page.locator(SWITCH);
  await sw.scrollIntoViewIfNeeded();
  await sw.click();
  await page.waitForTimeout(900);
  const on = await readMap(page);
  expect(on.aria_pressed, "pressed, the switch carries its own state").toBe("true");
  expect(on.switch_label_before, "and says a different sentence").toBe(LABEL_ON);
  expect(on.pins_hidden, "every unstamped pin is now hidden").toBe(on.pins_claim - on.pins_solid);
  expect(on.pins_hidden, "which is a number the off state did not have").toBeGreaterThan(off.pins_hidden);
  expect(on.pins_visible, "the visible count moved").not.toBe(off.pins_visible);
  expect(on.pins_solid, "the stamped pin is never hidden by its own filter").toBe(off.pins_solid);
  expect(on.date_badges, "and its date badge stays").toBe(off.date_badges);
  expect(on.band_text, "the sentence beside the map is the read, unchanged by the switch").toBe(off.band_text);
  expect(on.post.hit_is_self, "the switch still takes its own tap in the on state").toBe(true);
  await page.screenshot({ path: evidencePath(`qoder-bac10f40-ord0537-state_on--${runId}.png`) });

  await sw.click();
  await page.waitForTimeout(900);
  const back = await readMap(page);
  expect(back.pins_hidden, "pressing again puts them all back").toBe(0);
  expect(back.switch_label_before, "and the label comes back").toBe(LABEL_OFF);
  push("two_states", {
    off: {
      label: off.switch_label_before, aria_pressed: off.aria_pressed, band_text: off.band_text,
      pins_total: off.pins_total, pins_claim: off.pins_claim, pins_solid: off.pins_solid,
      pins_hollow: off.pins_hollow, pins_visible: off.pins_visible, pins_hidden: off.pins_hidden,
      date_badges: off.date_badges, badges_visible: off.badges_visible,
    },
    on: {
      label: on.switch_label_before, aria_pressed: on.aria_pressed, band_text: on.band_text,
      pins_total: on.pins_total, pins_claim: on.pins_claim, pins_solid: on.pins_solid,
      pins_hollow: on.pins_hollow, pins_visible: on.pins_visible, pins_hidden: on.pins_hidden,
      date_badges: on.date_badges, badges_visible: on.badges_visible,
    },
    back: { label: back.switch_label_before, pins_hidden: back.pins_hidden },
    states_differ: off.pins_hidden !== on.pins_hidden && off.switch_label_before !== on.switch_label_before,
  });
});

test("ORD-0537 zh 390x844 arm=under_the_bar the fixed band wins where it covers, and the page can move it out", async ({ page }) => {
  /* The other half of "is it reachable": park the switch's centre INSIDE the fixed bottom bar's band
     and hit-test it there. The bar is `position: fixed; z-index: 10`, so it wins -- that is the
     shell's rule, not a defect of this control. What the traveller needs to be true is the second
     half: there is a scroll position that lifts the centre above the band and lands the tap on the
     switch, which is what the first arm measures. */
  await mapWithOneStamp(page);
  const parked = await page.evaluate((sel) => {
    const doc = document;
    const sw = doc.querySelector<HTMLElement>(sel.SWITCH);
    const nav = doc.querySelector<HTMLElement>(sel.NAV);
    if (!sw || !nav) return null;
    const navTop = Math.round(nav.getBoundingClientRect().top);
    const r = sw.getBoundingClientRect();
    window.scrollBy(0, r.y - (navTop + 20));
    const b = sw.getBoundingClientRect();
    const c = { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
    const at = doc.elementFromPoint(c.x, c.y);
    return {
      nav_top: navTop,
      switch_box: { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) },
      centre: c,
      centre_inside_band: c.y >= navTop && c.y < window.innerHeight,
      hit: at ? at.tagName.toLowerCase() + "." + String(at.className || "").slice(0, 26) : "none",
      hit_is_self: !!(at && (at === sw || sw.contains(at))),
      hit_is_nav: !!(at && nav.contains(at)),
    };
  }, SEL);
  expect(parked, "both elements exist at this width").not.toBeNull();
  expect(parked!.centre_inside_band, "the centre really is inside the fixed band").toBe(true);
  expect(parked!.hit_is_nav, "and there the band owns the pointer").toBe(true);
  expect(parked!.hit_is_self, "so the switch is not hittable at that scroll position").toBe(false);
  /* The picture has to be taken in the state it is supposed to witness: the first draft screenshotted
     at the end of the arm, which is after the lift, so it showed the reachable position while the
     numbers in the same file described the covered one. */
  await page.screenshot({ path: evidencePath(`qoder-bac10f40-ord0537-parked_under_bar--${runId}.png`) });

  const lifted = await readMap(page);
  await page.locator(SWITCH).scrollIntoViewIfNeeded();
  const again = await readMap(page);
  expect(again.post.hit_is_self, "one scroll later the same control takes its own tap").toBe(true);
  await page.screenshot({ path: evidencePath(`qoder-bac10f40-ord0537-lifted_clear--${runId}.png`) });
  push("under_the_bar", {
    parked,
    before_lift: { centre: parked!.centre, hit: parked!.hit },
    lifted: { centre: again.post.centre, hit: again.post.hit, hit_is_self: again.post.hit_is_self, nav_top: again.nav_top },
    scroll_y_after: again.scroll_y_after,
  });
});

test("ORD-0537 zh the arms kept their own files", async () => {
  expect(rows.length, "one file per arm, written at the moment that arm reads").toBeGreaterThanOrEqual(2);
  writeFileSync(
    evidencePath(`qoder-bac10f40-ord0537-index--${runId}.json`),
    JSON.stringify({ file: FILE, run_id: runId, viewport: PHONE, arms: rows.map((r) => r.arm) }, null, 1),
    "utf8",
  );
});
