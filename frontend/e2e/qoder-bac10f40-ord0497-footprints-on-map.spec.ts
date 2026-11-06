/**
 * ORD-0497 — the footprints on the map, measured on the map.
 *
 * What this spec defends, in the order's own words: a stop the server holds a stamp for has to look
 * different from one it does not, the distinction may only come from
 * `GET /api/v1/journeys/{id}/checkins`, an empty answer has to be named as *this read bringing back
 * nothing* rather than as a fact about the traveller, and a read that never happened must not leave
 * the screen claiming anything at all.
 *
 * Four arms, and the fourth is the one the order asked for as the counterexample:
 *   1. `empty_read_names_itself`   — a journey with nothing stamped says so in both language arms, and
 *                                    every located pin is hollow.
 *   2. `solid_after_a_real_stamp`  — the 打卡 control on the trips screen is pressed (the real POST),
 *                                    then the MAP is mounted fresh: it can only know what it just
 *                                    read back, because it never saw the press.
 *   3. `filter_toggle`             — 「只看打过」 hides the unstamped pins, shows them again, and is
 *                                    gone after a reload: the switch is state of this screen only.
 *   4. `read_never_arrived`        — the read leg is cut (the response never lands). The screen names
 *                                    that it did not reach the server, paints no solid pin and no
 *                                    date badge, and leaves every pin exactly as an unanswered map
 *                                    draws it. Removing the read must not invent a stamp.
 *
 * Nothing here rewrites a response body to make the product look right. Arm 4 only ever makes the
 * door *not answer*; the two arms that need a served answer (arm 1's empty list, arm 2's stamp) take
 * what the real backend gives on a fresh authoritative run database.
 *
 * Geometry: 390x844, the phone shape the order names. Hit tests are taken at the control's own
 * centre with `document.elementFromPoint`, and the reading records whether that centre was on screen
 * before anything was scrolled, so a below-the-fold null cannot be read as a product defect (the
 * ORD-0390 lesson this lane keeps having to re-apply).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";

const FILE = "e2e/qoder-bac10f40-ord0497-footprints-on-map.spec.ts";
const PHONE = { width: 390, height: 844 };
const PANEL = "[data-testid='v8-maplibre-panel']";
const BAND = "[data-testid='v8-map-footprint-state']";
const FILTER = "[data-testid='v8-map-footprint-filter']";
/* The sentences, taken from the copy table the product itself reads. The spec names them because it
   has to tell the four arms apart -- and an arm that passes against a paraphrase proves nothing. */
const COPY = {
  none: {
    zh: "这台服务器这次没有交回任何足迹，所以这一趟在图上的站全部画成空心。",
    en: "This server returned no footprint on this read, so every stop of this trip is painted hollow.",
  },
  unreachable: {
    zh: "这次没有问到服务器，所以图上不区分到过的站与没到过的站——这一片的画法不替服务器说话。",
    en: "This read never reached the server, so the map draws no distinction — nothing painted here speaks for the server.",
  },
  filterLabel: { zh: "只看打过", en: "Stamped only" },
  filterClear: { zh: "看全部站", en: "Show every stop" },
  stampedLead: { zh: "这台服务器这次交回", en: "This server handed back" },
  done: { zh: "已打卡", en: "Checked in" },
};

const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

const SEL = { PANEL, BAND, FILTER };

/** Everything the map says about footprints, read off what is painted. */
const witness = (sel: typeof SEL) => {
  const doc = document;
  const panel = doc.querySelector<HTMLElement>(sel.PANEL);
  const pins = Array.from(doc.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
  const pick = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2);
    const cy = Math.round(r.y + r.height / 2);
    const at = doc.elementFromPoint(cx, cy);
    const cs = getComputedStyle(el);
    return {
      cls: el.className,
      text: (el.textContent ?? "").trim(),
      box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      centre: { x: cx, y: cy },
      on_screen: r.top >= 0 && r.bottom <= window.innerHeight,
      visibility: cs.visibility,
      background: cs.backgroundColor,
      hit: at ? at.tagName.toLowerCase() + "." + String(at.className || "").slice(0, 26) : "none",
      hit_is_pin: !!(at && (at === el || el.contains(at))),
    };
  };
  const badges = Array.from(doc.querySelectorAll<HTMLElement>(".v8-map-panel .v8-map-stamp-date"));
  const band = doc.querySelector<HTMLElement>(sel.BAND);
  const filter = doc.querySelector<HTMLElement>(sel.FILTER);
  const solid = pins.filter((el) => el.classList.contains("stamped"));
  const hollow = pins.filter((el) => el.classList.contains("un-stamped"));
  return {
    band_text: (band?.innerText ?? "").trim(),
    panel_present: !!panel,
    pin_total: pins.length,
    pin_claim: pins.filter((el) => el.classList.contains("stamped") || el.classList.contains("un-stamped")).length,
    solid_count: solid.length,
    hollow_count: hollow.length,
    hidden_count: pins.filter((el) => getComputedStyle(el).visibility === "hidden").length,
    badge_count: badges.length,
    badge_texts: badges.map((el) => (el.textContent ?? "").trim()),
    badge_testids: badges.map((el) => el.getAttribute("data-testid") ?? ""),
    filter_present: !!filter,
    filter_pressed: filter?.getAttribute("aria-pressed") ?? null,
    filter_text: (filter?.innerText ?? "").trim(),
    filter_hit: filter
      ? (() => {
          const r = filter.getBoundingClientRect();
          const at = doc.elementFromPoint(Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2));
          return at ? at.tagName.toLowerCase() + "." + String(at.className || "").slice(0, 26) : "none";
        })()
      : "no-element",
    first_solid: solid.length ? pick(solid[0]) : null,
    first_hollow: hollow.length ? pick(hollow[0]) : null,
    localstorage_footprint_keys: Object.keys(localStorage).filter((k) =>
      /footprint|stamp|checkin|filter/i.test(k),
    ),
  };
};

