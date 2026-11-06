/**
 * ORD-0240 -- the model's own words are dynamic content, so they cannot be "translated" by a static
 * table, and they must not sit in a Chinese screen's main flow looking like this product's promise.
 *
 * Measured source (lane report `4e974ae5`): a Beijing trip whose successful run painted
 * `Detected hard constraints: sleep in/no pre-10 plans, West Lake night view…` while the failed run's
 * Chinese fallback wording was honest.  The string is a passthrough of the agent mission's
 * `reasoning_summary`, not a dictionary entry (grep for it in this repo: 0 hits).
 *
 * Acceptance shape, per the order: ① the sentence may not appear unlabelled in a Chinese screen;
 * ② if technical detail shows it, it carries a label; ③ putting the string back into the render path
 * reddens a gate; ④ the sentence is never added to a translation table (checked structurally below:
 * the only place it can live is inside the technical disclosure, and this file holds the literals).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-model-words-are-labelled.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** The observed model sentence, copied verbatim from the screen it appeared on. */
const MODEL_WORDS =
  "Detected hard constraints: sleep in/no pre-10 plans, West Lake night view is a hard requirement.";
/** This lane's own wording for the two labelled parts of the technical disclosure. */
const FROZEN_MODEL_LABEL_ZH = "模型原始输出（未翻译，不代表本界面的结论）";
const FROZEN_REASON_LABEL_ZH = "服务器给出的原因";

async function openSheetWithModelWords(page: Page, summary: string | null, nodeId: string) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  let polls = 0;
  let planReads = 0;
  let catalogueReads = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    polls += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mission: { status: "COMPLETED", failure_reason: null, reasoning_summary: summary },
      }),
    });
  });
  /* The sheet keeps the mission's own words only on the branch where a plan really appeared; on any
     other outcome it returns early with `summary: null`, which would make "the sentence is not on
     screen" true for the wrong reason.  So the plan reads are sequenced like a run that produced
     one: empty baseline first, the new plan afterwards. */
  await page.route("**/api/v1/journeys/*/plans", async (route) => {
    planReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(planReads === 1 ? [] : [{ id: "ord0240-plan-1", status: "DRAFT" }]),
    });
  });
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) =>
    route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify([]) }),
  );
  /* The pre-flight refuses to send at all when the city catalogue holds fewer than two places, and
     the seeded journey's own city is one of those (`BLOCKED:CATALOG_EMPTY`), so the directory that
     the real run would have is supplied here. */
  await page.route("**/api/v1/places?city_code=*", async (route) => {
    catalogueReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { id: "ord0240-place-1", name: "海河游船码头" },
        { id: "ord0240-place-2", name: "天津之眼" },
      ]),
    });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想轻松玩两天，晚上看看夜景");
  await page.getByTestId("plan-ai-submit").click();
  await expect(
    page.getByTestId("plan-ai-result"),
    node(
      "ord0240-stub",
      `the sheet never answered; the mission route fired ${polls} time(s) and /plans ${planReads} time(s), so nothing below was measured on a real reply`,
    ),
  ).toBeVisible({ timeout: 60_000 });
  await expect
    .poll(() => polls, {
      timeout: 20_000,
      message: node("ord0240-stub", "the agent/commands stub never fired, so the arm is unmeasured"),
    })
    .toBeGreaterThan(0);
  const resultStatus = await page
    .getByTestId("plan-ai-result")
    .getAttribute("data-result-status");
  /* Positive control, and it earns its keep: without it a stub that lands on an early-return branch
     leaves no model text anywhere, and every "it is not painted" judgement below would pass while
     measuring nothing. */
  await expect
    .poll(
      () =>
        page.evaluate(() =>
          Array.from(document.querySelectorAll("body *")).some((el) =>
            (el.textContent ?? "").includes("Detected hard constraints"),
          ),
        ),
      {
        timeout: 15_000,
        message: node(
          nodeId,
          `no element holds the model sentence anywhere (result status "${resultStatus}") -- the run stopped in a branch that drops it, so "not on screen" below would be vacuous`,
        ),
      },
    )
    .toBe(true);
  return { polls, planReads, resultStatus };
}

/** Where the sentence actually sits, with the styles a "is it painted" argument needs. */
async function sentenceHomes(page: Page) {
  return await page.evaluate(() => {
    const needle = "Detected hard constraints";
    const out: Record<string, unknown>[] = [];
    for (const el of Array.from(document.querySelectorAll("body *"))) {
      const text = el.textContent ?? "";
      if (!text.includes(needle)) continue;
      if (Array.from(el.children).some((kid) => (kid.textContent ?? "").includes(needle))) continue;
      const style = getComputedStyle(el);
      const box = el.getBoundingClientRect();
      const details = el.closest("details");
      out.push({
        tag: el.tagName,
        testid: el.getAttribute("data-testid") ?? "",
        display: style.display,
        visibility: style.visibility,
        contentVisibility: style.contentVisibility,
        opacity: style.opacity,
        box: [Math.round(box.width), Math.round(box.height)],
        detailsOpen: details ? details.hasAttribute("open") : null,
        detailsDisplay: details ? getComputedStyle(details).display : null,
        inSrOnly: Boolean(el.closest(".v8-sr-only")),
      });
    }
    return out;
  });
}

/** The traveller-facing copy: the headline and its body paragraph, not the disclosure. */
async function mainFlowText(page: Page): Promise<string> {
  return await page.evaluate(() => {
    const result = document.querySelector('[data-testid="plan-ai-result"]');
    if (!result) return "";
    const parts: string[] = [];
    for (const child of Array.from(result.children)) {
      if (child.tagName === "DETAILS") continue;
      const style = getComputedStyle(child);
      if (style.display === "none" || style.visibility === "hidden") continue;
      parts.push((child.textContent ?? "").trim());
    }
    return parts.join(" ").replace(/\s+/g, " ");
  });
}

