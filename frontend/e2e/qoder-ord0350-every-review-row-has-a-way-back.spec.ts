/**
 * ORD-0350 -- every row on the confirmation step carries a named exit that lands on the control
 * editing that value, with the value still there.
 *
 * The confirmation step tells a traveller which answers this device filled in for them; until this
 * order the only way back to one of them was to leave the review step and hunt for the control.  This
 * spec walks the real wizard: it reads the rows, presses a step-2 exit and a step-1 exit, and checks
 * the step, the focused control and the surviving value -- plus the absence that makes the press
 * meaningful (on the review step the editing control is not in the DOM at all).
 *
 * It also re-measures the ORD-0135 ③ jump, whose click handler this order folded into the same route
 * rather than duplicating.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0350-every-review-row-has-a-way-back.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const KEY = "citynexus.v8.trial_draft";

/** City, dates and places carry values; party / budget / pace stay at defaults so the step prints
 *  the 我们替你填的 marks HJ-P3 read on the phone. */
const DRAFT = {
  cityName: "天津",
  arrivalLocation: "天津站",
  departureLocation: "天津滨海国际机场",
  interests: [],
  specialRequirements: "",
};

async function openReview(page: Page, draft: unknown = DRAFT) {
  /* The product's own boot decides to show the wizard: no stored journey and a step-3 draft in the
     key the wizard itself restores from.  Mounting a copy of the component in a harness would have
     put a second wizard on the page, and every locator would have been ambiguous -- the app's own
     wizard is the thing under test. */
  await page.addInitScript(
    ([key, seed]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.storage-seeded", "1");
      localStorage.setItem("citynexus.locale", "zh-CN");
      localStorage.removeItem("citynexus.v8.journey_id");
      localStorage.setItem(key, JSON.stringify({ step: 3, draft: seed }));
    },
    [KEY, draft] as const,
  );
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.waitForSelector('[data-testid="trial-step3-body"]', { timeout: 30_000 });
}

const activeTestId = (page: Page) =>
  page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return "NONE";
    return el.getAttribute("data-testid") ?? el.id ?? el.tagName;
  });

const ROWS = [
  { name: "city", label: "目的地", target: "trial-city", step: "trial-step1-body" },
  { name: "arrival", label: "抵达", target: "trial-arrival", step: "trial-step1-body" },
  { name: "departure", label: "离开", target: "trial-departure", step: "trial-step1-body" },
  { name: "party", label: "人数 / 房间", target: "trial-party", step: "trial-step2-body" },
  { name: "budget", label: "总预算 / 住宿", target: "trial-budget", step: "trial-step2-body" },
  { name: "pace", label: "节奏", target: "trial-pace-fieldset", step: "trial-step2-body" },
  { name: "interests", label: "兴趣", target: "trial-interests-fieldset", step: "trial-step2-body" },
  { name: "special", label: "特殊要求", target: "trial-special", step: "trial-step2-body" },
  /* The auto-set row edits three values at once and its controls sit behind a disclosure on step 1,
     so its exit names that disclosure instead of pretending to reach a hidden input. */
  { name: "auto-set", label: "自动设置", target: "trial-show-advanced", step: "trial-step1-body" },
];

test("every review row names its own exit, and each exit says which item it edits", async ({ page }) => {
  await openReview(page);
  const seen = [];
  for (const row of ROWS) {
    const exit = page.getByTestId(`review-edit-${row.name}`);
    await expect(exit, node("exits", `row ${row.name} carries no exit`)).toBeVisible();
    /* The visible text is short; the item is named in the accessible name, because a review row that
       reads "改这一项：人数 / 房间 改这一项：人数 / 房间" is worse for the traveller than either alone. */
    const accessible = (await exit.getAttribute("aria-label") ?? "").trim();
    expect(accessible.includes(row.label), node("exits", `exit ${row.name} does not name its item: ${accessible}`)).toBe(true);
    expect(await exit.getAttribute("data-target"), node("exits", `exit ${row.name} names no target`)).toBe(row.target);
    seen.push({ name: row.name, accessible, target: row.target });
  }
  writeFileSync(evidencePath("ord0350-exists.json"), JSON.stringify({ rowCount: seen.length, rows: seen }, null, 1));
  expect(seen.length, node("exits", "row count drifted from the list this spec walks")).toBe(ROWS.length);
});