type Witness = ReturnType<typeof witness>;

async function readMap(page: Page): Promise<Witness> {
  return page.evaluate(witness, SEL);
}

/** Wait until the surface has actually settled -- the band's sentence AND the pins.
 *
 * maplibre is a dynamic import and the pins are painted inside that promise, so a reading taken the
 * moment the band settles is a reading of a map with nothing on it yet. That is exactly how arm 4 of
 * the first run of this file "found" zero pins: the instrument sampled a frame before the pins
 * existed, and a zero it invented itself would have been reported as a product fact. */
async function settleMap(page: Page): Promise<Witness> {
  await expect(page.locator(BAND)).toBeVisible({ timeout: 25_000 });
  await page.waitForFunction(
    () => {
      const band = document.querySelector<HTMLElement>("[data-testid='v8-map-footprint-state']");
      if (!band) return false;
      if (/正在向服务器问|Asking the server/.test(band.innerText)) return false;
      return document.querySelectorAll(".v8-map-panel .v9-map-marker").length > 1;
    },
    null,
    { timeout: 25_000 },
  );
  await page.waitForTimeout(500);
  return readMap(page);
}

/** Land on the map screen by route: `/v8/map` is a key in `desktopRoutes`, so no drawer is in the
 *  way and the reading does not depend on which chrome a given width shows. */
async function enterMap(page: Page, locale: "zh-CN" | "en" = "zh-CN") {
  await page.addInitScript((code) => localStorage.setItem("citynexus.locale", code), locale);
  await page.setViewportSize(PHONE);
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/map", ["v8-maplibre-panel"]);
  return settleMap(page);
}

test("ORD-0497 zh 390x844 arm=empty_read_names_itself an answered-empty read is named as the read", async ({ page }) => {
  const w = await enterMap(page);
  expect(w.panel_present, "the map panel has to be the one being measured").toBe(true);
  expect(w.band_text, "the empty arm names THIS read, not the traveller").toBe(COPY.none.zh);
  expect(w.pin_total, "the seeded trip has located stops to paint").toBeGreaterThan(1);
  /* Every claim-bearing pin is hollow and none is solid: the server returned nothing, so nothing may
     be painted as visited. Pins that carry no claim (the traveller's own dot, a stay offer) are
     excluded from `pin_claim` rather than counted as evidence. */
  expect(w.solid_count, "an empty read must not paint a single solid pin").toBe(0);
  expect(w.hollow_count, "every plannable pin is hollow on an empty read").toBe(w.pin_claim);
  expect(w.badge_count, "no stamp, no date painted").toBe(0);
  expect(w.first_hollow?.hit_is_pin, "the hollow pin takes its own tap").toBe(true);
  push("empty_read_names_itself", {
    locale: "zh-CN",
    viewport: PHONE,
    band_text: w.band_text,
    pin_total: w.pin_total,
    pin_claim: w.pin_claim,
    solid_count: w.solid_count,
    hollow_count: w.hollow_count,
    hollow_probe: w.first_hollow,
  });

  /* The same arm in the other language, because both sentences were written here and an untested
     dictionary arm is an untested sentence. */
  const we = await enterMap(page, "en");
  expect(we.band_text, "the English empty arm is the sentence that was written").toBe(COPY.none.en);
  push("empty_read_names_itself_en", {
    locale: "en",
    viewport: PHONE,
    band_text: we.band_text,
    solid_count: we.solid_count,
    hollow_count: we.hollow_count,
    badge_count: we.badge_count,
  });
});

/** Ask the same door the screen asks, from the test, so the painted count and the served count are
 *  two independent readings of one fact rather than one reading quoted twice. */
