/**
 * ORD-0338 (甲) · 向导那段空白消掉之后，sticky 条的承诺必须仍然成立 —— 而且这次由布局负责，不由留白负责
 *
 * 定性收据：`evidence/ord0338-wizard-gap-D/ord0338_gap_anatomy_r1.json`（那 212px 归
 * `.cn-onboarding .trial-steps` 的 padding-bottom，且 load-bearing）。
 * 挪位置那一版被现尖的门否掉：`evidence/ord0338-wizard-gap-D/ord0338_fix_attempt_r1.json`。
 * 号令者 Q73 裁甲：给条一份真正的 in-flow 预留（条做成流内元素），两处 padding hack 一起删。
 *
 * 修法（`frontend/src/design-tokens.css` + `frontend/src/styles.css` + `JourneyTrialSetup.tsx`）：
 * `.cn-onboarding` 变成两行网格 —— 第一行 `.cn-onboarding__scroll` 是唯一的滚动容器，
 * 第二行是 `.trial-sticky-cta` 本体。一行网格盖不住内容，所以内容不需要为它留白。
 *
 * 那一处 padding 在 ≤680px 是 76+136=212px、在 >680px 是 76px（同一条声明的桌面分支），两档都归
 * 本单改，所以两档都自己量：
 *   mobile 390×844（hasTouch，与 ORD-0261/0264 已签的门同一枚介质）
 *     N1 带上界 / N2 不占高不显示 / N3 面板自己滚 + 条贴住面板底边 / N4 条在任何滚动位置都不盖内容
 *     N5 条不撞舞台球 / N6 臂甲：退回覆盖层 ⇒ N4 当场红 / N7 臂乙：装回那条 padding ⇒ N1 当场红
 *   desktop 1280×800
 *     D1 带上界 / D2 条不盖内容 / D3 条不撞舞台球
 *
 * 判据全部读渲染后的文档（rect / getComputedStyle / elementFromPoint / matchMedia），不读源码字符串；
 * 每份读数把**量到的**视口写进收据，不写文件里声明的那个（本量具自己栽过一次：重写文件时漏掉
 * `test.use`，整批"移动"读数其实是 1280 桌面，被 N7 的落地见证逮住）。
 * 两枚臂都只在页面里 `addStyleTag`，落盘产品字节 0 改。
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0338-wizard-gap-clearance.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const BAND_BOUND = 60;
const SWEEP_STEP = 32;
const SCROLL = '[data-testid="trial-scroll"]';
const CTA = '[data-testid="trial-sticky-cta"]';
const PROBE = "input, textarea, button, summary, label > span, [data-testid], .trial-supported-list li";

/* The pane is a `.v8-page`, and `.v8-page` opens with `v8-reveal .25s` (styles.css), whose
   `from` state is `translateY(4px)`.  Measuring rects during it reads a layout that is not there
   yet -- the first run of this instrument caught the CTA 6px off its own row because of it. */
async function settle(page: Page) {
  return page
    .waitForFunction(
      () => {
        const host = document.querySelector(".cn-onboarding");
        if (!host) return false;
        const running = (host as HTMLElement).getAnimations({ subtree: false });
        return running.length === 0 || running.every((a) => a.playState === "finished");
      },
      { timeout: 5_000 },
    )
    .then(() => true)
    .catch(() => false);
}

async function openWizard(page: Page) {
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.waitForSelector('[data-testid="trial-setup"]', { timeout: 60_000 });
  await page.waitForSelector('[data-testid="trial-city"]', { timeout: 30_000 });
  const settled = await settle(page);
  return { settled };
}

/** What the browser actually laid out at, read back from the page -- never from this file. */
async function measuredViewport(page: Page) {
  return page.evaluate(() => ({
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    mq680: window.matchMedia("(max-width: 680px)").matches,
  }));
}

