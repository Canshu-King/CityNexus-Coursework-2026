import { expect, test } from "@playwright/test";
import { evidencePath } from "./_qoderEvidence";

async function readOfflineSnapshot(page: import("@playwright/test").Page, journeyId: string) {
  return page.evaluate(async (id) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("citynexus-offline");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return new Promise<{ snapshot?: { execution?: { now_state?: string } } } | undefined>((resolve, reject) => {
      const transaction = database.transaction("journeys", "readonly");
      const request = transaction.objectStore("journeys").get(id);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }, journeyId);
}

test("accepted authoritative execution truth converges to Dexie offline truth", async ({ page, request }) => {
  const reset = await request.post("/api/v1/demo/reset");
  const ids = await reset.json();
  const generated = await request.post(`/api/v1/journeys/${ids.journey_id}/plans/generate`, {
    data: { candidate_count: 1 },
  });
  expect(generated.ok()).toBeTruthy();
  const plan = (await generated.json())[0];
  expect((await request.post(`/api/v1/journeys/${ids.journey_id}/plans/${plan.id}/select`)).ok()).toBeTruthy();
  expect((await request.post(`/api/v1/journeys/${ids.journey_id}/start`)).ok()).toBeTruthy();

  await page.addInitScript(
    (journeyId) => localStorage.setItem("citynexus.v8.journey_id", journeyId),
    ids.journey_id,
  );
  await page.goto("/v8/authoritative");
  await expect(page.getByTestId("v12-authoritative-panel")).toBeVisible();
  await expect(page.getByRole("button", { name: "Mark EN ROUTE" })).toBeVisible();
  await page.getByRole("button", { name: "Mark EN ROUTE" }).click();
  await expect(page.getByRole("button", { name: "Mark ARRIVED" })).toBeVisible();

  await expect.poll(async () => {
    const saved = await readOfflineSnapshot(page, ids.journey_id);
    return saved?.snapshot?.execution?.now_state ?? null;
  }).toBe("EN_ROUTE");

  const server = await request.get(`/api/v1/journeys/${ids.journey_id}/offline-snapshot`);
  const serverBody = await server.json();
  expect(serverBody.snapshot.execution.now_state).toBe("EN_ROUTE");
  expect(serverBody.snapshot.active_plan_version_id).toBe(plan.id);

  await page.screenshot({ path: evidencePath("product-v13/authoritative-online-before-offline.png"), fullPage: true });
  await expect.poll(async () => page.evaluate(() => navigator.serviceWorker?.ready.then(() => true))).toBeTruthy();
  await expect.poll(async () => page.evaluate(() => Boolean(navigator.serviceWorker?.controller))).toBeTruthy();
  // The dev server intentionally does not precache its HTML shell.  Simulate
  // the real offline API boundary while retaining the already loaded shell so
  // this regression isolates Dexie convergence rather than Vite transport.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, get: () => false });
    const networkFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
      if (url.includes("/api/v1/")) return Promise.reject(new TypeError("offline"));
      return networkFetch(input, init);
    };
  });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your next 24 hours are saved." })).toBeVisible();
  await expect(page.getByText("NOW · EN ROUTE", { exact: true })).toBeVisible();
  await expect(page.getByText(/Financial actions are disabled offline/)).toBeVisible();
  await page.screenshot({ path: evidencePath("product-v13/authoritative-offline.png"), fullPage: true });
});
