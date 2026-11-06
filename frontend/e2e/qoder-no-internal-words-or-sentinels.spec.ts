/**
 * ORD-0251 -- the interface must not hand a traveller a word the product invented for itself, and it
 * must not express "we could not tell" as a number.
 *
 * Measured source: `/v8/me` printed the server's own push label (`RECORD_ONLY`, from
 * `backend/app/integrations/push.py`) as the status line, and the AI sheet published its internal
 * indeterminate sentinel on `data-plan-count="-1"` -- an attribute gates read, where `-1` means
 * "not knowable", not "negative one plans".
 *
 * Both scans below are the two-layer ones (`_qoderTextScan`): a raw token must be absent from what is
 * painted AND from what an announcer could speak, because "only in sr-only" is still a human-facing
 * defect -- it just announces it instead of showing it.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-no-internal-words-or-sentinels.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** The frozen human wording, held by this gate and NOT imported from the product file. */
const FROZEN_RECORD_ONLY_ZH = "只记录，不发推送";
const FROZEN_RECORD_ONLY_HELP_ZH = "服务器没有配置推送密钥，所以通知只会记在行程里，不会弹到这台设备上。";
const FROZEN_UNKNOWN_ZH = "说不准推送能不能用";
/** What the sheet's own component uses internally for "not knowable"; it must never reach the DOM. */
const SENTINEL = "-1";

async function seedJourney(page: Page) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  return journeyId;
}

/** Answer the push read with a chosen server label, and record that the read really fired. */
async function stubPushConfig(page: Page, capability: string) {
  let reads = 0;
  await page.route("**/api/v1/travelers/*/push-config", async (route) => {
    reads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        capability,
        public_key: capability === "WEB_PUSH_CONFIGURED" ? "BFakeKeyForThisGate0000000" : null,
        record_fallback: true,
      }),
    });
  });
  return () => reads;
}