async function doorRows(page: Page): Promise<number> {
  const journeyId = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
  expect(journeyId, "the screen has to have a journey for the door to answer about").toBeTruthy();
  const url = new URL(`/api/v1/journeys/${journeyId}/checkins`, page.url()).toString();
  const answer = await page.request.get(url);
  expect(answer.status(), "the read route answers").toBe(200);
  const body = await answer.json();
  expect(Array.isArray(body), "the door answers a list, and an empty trip is an empty list").toBe(true);
  return body.length;
}

/** The stop rows beside the map, counted as nodes: the filter is allowed to hide pins, not to delete
 *  anybody's row. */
async function chipRows(page: Page): Promise<number> {
  return page.locator("[data-testid^='map-stop-']").count();
}

test("ORD-0497 zh 390x844 arm=solid_after_a_real_stamp the map knows only what it read back", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.setViewportSize(PHONE);
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/trips", ["trip-itinerary"]);
  const control = page.locator("[data-testid='stop-checkin']").first();
  await control.scrollIntoViewIfNeeded();
  const before = (await control.innerText()).trim();
  expect(before, "the control starts as 打卡 on a journey nothing was stamped on").toBe("打卡");
  await control.click();
  /* The trips screen re-reads the door after the POST, so 已打卡 here is already a read-back -- but
     the arm that matters is the next one: a fresh map mount, which never saw this press. */
  await expect(control).toHaveText(COPY.done.zh, { timeout: 25_000 });
  await page.waitForTimeout(1_200);

  const w = await enterMap(page);
  const served = await doorRows(page);
  expect(served, "the door's own row count is what the map paints solid").toBe(w.solid_count);
  expect(w.band_text.startsWith(COPY.stampedLead.zh), `the read arm has to name the count, got "${w.band_text}"`).toBe(true);
  expect(w.solid_count, "exactly the one stop the server holds a stamp for is solid").toBe(1);
  expect(w.badge_count, "and it carries exactly one date").toBe(1);
  expect(/^\d+月\d+日$/.test(w.badge_texts[0] ?? ""), `the badge paints a day, got "${w.badge_texts[0]}"`).toBe(true);
  expect(w.hollow_count, "the stops with no stamp stay hollow -- the screen distinguishes, it does not fill in").toBeGreaterThan(0);
  expect(w.first_solid?.hit_is_pin, "the solid pin still takes its own tap").toBe(true);
  expect(w.first_solid?.on_screen, "and the reading was taken while it was on screen").toBe(true);
  await page.screenshot({ path: evidencePath("ord0497", `map_stamped_${process.env.CITYNEXUS_E2E_RUN_ID || "dev"}.png`) });
  push("solid_after_a_real_stamp", {
    locale: "zh-CN",
    viewport: PHONE,
    control_before: before,
    door_rows_served: served,
    band_text: w.band_text,
    pin_total: w.pin_total,
    solid_count: w.solid_count,
    hollow_count: w.hollow_count,
    badge_texts: w.badge_texts,
    badge_testids: w.badge_testids,
    solid_probe: w.first_solid,
    hollow_probe: w.first_hollow,
  });
});

