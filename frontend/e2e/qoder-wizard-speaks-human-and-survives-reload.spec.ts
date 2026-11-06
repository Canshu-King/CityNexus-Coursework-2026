/**
 * ORD-0135 (high) -- the create-a-trip wizard has to survive a refresh, speak the traveller's
 * language on the confirmation step, and turn "还需要填写" into a route rather than a diagnosis.
 *
 * Three nodes, all at 390x844 with touch on the production build, because that is the phone the
 * order was written from:
 *  N1 fill to step 3, refresh, and the traveller must still be on step 3 with her own values;
 *  N2 the confirmation step may print no internal key -- `relaxed`, `balanced`, `packed`,
 *     `food`, `culture` are this product's vocabulary, not hers (the leak measured at
 *     `JourneyTrialSetup.tsx`, which printed `draft.pace` straight out; the option table with
 *     轻松/适中/紧凑 already existed 600 lines above it).  Note the third key is `packed`, not the
 *     `strict` named in the order -- corrected against the file, not assumed;
 *  N3 with the step blocked, every named missing field has to be a control that moves focus to
 *     that field.  A sentence that says "抵达地点" is a diagnosis; the traveller still has to find
 *     the box on a 390px screen.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-wizard-speaks-human-and-survives-reload.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

/** Internal tokens that must never reach the traveller's screen. */
const LEAKS = [
  "relaxed",
  "balanced",
  "packed",
  "strict",
  "food",
  "nature",
  "culture",
  "seaside",
  "shopping",
  "family",
];

async function openWizard(page: Page): Promise<void> {
  /* The seeded storageState carries `citynexus.v8.journey_id`, which would open Today instead of the
     wizard.  Replayed on every navigation on purpose: node 1 refreshes mid-form and must still be
     journey-less when it comes back, or it would be measured against a loaded trip. */
  await page.addInitScript(() => {
    localStorage.removeItem("citynexus.v8.journey_id");
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
}

async function fillStep1(page: Page): Promise<void> {
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 20_000 });
}

async function fillStep2(page: Page): Promise<void> {
  await page.getByTestId("trial-party").fill("2");
  await page.getByTestId("trial-budget").fill("1200");
  const interest = page.getByTestId("trial-interest-food");
  if (await interest.count()) await interest.click();
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 20_000 });
}

/** Which element actually holds the caret -- focus claimed is not focus delivered. */
async function focusedTestId(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    if (!active) return null;
    return active.dataset.testid ?? active.id ?? active.tagName.toLowerCase();
  });
}

