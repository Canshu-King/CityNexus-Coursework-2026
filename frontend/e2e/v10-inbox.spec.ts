import { expect, test } from "@playwright/test";
import { createFreshJourney, useJourney } from "./_qoderFreshJourney";

test("V10 Journey Inbox exposes text, image, and deterministic code paths", async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const { journeyId } = await createFreshJourney(request);
  await useJourney(page, journeyId);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/bookings");
  await expect(page.getByRole("heading", { name: "Your real reservations, protected." })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Bring a confirmation into the Journey" })).toBeVisible();
  await expect(page.getByLabel("Confirmation screenshot")).toHaveAttribute("accept", "image/*");
  await expect(page.getByRole("button", { name: "Open scanner" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Extract text draft" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Extract image draft", exact: true })).toBeDisabled();
  await page.getByLabel("Pasted confirmation").fill("Train G12345 from Shanghai to Tianjin");
  await expect(page.getByRole("button", { name: "Extract text draft" })).toBeEnabled();
});
