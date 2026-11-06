import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0417 (hub slice) — the traveller's first screen carries a real map with real pins, and a tap on
 * one of them answered nothing.
 *
 * The commander measured it on the live line at 13:36 (`_mapselect_r1/map_select_r1.json`: six markers,
 * a tap on marker 1 at (192, 736), `changed_markers: []`, `visible_name_leaves_delta: 0`, verdict
 * MAP_CLICK_CHANGED_NOTHING_ON_SCREEN) while the same tap on `/v8/map` names the stop on the glass.
 * The cause was in the code, not in the browser: `MapPanel` binds the pin to
 * `selectRef.current?.(point.id ?? null)` (`frontend/src/components/v8/MapPanel.tsx`), and the hub's
 * call site passed `data`, `option` and `onNoLocationChange` only
 * (`frontend/src/app/V8Experience.tsx`) — so the optional call discarded the tap in silence while
 * the marker still advertised `role="button"`, `tabindex="0"` and an `aria-pressed` state.  A control
 * that looks focusable, answers a hit-test and then does nothing is the same defect ORD-0417 was opened
 * for, one screen over.  Nothing anywhere declared the hub map an overview: no comment at the call
 * site, and no spec in `frontend/e2e/` asserts that hub pins are inert.
 *
 * So the hub gets the same linkage the map page has, and this file measures it:
 *   hub-tap-named-{zh,en}    the hub panel's own painted text contains the tapped stop's name;
 *   hub-not-aria-{zh,en}     it reaches the eye as painted text, through a visible box, not an
 *                            attribute a reader recites;
 *   hub-unpicked-{zh,en}     before a tap the hub glass names no stop, so no arm can borrow a label;
 *   hub-pressed-{zh,en}      the pin that was tapped reports its own state flip, which is the proof
 *                            the tap reached the pin and not a neighbour sharing its pixel;
 *   hub-geometry-{w}x{h}-{zh,en}
 *                            the same 768 px reachability census as the map-page file, taken twice on
 *                            one scroll offset: the linkage may not cost the hub a reachable control,
 *                            may not grow the band past the 16 px `fitBounds` already paid for, may not
 *                            add a control, and may not clip the name.
 *
 * No response body is rewritten, nothing is mocked, no arm is skipped. Assertions read rendered text,
 * never a data attribute.
 */

const FILE = "e2e/qoder-ord0417-hub-tap-names-the-stop.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* Copied from the map-page file on purpose: same floor, same shapes, same safety gap, same arms. */
const HIT_FLOOR = 768;
const SHAPES: Array<[number, number]> = [[390, 844], [320, 653]];
const ARMS = [
  { locale: "zh", lead: "图上选中的这一站" },
  { locale: "en", lead: "Selected on the map" },
] as const;
const SAFETY_GAP_PX = 16;