test("ord0135-the-wizard-survives-a-refresh-on-the-step-you-were-on — 填到第 3 步刷新，还在第 3 步且字还在", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0135-the-wizard-survives-a-refresh-on-the-step-you-were-on";
  await openWizard(page);
  await fillStep1(page);
  await fillStep2(page);
  const before = {
    city: await page.getByTestId("review-city").innerText(),
    pace: await page.getByTestId("review-pace").innerText(),
  };
  await page.reload({ waitUntil: "load" });
  await expect(
    page.getByTestId("trial-step3-body"),
    node(
      NODE,
      `after a refresh the wizard is not on step 3 (it had ${JSON.stringify(before)}) -- a phone reload in the middle of a form sends the traveller back to the beginning`,
    ),
  ).toBeVisible({ timeout: 20_000 });
  const after = {
    city: await page.getByTestId("review-city").innerText(),
    pace: await page.getByTestId("review-pace").innerText(),
  };
  expect(
    after,
    node(NODE, `the step came back but the answers did not: before ${JSON.stringify(before)}, after ${JSON.stringify(after)}`),
  ).toEqual(before);
  writeFileSync(
    evidencePath("ui-overnight/ord0135-draft-refresh.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", before, after }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0135-the-confirmation-step-prints-no-internal-key — 确认页不许把 relaxed 这类内部枚举给用户看", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0135-the-confirmation-step-prints-no-internal-key";
  await openWizard(page);
  await fillStep1(page);
  await fillStep2(page);
  /* ORD-0208 3 added a 我们替你填的 marker to this row, so the cell is no longer exactly the pace
     word -- read the pace value before the marker instead of comparing the whole cell. */
  const paceCell = ((await page.getByTestId("review-pace").innerText()) ?? "").trim();
  const pace = paceCell.split("·")[0].trim();
  const interests = ((await page.getByTestId("review-interests").innerText()) ?? "").trim();
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  expect(
    ["轻松", "适中", "紧凑"].includes(pace),
    node(NODE, `the confirmation step prints 节奏 as "${pace}" -- an internal enum shown to the traveller instead of 轻松/适中/紧凑`),
  ).toBe(true);
  const leaked = LEAKS.filter((token) => new RegExp(`(^|[^a-z])${token}([^a-z]|$)`, "i").test(body));
  expect(
    leaked,
    node(NODE, `the wizard's own vocabulary reaches the screen: ${JSON.stringify(leaked)} (pace "${pace}", interests "${interests}")`),
  ).toEqual([]);
  writeFileSync(
    evidencePath("ui-overnight/ord0135-review-words.json"),
    JSON.stringify(
      { generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", pace, interests, leaked, body: body.slice(0, 400) },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0135-review-words.png"), fullPage: true });
});

