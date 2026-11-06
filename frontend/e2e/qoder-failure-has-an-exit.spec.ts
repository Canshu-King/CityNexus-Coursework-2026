/**
 * ORD-0169 / R14 -- one failure, one classification, four consumers reading it.
 *
 * Measured on the production bundle at 390x844 before this change: `localStorage` held a journey id
 * the server does not have, `GET /journeys/<that id>/experience` answered **404**, and the screen
 * said 「网络不可用 · 正在显示只读的已保存行程」 with `.v8-map-panel` absent from the DOM.  The
 * commander's evidence order matters and is followed here: the same-origin **404** is what disproves
 * "no network response" for that request, the **200** from an unrelated endpoint shows the global
 * banner was over-broad, and `navigator.onLine` is only a side note.
 *
 * R14 requires more than correct-looking copy, so every node checks the machine classification and
 * proves the consumers agree with it:
 *   the view carries `data-attribution-reason` / `evidence-kind` / `http-status` / `source-layer`;
 *   the body text is the copy table's entry for that same reason;
 *   the telemetry record (`window.citynexusAttribution`, same shape R14 §8 asks for) carries the same
 *   reason and evidence kind;
 *   the recovery button is the one the classification names.
 *
 * Both directions are gates, because the cheap way to pass the first one is to widen "not found"
 * until it swallows real network failures:
 *   N1 stale 404  -> JOURNEY_NOT_FOUND (never OFFLINE), id cleared, picker offered, map restored
 *   N2 real transport failure -> NETWORK_UNAVAILABLE (never JOURNEY_NOT_FOUND)
 *   N3 server 5xx -> SERVER_ERROR (never OFFLINE)
 *
 * This file's three `ord0126-*` nodes were replaced by these four, not deleted quietly: the views
 * they addressed (`journey-gone` / `load-failed` / `backend-unreachable`) no longer exist, having
 * been folded into the one classified `failure-view`, and every claim they made is asserted here
 * against the machine classification instead of against prose.  The one claim they carried that the
 * first draft of this file would have dropped -- "a sentence about saved data is only allowed when
 * the device actually holds it" -- is asserted in N2, where that sentence can appear.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-failure-has-an-exit.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

const GHOST = "e833311d-0000-4000-8000-000000000001";
const JOURNEY_KEYS = [
  "citynexus.v8.journey_id",
  "citynexus.demo.journey_id",
  "citynexus.demo.agent_run_id",
];

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

/** What the page is actually showing, as the machine reads it -- not as a human reads it. */
async function classification(page: Page) {
  return page.evaluate(() => {
    const view = document.querySelector<HTMLElement>("[data-testid='failure-view']");
    const line = document.querySelector<HTMLElement>("[data-testid='failure-attribution-line']");
    const records = (window as unknown as { citynexusAttribution?: Array<Record<string, unknown>> })
      .citynexusAttribution ?? [];
    return {
      viewPresent: Boolean(view),
      reason: view?.dataset.attributionReason ?? null,
      evidenceKind: view?.dataset.evidenceKind ?? null,
      httpStatus: view?.dataset.httpStatus ?? null,
      sourceLayer: view?.dataset.sourceLayer ?? null,
      recoveryAction: view?.dataset.recoveryAction ?? null,
      attributionLine: line?.textContent?.trim() ?? null,
      body: (document.body.innerText ?? "").replace(/\s+/g, " ").slice(0, 300),
      telemetry: records.map((item) => ({
        reason: item.reason,
        evidence_kind: item.evidence_kind,
        http_status: item.http_status,
        source_layer: item.source_layer,
        context: item.context,
      })),
      stored: {
        v8: localStorage.getItem("citynexus.v8.journey_id"),
        demo: localStorage.getItem("citynexus.demo.journey_id"),
        run: localStorage.getItem("citynexus.demo.agent_run_id"),
      },
      mapPanel: Boolean(document.querySelector(".v8-map-panel")),
      testIds: Array.from(document.querySelectorAll<HTMLElement>("[data-testid]"))
        .map((el) => el.dataset.testid as string)
        .slice(0, 30),
    };
  });
}

