import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0034 — a failed journey creation must be visible.
 *
 * The user's report was "stuck on 确认创建".  The shell's createJourney catch does write
 * the named backend error into state, and the shell does render state — but this screen
 * returns before the shell exists (V8Experience.tsx), so nothing reached the display.
 * Retrying the button looked identical to it working, which is what read as frozen.
 *
 * This gate drives the real product with a real failing request rather than asserting on
 * markup: POST /api/v1/journeys answers 500 with the same body shape the backend uses, so
 * the path under test is v8Request -> createJourney's catch -> the wizard's own failure
 * signal, not a fixture.
 *
 * It is deliberately killable in two places, and both are named in the run log:
 *   - remove the wizard's failure render  -> this test must go red
 *   - remove the `message` prop plumbing  -> see the reason assertion below
 */

const MEASURED_500 = {
  status: 500,
  contentType: "text/plain; charset=utf-8",
  body: "Internal Server Error",
};

async function openFreshWizard(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    try {
      localStorage.clear();
    } catch {
      /* nothing to clear before the first commit */
    }
  });
  await openScreen(page, "/v8", ["trial-setup"]);
}

async function walkToStep3(page: import("@playwright/test").Page) {
  await page.getByTestId("trial-city").fill("Tianjin");
  await page.getByTestId("trial-arrival").fill("Tianjin West Station");
  await page.getByTestId("trial-departure").fill("Tianjin Binhai Airport");
  await page.getByTestId("trial-next").click();
  await page.waitForTimeout(300);
  await page.getByTestId("trial-next").click();
  await page.waitForTimeout(500);
  await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 10_000 });
}

