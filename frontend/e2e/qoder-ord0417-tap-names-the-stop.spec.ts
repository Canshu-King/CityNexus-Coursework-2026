/**
 * ORD-0417 — tapping a stop on the v8 map has to say, in letters you can see, which stop it was.
 *
 * Measured on the authoritative tip (214ab0ff, instrument `_map_tap_names_stop_r1.js`, and re-run on
 * this lane's own private stack so it is not one browser's reading): six markers, `role=button`,
 * `textContent` = 1..6, stop name only in `aria-label`, `title` = null on all six. Tapping marker 2
 * left `.v8-map-caption` and `.v8-map-overlay-stack` byte-identical — the map surface answered the
 * tap with nothing.
 *
 * One correction the numbers force, recorded here rather than argued later: the tap was never
 * *silent* everywhere. The ORD-0390 card outside the panel did change
 * (`[data-testid="map-selected-stop"]` → 「已选中 · Riverside Museum」) and the page scrolled 388 px to
 * reach it. What was missing is the sentence on the glass the finger is on. So this file does not
 * re-litigate ORD-0390 — it asks a narrower thing that ORD-0390 cannot satisfy:
 *
 *   tap-named    the map panel's own rendered text contains the tapped stop's name;
 *   not-aria     the name reaches the eye through painted text, not through an attribute: the cell
 *                is checked for visibility (box, display, visibility, opacity, `.v8-sr-only`) and for
 *                lying inside the caption's own box, and the assertion reads `innerText`, which an
 *                `aria-label` cannot satisfy;
 *   unpicked     before any tap the glass names no stop at all -- the negative control that keeps
 *                "the name appeared after the tap" from being satisfied by a standing label;
 *   both-ways    ORD-0390's two directions still work -- row → camera and marker → card -- which is
 *                the regression this order could have caused and must be shown not to have caused;
 *   copy-{zh,en} the sentence is the dictionary's, verbatim, in both languages;
 *   geometry-{w}x{h}-{arm}
 *                at 390x844 / 360x800 / 320x653 / 414x896 in both languages, the census 口径 frozen
 *                by lane N3 (`hit_floor_px` 768, `elementFromPoint` at the control's own centre) is
 *                taken twice on the same scroll offset, once with nothing selected and once with the
 *                sentence painted, and the band may not cost the traveller a single reachable control,
 *                may not add a control of its own, may not uncover a pin it already covered while
 *                idle, and may not grow by more than the 16 px `safetyGap` `fitBounds` already paid
 *                for.
 *
 * Two designs were tried and rejected on measurements taken by this file, and the record stays here
 * because the shape of the surviving design is what they decided:
 *   · a line added to the bottom of the overlay stack, painted only once something is selected, grew
 *     the band from 139 px to 184 px and covered three pins (859, 600 and 1156 px² of their 28×28
 *     boxes) -- run id `ord0417r2map`;
 *   · the same line permanently reserved at a fixed two-line height kept the band's height constant
 *     but raised its *top edge* by 43 px against the tip, and still covered a pin at 390x844 that the
 *     untouched tip leaves clear -- run id `ord0417r3fix`.
 * Both bought their pixels out of the framing.  The sentence is therefore laid out inside the caption
 * box the camera already measured.
 *
 * What this file does NOT claim: that tapping a pin never leaves a pin under the band.  Selecting also
 * pans the camera (ORD-0390's `easeTo`), and on the untouched tip that pan alone puts two pins under
 * the band at 320x653 and moves the Copilot pill out of the reachable set -- measured by the same
 * census, recorded in `pins_covered_by_band_after_the_pan` rather than asserted, and judged in the
 * delivery by the tip-vs-candidate diff.  A clustering/framing order owns that, not this one.
 *
 * The control set, the floor and the (name, n-th-of-that-name) identity key are copied from
 * `e2e/qoder-ord0407-empty-state-actions.spec.ts` deliberately: a lane that re-defines the ruler to
 * pass its own change is not measurable.
 *
 * No response body is rewritten, nothing is mocked, no arm is skipped. Assertions read rendered
 * text, never a data attribute (Q64=①/Q70/Q71).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0417-tap-names-the-stop.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* Lane N3's frozen 口径: the fixed `v8-mobile-nav` covers 768..844 on a 390x844 phone. */
