import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0066 -- the wizard's blocked "Next" has to be reachable by the travellers who cannot see
 * the screen, not merely carry an attribute.
 *
 * The Commander measured the premise on the rebuilt production bundle (raw/focus_premise.json):
 * a `disabled` primary button cannot take focus at all, and injecting `aria-describedby` onto it
 * did not change that (focus_landed stayed false while a control input focused fine).  So an
 * attribute-only fix satisfies the old assertion and still leaves a screen-reader traveller with
 * no way to reach the reason.  This file therefore asserts the reachability itself -- focus, tab
 * order, and the accessible description resolved through the accessibility tree -- and every
 * measurement comes with a control that proves the instrument can answer "false".
 *
 * Route chosen: 甲 (aria-disabled, control stays focusable, refused activation speaks the reason).
 */

const FILE = "e2e/qoder-wizard-blocked-next-a11y.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

async function openFreshWizard(page: Page, locale: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await openScreen(page, "/v8", ["trial-setup"]);
}

const COPY = {
  "zh-CN": {
    next: "下一步",
    reason: /还需要填写/,
    city: "目的地",
    garbageCity: "拉萨市不存在的写法",
    fieldWarning: /没认出/,
    arrival: "拉萨贡嘎机场",
    departure: "拉萨火车站",
  },
  en: {
    next: "Next",
    reason: /Still needed/,
    city: "Destination",
    garbageCity: "Zzznotacity",
    fieldWarning: /not recognised/,
    arrival: "airport",
    departure: "station",
  },
} as const;

/* Where focus actually landed, read the same way the Commander's probe read it: call focus(),
   then ask the document.  `probe` returns the instrument's own discrimination too -- a <p> is
   not focusable, so a harness that reports true for it is broken, not the product. */
async function focusProbe(page: Page, selector: string) {
  return page.evaluate((target) => {
    const el = document.querySelector(target) as HTMLElement | null;
    if (!el) return { found: false, landed: false, active: "missing" };
    const before = document.activeElement?.getAttribute("data-testid") ?? document.activeElement?.tagName ?? "none";
    el.focus();
    const active = document.activeElement as HTMLElement | null;
    return {
      found: true,
      landed: active === el,
      active: active?.getAttribute("data-testid") ?? active?.tagName ?? "none",
      before,
    };
  }, selector);
}

/* Walk the real keyboard path: Tab until the control appears or the page runs out. */
async function tabWalker(page: Page, fromTestId: string, limit = 18) {
  await page.getByTestId(fromTestId).focus();
  const seen: string[] = [];
  for (let i = 0; i < limit; i += 1) {
    await page.keyboard.press("Tab");
    const active = await page.evaluate(
      () => document.activeElement?.getAttribute("data-testid") ?? document.activeElement?.tagName ?? "none",
    );
    seen.push(active);
    if (active === "trial-next" || active === "HTML") break;
  }
  return seen;
}