test("ord0135-the-review-step-button-carries-what-the-screen-already-says — 确认创建也要反映缺项", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0135-the-review-step-button-carries-what-the-screen-already-says";
  const evidence: Record<string, unknown> = { node: NODE };

  /* The commander measured this on the deployed bundle: the review step printed 「抵达 未填写 · 必填」
     while 确认创建 was fully pressable with an empty `trial-next-reason` slot.  The screen already
     knows; this node is about that knowledge reaching the control and the announcement. */

  /* --- Line 4 first (the reverse direction): a complete draft must leave the CTA enabled, or the
         fix would blunt the normal path.  Measured before anything is cleared. */
  await openWizard(page);
  await fillStep1(page);
  await fillStep2(page);
  const create = page.getByTestId("trial-create");
  await expect(create).toBeVisible({ timeout: 10_000 });
  /* `isDisabled()` counts `aria-disabled` as disabled too, so it cannot tell the two apart -- and
     telling them apart is what this node is about.  The native attribute is the only reading that
     answers it. */
  const completeState = {
    ariaDisabled: await create.getAttribute("aria-disabled"),
    hardDisabled: await create.getAttribute("disabled"),
  };
  expect(
    completeState.ariaDisabled,
    node(NODE, `a complete draft left 确认创建 in the blocked state: ${JSON.stringify(completeState)}`),
  ).toBeNull();
  expect(
    completeState.hardDisabled,
    node(NODE, `a complete draft hard-disabled 确认创建: ${JSON.stringify(completeState)}`),
  ).toBeNull();

  /* --- Lines 1-3: clear an anchor and come back to the review step.  Done by editing the stored
         draft and reloading -- the same route the commander's measurement took, and the one a
         traveller hits when a draft survives a reload incomplete.  Clicking 下一步 back up through
         step 1 is not available any more, by design: with the anchor cleared that button is exactly
         what this node asserts is blocked. */
  await page.evaluate(() => {
    const raw = localStorage.getItem("citynexus.v8.trial_draft");
    if (!raw) return;
    const parsed = JSON.parse(raw) as { step?: number; draft?: Record<string, unknown> };
    parsed.step = 3;
    parsed.draft = { ...(parsed.draft ?? {}), arrivalLocation: "" };
    localStorage.setItem("citynexus.v8.trial_draft", JSON.stringify(parsed));
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByTestId("review-arrival"),
    node(NODE, "clearing the arrival anchor did not reach the review step's own display"),
  ).toContainText(/未填写/);

  const blockedState = {
    ariaDisabled: await create.getAttribute("aria-disabled"),
    hardDisabled: await create.getAttribute("disabled"),
    describedBy: await create.getAttribute("aria-describedby"),
  };
  expect(
    blockedState.ariaDisabled,
    node(NODE, `the screen says 抵达 未填写 · 必填 and 确认创建 still carries no blocked state: ${JSON.stringify(blockedState)}`),
  ).toBe("true");
  expect(
    blockedState.hardDisabled,
    node(NODE, `确认创建 uses a hard disabled attribute, which drops it out of the tab sequence and makes its reason unreachable: ${JSON.stringify(blockedState)}`),
  ).toBeNull();
  /* Focusable for the same reason: the argument for aria-disabled over disabled is that the reason
     beside the button stays reachable. */
  await create.focus();
  expect(
    await focusedTestId(page),
    node(NODE, "the blocked 确认创建 cannot take focus, so a keyboard traveller never reaches its reason"),
  ).toBe("trial-create");

  const reasonId = blockedState.describedBy;
  expect(
    reasonId,
    node(NODE, `the blocked 确认创建 does not point at its reason: ${JSON.stringify(blockedState)}`),
  ).not.toBeNull();
  const reasonText = ((await page.locator(`#${reasonId}`).innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    /抵达/.test(reasonText) && /必填|填写/.test(reasonText),
    node(NODE, `the reason slot reads ${JSON.stringify(reasonText)}; it has to name the field (抵达) and that it is required`),
  ).toBe(true);
  expect(
    reasonText.includes("未填写 · 必填"),
    node(NODE, `the reason does not carry the sentence the screen already shows: ${JSON.stringify(reasonText)}`),
  ).toBe(true);

  /* A refused tap has to speak, not sit silent (the order allows "refuse then report", not "refuse
     silently").  The region is rewritten on a short timer, so it is awaited rather than read once. */
  const spoken = page.getByTestId("trial-next-reason");
  await create.click({ force: true }).catch(() => undefined);
  await expect(
    spoken,
    node(NODE, "activating the blocked 确认创建 announced nothing, so a tap gives no feedback"),
  ).toHaveText(/还需要填写/, { timeout: 10_000 });
  const statuses = await page.getByRole("status").all();
  const statusTexts: string[] = [];
  for (const element of statuses) {
    const text = ((await element.textContent()) ?? "").replace(/\s+/g, " ").trim();
    if (/还需要填写/.test(text)) statusTexts.push((await element.getAttribute("data-testid")) ?? "unnamed");
  }
  expect(
    statusTexts,
    node(NODE, `the reason text is in the DOM but no status region carries it, so nothing is announced: ${JSON.stringify(statusTexts)}`),
  ).toContain("trial-next-reason");

  /* The jump: focus must land on the field the label names, from the review step. */
  const jumps = page.getByTestId("trial-missing-jump");
  const jumpCount = await jumps.count();
  expect(
    jumpCount,
    node(NODE, "the review step names a missing field but offers no way to reach it"),
  ).toBeGreaterThan(0);
  const arrivalJump = jumps.filter({ hasText: "抵达" }).first();
  await arrivalJump.click();
  await page.waitForTimeout(300);
  expect(
    await focusedTestId(page),
    node(NODE, "the jump for 抵达地点 did not move the caret to the field it names"),
  ).toBe("trial-arrival");

  writeFileSync(
    evidencePath("ui-overnight/ord0135-review-blocked.json"),
    JSON.stringify({ generatedBy: "e345b9d6", ...evidence, completeState, blockedState, reasonText, statusTexts, jumpCount }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0135-review-blocked.png"), fullPage: true });
});

test("ord0135-a-missing-field-is-a-route-not-only-a-diagnosis — 「还需要填写」要点得过去", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0135-a-missing-field-is-a-route-not-only-a-diagnosis";
  await openWizard(page);
  /* Do not `click()` this control: Playwright refuses to click an `aria-disabled` element, and the
     wizard uses `aria-disabled` deliberately (ORD-0066 甲 measured that a hard `disabled` dropped
     the button out of the tab sequence, so the reason parked beside it was never reached).  The
     order asks for the disabled state to be expressed accessibly and for the block to still be
     walkable -- so both halves are asserted here instead of being clicked past. */
  const next = page.getByTestId("trial-next");
  await expect(next).toHaveAttribute("aria-disabled", "true", { timeout: 10_000 });
  const nextHandle = await next.elementHandle();
  await nextHandle?.focus();
  expect(
    await focusedTestId(page),
    node(NODE, "the blocked 下一步 is not focusable, so a keyboard traveller never hears its reason"),
  ).toBe("trial-next");
  const hint = page.getByTestId("trial-required-hint");
  await expect(
    hint,
    node(NODE, "the step is blocked but no visible reason is offered"),
  ).toBeVisible({ timeout: 10_000 });
  const line = ((await hint.innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    /还需要填写/.test(line),
    node(NODE, `the blocked hint reads "${line}" instead of naming the missing fields`),
  ).toBe(true);
  const jumps = page.getByTestId("trial-missing-jump");
  const count = await jumps.count();
  expect(
    count,
    node(NODE, `the hint lists missing fields but offers no way to reach them (${count} jump controls)`),
  ).toBeGreaterThan(0);
  const landed: Record<string, string | null> = {};
  for (let index = 0; index < count; index += 1) {
    const jump = jumps.nth(index);
    const target = (await jump.getAttribute("data-target")) ?? "";
    await jump.click();
    landed[target] = await focusedTestId(page);
    expect(
      landed[target],
      node(NODE, `the jump for "${(await jump.innerText()).trim()}" did not move the caret to ${target} (focus landed on ${landed[target]})`),
    ).toBe(target);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0135-missing-jumps.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", hintLine: line, focusAfterEachJump: landed }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0135-missing-jumps.png"), fullPage: true });
});

test("ord0216-a-fixed-field-leaves-the-hint-on-the-next-step — 补上的缺项不许跟着进确认屏", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0216-a-fixed-field-leaves-the-hint-on-the-next-step";
  /* HJ-P1's 2/2 (15:04): with 总预算 empty, step 2 blocks naming it; after filling it the stale
     「还需要填写：总预算」 still hung on step 3 -- where 201s would otherwise already have gone
     through.  The hint has to be recomputed from the current values wherever the traveller is. */
  await openWizard(page);
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 15_000 });

  /* Block step 2 on the budget, so the hint names it.  The blocked 下一步 carries
     `aria-disabled` and Playwright refuses to click it (this lane's own trap -- hit twice while
     writing this very node), so the blocked state is asserted, never clicked. */
  await page.getByTestId("trial-budget").fill("0");
  const hint = page.getByTestId("trial-required-hint");
  await expect(
    hint,
    node(NODE, "an empty budget blocked step 2 without naming it"),
  ).toContainText(/总预算/, { timeout: 10_000 });

  /* Fix the field via the jump the hint itself offers (ORD-0135 3's route), then advance: the
     sentence must drop 总预算 by the time the traveller reaches step 3. */
  await page.getByTestId("trial-budget").fill("1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });
  const bodyText = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  expect(
    /还需要填写：[^。]*总预算/.test(bodyText),
    node(NODE, `the fixed budget still hangs in the hint on step 3: "${bodyText.slice(0, 260)}"`),
  ).toBe(false);
  /* The typed value survives onto the review row. */
  const budget = ((await page.getByTestId("review-budget").innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    budget.includes("1500"),
    node(NODE, `the typed budget is not what the review shows: "${budget}"`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0216-stale-hint.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        regression: "总预算 was blocked on step 2, fixed, and must not reappear in the step-3 hint",
        budgetOnReview: budget,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
