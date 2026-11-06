/**
 * ORD-0468 -- the ladder keeps its own name when the screen has no candidate at all.
 *
 * ORD-0455 put the four rungs on the stay detail panel, and its own rule was that `method` only
 * speaks about a figure that exists. `/v8/stay` with zero candidates therefore fell off the ladder
 * entirely: `StayExplorer` returns the `stay-no-options` branch before the panel is ever mounted, so
 * the two rungs that matter most when nothing came back -- 说方法 and 事前警告 -- said nothing.
 * This batch adds one named empty reading (`ladder.method.noCandidates`) and proves four things on
 * the real screen, in the four-cell shape the commander named at 06:44:
 *
 *   ① does the target row appear at all, with its denominator (how many result cards are on screen)
 *   ② does the count text move with the interaction -- one reading before, one after, and a missing
 *      subject reported as `NOT_MEASURED_*` rather than as 0
 *   ③ how the row sits against the fixed bottom bar, measured after scrolling it into the view
 *   ④ whether any machine token is inside the row -- the whole line read back, then scanned
 *
 * The empty screen is reached by answering the experience route with `options: []`. That witness is
 * type `HTTP_RESPONSE`: it shows what the ladder does with a shape the product can produce (the
 * commander measured the same empty screen on his own tip at 18:40), never that this server sent it
 * on this run. Nothing here invents a candidate, a price, or a second progress system.
 */

import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import {
  estimationLadder,
  ladderTiers,
  type EstimateReading,
  type EstimationTier,
} from "../src/features/estimation/estimationLadder";
import {
  estimationLadderCopy,
  fillLadder,
  ladderCopyFor,
  type LadderCopyKey,
} from "../src/features/estimation/estimationLadderCopy";

const LOGIC = "src/features/estimation/estimationLadder.ts";
const COPY = "src/features/estimation/estimationLadderCopy.ts";
const PANEL = "src/features/estimation/EstimationLadderPanel.tsx";
const HOST = "src/features/stay/StayExplorer.tsx";
const DICTIONARY = "src/i18n/LocaleContext.tsx";
const WALL_CSS = "src/styles.css";
const EMPTY_KEY = "ladder.method.noCandidates";

test.setTimeout(240_000);

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0468-ladder-empty", name + ".json");
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O468 " + name + " " + JSON.stringify(body));
}

const CJK = /[㐀-䶿一-鿿]/;
const SCREAMING = /[A-Z][A-Z0-9]*_[A-Z0-9_]+/g;

const reading = (over: Partial<EstimateReading>): EstimateReading => ({
  amountMinor: null,
  amountLabel: "",
  source: "",
  confidence: null,
  binding: false,
  alternatives: 0,
  canRecompute: false,
  doorLabel: "Trip Studio",
  ...over,
});

/** ① M -- the empty branch is the only shape that may light `noCandidates`, and it lights nothing else. */
test("M 空态那一枚空读数独占说方法那一格，且省略 candidates 的老宿主行为一字不变", async () => {
  const empty = estimationLadder(reading({ candidates: 0 }));
  const emptyWithEverything = estimationLadder(
    reading({ candidates: 0, amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: 0.62, alternatives: 3, canRecompute: true }),
  );
  // A host that never passes `candidates` must keep the ORD-0455 answer, byte for byte in tier order.
  const omitted = estimationLadder(reading({ amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: null, alternatives: 3, canRecompute: true }));
  const one = estimationLadder(reading({ amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: null, alternatives: 3, canRecompute: true, candidates: 1 }));
  const painted = fillLadder(ladderCopyFor("en", EMPTY_KEY as LadderCopyKey), empty.rungs[0].vars);
  const paintedZh = fillLadder(ladderCopyFor("zh-CN", EMPTY_KEY as LadderCopyKey), empty.rungs[0].vars);
  const out = {
    empty_rungs: empty.rungs.map((r) => [r.tier, r.key, r.trigger, Object.keys(r.vars).length]),
    empty_tiers: ladderTiers(empty),
    empty_ignores_a_figure_it_should_not_have: emptyWithEverything.rungs.map((r) => r.key),
    omitted_candiates_tiers: ladderTiers(omitted),
    explicit_one_same_as_omitted: ladderTiers(omitted).join("+") === ladderTiers(one).join("+"),
    keys_also_identical: JSON.stringify(omitted.rungs.map((r) => r.key)) === JSON.stringify(one.rungs.map((r) => r.key)),
    empty_en_painted: painted,
    empty_zh_painted: paintedZh,
    empty_has_no_digit: !/\d/.test(painted) && !/\d/.test(paintedZh),
    empty_has_no_placeholder: !/\{\w+\}/.test(painted) && !/\{\w+\}/.test(paintedZh),
    copy_keys_both_arms: [
      Object.keys(estimationLadderCopy.en).includes(EMPTY_KEY),
      Object.keys(estimationLadderCopy["zh-CN"]).includes(EMPTY_KEY),
    ],
    tiers_seen_in_empty: [...new Set(ladderTiers(empty))],
  };
  write("m_derivation", out);
  expect(out.empty_tiers).toEqual(["method"]);
  expect(out.empty_rungs[0]).toEqual(["method", EMPTY_KEY, "no_candidates", 0]);
  expect(out.empty_ignores_a_figure_it_should_not_have).toEqual([EMPTY_KEY]);
  expect(out.explicit_one_same_as_omitted, "省略 candidates 的老宿主必须一字不变").toBe(true);
  expect(out.keys_also_identical, "连措辞 key 都不能变，否则 ORD-0455 的已交付读数就作废了").toBe(true);
  expect(out.empty_has_no_digit, "空读数里不许出现数字：0 会被读成一次测量").toBe(true);
  expect(out.empty_has_no_placeholder).toBe(true);
  expect(out.copy_keys_both_arms).toEqual([true, true]);
});

