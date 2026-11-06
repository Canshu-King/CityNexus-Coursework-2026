import { expect, test } from "@playwright/test";
import { evidencePath } from "./_qoderEvidence";

test("real-world trip operations: inbox to luggage-aware autonomous recovery", async ({ page, request }, testInfo) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  /* This spec exercises the phone-sized product (the desktop bar has no Trips
     entry at all), so it has to say so — the mobile navigation only exists at
     mobile widths, which is why nav-trips was present but never actionable. */
  await page.setViewportSize({ width: 390, height: 844 });
  const reset = await request.post("/api/v1/demo/reset");
  const ids = await reset.json();
  const created = await request.post("/api/v1/journeys", {
    data: {
      traveler_id: ids.traveler_id,
      title: "Tianjin rail arrival · real trip operations",
      city_code: "CN-TIANJIN",
      timezone: "Asia/Shanghai",
      start_at: "2026-10-09T08:00:00+08:00",
      end_at: "2026-10-11T15:00:00+08:00",
      budget: { amount_minor: 120000, currency: "CNY" },
      stay_budget: { amount_minor: 70000, currency: "CNY" },
      party_size: 2,
      occupancy: { adults: 2, rooms: 1 },
      arrival_anchor: { at: "2026-10-09T08:00:00+08:00", location: "Tianjin West Station" },
      departure_anchor: { at: "2026-10-11T15:00:00+08:00", location: "Tianjin Binhai Airport" },
    },
  });
  expect(created.ok()).toBeTruthy();
  const journey = await created.json();
  await request.put(`/api/v1/journeys/${journey.id}/operations/luggage`, {
    data: { baggage_present: true, baggage_size_class: "MEDIUM", luggage_drop_required: true },
  });
  await page.context().grantPermissions(["geolocation"], { origin: testInfo.project.use.baseURL ?? "http://127.0.0.1:4173" });
  await page.context().setGeolocation({ latitude: 39.1366, longitude: 117.1696, accuracy: 12 });

  await page.addInitScript((journeyId) => localStorage.setItem("citynexus.v8.journey_id", journeyId), journey.id);
  /* pass-12 C. The old step filled textarea#reservation-json and pressed
     "Review and protect". That control still exists but is now deliberately
     folded away: src/app/V8Experience.tsx keeps JourneyInboxWorkbench as the
     consumer booking path and puts the raw-JSON workbench behind
     Trips -> Bookings -> "Advanced / developer". Passing the fixture through the
     developer back door would make Playwright happy and prove nothing about the
     product, so the import now goes through the consumer form instead. */
  await page.goto("/v8/hub");
  await page.getByTestId("nav-trips").click();
  await page.getByRole("button", { name: "Bookings" }).click();
  await page.getByTestId("inbox-tab-manual").click();
  await page.getByTestId("manual-type").selectOption("RAIL");
  await page.getByTestId("manual-title").fill("Shanghai Hongqiao → Tianjin West Station");
  await page.getByTestId("manual-ref").fill("G12345-V9-UI");
  await page.getByTestId("manual-from").fill("Shanghai Hongqiao");
  await page.getByTestId("manual-to").fill("Tianjin West Station");
  await page.getByTestId("manual-depart").fill("2026-10-09T03:30");
  await page.getByTestId("manual-arrive").fill("2026-10-09T08:00");
  await page.getByTestId("manual-amount").fill("553");
  await page.getByTestId("manual-submit").click();
  await expect(page.getByTestId("v10-inbox-draft")).toBeVisible();
  await page.getByTestId("v13-2-confirm").click();
  await expect(page.getByText("Confirmed and protected on your trip.")).toBeVisible();
  /* The wallet list itself still lives under the Advanced disclosure; reaching a
     read-only summary is not the same as importing through a developer back
     door, so the downstream assertion is kept and only its path is disclosed. */
  await page.locator("details.trips-advanced > summary").click();
  await expect(page.getByTestId("wallet-reservation")).toContainText("G12345-V9-UI");

  /* The rest of this chain (Intelligence -> enable reversible recovery ->
     Live Journey) is desktop-only by the product's own route table:
     v8Routes.ts puts "intelligence" under the SYSTEM group of desktopRoutes and
     it is not in mobileRoutes at all. Switching width here is not a workaround —
     it is the spec following the product's actual surface split, and it keeps
     the luggage-aware recovery assertions alive, which is what this file exists
     to protect. */
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/v8/hub");

  const generated = await request.post(`/api/v1/journeys/${journey.id}/stay-options/generate`);
  expect(generated.ok()).toBeTruthy();
  await page.goto("/v8/stay");
  await page.getByTestId("stay-offer-card").first().click();
  await expect(page.getByText("Stay selected · Journey impact recalculated")).toBeVisible();

  await page.getByRole("button", { name: "Intelligence" }).click();
  await page.getByRole("button", { name: "Enable reversible recovery" }).click();
  await expect(page.getByText("Autonomous recovery is enabled")).toBeVisible();
  await page.getByRole("button", { name: "Live Journey" }).click();
  await expect(page.getByText(/Provider-backed route/)).toContainText("WGS84");
  await expect(page.getByRole("heading", { name: /Luggage drop/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "NAVIGATE →" })).toHaveAttribute(
    "href",
    /uri\.amap\.com\/navigation\?to=.*coordinate=wgs84/,
  );
  await page.getByRole("button", { name: "Update location" }).click();
  await expect(page.getByText(/Location updated · accuracy 12 m/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Your location/ })).toBeVisible();
  await page.getByRole("button", { name: "Rain detected" }).click();
  await expect(page.getByText(/Autopilot recovered the exposed leg/)).toBeVisible();

  const experience = await request.get(`/api/v1/journeys/${journey.id}/experience`);
  const body = await experience.json();
  expect(body.active_plan.id).toBeTruthy();
  expect(body.active_plan.legs.length).toBe(body.active_plan.activities.length - 1);
  expect(body.active_plan.activities.map((item: { metadata: { role: string } }) => item.metadata.role)).toEqual(expect.arrayContaining(["LUGGAGE_DROP", "CHECK_IN"]));

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("NOW", { exact: true })).toBeVisible();
  await expect(page.getByText("NEXT · LEAVE BY", { exact: true })).toBeVisible();
  await expect(page.locator(".v9-map-marker").first()).toBeVisible();
  for (const button of await page.getByRole("navigation", { name: "Mobile navigation" }).getByRole("button").all()) {
    const box = await button.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  const navigateBox = await page.getByRole("link", { name: "NAVIGATE →" }).boundingBox();
  expect(navigateBox?.height).toBeGreaterThanOrEqual(44);
  const contrast = await page.getByTestId("live-journey-current").evaluate((element) => {
    const parse = (value: string) => value.match(/\d+/g)?.slice(0, 3).map(Number) ?? [0, 0, 0];
    const luminance = (rgb: number[]) => rgb.map((value) => value / 255).map((value) => value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const style = getComputedStyle(element);
    const foreground = luminance(parse(style.color));
    const background = luminance(parse(getComputedStyle(element.closest(".v8-live-now")!).backgroundColor));
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
  await page.screenshot({ path: evidencePath("product-v9/ui/live-journey-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Switch language" }).click();
  /* The old expectation looked for 旅行票夹 in the mobile bar. That string is
     still real — it is zh nav.bookings, the desktop "Travel Wallet" entry — but
     it left the phone surface when the bottom bar was re-cut to mobileRoutes
     (src/app/V8Experience.tsx renders `mobile.trips` / `mobile.today` there,
     zh 行程 / 今天). The invariant this line exists to protect is "switching
     language localises the navigation the traveler can actually see", so it now
     checks the current zh labels and the absence of the English one. */
  await expect(page.getByRole("navigation", { name: "Mobile navigation" }).getByText("行程")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Mobile navigation" }).getByText("今天")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Mobile navigation" }).getByText("Today")).toHaveCount(0);

  const manifest = await request.get("/manifest.webmanifest");
  expect(manifest.ok()).toBeTruthy();
  expect((await manifest.json()).display).toBe("standalone");
  await expect.poll(async () => page.evaluate(() => navigator.serviceWorker?.ready.then(() => true))).toBeTruthy();
  await page.reload();
  await expect(page.getByTestId("v8-live-journey")).toBeVisible();
  await expect.poll(async () => page.evaluate(() => Boolean(navigator.serviceWorker?.controller))).toBeTruthy();
  await page.context().setOffline(true);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your next 24 hours are saved." })).toBeVisible();
  await expect(page.getByText(/Financial actions are disabled offline/)).toBeVisible();
  await page.screenshot({ path: evidencePath("product-v9/ui/offline-journey-mobile.png"), fullPage: true });
  await page.context().setOffline(false);
});