const HIT_FLOOR = 768;
const SHAPES: Array<[number, number]> = [[390, 844], [360, 800], [320, 653], [414, 896]];
const ARMS = [
  /* The authored dictionary lead-ins; every arm reads the painted sentence back and compares. */
  { locale: "zh", lead: "图上选中的这一站" },
  { locale: "en", lead: "Selected on the map" },
] as const;

/* `MapPanel`'s own number, in the expression it hands to `fitBounds`:
   `padding.bottom = side + overlayHeight + safetyGap`, with `side` 42 and `safetyGap` 16.  The band
   may not grow past the slack the framing already paid for. */
const SAFETY_GAP_PX = 16;

async function enterMap(page: Page, arm: (typeof ARMS)[number] | null = null): Promise<void> {
  /* The language is asked for before the app's first script runs, not toggled afterwards: the seed's
     storageState carries `citynexus.locale` from wherever the seed happened to leave it, and a
     "Chinese arm" that silently inherits English would prove nothing about the Chinese dictionary.
     The key is written, never `localStorage.clear()`ed -- clearing it would drop the journey id the
     seed project persisted. */
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
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

/**
 * The pin to tap: one whose own centre actually answers a hit-test on itself.
 *
 * This seed puts a check-in and a check-out on the same bed, so two pins share one pixel, and a
 * click at either centre lands on whichever element sits last in the document.  ORD-0390's `reach`
 * arm already records that as a clustering gap belonging to its own order rather than a layout bug;
 * this order must not be judged on a tap that never reached the pin it named.  So every arm taps a
 * pin it can *prove* it hit -- and the proof is the pin's own `aria-pressed` flipping, asserted
 * below, not the coordinate being plausible.
 */
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

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0417", `${RUN}__readings.json`),
    JSON.stringify({ file: FILE, run_id: RUN, hit_floor_px: HIT_FLOOR, shapes: SHAPES, rows }, null, 1),
    "utf-8",
  );
});

/* ----------------------------------------------------------------- the defect, closed */

for (const arm of ARMS) {
  test(`tap-named-${arm.locale} — a tap on a pin paints that stop's name on the glass`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page, arm);
    const { target } = await pickHittablePin(page);

    /* a finger, not a `.click()` on the locator: the coordinate the commander tapped at */
    await page.mouse.click(target.centre[0], target.centre[1]);
    await page.waitForTimeout(1_800);

    const glass = await readGlassName(page);
    const after = await markerCensus(page);
    const pressedAfter = after.pins.find((p) => p.painted === target.painted)?.pressed ?? null;
    const shot = evidencePath("ord0417", `${RUN}_tap-named_${arm.locale}.png`);
    await page.locator(".v8-map-panel").screenshot({ path: shot });
    push(`tap-named-${arm.locale}`, {
      tapped_painted: target.painted,
      tapped_name: target.aria,
      tap_landed_on_pin: target.lands_on_pin,
      name_in_glass_painted_text: glass.present && glass.painted.includes(target.aria),
      glass,
      marker_pressed_after_tap: pressedAfter,
      shot,
    });
    /* the tap has to be provably the one this arm claims: the shared selection flipped *this* pin.
       Without this, a co-located pin could answer for the name and the arm would be reading a
       different stop than it tapped. */
    expect(pressedAfter, node("A1", `tapping pin "${target.painted}" at ${JSON.stringify(target.centre)} must leave that pin pressed, got ${pressedAfter}`)).toBe(
      "true",
    );
    /* First assertion, and it aborts the arm: `expect(false, msg)` without a matcher is a no-op, so
       the first cut of this file let the mutant slip past to the next line and report Playwright's own
       "received value must not be null" instead of naming what a traveller cannot see. */
    expect(
      glass.present,
      node("A1", `nothing painted a name on the glass after tapping "${target.aria}"; the panel's whole rendered text is: ${glass.panel_text}`),
    ).toBe(true);
    expect(glass.painted, node("A1", `the painted glass sentence must contain the tapped stop's name "${target.aria}"`)).toContain(
      target.aria,
    );
    expect(glass.in_panel, node("A1", "the name must be inside the map panel, not on another surface")).toBe(true);
    /* the pin itself still knows it was pressed -- ORD-0390 must survive this order */
    expect(
      glass.panel_text.includes(target.aria),
      node("A1", "the panel's own rendered text must contain the name"),
    ).toBe(true);
  });
}