/** The band between the step row and the first field's own label -- the thing ORD-0338 was opened for. */
async function band(page: Page): Promise<any> {
  return page.evaluate(
    ([bandBound]) => {
      const norm = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();
      const steps = document.querySelector('[data-testid="trial-steps"]');
      const city = document.querySelector('[data-testid="trial-city"]');
      if (!steps || !city) return { missing: "trial-steps 或 trial-city 不在 DOM" };
      const items = Array.from(steps.querySelectorAll("li"));
      const label = city.closest("label")?.querySelector("span") ?? city;
      const top = Math.round(Math.max(...items.map((li) => li.getBoundingClientRect().bottom)));
      const bottom = Math.round(label.getBoundingClientRect().top);
      const members: Array<Record<string, unknown>> = [];
      for (const el of Array.from(document.querySelectorAll("body *"))) {
        const r = el.getBoundingClientRect();
        if (r.height <= 0 || r.width <= 0) continue;
        if (r.top < top - 1 || r.bottom > bottom + 1) continue;
        if (el.contains(steps) || el.contains(label) || steps.contains(el) || label.contains(el)) continue;
        const own = norm(
          Array.from(el.childNodes)
            .filter((n) => n.nodeType === 3)
            .map((n) => n.textContent)
            .join(" "),
        );
        members.push({
          tag: el.tagName.toLowerCase(),
          testid: el.getAttribute("data-testid"),
          ownText: own.slice(0, 40),
          height: Math.round(r.height),
        });
      }
      return {
        viewport: { innerWidth: window.innerWidth, innerHeight: window.innerHeight },
        band: { top, bottom, height: bottom - top },
        label: norm(label.textContent),
        members,
        tallestMember: members.reduce((m, e) => Math.max(m, Number(e.height)), 0),
        bound_px: bandBound,
        computed: {
          stepsPaddingBottom: getComputedStyle(steps).paddingBottom,
          cardPaddingBottom: getComputedStyle(document.querySelector(".trial-card")!).paddingBottom,
          sectionDisplay: getComputedStyle(document.querySelector(".cn-onboarding")!).display,
          /* Read defensively on purpose: on a tree without the pane this element does not exist,
             and `getComputedStyle(null)` would turn a product red into a TypeError. */
          scrollOverflow: (() => {
            const el = document.querySelector(".cn-onboarding__scroll");
            return el ? getComputedStyle(el).overflowY : "NO-PANE";
          })(),
          ctaPosition: getComputedStyle(document.querySelector(".trial-sticky-cta")!).position,
        },
      };
    },
    [BAND_BOUND] as const,
  );
}

/**
 * N4 / D2's measurement, on its own, so an arm can drive the very same instrument.  A node counts
 * as "on screen" only inside the scroll container's own box: content the container has clipped away
 * is not covered by chrome, and calling it covered is how this instrument first reported 4 false
 * overlaps (`trial-country` / `trial-currency`, scrolled out of view, centres sitting in the band).
 */