test.describe("ORD-0034 onboarding surfaces a failed create", () => {
  test("a 500 on journey creation shows a visible, named failure instead of silence", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 412, height: 915 });

    let sawCreateRequest = false;
    await page.route("**/api/v1/journeys", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      sawCreateRequest = true;
      await route.fulfill(MEASURED_500);
    });

    await openFreshWizard(page);
    await walkToStep3(page);

    // Precondition: before the attempt there must be no error node, or this proves nothing.
    await expect(page.getByTestId("trial-create-error")).toHaveCount(0);

    await page.getByTestId("trial-create").click();

    const error = page.getByTestId("trial-create-error");
    await expect(error, "e2e/qoder-onboarding-failure.spec.ts :: trial-create-error").toBeVisible({
      timeout: 15_000,
    });

    const text = (await error.textContent()) ?? "";
    /* Requirement 3: no silent "looks clickable again" state.  The notice has to say the
       step did not complete even when the reason itself cannot reach this screen. */
    const saysItFailed =
      /没有成功|did not complete|no journey was created/i.test(text);
    /* An unhandled server error crosses the wire as plain text, so the only thing the
       client may state is the status.  Naming a code the endpoint never sent would be the
       same lie in the other direction. */
    const namesTheStatus = text.includes(String(MEASURED_500.status));
    const inventsAReason = /V13_|VALIDATION_ERROR|arrival_anchor/i.test(text);

    const evidence = {
      generatedBy: "e345b9d6",
      sawCreateRequest,
      serverEnvelope: MEASURED_500,
      renderedText: text.trim(),
      saysItFailed,
      namesTheStatus,
      inventsAReason,
    };
    writeFileSync(
      evidencePath("ui-overnight/onboarding-create-failure.json"),
      JSON.stringify(evidence, null, 2) + "\n",
      "utf-8",
    );

    expect(sawCreateRequest, "the wizard never issued a POST /api/v1/journeys").toBe(true);
    expect(
      saysItFailed,
      "e2e/qoder-onboarding-failure.spec.ts :: a 500 stays silent — the failure notice does not " +
        `state that the step failed; it rendered: "${text.trim()}"`,
    ).toBe(true);
    expect(
      namesTheStatus,
      "e2e/qoder-onboarding-failure.spec.ts :: a 500 stays silent — the notice hides the only fact " +
        `the server actually sent: ${JSON.stringify(text.trim())}`,
    ).toBe(true);
    expect(
      inventsAReason,
      "e2e/qoder-onboarding-failure.spec.ts :: a 500 stays silent — the notice names a reason the " +
        `server never sent: ${JSON.stringify(text.trim())}`,
    ).toBe(false);
  });

  /* Split out on purpose.  Kept inside the test above, this would share one red/green
     verdict with the fix that has landed, and a suite reading could not tell "the wizard
     is no longer silent" apart from "the named reason still cannot reach this screen".
     The expected string is the envelope measured off the wire (review/probe-envelope.py):
     the generic `VALIDATION_ERROR` with `details.errors[].loc/msg`, not a code we wrote. */
  test("the named backend reason reaches the onboarding screen", async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 412, height: 915 });

    await page.route("**/api/v1/journeys", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      /* A4-1 froze the envelope: `VALIDATION_ERROR` with details.errors[].loc/msg.  This is
         the body a real create returns when an anchor location is left blank. */
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "VALIDATION_ERROR",
            message: "Request validation failed",
            trace_id: "probe-trace",
            details: {
              errors: [
                {
                  loc: ["body", "arrival_anchor", "location"],
                  msg: "String should have at least 1 character",
                  type: "string_too_short",
                },
              ],
            },
          },
        }),
      });
    });

    await openFreshWizard(page);
    await walkToStep3(page);
    await page.getByTestId("trial-create").click();

    const error = page.getByTestId("trial-create-error");
    await expect(error).toBeVisible({ timeout: 15_000 });
    const shown = (await error.innerText()).replace(/\s+/g, " ");
    await expect(
      error,
      "e2e/qoder-onboarding-failure.spec.ts :: named reason inside trial-create-error",
    ).toContainText("arrival_anchor.location");
    await expect(
      error,
      "e2e/qoder-onboarding-failure.spec.ts :: named reason inside trial-create-error",
    ).toContainText("String should have at least 1 character");
    expect(
      /^Request validation failed\s*$/.test(shown.trim()),
      "e2e/qoder-onboarding-failure.spec.ts :: named reason inside trial-create-error — the panel " +
        `shows only the constant headline: ${JSON.stringify(shown.trim())}`,
    ).toBe(false);
  });

  /* ORD-0044 -- the acceptance item.  Nothing is mocked here: a 4-day window is legal by a
     wide margin, so if the success path advances, `.v8-product-shell` must appear inside
     five seconds.  Remove the stay-options/generate call from createJourney and this node
     goes red while the journeys row still exists -- a half-created trip, which is precisely
     what the user was clicking "retry" against. */
  test("legal-window-create-advances — a legal trip reaches the app within 5s", async ({ page }) => {
    test.setTimeout(180_000);
    const started = Date.now();
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page);
    await page.getByTestId("trial-city").fill("Tianjin");
    await page.getByTestId("trial-arrival").fill("Tianjin West Station");
    await page.getByTestId("trial-departure").fill("Tianjin Binhai Airport");
    await page.getByTestId("trial-start").fill("2026-10-09T18:00");
    await page.getByTestId("trial-end").fill("2026-10-13T15:00");
    await page.getByTestId("trial-next").click();
    await page.getByTestId("trial-next").click();
    await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 10_000 });
    await page.getByTestId("trial-create").click();

    await expect(
      page.locator(".v8-product-shell"),
      "e2e/qoder-onboarding-failure.spec.ts :: legal-window-create-advances — a legal 4-day trip " +
        "never reached the app: the create stopped after the journey row was written, which is " +
        "the 'nothing happens, retry it' report on non-erroring input",
    ).toBeVisible({ timeout: 5_000 });
    const elapsed = Date.now() - started;
    expect(
      await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id")),
      "e2e/qoder-onboarding-failure.spec.ts :: legal-window-create-advances — the shell appeared " +
        "but no journey id was stored, so the create did not really complete",
    ).toMatch(/^[0-9a-f-]{36}$/);
    console.log(`legal-window-create-advances reached the shell in ${elapsed}ms`);
  });

  /* ORD-0044's root cause: the API requires a non-empty anchor location at both ends, but
     the field looked optional and the review page showed a dash, so a legal-looking trip
     died on a 422.  Blocked before submit is right; blocked *silently* is not. */
  test("missing-anchor-is-named-before-submit", async ({ page }) => {
    test.setTimeout(120_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page);
    await page.getByTestId("trial-city").fill("Tianjin");

    await expect(
      page.getByTestId("trial-required-hint"),
      "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — the anchor " +
        "fields are required by the API but the wizard gives no reason for the blocked Next",
    ).toContainText(/抵达|离开|arrive|leave/i, { timeout: 5_000 });
    await expect(
      page.getByTestId("trial-next"),
      "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — Next was " +
        "enabled with an empty anchor, so the traveller would have met the 422 instead",
    ).toBeDisabled();

    /* A disabled primary button with no explanation is still a silent block, just moved.
       The hint has to be reachable programmatically, not only visible. */
    for (const field of ["trial-arrival", "trial-departure"]) {
      const describedBy = await page.getByTestId(field).getAttribute("aria-describedby");
      expect(
        describedBy ?? "",
        "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — " +
          `${field} is blocked as required but is not tied to the visible reason`,
      ).toContain("trial-required-hint");
      expect(
        await page.getByTestId(field).getAttribute("aria-required"),
        "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — " +
          `${field} is required by the API but does not say so`,
      ).toBe("true");
    }

    // 360px, both themes: the reason must be readable where the screen is tightest.
    await page.setViewportSize({ width: 360, height: 800 });
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme, media: "screen" });
      const readable = await page.evaluate(() => {
        const hint = document.querySelector('[data-testid="trial-required-hint"]');
        if (!hint) return { found: false };
        const style = getComputedStyle(hint);
        const rect = hint.getBoundingClientRect();
        const luminance = (r: number, g: number, b: number) => {
          const channel = (value: number) =>
            value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
          return (
            0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
          );
        };
        const parse = (colour: string) =>
          (colour.match(/([\d.]+)/g) ?? []).slice(0, 4).map(Number);
        const fg = parse(style.color);
        const bg = parse(style.backgroundColor);
        const first = luminance(fg[0], fg[1], fg[2]);
        const second = luminance(bg[0] ?? 255, bg[1] ?? 255, bg[2] ?? 255);
        return {
          found: true,
          ratio: (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05),
          clipped: rect.right > window.innerWidth + 1 || rect.left < -1,
        };
      });
      expect(
        readable.found,
        "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — no hint " +
          `element at 360px/${scheme}`,
      ).toBe(true);
      expect(
        (readable.ratio ?? 0) >= 4.5,
        "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — the reason " +
          `text is ${readable.ratio?.toFixed(2)}:1 at 360px/${scheme}, below AA`,
      ).toBe(true);
      expect(
        readable.clipped,
        "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — the reason " +
          `line overflows the viewport at 360px/${scheme}`,
      ).toBe(false);
    }

    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.getByTestId("trial-arrival").fill("Tianjin West Station");
    await page.getByTestId("trial-departure").fill("Tianjin Binhai Airport");
    await expect(
      page.getByTestId("trial-next"),
      "e2e/qoder-onboarding-failure.spec.ts :: missing-anchor-is-named-before-submit — Next stayed " +
        "blocked after every required field was filled",
    ).toBeEnabled();
  });

  /* Ruling (1b) split out on purpose: with step 1 gating the anchors, the review page can
     only reach the traveller with a blank anchor through the wizard's own persisted draft
     (ORD-0036 d).  Driven that way, the row is a real last honest signal before submit —
     and a bare "—" here turns this node red, which a walk-through could never show. */
  test("review-page-says-missing-anchor-is-required", async ({ page }) => {
    test.setTimeout(120_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    const id = "e2e/qoder-onboarding-failure.spec.ts :: review-page-says-missing-anchor-is-required — ";
    const seen: Record<string, string> = {};

    /* The wording is chosen by locale, so both arms have to speak.  The draft is seeded
       before a reload rather than by an init script: the chromium project replays a
       storageState holding a journey id (playwright.config.ts), so storage must be
       rewritten between boots or the shell skips the wizard entirely. */
    for (const arm of [
      { locale: "en", phrase: "not filled in · required" },
      { locale: "zh-CN", phrase: "未填写 · 必填" },
    ] as const) {
      await page.goto("/v8", { waitUntil: "load" });
      await page.evaluate((locale) => {
        localStorage.clear();
        localStorage.setItem("citynexus.locale", locale);
        localStorage.setItem(
          "citynexus.v8.trial_draft",
          JSON.stringify({
            step: 3,
            draft: { cityName: "Tianjin", arrivalLocation: "", departureLocation: "" },
          }),
        );
      }, arm.locale);
      await page.reload({ waitUntil: "load" });
      await expect(
        page.getByTestId("trial-setup"),
        id + "the persisted step-3 draft did not reopen the review page",
      ).toBeVisible({ timeout: 25_000 });
      await expect(
        page.getByTestId("trial-create"),
        id + "the review step rendered without its create action",
      ).toBeVisible({ timeout: 15_000 });

      for (const row of ["review-arrival", "review-departure"]) {
        const text = (await page.getByTestId(row).innerText()).trim();
        seen[`${arm.locale}.${row}`] = text;
        expect(
          text,
          id + `${row} renders ${JSON.stringify(text)} in ${arm.locale}; a bare "—" reads as an ` +
            "acceptable blank and the API rejects it with a 422",
        ).toContain(arm.phrase);
        expect(
          text.includes("—"),
          id + `${row} still shows a dash in ${arm.locale}: ${JSON.stringify(text)}`,
        ).toBe(false);
        expect(
          await page.getByTestId(row).getAttribute("data-missing"),
          id + `${row} is blank but does not say the anchor is missing`,
        ).toBe("true");
      }
    }
    writeFileSync(
      evidencePath("ui-overnight/review-missing-anchor.json"),
      JSON.stringify(
        {
          generatedBy: "e345b9d6",
          order: "ORD-0044",
          node: "review-page-says-missing-anchor-is-required",
          rendered: seen,
        },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });

  test("backend-reason-lands-on-its-own-field", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/v1/journeys", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "VALIDATION_ERROR",
            message: "Request validation failed",
            details: {
              errors: [
                {
                  loc: ["body", "arrival_anchor", "location"],
                  msg: "String should have at least 1 character",
                  type: "string_too_short",
                },
              ],
            },
          },
        }),
      });
    });
    await openFreshWizard(page);
    await walkToStep3(page);
    await page.getByTestId("trial-create").click();
    await expect(page.getByTestId("trial-create-error")).toBeVisible({ timeout: 15_000 });

    /* Go back to the field the backend named: its own sentence must be on it, not only in
       the panel headline, or the traveller has to guess which box to touch. */
    await page.getByTestId("trial-back").click();
    await page.getByTestId("trial-back").click();
    const problem = page.getByTestId("trial-arrival-problem");
    await expect(
      problem,
      "e2e/qoder-onboarding-failure.spec.ts :: backend-reason-lands-on-its-own-field — the 422 " +
        "named arrival_anchor.location but the field shows nothing",
    ).toContainText("String should have at least 1 character", { timeout: 10_000 });
    await expect(
      page.getByTestId("trial-departure"),
      "e2e/qoder-onboarding-failure.spec.ts :: backend-reason-lands-on-its-own-field — a complaint " +
        "about the arrival anchor was smeared onto the departure field too",
    ).not.toHaveAttribute("aria-describedby", /departure-problem/);
  });
});