/** ② S -- shape of the source: one mount point, no dictionary growth, nothing on the wall. */
test("S 空态挂载只在早退分支里，词典与禁改墙 0 字节，逻辑件里没有文案", async () => {
  const src = (p: string) => readFileSync(p, "utf8");
  const logic = src(LOGIC);
  const host = src(HOST);
  const copy = src(COPY);
  const panel = src(PANEL);
  const dictionary = src(DICTIONARY);
  const styles = src(WALL_CSS);
  const emptyBranch = host.slice(host.indexOf("if (!data.options.length)"), host.indexOf("return <section"));
  const detailBranch = host.slice(host.indexOf("return <section"));
  const out = {
    files_present: [LOGIC, COPY, PANEL, HOST].filter((p) => existsSync(p)).length,
    empty_branch_mounts_ladder: (emptyBranch.match(/<EstimationLadderPanel/g) ?? []).length,
    detail_branch_still_mounts_ladder: (detailBranch.match(/<EstimationLadderPanel/g) ?? []).length,
    empty_branch_still_holds_no_options: emptyBranch.includes('data-testid="stay-no-options"'),
    empty_branch_says_it_outside_the_ladder: /假的价格卡片/.test(emptyBranch),
    no_candidate_key_in_logic_string_table: logic.includes(EMPTY_KEY),
    has_candidates_fact_in_logic: /has_candidates/.test(logic),
    logic_code_has_cjk: CJK.test(logic.replace(/^\s*(\/\/|\/\*|\*).*$/gm, "")),
    empty_key_in_shared_dictionary: dictionary.includes('"' + EMPTY_KEY + '"'),
    // Read through the product's own resolver: a dictionary row may wrap its value onto the next
    // line, and `copy.includes(key + '": "text')` silently misses exactly that shape.
    empty_key_copies: [
      ladderCopyFor("en", EMPTY_KEY as LadderCopyKey).startsWith("There is no method"),
      ladderCopyFor("zh-CN", EMPTY_KEY as LadderCopyKey).startsWith("这一级没有可报的方法"),
    ],
    empty_key_rows_in_copy_module: copy.split(EMPTY_KEY).length - 1,
    panel_candidates_attr: panel.includes("data-ladder-has-candidates"),
    classes_added_to_walled_css: (src("src/features/estimation/estimationLadder.css").match(/^\s*\.[A-Za-z][\w-]*/gm) ?? []).map((s) => s.trim()).filter((c) => styles.includes(c)),
    host_reaches_the_fact_from_options_length: /candidates: 0/.test(host) && /data\.options\.length/.test(host),
  };
  write("s_source_shape", out);
  expect(out.files_present).toBe(4);
  expect(out.empty_branch_mounts_ladder, "早退分支里必须挂上梯子").toBe(1);
  expect(out.detail_branch_still_mounts_ladder, "明细那一屏的挂载不能被挪走").toBe(1);
  expect(out.empty_branch_still_holds_no_options, "空态原有的那句不能被我替掉").toBe(true);
  expect(out.empty_branch_says_it_outside_the_ladder, "那句门（先选好行程再回来）是补救的出处，不能重复造").toBe(true);
  expect(out.has_candidates_fact_in_logic).toBe(true);
  expect(out.logic_code_has_cjk, "档位逻辑里不该出现文案").toBe(false);
  expect(out.empty_key_in_shared_dictionary, "共享字典不该长出这一枚 key").toBe(false);
  expect(out.empty_key_copies).toEqual([true, true]);
  expect(out.empty_key_rows_in_copy_module, "两套语言各一枚，不多不少").toBe(2);
  expect(out.classes_added_to_walled_css).toEqual([]);
  expect(out.host_reaches_the_fact_from_options_length).toBe(true);
});

