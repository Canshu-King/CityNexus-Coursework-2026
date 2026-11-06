/**
 * ORD-0572 · 底栏选中态（车道 8ac89982）
 *
 * 派单要件逐枚判：
 *  1. 命中当前路由的那一枚底栏按钮带 `aria-current="page"`，其余三枚必须**仍然没有**这个属性；
 *  2. 屏上四枚可见文字一字不变 —— 正对照取自改动前那一代我自己那份 C2 读数
 *     （`evidence/c2_hubtrips_r1/census_hub_zh.json` 等，取的是现尖字节、同一形状 390x844），
 *     所以"没改文案"是比出来的，不是我念一句；
 *  3. `data-contrast-role="state"` 与 `.active` 仍在原位（暗色对比闸与样式读它们，这手只加一个属性）。
 *
 * `/v8/live` 那一格特别判"map 那枚才算当前"：这是面板自己已有的等价关系，不能被我加属性时弄丢。
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";

import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-8ac89982-ord0572-nav-current.spec.ts";
const NAV = ["hub", "trips", "map", "me"];
const SCREENS = ["hub", "trips", "map", "live", "me"];
const ARMS = [
  { locale: "zh", storage: "zh-CN" },
  { locale: "en", storage: "en" },
];

async function readNav(page: Page) {
  return page.evaluate((keys: string[]) => {
    const rows = keys.map((key) => {
      const el = document.querySelector(`[data-testid='nav-${key}']`);
      const span = el?.querySelector("span");
      return {
        key,
        present: !!el,
        aria_current: el?.getAttribute("aria-current") ?? null,
        contrast_role: el?.getAttribute("data-contrast-role") ?? null,
        active_class: !!el?.classList.contains("active"),
        visible_text: (span?.textContent ?? "").replace(/\s+/g, " ").trim(),
      };
    });
    return {
      landed_href: location.href,
      rows,
      current_count: rows.filter((row) => row.aria_current === "page").length,
    };
  }, NAV);
}

test.describe("ORD-0572 the bottom bar says which screen you are on", () => {
  for (const arm of ARMS) {
    for (const screen of SCREENS) {
      test(`nav-current-${screen}-${arm.locale}`, async ({ page }) => {
        await page.setViewportSize({ width: 390, height: 844 });
        await page.addInitScript((loc) => {
          try {
            localStorage.setItem("citynexus.locale", loc);
          } catch {}
        }, arm.storage);
        await ensureDemoJourney(page);
        await page.goto(`/v8/${screen}`, { waitUntil: "load" });
        await expect(page.getByTestId("nav-hub")).toBeVisible({ timeout: 25_000 });
        const reading = await readNav(page);
        writeFileSync(
          evidencePath(`ord0572_${screen}_${arm.locale}.json`),
          JSON.stringify({ file: FILE, screen, arm: arm.locale, ...reading }, null, 1),
          "utf-8",
        );

        expect(reading.rows.every((row) => row.present), `${screen}/${arm.locale}: a nav key vanished: ${JSON.stringify(reading.rows)}`).toBe(true);
        expect(reading.current_count, `${screen}/${arm.locale}: ${reading.current_count} buttons claim aria-current, exactly one must`).toBe(1);
        const expected = screen === "live" ? "map" : screen;
        expect(reading.rows.find((row) => row.aria_current === "page")!.key,
          `${screen}/${arm.locale}: the one that claims current is not the screen you are on`).toBe(expected);
        for (const row of reading.rows.filter((one) => one.key !== expected)) {
          expect(row.aria_current, `${screen}/${arm.locale}: ${row.key} must stay without aria-current`).toBeNull();
          expect(row.active_class, `${screen}/${arm.locale}: ${row.key} must stay inactive`).toBe(false);
        }
        const on = reading.rows.find((row) => row.key === expected)!;
        expect(on.contrast_role, `${screen}/${arm.locale}: the contrast role the dark gate reads is gone`).toBe("state");
        expect(on.active_class, `${screen}/${arm.locale}: the active class must still be on the same button`).toBe(true);
      });
    }
  }
});
