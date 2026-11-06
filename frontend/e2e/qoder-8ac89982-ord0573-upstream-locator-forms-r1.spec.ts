/**
 * ORD-0573 · 代价尺（车道 8ac89982）：`role="tab"` 会不会拿走别人正在用的 locator
 *
 * 这枚尺不改产品字节，只回答一件事：已在线的上游闸用
 * `getByRole("button", { name: "Bookings" })` 点这枚分区标签（实测两处，都是那句同一个
 * locator：`product-v8.spec.ts` 与 `real-world-trip-operations-v9.spec.ts` 各一处；两枚都强制 `en`，
 * 见各自 `addInitScript` 那行）。一个元素只认一个 role：挂了 `role="tab"`，button 形就
 * 找不到它了。这一手在真屏上把两种形制各数一遍，谁数到几、答的是哪一枚，写在读数里。
 *
 * 判据与产品字节无关，改前改后两代都该绿（所以它是"这手到底挪了什么"的证明，不是.pass 门）：
 *  1. 四枚分区词：`data-testid=trips-tab-<key>` 那一枚必须恰被两种形制之一答中（异或），
 *     不许两种都答中、也不许两种都落空；另一形数到的枚数照记，不判——同一屏上别的控件
 *     本来也可能带同一个词（中文臂"行程"就是这样，实测过，见下）。
 *  2. 一枚负对照词（屏上没有任何控件叫这个）：两形都必须数到 0。若它数到 1，说明计数器是
 *     坏的、整枚尺作废 —— 这就是它"该红的那一次"。
 *
 * 第一代的教训写在这里防再犯：先前把判据写成"两形之和恰为一"，中文臂 `行程` 实得 3 枚
 * （屏上另有带这个词的控件），那是尺自己的错判，不是产品的事。判据只能盯"那一枚元素被
 * 哪一种形答到"，不能盯整词命中数。
 */
import { expect, test, type Page, type Locator } from "@playwright/test";
import { writeFileSync } from "node:fs";

import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-8ac89982-ord0573-upstream-locator-forms-r1.spec.ts";
const WORDS = {
  zh: [
    { key: "itinerary", word: "行程" },
    { key: "stay", word: "住宿" },
    { key: "bookings", word: "预订" },
    { key: "spend", word: "花费" },
  ],
  en: [
    { key: "itinerary", word: "Itinerary" },
    { key: "stay", word: "Stay" },
    { key: "bookings", word: "Bookings" },
    { key: "spend", word: "Spend" },
  ],
};
const ARMS = [
  { locale: "zh", storage: "zh-CN" },
  { locale: "en", storage: "en" },
];
const CONTROL_WORD = "Bookings Qoder Probe 573";

async function testids(locator: Locator): Promise<(string | null)[]> {
  return locator.evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("data-testid")),
  );
}

async function countForms(page: Page, key: string, word: string) {
  const button = page.getByRole("button", { name: word });
  const tab = page.getByRole("tab", { name: word });
  const [buttonIds, tabIds, own] = await Promise.all([
    testids(button),
    testids(tab),
    page.getByTestId(`trips-tab-${key}`).getAttribute("role"),
  ]);
  const answers_button = buttonIds.includes(`trips-tab-${key}`);
  const answers_tab = tabIds.includes(`trips-tab-${key}`);
  return {
    word,
    own_role: own,
    button_count: buttonIds.length,
    tab_count: tabIds.length,
    tab_answered_by_button_form: answers_button,
    tab_answered_by_tab_form: answers_tab,
    form_that_answers:
      answers_button && !answers_tab
        ? "button"
        : answers_tab && !answers_button
          ? "tab"
          : answers_tab && answers_button
            ? "both"
            : "none",
    other_testids_in_button_form: buttonIds.filter(
      (id) => id !== `trips-tab-${key}`,
    ),
  };
}

test.describe("ORD-0573 the cost of role=tab on shipped locators", () => {
  for (const arm of ARMS) {
    test(`locator-forms-${arm.locale}`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.addInitScript((loc) => {
        try {
          localStorage.setItem("citynexus.locale", loc);
        } catch {}
      }, arm.storage);
      await ensureDemoJourney(page);
      await page.goto("/v8/trips", { waitUntil: "load" });
      await expect(page.getByTestId("trips-tab-itinerary")).toBeVisible({ timeout: 25_000 });

      const rows: Record<string, unknown>[] = [];
      for (const item of WORDS[arm.locale]) {
        const row = { key: item.key, ...(await countForms(page, item.key, item.word)) };
        rows.push(row);
        expect(
          ["button", "tab"].includes(row.form_that_answers as string),
          `${arm.locale}: ${item.word} answered by ${row.form_that_answers} -- the section tab must be reachable by exactly one of the two forms (button saw ${row.button_count}, tab saw ${row.tab_count})`
        ).toBe(true);
      }

      const control = await countForms(page, "itinerary", CONTROL_WORD);
      rows.push({ key: "negative_control", control_word: CONTROL_WORD, ...control });
      expect(control.button_count, `${arm.locale}: the button counter is not blind (control read ${control.button_count})`).toBe(0);
      expect(control.tab_count, `${arm.locale}: the tab counter is not blind (control read ${control.tab_count})`).toBe(0);

      const nav_role = await page.getByTestId("trips-tabs").getAttribute("role");
      const payload = {
        file: FILE,
        arm: arm.locale,
        landed_href: page.url(),
        nav_role,
        roles_generation: nav_role === "tablist",
        form_that_answers: rows.filter((row) => row.key !== "negative_control").map((row) => row.form_that_answers),
        rows,
      };
      writeFileSync(
        evidencePath(`ord0573cost_${arm.locale}.json`),
        JSON.stringify(payload, null, 1),
        "utf-8"
      );
    });
  }
});