test("ord0240-1-no-model-sentence-in-the-main-flow — 中文主文案里不得出现模型原文", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0240-1-no-model-sentence-in-the-main-flow";
  await openSheetWithModelWords(page, MODEL_WORDS, NODE);
  const flow = await mainFlowText(page);
  expect(
    flow,
    node(NODE, `the sheet's own copy carries the model's sentence, so a traveller reads it as our promise: "${flow}"`),
  ).not.toContain("Detected hard constraints");
  /* "and while the disclosure is collapsed nothing of it is painted" is node 3's claim about the
     disclosure, and lives only there -- while both nodes asserted it, one mutant owned two nodes. */
});

test("ord0240-2-the-disclosure-says-whose-words-they-are — 展开必须写明是谁的话", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0240-2-the-disclosure-says-whose-words-they-are";
  await openSheetWithModelWords(page, MODEL_WORDS, NODE);
  /* Open it if it is not already open: this node judges the labelling, and a screen that shipped the
     disclosure open by default is node 3's defect, not this one's. */
  const details = page.locator(".plan-ai-detail");
  /* `getAttribute("open")` yields "" for a boolean attribute, which is falsy in JS: testing the value
     made an already-open disclosure look closed, this node reddened for another node's defect, and
     the matrix blamed the wrong invariant. Presence is the question. */
  const alreadyOpen = await details.first().evaluate((el) => el.hasAttribute("open"));
  if (!alreadyOpen) {
    await page.getByTestId("plan-ai-detail-toggle").click();
  }
  const label = page.getByTestId("plan-ai-model-label");
  await expect(
    label,
    node(NODE, "no label sits beside the model text, so raw output reads as product wording"),
  ).toBeVisible({ timeout: 15_000 });
  const said = (await label.innerText()).replace(/\s+/g, " ").trim();
  expect(
    said,
    node(NODE, `the label is not the frozen sentence: expected "${FROZEN_MODEL_LABEL_ZH}" / got "${said}"`),
  ).toBe(FROZEN_MODEL_LABEL_ZH);
  const raw = (await page.getByTestId("plan-ai-model-output").innerText()).replace(/\s+/g, " ").trim();
  expect(
    raw,
    node(NODE, `the raw model text was rewritten instead of quoted: got "${raw}"`),
  ).toBe(MODEL_WORDS);
  const scan = await scanScreenText(page);
  for (const needle of [FROZEN_MODEL_LABEL_ZH, MODEL_WORDS]) {
    expect(
      scan.accessible,
      node(NODE, `opening the disclosure still hides "${needle.slice(0, 40)}" from the announced layer`),
    ).toContain(needle);
  }
  writeFileSync(
    evidencePath("ui-overnight/ord0240-2-labelled-output.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        label: said,
        rawModelText: raw,
        rule: "原文照引且标明\"模型原始输出（未翻译）\"；不进任何字典",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0240-3-the-only-home-for-raw-words-is-the-technical-disclosure — 原文唯一的住处", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0240-3-the-only-home-for-raw-words-is-the-technical-disclosure";
  await openSheetWithModelWords(page, MODEL_WORDS, NODE);
  const where = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("body *")).filter((el) =>
      (el.textContent ?? "").includes("Detected hard constraints"),
    );
    /* the innermost element actually holding the text */
    const leaf = nodes[nodes.length - 1];
    if (!leaf) return { present: false, insideDetails: false, tag: "", testid: "" };
    return {
      present: true,
      insideDetails: Boolean(leaf.closest("details")),
      tag: leaf.tagName,
      testid: leaf.getAttribute("data-testid") ?? "",
    };
  });
  expect(
    where,
    node(NODE, `the model sentence has no rendered home at all: ${JSON.stringify(where)}`),
  ).toMatchObject({ present: true });
  expect(
    where.insideDetails,
    node(NODE, `the raw text sits outside the technical disclosure: ${JSON.stringify(where)}`),
  ).toBe(true);
  expect(
    where.testid,
    node(NODE, `the raw text is not in the quoted element a reader can identify: ${JSON.stringify(where)}`),
  ).toBe("plan-ai-model-output");
  /* This node owns the disclosure, so it also owns "collapsed means not painted": a `<details open>`
     by default, or any other way of spilling the technical layer into the first screen, reddens here
     while node 1's copy check stays green. */
  const scan = await scanScreenText(page);
  const homes = await sentenceHomes(page);
  expect(
    scan.visual,
    node(
      NODE,
      `the model sentence is painted while the technical detail is still collapsed; homes=${JSON.stringify(homes)}`,
    ),
  ).not.toContain("Detected hard constraints");
  /* Each part of the disclosure appears only when it has content: this reply carries the model's
     words and no failure reason, so the reason label must not be on screen either. */
  await expect
    .poll(() => page.getByTestId("plan-ai-detail-kind").count(), {
      timeout: 10_000,
      message: node(
        NODE,
        `a "${FROZEN_REASON_LABEL_ZH}" label appeared for a reply that gave no failure reason`,
      ),
    })
    .toBe(0);
  await page.getByTestId("plan-ai-detail-toggle").click();
  expect(
    (await mainFlowText(page)).includes("Detected hard constraints"),
    node(NODE, "opening the disclosure also painted the sentence into the main flow"),
  ).toBe(false);
});
