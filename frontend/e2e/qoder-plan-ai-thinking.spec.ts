import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0038 — "is it thinking, or is it stuck?"
 *
 * The sheet used to advance a four-row checklist from a 1200ms `setInterval`.  Nothing in
 * that list was a signal: `POST /agent/commands` is synchronous and reports no phase while
 * it works, so the ✓ marks asserted backend work nobody had told us about.  A fake progress
 * bar is worse than none, because it makes people believe the system is working.
 *
 * What is asserted here is only what the component can prove: the seconds that really
 * elapsed, the phase it is genuinely awaiting, that cancel aborts the request rather than
 * hiding the panel, that a refused/failed request cannot be answered by the fallback
 * re-plan, and that the AI pre-flight says "unknown" when the status endpoint does not
 * answer.
 *
 * Two rules this file holds itself to, because a first revision failed both:
 *  - every failure path, including a visibility wait or a poll, names its own node id, so a
 *    red can never be attributed to "the element didn't show up";
 *  - assertions read what the traveller sees.  The first revision polled the
 *    `data-elapsed-seconds` attribute and survived a mutant that froze the rendered number,
 *    which is exactly the difference between a gate and a decoration.
 *
 * The model is switched off in every environment this lane may test in
 * (`CITYNEXUS_AI_ENABLED=false`), so the harness holds requests open to stand in for a slow
 * model.  That substitutes for *latency*, never for a real AI result.
 */

const COMMANDS = "**/api/v1/journeys/*/agent/commands";
const STATUS = "**/api/v1/agent/runtime-status";
const GENERATE = "**/api/v1/journeys/*/plans/generate";
const REQUEST_TEXT = "带爸妈去厦门三天，不要走太多路，酒店要靠近地铁。";
const FILE = "e2e/qoder-plan-ai-thinking.spec.ts";

const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

async function openSheet(page: Page) {
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByTestId("plan-ai-input").fill(REQUEST_TEXT);
}

function readyStatus(page: Page) {
  return page.route(STATUS, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: '{"deepseek_configured":true}',
    }),
  );
}

/** The number the traveller actually reads off the sheet, not a debug attribute. */
async function renderedSeconds(page: Page): Promise<number> {
  const text = await page.getByTestId("plan-ai-elapsed").innerText();
  const match = text.match(/(\d+)/);
  return match ? Number(match[1]) : -1;
}

