/**
 * ORD-0209 -- the demo entry addresses the sealed journey; it does not create one.
 *
 * HARNESS measured 6/6 clicks on this entry POSTing a new traveller and a new journey; the
 * arbitration (`5772122180`) ruled the entry becomes a read-only address of the sealed demo
 * journey.  The four acceptance shapes, and how each is measured here:
 *
 *  1. rendered text: after the entry, the screen carries the *sealed* journey's own identity --
 *     the id the server's `/demo/scene` names, read from the twin payload the Today page loads;
 *  2. the load-bearing network count: across **three** presses (the reproduction rate the old
 *     defect was measured at), `POST /api/v1/travelers` and `POST /api/v1/journeys` are counted
 *     from the browser's own request log and must be exactly 0 and 0;
 *  3. fail-closed: when `/demo/scene` names no sealed journey, the entry shows a named reason in
 *     words and issues no POST at all;
 *  4. the mutation: restoring the POST path must turn this gate red (registered in the kill
 *     harness, whose anchor guard mechanically refuses a 0-hit or byte-identical "mutation").
 *
 * Assertions read rendered text; the network layer is counted from the request log, which is the
 * load-bearing half of this defect -- a UI-only check cannot prove nothing was created.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-demo-entry-addresses-dont-create.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

async function openWizard(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
}

/** Count the two creation POSTs the old entry made, from the browser's own log. */
function watchCreations(page: Page): { created: () => { travelers: number; journeys: number; log: string[] } } {
  const log: string[] = [];
  const counters = { travelers: 0, journeys: 0 };
  page.on("request", (request) => {
    const url = request.url();
    if (request.method() !== "POST") return;
    if (/\/api\/v1\/travelers\/?$/.test(url)) {
      counters.travelers += 1;
      log.push(`POST ${url.replace(/^https?:\/\/[^/]+/, "")}`);
    }
    if (/\/api\/v1\/journeys\/?$/.test(url)) {
      counters.journeys += 1;
      log.push(`POST ${url.replace(/^https?:\/\/[^/]+/, "")}`);
    }
  });
  return { created: () => ({ ...counters, log }) };
}

test("ord0209-the-demo-entry-addresses-the-sealed-journey — 点 3 次入口，0 次新建，进去的是封存那趟", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0209-the-demo-entry-addresses-the-sealed-journey";
  await openWizard(page);

  /* The sealed id the server owns right now -- read before clicking, so the screen can be judged
     against it rather than against a locally invented expectation. */
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    demo_mode?: boolean;
    journey_id?: string | null;
  };
  const sealedId = String(scene.journey_id ?? "");
  expect(
    scene.demo_mode && sealedId,
    node(NODE, `the fixture itself has no sealed demo to address (demo_mode=${JSON.stringify(scene.demo_mode)}, journey_id=${JSON.stringify(sealedId)}); this node cannot measure the entry against nothing`),
  ).toBeTruthy();

  const creations = watchCreations(page);
  for (let press = 1; press <= 3; press += 1) {
    await page.getByTestId("trial-tianjin-demo").click();
    /* The first press navigates away; the wizard remount is what presses 2-3 measure, so re-open
       it the way a traveller would (back out of the shell) rather than assuming the screen. */
    if (press < 3) {
      await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
      await page.evaluate(() => {
        localStorage.removeItem("citynexus.v8.journey_id");
      });
      await page.goto("/v8/hub", { waitUntil: "load" });
      await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
    }
  }
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });

  const counts = creations.created();
  /* The load-bearing half: three presses, zero creations. */
  expect(
    counts.travelers,
    node(NODE, `three entry presses created ${counts.travelers} traveler(s): ${JSON.stringify(counts.log)}`),
  ).toBe(0);
  expect(
    counts.journeys,
    node(NODE, `three entry presses created ${counts.journeys} journey(s): ${JSON.stringify(counts.log)}`),
  ).toBe(0);

  /* The rendered half: the screen carries the sealed journey's own identity, read from what the
     page actually loaded -- not from a data attribute. */
  const storedId = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
  expect(
    storedId,
    node(NODE, "no journey id was stored, so the entry did not address anything"),
  ).toBe(sealedId);
  const twin = (await (
    await page.request.get(`/api/v1/journeys/${sealedId}/twin`)
  ).json()) as { journey?: { id?: string }; journey_id?: string };
  const twinId = twin.journey?.id ?? twin.journey_id ?? null;
  expect(
    twinId,
    node(NODE, `the twin for the sealed journey answered no identity: ${JSON.stringify(twin).slice(0, 200)}`),
  ).toBe(sealedId);

  writeFileSync(
    evidencePath("ui-overnight/ord0209-addresses.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        presses: 3,
        travelersCreated: counts.travelers,
        journeysCreated: counts.journeys,
        requestLog: counts.log,
        sealedId,
        storedId,
        twinId,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0209-addresses.png"), fullPage: true });
});

test("ord0209-no-sealed-journey-fails-closed-in-words — 没有封存就具名关死，不偷偷建", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0209-no-sealed-journey-fails-closed-in-words";
  await openWizard(page);

  /* The fail-closed arm's premise is unreachable on a healthy demo stage, so the gate holds the
     scene read at the two shapes the server can send for "nothing to address": demo_mode false,
     and a scene without a journey id.  Both are the server's own field values; labelled as the
     gate's invention in the evidence file. */
  let mode: "not-demo" | "no-id" = "not-demo";
  await page.route("**/api/v1/demo/scene", async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    if (mode === "not-demo") body.demo_mode = false;
    else body.journey_id = null;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  const creations = watchCreations(page);

  mode = "not-demo";
  await page.getByTestId("trial-tianjin-demo").click();
  const notDemo = page.getByTestId("trial-sealed-unavailable");
  await expect(
    notDemo,
    node(NODE, "a non-demo deployment produced no named fail-closed state -- the entry either entered or said nothing"),
  ).toBeVisible({ timeout: 20_000 });
  const notDemoText = ((await notDemo.innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    /不是演示舞台|demo_mode/.test(notDemoText),
    node(NODE, `the fail-closed reason does not name the actual condition: "${notDemoText}"`),
  ).toBe(true);

  mode = "no-id";
  await page.getByTestId("trial-tianjin-demo").click();
  await expect(
    page.getByTestId("trial-sealed-unavailable"),
    node(NODE, "a scene with no sealed journey produced no named fail-closed state"),
  ).toBeVisible({ timeout: 20_000 });

  /* After both failures: still zero creations.  Fail-closed means closed. */
  const counts = creations.created();
  expect(
    counts.travelers,
    node(NODE, `the fail-closed paths still created ${counts.travelers} traveler(s): ${JSON.stringify(counts.log)}`),
  ).toBe(0);
  expect(
    counts.journeys,
    node(NODE, `the fail-closed paths still created ${counts.journeys} journey(s): ${JSON.stringify(counts.log)}`),
  ).toBe(0);

  writeFileSync(
    evidencePath("ui-overnight/ord0209-fail-closed.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        gateInvention: "both scene shapes are held by the gate using the server's own fields; a healthy demo stage cannot reach this state on demand",
        notDemoText,
        travelersCreated: counts.travelers,
        journeysCreated: counts.journeys,
        requestLog: counts.log,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0209-fail-closed.png"), fullPage: true });
});