async function enterHub(page: Page, arm: (typeof ARMS)[number] | null = null): Promise<void> {
  /* Language before the app's first script, exactly as the map-page file does it, and the journey id
     the seed persisted is never cleared away. */
  if (arm) {
    await page.addInitScript(
      (loc) => {
        try {
          localStorage.setItem("citynexus.locale", loc);
        } catch {}
      },
      arm.locale === "zh" ? "zh-CN" : "en",
    );
  }
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

async function pickHittablePin(page: Page) {
  const census = await markerCensus(page);
  expect(
    census.pins.length,
    node("A0", "the seeded journey put no pickable stop on the map"),
  ).toBeGreaterThan(1);
  const candidates = census.pins.filter((p) => p.lands_on_pin && p.aria.length > 0);
  expect(
    candidates.length,
    node(
      "A0",
      `no pin answers a hit-test at its own centre, so a tap cannot be attributed to a stop: ${JSON.stringify(
        census.pins.map((p) => ({ painted: p.painted, centre: p.centre, lands: p.lands_on_pin })),
      )}`,
    ),
  ).toBeGreaterThan(0);
  return { census, target: candidates[Math.min(1, candidates.length - 1)] };
}

/**
 * A pin to tap, plus the honesty note about what tapping it can prove.
 *
 * `pickHittablePin` demands a pin that answers a hit-test at its own centre. At 320x653 the hub's whole
 * journey is fitted into a dot: nine markers share roughly one pixel, so the topmost one takes every tap
 * and no marker can answer for itself. Refusing there reported a product red for the wrong reason -- the
 * camera is degenerate, not the linkage. This picker takes the marker that is actually under the finger,
 * measures how far apart the pins really are, and says which claim the shape cannot carry.
 */
async function pickPinForCamera(page: Page) {
  const census = await markerCensus(page);
  const selfLanding = census.pins.filter((pin) => pin.lands_on_pin);
  const onMarker = census.pins.filter((pin) => pin.lands_on_marker);
  const xs = census.pins.map((pin) => pin.centre[0]);
  const ys = census.pins.map((pin) => pin.centre[1]);
  const spread = census.pins.length
    ? { x: Math.max(...xs) - Math.min(...xs), y: Math.max(...ys) - Math.min(...ys) }
    : null;
  return {
    census,
    target: selfLanding[0] ?? onMarker[0] ?? null,
    attributable_to_one_stop: selfLanding.length > 0,
    pins_answerable: onMarker.length,
    pin_spread_px: spread,
  };
}

/**
 * The markers, as a traveller's finger meets them: painted number, the name hidden in `aria-label`,
 * the real centre, whether a hit at that centre lands on the pin, and where the pin sits against the
 * bottom overlay band.
 */
async function markerCensus(page: Page) {
  return page.evaluate(() => {
    const pins = Array.from(
      document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"),
    ).filter((el) => el.getAttribute("role") === "button");
    const band = document.querySelector<HTMLElement>(".v8-map-overlay-stack");
    const bb = band?.getBoundingClientRect();
    return {
      lang: document.documentElement.lang || null,
      band_box: bb
        ? [Math.round(bb.x), Math.round(bb.y), Math.round(bb.width), Math.round(bb.height)]
        : null,
      scroll_y: Math.round(window.scrollY),
      inner_height: window.innerHeight,
      pins: pins.map((el) => {
        const r = el.getBoundingClientRect();
        const cx = Math.round(r.x + r.width / 2);
        const cy = Math.round(r.y + r.height / 2);
        const top = document.elementFromPoint(cx, cy);
        const overlap = (bb &&
          Math.max(0, Math.min(bb.right, r.right) - Math.max(bb.left, r.left)) *
            Math.max(0, Math.min(bb.bottom, r.bottom) - Math.max(bb.top, r.top))) || 0;
        return {
          painted: (el.textContent ?? "").trim(),
          aria: el.getAttribute("aria-label") ?? "",
          title_attr: el.getAttribute("title"),
          pressed: el.getAttribute("aria-pressed"),
          class: el.className,
          centre: [cx, cy] as [number, number],
          box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
          lands_on_pin: !!top && (top === el || el.contains(top)),
          /* Whether the tap lands on *any* marker. At 320x653 the whole journey fits into a dot and the
             nine pins share one pixel, so only the topmost one can answer for itself -- which is a fact
             about the camera, and the difference between "this arm cannot attribute a tap to a stop" and
             "the hub has no tappable pin". */
          lands_on_marker: !!top && !!top.closest(".v9-map-marker"),
          /* When a pin is covered, say by what. Without this the reading can only report "no pin is
             tappable", which reads like a broken hub even when it is a notice sitting on top of it. */
          covered_by: !top || (top === el || el.contains(top))
            ? null
            : [top.tagName.toLowerCase(),
               (top as HTMLElement).dataset?.testid ?? "",
               ((top as HTMLElement).className ?? "").toString().trim().split(/\s+/).slice(0, 2).join(".")]
              .filter((part) => part !== "").join("#"),
          covered_px2_by_band: Math.round(overlap),
        };
      }),
    };
  });
}

/**
 * Lane N3's control census, verbatim in rule if not in text: the same selector set, `centre.y <= 768`,
 * `elementFromPoint` at the control's own centre, and identity by (name, n-th of that name) because
 * most controls on these screens carry no test id.
 */
async function controlCensus(page: Page) {
  return page.evaluate((floor: number) => {
    const sel =
      "button, a[href], input, select, textarea, [role=button], [data-testid=pretrip-empty-action]";
    const seen: Record<string, number> = {};
    return Array.from(document.querySelectorAll(sel))
      .map((el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return null;
        const cx = Math.round(r.left + r.width / 2);
        const cy = Math.round(r.top + r.height / 2);
        const hit = document.elementFromPoint(cx, cy);
        const text = (el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 60);
        const testid = el.getAttribute("data-testid");
        const name =
          text || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "(no label)";
        const bare = testid ?? name;
        seen[bare] = (seen[bare] ?? 0) + 1;
        return {
          key: testid ?? `${name}#${seen[bare]}`,
          name,
          centre: [cx, cy] as [number, number],
          in_viewport: cy >= 0 && cy <= window.innerHeight,
          centre_above_floor: cy <= floor,
          hit_test: !!hit && (hit === el || el.contains(hit) || hit.contains(el)),
        };
      })
      .filter((x): x is NonNullable<typeof x> => x !== null);
  }, HIT_FLOOR);
}

/**
 * The one cell this order is about, judged as something an eye can read rather than as a node that
 * exists: a real box, laid out, not hidden, not faded, not clipped away, not the screen-reader-only
 * utility class -- and carrying the name as *painted text*.
 */
async function readGlassName(page: Page) {
  return page.evaluate(() => {
    const boxOf = (el: HTMLElement | null) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
    };
    const panel = document.querySelector<HTMLElement>(".v8-map-panel");
    const el = panel?.querySelector<HTMLElement>(".v8-map-selected-stop") ?? null;
    if (!el) {
      return { present: false as const, panel_text: (panel?.innerText ?? "").replace(/\s+/g, " ").trim() };
    }
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const cap = el.closest<HTMLElement>(".v8-map-caption");
    const cb = cap?.getBoundingClientRect();
    return {
      present: true as const,
      painted: (el.innerText ?? "").replace(/\s+/g, " ").trim(),
      text_content: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      display: cs.display,
      visibility: cs.visibility,
      opacity: Number(cs.opacity),
      clip: cs.clipPath,
      sr_only: el.classList.contains("v8-sr-only"),
      in_panel: !!el.closest(".v8-map-panel"),
      /* the sentence is painted inside the caption box the camera already accounts for; if it ever
         escapes that box it is over the canvas, and a traveller reads it off the map. */
      caption_box: cb ? [Math.round(cb.x), Math.round(cb.y), Math.round(cb.width), Math.round(cb.height)] : null,
      inside_caption: !!cb && r.left >= cb.left - 1 && r.right <= cb.right + 1 && r.top >= cb.top - 1 && r.bottom <= cb.bottom + 1,
      panel_text: (panel?.innerText ?? "").replace(/\s+/g, " ").trim(),
    };
  });
}


