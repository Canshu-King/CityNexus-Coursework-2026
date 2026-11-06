/**
 * ORD-0438 — the journey's progress is one contract, not a second counting system.
 *
 * Four arms. The discriminating one is M: on today's sealed data the server's day total and this
 * screen's own day grouping happen to be the same number (measured: both 3), so no screen reading can
 * tell "which one the headline used". M feeds the derivation inputs where those two disagree and asserts
 * the headline follows the server projection, which is the only reading that can fail.
 *
 *   M  module           -- day total from `temporal_shape.day_count` even when the grouping says
 *                          otherwise; a null there stays unknown instead of becoming 1; the terminal
 *                          rule is the one `lib/executionWindow.ts` owns, so a cancelled stop counts as
 *                          over but never as walked; the day position comes from the group holding the
 *                          cursor and says why when it cannot.
 *   S  shape-of-source  -- the module carries no status list and no date formatter of its own: those
 *                          would be the second system this order forbids.
 *   Z / E  screens      -- what a traveller actually reads, in both arms, reachable on a 390x844
 *                          screen above the fixed bar (hit-tested, not assumed).
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import {
  dayOfGapCopy,
  journeyProgress,
  progressContractCopy,
} from "../src/features/progress/journeyProgress";
import { TERMINAL_ACTIVITY_STATES } from "../src/lib/executionWindow";

const MODULE = "src/features/progress/journeyProgress.ts";

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0438", name + ".json");
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O438GATE " + name + " " + JSON.stringify(body));
}

test("ORD-0438 M the derivation follows the server, not a second count", () => {
  const groupedTwoServerSaysThree = journeyProgress({
    serverDayCount: 3,
    days: [
      { key: "2026-10-09", activityIds: ["a"] },
      { key: "2026-10-10", activityIds: ["b"] },
    ],
    activities: [{ id: "a", status: "NOT_STARTED" }, { id: "b", status: "NOT_STARTED" }],
    currentActivityId: "a",
  });
  const rederived = journeyProgress({
    serverDayCount: null,
    days: [{ key: "2026-10-09", activityIds: ["a"] }],
    activities: [{ id: "a", status: "NOT_STARTED" }],
    currentActivityId: "a",
  });
  const mixed = journeyProgress({
    serverDayCount: 2,
    days: [{ key: "2026-10-09", activityIds: ["a", "b"] }],
    activities: [
      { id: "a", status: "COMPLETED" },
      { id: "b", status: "CANCELLED" },
      { id: "c", status: "SKIPPED" },
      { id: "d", status: "NOT_STARTED" },
    ],
    currentActivityId: "ghost",
  });
  const empty = journeyProgress({ serverDayCount: null, days: [], activities: [], currentActivityId: null });

  write("M_derivation", {
    groupedTwoServerSaysThree: {
      progress: groupedTwoServerSaysThree,
      copy_zh: progressContractCopy(groupedTwoServerSaysThree, true),
    },
    serverHasNoShape: {
      progress: rederived,
      copy_en: progressContractCopy(rederived, false),
    },
    mixedStatesWithAnUnreadableCursor: {
      progress: mixed,
      copy_zh: progressContractCopy(mixed, true),
      day_gap_zh: dayOfGapCopy(mixed, true),
    },
    nothingPlanned: {
      progress: empty,
      copy_zh: progressContractCopy(empty, true),
      copy_en: progressContractCopy(empty, false),
    },
    terminal_rule_size: TERMINAL_ACTIVITY_STATES.size,
  });

  expect(groupedTwoServerSaysThree.day_total,
    "the headline must follow the server projection even when this screen's own grouping says otherwise")
    .toBe(3);
  expect(groupedTwoServerSaysThree.day_total_source).toBe("server_temporal_shape");
  expect(groupedTwoServerSaysThree.day_of, "the day position is the group holding the cursor").toBe(1);
  expect(progressContractCopy(groupedTwoServerSaysThree, true)).toContain("共 3 天");

  expect(rederived.day_total, "a server that gives no whole-day count must not become 1").toBeNull();
  expect(rederived.day_total_source).toBe("server_has_no_shape");
  expect(progressContractCopy(rederived, false)).toMatch(/stays blank/);

  expect(mixed.completed_count, "only a walked stop counts as completed").toBe(1);
  expect(mixed.settled_count, "skipped and cancelled are over, which is not the same as walked").toBe(3);
  expect(mixed.planned_total).toBe(4);
  expect(mixed.day_of).toBeNull();
  expect(mixed.day_of_basis, "a cursor naming no group is a named state, not a silent day 1").toBe("cursor_unreadable");
  expect(dayOfGapCopy(mixed, true)).toContain("留空");
  expect(progressContractCopy(mixed, true), "the copy must separate 已完成 from 跳过或取消").toContain("不算走完");

  expect(empty.planned_total).toBe(0);
  expect(progressContractCopy(empty, true), "an empty counter reads as a contract, never as 没有数据")
    .toMatch(/之后|才/);
  expect(progressContractCopy(empty, true)).not.toMatch(/没有数据|暂无/);
});

test("ORD-0438 S the module owns no second copy of the rules", () => {
  const src = readFileSync(MODULE, "utf8");
  const lines = src.split(/\r?\n/);
  const ownStatusList = lines.filter((l) => /\[\s*["']COMPLETED["']/.test(l));
  const ownDateGrouping = lines.filter((l) => /DateTimeFormat|timeZone/.test(l));
  write("S_source_shape", {
    imports_execution_window: /from "\.\.\/\.\.\/lib\/executionWindow"/.test(src),
    own_status_list_lines: ownStatusList,
    own_date_grouping_lines: ownDateGrouping,
    line_count: lines.length,
  });
  expect(src, "the terminal-state rule has exactly one owner in this repo")
    .toMatch(/from "\.\.\/\.\.\/lib\/executionWindow"/);
  expect(ownStatusList, "a local list of stop states would be the second progress system").toEqual([]);
  expect(ownDateGrouping, "day grouping stays with the screen that draws the day strip").toEqual([]);
  expect(src, "no percentage: the backend refuses to invent a denominator").not.toMatch(/percent\w*\s*[=:]/);
});

async function screenArm(page: Page, arm: "zh-CN" | "en") {
  await ensureDemoJourney(page);
  await page.goto("/v8/trips", { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-itinerary").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1500);

  const api = await page.evaluate(async () => {
    const id = localStorage.getItem("citynexus.v8.journey_id");
    if (!id) return { id, day_count: null, statuses: [] as string[], expected_day_of: null as number | null };
    const exp = await (await fetch(`/api/v1/journeys/${id}/experience`)).json();
    const acts = (exp?.active_plan?.activities ?? []) as Array<{ id: string; status: string; start_at: string }>;
    const tz = exp?.journey?.timezone || "UTC";
    const key = (iso: string) => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric",
      month: "2-digit", day: "2-digit" }).format(new Date(iso));
    const groups = Array.from(new Set(acts.map((a) => key(a.start_at)))).sort();
    const cursor = exp?.active_plan?.current_activity_id ?? null;
    const cursorAct = acts.find((a) => a.id === cursor) ?? null;
    return {
      id,
      day_count: exp?.journey?.temporal_shape?.day_count ?? null,
      statuses: acts.map((a) => a.status),
      expected_day_of: cursorAct ? groups.indexOf(key(cursorAct.start_at)) + 1 : null,
    };
  });
  const read = await page.evaluate(() => {
    const nav = document.querySelector<HTMLElement>(".v8-mobile-nav");
    const hitLine = nav ? Math.round(nav.getBoundingClientRect().top) : null;
    const grab = (testId: string) => {
      const el = document.querySelector<HTMLElement>(`[data-testid='${testId}']`);
      if (!el) return null;
      const b = el.getBoundingClientRect();
      const cx = Math.round(b.x + b.width / 2);
      const cy = Math.round(b.y + b.height / 2);
      const top = document.elementFromPoint(cx, cy);
      const range = document.createRange();
      range.selectNodeContents(el);
      const rects = Array.from(range.getClientRects()).map((r) => ({
        y: Math.round(r.y), bottom: Math.round(r.bottom),
      }));
      return {
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
        box: { y: Math.round(b.y), bottom: Math.round(b.bottom), h: Math.round(b.height) },
        rects,
        centre: { x: cx, y: cy },
        centreHitInside: !!(top && (top === el || el.contains(top))),
        centreAboveNav: hitLine === null ? null : cy < hitLine,
        lastLineBottom: rects.length ? Math.max(...rects.map((r) => r.bottom)) : null,
        lastLineClearsNav: hitLine === null || !rects.length ? null : Math.max(...rects.map((r) => r.bottom)) <= hitLine,
      };
    };
    return {
      lang: document.documentElement.lang || null,
      hitLine,
      day_total: grab("trip-day-total"),
      contract: grab("trip-progress-contract"),
      day_gap: grab("trip-progress-daygap"),
    };
  });
  const serverTotal = api.day_count as number | null;
  const headlineNumber = Number((read.day_total?.text ?? "").match(/\d+/)?.[0] ?? NaN);
  const completedFromApi = (api.statuses as string[]).filter((s) => s === "COMPLETED").length;
  const settledFromApi = (api.statuses as string[]).filter(
    (s) => ["COMPLETED", "SKIPPED", "CANCELLED"].includes(s)).length;
  const contractText = read.contract?.text ?? "";
  const hasCjk = /[一-鿿]/.test(contractText);
  const expectedDayOf = api.expected_day_of as number | null;
  const dayPhrase = expectedDayOf === null
    ? null
    : arm === "zh-CN" ? `第 ${expectedDayOf} 天` : `day ${expectedDayOf} of`;
  const out = {
    arm,
    api: { journey_id: api.id, server_day_count: serverTotal, activity_status_count: (api.statuses as string[]).length,
           completed_from_api: completedFromApi, settled_from_api: settledFromApi,
           expected_day_of: expectedDayOf },
    screen: read,
    day_phrase_expected: dayPhrase,
    day_phrase_on_screen: dayPhrase === null ? null : contractText.includes(dayPhrase),
    headline_follows_server_projection: serverTotal === null ? read.day_total?.text === (arm === "zh-CN" ? "天数未知" : "length unknown")
      : headlineNumber === serverTotal,
    contract_reachable: !!(read.contract && read.contract.centreHitInside && read.contract.lastLineClearsNav),
    cjk_in_contract: hasCjk,
  };
  write("screen_" + arm, out);
  return out;
}

test("ORD-0438 Z 中文臂读到的是合同句，不是「没有数据」，而且落在可命中带里", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const r = await screenArm(page, "zh-CN");
  expect(r.headline_follows_server_projection, "屏上的共几天要等于服务端那一枚：" + JSON.stringify(r.api)).toBe(true);
  expect(r.contract_reachable, "合同那一行必须中心命中且最后一条绘制行在底栏之上：" + JSON.stringify(r.screen.contract)).toBe(true);
  expect(r.cjk_in_contract, "中文臂这一行必须是汉字").toBe(true);
  expect(r.screen.contract?.text ?? "").not.toMatch(/没有数据|暂无/);
  expect(r.screen.contract?.text ?? "").toMatch(/服务端|之后|才|自己/);
  expect(r.day_phrase_on_screen, "已走到第几天要真在这一行上：" + JSON.stringify(r.api)).toBe(true);
  await ctx.close();
});

test("ORD-0438 E 英文臂同一格，零汉字", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const r = await screenArm(page, "en");
  expect(r.headline_follows_server_projection).toBe(true);
  expect(r.contract_reachable).toBe(true);
  expect(r.cjk_in_contract, "英文臂不许漏汉字").toBe(false);
  expect(r.screen.contract?.text ?? "").toMatch(/server|walked|stops|chosen/i);
  expect(r.day_phrase_on_screen, "the day position has to be in the line").toBe(true);
  await ctx.close();
});
