/**
 * ORD-0247 -- `HUMAN_ACCESSIBILITY`: the "高级：结构化数据" disclosure on the bookings screen is the only
 * entry point to structured import, and it measured 311x14 px while every control beside it takes the
 * >=44px mobile floor.  The floor enumerated controls and never covered `<summary>`.
 *
 * Three claims, each with its own killer (see review/ord0247_kill_arms.py):
 *   1. the box is at least as tall as the product's own touch token          -- rule deleted
 *   2. a tap at the box centre hits THAT element and opens it                -- pointer-events taken away
 *   3. the 44px floor stays mobile-only; desktop keeps compact density       -- rule moved out of the media query
 *
 * Claim 2 is the one the order actually names ("elementFromPoint 命中的就是该控件本身，不是它的父容器"):
 * a tall parent would satisfy a naive geometry check while the finger still lands on the wrong node.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-advanced-summary-is-tappable.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const MOBILE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };
/** Amendment F-2: the 44px floor is a touch rule; desktop keeps the compact density. */
const DESKTOP_MAX_HEIGHT = 30;
const ADVANCED = '[data-testid="bookings-advanced"]';
type Reading = Record<string, unknown>;
const readings: Reading[] = [];

test.use({ viewport: MOBILE, hasTouch: true });

async function openBookings(page: Page): Promise<Locator> {
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
  await page.goto("/v8/bookings", { waitUntil: "load" });
  await expect(page.getByTestId("v8-bookings")).toBeVisible({ timeout: 30_000 });
  const summary = page.locator(`${ADVANCED} > summary`);
  await expect(summary, node("seed", "the 高级：结构化数据 disclosure never rendered")).toBeVisible({
    timeout: 15_000,
  });
  return summary;
}

/** Box, the element the browser itself says is at the centre, and the rendered words. */
async function probe(page: Page, summary: Locator) {
  return await summary.evaluate((el) => {
    const box = el.getBoundingClientRect();
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    const details = el.closest("details");
    return {
      width: Math.round(box.width),
      height: Math.round(box.height),
      centre: [Math.round(cx), Math.round(cy)],
      hitTag: hit ? hit.tagName : "",
      hitIsSummaryItself: hit === el,
      hitTestId: hit ? hit.getAttribute("data-testid") ?? "" : "",
      hitClass: hit ? String(hit.className) : "",
      text: (el.textContent ?? "").trim(),
      insideSrOnly: Boolean(el.closest(".v8-sr-only")),
      display: getComputedStyle(el).display,
      markerListStyle: getComputedStyle(el).listStyleType,
      open: details ? details.hasAttribute("open") : null,
      touchFloor: getComputedStyle(document.documentElement).getPropertyValue("--cn-touch-min").trim(),
    };
  });
}

test("ord0247-1-the-disclosure-box-is-at-least-the-products-touch-floor — 命中框够 44px", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const summary = await openBookings(page);
  const measured = await probe(page, summary);

  // The number comes from the product's own token, so the gate cannot drift from the design system.
  expect(measured.touchFloor, node("ord0247-1", "--cn-touch-min is not the frozen 44px")).toBe("44px");
  expect(
    measured.height,
    node(
      "ord0247-1",
      `"${measured.text}" measures ${measured.width}x${measured.height} at ${MOBILE.width}x${MOBILE.height}; the mobile floor is ${measured.touchFloor}`,
    ),
  ).toBeGreaterThanOrEqual(44);

  readings.push({ node: "ord0247-1", viewport: MOBILE, ...measured });
});

test("ord0247-2-a-tap-at-the-centre-belongs-to-that-summary-and-opens-it — 命中的就是它自己", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const summary = await openBookings(page);
  const before = await probe(page, summary);

  expect(
    before.hitIsSummaryItself,
    node(
      "ord0247-2",
      `elementFromPoint(${before.centre.join(",")}) returned <${before.hitTag}> class="${before.hitClass}" testid="${before.hitTestId}", not the summary itself — a tall ancestor would pass geometry while the finger lands elsewhere`,
    ),
  ).toBe(true);
  expect(before.open, node("ord0247-2", "the disclosure started open, so the tap proves nothing")).toBe(
    false,
  );

  // A real touch event at the measured centre, not a click() on the locator.
  await page.touchscreen.tap(before.centre[0], before.centre[1]);
  const after = await probe(page, summary);
  expect(
    after.open,
    node(
      "ord0247-2",
      `tapping the centre left it closed; the hit test answered <${after.hitTag}> class="${after.hitClass}"`,
    ),
  ).toBe(true);

  readings.push({ node: "ord0247-2", viewport: MOBILE, before, after });
});

test("ord0247-3-the-44px-floor-is-mobile-only-desktop-stays-compact — 桌面仍按紧凑密度", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await page.setViewportSize(DESKTOP);
  const summary = await openBookings(page);
  const measured = await probe(page, summary);

  expect(
    measured.height,
    node(
      "ord0247-3",
      `the disclosure measures ${measured.width}x${measured.height} at ${DESKTOP.width}x${DESKTOP.height} — the touch floor leaked onto the pointer layout (Amendment F-2 keeps desktop at <=${DESKTOP_MAX_HEIGHT}px)`,
    ),
  ).toBeLessThanOrEqual(DESKTOP_MAX_HEIGHT);
  expect(
    measured.hitIsSummaryItself,
    node("ord0247-3", `desktop centre hit returned <${measured.hitTag}> instead of the summary`),
  ).toBe(true);
  // The native marker is still there: growing the box with line-height must not have traded the
  // affordance for the hit target.
  expect(
    measured.display,
    node("ord0247-3", `display became "${measured.display}", which drops the disclosure marker`),
  ).toBe("list-item");

  readings.push({ node: "ord0247-3", viewport: DESKTOP, ...measured });
});

test.afterAll(async () => {
  writeFileSync(
    evidencePath("ord0247-advanced-summary-tap-target.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0247",
        at: new Date().toISOString(),
        target: "details.v8-advanced > summary (「高级：结构化数据」, bookings screen)",
        judgedBy:
          "elementFromPoint(盒中心) === 该 summary 本身 ＋ 真触摸屏点把它打开；桌面另一格守住 44px 只在移动层",
        mobileTouchFloor: "--cn-touch-min = 44px",
        desktopMaxHeight: DESKTOP_MAX_HEIGHT,
        readings,
      },
      null,
      1,
    ),
  );
});
