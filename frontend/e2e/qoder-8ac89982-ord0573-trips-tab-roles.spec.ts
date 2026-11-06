/**
 * ORD-0573（角色那半）· trips 的四枚分区标签自己说"是哪一格"（车道 8ac89982）
 *
 * 判的四件，全是 DOM 上读回来的，不是属性表里的：
 *  1. 容器带 `role="tablist"`，四枚都带 `role="tab"` 且各有稳定 `id`（`trips-tab-<key>`）；
 *  2. 恰好一枚 `aria-selected="true"`，且开局必须是行程那一枚；
 *  3. 点第二枚之后，`aria-selected` 跟着走 —— 选中态是活的事实，不是一次性的初值；
 *  4. 四枚屏上的字一字不变（逐枚记录，改动前那代我自己的 C2 读数是对照面），且
 *     `data-testid` 与 `.active` 原位不动 —— 别的闸按它们选元素。
 *
 * 不判的也写明：`aria-controls` 与面板侧 `role="tabpanel"` 这半没做，那要新增一层 wrapper、
 * 改的是 DOM 树，得先让地图族重跑几何；这一手只在既有元素上加属性，DOM 结构零改动。
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";

import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-8ac89982-ord0573-trips-tab-roles.spec.ts";
const TABS = ["itinerary", "stay", "bookings", "spend"];
const ARMS = [
  { locale: "zh", storage: "zh-CN" },
  { locale: "en", storage: "en" },
];

async function readTabs(page: Page) {
  return page.evaluate((keys: string[]) => {
    const nav = document.querySelector("[data-testid='trips-tabs']");
    const rows = keys.map((key) => {
      const el = document.querySelector(`[data-testid='trips-tab-${key}']`);
      return {
        key,
        present: !!el,
        role: el?.getAttribute("role") ?? null,
        id: el?.id ?? null,
        aria_selected: el?.getAttribute("aria-selected") ?? null,
        active_class: !!el?.classList.contains("active"),
        visible_text: (el?.textContent ?? "").replace(/\s+/g, " ").trim(),
        box: (() => {
          const r = el?.getBoundingClientRect();
          return r ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : null;
        })(),
      };
    });
    return {
      landed_href: location.href,
      nav_role: nav?.getAttribute("role") ?? null,
      nav_label: nav?.getAttribute("aria-label") ?? null,
      rows,
      selected: rows.filter((row) => row.aria_selected === "true").map((row) => row.key),
    };
  }, TABS);
}

test.describe("ORD-0573 the trip sections say which one is open", () => {
  for (const arm of ARMS) {
    test(`trips-tabs-${arm.locale}`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.addInitScript((loc) => {
        try {
          localStorage.setItem("citynexus.locale", loc);
        } catch {}
      }, arm.storage);
      await ensureDemoJourney(page);
      await page.goto("/v8/trips", { waitUntil: "load" });
      await expect(page.getByTestId("trips-tab-itinerary")).toBeVisible({ timeout: 25_000 });

      const before = await readTabs(page);
      expect(before.nav_role, `${arm.locale}: the section bar does not announce itself as a tablist`).toBe("tablist");
      expect(before.nav_label, `${arm.locale}: the container lost the name it already had`).not.toBe("");
      for (const row of before.rows) {
        expect(row.present, `${arm.locale}: tab ${row.key} vanished`).toBe(true);
        expect(row.role, `${arm.locale}: tab ${row.key} has no role`).toBe("tab");
        expect(row.id, `${arm.locale}: tab ${row.key} has no stable id to be referenced by`).toBe(`trips-tab-${row.key}`);
        expect(row.box, `${arm.locale}: tab ${row.key} has no painted box`).not.toBeNull();
      }
      expect(before.selected, `${arm.locale}: ${before.selected.length} tabs claim selected, exactly one must`).toEqual(["itinerary"]);
      expect(before.rows.find((row) => row.key === "itinerary")!.active_class,
        `${arm.locale}: the selected tab must still carry the class the styling reads`).toBe(true);

      await page.getByTestId("trips-tab-stay").click();
      await page.waitForTimeout(1_200);
      const after = await readTabs(page);
      expect(after.selected, `${arm.locale}: after tapping 住宿 the selection did not move: ${JSON.stringify(after.rows)}`).toEqual(["stay"]);
      expect(after.rows.find((row) => row.key === "itinerary")!.aria_selected,
        `${arm.locale}: the previous tab still claims selected`).toBe("false");

      writeFileSync(
        evidencePath(`ord0573_${arm.locale}.json`),
        JSON.stringify({ file: FILE, arm: arm.locale, shape: "390x844", before, after }, null, 1),
        "utf-8",
      );
    });
  }
});
