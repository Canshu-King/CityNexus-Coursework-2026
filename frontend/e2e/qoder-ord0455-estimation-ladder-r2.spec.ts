/**
 * ORD-0455 r2 -- the same four tiers, one rung more: ORD-0468 gave the empty screen a named reading.
 *
 * r1 (`qoder-ord0455-estimation-ladder-r1.spec.ts`, published and left untouched) froze its
 * reachability denominator at "10 枚具体措辞". When 0468 added `ladder.method.noCandidates`, r1's M1
 * correctly went red -- the copy module held a rung its own REACH table could not light. That is the
 * denominator moving, not a behaviour regression: M2/S/Z/E/C all stayed green on the same run.
 * r2 fixes the instrument rather than the reading: the set of reachable rungs is now computed against
 * the copy module instead of asserted as a literal count, and the empty shape joins both the reach
 * table and the 不变量 cross-product.
 *
 * ORD-0455 — the estimation ladder: four tiers, each with a trigger condition that can be read.
 *
 * Three faces, because the order asks for two different things and they need different witnesses:
 *
 *  M  the trigger conditions. Every rung is derived from cells the server sent, so the predicate set
 *     is enumerated as a cross-product over those cells and the invariants are read off the
 *     derivation -- never from a literal list of expected tiers.
 *  S  the shape of the source. Wording lives in exactly one module, the ladder logic carries no CJK,
 *     and nothing reached into `i18n/LocaleContext.tsx` or `frontend/src/styles.css`.
 *  Z/E/C  the painted screen. Z and E read what the sealed demo's own stay data lights up, in both
 *     languages, hit-tested inside the reachable band. C answers the experience response with two
 *     stay shapes the demo does not contain, so "all four tiers can land on one screen, in ladder
 *     order, and the boundary between warning and explanation is the confidence cell" is a
 *     measurement rather than an inference from M. C's readings are witnesses of type
 *     `HTTP_RESPONSE`: they show what the ladder does with a shape, never that this server sends it.
 */

import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { ensureDemoJourney } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import {
  ESTIMATION_TIERS,
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
const CSS = "src/features/estimation/estimationLadder.css";
const HOST = "src/features/stay/StayExplorer.tsx";
const WALL_V8 = "src/app/V8Experience.tsx";
const WALL_CSS = "src/styles.css";
const DICTIONARY = "src/i18n/LocaleContext.tsx";

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0455-ladder", name + ".json");
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O455 " + name + " " + JSON.stringify(body));
}

/* The first browser arm on a cold dev server pays vite's transform of the whole graph, and the run
   measured that at over the config's 60 s while a later arm on the same cache finished in seconds.
   The arm budget is raised; no assertion is. */
test.setTimeout(240_000);

const CJK = /[㐀-䶿一-鿿]/;

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

/** Every rung key the derivation can emit, with one reading that emits it. */
const REACH: Array<{ key: string; case: Partial<EstimateReading> }> = [
  { key: "ladder.method.sourced", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "一次采回来的报价回放", confidence: 0.62 } },
  { key: "ladder.method.noConfidence", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "一次采回来的报价回放", confidence: null } },
  { key: "ladder.method.noSource", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "", confidence: 0.62 } },
  { key: "ladder.pre.noAmount", case: { amountMinor: null, amountLabel: "" } },
  { key: "ladder.pre.noConfidence", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: null } },
  { key: "ladder.verify.open", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: 0.62, binding: false } },
  { key: "ladder.verify.binding", case: { amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: 0.62, binding: true } },
  { key: "ladder.remedy.switch", case: { amountMinor: null, amountLabel: "", alternatives: 3 } },
  { key: "ladder.remedy.recompute", case: { amountMinor: null, amountLabel: "", alternatives: 0, canRecompute: true } },
  { key: "ladder.remedy.absent", case: { amountMinor: null, amountLabel: "", alternatives: 0, canRecompute: false } },
  { key: "ladder.method.noCandidates", case: { candidates: 0, amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: 0.62, alternatives: 3, canRecompute: true } },
];