async function overlaps(page: Page): Promise<any> {
  return page.evaluate(
    async ([scrollSel, ctaSel, step, probeSel]) => {
      const scroller = document.querySelector(scrollSel) as HTMLElement | null;
      const cta = document.querySelector(ctaSel) as HTMLElement | null;
      if (!scroller || !cta) return { missing: "面板或条不在 DOM" };
      const max = Math.max(0, Math.round(scroller.scrollHeight - scroller.clientHeight));
      const was = scroller.scrollTop;
      const hits: Array<Record<string, unknown>> = [];
      const offsets: number[] = [];
      for (let off = 0; off <= max; off += step) offsets.push(off);
      if (offsets[offsets.length - 1] !== max) offsets.push(max);
      let scrollBottom = 0;
      let barTop = 0;
      let nodesProbed = 0;
      for (const off of offsets) {
        scroller.scrollTop = off;
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        const sr = scroller.getBoundingClientRect();
        const bar = cta.getBoundingClientRect();
        scrollBottom = Math.round(sr.bottom);
        barTop = Math.round(bar.top);
        for (const el of Array.from(scroller.querySelectorAll(probeSel))) {
          const r = el.getBoundingClientRect();
          if (r.height <= 0 || r.width <= 0) continue;
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          /* one pixel of margin: when the band starts exactly where the row ends, a centre on
             that boundary row belongs to neither, and elementFromPoint answers with the bar. */
          if (cy < sr.top + 1 || cy > sr.bottom - 1 || cy < 1 || cy > window.innerHeight - 1) continue;
          if (cta.contains(el)) continue;
          nodesProbed += 1;
          const landed = document.elementFromPoint(cx, cy);
          if (landed && cta.contains(landed)) {
            hits.push({
              offset: off,
              el: el.getAttribute("data-testid") || el.tagName.toLowerCase(),
              elText: String(el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 24),
              centre: [Math.round(cx), Math.round(cy)],
              landed: landed.getAttribute("data-testid") || landed.tagName.toLowerCase(),
              scrollportBottom: Math.round(sr.bottom),
              barTop: Math.round(bar.top),
            });
            break;
          }
        }
      }
      scroller.scrollTop = was;
      return {
        viewport: { innerWidth: window.innerWidth, innerHeight: window.innerHeight },
        max,
        sampled: offsets.length,
        nodesProbed,
        scrollBottom,
        barTop,
        barBottom: Math.round(cta.getBoundingClientRect().bottom),
        disjoint: barTop >= scrollBottom - 1,
        hits,
        hitCount: hits.length,
      };
    },
    [SCROLL, CTA, SWEEP_STEP, PROBE] as const,
  );
}

/** A real wheel roll from the middle of the scroll container, which is where a thumb is. */
async function rollWheel(page: Page, px = 240) {
  const centre = await page.evaluate((s) => {
    const el = document.querySelector(s) as HTMLElement | null;
    if (!el) return [-1, -1];
    const r = el.getBoundingClientRect();
    return [Math.round(r.left + r.width / 2), Math.round(r.top + Math.min(r.height / 2, 300))];
  }, SCROLL);
  if (centre[0] < 0) return -1;
  await page.mouse.move(centre[0], centre[1]);
  await page.mouse.wheel(0, px);
  await page.waitForTimeout(120);
  return page.evaluate(
    (s) => {
      const el = document.querySelector(s) as HTMLElement | null;
      return el ? Math.round(el.scrollTop) : -1;
    },
    SCROLL,
  );
}

/** Step 1 with three fields filled is shorter than the pane; open the blocks so a sweep has ground. */
async function fillAndExpand(page: Page) {
  await page.getByTestId("trial-city").fill("天津");
  await page.getByTestId("trial-arrival").fill("天津站");
  await page.getByTestId("trial-departure").fill("天津西站");
  await page.getByTestId("trial-show-advanced").click();
  await expect(page.locator(".trial-advanced")).toHaveAttribute("open", "");
  await page.getByTestId("trial-supported-toggle").click();
  await expect(page.locator(".trial-supported-list li").first()).toBeVisible();
}

function writeArm(arm: string, payload: Record<string, unknown>) {
  writeFileSync(
    evidencePath(`ord0338-jia-${arm}.json`),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0338",
        file: FILE,
        arm,
        run: process.env.CITYNEXUS_E2E_RUN_ID ?? "dev",
        tree_sha: process.env.CNX_TREE_SHA ?? "CNX_TREE_SHA-unset",
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        sweep_step_px: SWEEP_STEP,
        ...payload,
      },
      null,
      1,
    ),
  );
}

