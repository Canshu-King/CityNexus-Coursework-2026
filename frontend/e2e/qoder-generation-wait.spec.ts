import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0047 (narrowed by arbitration 5749041556) — the ~115 s a cold city's first stay
 * search takes has to look like waiting for something, not like a frozen screen.
 *
 * Measured by the Commander through the production preview on the user's own path:
 *   cold Xiamen   stay-options/generate 200 114.98s  options=0
 *   same call     stay-options/generate 200   2.52s  options=4
 *   warm Tianjin  stay-options/generate 200   0.14s  options=4
 *
 * Two things are under test here, and only these: the waiting is named and counted, and a
 * zero-result answer is not drawn as success.  The backend owns the 115 s and the empty
 * first page (ORD-0049, `8c66aa92`); nothing in this file changes how long it takes or
 * makes the suite tolerate a longer wait.
 *
 * The honesty rule from ORD-0038 still applies and is asserted: only stages the client
 * really awaited may appear, no percentage, and no claiming to have cancelled work the
 * server never agreed to cancel — `stay-options/generate` (backend/app/main.py) is a
 * synchronous route with no operation id and no cancel endpoint, so an aborted fetch means
 * the traveller stopped watching, nothing more.
 */

const FILE = "e2e/qoder-generation-wait.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const GENERATE = "**/api/v1/journeys/*/stay-options/generate";

const ARMS = [
  { locale: "en", phase: /looking for stays|stay options/i, empty: /no stay options/i },
  { locale: "zh-CN", phase: /正在搜索住宿/, empty: /没找到可推荐的住宿/ },
] as const;

async function openFreshWizard(page: Page, locale: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await page.goto("/v8", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 25_000 });
  await page.getByTestId("trial-city").fill("Tianjin");
  await page.getByTestId("trial-arrival").fill("Tianjin West Station");
  await page.getByTestId("trial-departure").fill("Tianjin Binhai Airport");
  await page.getByTestId("trial-next").click();
  await page.waitForTimeout(200);
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 10_000 });
}

/** Answer `generate` after `delay`, then report how many results it handed back. */
async function slowGenerate(
  page: Page,
  delay: number,
  topK: unknown[] | null,
  generation?: { outcome: string } | null,
) {
  await page.route(GENERATE, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await new Promise<void>((resolve) => setTimeout(resolve, delay));
    // Once the traveller stopped waiting, or this node swapped its mock, the route is gone
    // and answering it throws -- which would strand the next state on a pending request.
    try {
      if (topK === null) {
        await route.fulfill({ status: 500, contentType: "text/plain; charset=utf-8", body: "Internal Server Error" });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          optimizer: "BOUNDED_DETERMINISTIC",
          top_k: topK,
          hard_constraints: {},
          ...(generation ? { generation } : {}),
        }),
      });
    } catch {
      /* abandoned; the product already moved on */
    }
  });
}

const option = (id: string) => ({
  id,
  rank: 1,
  stay_total_minor: 42000,
  currency: "CNY",
  provider: { name: "Measured Hotel" },
  nights: [{ check_in_at: "2026-10-09T15:00:00+08:00", check_out_at: "2026-10-10T12:00:00+08:00" }],
});

function renderedSeconds(page: Page) {
  return page.evaluate(() => {
    const el = document.querySelector('[data-testid="trial-wait-elapsed"]');
    const digits = el?.textContent?.match(/\d+/);
    return digits ? Number(digits[0]) : null;
  });
}