test("M1 每一档措辞都有触发条件点得亮，且没有谁都不引用的死文案", async () => {
  const perKey = REACH.map((row) => {
    const ladder = estimationLadder(reading(row.case));
    return {
      key: row.key,
      fires: ladder.rungs.map((r) => r.key),
      has_it: ladder.rungs.some((r) => r.key === row.key),
      tiers: ladderTiers(ladder),
    };
  });
  const unreachable = perKey.filter((e) => !e.has_it).map((e) => e.key);
  const derivedKeys = new Set<string>();
  for (const row of REACH) for (const rung of estimationLadder(reading(row.case)).rungs) derivedKeys.add(rung.key);
  const copyKeys = Object.keys(estimationLadderCopy.en);
  const tierKeys = copyKeys.filter((k) => k.startsWith("ladder.tier."));
  const copyRungKeys = copyKeys.filter((k) => !tierKeys.includes(k));
  const orphans = copyKeys.filter((k) => !derivedKeys.has(k) && !tierKeys.includes(k));
  const neverReached = copyRungKeys.filter((k) => !derivedKeys.has(k)).sort();
  const derivedButAbsent = [...derivedKeys].filter((k) => !copyRungKeys.includes(k)).sort();
  write("m1_reachability", {
    rung_keys_derived: derivedKeys.size,
    rung_keys_in_copy: copyRungKeys.length,
    rung_keys_with_copy: [...derivedKeys].filter((k) => copyKeys.includes(k)).length,
    copy_rungs_never_derived: neverReached,
    derived_without_copy: derivedButAbsent,
    unreachable,
    orphan_copy_keys: orphans,
    per_key: perKey,
  });
  expect(unreachable, "有措辞点不亮：" + JSON.stringify(unreachable)).toEqual([]);
  expect(orphans, "有措辞谁都不引用：" + JSON.stringify(orphans)).toEqual([]);
  expect(neverReached, "文案里有枚措辞谁都不点亮：" + JSON.stringify(neverReached)).toEqual([]);
  expect(derivedButAbsent, "推导里出现了文案没有的 key：" + JSON.stringify(derivedButAbsent)).toEqual([]);
  expect(perKey.filter((e) => !e.has_it).length, "可达集合必须正好等于文案模块的措辞集合").toBe(0);
});

test("M2 触发条件的不变量：警告必配补救、没有数就不许说方法与核对、档位按梯序", async () => {
  const amounts: Array<number | null> = [null, 0, 156000];
  const labels = ["", "¥1,560.00"];
  const sources = ["", "一次采回来的报价回放"];
  const confidences: Array<number | null> = [null, 0, 0.62];
  const violations: string[] = [];
  const tiersSeen = new Set<EstimationTier>();
  let cases = 0;

  for (const amountMinor of amounts)
    for (const amountLabel of labels)
      for (const source of sources)
        for (const confidence of confidences)
          for (const binding of [false, true])
            for (const alternatives of [0, 3])
              for (const canRecompute of [false, true])
                // ORD-0468 adds a dimension: the screen may hold no candidate at all.
                for (const candidates of [undefined, 0, 2]) {
                cases += 1;
                const r = reading({ amountMinor, amountLabel, source, confidence, binding, alternatives, canRecompute, candidates });
                const { rungs, facts } = estimationLadder(r);
                for (const rung of rungs) tiersSeen.add(rung.tier);
                const of = (t: EstimationTier) => rungs.filter((x) => x.tier === t);
                const shown = facts.has_amount && amountLabel.length > 0;
                const shape = JSON.stringify({ amountMinor, amountLabel, source, confidence, binding, alternatives, canRecompute, candidates });
                if (!facts.has_candidates) {
                  // The empty screen owes exactly one rung and cannot light anything else.
                  if (JSON.stringify(rungs.map((x) => x.key)) !== JSON.stringify(["ladder.method.noCandidates"]))
                    violations.push("空态那一形不是一枚具名空读数: " + shape + " -> " + JSON.stringify(rungs.map((x) => x.key)));
                  continue;
                }

                if ((of("pre_warning").length > 0) !== (of("remedy").length > 0))
                  violations.push("警告与补救不配对: " + shape);
                if (of("pre_warning").length > 1) violations.push("两条警告: " + shape);
                if (of("remedy").length > 1) violations.push("两条补救: " + shape);
                if (!shown && (of("method").length || of("post_verify").length))
                  violations.push("没有数却还在解释/要求核对: " + shape);
                if (shown && of("method").length !== 1) violations.push("有数却不只一档解释: " + shape);
                if (shown && of("post_verify").length !== 1) violations.push("有数却不只一档核对: " + shape);
                const order = rungs.map((x) => ESTIMATION_TIERS.indexOf(x.tier));
                if (order.some((v, i) => i > 0 && v < order[i - 1]))
                  violations.push("梯序被打乱: " + JSON.stringify(rungs.map((x) => x.tier)));
                for (const rung of rungs) {
                  const painted = fillLadder(ladderCopyFor("en", rung.key as LadderCopyKey), rung.vars);
                  for (const token of painted.match(/\{(\w+)\}/g) ?? []) {
                    const name = token.slice(1, -1);
                    // Only a cell the server genuinely did not send may stay visible as a
                    // placeholder; an empty {amount} on a rung that needs a figure is a bug.
                    if (name === "amount" && shown) violations.push("方法档漏出空 " + token + ": " + painted);
                    if (name === "amount" && !shown && rung.tier !== "method") continue;
                    if (name === "door" && !r.canRecompute && rung.key !== "ladder.remedy.absent")
                      violations.push("门不存在却引用了门: " + rung.key);
                  }
                }
              }

  const neverReached = ESTIMATION_TIERS.filter((t) => !tiersSeen.has(t));
  write("m2_invariants", {
    cases,
    violations,
    violations_total: violations.length,
    tiers_seen: [...tiersSeen],
    tiers_never_reached: neverReached,
    boundary: [
      { shape: "数+置信度+不可取消", tiers: ladderTiers(estimationLadder(reading({ amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: 0.62, binding: true, alternatives: 3, canRecompute: true }))) },
      { shape: "数+无置信度（四档齐）", tiers: ladderTiers(estimationLadder(reading({ amountMinor: 156000, amountLabel: "¥1,560.00", source: "s", confidence: null, binding: true, alternatives: 3, canRecompute: true }))) },
      { shape: "无数+两家门", tiers: ladderTiers(estimationLadder(reading({ amountMinor: null, alternatives: 3, canRecompute: true }))) },
      { shape: "无数+无门", tiers: ladderTiers(estimationLadder(reading({ amountMinor: null, alternatives: 0, canRecompute: false }))) },
    ],
  });
  expect(neverReached, "四档里有点不到的：" + JSON.stringify(neverReached)).toEqual([]);
  expect(violations, "不变量违反 " + violations.length + " 例，首 3 例 " + JSON.stringify(violations.slice(0, 3))).toEqual([]);
  expect(cases).toBe(3 * 2 * 2 * 3 * 2 * 2 * 2 * 3);
});