/* ------------------------------------------------- the name must reach the eye, not only the reader */

for (const arm of ARMS) {
  test(`not-aria-${arm.locale} — the name is painted, not an attribute a reader recites`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page, arm);
    const { target } = await pickHittablePin(page);
    /* Prove the *old* channel is still only an attribute, so this arm cannot be satisfied by it. */
    expect(target.painted, node("A2", "a pin was expected to paint its number, not its name")).not.toContain(
      target.aria,
    );
    expect(target.title_attr, node("A2", "title is still empty on every pin; if it were not, a tooltip would be counted as visible text")).toBeFalsy();

    await page.mouse.click(target.centre[0], target.centre[1]);
    await page.waitForTimeout(1_800);
    const glass = await readGlassName(page);
    const pressedAfter = (await markerCensus(page)).pins.find((p) => p.painted === target.painted)?.pressed ?? null;
    push(`not-aria-${arm.locale}`, {
      tapped_name: target.aria,
      pin_painted: target.painted,
      pin_title_attr: target.title_attr,
      marker_pressed_after_tap: pressedAfter,
      painted: glass.present ? glass.painted : null,
      box: glass.present ? glass.box : null,
      display: glass.display,
      visibility: glass.visibility,
      opacity: glass.opacity,
      clip: glass.clip,
      sr_only: glass.sr_only,
    });
    expect(pressedAfter, node("A2", `this arm must be reading the pin it tapped; pin "${target.painted}" ended pressed=${pressedAfter}`)).toBe(
      "true",
    );
    expect(
      glass.present,
      node("A2", `no visible cell carries the name after tapping "${target.aria}" — an aria-label alone does not close ORD-0417`),
    ).toBe(true);
    expect(glass.display, node("A2", "the cell must be laid out")).not.toBe("none");
    expect(glass.visibility, node("A2", "the cell must not be hidden")).toBe("visible");
    expect(glass.opacity, node("A2", "the cell must not be faded out")).toBeGreaterThan(0);
    expect(glass.clip, node("A2", "the cell must not be clipped away")).not.toBe("inset(50%)");
    expect(glass.sr_only, node("A2", "the cell must not be the screen-reader-only utility")).toBe(false);
    const box = glass.box ?? [0, 0, 0, 0];
    expect(box[2], node("A2", "the cell must have width a real eye can read")).toBeGreaterThan(40);
    expect(box[3], node("A2", "the cell must have height a real eye can read")).toBeGreaterThan(0);
    expect(glass.inside_caption, node("A2", `the sentence must be painted inside the caption box the camera accounts for; caption ${JSON.stringify(glass.caption_box)}, cell ${JSON.stringify(glass.box)}`)).toBe(
      true,
    );
    expect(glass.painted, node("A2", "innerText -- painted text only, an attribute cannot satisfy this -- must contain the name")).toContain(
      target.aria,
    );
  });
}

/* --------------------------------- the caption is the answer to a tap, not a standing label */