async function focusedTestId(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    return active ? (active.dataset.testid ?? active.id ?? active.tagName.toLowerCase()) : null;
  });
}

/** Seed the stored journey id after the first navigation (an init script would replay on reload). */
async function seedStoredId(page: Page, id: string): Promise<void> {
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate(
    ([journeyId, keys]) => {
      (keys as string[]).forEach((key) => localStorage.removeItem(key));
      localStorage.setItem("citynexus.v8.journey_id", journeyId as string);
      localStorage.setItem("citynexus.locale", "zh-CN");
    },
    [id, JOURNEY_KEYS],
  );
  await page.reload({ waitUntil: "load" });
}

test("ord0169-a-stale-journey-404-is-never-called-a-network-failure — 死 id 的 404 不许说成网络不可用", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0169-a-stale-journey-404-is-never-called-a-network-failure";
  const seeded = await readScene(page);
  await seedStoredId(page, GHOST);

  /* The discriminating evidence, taken in the page itself: the target request answers 404 (so it
     reached the server) while an unrelated endpoint answers 200 (so no global outage). */
  const probes = await page.evaluate(async (id) => {
    const target = await fetch(`/api/v1/journeys/${id}/experience`);
    const other = await fetch("/api/v1/demo/scene");
    return { target: target.status, other: other.status, onLine: navigator.onLine };
  }, GHOST);

  await expect(
    page.getByTestId("failure-view"),
    node(NODE, `the stale id produced no classified failure view at all (probes ${JSON.stringify(probes)})`),
  ).toBeVisible({ timeout: 20_000 });
  const read = await classification(page);
  expect(
    read.reason,
    node(NODE, `the view classifies this as ${JSON.stringify(read.reason)} while the request answered ${probes.target} and /demo/scene answered ${probes.other}`),
  ).toBe("JOURNEY_NOT_FOUND");
  expect(
    read.evidenceKind,
    node(NODE, `the classification rests on ${JSON.stringify(read.evidenceKind)}; a response arrived, so the evidence is HTTP_RESPONSE`),
  ).toBe("HTTP_RESPONSE");
  expect(read.httpStatus, node(NODE, `the view does not carry the status it classified on (got ${JSON.stringify(read.httpStatus)})`)).toBe("404");
  expect(read.sourceLayer, node(NODE, `source_layer is ${JSON.stringify(read.sourceLayer)}, not RESOURCE_STATE`)).toBe("RESOURCE_STATE");
  expect(
    /网络不可用|离线 · 只读|OFFLINE/i.test(read.body),
    node(NODE, `the screen still blames the network: "${read.body}"`),
  ).toBe(false);
  /* Consumer 2: telemetry must record the same conclusion and the evidence it used (R14 §8). */
  const record = read.telemetry.find((item) => item.context === "shell.boot.journeyRead") ?? read.telemetry[0];
  expect(
    record?.reason,
    node(NODE, `telemetry recorded ${JSON.stringify(read.telemetry)} while the view says ${read.reason} -- the four consumers disagree`),
  ).toBe(read.reason);
  expect(
    read.attributionLine ?? "",
    node(NODE, "the view prints no attribution line, so the classification is not inspectable in production"),
  ).toContain(`attribution_reason=${read.reason}`);
  /* Consumer 3: the recovery action is the one this cause calls for. */
  expect(
    read.recoveryAction,
    node(NODE, `recovery_action is ${JSON.stringify(read.recoveryAction)}; a missing journey is recovered by choosing one`),
  ).toBe("choose-journey");
  expect(
    await focusedTestId(page),
    node(NODE, "no recovery control is focusable on this screen"),
  ).not.toBeNull();
  /* Consumer 4 / R14-S: the invalid stored reference is gone, all of it. */
  expect(
    read.stored,
    node(NODE, `the stale reference survives in storage: ${JSON.stringify(read.stored)} -- the next refresh walks into the same 404`),
  ).toEqual({ v8: null, demo: null, run: null });
  expect(
    read.mapPanel,
    node(NODE, "the map panel is absent on this screen; the false-offline branch is still what is rendering"),
  ).toBe(false);

  writeFileSync(
    evidencePath("ui-overnight/ord0169-stale-journey.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", ghost: GHOST, probes, read }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0169-stale-journey.png"), fullPage: true });
});

