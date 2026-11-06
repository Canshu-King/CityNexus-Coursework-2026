/**
 * ORD-0250 -- "开始规划" with an empty request used to be a silent refusal: no request left the
 * browser (the backend log for that press holds no POST) and the screen said nothing.  The grey
 * example sentence sits in the field as a placeholder, so the box reads as answerable and the tap
 * reads as ignored -- the same family as the wizard that used to swallow the traveller.
 *
 * Accepted shape (the order's own words): either keep the control disabled AND say what is missing on
 * screen, or explain on press.  This lane does both halves at once and is explicitly NOT allowed to
 * buy green by filling the field for the traveller, so every node below re-reads the field's value.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-empty-request-says-what-is-missing.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** Held by this gate; the product constant is never imported. */
const FROZEN_REASON_ZH = "还差你自己的一句话：上面灰色的例子只是提示，不算你说的需求，所以我们没有东西可以拿去排。";
/** The floor as it stands in code today, asserted as a boundary rather than trusted.  These fixtures
 *  are 7 and 8 *characters* (one CJK glyph = one `length` unit), not 7 and 8 words. */
const MIN_LENGTH = 8;
const SEVEN = "我想慢慢逛两天";
const EIGHT = "我想慢慢逛两天呀";

async function openEmptySheet(page: Page) {
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
  let posts = 0;
  /* Registered before any navigation, and counted rather than answered: a refused press must not
     create a request at all, so the only correct stub behaviour here is "resolve fast if it fires". */
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    posts += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mission: { status: "COMPLETED", failure_reason: null, reasoning_summary: null } }),
    });
  });
  await page.route("**/api/v1/journeys/*/plans", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }),
  );
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  return { posts: () => posts, journeyId };
}

const input = (page: Page) => page.getByTestId("plan-ai-input");
const hint = (page: Page) => page.getByTestId("plan-ai-empty-request");

