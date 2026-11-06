/**
 * ORD-0180 / ORD-0183 -- the review actions, the temporary bridge, and what may not be inferred.
 *
 * ORD-0180 ①: the panel's review actions go to the endpoints the product already has
 * (`/replans/{id}/accept` and `/reject`), and the accept **must** carry an `Idempotency-Key` -- the
 * measured convention, not an optional extra.  ORD-0180 ④(甲) adds the other half: replaying the same
 * key must not produce a second change.
 *
 * ORD-0183 ①: the client-side status filter is a bridge and has to be **findable and removable** --
 * the literal string `TEMPORARY-CLIENT-FILTER-ORD-0180` marks it in the code, and this gate fails if
 * the marker is gone while the filter is still there (the reverse: marker present with the request
 * now filtering server-side is checked in the same place).
 * ORD-0183 ②: a row appearing in that filtered list is **view semantics, not an authorisation
 * token** -- so the gate proves the mutation still goes to the server and that a server refusal is
 * what the screen reports, rather than the screen deciding it may proceed from list membership.
 *
 * Reads only what the product exposes: the request the panel actually sends (via the request log) and
 * the response the server actually gives.
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-replan-review-actions.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
const MARKER = "TEMPORARY-CLIENT-FILTER-ORD-0180";

test.use({ viewport: VIEWPORT, hasTouch: true });

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

async function openToday(page: Page, journeyId: string): Promise<void> {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
}

async function openStageEntry(page: Page, label: string): Promise<void> {
  const ball = page.getByTestId("sim-ball");
  await ball.scrollIntoViewIfNeeded();
  await ball.click();
  await expect(
    page.getByTestId("sim-panel"),
    node("stage-entry", `the stage window did not open for ${label}`),
  ).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(800);
}

/** A replan needs a triggering event, so raise one first, then ask for the replan and wait. */
async function requestReplan(page: Page, journeyId: string): Promise<{ proposals: number; steps: string[]; answers: Array<{ url: string; status: number; body: string }>; rawStatuses: string[] }> {
  const steps: string[] = [];
  /* The stage's own answers, not just the fact that the tap was allowed: a row that is pressable and
     a server that refuses both look the same from the outside unless the response is kept. */
  const answers: Array<{ url: string; status: number; body: string }> = [];
  const listener = (response: import("@playwright/test").Response) => {
    if (/\/replans$/.test(response.url())) {
      void response
        .text()
        .then((body) => answers.push({ url: response.url().replace(/^https?:\/\/[^/]+/, ""), status: response.status(), body: body.slice(0, 200) }))
        .catch(() => undefined);
    }
  };
  page.on("response", listener);
  const press = async (testId: string) => {
    const row = page.getByTestId(testId);
    await row.scrollIntoViewIfNeeded();
    const disabled = await row.getAttribute("aria-disabled");
    steps.push(`${testId}:aria-disabled=${disabled ?? "null"}`);
    if (disabled === "true") return false;
    await row.click();
    await page.waitForTimeout(2_500);
    return true;
  };
  await press("sim-action-event");
  await press("sim-action-replan");
  const listed = await page.request.get(`/api/v1/journeys/${journeyId}/replans`);
  const proposals = ((await listed.json()) as Array<{ status?: string }>) ?? [];
  page.off("response", listener);
  return {
    answers,
    rawStatuses: proposals.map((item) => String(item.status ?? "")),
    proposals: proposals.filter(
      (item) =>
        !["ACCEPTED", "REJECTED", "DECLINED", "SUPERSEDED"].includes(
          String(item.status ?? "").toUpperCase(),
        ),
    ).length,
    steps,
  };
}