for (const arm of ARMS) {
  test(`unpicked-${arm.locale} — before a tap the glass names no stop, so the other arms cannot borrow a label`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page, arm);
    const pins = await markerCensus(page);
    const glass = await readGlassName(page);
    const names = pins.pins.map((p) => p.aria).filter(Boolean);
    const leaked = names.filter((n) => glass.present && glass.panel_text.includes(n));
    push(`unpicked-${arm.locale}`, {
      lang: pins.lang,
      cell_painted_before_any_tap: glass.present ? glass.painted : null,
      pins_on_glass: pins.pins.length,
      stop_names_leaked_into_panel_text_before_any_tap: leaked,
      band_box_idle: pins.band_box,
    });
    /* The negative control the `tap-named-*` arms need: if the panel already read the traveller's
       destination before anything was picked, "the name appeared after the tap" would be worth
       nothing.  And the cell must not exist until a stop is selected -- this order's whole geometry
       argument is that a permanent box here would raise the band and take ground the camera promised
       for the pins. */
    expect(
      glass.present,
      node("A2b", `the glass cell must not be painted while nothing is selected; it read "${glass.present ? glass.painted : ""}"`),
    ).toBe(false);
    expect(leaked, node("A2b", `a stop name is readable on the panel before anything was tapped: ${JSON.stringify(leaked)}`)).toEqual(
      [],
    );
  });
}

/* ------------------------------------------------------- ORD-0390's two directions still work */

test("both-ways — row to camera and marker to card both survive the new band", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterMap(page);
  const camera = () =>
    page.evaluate(() => {
      const host = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
      const map = (host as (HTMLElement & { citynexusMap?: any }) | null)?.citynexusMap;
      if (!map) return null;
      const c = map.getCenter();
      return { lng: Number(c.lng.toFixed(6)), lat: Number(c.lat.toFixed(6)) };
    });
  const card = () => page.getByTestId("map-selected-stop").innerText().then((s) => s.trim());

  /* forward: card -> map. Pick the row whose stop is furthest from the camera, so a real move is due. */
  const start = await camera(page);
  expect(start, node("A3", "the camera seam is not readable")).not.toBeNull();
  const rowLocators = page.getByTestId(/^map-stop-/);
  expect(await rowLocators.count(), node("A3", "no stop rows were offered")).toBeGreaterThan(1);
  const rowName = (await rowLocators.last().innerText()).trim();
  await rowLocators.last().click();
  await page.waitForTimeout(1_400);
  const end = await camera(page);
  const rowPressed = await rowLocators.last().getAttribute("aria-pressed");
  const pinsAfterRow = await markerCensus(page);
  const ringOn = pinsAfterRow.pins.filter((p) => p.class.includes("selected")).map((p) => p.aria);
  /* the new band names the stop in this direction too -- that is the bonus, not the contract */
  const glassFromRow = await readGlassName(page);
  push("card-to-map", {
    start,
    end,
    moved: !!start && !!end && (start.lng !== end.lng || start.lat !== end.lat),
    rowPressed,
    rowName,
    ringOn,
    glass_painted: glassFromRow.present ? glassFromRow.painted : null,
    glass_names_row: glassFromRow.present && glassFromRow.painted.includes(rowName),
  });
  expect(
    !!start && !!end && (start.lng !== end.lng || start.lat !== end.lat),
    node("A3", `clicking a stop row must still move the camera: ${JSON.stringify(start)} -> ${JSON.stringify(end)}`),
  ).toBe(true);
  expect(rowPressed, node("A3", "the pressed row must still report itself pressed")).toBe("true");
  expect(ringOn.length, node("A3", "a pin must still take the selection ring when a row is pressed")).toBeGreaterThan(0);

  /* reverse: map -> card. Tap a *different* pin and the card must follow, as ORD-0390 shipped. */
  const cardBefore = await card();
  const pinsNow = await markerCensus(page);
  const other =
    pinsNow.pins.find((p) => p.lands_on_pin && p.aria && p.aria !== cardBefore) ??
    pinsNow.pins.find((p) => p.lands_on_pin) ??
    pinsNow.pins[0];
  expect(other.lands_on_pin, node("A3", "the reverse half must tap a pin it can prove it hit")).toBe(true);
  await page.mouse.click(other.centre[0], other.centre[1]);
  await page.waitForTimeout(1_600);
  const cardAfter = await card();
  const glassFromPin = await readGlassName(page);
  push("map-to-card", {
    tapped: other.aria,
    cardBefore,
    cardAfter,
    card_changed: cardBefore !== cardAfter,
    card_names_pin: cardAfter.includes(other.aria),
    glass_painted: glassFromPin.present ? glassFromPin.painted : null,
  });
  expect(cardAfter, node("A3", `tapping the pin for "${other.aria}" must still update the ORD-0390 card`)).toContain(
    other.aria,
  );
});