test.describe("ORD-0038 the AI sheet reports what it actually knows", () => {
  // Both render arms, because the sheet builds the waiting line twice — once per language
  // — and a mutant in whichever arm the suite does not run survives unnoticed.
  for (const language of ["en", "zh-CN"] as const) {
    test(`plan-ai-elapsed-ticks — the waiting number advances (${language === "en" ? "English" : "Chinese"} copy)`, async ({
      page,
    }) => {
      test.setTimeout(150_000);
      await page.addInitScript(
        (value) => localStorage.setItem("citynexus.locale", value),
        language,
      );
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 390, height: 844 });
      await readyStatus(page);
      await page.route(COMMANDS, async (route) => {
        await new Promise<void>((resolve) => setTimeout(resolve, 20_000));
        void route.abort();
      });
      await openSheet(page);
      await page.getByTestId("plan-ai-submit").click();

      await expect(
        page.getByTestId("plan-ai-live"),
        node("plan-ai-elapsed-ticks",
          `the sheet never showed a waiting state at all (${language} copy)`),
      ).toBeVisible({ timeout: 15_000 });
      const first = await renderedSeconds(page);
      const rendered = (await page.getByTestId("plan-ai-elapsed").innerText()).trim();
      expect(
        first >= 0,
        node("plan-ai-elapsed-ticks",
          `the waiting line carries no number the traveller can read (${language}): `
          + `${JSON.stringify(rendered)}`),
      ).toBe(true);
      await expect
        .poll(() => renderedSeconds(page), {
          timeout: 12_000,
          message: node("plan-ai-elapsed-ticks",
            `the seconds the traveller reads on screen never advanced (${language} copy); a waiting `
            + "line stuck at one number is indistinguishable from a hang, which is the report this "
            + "order answers"),
        })
        .toBeGreaterThan(first);
    });
  }

  test("plan-ai-no-fabricated-stage — no step may claim done before it was awaited", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await readyStatus(page);
    await page.route(COMMANDS, async (route) => {
      await new Promise<void>((resolve) => setTimeout(resolve, 20_000));
      void route.abort();
    });
    await openSheet(page);
    await page.getByTestId("plan-ai-submit").click();
    await expect(page.getByTestId("plan-ai-live"), node("plan-ai-no-fabricated-stage",
      "no waiting block, so there is no phase list to read")).toBeVisible({ timeout: 15_000 });
    // Stay in the request phase: nothing after it has happened yet.
    await page.waitForTimeout(2500);

    const rows = await page
      .getByTestId("plan-ai-steps")
      .evaluate((list) =>
        Array.from(list.querySelectorAll("li")).map((li) => ({
          phase: li.getAttribute("data-phase") ?? "",
          state: li.getAttribute("data-phase-state") ?? "",
        })),
      );
    const phases = (wanted: string) =>
      rows.filter((row) => row.state === wanted).map((row) => row.phase);

    expect(
      rows.length,
      node("plan-ai-no-fabricated-stage", "no phase list to read"),
    ).toBeGreaterThan(0);
    expect(
      phases("done"),
      node("plan-ai-no-fabricated-stage",
        "while the request is still in flight only the baseline read has really finished, but "
        + `these claim done: ${JSON.stringify(phases("done"))}. The old sheet ticked a ✓ every `
        + "1.2s with no signal behind it"),
    ).toEqual(["baseline"]);
    expect(
      phases("active"),
      node("plan-ai-no-fabricated-stage",
        `exactly one row may be current and it must be the request, got ${JSON.stringify(phases("active"))}`),
    ).toEqual(["request"]);
    expect(
      phases("pending"),
      node("plan-ai-no-fabricated-stage",
        "verification has not started, so its row must still be pending, got "
        + JSON.stringify(phases("pending"))),
    ).toEqual(["verify"]);
    expect(
      rows.map((row) => row.phase),
      node("plan-ai-no-fabricated-stage",
        "the fallback re-plan row rendered before it ran, which is a step the run never took"),
    ).not.toContain("fallback");
  });

  test("plan-ai-cancel-aborts-request — cancel stops the request, and says what it cannot undo", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await readyStatus(page);
    const failures: string[] = [];
    page.on("requestfailed", (request) => {
      if (request.url().includes("/agent/commands")) failures.push(request.failure()?.errorText ?? "?");
    });
    // Short enough that a product which ignores the abort still produces a result the
    // named assertions can read, instead of the test dying on a timeout.
    await page.route(COMMANDS, async (route) => {
      await new Promise<void>((resolve) => setTimeout(resolve, 8_000));
      void route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await openSheet(page);
    await page.getByTestId("plan-ai-submit").click();
    await expect(page.getByTestId("plan-ai-cancel"), node("plan-ai-cancel-aborts-request",
      "the sheet offered no cancel control while a request was in flight")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("plan-ai-cancel").click();

    const result = page.getByTestId("plan-ai-result");
    await expect(result, node("plan-ai-cancel-aborts-request",
      "after cancelling, the sheet showed neither a cancellation nor any other outcome")).toBeVisible({
      timeout: 25_000,
    });
    const status = await result.getAttribute("data-result-status");
    const tone = await result.getAttribute("data-result-tone");
    const rendered = (await result.innerText()).replace(/\s+/g, " ").trim();

    expect(
      status,
      node("plan-ai-cancel-aborts-request",
        `cancelling an in-flight request must read as CANCELLED_AFTER_SEND, got `
        + `${JSON.stringify(status)}; rendered ${JSON.stringify(rendered)}`),
    ).toBe("CANCELLED_AFTER_SEND");
    await expect
      .poll(
        () => failures.filter((text) => /ABORTED/i.test(text)).length,
        {
          timeout: 12_000,
          message: node("plan-ai-cancel-aborts-request",
            "the browser never aborted the request; a cancel that only hides the sheet leaves the "
            + "model running and spending money"),
        },
      )
      .toBeGreaterThan(0);
    expect(
      /untouched|没有被改动/.test(rendered),
      node("plan-ai-cancel-aborts-request",
        `the sheet promised the trip was unchanged after the request had already been sent: `
        + `${JSON.stringify(rendered)}`),
    ).toBe(false);
    expect(
      tone,
      node("plan-ai-cancel-aborts-request",
        `a cancellation is not a success; tone was ${JSON.stringify(tone)}`),
    ).not.toBe("ok");
    await expect(page.getByTestId("plan-ai-live")).toHaveCount(0);
  });

  test("plan-ai-cancel-before-send — cancelling the baseline read really did send nothing", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await readyStatus(page);
    let commandCalls = 0;
    await page.route(COMMANDS, (route) => {
      commandCalls += 1;
      void route.abort();
    });
    await ensureDemoJourney(page);
    await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
    await page.getByTestId("hub-plan-with-ai").click();
    await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("plan-ai-input").fill(REQUEST_TEXT);
    // Armed only now, so the first /plans request it sees is the sheet's baseline read
    // rather than something the hub does while the sheet is opening.
    let plansCalls = 0;
    await page.route("**/api/v1/journeys/*/plans", async (route) => {
      plansCalls += 1;
      if (plansCalls === 1) {
        // Hold the baseline read so cancel genuinely lands before the command request is
        // ever sent.  Aborting it from here would be a network error, not a cancellation,
        // and would exercise a different branch.
        await new Promise<void>((resolve) => setTimeout(resolve, 6_000));
        void route.abort();
        return;
      }
      void route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });
    await page.getByTestId("plan-ai-submit").click();
    await expect(page.getByTestId("plan-ai-cancel"), node("plan-ai-cancel-before-send",
      "no cancel control during the baseline read")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("plan-ai-cancel").click();

    const result = page.getByTestId("plan-ai-result");
    await expect(result, node("plan-ai-cancel-before-send",
      "after cancelling the baseline read the sheet showed no outcome at all")).toBeVisible({
      timeout: 20_000,
    });
    const status = await result.getAttribute("data-result-status");
    expect(
      status,
      node("plan-ai-cancel-before-send",
        `nothing had been sent yet, so this must read as a clean CANCELLED, not `
        + `${JSON.stringify(status)} (baseline reads seen: ${plansCalls}; if that is not 1 the hub `
        + "made an extra /plans call and this case needs a different lever)"),
    ).toBe("CANCELLED");
    // Let a stray request arrive before claiming none was made, or a mutant that ignores
    // the abort still reads zero.
    await page.waitForTimeout(1500);
    expect(
      commandCalls,
      node("plan-ai-cancel-before-send",
        `the command request was sent ${commandCalls} time(s) after the traveller cancelled the `
        + "step before it; \"cancel\" must stop the run, not just the panel"),
    ).toBe(0);
  });

  test("plan-ai-failure-not-loading — a refused request is named, and the fallback may not answer for it", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await readyStatus(page);
    const generateCalls: string[] = [];
    await page.route(GENERATE, (route) => {
      generateCalls.push(route.request().url());
      void route.abort();
    });
    await page.route(COMMANDS, (route) =>
      route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "JOURNEY_DAYS_OUT_OF_BOUNDS: 行程天数超过上限" }),
      }),
    );
    await openSheet(page);
    await page.getByTestId("plan-ai-submit").click();

    const result = page.getByTestId("plan-ai-result");
    await expect(result, node("plan-ai-failure-not-loading",
      "a refused request produced no readable outcome")).toBeVisible({ timeout: 20_000 });
    const status = await result.getAttribute("data-result-status");
    const busyStillShown = await page.getByTestId("plan-ai-live").count();

    expect(
      status,
      node("plan-ai-failure-not-loading",
        `a 422 must be reported as the refusal it is, got ${JSON.stringify(status)}; the `
        + "synthetic fallback re-plan ran "
        + `${generateCalls.length} time(s), which can answer for a call the server rejected`),
    ).toBe("HTTP_422");
    expect(
      busyStillShown,
      node("plan-ai-failure-not-loading",
        "the waiting block is still on screen next to the failure, so loading and failure are "
        + "not one glance apart"),
    ).toBe(0);
    expect(
      generateCalls.length,
      node("plan-ai-failure-not-loading",
        `the fallback re-plan ran ${generateCalls.length} time(s) after a refused request`),
    ).toBe(0);

    // Only now that the status is known good: a refusal must carry the backend's own reason.
    const toggle = page.getByTestId("plan-ai-detail-toggle");
    await expect(toggle, node("plan-ai-failure-not-loading",
      "a refusal with a backend reason offered no technical detail to open")).toBeVisible({
      timeout: 10_000,
    });
    await toggle.click();
    const reason = (await page.getByTestId("plan-ai-reason").innerText()).trim();
    expect(
      reason.includes("JOURNEY_DAYS_OUT_OF_BOUNDS"),
      node("plan-ai-failure-not-loading",
        `the backend's own reason never reached the sheet, technical detail reads `
        + `${JSON.stringify(reason)}`),
    ).toBe(true);
  });

  test("plan-ai-no-synthetic-success — rows the fallback assembled are not an AI result", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await readyStatus(page);
    /* Measured on the shipped bundle with no model configured: the mission came back not
       completed with `DeepSeek API key is not configured`, this component's own fallback
       re-plan produced 2 rows, and the sheet said "已生成 2 个可选方案". */
    await page.route(COMMANDS, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          mission: { status: "FAILED", failure_reason: "DeepSeek API key is not configured" },
        }),
      }),
    );
    let generateCalls = 0;
    /* State, not call order: the hub fetches /plans while the sheet is opening, so a
       counter would let that request consume the "baseline" answer. */
    let plansBody = "[]";
    await page.route("**/api/v1/journeys/*/plans", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: plansBody }),
    );
    await page.route("**/api/v1/places?*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{ id: "place-a" }, { id: "place-b" }]),
      }),
    );
    await page.route(GENERATE, (route) => {
      generateCalls += 1;
      // From here on the two rows exist, because this call is what created them.
      plansBody = JSON.stringify([{ id: "plan-a" }, { id: "plan-b" }]);
      void route.fulfill({
        status: 200,
        contentType: "application/json",
        body: plansBody,
      });
    });
    await openSheet(page);
    await page.getByTestId("plan-ai-submit").click();

    const result = page.getByTestId("plan-ai-result");
    await expect(result, node("plan-ai-no-synthetic-success",
      "the run produced no outcome block at all")).toBeVisible({ timeout: 20_000 });
    const status = await result.getAttribute("data-result-status");
    const tone = await result.getAttribute("data-result-tone");
    const rendered = (await result.innerText()).replace(/\s+/g, " ").trim();

    expect(
      generateCalls,
      node("plan-ai-no-synthetic-success",
        `the fallback re-plan never ran (calls: ${generateCalls}), so this case is no longer `
        + "reproducing the sequence that produced the false success"),
    ).toBeGreaterThan(0);
    expect(
      status,
      node("plan-ai-no-synthetic-success",
        `a mission that reported it did not run must never be presented as AI success; got `
        + `${JSON.stringify(status)}: ${JSON.stringify(rendered)}`),
    ).toBe("PLANS_WITHOUT_AI");
    expect(
      tone,
      node("plan-ai-no-synthetic-success",
        `the tone said ${JSON.stringify(tone)} while the AI was absent`),
    ).not.toBe("ok");
    expect(
      /没有参与|did not take part/.test(rendered),
      node("plan-ai-no-synthetic-success",
        `the copy never told the traveller the AI was absent: ${JSON.stringify(rendered)}`),
    ).toBe(true);
  });

  test("plan-ai-preflight-honest — an unreadable AI status is reported as unknown, never as ready", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    const cases: { label: string; fail?: boolean; status: number; body: string; want: string }[] = [
      { label: "configured", status: 200, body: '{"deepseek_configured":true}', want: "configured_unverified" },
      { label: "not configured", status: 200, body: '{"deepseek_configured":false}', want: "not_configured" },
      // ORD-0359: three causes, three words. A server that names the operator's switch must not be
      // painted as a missing credential, and neither may be painted as the provider refusing.
      { label: "switch off", status: 200, body: '{"ai_state":"DISABLED_BY_OPS","deepseek_configured":true}', want: "disabled_by_ops" },
      { label: "no credential", status: 200, body: '{"ai_state":"NOT_CONFIGURED","deepseek_configured":false}', want: "not_configured" },
      { label: "provider refused", status: 200, body: '{"ai_state":"PROVIDER_REFUSED","deepseek_configured":true}', want: "provider_refused" },
      { label: "server says configured", status: 200, body: '{"ai_state":"CONFIGURED_UNVERIFIED","deepseek_configured":true}', want: "configured_unverified" },
      // the named field wins over the boolean, so a stale `deepseek_configured` cannot overrule it
      { label: "named beats boolean", status: 200, body: '{"ai_state":"DISABLED_BY_OPS","deepseek_configured":true}', want: "disabled_by_ops" },
      { label: "http 500", status: 500, body: "boom", want: "unknown" },
      { label: "no such field", status: 200, body: '{"runtime_identity":"x"}', want: "unknown" },
      // The transport-failure branch: fetch() rejects here, which no fulfill can produce.
      { label: "never answered", status: 0, body: "", fail: true, want: "unknown" },
    ];
    const seen: Record<string, string> = {};
    for (const item of cases) {
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 390, height: 844 });
      if (item.fail) {
        await page.route(STATUS, (route) => void route.abort());
      } else {
        await page.route(STATUS, (route) =>
          route.fulfill({ status: item.status, contentType: "application/json", body: item.body }),
        );
      }
      await openSheet(page);
      const chip = page.getByTestId("plan-ai-preflight");
      await expect(chip, node("plan-ai-preflight-honest",
        `the sheet never stated an AI pre-flight for case ${JSON.stringify(item.label)}`)).toBeVisible({
        timeout: 15_000,
      });
      const got = (await chip.getAttribute("data-ai-state")) ?? "";
      const text = (await chip.innerText()).trim();
      seen[item.label] = `${got} :: ${text}`;
      expect(
        /已接入 AI 模型|A model is configured on this server|配了一个 AI 凭据/.test(text) &&
          item.want !== "configured_unverified",
        node("plan-ai-preflight-honest",
          `the sheet claimed a model was configured after case ${JSON.stringify(item.label)}: `
          + `${JSON.stringify(text)}`),
      ).toBe(false);
      expect(
        got,
        node("plan-ai-preflight-honest",
          `case ${JSON.stringify(item.label)} must read as ${item.want}, sheet said `
          + `${JSON.stringify(got)}`),
      ).toBe(item.want);
      await page.getByTestId("plan-ai-dismiss").click();
      await expect(page.getByTestId("plan-ai-sheet")).toHaveCount(0);
    }
    writeFileSync(
      evidencePath("ui-overnight/plan-ai-preflight.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0038", cases: seen }, null, 2) + "\n",
      "utf-8",
    );
  });
});