test("ord0180-the-bridge-is-marked-and-presence-is-not-permission — 过渡桥有标记，列表出现不等于获准", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0180-the-bridge-is-marked-and-presence-is-not-permission";

  /* ORD-0183 ①: the bridge must be discoverable in the code.  Read from disk rather than trusted:
     a marker in a comment is the only thing that makes it removable later, and this file is the
     reader the order asks for. */
  const source = readFileSync(
    "D:/moodle/_qoder_integration_20260919/frontend/src/features/simulation/SimulationPanel.tsx",
    "utf-8",
  );
  const hasMarker = source.includes(MARKER);
  const stillFiltersClientSide = source.includes("openProposals = (replans.data ?? []).filter(");
  expect(
    hasMarker,
    node(NODE, `the client-side status filter is present (${stillFiltersClientSide}) without the ${MARKER} marker -- an unmarked shim becomes the next person's contract`),
  ).toBe(true);
  expect(
    hasMarker && stillFiltersClientSide,
    node(NODE, `the ${MARKER} marker is present but the client filter is gone; the marker has to go in the same change or it becomes a lie`),
  ).toBe(true);

  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  await openToday(page, journeyId);
  await openStageEntry(page, "the review rows");
  /* The replan rows only exist once the stage has an open proposal; the helper raises the trigger
     first and reports which step blocked, so a run that measured nothing cannot read as green. */
  const staged = await requestReplan(page, journeyId);

  /* What the panel sends, recorded from the browser's own request log. */
  const sent: Array<{ url: string; method: string; key: string | null }> = [];
  page.on("request", (request) => {
    const url = request.url();
    if (!/\/replans\/[^/]+\/(accept|reject)/.test(url)) return;
    sent.push({
      url: url.replace(/^https?:\/\/[^/]+/, ""),
      method: request.method(),
      key: request.headers()["idempotency-key"] ?? null,
    });
  });

  const accept = page.getByTestId("sim-action-accept");
  await accept.scrollIntoViewIfNeeded();
  const blocked = (await accept.getAttribute("aria-disabled")) === "true";
  const listed = await page.request.get(`/api/v1/journeys/${journeyId}/replans`);
  const proposals = ((await listed.json()) as Array<{ id: string; status: string }>) ?? [];
  const open = proposals.filter(
    (item) => !["ACCEPTED", "REJECTED", "DECLINED", "SUPERSEDED"].includes(String(item.status ?? "").toUpperCase()),
  );
  writeFileSync(
    evidencePath("ui-overnight/ord0180-bridge.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        marker: MARKER,
        markerPresent: hasMarker,
        clientFilterPresent: stillFiltersClientSide,
        proposals: proposals.length,
        openProposals: open.length,
        acceptBlocked: blocked,
        staged,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );

  /* This fixture cannot produce a real proposal: POST /replans is refused by the server itself with
     422 MOBILITY_DATA_UNAVAILABLE (captured in `staged.answers`), which is an environment condition
     and not something this lane may paper over.  So the two things that ARE the panel's own are
     measured directly, and the server-side half is named as unmeasured rather than implied:
       (i)  the accept request carries an Idempotency-Key and the same key across retries, which is
            what makes a replay a replay instead of a second acceptance;
       (ii) a row merely *appearing* in the list does not authorise the action -- with the list
            answered by the gate, the real server still refuses, and the screen must report that
            refusal rather than proceed (ORD-0183 2). */
  await page.route(`**/api/v1/journeys/${journeyId}/replans`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const real = await route.fetch();
    const rows = (await real.json()) as Array<Record<string, unknown>>;
    /* The gate's invention, labelled: one open-looking row, so the accept action has a subject. */
    const stubbed = rows.length
      ? rows
      : [
          {
            id: "gate-stub-proposal-0001",
            journey_id: journeyId,
            status: "PRESENTED",
            created_at: new Date().toISOString(),
          },
        ];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(stubbed),
    });
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await openStageEntry(page, "the stubbed review rows");

  const acceptRow = page.getByTestId("sim-action-accept");
  await acceptRow.scrollIntoViewIfNeeded();
  expect(
    await acceptRow.getAttribute("aria-disabled"),
    node(NODE, "the list shows an open proposal and the accept row is still blocked -- the row is not following the list it reads"),
  ).toBeNull();

  await acceptRow.click();
  await page.waitForTimeout(1_500);
  const outcomeAfterFirst = (await page.getByTestId("sim-outcome").innerText().catch(() => ""))
    .replace(/\s+/g, " ");
  /* Second attempt on the same row (it stays offered because the server refused): the panel must
     resend the SAME key, or the second tap is a different request wearing the same intent. */
  if ((await acceptRow.getAttribute("aria-disabled")) === null) {
    await acceptRow.click();
    await page.waitForTimeout(1_500);
  }
  const keys = sent.filter((item) => item.url.includes("/accept")).map((item) => item.key);

  writeFileSync(
    evidencePath("ui-overnight/ord0180-idempotent-accept.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        proposalIdUsed: "gate-stub-proposal-0001",
        sentAcceptRequests: keys,
        distinctKeys: Array.from(new Set(keys)),
        outcomeAfterFirst,
        notMeasured:
          "the server-side replay invariant (same Idempotency-Key produces no second change) could not be measured on this fixture: POST /replans is refused 422 MOBILITY_DATA_UNAVAILABLE, so no real proposal exists to accept.  Measured instead: the panel sends a key and keeps it stable across retries, and it reports the server's refusal rather than treating list membership as authority.",
        stageRefusal: staged.answers.filter((item) => item.status !== 200),
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );

  expect(
    keys.length,
    node(NODE, `the accept row was pressable and no accept request went out: ${JSON.stringify(sent)}`),
  ).toBeGreaterThan(0);
  expect(
    keys.every((key) => typeof key === "string" && key.length > 0),
    node(NODE, `an accept request went out without an Idempotency-Key: ${JSON.stringify(sent)}`),
  ).toBe(true);
  expect(
    Array.from(new Set(keys)).length,
    node(NODE, `the row minted a fresh key per tap, so a replay would be a second acceptance: ${JSON.stringify(keys)}`),
  ).toBe(1);
  expect(
    /拒绝|没有|失败|不存在|未找到|空/.test(outcomeAfterFirst),
    node(NODE, `with the list showing an open proposal and the server refusing that id, the screen reported ${JSON.stringify(outcomeAfterFirst)} -- list membership must not read as permission`),
  ).toBe(true);
});