test("ORD-0497 zh 390x844 arm=filter_toggle 只看打过 hides, reveals, and does not survive", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.setViewportSize(PHONE);
  /* The filter is offered only on an answered read, so this arm answers first: stamp one stop through
     the same real door the traveller uses. */
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/trips", ["trip-itinerary"]);
  await page.locator("[data-testid='stop-checkin']").first().click();
  await page.waitForTimeout(1_200);

  await enterMap(page);
  const filter = page.locator(FILTER);
  await expect(filter).toBeVisible({ timeout: 25_000 });
  const label = (await filter.innerText()).trim();
  expect(label, "off, the control offers the filter").toBe(COPY.filterLabel.zh);
  const open = await readMap(page);
  expect(open.hidden_count, "nothing is hidden before the press").toBe(0);
  await filter.scrollIntoViewIfNeeded();
  const hit = await readMap(page);
  expect(hit.filter_hit, "the switch takes its own tap").toContain("v8-map-footprint-filter");
  /* The switch is a view, not a write: the door's row count and the row count beside the map have to
     be the same numbers on both sides of every press. Anything else would mean pressing a filter
     changed what the server holds, or deleted somebody's row. */
  const doorBefore = await doorRows(page);
  const chipsBefore = await chipRows(page);
  /* A picture of the two things the numbers above claim: the sentence beside the map and the switch
     under it. The map itself is screenshotted by the arm that stamps, so the two together show the
     claim and its control on one screen. */
  await page.screenshot({
    path: evidencePath("ord0497", `facts_read_${process.env.CITYNEXUS_E2E_RUN_ID || "dev"}.png`),
  });

  await filter.click();
  await page.waitForTimeout(700);
  const on = await readMap(page);
  expect(on.filter_pressed, "the control carries its own state").toBe("true");
  expect(on.filter_text, "and says a different sentence once it is on").toBe(COPY.filterClear.zh);
  expect(on.hidden_count, "every unstamped pin is hidden").toBe(on.pin_claim - on.solid_count);
  expect(on.solid_count, "the stamped pin is never hidden by its own filter").toBe(1);
  expect(on.badge_count, "its date stays painted").toBe(1);
  /* Whole page, not the viewport: the pressed switch is below the fold and the pins it hid are above
     it, and a picture that can only show one of the two proves the click and not the filter. */
  await page.screenshot({
    fullPage: true,
    path: evidencePath("ord0497", `facts_filtered_on_${process.env.CITYNEXUS_E2E_RUN_ID || "dev"}.png`),
  });

  await filter.click();
  await page.waitForTimeout(700);
  const back = await readMap(page);
  expect(back.hidden_count, "pressing again shows them all").toBe(0);
  expect(back.filter_pressed, "and the state says so").toBe("false");
  const doorAfter = await doorRows(page);
  const chipsAfter = await chipRows(page);
  expect(doorAfter - doorBefore, "the switch writes no row to the server").toBe(0);
  expect(chipsAfter - chipsBefore, "and removes no row from the screen").toBe(0);

  /* The order's other half: the switch state is not written down anywhere. A reload is a fresh mount,
     and a filter that survived it would mean the screen had found somewhere to store it. */
  await filter.click();
  await page.waitForTimeout(500);
  await page.reload({ waitUntil: "networkidle" });
  /* Settle before judging the reload: `hidden_count == 0` is also what an unbuilt map reports, and a
     green that a blank frame could produce is not a green. */
  const reloaded = await settleMap(page);
  expect(reloaded.filter_pressed, "the reload does not remember the switch").toBe("false");
  expect(reloaded.hidden_count, "and nothing is hidden on it").toBe(0);
  expect(reloaded.localstorage_footprint_keys, "no footprint/filter key is written to this origin").toEqual([]);
  push("filter_toggle", {
    locale: "zh-CN",
    viewport: PHONE,
    label_before: label,
    filter_hit: hit.filter_hit,
    door_rows_before: doorBefore,
    door_rows_after: doorAfter,
    chip_rows_before: chipsBefore,
    chip_rows_after: chipsAfter,
    open_hidden: open.hidden_count,
    on_hidden: on.hidden_count,
    on_pin_claim: on.pin_claim,
    on_solid: on.solid_count,
    back_hidden: back.hidden_count,
    reloaded_pressed: reloaded.filter_pressed,
    reloaded_hidden: reloaded.hidden_count,
    localstorage_footprint_keys: reloaded.localstorage_footprint_keys,
  });
});

test("ORD-0497 zh 390x844 arm=read_never_arrived cutting the read leg invents no stamp", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.setViewportSize(PHONE);
  await ensureDemoJourney(page);
  /* Cut the read before the screen asks. This is a failure injection, not a doctored answer: the door
     never responds, which is the one state in which a client is most likely to guess. */
  await page.route("**/api/v1/journeys/*/checkins*", (route) => route.abort());
  await page.goto("/v8/map", { waitUntil: "load" });
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  const w = await settleMap(page);
  expect(w.band_text, "the screen says it never reached the server").toBe(COPY.unreachable.zh);
  expect(w.solid_count, "and does not paint one solid pin").toBe(0);
  expect(w.badge_count, "nor a single date").toBe(0);
  expect(w.hollow_count, "nor an hollow one -- no claim is made about stops it cannot ask about").toBe(0);
  expect(w.filter_present, "the filter is not offered over an unanswered read").toBe(false);
  expect(w.pin_total, "the map itself is still the map").toBeGreaterThan(1);
  push("read_never_arrived", {
    locale: "zh-CN",
    viewport: PHONE,
    injected: "route.abort on the checkins read",
    band_text: w.band_text,
    pin_total: w.pin_total,
    pin_claim: w.pin_claim,
    solid_count: w.solid_count,
    hollow_count: w.hollow_count,
    badge_count: w.badge_count,
    filter_present: w.filter_present,
    probe: w.first_hollow ?? null,
  });
});

test("ORD-0497 zh 390x844 the arms write their readings", async () => {
  /* The run id belongs in the file name: two earlier artefacts in this project were overwritten by a
     later run that shared the name, which turned "the clean readings" into a reading of something
     else. One file per run, always. */
  writeFileSync(
    evidencePath(
      `qoder-bac10f40-ord0497-footprints-on-map--${process.env.CITYNEXUS_E2E_RUN_ID || "dev"}.json`,
    ),
    JSON.stringify({ file: FILE, viewport: PHONE, arms: 5, rows }, null, 1),
  );
  expect(rows.length).toBeGreaterThanOrEqual(5);
});