type ScreenRead = Awaited<ReturnType<typeof probe>>;

const probe = (page: Page) =>
  page.evaluate(() => {
    const nav = document.querySelector<HTMLElement>(".v8-mobile-nav");
    const hitLine = nav ? Math.round(nav.getBoundingClientRect().top) : null;
    const list = document.querySelector<HTMLElement>('[data-testid="estimation-ladder"]');
    const rung = (el: HTMLElement) => {
      const b = el.getBoundingClientRect();
      const cx = Math.round(b.x + b.width / 2);
      const cy = Math.round(b.y + b.height / 2);
      const top = document.elementFromPoint(cx, cy);
      const range = document.createRange();
      range.selectNodeContents(el);
      const bottoms = Array.from(range.getClientRects()).map((r) => Math.round(r.bottom));
      return {
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
        chip: (el.querySelector(".cnx-ladder-tier")?.textContent ?? "").replace(/\s+/g, " ").trim(),
        said: (el.querySelector(".cnx-ladder-said")?.textContent ?? "").replace(/\s+/g, " ").trim(),
        tier: el.getAttribute("data-ladder-tier"),
        trigger: el.getAttribute("data-ladder-trigger"),
        key: el.getAttribute("data-ladder-key"),
        centre: { x: cx, y: cy },
        box: { y: Math.round(b.y), bottom: Math.round(b.bottom), h: Math.round(b.height) },
        centreHitSelf: !!(top && (top === el || el.contains(top))),
        lastLineBottom: bottoms.length ? Math.max(...bottoms) : null,
        lastLineClearsNav: hitLine === null || !bottoms.length ? null : Math.max(...bottoms) <= hitLine,
      };
    };
    const state = document.querySelector<HTMLElement>('[data-testid="stay-no-options"]');
    return {
      url: location.href,
      lang: document.documentElement.lang || null,
      hitLine,
      ladder_present: !!list,
      no_options_present: !!state,
      no_options_painted: state ? (state.textContent ?? "").replace(/\s+/g, " ").trim() : null,
      body_chars: document.body.innerText.replace(/\s+/g, " ").trim().length,
      offer_cards: document.querySelectorAll('[data-testid="stay-offer-card"]').length,
      facts: list
        ? {
            rungs_attr: list.getAttribute("data-ladder-rungs"),
            has_candidates: list.getAttribute("data-ladder-has-candidates"),
            candidates: list.getAttribute("data-ladder-candidates"),
            has_amount: list.getAttribute("data-ladder-has-amount"),
            has_confidence: list.getAttribute("data-ladder-has-confidence"),
            can_switch: list.getAttribute("data-ladder-can-switch"),
            source_token: list.getAttribute("data-cnx-value"),
          }
        : null,
      rungs: list ? Array.from(list.querySelectorAll<HTMLElement>(".cnx-ladder-rung")).map(rung) : [],
    };
  });

const openStay = async (page: Page) => {
  await ensureDemoJourney(page);
  await page.goto("/v8/stay", { waitUntil: "load" });
  await page.getByTestId("v8-stay-explorer").waitFor({ timeout: 25_000 });
  await page.waitForTimeout(1500);
  const card = page.getByTestId("stay-offer-card").first();
  if (await card.isVisible().catch(() => false)) await card.click();
  await page.waitForTimeout(900);
  const ladder = page.getByTestId("estimation-ladder").first();
  await expect(ladder).toBeVisible({ timeout: 15_000 });
  await ladder.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  return probe(page);
};