const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

for (const arm of ARMS) {
  test(`hub-tap-named-${arm.locale} — a tap on a hub pin names that stop on the hub's own glass`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterHub(page, arm);
    const { census, target } = await pickHittablePin(page);
    expect(
      target,
      node("H1", `${arm.locale}: the hub put no pin on the map that answers a hit-test at its own centre: ${JSON.stringify(census.pins.map((p) => [p.painted, p.centre, p.lands_on_pin]))}`),
    ).not.toBeNull();
    await page.mouse.click(target!.centre[0], target!.centre[1]);
    await page.waitForTimeout(1_800);
    const glass = await readGlassName(page);
    const shot = evidencePath("ord0417-hub", `${RUN}_tap_named_${arm.locale}.png`);
    await page.screenshot({ path: shot });
    push(`hub-tap-named-${arm.locale}`, {
      tapped: { painted: target!.painted, aria: target!.aria, centre: target!.centre },
      glass,
      names_tapped_stop: glass.present && !!target!.aria && String(glass.painted ?? "").includes(target!.aria),
      lead_authored: arm.lead,
      lead_painted: glass.present && String(glass.painted ?? "").includes(arm.lead),
      shot,
    });
    expect(
      glass.present,
      node("H1", `${arm.locale}: after tapping hub pin ${target!.painted} the hub panel painted no name at all (panel text: ${JSON.stringify(glass.panel_text.slice(0, 160))})`),
    ).toBe(true);
    expect(
      glass.panel_text,
      node("H1", `${arm.locale}: the hub glass says ${JSON.stringify(glass.panel_text.slice(0, 160))} but never names the tapped stop ${JSON.stringify(target!.aria)}`),
    ).toContain(target!.aria);
    expect(
      glass.panel_text,
      node("H1", `${arm.locale}: the sentence on the hub is not the dictionary's — expected the lead-in ${JSON.stringify(arm.lead)} in ${JSON.stringify(glass.panel_text.slice(0, 160))}`),
    ).toContain(arm.lead);
  });
}