test.describe("ORD-0066 the wizard's blocked Next can be reached and heard", () => {
  for (const [locale, copy] of Object.entries(COPY)) {
    test(`a11y-blocked-next-takes-focus [${locale}] — reachability, with both controls`, async ({
      page,
    }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 360, height: 800 });
      await openFreshWizard(page, locale);
      await page.getByTestId("trial-city").fill(copy.garbageCity);

      // The anchors are still empty, so the button is blocked -- the state under test.
      await expect(page.getByTestId("trial-required-hint")).toBeVisible();

      const control = await focusProbe(page, '[data-testid="trial-city"]');
      expect(
        control.landed,
        node("a11y-blocked-next-takes-focus",
          `the instrument cannot focus a plain input, so no focus reading here means anything: ${JSON.stringify(control)}`),
      ).toBe(true);

      const negative = await focusProbe(page, "#trial-required-hint");
      expect(
        negative.landed,
        node("a11y-blocked-next-takes-focus",
          `a non-interactive paragraph took focus, so "landed" is not measuring focusability: ${JSON.stringify(negative)}`),
      ).toBe(false);

      const next = await focusProbe(page, '[data-testid="trial-next"]');
      expect(
        next.found,
        node("a11y-blocked-next-takes-focus", "there is no trial-next button to measure"),
      ).toBe(true);
      expect(
        next.landed,
        node("a11y-blocked-next-takes-focus",
          `the blocked Next is unreachable for a keyboard and screen-reader traveller: ${JSON.stringify(next)}`),
      ).toBe(true);

      const tabbed = await tabWalker(page, "trial-departure");
      expect(
        tabbed.includes("trial-departure") || tabbed.includes("trial-next"),
        node("a11y-blocked-next-takes-focus",
          `Tab did not move focus at all, so the walk proves nothing: ${JSON.stringify(tabbed)}`),
      ).toBe(true);
      expect(
        tabbed,
        node("a11y-blocked-next-takes-focus",
          "the blocked Next is not in the tab sequence, so a keyboard traveller steps straight over it"),
      ).toContain("trial-next");

      writeFileSync(
        evidencePath(`ui-overnight/wizard-next-focus-${locale}.json`),
        JSON.stringify(
          { generatedBy: "e345b9d6", order: "ORD-0066", node: "a11y-blocked-next-takes-focus", locale, control, negative, next, tabbed },
          null,
          2,
        ) + "\n",
        "utf-8",
      );
    });

    test(`a11y-blocked-next-carries-its-reason-in-the-a11y-tree [${locale}]`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 360, height: 800 });
      await openFreshWizard(page, locale);
      await page.getByTestId("trial-city").fill(copy.garbageCity);

      /* Control first: does an accessibility-tree description query see a relation this app
         already relies on?  The destination field is described by the unrecognised-city caution,
         so if this resolves the matcher is live and the button's own result means something. */
      await expect(
        page.getByRole("textbox", { name: new RegExp(copy.city), description: copy.fieldWarning }),
        node("a11y-blocked-next-carries-its-reason-in-the-a11y-tree",
          "the description matcher resolves nothing even for the field-level relation, so this node cannot detect a broken aria-describedby"),
      ).toHaveCount(1);

      // The requirement: focus on the button yields the missing-field sentence as its description.
      await expect(
        page.getByRole("button", { name: new RegExp(copy.next), description: copy.reason }),
        node("a11y-blocked-next-carries-its-reason-in-the-a11y-tree",
          "a screen reader landing on the blocked Next gets no accessible description, i.e. the reason is visual only"),
      ).toHaveCount(1);

      // The block itself must not have been quietly lifted: semantics unchanged.
      await expect(
        page.getByRole("button", { name: new RegExp(copy.next), disabled: true }),
        node("a11y-blocked-next-carries-its-reason-in-the-a11y-tree",
          "the wizard now lets a traveller advance with required fields empty"),
      ).toHaveCount(1);

      /* And the relation is gone when there is nothing to say, so it cannot be a decoration. */
      await page.getByTestId("trial-arrival").fill(copy.arrival);
      await page.getByTestId("trial-departure").fill(copy.departure);
      await expect(
        page.getByRole("button", { name: new RegExp(copy.next), description: copy.reason }),
        node("a11y-blocked-next-carries-its-reason-in-the-a11y-tree",
          "the reason sentence stays attached to a button that is no longer blocked"),
      ).toHaveCount(0);
    });

    test(`a11y-refused-tap-speaks-and-does-not-advance [${locale}]`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: "light", media: "screen" });
      await page.setViewportSize({ width: 360, height: 800 });
      await openFreshWizard(page, locale);
      await page.getByTestId("trial-city").fill(copy.garbageCity);

      await page.getByTestId("trial-next").focus();
      await page.keyboard.press("Enter");

      /* The region is rewritten on a short timer -- a live region that never changes its text
         never speaks twice -- so this has to be awaited, not read once. */
      await expect(
        page.getByTestId("trial-next-reason"),
        node("a11y-refused-tap-speaks-and-does-not-advance",
          "activating the blocked Next is silent: nothing is announced, so the traveller gets no feedback"),
      ).toHaveText(copy.reason, { timeout: 10_000 });
      const spoken = (await page.getByTestId("trial-next-reason").innerText()) ?? "";

      /* Text in the DOM is not an announcement.  Ask the accessibility tree which elements it
         treats as a status region, and require the spoken one to be among them -- stripping the
         role leaves the words on screen and this red.  The visible hint is the control that
         proves this enumeration sees a status region in this app at all. */
      const statuses = await page.getByRole("status").all();
      const statusTestIds: string[] = [];
      for (const element of statuses) {
        const text = ((await element.textContent()) ?? "").replace(/\s+/g, " ").trim();
        if (!copy.reason.test(text)) continue;
        statusTestIds.push((await element.getAttribute("data-testid")) ?? "unnamed");
      }
      expect(
        statusTestIds,
        node("a11y-refused-tap-speaks-and-does-not-advance",
          `the a11y tree sees no status region carrying the reason (found: ${JSON.stringify(statusTestIds)})`),
      ).toContain("trial-required-hint");
      expect(
        statusTestIds,
        node("a11y-refused-tap-speaks-and-does-not-advance",
          `the refused activation writes the reason into a node the accessibility tree does not treat as a live status region, so nothing is announced: ${JSON.stringify(statusTestIds)}`),
      ).toContain("trial-next-reason");

      await expect(
        page.getByTestId("trial-step1-body"),
        node("a11y-refused-tap-speaks-and-does-not-advance",
          "the refused activation advanced the wizard past empty required fields"),
      ).toBeVisible();

      /* Positive control for the same key on the same button: once the fields are filled the
         activation does advance, so the reading above is a refusal and not a dead probe. */
      await page.getByTestId("trial-arrival").fill(copy.arrival);
      await page.getByTestId("trial-departure").fill(copy.departure);
      await page.getByTestId("trial-next").focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByTestId("trial-step2-body"),
        node("a11y-refused-tap-speaks-and-does-not-advance",
          "Enter never activates this button even when it is ready, so the earlier 'did not advance' proved nothing"),
      ).toBeVisible({ timeout: 15_000 });

      writeFileSync(
        evidencePath(`ui-overnight/wizard-next-spoken-${locale}.json`),
        JSON.stringify(
          { generatedBy: "e345b9d6", order: "ORD-0066", node: "a11y-refused-tap-speaks-and-does-not-advance", locale, spoken: spoken.trim() },
          null,
          2,
        ) + "\n",
        "utf-8",
      );
    });
  }

  test("a11y-step2-block-is-named-too — no new silent blocker one step down", async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 360, height: 800 });
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-city").fill("天津");
    await page.getByTestId("trial-arrival").fill("天津西站");
    await page.getByTestId("trial-departure").fill("天津滨海国际机场");
    await page.getByTestId("trial-next").click();
    await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 15_000 });

    await page.getByTestId("trial-budget").fill("0");
    const hint = page.getByTestId("trial-required-hint");
    await expect(
      hint,
      node("a11y-step2-block-is-named-too",
        "step 2 blocks the primary button again without saying which field it is waiting on"),
    ).toContainText(/总预算|旅行人数/);
    await expect(
      page.getByRole("button", { name: /下一步/, description: /还需要填写/ }),
      node("a11y-step2-block-is-named-too", "step 2's reason is visible but not reachable in the a11y tree"),
    ).toHaveCount(1);
    const next = await focusProbe(page, '[data-testid="trial-next"]');
    expect(
      next.landed,
      node("a11y-step2-block-is-named-too", `step 2's blocked Next cannot take focus: ${JSON.stringify(next)}`),
    ).toBe(true);

    await page.getByTestId("trial-budget").fill("3000");
    await expect(
      page.getByTestId("trial-required-hint"),
      node("a11y-step2-block-is-named-too", "the hint outstays the problem"),
    ).toHaveCount(0);
  });
});