test.describe("mobile 390x844", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("N1 带上界：步骤条与「目的地」之间不再有一整块空白", async ({ page }) => {
    test.setTimeout(300_000);
    const s = await openWizard(page);
    const g = await band(page);
    const media = await measuredViewport(page);
    writeArm("N1-band-bound", { ...g, settled: s.settled, media });
    expect(g.missing, node("N1", `向导没挂全：${g.missing}`)).toBeUndefined();
    expect(media.innerWidth <= 680, node("N1", `量到的视口宽 ${media.innerWidth}px，这一枚要在 ≤680px 的量具上跑`)).toBe(true);
    expect(g.label, node("N1", `带下方不是「目的地」而是「${g.label}」，量错了位置`)).toContain("目的地");
    expect(
      g.band.height <= BAND_BOUND,
      node("N1", `带高 ${g.band.height}px 超过上界 ${BAND_BOUND}px；computed=${JSON.stringify(g.computed)}`),
    ).toBe(true);
  });

  test("N2 不许「占高但不显示」：带内 0 枚内容节点", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    const g = await band(page);
    writeArm("N2-no-invisible-reserved", g);
    expect(g.missing, node("N2", `量具没挂上：${g.missing}`)).toBeUndefined();
    expect(g.members.filter((m: any) => m.ownText).length, node("N2", `带里有带文字的节点：${JSON.stringify(g.members)}`)).toBe(0);
    expect(
      g.tallestMember <= BAND_BOUND,
      node("N2", `带里有 ${g.tallestMember}px 高的节点却不显示内容（塌布局占高过不了这一关）：${JSON.stringify(g.members)}`),
    ).toBe(true);
  });

  test("N3 面板自己滚、条钉在面板底边（两种形状都量）", async ({ page }) => {
    test.setTimeout(300_000);
    const s = await openWizard(page);
    await page.getByTestId("trial-city").fill("天津");
    const rest = await page.evaluate(
      ([sc, ct]) => {
        const scrollEl = document.querySelector(sc) as HTMLElement | null;
        const cta = document.querySelector(ct) as HTMLElement | null;
        const pane = document.querySelector(".cn-onboarding");
        if (!scrollEl || !cta || !pane) {
          return { missing: `没有 ${sc} 这条流内滚动容器（旧形状）`, ctaPosition: cta ? getComputedStyle(cta).position : "NONE" };
        }
        const bar = cta.getBoundingClientRect();
        return {
          overflowPx: Math.round(scrollEl.scrollHeight - scrollEl.clientHeight),
          windowScrollY: Math.round(window.scrollY),
          ctaBottomGapToPane: Math.round(pane.getBoundingClientRect().bottom - bar.bottom),
        };
      },
      [SCROLL, CTA] as const,
    );
    await page.evaluate((sc) => {
      const el = document.querySelector(sc) as HTMLElement | null;
      if (el) el.scrollTop = 0;
    }, SCROLL);
    const rolledBy = await rollWheel(page);
    const atEnd = await page.evaluate(
      ([sc, ct]) => {
        const scrollEl = document.querySelector(sc) as HTMLElement | null;
        const cta = document.querySelector(ct) as HTMLElement | null;
        const pane = document.querySelector(".cn-onboarding");
        if (!scrollEl || !cta || !pane) {
          return { missing: `滚到底这一步没有 ${sc}`, ctaPosition: cta ? getComputedStyle(cta).position : "NONE" };
        }
        scrollEl.scrollTop = scrollEl.scrollHeight;
        const bar = cta.getBoundingClientRect();
        return {
          scrollTopAtEnd: Math.round(scrollEl.scrollTop),
          windowScrollY: Math.round(window.scrollY),
          ctaBottomGapToPane: Math.round(pane.getBoundingClientRect().bottom - bar.bottom),
          ctaPosition: getComputedStyle(cta).position,
        };
      },
      [SCROLL, CTA] as const,
    );
    writeArm("N3-pane-scrolls", {
      media: await measuredViewport(page),
      settled: s.settled,
      rest,
      wheel: { rolledByRealWheel: rolledBy },
      atEnd,
    });
    expect(s.settled, node("N3", "页面动画没停就量了几何，读数不可信")).toBe(true);
    expect(atEnd.ctaPosition, node("N3", `条的 position 是 ${atEnd.ctaPosition}，不是流内行`)).not.toBe("sticky");
    expect(rest.overflowPx, node("N3", `面板内容只有 ${rest.overflowPx}px 可滚，这一步没溢出，N3/N4 量不到东西`)).toBeGreaterThan(64);
    expect(rolledBy, node("N3", "真滚轮没让面板动过一格——旅客的手势到不了这一步以下的内容")).toBeGreaterThan(0);
    expect(rest.windowScrollY, node("N3", `静息时窗口 scrollY=${rest.windowScrollY}，面板不是唯一滚动容器`)).toBe(0);
    expect(atEnd.windowScrollY, node("N3", `滚到底时窗口 scrollY=${atEnd.windowScrollY}，出现了双滚动`)).toBe(0);
    expect(Math.abs(rest.ctaBottomGapToPane), node("N3", `静息时条没贴住面板底边，差 ${rest.ctaBottomGapToPane}px`)).toBeLessThanOrEqual(2);
    expect(Math.abs(atEnd.ctaBottomGapToPane), node("N3", `滚到底时条没贴住面板底边，差 ${atEnd.ctaBottomGapToPane}px`)).toBeLessThanOrEqual(2);
  });

  test("N4 条在任何滚动位置都不盖内容（结构 + 逐点命中）", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    await fillAndExpand(page);
    const o = await overlaps(page);
    await page.screenshot({
      path: evidencePath(`ord0338-jia-N4-mobile-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
    });
    writeArm("N4-bar-covers-nothing-mobile", { media: await measuredViewport(page), ...o });
    expect(o.missing, node("N4", o.missing)).toBeUndefined();
    expect(o.max, node("N4", `面板只溢出 ${o.max}px，扫不出几个滚动位置`)).toBeGreaterThanOrEqual(SWEEP_STEP * 4);
    expect(o.sampled, node("N4", `只扫到 ${o.sampled} 个滚动位置，判据太薄`)).toBeGreaterThanOrEqual(5);
    expect(o.nodesProbed, node("N4", `全程只量了 ${o.nodesProbed} 次中心命中，判据太薄`)).toBeGreaterThanOrEqual(20);
    expect(o.disjoint, node("N4", `条 ${o.barTop}..${o.barBottom} 与滚动容器底边 ${o.scrollBottom} 相交，还在盖内容`)).toBe(true);
    expect(o.hitCount, node("N4", `${o.hitCount} 枚内容节点的中心在某个滚动位置命到条里：${JSON.stringify(o.hits.slice(0, 6))}`)).toBe(0);
  });

  test("N5 条不撞舞台球（我删了那条 bottom，得自己站住）", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    const g = await page.evaluate(
      ([ct]) => {
        const cta = document.querySelector(ct) as HTMLElement;
        const ball = document.querySelector(".cn-sim-float");
        const b = cta.getBoundingClientRect();
        if (!ball) return { ballPresent: false, cta: [Math.round(b.top), Math.round(b.bottom)] };
        const f = ball.getBoundingClientRect();
        return {
          viewport: { innerWidth: window.innerWidth, innerHeight: window.innerHeight },
          ballPresent: true,
          cta: [Math.round(b.top), Math.round(b.bottom)],
          ball: [Math.round(f.top), Math.round(f.bottom)],
          clearanceVar: getComputedStyle(document.documentElement).getPropertyValue("--cn-sim-float-clearance").trim(),
          intersects: b.top < f.bottom && f.top < b.bottom,
        };
      },
      [CTA] as const,
    );
    writeArm("N5-bar-vs-stage-ball-mobile", g);
    expect(g.ballPresent, node("N5", "舞台上没有 .cn-sim-float，这一枚量不到东西")).toBe(true);
    expect(g.intersects, node("N5", `条 ${JSON.stringify(g.cta)} 与球 ${JSON.stringify(g.ball)} 相交（clearance=${g.clearanceVar}）`)).toBe(false);
  });

  test("N6 臂甲：只把面板退回「窗口滚动 + sticky 条」（padding 一律不加回），N4 必须当场红", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    await page.getByTestId("trial-city").fill("天津");
    await page.getByTestId("trial-show-advanced").click();
    const before = await overlaps(page);
    await page.addStyleTag({
      content:
        ".cn-onboarding { display: block !important; height: auto !important; padding-top: 32px !important; }"
        + ".cn-onboarding__scroll { overflow: visible !important; }"
        + ".trial-sticky-cta { position: sticky !important; bottom: 0 !important; z-index: 5 !important;"
        + " background: linear-gradient(to top, var(--v8-canvas) 70%, transparent) !important; }",
    });
    await page.waitForTimeout(300);
    const after = await overlaps(page);
    const landed = await page.evaluate(
      ([ct, sc]) => {
        const pane = document.querySelector(ct) as HTMLElement | null;
        const scrollEl = document.querySelector(sc) as HTMLElement | null;
        return {
          ctaPosition: pane ? getComputedStyle(pane).position : "NO-CTA",
          sectionDisplay: getComputedStyle(document.querySelector(".cn-onboarding") as HTMLElement).display,
          scrollOverflow: scrollEl ? getComputedStyle(scrollEl).overflowY : "NO-PANE",
          documentScrollable:
            Math.round((document.scrollingElement as HTMLElement).scrollHeight - window.innerHeight) > 0,
        };
      },
      [CTA, SCROLL] as const,
    );
    writeArm("N6-arm-sticky-overlay", {
      landing_witness: landed,
      candidate_unmodified: before,
      retrofitted_to_sticky_overlay_without_any_reserve: after,
    });
    expect(landed.ctaPosition, node("N6", `臂没落地：条仍是 ${landed.ctaPosition}`)).toBe("sticky");
    expect(landed.sectionDisplay, node("N6", `臂没落地：section 仍是 ${landed.sectionDisplay}`)).toBe("block");
    expect(landed.scrollOverflow, node("N6", `臂没落地：滚动容器仍是 ${landed.scrollOverflow}`)).toBe("visible");
    expect(before.hitCount, node("N6", `候选树上就已经有 ${before.hitCount} 处重叠，正控失效`)).toBe(0);
    expect(
      after.disjoint === false || after.hitCount > 0,
      node(
        "N6",
        `退回 sticky 覆盖层（且没加任何留白）之后判据仍判绿（disjoint=${after.disjoint} hitCount=${after.hitCount}）`
        + " —— 那 N4 量的就不是这条机制",
      ),
    ).toBe(true);
  });

  test("N7 臂乙：只把那条 padding 装回来，带高必须回到一整块", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    const fixed = await band(page);
    /* 212px is the *effective* value of the deleted declaration: the anatomy receipt derived it from
       `calc(76px + var(--cn-sim-float-clearance))` with that variable computing to 136px on :root,
       and proved the equality by zeroing the declaration on the tip tree.  The arm writes the
       literal, so a var()/env() parse cannot quietly turn the mutation into a no-op -- and the
       landing witness below is what proves it landed at all. */
    await page.addStyleTag({
      content: "@media (max-width: 680px) { .cn-onboarding .trial-steps { padding-bottom: 212px !important; } }",
    });
    await page.waitForTimeout(300);
    const retro = await band(page);
    writeArm("N7-arm-old-padding-back", {
      candidate_unmodified: fixed,
      with_that_padding_back: retro,
      landing_witness: {
        stepsPaddingBottom: retro.computed?.stepsPaddingBottom,
        viewport: retro.viewport,
        media: await measuredViewport(page),
      },
      injected_css: "212px literal = effective value of the deleted declaration",
    });
    expect(fixed.band.height <= BAND_BOUND, node("N7", `候选树上带高已是 ${fixed.band.height}px，与 N1 不是同一件事`)).toBe(true);
    expect(
      retro.computed.stepsPaddingBottom,
      node("N7", `臂没有落地：注入后 computed padding-bottom 仍是 ${retro.computed.stepsPaddingBottom}；`
        + `视口=${JSON.stringify(retro.viewport)} media=${JSON.stringify(await measuredViewport(page))}`),
    ).toBe("212px");
    expect(
      retro.band.height > 200,
      node("N7", `装回那条 padding 之后带高只有 ${retro.band.height}px —— N1 量的就不是它`),
    ).toBe(true);
  });
});

test.describe("desktop 1280x800", () => {
  /* The deleted declaration had a desktop branch too (`padding-bottom: calc(76px + env(...))`, the
     one that is not inside the ≤680px media query), so the same three promises get read here rather
     than assumed.  No touch emulation: this is the medium the desktop branch ships in. */
  test.use({ viewport: { width: 1280, height: 800 } });

  test("D1 带上界（桌面档：那条声明的 76px 分支也归本单删）", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    const g = await band(page);
    writeArm("D1-band-bound-desktop", { ...g, media: await measuredViewport(page) });
    expect(g.missing, node("D1", `向导没挂全：${g.missing}`)).toBeUndefined();
    expect(g.viewport.innerWidth >= 961, node("D1", `量到的视口只有 ${g.viewport.innerWidth}px，不是桌面档`)).toBe(true);
    expect(g.label, node("D1", `带下方不是「目的地」而是「${g.label}」`)).toContain("目的地");
    expect(
      g.band.height <= BAND_BOUND,
      node("D1", `桌面带高 ${g.band.height}px 超过上界 ${BAND_BOUND}px；computed=${JSON.stringify(g.computed)}`),
    ).toBe(true);
  });

  test("D2 桌面：条不盖内容", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    await fillAndExpand(page);
    const o = await overlaps(page);
    await page.screenshot({
      path: evidencePath(`ord0338-jia-D2-desktop-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
    });
    writeArm("D2-bar-covers-nothing-desktop", { media: await measuredViewport(page), ...o });
    expect(o.missing, node("D2", o.missing)).toBeUndefined();
    expect(o.disjoint, node("D2", `桌面：条 ${o.barTop}..${o.barBottom} 与滚动容器底边 ${o.scrollBottom} 相交`)).toBe(true);
    expect(o.hitCount, node("D2", `桌面：${o.hitCount} 枚节点中心命到条里：${JSON.stringify(o.hits.slice(0, 6))}`)).toBe(0);
    expect(o.nodesProbed, node("D2", `桌面：全程只量了 ${o.nodesProbed} 次命中，判据太薄`)).toBeGreaterThanOrEqual(10);
  });

  test("D3 桌面：条不撞舞台球", async ({ page }) => {
    test.setTimeout(300_000);
    await openWizard(page);
    const g = await page.evaluate(
      ([ct]) => {
        const cta = document.querySelector(ct) as HTMLElement;
        const ball = document.querySelector(".cn-sim-float");
        const b = cta.getBoundingClientRect();
        if (!ball) return { ballPresent: false, cta: [Math.round(b.top), Math.round(b.bottom)] };
        const f = ball.getBoundingClientRect();
        return {
          viewport: { innerWidth: window.innerWidth, innerHeight: window.innerHeight },
          ballPresent: true,
          cta: [Math.round(b.top), Math.round(b.bottom)],
          ball: [Math.round(f.top), Math.round(f.bottom)],
          clearanceVar: getComputedStyle(document.documentElement).getPropertyValue("--cn-sim-float-clearance").trim(),
          intersects: b.top < f.bottom && f.top < b.bottom,
        };
      },
      [CTA] as const,
    );
    writeArm("D3-bar-vs-stage-ball-desktop", g);
    expect(g.ballPresent, node("D3", "桌面档没有 .cn-sim-float，量不到东西")).toBe(true);
    expect(g.intersects, node("D3", `桌面：条 ${JSON.stringify(g.cta)} 与球 ${JSON.stringify(g.ball)} 相交（clearance=${g.clearanceVar}）`)).toBe(false);
  });
});