for (const arm of ARMS) {
  test(`hub-not-aria-${arm.locale} — the hub names the stop where the eye can see it`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterHub(page, arm);
    const { target } = await pickHittablePin(page);
    expect(target, node("H2", `${arm.locale}: no hittable hub pin to tap`)).not.toBeNull();
    await page.mouse.click(target!.centre[0], target!.centre[1]);
    await page.waitForTimeout(1_800);
    const cell = await page.evaluate((name: string) => {
      const panel = document.querySelector<HTMLElement>(".v8-map-panel");
      const hits = Array.from(panel?.querySelectorAll<HTMLElement>("*") ?? []).filter(
        (el) => (el.innerText ?? "").includes(name) && el.children.length === 0,
      );
      const el = hits[0] ?? null;
      if (!el) return { found: false as const, leaves: hits.length };
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const cls = el.className ?? "";
      return {
        found: true as const,
        leaves: hits.length,
        tag: el.tagName.toLowerCase(),
        box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        display: cs.display,
        visibility: cs.visibility,
        opacity: cs.opacity,
        screen_reader_only: String(cls).includes("v8-sr-only"),
        inside_panel: !!el.closest(".v8-map-panel"),
        text: (el.innerText ?? "").replace(/\s+/g, " ").trim().slice(0, 120),
      };
    }, target!.aria);
    const shot = evidencePath("ord0417-hub", `${RUN}_not_aria_${arm.locale}.png`);
    await page.locator(".v8-map-panel").first().screenshot({ path: shot });
    push(`hub-not-aria-${arm.locale}`, { cell, shot });
    expect(cell.found, node("H2", `${arm.locale}: the tapped stop's name appears nowhere painted on the hub panel — ${JSON.stringify(cell)}`)).toBe(true);
    expect(cell.screen_reader_only, node("H2", `${arm.locale}: the name is only in a screen-reader layer: ${JSON.stringify(cell)}`)).toBe(false);
    expect([cell.display, cell.visibility, Number(cell.opacity)], node("H2", `${arm.locale}: the naming element is not visible: ${JSON.stringify(cell)}`)).toEqual(["inline", "visible", 1]);
    expect(cell.box![2], node("H2", `${arm.locale}: zero-width naming element: ${JSON.stringify(cell.box)}`)).toBeGreaterThan(0);
    expect(cell.inside_panel, node("H2", `${arm.locale}: the name is painted outside the map panel: ${JSON.stringify(cell)}`)).toBe(true);
  });
}

for (const arm of ARMS) {
  test(`hub-unpicked-${arm.locale} — before a tap the hub glass names no stop`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterHub(page, arm);
    const before = await readGlassName(page);
    const pressed = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker")).map((el) =>
        el.getAttribute("aria-pressed"),
      ),
    );
    push(`hub-unpicked-${arm.locale}`, { glass: before, aria_pressed_before: pressed });
    expect(
      before.present,
      node("H3", `${arm.locale}: the hub already named a stop before anything was picked (${JSON.stringify(before.panel_text.slice(0, 120))})`),
    ).toBe(false);
    expect(pressed, node("H3", `${arm.locale}: a hub pin claims to be selected before any tap: ${JSON.stringify(pressed)}`)).not.toContain("true");
  });
}

for (const arm of ARMS) {
  test(`hub-pressed-${arm.locale} — the pin that was tapped says so in its own state`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterHub(page, arm);
    const { target } = await pickHittablePin(page);
    expect(target, node("H4", `${arm.locale}: no hittable hub pin`)).not.toBeNull();
    await page.mouse.click(target!.centre[0], target!.centre[1]);
    await page.waitForTimeout(1_800);
    const states = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker")).map((el) => ({
        painted: (el.textContent ?? "").trim(),
        aria: el.getAttribute("aria-label") ?? "",
        pressed: el.getAttribute("aria-pressed"),
      })),
    );
    const mine = states.find((s) => s.painted === target!.painted);
    push(`hub-pressed-${arm.locale}`, { tapped: target!.painted, states });
    expect(
      mine?.pressed,
      node("H4", `${arm.locale}: hub pin ${target!.painted} was tapped but still reports aria-pressed=${JSON.stringify(mine?.pressed)}; states ${JSON.stringify(states)}`),
    ).toBe("true");
    expect(
      states.filter((s) => s.pressed === "true").length,
      node("H4", `${arm.locale}: more than one hub pin reports a selection: ${JSON.stringify(states)}`),
    ).toBe(1);
    expect(
      states.length,
      node("H4", `${arm.locale}: the hub panel exposes ${states.length} markers, too few to tell a selection apart: ${JSON.stringify(states)}`),
    ).toBeGreaterThanOrEqual(2);
  });
}