test("ord0169-the-recovered-shell-brings-the-map-back — 恢复之后地图必须回来", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0169-the-recovered-shell-brings-the-map-back";
  const seeded = await readScene(page);
  const valid = String(seeded.journey_id ?? "");
  await seedStoredId(page, GHOST);
  await expect(page.getByTestId("failure-view")).toBeVisible({ timeout: 20_000 });

  /* Take the exit the classification offered, then load the journey that does exist.  The claim is
     about the recovered state: the normal shell renders and the map is back, which is what makes
     4176e7b's single-finger drag reachable at all. */
  await page.getByTestId("failure-choose-journey").click();
  await expect(
    page.getByTestId("trial-setup"),
    node(NODE, "the choose-journey exit did not reach the trip picker"),
  ).toBeVisible({ timeout: 20_000 });
  await page.evaluate((id) => localStorage.setItem("citynexus.v8.journey_id", id), valid);
  await page.reload({ waitUntil: "load" });
  await expect(
    page.getByTestId("today-hero"),
    node(NODE, `after the stale id was cleared and ${valid.slice(0, 8)}… loaded, the shell did not come back`),
  ).toBeVisible({ timeout: 30_000 });
  const read = await classification(page);
  expect(
    read.mapPanel,
    node(NODE, `the recovered shell has no .v8-map-panel (test ids on screen: ${JSON.stringify(read.testIds)})`),
  ).toBe(true);
  writeFileSync(
    evidencePath("ui-overnight/ord0169-map-restored.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", valid, mapPanel: read.mapPanel, testIds: read.testIds }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0169-map-restored.png"), fullPage: true });
});

