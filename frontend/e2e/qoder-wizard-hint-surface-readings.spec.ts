/**
 * ORD-0221 ③④ -- the runtime readings the arbitration asked for, surface by surface.
 *
 * Two standing bans shape this file:
 *  - **`innerText` of the body may not be used to decide what is on screen.**  `.v8-sr-only` is
 *    clipped to 1px, not `display:none`, so a body-text check counts a screen-reader sentence as
 *    visible -- that is exactly how HJ-P1's 15:04 reading went wrong and why he retracted the
 *    visible half of it at 18:07.  Every reading here names the element it came from and carries its
 *    own geometry, so "visible" is computed, not assumed.
 *  - **a commit message is not a reading.**  Each item is a before→after pair taken from the running
 *    screen, written out verbatim to the evidence file.
 *
 * ③ step 2, in front of the traveller: budget 0 blocks the step and the visible hint names 总预算;
 *   the value is corrected on the same screen; the same element's text is read again without any
 *   navigation in between.
 * ④ step 3, the same field: the visible hint and the live regions are read separately, so the
 *   receipt shows what a sighted traveller sees and what a screen-reader user is told.  The live
 *   region's staleness is *recorded* here and fixed under ORD-0220 ② -- this node does not assert it
 *   green, and says so in the file it writes.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-wizard-hint-surface-readings.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

type Surface = {
  selector: string;
  exists: boolean;
  text: string;
  rect: { top: number; height: number; width: number } | null;
  /** Computed from the element's own box: a 1px-clipped live region is never counted as visible. */
  visuallyRendered: boolean;
  insideSrOnly: boolean;
  inViewport: boolean;
  computed?: { visibility: string; display: string; opacity: string };
};

/** One element, read as rendered: its text plus the geometry that decides "on screen or not". */
async function surface(page: Page, selector: string): Promise<Surface> {
  return await page.evaluate((target) => {
    const el = document.querySelector(target);
    if (!el) {
      return {
        selector: target,
        exists: false,
        text: "",
        rect: null,
        visuallyRendered: false,
        insideSrOnly: false,
        inViewport: false,
      };
    }
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const insideSrOnly = !!el.closest(".v8-sr-only");
    return {
      selector: target,
      exists: true,
      text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
      rect: { top: Math.round(box.top), height: Math.round(box.height), width: Math.round(box.width) },
      visuallyRendered:
        !insideSrOnly &&
        box.width > 1 &&
        box.height > 1 &&
        style.visibility !== "hidden" &&
        style.display !== "none" &&
        Number(style.opacity) > 0,
      insideSrOnly,
      inViewport: box.bottom > 0 && box.top < window.innerHeight,
      computed: { visibility: style.visibility, display: style.display, opacity: style.opacity },
    };
  }, selector);
}