/** The four cells, computed from one screen reading. ①③④ are direct; ② is the before/after pair. */
const fourCells = (arm: string, read: ScreenRead, denominator: { cards: number; before: ScreenRead | null }) => {
  const lines = read.rungs.map((r) => r.text);
  const screaming = lines.flatMap((t) => t.match(/[A-Z][A-Z0-9]*_[A-Z0-9_]+/g) ?? []);
  return {
    arm,
    cell_1_target_row: {
      ladder_present: read.ladder_present,
      denominator_result_cards_on_screen: read.offer_cards,
      stated_denominator_match: read.offer_cards === denominator.cards,
      empty_state_sentence_still_there: read.no_options_present,
    },
    cell_2_count_moves: {
      rungs_here: read.rungs.length,
      rungs_before_this_change: denominator.before ? denominator.before.rungs.length : "NOT_MEASURED_no_before_reading",
      tiers_here: read.rungs.map((r) => r.tier),
      tiers_before_this_change: denominator.before ? denominator.before.rungs.map((r) => r.tier) : "NOT_MEASURED_no_before_reading",
      painted_here: lines,
      digits_in_empty_reading: lines.join("").match(/\d/g) ?? [],
      placeholder_leaks: lines.filter((t) => /\{\w+\}/.test(t)),
    },
    cell_3_against_bottom_bar: {
      hit_line_px_measured_on_this_box: read.hitLine,
      scroll_into_view_before_measure: true,
      rows: read.rungs.map((r) => ({ text: r.said.slice(0, 28), box: r.box, centre: r.centre, centreHitSelf: r.centreHitSelf, lastLineBottom: r.lastLineBottom, lastLineClearsNav: r.lastLineClearsNav })),
      every_row_centre_hit: read.rungs.every((r) => r.centreHitSelf),
      every_row_clears_nav: read.rungs.every((r) => r.lastLineClearsNav === true),
    },
    cell_4_machine_tokens: {
      scanned_lines: lines,
      screaming_tokens_found: screaming,
      token_count: screaming.length,
      attributes_holding_the_raw_values: read.facts,
    },
    screen: read,
  };
};

test("① 真字节先读一遍：这一代 demo 旅程仍有 4 家候选，空态在真数据下量不到（具名）", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const read = await openStay(page);
  const out = {
    witness: "PRODUCT_BYTES —— 这台服务器这次真的交回了这些候选",
    offer_cards: read.offer_cards,
    ladder_present: read.ladder_present,
    no_options_present: read.no_options_present,
    tiers: read.rungs.map((r) => r.tier),
    keys: read.rungs.map((r) => r.key),
    facts: read.facts,
  };
  write("product_bytes_state", out);
  expect(read.offer_cards, "这代 demo 旅程的候选枚数（下面那格的空态要靠应答出来，先把它钉死）").toBeGreaterThan(0);
  expect(read.ladder_present).toBe(true);
  await ctx.close();
});

/** Answer the experience route with no stay candidates -- the shape the empty branch is written for. */
async function emptyArm(page: Page): Promise<ScreenRead & { patched: string[] }> {
  const patched: string[] = [];
  await page.route(/\/api\/v1\//, async (route) => {
    const url = route.request().url();
    if (!/experience|journey|generate|options/i.test(url)) return route.continue();
    const res = await route.fetch();
    const type = res.headers()["content-type"] ?? "";
    if (!type.includes("json")) return route.fulfill({ response: res });
    const body = await res.text();
    if (!body.includes('"offers"')) return route.fulfill({ response: res, body });
    const data = JSON.parse(body);
    if (!Array.isArray(data?.options)) return route.fulfill({ response: res, body });
    const emptied = { ...data, options: [], offers: [], selected_option_id: null };
    patched.push(url.replace(/^https?:\/\/[^/]+/, "") + " options " + data.options.length + "->0");
    return route.fulfill({ response: res, body: JSON.stringify(emptied) });
  });
  await ensureDemoJourney(page);
  await page.goto("/v8/stay", { waitUntil: "load" });
  await page.getByTestId("v8-stay-explorer").waitFor({ timeout: 25_000 });
  await page.waitForTimeout(1500);
  const ladder = page.getByTestId("estimation-ladder").first();
  await expect(ladder).toBeVisible({ timeout: 15_000 });
  await ladder.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const read = await probe(page);
  return { ...read, patched };
}