/* ------------------------------------------------------------------ copy is the dictionary's */

for (const arm of ARMS) {
  test(`copy-${arm.locale} — the glass sentence is the dictionary's, verbatim`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page, arm);
    const { census: pins, target } = await pickHittablePin(page);
    await page.mouse.click(target.centre[0], target.centre[1]);
    await page.waitForTimeout(1_600);
    const glass = await readGlassName(page);
    const expected = glass.present ? `${arm.lead} · ${target.aria}` : null;
    const pressedAfter = (await markerCensus(page)).pins.find((p) => p.painted === target.painted)?.pressed ?? null;
    push(`copy-${arm.locale}`, {
      lang: pins.lang,
      lead: arm.lead,
      tapped_name: target.aria,
      marker_pressed_after_tap: pressedAfter,
      painted: glass.present ? glass.painted : null,
      expected,
      text_content: glass.present ? glass.text_content : null,
    });
    expect(pressedAfter, node("A4", `arm ${arm.locale}: this sentence must belong to the pin that was tapped`)).toBe(
      "true",
    );
    expect(glass.present, node("A4", `arm ${arm.locale}: the glass cell did not render`)).toBe(true);
    expect(pins.lang, node("A4", `arm ${arm.locale} is not painting in the language it asked for`)).toContain(
      arm.locale,
    );
    /* exact, not substring: a paraphrase in the component would pass a `toContain` and would put the
       component and the dictionary out of agreement, which is what the pairing ruler exists to catch. */
    expect(glass.painted, node("A4", `arm ${arm.locale} must paint the dictionary's sentence exactly`)).toBe(expected);
  });
}

/* ---------------------------------------------------------- what the new band costs, per shape */