test("ord0250-1-a-refused-press-leaves-a-reason-on-the-screen — 点了不说为什么，就是没点", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const counter = await openEmptySheet(page);
  const box = input(page);

  // The trap this order is about: the placeholder makes an empty field look answerable.
  const placeholder = await box.getAttribute("placeholder");
  expect(placeholder && placeholder.length > 0, node("ord0250-1", "no placeholder example is shown")).toBeTruthy();
  expect((await box.inputValue()) === "", node("ord0250-1", "the field is not empty")).toBe(true);

  /* `{ force: true }` because Playwright reads aria-disabled="true" as "not actionable" and waits
     forever -- this lane's own trap list, item 1.  A real browser does deliver the click to an
     aria-disabled control, and what the traveller sees afterwards is the whole subject of this order. */
  await page.getByTestId("plan-ai-submit").click({ force: true });

  await expect(
    hint(page),
    node(
      "ord0250-1",
      `the press was refused with no word on screen (posts=${counter.posts()}); the traveller tapped and nothing answered`,
    ),
  ).toBeVisible({ timeout: 10_000 });
  const painted = (await hint(page).innerText()).trim();
  expect(painted, node("ord0250-1", `the reason read "${painted}"`)).toBe(FROZEN_REASON_ZH);

  const scan = await scanScreenText(page);
  for (const [layer, text] of [
    ["painted", scan.visual],
    ["announced", scan.accessible],
  ] as const) {
    expect(
      text.includes(FROZEN_REASON_ZH),
      node("ord0250-1", `the reason never reached the ${layer} layer`),
    ).toBe(true);
  }
  // ORD-0220 (3)'s lesson: a hint off-screen is a hint nobody sees.
  const onScreen = await hint(page).evaluate((el) => {
    const box = el.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return {
      inViewport: box.top >= 0 && box.bottom <= window.innerHeight,
      hitIsHintOrItsText: hit === el || el.contains(hit),
      hitTag: hit ? hit.tagName : "",
      insideSrOnly: Boolean(el.closest(".v8-sr-only")),
    };
  });
  expect(onScreen.inViewport, node("ord0250-1", `the reason sat outside the viewport: ${JSON.stringify(onScreen)}`)).toBe(true);
  expect(onScreen.hitIsHintOrItsText, node("ord0250-1", `the centre hit <${onScreen.hitTag}>`)).toBe(true);
  expect(onScreen.insideSrOnly, node("ord0250-1", "the reason is screen-reader-only, so nobody sees why")).toBe(false);

  expect(counter.posts(), node("ord0250-1", "a refused press issued a request after all")).toBe(0);
  /* Looked-at evidence, not just asserted evidence: the run id is in the name because a second pass
     must not overwrite the picture this reading belongs to. */
  await page.screenshot({
    path: evidencePath(`ui-overnight/ord0250-hint-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
  });
  expect(
    (await input(page).inputValue()) === "",
    node("ord0250-1", "the field was filled in for the traveller to make this assertion pass"),
  ).toBe(true);

  writeFileSync(
    evidencePath("ord0250-refused-press-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0250-1",
        placeholderShown: placeholder,
        fieldValueAfterPress: await input(page).inputValue(),
        reasonPainted: painted,
        inViewport: onScreen.inViewport,
        centreHitTag: onScreen.hitTag,
        posts: counter.posts(),
        rule: "拒绝照旧（没有需求就没得排），但必须留下一句人话，且不替用户填任何东西",
      },
      null,
      1,
    ),
  );
});

test("ord0250-2-the-floor-is-a-real-boundary-not-a-vibe — 边界就是代码里那一格", async ({ page }) => {
  test.setTimeout(300_000);
  const counter = await openEmptySheet(page);

  await input(page).fill(SEVEN);
  expect(
    SEVEN.trim().length,
    node("ord0250-2", "the fixture itself no longer sits under the floor, so this node measures nothing"),
  ).toBe(MIN_LENGTH - 1);
  // Forced because under the floor the control is aria-disabled (see node 1's note).
  await page.getByTestId("plan-ai-submit").click({ force: true });
  // A poll for 0 would answer on its first sample, so this waits a real window before judging.
  await page.waitForTimeout(1_500);
  expect(counter.posts(), node("ord0250-2", "a below-floor request was sent")).toBe(0);
  /* Deliberately not asserted: whether a reason was painted belongs to node 1, and repeating the
     claim here would give that node's mutant two nodes to redden.  Recorded so the reading exists. */
  const reasonPainted = await hint(page).isVisible().catch(() => false);

  await input(page).fill(EIGHT);
  expect(EIGHT.trim().length, node("ord0250-2", "fixture moved off the floor")).toBe(MIN_LENGTH);
  await page.getByTestId("plan-ai-submit").click();
  await expect
    .poll(() => counter.posts(), {
      timeout: 60_000,
      message: node("ord0250-2", "exactly the floor's worth of words was still refused -- the guard is off by one"),
    })
    .toBe(1);
  await expect(hint(page), node("ord0250-2", "the nag stayed up once the requirement was met")).toHaveCount(0);

  writeFileSync(
    evidencePath("ord0250-length-boundary.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0250-2",
        underFloor: { text: SEVEN, length: SEVEN.trim().length },
        reasonPaintedAtSeven: reasonPainted,
        atFloor: { text: EIGHT, length: EIGHT.trim().length },
        posts: counter.posts(),
      },
      null,
      1,
    ),
  );
});

test("ord0250-3-the-control-says-it-cannot-be-advanced-and-is-still-reachable — 不能走也要说得清", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await openEmptySheet(page);
  const button = page.getByTestId("plan-ai-submit");

  expect(
    await button.getAttribute("aria-disabled"),
    node("ord0250-3", "an unusable press was presented as an ordinary button"),
  ).toBe("true");
  // ORD-0066 (甲): a hard `disabled` removes the control from the keyboard path; reach it instead.
  await button.focus();
  const focused = await button.evaluate((el) => document.activeElement === el);
  expect(focused, node("ord0250-3", "the control could not be focused, so a keyboard traveller skips it")).toBe(
    true,
  );
  const washed = await button.evaluate((el) => Number(getComputedStyle(el).opacity));
  expect(
    washed < 0.6,
    node("ord0250-3", `opacity ${washed} -- it does not look like a control that cannot advance`),
  ).toBe(true);

  await input(page).fill(EIGHT);
  expect(
    (await button.getAttribute("aria-disabled")) ?? "",
    node("ord0250-3", "it still claimed it could not advance once the request was long enough"),
  ).toBe("");
});
