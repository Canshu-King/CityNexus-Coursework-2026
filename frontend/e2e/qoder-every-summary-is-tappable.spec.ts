/**
 * ORD-0247, widened by the commander @12:3x: two surfaces reported the same defect (311x14 on the
 * bookings 「高级：结构化数据」 and 316x25 on the AI sheet's 「技术详情」), so the acceptance is "the class of
 * controls", not "the control somebody happened to tap".
 *
 * So this file measures **every rendered `<summary>`** it can reach instead of naming one selector.
 * Two guards keep that from becoming a vacuous sweep:
 *   - the inventory must actually contain the reported controls (a sweep over nothing would pass even
 *     with the rule deleted);
 *   - the desktop read checks the *applied* `min-height`, so the touch floor cannot leak onto the
 *     pointer layout by hiding behind something that happens to be tall today.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-every-summary-is-tappable.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const MOBILE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 900 };
const FLOOR = 44;
const REPORTED = ["高级：结构化数据", "技术详情"];

type Entry = {
  surface: string;
  text: string;
  width: number;
  height: number;
  minHeight: string;
  inViewport: boolean;
};

/** Every <summary> the surface currently paints, with the box and the applied floor. */
async function inventory(page: Page, surface: string): Promise<Entry[]> {
  return await page.evaluate((name) => {
    const out: unknown[] = [];
    for (const el of Array.from(document.querySelectorAll("summary"))) {
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      out.push({
        surface: name,
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 40),
        width: Math.round(box.width),
        height: Math.round(box.height),
        minHeight: style.minHeight,
        inViewport: box.top >= 0 && box.bottom <= window.innerHeight && box.height > 0,
      });
    }
    return out;
  }, surface);
}

async function seedJourney(page: Page) {
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
}

/**
 * The surfaces that actually carry disclosures.  The AI sheet is reached by a real submitted request,
 * because 「技术详情」 exists only inside an answered result -- sweeping a sheet that never answered
 * would be a sweep over nothing.
 */
async function collectAll(page: Page): Promise<Entry[]> {
  const rows: Entry[] = [];
  for (const path of ["/v8/hub", "/v8/bookings", "/v8/me", "/v8/trips"]) {
    await page.goto(path, { waitUntil: "load" });
    const marker = path === "/v8/hub" ? "today-hero" : null;
    if (marker) await expect(page.getByTestId(marker)).toBeVisible({ timeout: 30_000 });
    else await page.waitForTimeout(1_200);
    rows.push(...(await inventory(page, path)));
  }
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景，别排太满");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result"), node("sweep", "the sheet never answered")).toBeVisible({
    timeout: 180_000,
  });
  rows.push(...(await inventory(page, "plan-ai-sheet")));
  return rows;
}

test("ord0247w-1-every-disclosure-clears-the-touch-floor — 整类 summary 都够 44px", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize(MOBILE);
  await seedJourney(page);
  const rows = await collectAll(page);

  const under = rows.filter((row) => row.height < FLOOR);
  expect(
    under.length,
    node(
      "ord0247w-1",
      `${under.length} disclosure(s) under ${FLOOR}px: ${under
        .map((r) => `${r.surface}:${r.text}=${r.width}x${r.height}`)
        .join(" | ")}`,
    ),
  ).toBe(0);

  writeFileSync(
    evidencePath("ord0247w-mobile-summary-inventory.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0247w-1",
        viewport: MOBILE,
        floor: FLOOR,
        counted: rows.length,
        surfaces: [...new Set(rows.map((r) => r.surface))],
        rows,
        underFloor: under,
      },
      null,
      1,
    ),
  );
});

test("ord0247w-2-the-sweep-really-reached-the-reported-controls — 扫到的是那两处", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize(MOBILE);
  await seedJourney(page);
  const rows = await collectAll(page);

  /* The positive control the whole class-wide claim depends on: if the reported disclosures are not in
     the inventory, node 1 is measuring nothing and a deleted rule would still read green. */
  for (const label of REPORTED) {
    expect(
      rows.some((row) => row.text.includes(label)),
      node(
        "ord0247w-2",
        `"${label}" was not in the sweep at all (inventoried: ${rows.map((r) => r.text).join(" / ") || "nothing"})`,
      ),
    ).toBe(true);
  }
  expect(
    rows.length,
    node("ord0247w-2", `the sweep found only ${rows.length} disclosures across the surfaces`),
  ).toBeGreaterThanOrEqual(REPORTED.length);
});

test("ord0247w-3-the-touch-floor-stays-out-of-the-pointer-layout — 桌面不被撑大", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize(DESKTOP);
  await seedJourney(page);
  const rows = await collectAll(page);

  const carried = rows.filter((row) => row.minHeight === `${FLOOR}px`);
  expect(
    carried.length,
    node(
      "ord0247w-3",
      `${carried.length} disclosure(s) still apply min-height:${FLOOR}px at ${DESKTOP.width}x${DESKTOP.height} (Amendment F-2 keeps desktop compact): ${carried
        .map((r) => `${r.surface}:${r.text}`)
        .join(" | ")}`,
    ),
  ).toBe(0);

  writeFileSync(
    evidencePath("ord0247w-desktop-summary-inventory.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0247w-3",
        viewport: DESKTOP,
        counted: rows.length,
        minHeights: [...new Set(rows.map((r) => r.minHeight))],
        rows,
      },
      null,
      1,
    ),
  );
});