for (const [w, h] of SHAPES) {
  for (const arm of ARMS) {
    test(`geometry-${w}x${h}-${arm.locale} — the band costs no reachable control and covers no pin`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: w, height: h });
      await enterMap(page, arm);
      const idleControls = await controlCensus(page);
      const { census: idlePins, target } = await pickHittablePin(page);
      await page.mouse.click(target.centre[0], target.centre[1]);
      await page.waitForTimeout(1_800);

      /* pins must still answer a hit-test where they lie, and the name must already be painted */
      const glass = await readGlassName(page);

      /* back to the same scroll offset the idle census was taken at, so the only variable is the
         band -- a page that scrolled would otherwise look like a lost control. */
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(900);
      const liveControls = await controlCensus(page);
      const livePins = await markerCensus(page);
      const liveBand = await page.evaluate(() => {
        const b = document.querySelector<HTMLElement>(".v8-map-overlay-stack")?.getBoundingClientRect();
        return b ? [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)] : null;
      });
      const shot = evidencePath("ord0417", `${RUN}_geometry_${w}x${h}_${arm.locale}.png`);
      await page.screenshot({ path: shot });

      const reachable = (list: typeof idleControls) =>
        list.filter((c) => c.hit_test && c.centre_above_floor && c.in_viewport).map((c) => c.key);
      const beforeReachable = reachable(idleControls);
      const afterReachable = new Set(reachable(liveControls));
      const lost = beforeReachable.filter((k) => !afterReachable.has(k));
      /* Only regressions this order could have caused are judged.  A pin that was already under a
         neighbour, already covered, or already below the line when nothing was selected is a
         pre-existing state, not a consequence of the band -- the arbiter's standing rule on
         precedence, and the reason a delta is taken between two readings of the same screen rather
         than asserted against an absolute. */
      const idleLands = new Set(idlePins.pins.filter((p) => p.lands_on_pin).map((p) => p.painted));
      const idleCovered = new Map(idlePins.pins.map((p) => [p.painted, p.covered_px2_by_band]));
      const idleBelow = new Set(
        idlePins.pins.filter((p) => p.centre[1] > HIT_FLOOR).map((p) => p.painted),
      );
      const covered = livePins.pins
        .filter((p) => (idleCovered.get(p.painted) ?? 0) === 0 && p.covered_px2_by_band > 0)
        .map((p) => ({ painted: p.painted, aria: p.aria, px2: p.covered_px2_by_band }));
      const unhittable = livePins.pins
        .filter((p) => idleLands.has(p.painted) && !p.lands_on_pin)
        .map((p) => ({ painted: p.painted, centre: p.centre }));
      const belowFloor = livePins.pins
        .filter((p) => p.centre[1] > HIT_FLOOR && !idleBelow.has(p.painted))
        .map((p) => ({ painted: p.painted, centre: p.centre }));

      const pinsCoveredAtIdle = idlePins.pins
        .filter((p) => p.covered_px2_by_band > 0)
        .map((p) => ({ painted: p.painted, aria: p.aria, px2: p.covered_px2_by_band }));

      const bandGrowth =
        idlePins.band_box && liveBand ? liveBand[3] - idlePins.band_box[3] : null;

      push(`geometry-${w}x${h}-${arm.locale}`, {
        viewport: [w, h],
        lang: livePins.lang,
        hit_floor_px: HIT_FLOOR,
        safety_gap_px: SAFETY_GAP_PX,
        controls_idle: idleControls.length,
        controls_with_band: liveControls.length,
        reachable_idle: beforeReachable.length,
        reachable_with_band: afterReachable.size,
        lost,
        band_box_idle: idlePins.band_box,
        band_box_with_selection: liveBand,
        band_growth_px: bandGrowth,
        pins_covered_by_band_at_idle: pinsCoveredAtIdle,
        pins_covered_by_band_after_the_pan: covered,
        pins_unhittable: unhittable,
        pins_below_floor: belowFloor,
        glass_painted: glass.present ? glass.painted : null,
        shot,
      });

      expect(glass.present, node("A5", `${w}x${h}/${arm.locale}: no name was painted on the glass at all`)).toBe(
        true,
      );
      expect(
        pinsCoveredAtIdle,
        node("A5", `${w}x${h}/${arm.locale}: the band already covered a pin before anything was picked: ${JSON.stringify(pinsCoveredAtIdle)}`),
      ).toEqual([]);
      /* The one thing this order is accountable for: the sentence must not buy its pixels out of the
         framing.  `fitBounds` measured this band once, with nothing selected, and paid
         `side 42 + overlayHeight + safetyGap 16`; growth beyond that 16 px is the camera's promise
         broken, not a layout preference.  With the box unchanged the band cannot be what covers a pin,
         which is why `pins_covered_by_band_after_the_pan` is recorded rather than asserted -- tapping
         a pin also pans the camera (ORD-0390), and on the untouched tip that pan already leaves two
         pins under the band at 320x653.  The tip-vs-candidate diff in the delivery receipt is what
         judges it. */
      expect(
        bandGrowth,
        node("A5", `${w}x${h}/${arm.locale}: the band grew ${bandGrowth} px on selection, past the ${SAFETY_GAP_PX} px safety gap fitBounds reserved for it: ${JSON.stringify(idlePins.band_box)} -> ${JSON.stringify(liveBand)}`),
      ).toBeLessThanOrEqual(SAFETY_GAP_PX);
      expect(lost, node("A5", `${w}x${h}/${arm.locale} lost a reachable control: ${JSON.stringify(lost)}`)).toEqual(
        [],
      );
      expect(
        liveControls.length,
        node("A5", `${w}x${h}/${arm.locale}: the band must not add a control -- ${idleControls.length} -> ${liveControls.length}`),
      ).toBe(idleControls.length);
      expect(
        unhittable,
        node("A5", `${w}x${h}/${arm.locale}: a pin no longer answers a hit at its own centre: ${JSON.stringify(unhittable)}`),
      ).toEqual([]);
      expect(
        belowFloor,
        node("A5", `${w}x${h}/${arm.locale}: a pin's centre fell below the ${HIT_FLOOR} px line: ${JSON.stringify(belowFloor)}`),
      ).toEqual([]);
    });
  }
}