test("ord0169-a-real-transport-failure-still-says-offline — 真断网仍须说网络不可用（反向门）", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0169-a-real-transport-failure-still-says-offline";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  /* Both the target read and the health probe are killed at the transport level, so there is no
     response anywhere to classify from -- the only state that may say the network is unavailable. */
  await page.route("**/api/v1/**", async (route) => route.abort("connectionfailed"));
  await seedStoredId(page, journeyId);

  /* Captured before the visibility assertion so a red names the screen that rendered instead of only
     reporting "element not found" -- the difference between "the classification was wrong" and
     "nothing rendered at all" is exactly what a mutation verdict has to distinguish. */
  await page.waitForTimeout(1_500);
  const onScreen = await classification(page);
  expect(
    page.getByTestId("failure-view"),
    node(
      NODE,
      `a total transport failure produced no classified failure view; the page held ${JSON.stringify(
        onScreen.testIds,
      )} and read "${onScreen.body}" (attribution records: ${JSON.stringify(onScreen.telemetry)})`,
    ),
  ).toBeVisible({ timeout: 20_000 });
  const read = await classification(page);
  expect(
    read.reason,
    node(NODE, `the transport failure was classified ${JSON.stringify(read.reason)}; widening "not found" until it swallows network failures is exactly the reverse defect`),
  ).toBe("NETWORK_UNAVAILABLE");
  expect(
    read.evidenceKind,
    node(NODE, `the network verdict rests on ${JSON.stringify(read.evidenceKind)}; with no response it has to be TRANSPORT or HEALTH_PROBE`),
  ).toMatch(/TRANSPORT|HEALTH_PROBE/);
  expect(
    /网络不可用/.test(read.body),
    node(NODE, `a real transport failure no longer says 网络不可用: "${read.body}"`),
  ).toBe(true);
  /* The claim the old ord0126 node carried: a sentence about saved data needs saved data.  Measured
     before the action check because the two share one piece of evidence: whether a copy exists. */
  const storage = await page.evaluate(async () => {
    const databases = indexedDB.databases ? await indexedDB.databases() : [];
    const hit = databases.find((item) => item.name === "citynexus-offline");
    let rows = 0;
    if (hit?.name) {
      rows = await new Promise<number>((resolve) => {
        const open = indexedDB.open(hit.name as string);
        open.onsuccess = () => {
          const db = open.result;
          if (!db.objectStoreNames.contains("journeys")) {
            resolve(0);
            return;
          }
          const count = db.transaction("journeys", "readonly").objectStore("journeys").count();
          count.onsuccess = () => resolve(Number(count.result ?? 0));
          count.onerror = () => resolve(0);
        };
        open.onerror = () => resolve(0);
      });
    }
    return {
      offlineDatabase: hit?.name ?? null,
      journeyRows: rows,
      serviceWorkerController: Boolean(navigator.serviceWorker?.controller),
      cacheNames: "caches" in window ? await caches.keys() : [],
    };
  });
  /* The classification asks for `read-only-cached`; the view may downgrade that to `retry` only when
     the device holds no copy to read.  A button offering a copy that is not there would be the false
     claim in interactive form. */
  const wantsCached = read.recoveryAction === "read-only-cached";
  expect(
    wantsCached || read.recoveryAction === "retry",
    node(NODE, `recovery_action for a network failure is ${JSON.stringify(read.recoveryAction)}; it has to be read-only-cached, or retry when there is nothing cached`),
  ).toBe(true);
  if (!wantsCached) {
    expect(
      storage.journeyRows,
      node(NODE, `the view offers retry instead of the cached copy while the device holds ${storage.journeyRows} row(s) -- it downgraded an action it could have honoured`),
    ).toBe(0);
  }
  const claimedSaved = /已保存/.test(read.body);
  if (claimedSaved) {
    expect(
      storage.journeyRows,
      node(NODE, `the screen claims 已保存 while the device holds ${storage.journeyRows} row(s) in ${storage.offlineDatabase} (controller=${storage.serviceWorkerController}, caches=${JSON.stringify(storage.cacheNames)})`),
    ).toBeGreaterThan(0);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0169-true-offline.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", read, storage, claimedSaved }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0169-true-offline.png"), fullPage: true });
});

test("ord0169-a-server-5xx-is-not-offline — 服务器自己出错不许说成网络问题", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0169-a-server-5xx-is-not-offline";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  await page.route(`**/api/v1/journeys/${journeyId}/experience`, async (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "INTERNAL", message: "gate-stub: runtime failure" } }),
    }),
  );
  await seedStoredId(page, journeyId);

  await expect(
    page.getByTestId("failure-view"),
    node(NODE, "a 500 on the journey read produced no failure view"),
  ).toBeVisible({ timeout: 20_000 });
  const read = await classification(page);
  expect(
    read.reason,
    node(NODE, `a 500 was classified ${JSON.stringify(read.reason)} (R14-R/R14-P: an application error keeps its runtime class)`),
  ).toBe("SERVER_ERROR");
  expect(read.httpStatus, node(NODE, `the view carries http_status ${JSON.stringify(read.httpStatus)} for a 500`)).toBe("500");
  expect(
    read.sourceLayer,
    node(NODE, `source_layer is ${JSON.stringify(read.sourceLayer)}; a server error is RUNTIME`),
  ).toBe("RUNTIME");
  expect(
    /网络不可用/.test(read.body),
    node(NODE, `a server error is being blamed on the network: "${read.body}"`),
  ).toBe(false);
  expect(
    read.recoveryAction,
    node(NODE, `recovery_action for a retryable server error is ${JSON.stringify(read.recoveryAction)}, not retry`),
  ).toBe("retry");
  writeFileSync(
    evidencePath("ui-overnight/ord0169-server-5xx.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", read }, null, 2) + "\n",
    "utf-8",
  );
});