test.describe("ORD-0047 the stay search says what it is waiting for", () => {
  test("gen-wait-phase-stays-visible — the phase line is on screen the whole time, in both locales", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    const observed: Record<string, unknown> = {};
    for (const arm of ARMS) {
      await slowGenerate(page, 9_000, [option("a")]);
      await openFreshWizard(page, arm.locale);
      await page.getByTestId("trial-create").click();

      const wait = page.getByTestId("trial-wait");
      await expect(
        wait,
        node("gen-wait-phase-stays-visible", `no waiting block appeared for ${arm.locale}`),
      ).toBeVisible({ timeout: 5_000 });

      // "Continuously visible" is sampled, not asserted once: a block that flashes and is
      // replaced by a spinner has the same defect the order names.
      const samples: Array<{ phase: string | null; seconds: number | null; visible: boolean }> = [];
      for (let i = 0; i < 12; i += 1) {
        samples.push({
          visible: await wait.isVisible().catch(() => false),
          phase: await wait.getAttribute("data-phase").catch(() => null),
          seconds: await renderedSeconds(page),
        });
        await page.waitForTimeout(500);
      }
      const unseen = samples.filter((item) => !item.visible).length;
      const phases = samples.map((item) => item.phase).filter(Boolean) as string[];
      const seconds = samples.map((item) => item.seconds).filter((v): v is number => v !== null);

      expect(
        unseen,
        node("gen-wait-phase-stays-visible", `${arm.locale}: the block disappeared on ${unseen}/12 samples`),
      ).toBe(0);
      expect(
        phases.every((phase) => ["traveler", "journey", "stay-options", "experience"].includes(phase)),
        node("gen-wait-phase-stays-visible", `${arm.locale}: a stage the client never awaited was shown: ${JSON.stringify(phases)}`),
      ).toBe(true);
      expect(
        phases.some((phase) => phase === "stay-options"),
        node("gen-wait-phase-stays-visible", `${arm.locale}: the 115 s stage itself was never named (${JSON.stringify(phases)})`),
      ).toBe(true);
      expect(
        new Set(seconds).size,
        node("gen-wait-phase-stays-visible", `${arm.locale}: the waiting number never advanced (${JSON.stringify(seconds)})`),
      ).toBeGreaterThan(1);
      const rendered = (await wait.innerText()).replace(/\s+/g, " ");
      expect(
        /%|percent|步骤 \d|step \d of/i.test(rendered),
        node("gen-wait-phase-stays-visible", `${arm.locale}: the block claims progress the backend never reported: ${JSON.stringify(rendered)}`),
      ).toBe(false);
      expect(
        rendered,
        node("gen-wait-phase-stays-visible", `${arm.locale}: the phase words are not on screen`),
      ).toMatch(arm.phase);
      observed[arm.locale] = { rendered, seconds: new Set(seconds).size, phases: [...new Set(phases)] };
    }
    writeFileSync(
      evidencePath("ui-overnight/generation-wait.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0047", node: "gen-wait-phase-stays-visible", observed }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("gen-wait-stop-waiting-aborts — stopping the wait aborts the request without claiming a server cancel", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    const aborted: string[] = [];
    page.on("requestfailed", (request) => {
      if (request.url().includes("stay-options/generate")) aborted.push(request.failure()?.errorText ?? "?");
    });
    await slowGenerate(page, 15_000, [option("a")]);
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-create").click();
    const stop = page.getByTestId("trial-stop-waiting");
    await expect(stop, node("gen-wait-stop-waiting-aborts", "no way to stop waiting was offered")).toBeVisible({
      timeout: 8_000,
    });
    const stopBox = await stop.boundingBox();
    expect(
      stopBox?.height ?? 0,
      node("gen-wait-stop-waiting-aborts",
        `the control a traveller presses in frustration is ${(stopBox?.height ?? 0).toFixed(1)}px tall at 390px, under 44`),
    ).toBeGreaterThanOrEqual(44);
    await stop.click();

    const outcome = page.getByTestId("trial-wait-outcome");
    await expect(
      outcome,
      node("gen-wait-stop-waiting-aborts", "after stopping, the screen said nothing about what just happened"),
    ).toBeVisible({ timeout: 15_000 });
    const rendered = (await outcome.innerText()).replace(/\s+/g, " ").trim();

    await expect
      .poll(() => aborted.filter((text) => /ABORTED/i.test(text)).length, {
        timeout: 12_000,
        message: node("gen-wait-stop-waiting-aborts", "the request kept running from the browser's point of view; a stop button that only hides text is not a stop"),
      })
      .toBeGreaterThan(0);

    // The server never agreed to cancel anything, so the copy may not promise that it did.
    expect(
      /取消生成|cancelled the search|search (was |has been )?cancelled|生成已取消/i.test(rendered),
      node("gen-wait-stop-waiting-aborts", `the screen claims a server-side cancel that no endpoint offers: ${JSON.stringify(rendered)}`),
    ).toBe(false);
    expect(
      /still running|may still be|仍在|还在/i.test(rendered),
      node("gen-wait-stop-waiting-aborts", `stopping the wait must say the search may still be going: ${JSON.stringify(rendered)}`),
    ).toBe(true);
    // The form and the step survive: still on the review page, and the typed values are
    // there when the traveller walks back.
    await expect(
      page.getByTestId("trial-create"),
      node("gen-wait-stop-waiting-aborts", "stopping the wait threw the traveller off step 3"),
    ).toBeVisible();
    expect(
      (await page.getByTestId("review-city").innerText()).trim(),
      node("gen-wait-stop-waiting-aborts", "the review page lost the city after the wait stopped"),
    ).toContain("Tianjin");
    await page.getByTestId("trial-back").click();
    await page.getByTestId("trial-back").click();
    await expect(
      page.getByTestId("trial-city"),
      node("gen-wait-stop-waiting-aborts", "step 1 no longer renders its fields"),
    ).toBeVisible();
    await expect(
      page.getByTestId("trial-city"),
      node("gen-wait-stop-waiting-aborts", "the city that was typed is gone after stopping the wait"),
    ).toHaveValue("Tianjin");
    await expect(
      page.getByTestId("trial-arrival"),
      node("gen-wait-stop-waiting-aborts", "the anchor that was typed is gone after stopping the wait"),
    ).toHaveValue("Tianjin West Station");
    await expect(page.getByTestId("trial-wait")).toHaveCount(0);
  });

  test("gen-empty-options-is-not-success — a 200 with zero results is drawn as zero results", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    const observed: Record<string, string> = {};
    for (const arm of ARMS) {
      await slowGenerate(page, 300, []);
      await openFreshWizard(page, arm.locale);
      await page.getByTestId("trial-create").click();

      const empty = page.getByTestId("hub-no-stays");
      await expect(
        empty,
        node("gen-empty-options-is-not-success", `${arm.locale}: zero stay options were presented as an ordinary Today`),
      ).toBeVisible({ timeout: 25_000 });
      const rendered = (await empty.innerText()).replace(/\s+/g, " ").trim();
      observed[arm.locale] = rendered;
      expect(
        rendered,
        node("gen-empty-options-is-not-success", `${arm.locale}: the empty result is not named`),
      ).toMatch(arm.empty);
      const retry = page.getByTestId("hub-stay-retry");
      await expect(
        retry,
        node("gen-empty-options-is-not-success", `${arm.locale}: an empty result with no next action is a dead end`),
      ).toBeVisible();
      const retryBox = await retry.boundingBox();
      expect(
        retryBox?.height ?? 0,
        node("gen-empty-options-is-not-success",
          `${arm.locale}: the only way out of the empty screen is ${(retryBox?.height ?? 0).toFixed(1)}px tall at 390px, under 44`),
      ).toBeGreaterThanOrEqual(44);
      // A planning sheet opened on top of nothing to plan is the "success" face of the bug.
      await expect(
        page.getByTestId("plan-ai-sheet"),
        node("gen-empty-options-is-not-success", `${arm.locale}: the AI sheet opened as if stays had been found`),
      ).toHaveCount(0);
    }
    writeFileSync(
      evidencePath("ui-overnight/generation-empty.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0047", node: "gen-empty-options-is-not-success", observed }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("gen-three-states-distinguishable — waiting, empty and refused must not share a face", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    const seen: Record<string, { text: string; state: string | null }> = {};

    await slowGenerate(page, 9_000, [option("a")]);
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-create").click();
    await page.waitForTimeout(1_500);
    seen.waiting = {
      text: (await page.getByTestId("trial-wait").innerText()).replace(/\s+/g, " ").trim(),
      state: await page.getByTestId("trial-wait").getAttribute("data-phase"),
    };
    await page.getByTestId("trial-stop-waiting").click();
    await page.waitForTimeout(1_000);

    // A refused search that never answers is still refused, not "empty", not "waiting".
    await page.unroute(GENERATE);
    await slowGenerate(page, 300, null);
    await page.getByTestId("trial-create").click();
    const failure = page.getByTestId("trial-create-error");
    await expect(
      failure,
      node("gen-three-states-distinguishable", "a 500 from the stay search did not read as a failure"),
    ).toBeVisible({ timeout: 20_000 });
    seen.failed = { text: (await failure.innerText()).replace(/\s+/g, " ").trim(), state: null };
    expect(
      /did not complete|没有成功|500/i.test(seen.failed.text),
      node("gen-three-states-distinguishable", `the refused case rendered: ${JSON.stringify(seen.failed.text)}`),
    ).toBe(true);
    // And the journey really was created before the refusal, so the notice may not deny it.
    expect(
      /no journey was created/i.test(seen.failed.text),
      node("gen-three-states-distinguishable", `the notice denies a journey that exists: ${JSON.stringify(seen.failed.text)}`),
    ).toBe(false);

    await page.unroute(GENERATE);
    await slowGenerate(page, 300, []);
    await page.getByTestId("trial-create").click();
    await expect(page.getByTestId("hub-no-stays")).toBeVisible({ timeout: 25_000 });
    seen["hub empty"] = {
      text: (await page.getByTestId("hub-no-stays").innerText()).replace(/\s+/g, " ").trim(),
      state: await page.getByTestId("hub-no-stays").getAttribute("data-stay-search"),
    };

    /* The three faces the ruling named -- still loading, empty, refused -- are swept on the
       same surface, because that is where a traveller meets them. */
    await page.unroute(GENERATE);
    await slowGenerate(page, 6_000, []);
    await page.getByTestId("hub-stay-retry").click();
    const searching = page.getByTestId("hub-stay-searching");
    await expect(
      searching,
      node("gen-three-states-distinguishable", "a retry from Today showed no waiting face at all"),
    ).toBeVisible({ timeout: 8_000 });
    await expect(
      searching,
      node("gen-three-states-distinguishable", "Today's in-flight state is not labelled as searching"),
    ).toHaveAttribute("data-stay-search", "searching", { timeout: 8_000 });
    seen["hub searching"] = {
      text: (await searching.innerText()).replace(/\s+/g, " ").trim(),
      state: "searching",
    };
    await expect(
      page.getByTestId("hub-stay-elapsed"),
      node("gen-three-states-distinguishable", "Today's waiting face has no counted seconds"),
    ).toContainText(/\d+/);
    // Let this retry finish before starting the next one; two overlapping searches would
    // make the face the assertion reads a race rather than a product state.
    await expect(
      page.getByTestId("hub-no-stays"),
      node("gen-three-states-distinguishable", "the delayed retry never came back to an empty result"),
    ).toBeVisible({ timeout: 25_000 });

    await page.unroute(GENERATE);
    await slowGenerate(page, 300, null);
    await page.getByTestId("hub-stay-retry").click();
    const refused = page.getByTestId("hub-stay-refused");
    await expect(
      refused,
      node("gen-three-states-distinguishable", "a refused retry from Today rendered no state at all"),
    ).toBeVisible({ timeout: 20_000 });
    await expect(
      refused,
      node("gen-three-states-distinguishable", "Today's refused retry is not labelled as failed"),
    ).toHaveAttribute("data-stay-search", "failed", { timeout: 8_000 });
    seen["hub refused"] = {
      text: (await refused.innerText()).replace(/\s+/g, " ").trim(),
      state: "failed",
    };

    const hubFaces = ["hub searching", "hub empty", "hub refused"].map((key) => seen[key].text);
    expect(
      new Set(hubFaces).size,
      node("gen-three-states-distinguishable",
        `Today cannot tell loading from empty from refused: ${JSON.stringify(hubFaces)}`),
    ).toBe(3);
    expect(
      new Set([seen.waiting.text, seen["hub empty"].text, seen.failed.text]).size,
      node("gen-three-states-distinguishable", `two of the three states rendered the same words: ${JSON.stringify(seen)}`),
    ).toBe(3);
    writeFileSync(
      evidencePath("ui-overnight/generation-states.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0047", node: "gen-three-states-distinguishable", seen }, null, 2) + "\n",
      "utf-8",
    );
  });

  /* The empty block may only say what `generation.outcome` said. This lane's own rule from
     ORD-0038 applies to causes as well as to stages: an invented reason is worse than "the
     server did not say". */
  test("gen-empty-reason-comes-from-the-server — a reported cause is named, an unreported one stays unknown", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });

    await slowGenerate(page, 300, [], { outcome: "NO_OFFERS_FROM_PROVIDER" });
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-create").click();
    const reported = page.getByTestId("hub-no-stays");
    await expect(
      reported,
      node("gen-empty-reason-comes-from-the-server", "a reported outcome never reached the screen"),
    ).toBeVisible({ timeout: 25_000 });
    await expect(
      reported,
      node("gen-empty-reason-comes-from-the-server", "the reported outcome is not carried on the block"),
    ).toHaveAttribute("data-stay-outcome", "NO_OFFERS_FROM_PROVIDER");
    const reportedText = (await reported.innerText()).replace(/\s+/g, " ");
    expect(
      /provider|提供方/i.test(reportedText),
      node("gen-empty-reason-comes-from-the-server",
        `the server named the providers and the screen said something else: ${JSON.stringify(reportedText)}`),
    ).toBe(true);

    await page.unroute(GENERATE);
    await slowGenerate(page, 300, []);
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-create").click();
    const unknown = page.getByTestId("hub-no-stays");
    await expect(
      unknown,
      node("gen-empty-reason-comes-from-the-server", "an empty result without an outcome was not drawn as empty"),
    ).toBeVisible({ timeout: 25_000 });
    await expect(
      unknown,
      node("gen-empty-reason-comes-from-the-server", "a search that reported no cause must say so, not guess one"),
    ).toHaveAttribute("data-stay-outcome", "unknown");
    const unknownText = (await unknown.innerText()).replace(/\s+/g, " ");
    expect(
      /provider|提供方/i.test(unknownText),
      node("gen-empty-reason-comes-from-the-server",
        `the screen blamed the providers without being told to: ${JSON.stringify(unknownText)}`),
    ).toBe(false);
    expect(
      /no stay options|没找到/i.test(unknownText),
      node("gen-empty-reason-comes-from-the-server",
        `the unknown arm stopped saying the plain fact that nothing came back: ${JSON.stringify(unknownText)}`),
    ).toBe(true);
    writeFileSync(
      evidencePath("ui-overnight/generation-outcome.json"),
      JSON.stringify(
        {
          generatedBy: "e345b9d6",
          order: "ORD-0047",
          node: "gen-empty-reason-comes-from-the-server",
          reported: reportedText.trim(),
          unknown: unknownText.trim(),
        },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });
});
