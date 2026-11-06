/**
 * ORD-0570 (lane 6a150e1e) -- the topbar avatar must say whose door it is, and its letters must not
 * be cut. Read on 360 / 390 / 412 against this lane's own stack.
 *
 * The shape of the judgement, and why it is not circular:
 *  - the accessible name is asserted with `toHaveAccessibleName`, which computes the name the way an
 *    AT reads it -- not `getAttribute("aria-label")`, which a hard-coded attribute could satisfy while
 *    the button still read as nothing to a screen reader.
 *  - the words the name must contain are not written in this file. A1 opens the door first, reads the
 *    destination's own rendered heading off the screen, and then requires the name to say that. So the
 *    label is judged against the mechanism it claims, and a label invented next to the assertion cannot
 *    pass.
 *  - the glyph is measured with `Range.getClientRects()` on the text node plus `scrollWidth` against
 *    `clientWidth`, because "the two letters fit" is a geometry fact, and `textContent` is not evidence
 *    of it.
 *  - A5 is the regression this fix could cause: stopping a flex child from shrinking can make the row
 *    wider than the phone. The arm reads the row's own overflow at three widths.
 *
 * The world is this lane's own store (`spend_day24.db`, seeded by `spend_seed_day24_r1.py`), journey
 * `b25faac4…` with `city_code = CN-XIAMEN`, so the avatar's visible letters are `XI`.
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";

const SEED = JSON.parse(
  readFileSync("D:/moodle/_qoder_6a_ord0531_20261001/spend_seed_day24_report.json", "utf8"),
) as { journeys: { bare: string; priced: string } };

const EVIDENCE = "D:/moodle/_qoder_6a_ord0531_20261001/e2e_ord0570";
const readings: Record<string, unknown> = {};

test.setTimeout(180_000);

function note(name: string, body: unknown) {
  readings[name] = body;
  console.log(`AVT ${name} ${JSON.stringify(body)}`);
}

async function openTopbar(page: Page, width: number) {
  await page.setViewportSize({ width, height: 844 });
  await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id),
    SEED.journeys.priced);
  await page.goto("/v8/trips", { waitUntil: "load" });
  await expect(page.getByTestId("trips-tabs"), "the shell must mount before the avatar means anything")
    .toBeVisible({ timeout: 30_000 });
  const avatar = page.locator(".v8-avatar").first();
  await expect(avatar, "the topbar avatar must be on screen at this width").toBeVisible({ timeout: 20_000 });
  return avatar;
}

/** The box, the letters inside it, and whether anything hangs out. */
async function measureAvatar(page: Page, width: number) {
  const avatar = await openTopbar(page, width);
  const box = await avatar.evaluate((el) => {
    const rect = el.getBoundingClientRect();
    const text = [...el.childNodes].find((n) => n.nodeType === Node.TEXT_NODE
      && (n.textContent ?? "").trim().length > 0);
    let glyphs = null as null | { x: number; w: number; h: number };
    if (text) {
      const range = document.createRange();
      range.selectNodeContents(text);
      const r = range.getClientRects()[0];
      if (r) glyphs = { x: r.x, w: r.width, h: r.height };
    }
    const styles = getComputedStyle(el);
    return {
      visible_text: (el.textContent ?? "").trim(),
      box: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
      glyphs,
      scroll_width: el.scrollWidth,
      client_width: el.clientWidth,
      flex_shrink: styles.flexShrink,
      overflow_x: styles.overflowX,
      row: (() => {
        const row = el.parentElement;
        if (!row) return null;
        return { scroll_width: row.scrollWidth, client_width: row.clientWidth };
      })(),
      document_overflow: (() => {
        const root = document.scrollingElement ?? document.documentElement;
        return { scroll_width: root.scrollWidth, client_width: root.clientWidth };
      })(),
    };
  });
  return { avatar, box };
}