test("S 文案只有一处、逻辑里没有中文、共享字典与禁改墙一字未动", async () => {
  const src = (p: string) => readFileSync(p, "utf8");
  const logic = src(LOGIC);
  const copy = src(COPY);
  const panel = src(PANEL);
  const css = src(CSS);
  const host = src(HOST);
  const dictionary = src(DICTIONARY);
  const styles = src(WALL_CSS);
  const enKeys = Object.keys(estimationLadderCopy.en).sort();
  const zhKeys = Object.keys(estimationLadderCopy["zh-CN"]).sort();
  const logicCode = logic.replace(/^\s*(\/\/|\/\*|\*).*$/gm, "");
  const out = {
    files_present: [LOGIC, COPY, PANEL, CSS].filter((p) => existsSync(p)).length,
    logic_has_cjk_in_code: CJK.test(logicCode),
    copy_has_both_arms: /en:\s*\{/.test(copy) && /"zh-CN":\s*\{/.test(copy),
    panel_falls_back_to_zh_ternary: /\?\?\s*\(?\s*zh\s*\?/.test(panel),
    panel_reads_words_only_via_copy: /ladderCopyFor\(locale/.test(panel),
    panel_painted_testids: (panel.match(/data-testid=\{?"([^"}]+)/g) ?? []).length,
    ladder_keys_in_shared_dictionary: enKeys.filter((k) => dictionary.includes('"' + k + '"')),
    ladder_classes_in_styles_css: (css.match(/^\s*\.[A-Za-z][\w-]*/gm) ?? []).map((s) => s.trim()).filter((c) => styles.includes(c)),
    css_declares_colour: /(^|[\s{;])color\s*:/.test(css),
    key_parity: enKeys.length === zhKeys.length && enKeys.every((k, i) => k === zhKeys[i]),
    keys_total: enKeys.length,
    host_names_the_shared_display_layer: /evidenceSourceLabel/.test(host),
    host_passes_the_door_in: /t\("nav\.studio"\)/.test(host) && /Boolean\(onNavigate\)/.test(host),
  };
  write("s_source_shape", out);
  expect(out.files_present, "四枚新文件都要在树上").toBe(4);
  expect(out.logic_has_cjk_in_code, "档位逻辑里不该出现文案").toBe(false);
  expect(out.copy_has_both_arms, "两套语言必须在同一件里").toBe(true);
  expect(out.panel_falls_back_to_zh_ternary, "组件不许自造就近三元兜底").toBe(false);
  expect(out.panel_reads_words_only_via_copy, "组件只能从文案模块取词").toBe(true);
  expect(out.ladder_keys_in_shared_dictionary, "共享字典不该长出这几枚 key").toEqual([]);
  expect(out.ladder_classes_in_styles_css, "禁改墙 styles.css 不该出现这批类名").toEqual([]);
  expect(out.css_declares_colour, "这批不动任何颜色，免得改动暗色对比读数").toBe(false);
  expect(out.key_parity, "两套语言必须同键").toBe(true);
  expect(out.host_names_the_shared_display_layer, "来源那一格必须走共享显示层").toBe(true);
  expect(out.host_passes_the_door_in, "补救那一档的门要由宿主真的交进来").toBe(true);
});

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
        // The chip and the sentence are separate nodes; `textContent` glues them ("怎么来的这台
        // 服务器…"), so each is read on its own and the wording claim is made about the sentence.
        chip: (el.querySelector(".cnx-ladder-tier")?.textContent ?? "").replace(/\s+/g, " ").trim(),
        said: (el.querySelector(".cnx-ladder-said")?.textContent ?? "").replace(/\s+/g, " ").trim(),
        tier: el.getAttribute("data-ladder-tier"),
        trigger: el.getAttribute("data-ladder-trigger"),
        key: el.getAttribute("data-ladder-key"),
        centre: { x: cx, y: cy },
        centreHitSelf: !!(top && (top === el || el.contains(top))),
        centreAboveNav: hitLine === null ? null : cy < hitLine,
        lastLineBottom: bottoms.length ? Math.max(...bottoms) : null,
        lastLineClearsNav: hitLine === null || !bottoms.length ? null : Math.max(...bottoms) <= hitLine,
      };
    };
    const known = document.querySelector<HTMLElement>('[data-testid="stay-price-known"]');
    const unknown = document.querySelector<HTMLElement>('[data-testid="stay-price-unknown"]');
    return {
      url: location.href,
      lang: document.documentElement.lang || null,
      hitLine,
      present: !!list,
      facts: list
        ? {
            rungs_attr: list.getAttribute("data-ladder-rungs"),
            has_amount: list.getAttribute("data-ladder-has-amount"),
            has_source: list.getAttribute("data-ladder-has-source"),
            has_confidence: list.getAttribute("data-ladder-has-confidence"),
            binding: list.getAttribute("data-ladder-binding"),
            can_switch: list.getAttribute("data-ladder-can-switch"),
            can_recompute: list.getAttribute("data-ladder-can-recompute"),
            source_token: list.getAttribute("data-cnx-value"),
          }
        : null,
      price: {
        known: known ? (known.textContent ?? "").replace(/\s+/g, " ").trim() : null,
        unknown: unknown ? (unknown.textContent ?? "").replace(/\s+/g, " ").trim() : null,
      },
      rungs: list ? Array.from(list.querySelectorAll<HTMLElement>(".cnx-ladder-rung")).map(rung) : [],
    };
  });