test("a step-2 exit mounts that step, focuses the control, and the value is still there", async ({ page }) => {
  await openReview(page);
  /* Absence first: while the review step is up the editing control is not in the DOM, so a button that
     merely scrolled, or only went "back one step", could not pass what follows. */
  expect(
    await page.locator('[data-testid="trial-party"]').count(),
    node("press", "the step-2 control was already mounted on the review step"),
  ).toBe(0);

  await page.getByTestId("review-edit-party").click();
  await expect(
    page.getByTestId("trial-step2-body"),
    node("press", "the exit did not mount the step that owns the field"),
  ).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(250);
  expect(await activeTestId(page), node("press", "focus did not land on the party control")).toBe("trial-party");

  const before = await page.getByTestId("trial-party").inputValue();
  expect(before.length, node("press", "the value disappeared on the way back")).toBeGreaterThan(0);
  await page.getByTestId("trial-party").fill("3");
  expect(await page.getByTestId("trial-party").inputValue(), node("press", "the landing spot is not an editor")).toBe("3");
  writeFileSync(evidencePath("ord0350-step2-press.json"), JSON.stringify({ before, focused: "trial-party", afterEdit: "3" }, null, 1));
});

test("a step-1 exit does the same on the other step", async ({ page }) => {
  await openReview(page);
  await page.getByTestId("review-edit-city").click();
  await expect(
    page.getByTestId("trial-step1-body"),
    node("press", "the step-1 exit did not reach step 1"),
  ).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(250);
  expect(await activeTestId(page), node("press", "focus did not land on the city field")).toBe("trial-city");
  expect(await page.getByTestId("trial-city").inputValue(), node("press", "the city answer was lost")).toBe(DRAFT.cityName);
});

test("the ORD-0135 ③ missing-field jump still routes through the shared helper", async ({ page }) => {
  /* This order replaced that button's inline handler with the shared route, so what a shipped gate
     asserts about it is re-measured here instead of assumed intact. */
  await openReview(page, { cityName: "天津" });
  const jump = page.getByTestId("trial-missing-jump").filter({ hasText: "抵达" }).first();
  await expect(jump, node("regression", "the missing-field list lost its jump")).toBeVisible({ timeout: 15_000 });
  await jump.click();
  await page.waitForTimeout(400);
  expect(
    await activeTestId(page),
    node("regression", "the shared route no longer focuses the field its label names"),
  ).toBe("trial-arrival");
});

test("the exits leave the answer cells exactly as the other gates read them", async ({ page }) => {
  /* Three shipped gates compare a review row's own text -- `qoder-stated-versus-stored-versus-planned
     .spec.ts` reads 节奏 and 兴趣 as frozen strings.  An exit parked inside the answer cell would
     have quietly reddened them, so the cell must carry no button text at all. */
  await openReview(page);
  const cells = {};
  for (const testId of ["review-pace", "review-interests", "review-party", "review-budget", "review-city"]) {
    cells[testId] = (await page.getByTestId(testId).innerText()).replace(/\s+/g, " ").trim();
  }
  writeFileSync(evidencePath("ord0350-answer-cells.json"), JSON.stringify(cells, null, 1));
  for (const [testId, text] of Object.entries(cells)) {
    expect(text.includes("改这一项"), node("cells", `${testId} now carries the exit's own words`)).toBe(false);
  }
  expect(cells["review-pace"].length, node("cells", "the pace answer cell went empty")).toBeGreaterThan(0);
});