test("② 空态中文臂：四格里每一格都读得到，且不是一句口号", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const empty = await emptyArm(page);
  const out = fourCells("empty_zh", empty, { cards: 0, before: null });
  write("cell_four_zh", out);
  await ctx.close();

  expect(out.cell_1_target_row.ladder_present, "空态里梯子必须出现，不许整块隐掉").toBe(true);
  expect(out.cell_1_target_row.denominator_result_cards_on_screen, "分母：屏上结果卡枚数").toBe(0);
  expect(out.cell_1_target_row.empty_state_sentence_still_there, "空态原有那句不能被梯子替掉").toBe(true);
  expect(out.cell_2_count_moves.tiers_here, "空态只该亮说方法那一格").toEqual(["method"]);
  expect(out.cell_2_count_moves.painted_here[0]).toContain("这一级没有可报的方法");
  expect(out.cell_2_count_moves.digits_in_empty_reading, "空读数里不许有数字").toEqual([]);
  expect(out.cell_2_count_moves.placeholder_leaks).toEqual([]);
  expect(out.cell_3_against_bottom_bar.every_row_centre_hit, "那一格必须中心命中：" + JSON.stringify(out.cell_3_against_bottom_bar.rows)).toBe(true);
  expect(out.cell_3_against_bottom_bar.every_row_clears_nav, "末行必须在底栏之上：" + JSON.stringify(out.cell_3_against_bottom_bar.rows)).toBe(true);
  expect(out.cell_4_machine_tokens.token_count, "格子里不许有机器 token：" + JSON.stringify(out.cell_4_machine_tokens.screaming_tokens_found)).toBe(0);
  expect(CJK.test(out.cell_2_count_moves.painted_here[0]), "中文臂必须是中文").toBe(true);
});

test("③ 空态英文臂：同一格、同一枚 key，零汉字", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const empty = await emptyArm(page);
  const out = fourCells("empty_en", empty, { cards: 0, before: null });
  write("cell_four_en", out);
  await ctx.close();

  expect(out.cell_1_target_row.ladder_present).toBe(true);
  expect(out.cell_1_target_row.denominator_result_cards_on_screen).toBe(0);
  expect(out.cell_2_count_moves.tiers_here).toEqual(["method"]);
  expect(out.cell_2_count_moves.painted_here[0]).toContain("There is no method to report at this rung");
  expect(out.cell_2_count_moves.digits_in_empty_reading).toEqual([]);
  expect(out.cell_3_against_bottom_bar.every_row_centre_hit).toBe(true);
  expect(out.cell_3_against_bottom_bar.every_row_clears_nav).toBe(true);
  expect(out.cell_4_machine_tokens.token_count).toBe(0);
  const joined = out.cell_2_count_moves.painted_here.join(" ");
  expect(CJK.test(joined), "英文臂不该出现汉字：" + joined).toBe(false);
});

test("④ 反件：把候选从 4 抹成 0 的那一瞬间，亮的档位真的换了一格（不是同一句话赖在屏上）", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const before = await openStay(page);
  const again = await ctx.newPage();
  await again.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const after = await emptyArm(again);
  const out = {
    cell_2_before_after: {
      witness_before: "PRODUCT_BYTES",
      witness_after: "HTTP_RESPONSE —— 同一台机器、同一把闸、同一次跑",
      before_cards: before.offer_cards,
      after_cards: after.offer_cards,
      before_tiers: before.rungs.map((r) => r.tier),
      after_tiers: after.rungs.map((r) => r.tier),
      before_keys: before.rungs.map((r) => r.key),
      after_keys: after.rungs.map((r) => r.key),
      before_painted: before.rungs.map((r) => r.said),
      after_painted: after.rungs.map((r) => r.said),
      changed: JSON.stringify(before.rungs.map((r) => r.key)) !== JSON.stringify(after.rungs.map((r) => r.key)),
      endpoints_patched: after.patched,
    },
  };
  write("cell_2_pair", out);
  await ctx.close();

  expect(out.cell_2_before_after.before_cards).toBeGreaterThan(0);
  expect(out.cell_2_before_after.after_cards).toBe(0);
  expect(out.cell_2_before_after.changed, "抹掉候选之后屏上的措辞必须真的换了一格").toBe(true);
  expect(out.cell_2_before_after.after_keys).toEqual(["ladder.method.noCandidates"]);
  expect(out.cell_2_before_after.before_keys, "有候选时不该出现空读数那一枚").not.toContain("ladder.method.noCandidates");
  expect(out.cell_2_before_after.endpoints_patched.length, "反件没改到任何响应").toBeGreaterThan(0);
});