/**
 * Put the map where a traveller looking at the hub would have it, and report how far the page moved.
 *
 * The hub is a tall screen: at 320x653 the map sits entirely below the fold, so a census taken without
 * this step measures controls that nobody on that phone can reach at all, and the pin pick finds nothing
 * that answers a hit-test at its own centre -- the arm then aborts before judging anything. Scrolling the
 * surface into view is what the standing reachability ruling asks for (hit-test in every shape), and it
 * is also the only way the same two shapes can be compared with each other.
 */
async function bringMapIntoView(page: Page): Promise<number> {
  const moved = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    if (!el) return -1;
    el.scrollIntoView({ block: "center" });
    return Math.round(window.scrollY);
  });
  await page.waitForTimeout(900);
  return moved;
}

for (const [w, h] of SHAPES) {
  for (const arm of ARMS) {
    test(`hub-geometry-${w}x${h}-${arm.locale} — the linkage costs the hub no reachable control`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: w, height: h });
      await enterHub(page, arm);
      const scrolledIdle = await bringMapIntoView(page);
      const idleControls = await controlCensus(page);
      const { census: idlePins, target, attributable_to_one_stop: attributable, pins_answerable: answerable, pin_spread_px: spread } = await pickPinForCamera(page);
      expect(target, node("H5", `${w}x${h}/${arm.locale}: no hub marker answers a hit at any pin's centre: ${JSON.stringify(idlePins.pins.map((pin) => [pin.painted, pin.centre, pin.lands_on_marker, pin.covered_by]))}`)).not.toBeNull();
      await page.mouse.click(target!.centre[0], target!.centre[1]);
      await page.waitForTimeout(1_800);
      const glass = await readGlassName(page);
      /* Back into view, not to the top of the document: the first version of this arm scrolled to zero
         and then censused, which compared an idle screen with the map scrolled off it and reported the
         difference as a lost control. */
      const scrolledLive = await bringMapIntoView(page);
      const liveControls = await controlCensus(page);
      const livePins = await markerCensus(page);
      const liveBand = await page.evaluate(() => {
        const b = document.querySelector<HTMLElement>(".v8-map-overlay-stack")?.getBoundingClientRect();
        return b ? [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)] : null;
      });
      /* Reachable means what the standing ruling says it means: the control answers a hit-test at its own
         centre and that centre is inside the viewport. `HIT_FLOOR` used to be a predicate here; it is a
         number this lane invented, and a control below it is still tappable, so it is recorded and not
         judged. */
      const reachable = (list: typeof idleControls) =>
        list.filter((c) => c.hit_test && c.in_viewport).map((c) => c.key);
      const beforeReachable = reachable(idleControls);
      const afterReachable = new Set(reachable(liveControls));
      const lost = beforeReachable.filter((k) => !afterReachable.has(k));
      const bandGrowth =
        idlePins.band_box && liveBand ? liveBand[3] - idlePins.band_box[3] : null;
      const clip = await page.evaluate(() => {
        const el = document.querySelector<HTMLElement>(".v8-map-panel .v8-map-selected-stop");
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const host = el.closest(".v8-map-caption")?.getBoundingClientRect();
        return {
          box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
          scroll_w: el.scrollWidth,
          client_w: el.clientWidth,
          /* Whether the word is on the part of the screen a traveller can look at. The three checks this
             arm already makes -- painted, inside its caption, not clipped by its own box -- can all be
             true while the cell sits below the fold, which is how the first version of this arm stayed
             green while the name was off the screen. */
          in_viewport: r.top >= 0 && r.bottom <= window.innerHeight,
          inner_height: window.innerHeight,
          outside_caption: host ? r.right > host.right + 1 || r.bottom > host.bottom + 1 : null,
        };
      });
      const shot = evidencePath("ord0417-hub", `${RUN}_geometry_${w}x${h}_${arm.locale}.png`);
      await page.screenshot({ path: shot });
      push(`hub-geometry-${w}x${h}-${arm.locale}`, {
        viewport: [w, h],
        scrolled_to_idle_px: scrolledIdle,
        scrolled_to_live_px: scrolledLive,
        hit_floor_px: HIT_FLOOR,
        safety_gap_px: SAFETY_GAP_PX,
        controls_idle: idleControls.length,
        controls_with_selection: liveControls.length,
        reachable_idle: beforeReachable.length,
        reachable_with_selection: afterReachable.size,
        lost,
        /* Where each lost control actually went. A key alone cannot tell "it moved" from "it vanished",
           and the first version of this arm could only say the key had gone -- which is why the reading
           it produced could not be argued with. */
        lost_detail: lost.map((key) => {
          const was = idleControls.find((c) => c.key === key);
          const now = liveControls.find((c) => c.key === key);
          return {
            key,
            idle: was ? { centre: was.centre, hit: was.hit_test, in_viewport: was.in_viewport, above_floor: was.centre_above_floor } : null,
            live: now ? { centre: now.centre, hit: now.hit_test, in_viewport: now.in_viewport, above_floor: now.centre_above_floor } : "gone from the document",
          };
        }),
        pins_idle: idlePins.pins.map((pin) => [pin.painted, pin.centre, pin.lands_on_pin, pin.covered_by]),
        pins_with_selection: livePins.pins.map((pin) => [pin.painted, pin.centre, pin.lands_on_pin, pin.covered_by]),
        /* Named rather than hidden: at the short shape the whole journey is fitted into one dot, so the
           tap reaches a marker but cannot be said to belong to one stop. The geometry claims below still
           hold -- they are about the glass, not about which stop was chosen -- and this says which claim
           the shape cannot carry. */
        pin_spread_px: spread,
        pins_answerable: answerable,
        attribution_claim: attributable
          ? "measured"
          : "NOT_MEASURED: the camera puts every pin on the same pixel, so a tap cannot be attributed to one stop",
        band_box_idle: idlePins.band_box,
        band_box_with_selection: liveBand,
        band_growth_px: bandGrowth,
        name_painted: glass.present,
        cell: clip,
        shot,
      });
      expect(
        glass.present,
        node("H5", `${w}x${h}/${arm.locale}: no name was painted on the hub glass at all`),
      ).toBe(true);
      expect(
        bandGrowth,
        node("H5", `${w}x${h}/${arm.locale}: the band grew ${bandGrowth} px past the ${SAFETY_GAP_PX} px safety gap fitBounds reserved: ${JSON.stringify(idlePins.band_box)} -> ${JSON.stringify(liveBand)}`),
      ).toBeLessThanOrEqual(SAFETY_GAP_PX);
      expect(lost, node("H5", `${w}x${h}/${arm.locale} lost a reachable control: ${JSON.stringify(lost)}`)).toEqual([]);
      expect(
        liveControls.length,
        node("H5", `${w}x${h}/${arm.locale}: the linkage must not add a control — ${idleControls.length} -> ${liveControls.length}`),
      ).toBe(idleControls.length);
      expect(clip, node("H5", `${w}x${h}/${arm.locale}: the naming cell vanished from the hub`)).not.toBeNull();
      expect(
        clip!.in_viewport,
        node("H6", `${w}x${h}/${arm.locale}: the name is painted but not on the visible part of the screen: ${JSON.stringify(clip)}`),
      ).toBe(true);
      expect(
        clip!.outside_caption,
        node("H5", `${w}x${h}/${arm.locale}: the name is painted outside its caption box: ${JSON.stringify(clip)}`),
      ).toBe(false);
      expect(
        clip!.scroll_w - clip!.client_w,
        node("H5", `${w}x${h}/${arm.locale}: the name is clipped inside its own box: ${JSON.stringify(clip)}`),
      ).toBeLessThanOrEqual(1);
    });
  }
}

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0417-hub", `${RUN}_readings.json`),
    JSON.stringify({ file: FILE, run_id: RUN, hit_floor_px: HIT_FLOOR, shapes: SHAPES, rows }, null, 1),
  );
});
