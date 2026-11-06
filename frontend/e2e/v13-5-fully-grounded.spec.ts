import { expect, test } from "@playwright/test";
import { evidencePath } from "./_qoderEvidence";

function reservation(role: "ARRIVAL" | "MID_TRIP" | "DEPARTURE") {
  if (role === "ARRIVAL") {
    return {
      reservation_type: "RAIL",
      provider_key: "v135-browser",
      confirmation_ref: "BROWSER-ARRIVAL",
      origin: { name: "Beijing South", city: "Beijing" },
      destination: { name: "Tianjin West Station", city: "Tianjin", latitude: 39.112, longitude: 117.198 },
      scheduled_departure_at: "2026-10-10T07:55:00+08:00",
      scheduled_arrival_at: "2026-10-10T09:00:00+08:00",
      price_amount_minor: 5500,
      currency: "CNY",
      price_status: "KNOWN",
      anchor_role: role,
    };
  }
  if (role === "DEPARTURE") {
    return {
      reservation_type: "RAIL",
      provider_key: "v135-browser",
      confirmation_ref: "BROWSER-DEPARTURE",
      origin: { name: "Tianjin West Station", city: "Tianjin", latitude: 39.112, longitude: 117.198 },
      destination: { name: "Beijing South", city: "Beijing" },
      scheduled_departure_at: "2026-10-10T18:30:00+08:00",
      scheduled_arrival_at: "2026-10-10T19:00:00+08:00",
      price_amount_minor: 5500,
      currency: "CNY",
      price_status: "KNOWN",
      anchor_role: role,
    };
  }
  return {
    reservation_type: "FERRY",
    provider_key: "v135-browser",
    confirmation_ref: "BROWSER-CRUISE",
    origin: { name: "Haihe Wharf", city: "Tianjin" },
    destination: { name: "Haihe Cruise", city: "Tianjin" },
    scheduled_departure_at: "2026-10-10T15:30:00+08:00",
    scheduled_arrival_at: "2026-10-10T16:30:00+08:00",
    price_amount_minor: 12000,
    currency: "CNY",
    price_status: "KNOWN",
    anchor_role: role,
    semantic_node_key: "place-river-cruise",
  };
}

test("City Discovery renders the fully grounded Journey proof", async ({ page, request }) => {
  const reset = await request.post("/api/v1/demo/reset");
  const { journey_id: journeyId } = await reset.json();
  for (const role of ["ARRIVAL", "MID_TRIP", "DEPARTURE"] as const) {
    const imported = await request.post(`/api/v1/journeys/${journeyId}/inbox/import`, {
      data: { artifact_type: "STRUCTURED_JSON", payload: { reservation: reservation(role) } },
    });
    expect(imported.ok()).toBeTruthy();
    const draft = await imported.json();
    const confirmed = await request.post(`/api/v1/journeys/${journeyId}/inbox/${draft.id}/confirm`);
    expect(confirmed.ok()).toBeTruthy();
  }

  await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
  await page.goto("/v8/discover");
  await expect(page.getByTestId("v13-discovery")).toBeVisible();
  await page.getByTestId("v13-discover-submit").click();
  const result = page.getByTestId("v13-discovery-result");
  await expect(result).toBeVisible();
  await expect(page.getByText("FULLY GROUNDED", { exact: true })).toBeVisible();
  await expect(page.getByText("valhalla", { exact: true })).toBeVisible();
  await expect(page.getByTestId("v13-discovery-selected-plan")).toContainText("SELECTED");
  /* The screenshot is the artefact a human reviews, so it has to be taken once the
     surface has settled.  Measured on this pass: every assertion above passed and
     the shipped PNG still showed an empty page — the card fades in, and
     `toBeVisible()` is true at opacity 0.  Scoped to this subtree on purpose: the
     app runs infinite pulse animations elsewhere, so a document-wide wait could
     never finish. */
  await expect
    .poll(
      () =>
        result.evaluate((element) =>
          element
            .getAnimations({ subtree: true })
            .every((animation) => animation.playState === "finished"),
        ),
      {
        message: "the discovery result never stopped animating, so a capture of it is not evidence",
        timeout: 10_000,
      },
    )
    .toBe(true);
  await page.screenshot({ path: evidencePath("product-v13.5/fully-grounded-discovery.png"), fullPage: true });
});