/** Every announcement surface on the screen, each with its own visibility verdict. */
async function liveRegions(page: Page): Promise<Surface[]> {
  return await page.evaluate(() =>
    Array.from(document.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).map((el) => {
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const insideSrOnly = !!el.closest(".v8-sr-only");
      return {
        selector: el.id ? `#${el.id}` : `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).trim().split(/\s+/).join(".")}` : ""}`,
        exists: true,
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
        rect: { top: Math.round(box.top), height: Math.round(box.height), width: Math.round(box.width) },
        visuallyRendered:
          !insideSrOnly &&
          box.width > 1 &&
          box.height > 1 &&
          style.visibility !== "hidden" &&
          style.display !== "none" &&
          Number(style.opacity) > 0,
        insideSrOnly,
        inViewport: box.bottom > 0 && box.top < window.innerHeight,
      };
    }),
  );
}

/**
 * The hint rises with a fade, so a read taken at first paint catches its own `opacity: 0` --
 * measured on this tip: opacity 0 right after the block, 1 with the same text 1.5s later
 * (`review/run-run074395.log`), and a settled-text read once landed mid-fade at 0.282.  "Before"
 * has to mean the screen the traveller actually looks at, so the surface is waited for until it is
 * rendered **and** fully opaque; the bound and the last reading stay in the message, so a hint that
 * never becomes visible goes red instead of being waited away.
 */
async function settledSurface(page: Page, selector: string, owner: string): Promise<Surface> {
  let last: Surface | null = null;
  await expect
    .poll(
      async () => {
        last = await surface(page, selector);
        return last.visuallyRendered && last.computed?.opacity === "1";
      },
      {
        message: node(owner, `the surface never became a settled, visible thing to read from: ${JSON.stringify(last)}`),
        timeout: 10_000,
      },
    )
    .toBe(true);
  return last as Surface;
}

async function openWizard(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
}

async function reachStep2WithABlockedBudget(page: Page): Promise<void> {
  await openWizard(page);
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("北京南站");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step2-body")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("trial-budget").fill("0");
}

test("ord0221-3-step2-the-hint-clears-in-front-of-the-traveller — 第 2 步改完当场消失的可见文本", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0221-3-step2-the-hint-clears-in-front-of-the-traveller";
  await reachStep2WithABlockedBudget(page);

  const hint = page.getByTestId("trial-required-hint");
  await expect(
    hint,
    node(NODE, "an empty budget did not leave a hint element to read"),
  ).toHaveCount(1);
  const before = await settledSurface(page, '[data-testid="trial-required-hint"]', NODE);
  expect(
    before.insideSrOnly,
    node(NODE, `the surface being read is a screen-reader region, not the screen: ${JSON.stringify(before)}`),
  ).toBe(false);
  expect(
    before.text.includes("总预算"),
    node(NODE, `with 总预算 at 0 the visible hint does not name it: "${before.text}"`),
  ).toBe(true);
  // The step really is blocked at this moment -- otherwise "it disappeared" would be trivially true.
  expect(
    await page.getByTestId("trial-next").getAttribute("aria-disabled"),
    node(NODE, "the hint names a missing field while 下一步 is not blocked"),
  ).toBe("true");

  /* Correct the value on the same screen.  No navigation, no reload: this is the "当场" the order
     asks about. */
  await page.getByTestId("trial-budget").fill("1500");
  const after = await surface(page, "[data-testid=\"trial-required-hint\"]");
  expect(
    after.text.includes("总预算"),
    node(NODE, `the corrected budget is still named in the visible hint: before "${before.text}" / after "${after.text}"`),
  ).toBe(false);

  const live = await liveRegions(page);
  writeFileSync(
    evidencePath("ui-overnight/ord0221-step2-visible-before-after.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0221 ③",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        method: "per-element textContent + the element's own box; body innerText is never used",
        before,
        after,
        nextAriaDisabledAtBefore: "true",
        liveRegionsAfter: live,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0221-step2-visible-before-after.png"), fullPage: true });
});

test("ord0221-4-step3-visible-hint-and-live-region-read-apart — 第 3 步：看见的与听到的分开量", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0221-4-step3-visible-hint-and-live-region-read-apart";
  await reachStep2WithABlockedBudget(page);
  const atStep2 = await settledSurface(page, '[data-testid="trial-required-hint"]', NODE);
  /* HJ-P1's route, not a hypothetical one: press the blocked 下一步.  `aria-disabled` keeps
     Playwright's ordinary click away (this lane's own trap), so the press is forced -- which is what
     the traveller's thumb does, and the refusal writes the announcement region. */
  await page.getByTestId("trial-next").click({ force: true });
  await expect(
    page.locator("#trial-next-reason"),
    node(NODE, "the refused press wrote no announcement at all, so there is no live-region reading to take"),
  ).not.toHaveText("", { timeout: 5_000 });
  const spokenAtRefusal = await surface(page, "#trial-next-reason");
  await page.getByTestId("trial-budget").fill("1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });

  const atStep3 = await surface(page, "[data-testid=\"trial-required-hint\"]");
  expect(
    atStep3.text.includes("总预算"),
    node(NODE, `the corrected budget still hangs in the visible hint on step 3: "${atStep3.text}" (step 2 said "${atStep2.text}")`),
  ).toBe(false);
  const budget = ((await page.getByTestId("review-budget").innerText()) ?? "").replace(/\s+/g, " ");
  expect(
    budget.includes("1500"),
    node(NODE, `the review step does not show the corrected value: "${budget}"`),
  ).toBe(true);

  /* What a screen-reader user is told, read as its own surface.  Recorded, not asserted green: the
     live region keeping its stale sentence is ORD-0220 ②, and pretending otherwise here would be
     the "innerText counts sr-only as visible" error with the labels swapped. */
  const live = await liveRegions(page);
  const staleLive = live.filter(
    (region) => !region.visuallyRendered && region.text.includes("总预算"),
  );
  writeFileSync(
    evidencePath("ui-overnight/ord0221-step3-visible-before-after.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0221 ④",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        method: "per-element textContent + the element's own box; body innerText is never used",
        visibleHint: { step2_before: atStep2, step3_after: atStep3 },
        spokenAtRefusal,
        reviewBudget: budget,
        allAnnouncementSurfaces: live,
        nonVisibleSurfacesStillNamingTheField: staleLive,
        note: "ORD-0220 ② owns the live-region staleness recorded here; this node only refuses to call it visible",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0221-step3-visible-before-after.png"), fullPage: true });
});

test("ord0220-2-the-announcement-retires-when-the-field-is-fixed — 听的那句也要跟现值重算", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0220-2-the-announcement-retires-when-the-field-is-fixed";
  await reachStep2WithABlockedBudget(page);
  /* Press the blocked 下一步 the way a thumb does.  Without this the region is never written and
     the node would pass on an unfired stub -- this lane's own recorded trap. */
  await page.getByTestId("trial-next").click({ force: true });
  await expect
    .poll(() => surface(page, "#trial-next-reason").then((read) => read.text), {
      message: node(NODE, "the refused press wrote no announcement, so there is nothing to retire"),
      timeout: 5_000,
    })
    .toContain("总预算");
  const spoken = await surface(page, "#trial-next-reason");
  expect(
    spoken.insideSrOnly && !spoken.visuallyRendered,
    node(NODE, `the surface being retired is not the screen-reader one: ${JSON.stringify(spoken)}`),
  ).toBe(true);

  /* Fix the field.  The visible hint has always recomputed; the announcement must retire with it,
     or a screen-reader user is told the trip still needs a number that is already on the screen. */
  await page.getByTestId("trial-budget").fill("1500");
  await expect
    .poll(() => surface(page, "#trial-next-reason").then((read) => read.text), {
      message: node(
        NODE,
        `the announcement still names 总预算 after it was fixed: it was "${spoken.text}" and is still that`,
      ),
      timeout: 5_000,
    })
    .not.toContain("总预算");

  // And it stays retired on step 3, which is where HJ-P1's reading lived.
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });
  const atStep3 = await surface(page, "#trial-next-reason");
  expect(
    atStep3.text.includes("总预算"),
    node(NODE, `step 3 still announces a field the traveller already filled: "${atStep3.text}"`),
  ).toBe(false);
  writeFileSync(
    evidencePath("ui-overnight/ord0220-announcement-retired.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0220 ②",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        method: "the region's own textContent, read per surface; body innerText is never used",
        spokenAtRefusal: spoken,
        afterFix: await surface(page, "#trial-next-reason"),
        atStep3,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0220-3-a-refused-press-brings-its-reason-on-screen — 按下去什么都没发生，这一格不再成立", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0220-3-a-refused-press-brings-its-reason-on-screen";
  await reachStep2WithABlockedBudget(page);
  await settledSurface(page, '[data-testid="trial-required-hint"]', NODE);
  /* Reproduce the reported state: the traveller is at the top of the step, thumb on the sticky
     下一步, and the sentence is below the fold. */
  await page.evaluate(() => window.scrollTo(0, 0));
  const before = await page.evaluate(() => {
    const el = document.getElementById("trial-required-hint");
    const box = el?.getBoundingClientRect();
    return {
      top: box ? Math.round(box.top) : null,
      bottom: box ? Math.round(box.bottom) : null,
      innerHeight: window.innerHeight,
      inViewport: !!box && box.bottom > 0 && box.top < window.innerHeight,
    };
  });

  await page.getByTestId("trial-next").click({ force: true });
  /* The reveal is instant (`scrollIntoView({block:"center"})`), so this bound is a guard, not an
     animation timer: commander's 19:48 rule says a gate must not measure an animation, because
     `behavior:"smooth"` sampled scrollY=0 six times on this machine while the same element moved
     0 -> 650 instantly five times out of five. */
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const el = document.getElementById("trial-required-hint");
          const box = el?.getBoundingClientRect();
          return box ? Math.round(box.bottom) <= window.innerHeight : false;
        }),
      {
        message: node(
          NODE,
          `the press was refused but its reason never came on screen: before ${JSON.stringify(before)}`,
        ),
        timeout: 1_500,
      },
    )
    .toBe(true);
  const focused = await page.evaluate(() => document.activeElement?.id ?? null);
  /* Scroll, not focus: making the paragraph focusable would need `tabIndex={-1}`, and ORD-0066's
     gate carries a deliberate negative control that that element must not take focus.  So the
     position stays where it was, and the invariant is the visible one. */
  expect(
    focused,
    node(NODE, `the refused press moved focus onto the reason paragraph, which ORD-0066's instrument measures as not focusable: ${JSON.stringify(focused)}`),
  ).not.toBe("trial-required-hint");
  const after = await page.evaluate(() => {
    const el = document.getElementById("trial-required-hint");
    const box = el?.getBoundingClientRect();
    return {
      top: box ? Math.round(box.top) : null,
      bottom: box ? Math.round(box.bottom) : null,
      innerHeight: window.innerHeight,
      inViewport: !!box && box.bottom > 0 && box.top < window.innerHeight,
    };
  });
  writeFileSync(
    evidencePath("ui-overnight/ord0220-reason-on-screen.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0220 ③",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        before,
        after,
        focusedAfterPress: focused,
        rule: "拦下时提示必须滚进视口（bottom <= innerHeight）；焦点位置只记录不要求（见 ORD-0066 负控）",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0220-reason-on-screen.png"), fullPage: false });
});

test("ord0222-6-the-announcement-is-still-gone-after-the-trip-exists — 创建成功之后也不该再播那一句", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0222-6-the-announcement-is-still-gone-after-the-trip-exists";
  await reachStep2WithABlockedBudget(page);
  await page.getByTestId("trial-next").click({ force: true });
  await expect
    .poll(() => surface(page, "#trial-next-reason").then((read) => read.text), {
      message: node(NODE, "the refused press never wrote an announcement, so this arm would pass on nothing"),
      timeout: 5_000,
    })
    .toContain("总预算");
  await page.getByTestId("trial-budget").fill("1500");
  await page.getByTestId("trial-next").click();
  await expect(page.getByTestId("trial-step3-body")).toBeVisible({ timeout: 15_000 });
  /* Create for real.  The row must exist before the claim "nothing stale is announced after the
     trip exists" means anything. */
  await page.getByTestId("trial-create").click();
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 60_000 });
  const afterCreate = await surface(page, "#trial-next-reason");
  expect(
    afterCreate.text.includes("总预算"),
    node(NODE, `the trip exists and the announcement still names the fixed field: "${afterCreate.text}"`),
  ).toBe(false);
  writeFileSync(
    evidencePath("ui-overnight/ord0222-after-create.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0222 ②-follow-up (HJ-P1 19:46 wording)",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        journeyOnScreen: ((await page.getByTestId("today-heading").innerText()) ?? "").trim(),
        announcementAfterCreate: afterCreate,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