type ScreenRead = Awaited<ReturnType<typeof probe>>;

/** Re-derive the rungs from the facts the DOM reports the server sent, independent of the component. */
const tiersFromDomFacts = (read: ScreenRead): EstimationTier[] | null => {
  const f = read.facts;
  if (!f) return null;
  const yes = (v: string | null) => v === "yes";
  return ladderTiers(
    estimationLadder(
      reading({
        amountMinor: yes(f.has_amount) ? 1 : null,
        amountLabel: read.price.known ?? "",
        source: yes(f.has_source) ? String(f.source_token || "labelled") : "",
        confidence: yes(f.has_confidence) ? 0.5 : null,
        binding: yes(f.binding),
        alternatives: yes(f.can_switch) ? 1 : 0,
        canRecompute: yes(f.can_recompute),
      }),
    ),
  );
};

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

const judge = (arm: string, read: ScreenRead, lang: "zh" | "en") => {
  const predicted = tiersFromDomFacts(read);
  const texts = read.rungs.map((r) => r.said);
  const chips = read.rungs.map((r) => r.chip);
  const glued = read.rungs.map((r) => r.text);
  const out = {
    arm,
    predicted_from_dom_facts: predicted,
    painted: read.rungs.map((r) => r.tier),
    painted_keys: read.rungs.map((r) => r.key),
    triggers: read.rungs.map((r) => r.trigger),
    tier_chips: chips,
    sentences: texts,
    rung_text_content: glued,
    all_rungs_hit: read.rungs.every((r) => r.centreHitSelf && r.lastLineClearsNav === true),
    leaked_placeholder: texts.filter((t) => /\{\w+\}/.test(t)),
    empty_sentences: texts.filter((t) => t.length < 8).length,
    cjk_rungs: texts.filter((t) => CJK.test(t)).length,
    screen: read,
  };
  expect(out.painted, arm + "：屏上档位必须等于按 DOM 事实重算出来的档位").toEqual(predicted);
  expect(out.empty_sentences, arm + "：每一档都得是一句话，不能是空串或光杆标签").toBe(0);
  expect(out.all_rungs_hit, arm + "：每一档都要中心命中且末行在底栏之上：" + JSON.stringify(read.rungs)).toBe(true);
  expect(out.leaked_placeholder, arm + "：不许把没填上的占位符放上屏：" + JSON.stringify(out.leaked_placeholder)).toEqual([]);
  expect(chips.every((c) => c.length > 0), arm + "：档位名必须一起印在句子旁边").toBe(true);
  expect(read.rungs.every((x) => x.text.startsWith(x.chip) && x.text.includes(x.said)),
    arm + "：档位名必须印在句子前面，两者在同一格内").toBe(true);
  if (lang === "zh") expect(out.cjk_rungs, "中文臂每一档都得是中文").toBe(texts.length);
  else expect(out.cjk_rungs, "英文臂不该出现汉字").toBe(0);
  return out;
};