test("ord0251-1-the-push-card-says-the-state-in-plain-words — 状态句不得是服务器标签", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await seedJourney(page);
  const pushReads = await stubPushConfig(page, "RECORD_ONLY");
  await page.goto("/v8/me", { waitUntil: "load" });

  const card = page.getByTestId("push-capability");
  await expect(card, node("ord0251-1", "the push card never rendered")).toBeVisible({ timeout: 20_000 });
  /* The stub firing only proves the request left; the answer still has to reach React.  Waiting on the
     attribute the answered read writes is what makes "CHECKING" below a defect rather than a race. */
  await expect
    .poll(() => card.getAttribute("data-capability"), {
      timeout: 20_000,
      message: node(
        "ord0251-1",
        `the read never landed (stub fired ${String(pushReads())}x), so any state line below is still "checking"`,
      ),
    })
    .toBe("RECORD_ONLY");

  const state = (await card.locator("strong").innerText()).trim();
  const help = (await card.locator("p").first().innerText()).trim();
  expect(state, node("ord0251-1", `the status line read "${state}"`)).toBe(FROZEN_RECORD_ONLY_ZH);
  expect(help, node("ord0251-1", `the explanation read "${help}"`)).toBe(FROZEN_RECORD_ONLY_HELP_ZH);

  /* The raw label must be absent from BOTH layers, while staying on the element as a diagnosable
     attribute -- that is the split: gates can still name what the server said, a traveller cannot. */
  const scan = await scanScreenText(page);
  for (const [layer, text] of [
    ["painted", scan.visual],
    ["announced", scan.accessible],
  ] as const) {
    expect(
      text.includes("RECORD_ONLY"),
      node("ord0251-1", `the server label reached the ${layer} layer of /v8/me`),
    ).toBe(false);
  }
  expect(await card.getAttribute("data-capability")).toBe("RECORD_ONLY");
  await card.screenshot({
    path: evidencePath(`ui-overnight/ord0251-push-card-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
  });

  writeFileSync(
    evidencePath("ord0251-push-card-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0251-1",
        serverLabel: "RECORD_ONLY",
        stateLine: state,
        explanation: help,
        labelInPaintedText: scan.visual.includes("RECORD_ONLY"),
        labelInAnnouncedText: scan.accessible.includes("RECORD_ONLY"),
        rawKeptAsAttribute: await card.getAttribute("data-capability"),
        visualLeaves: scan.visualLeaves,
      },
      null,
      1,
    ),
  );
});

test("ord0251-2-a-label-this-screen-does-not-know-is-not-echoed — 不认识的标签不许原样回显", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await seedJourney(page);
  const invented = "A_SERVER_STATUS_NOMINATED_BY_NOBODY_HERE";
  await stubPushConfig(page, invented);
  await page.goto("/v8/me", { waitUntil: "load" });

  const card = page.getByTestId("push-capability");
  await expect(card, node("ord0251-2", "the push card never rendered")).toBeVisible({ timeout: 20_000 });
  await expect
    .poll(() => card.getAttribute("data-capability"), {
      timeout: 20_000,
      message: node("ord0251-2", "the invented label never reached the card, so nothing below was measured"),
    })
    .toBe(invented);
  const state = (await card.locator("strong").innerText()).trim();
  const scan = await scanScreenText(page);

  expect(state, node("ord0251-2", `an unknown label came through as "${state}"`)).toBe(FROZEN_UNKNOWN_ZH);
  for (const [layer, text] of [
    ["painted", scan.visual],
    ["announced", scan.accessible],
  ] as const) {
    expect(
      text.includes(invented),
      node("ord0251-2", `the unknown label leaked into the ${layer} layer`),
    ).toBe(false);
  }
  // Fail closed also means "did not fall back to the known-good sentence".
  expect(state).not.toBe(FROZEN_RECORD_ONLY_ZH);

  writeFileSync(
    evidencePath("ord0251-unknown-label-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0251-2",
        inventedLabel: invented,
        stateLine: state,
        leakedInPaintedText: scan.visual.includes(invented),
        leakedInAnnouncedText: scan.accessible.includes(invented),
        fellBackToRecordOnly: state === FROZEN_RECORD_ONLY_ZH,
      },
      null,
      1,
    ),
  );
});

test("ord0251-3-the-plan-count-attribute-never-carries-the-unknown-sentinel — 「不知道」不是负数", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const journeyId = await seedJourney(page);
  let posts = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    posts += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mission: { status: "COMPLETED", failure_reason: null, reasoning_summary: null },
      }),
    });
  });
  /* Make the pre-run baseline unreadable: this is the branch that produces the component's internal
     "not knowable" value, so the attribute under test is exercised on its real path. */
  let planReads = 0;
  await page.route("**/api/v1/journeys/*/plans", async (route) => {
    planReads += 1;
    await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({}) });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景");
  await page.getByTestId("plan-ai-submit").click();

  const result = page.getByTestId("plan-ai-result");
  await expect(result, node("ord0251-3", "the sheet never answered")).toBeVisible({ timeout: 90_000 });
  expect(posts, node("ord0251-3", "0 POST, so the sheet was never exercised")).toBe(1);
  expect(planReads, node("ord0251-3", "the baseline read never fired")).toBeGreaterThan(0);

  const status = await result.getAttribute("data-result-status");
  const count = await result.getAttribute("data-plan-count");
  // Positive control: this really is the indeterminate branch, not an empty-but-known zero.
  expect(status, node("ord0251-3", `expected the unreadable-baseline branch, got "${status}"`)).toBe(
    "BASELINE_UNAVAILABLE",
  );
  expect(
    count === SENTINEL,
    node(
      "ord0251-3",
      `data-plan-count carried "${count}": the sentinel for "not knowable" reads as a count to anything that parses it`,
    ),
  ).toBe(false);
  expect(count, node("ord0251-3", "the unknown state has no name of its own")).toBe("unknown");
  const painted = (await result.innerText()).replace(/\s+/g, " ");
  expect(
    painted.includes("-1"),
    node("ord0251-3", `the sentinel also reached the traveller: "${painted.slice(0, 120)}"`),
  ).toBe(false);

  writeFileSync(
    evidencePath("ord0251-plan-count-attribute.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0251-3",
        journeyId,
        posts,
        planReads,
        resultStatus: status,
        dataPlanCount: count,
        sentinelOnAttribute: count === SENTINEL,
        paintedText: painted.slice(0, 160),
        rule: "内部仍然可以用 -1 表示「不知道」，但它不得出现在被门消费的 data-* 上，也不得上屏",
      },
      null,
      1,
    ),
  );
});
