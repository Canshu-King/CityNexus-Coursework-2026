import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * Domain: time and clock.  One question, asked of the screen: when a moment on Today is anchored to
 * the JOURNEY, can the reader tell which moment it is?
 *
 * Three zones are made to disagree on purpose, so the readings cannot be confused:
 *   device clock  = UTC            (this test pins the browser's timezone)
 *   journey zone  = America/New_York
 *   the instant   = 2026-10-09T18:00:00+08:00
 * The same instant therefore reads 10:00 in UTC and 06:00 in New York.  If the screen renders the
 * device clock, the reader gets 10:00; if it renders the journey zone, 06:00.  Before this arm the
 * screen printed the time with no zone at all, so the two were indistinguishable -- which is the
 * defect this domain exists to catch.
 */

const FILE = "e2e/qoder-a68b0631-time-anchors-carry-their-zone-r1.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const JOURNEY_TZ = "America/New_York";
const INSTANT = "2026-10-09T18:00:00+08:00";   // 10:00 UTC, 06:00 New York

test.use({ timezoneId: "UTC" });

async function patchExperience(page: Page, patch: (body: any) => void) {
  await page.route("**/api/v1/journeys/*/experience", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    let body: any;
    let status = 0;
    try {
      const upstream = await route.fetch();
      status = upstream.status();
      body = await upstream.json();
    } catch {
      return route.fallback();
    }
    if (status !== 200 || !body?.journey) {
      throw new Error(
        node("gate precondition",
          `/experience answered ${status} without a journey payload, so the anchors could not be `
          + "patched. The gate is not measuring the product: " + JSON.stringify(body).slice(0, 200)),
      );
    }
    patch(body);
    try {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    } catch {
      /* the page moved on */
    }
  });
}

async function openHub(page: Page, readyTestIds: string[]) {
  await page.addInitScript(() => {
    if (sessionStorage.getItem("cnx.qoder.time-zone-seeded")) return;
    sessionStorage.setItem("cnx.qoder.time-zone-seeded", "1");
    localStorage.removeItem("citynexus.v8.journey_id");
  });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", readyTestIds);
}

function record(name: string, payload: Record<string, unknown>) {
  const p = evidencePath("time-anchors", name);
  writeFileSync(p, JSON.stringify(payload, null, 2), "utf8");
  return p;
}

test.describe("a journey-anchored moment says which moment it is", () => {
  test("today-when names the journey zone and reads the journey's clock", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    await patchExperience(page, (body) => {
      body.journey.timezone = JOURNEY_TZ;
      body.journey.start_at = INSTANT;
      body.journey.end_at = INSTANT;
      body.trip.arrival_anchor = { at: INSTANT, location: "Xiamen" };
      body.trip.departure_anchor = { at: INSTANT, location: "Xiamen" };
    });
    await openHub(page, ["today-heading", "today-when"]);

    const when = page.getByTestId("today-when");
    const text = (await when.innerText()).replace(/\s+/g, " ").trim();
    record("today-when.json", { text, journeyTz: JOURNEY_TZ, instant: INSTANT, deviceTz: "UTC" });

    // 1. the reader can tell WHICH moment: the zone is named on the screen
    expect(
      text,
      node("today-when-names-the-zone",
        `Today rendered a journey-anchored moment without saying which zone it is: ${JSON.stringify(text)}`),
    ).toContain(JOURNEY_TZ);

    // 2. the moment is the JOURNEY's clock, not the device's: 06:00 New York, never 10:00 UTC
    expect(
      /06:00/.test(text),
      node("today-when-reads-the-journey-clock",
        `the moment was not rendered in the journey's zone (expected 06:00 New York, device is UTC): ${JSON.stringify(text)}`),
    ).toBe(true);
    expect(
      /10:00/.test(text),
      node("today-when-is-not-the-device-clock",
        `the moment was rendered in the DEVICE zone (UTC) while the journey is in New York: ${JSON.stringify(text)}`),
    ).toBe(false);
  });

  test("the arrival strip names the journey zone too", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    await patchExperience(page, (body) => {
      body.journey.timezone = JOURNEY_TZ;
      body.trip.arrival_anchor = { at: INSTANT, location: "Xiamen" };
      body.trip.departure_anchor = { at: INSTANT, location: "Xiamen" };
    });
    await openHub(page, ["today-heading"]);

    const strip = page.locator(".cn-strip-item").filter({ hasText: /到达|Arrival/ }).first();
    const text = (await strip.innerText()).replace(/\s+/g, " ").trim();
    record("arrival-strip.json", { text, journeyTz: JOURNEY_TZ, instant: INSTANT });

    expect(
      text,
      node("arrival-strip-names-the-zone",
        `the arrival anchor was rendered without its zone: ${JSON.stringify(text)}`),
    ).toContain(JOURNEY_TZ);
    expect(
      /06:00/.test(text),
      node("arrival-strip-reads-the-journey-clock",
        `the arrival anchor was not in the journey's zone: ${JSON.stringify(text)}`),
    ).toBe(true);
  });

  test("an unusable zone is said out loud, never claimed", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    const BOGUS = "Mars/Phobos";
    await patchExperience(page, (body) => {
      body.journey.timezone = BOGUS;
      body.journey.start_at = INSTANT;
      body.journey.end_at = INSTANT;
      body.trip.arrival_anchor = { at: INSTANT, location: "Xiamen" };
    });
    await openHub(page, ["today-heading", "today-when"]);

    const text = (await page.getByTestId("today-when").innerText()).replace(/\s+/g, " ").trim();
    record("today-when-unusable-zone.json",
      { text, bogusZone: BOGUS, instant: INSTANT, deviceTz: "UTC" });

    // The reading is the device clock, and the screen has to say so.
    expect(
      text,
      node("unusable-zone-says-device-time",
        `the zone could not be used and the screen did not say the reading is the device clock: ${JSON.stringify(text)}`),
    ).toMatch(/本设备时间|this device's time/);
    // ...and it must NOT claim the zone it did not honour.
    expect(
      text,
      node("unusable-zone-is-not-claimed",
        `the screen named a zone it did not render in: ${JSON.stringify(text)}`),
    ).not.toContain(BOGUS);
    // The device is UTC here, so the moment must read 10:00 -- the fallback really happened.
    expect(
      /10:00/.test(text),
      node("unusable-zone-reads-the-device-clock",
        `the fallback reading was not the device clock: ${JSON.stringify(text)}`),
    ).toBe(true);
  });
});