test("Z 中文臂：住处这一屏真读到了梯子的措辞，且落在可命中带里", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  const api: string[] = [];
  page.on("request", (r) => {
    if (/\/api\/v1\//.test(r.url())) api.push(r.method() + " " + r.url().replace(/^https?:\/\/[^/]+/, ""));
  });
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  const read = await openStay(page);
  const out = judge("zh-CN", read, "zh");
  write("screen_zh", { ...out, api_calls_named: [...new Set(api)].slice(0, 40) });
  await ctx.close();
});

test("E 英文臂：同一格同一套触发条件，零汉字", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  const out = judge("en", await openStay(page), "en");
  write("screen_en", out);
  await ctx.close();
});

/**
 * CONTROL. The demo's own stay evidence carries no confidence cell, so the boundary between "explain
 * the figure" and "warn before comparing" cannot be walked on live bytes. These two arms answer the
 * experience response with the two shapes that straddle that boundary and read the screen each time.
 */
async function controlArm(page: Page, shape: "no-confidence" | "with-confidence"): Promise<ScreenRead & { patched: string[] }> {
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
    data.options.forEach((option: { stay?: Record<string, unknown> }, index: number) => {
      const stay = option?.stay;
      if (!stay) return;
      stay.total_amount_minor = 156000 + index * 1000;
      stay.nightly_amount_minor = 52000 + index * 1000;
      stay.currency = "CNY";
      stay.refundable = false;
      stay.evidence =
        shape === "no-confidence" ? { source: "CAPTURED_REPLAY" } : { source: "CAPTURED_REPLAY", confidence: 0.62 };
    });
    patched.push(url.replace(/^https?:\/\/[^/]+/, "") + " options=" + data.options.length);
    return route.fulfill({ response: res, body: JSON.stringify(data) });
  });
  const read = await openStay(page);
  return { ...read, patched };
}

test("C 反件：四档能否同屏按梯序落地，边界就在置信度那一格", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));

  const four = await controlArm(page, "no-confidence");
  const fourOut = judge("control_no_confidence", four, "zh");
  const two = await controlArm(page, "with-confidence");
  const twoOut = judge("control_with_confidence", two, "zh");
  await ctx.close();

  expect(four.patched.length, "反件没改到任何响应：" + JSON.stringify(four.patched)).toBeGreaterThan(0);
  expect(two.patched.length, "第二枚反件没改到任何响应：" + JSON.stringify(two.patched)).toBeGreaterThan(0);
  expect(fourOut.painted, "有数、无置信度 ⇒ 四档齐全：" + JSON.stringify(fourOut.triggers)).toEqual([
    "method",
    "pre_warning",
    "post_verify",
    "remedy",
  ]);
  expect(fourOut.sentences.every((s) => CJK.test(s)), "中文臂四档都是中文").toBe(true);
  expect(twoOut.painted, "补上置信度 ⇒ 警告与补救自己退场：" + JSON.stringify(twoOut.triggers)).toEqual([
    "method",
    "post_verify",
  ]);
  write("control_pair", {
    witness: "HTTP_RESPONSE —— 这两代形状是本地应答出来的，不是这台服务器给的；判据是梯子的边界，不是产品数据",
    endpoints_patched: four.patched.concat(two.patched),
    four_tiers: fourOut.sentences,
    four_triggers: fourOut.triggers,
    two_tiers: twoOut.sentences,
    two_triggers: twoOut.triggers,
  });
});