test.describe("ORD-0570 the topbar avatar names its door and keeps its letters", () => {
  test("A1 the accessible name says the door the click actually opens", async ({ page }) => {
    /* Where the expected words come from: the bottom nav entry for the same route, read off this very
       screen. Nothing about the label is written in this file, so a label invented next to the
       assertion cannot pass, and a label that only repeats the two visible letters fails. */
    await page.setViewportSize({ width: 390, height: 844 });
    await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id),
      SEED.journeys.priced);
    await page.goto("/v8/trips", { waitUntil: "load" });
    await expect(page.getByTestId("trips-tabs")).toBeVisible({ timeout: 30_000 });
    const navMe = page.getByTestId("nav-me");
    await expect(navMe, "the route the avatar opens must have its own word in the bottom nav")
      .toBeVisible({ timeout: 20_000 });
    const doorWord = (await navMe.innerText()).trim();
    const avatar = page.locator(".v8-avatar").first();
    await expect(avatar).toBeVisible({ timeout: 20_000 });
    const visibleLetters = (await avatar.innerText()).trim();
    const tripsHeading = (await page.locator(".v8-page h1").first().innerText()).trim();

    /* Same door, two handles: clicking the avatar and clicking the nav entry must land on one screen.
       The heading has to have CHANGED first -- the first version of this arm read the heading without
       waiting for the route, got the trips heading twice, and "same door" passed while nothing navigated. */
    await avatar.click();
    await expect(page.locator(".v8-page h1").first(), "the click must actually change screens")
      .not.toHaveText(tripsHeading, { timeout: 20_000 });
    await expect(page.locator(".v8-page h1").first()).toBeVisible({ timeout: 20_000 });
    const viaAvatar = (await page.locator(".v8-page h1").first().innerText()).trim();
    await page.goto("/v8/trips", { waitUntil: "load" });
    await expect(page.getByTestId("trips-tabs")).toBeVisible({ timeout: 30_000 });
    await page.getByTestId("nav-me").click();
    await expect(page.locator(".v8-page h1").first(), "the nav entry must change screens too")
      .not.toHaveText(tripsHeading, { timeout: 20_000 });
    const viaNav = (await page.locator(".v8-page h1").first().innerText()).trim();
    await page.goto("/v8/trips", { waitUntil: "load" });
    await expect(page.getByTestId("trips-tabs")).toBeVisible({ timeout: 30_000 });
    const back = page.locator(".v8-avatar").first();
    await expect(back).toBeVisible({ timeout: 20_000 });

    note("A1_name", { trips_heading_before: tripsHeading, door_word_in_bottom_nav: doorWord, visible_letters: visibleLetters,
      heading_via_avatar: viaAvatar, heading_via_nav: viaNav,
      aria_label_attribute: await back.evaluate((el) => el.getAttribute("aria-label")),
      url: page.url() });

    expect(viaAvatar.length, "the screen the avatar opens has its own heading").toBeGreaterThan(0);
    expect(viaAvatar, "and it is not the trips screen -- the click really navigated").not.toBe(tripsHeading);
    expect(viaAvatar, "the avatar and the nav entry must be the same door").toBe(viaNav);
    expect(doorWord.length).toBeGreaterThan(0);
    /* The judgement is the computed name, not the attribute: an empty one is the defect this order is. */
    await expect(back, "the avatar must carry an accessible name, not just two letters")
      .not.toHaveAccessibleName(visibleLetters);
    await expect(back, "and that name must be the product's own word for the screen it opens")
      .toHaveAccessibleName(doorWord);
    await page.screenshot({ path: `${EVIDENCE}/A1_topbar_avatar.png` });
  });

  test("A2 the two letters are inside the circle at 360 / 390 / 412", async ({ page }) => {
    const perWidth: Record<string, unknown> = {};
    for (const width of [360, 390, 412]) {
      const { avatar, box } = await measureAvatar(page, width);
      perWidth[String(width)] = box;
      /* Readings are recorded before the judgement, so a red arm still leaves the number behind. */
      note(`A2_geometry_at_${width}`, box);
      expect(box.visible_text, `${width}px: the avatar shows its letters`)
        .not.toBe("");
      expect(box.scroll_width, `${width}px: nothing hangs out of the circle`)
        .toBeLessThanOrEqual(box.client_width);
      if (box.glyphs) {
        expect(box.glyphs.w, `${width}px: the glyph run is no wider than the box`)
          .toBeLessThanOrEqual(box.box.w + 0.5);
        expect(box.glyphs.x, `${width}px: the glyph run starts inside the box`)
          .toBeGreaterThanOrEqual(box.box.x - 0.5);
      }
      await expect(avatar, `${width}px: the circle is not squeezed below its own base`)
        .toHaveCSS("flex-shrink", "0");
      await page.screenshot({ path: `${EVIDENCE}/A2_avatar_${width}.png` });
    }
    note("A2_geometry_all_widths", perWidth);
  });

  test("A5 the row does not overflow the phone once the avatar stops shrinking", async ({ page }) => {
    const perWidth: Record<string, unknown> = {};
    for (const width of [360, 390, 412]) {
      await page.setViewportSize({ width, height: 844 });
      await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id),
        SEED.journeys.priced);
      await page.goto("/v8/trips", { waitUntil: "load" });
      await expect(page.getByTestId("trips-tabs")).toBeVisible({ timeout: 30_000 });
      const numbers = await page.evaluate(() => {
        const root = document.scrollingElement ?? document.documentElement;
        const avatar = document.querySelector(".v8-avatar");
        const row = avatar?.parentElement;
        return {
          document_scroll_width: root.scrollWidth,
          document_client_width: root.clientWidth,
          row_scroll_width: row?.scrollWidth ?? null,
          row_client_width: row?.clientWidth ?? null,
        };
      });
      perWidth[String(width)] = numbers;
      note(`A5_overflow_at_${width}`, numbers);
      expect(numbers.document_scroll_width, `${width}px: no horizontal page overflow`)
        .toBeLessThanOrEqual(numbers.document_client_width);
      if (numbers.row_scroll_width !== null && numbers.row_client_width !== null) {
        expect(numbers.row_scroll_width, `${width}px: the topbar row itself does not overflow`)
          .toBeLessThanOrEqual(numbers.row_client_width);
      }
    }
    note("A5_overflow_matrix", perWidth);
  });

  test("A9 write the readings file", async () => {
    writeFileSync(`${EVIDENCE}/ord0570_readings.json`, JSON.stringify(readings, null, 1), "utf8");
  });
});
