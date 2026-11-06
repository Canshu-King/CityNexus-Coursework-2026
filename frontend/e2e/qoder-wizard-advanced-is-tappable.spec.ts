/**
 * ORD-0261 + ORD-0264 -- wizard "高级设置" tappability, re-derived on ORD-0338 (甲).
 *
 * 原契约（仲裁者 17:19）: SCROLL_REACHABLE_AND_TAPPABLE，判据是"滚轮滚得动 + 中心命到自己"。
 * ORD-0338 甲 之后判据要重写，因为那条 `scrolled > 0` 代理在新布局下必死：`.cn-onboarding`
 * 现在是两行网格（`.cn-onboarding__scroll` 是唯一的滚动容器，条是第二行），窗口 `scrollY` 恒 0，
 * 于是"滚了多少"与"点不点得到"两件事彻底脱钩 —— 继续拿它当可达性证据，会把一棵真能点的树判红、
 * 把一棵滚不动的树判绿。号令者 Q73 原话：
 *   承诺是"旅客能点到那一步"，因此"静息已可见"与"滚一轮后可见"两种形状都必须判 GREEN，
 *   `scrolled > 0` 不得再当作可达性的代理。
 *
 * 重推后的判据（三枚节点共用同一个量具 `shapes()`）：
 *   判 GREEN 的充要条件 = 在该控件中心落在滚动口内的**每一种形状**里，中心都命到它自己；
 *   并且至少一种形状里它落在滚动口内（否则"永远看不见"又叫可达）；
 *   滚轮位移只作为"手势确实移动了这棵树"的前件读数，永不单独判可达。
 * 形状取三种：静息（scrollTop=0）、滚一轮（一次真滚轮 240px）、滚到底。
 *
 * 五格读数保持：1 locality（DOM 归属，按容器不按类名）、2 人路径（真滚轮，不用 scrollIntoView）、
 * 3 语义（草稿活过滚动与展开、点得开）、4 臂（见第 3 枚节点）、5 正控（「看支持的目的地」）。
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-wizard-advanced-is-tappable.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
const SCROLL = '[data-testid="trial-scroll"]';
const CTA = '[data-testid="trial-sticky-cta"]';
test.use({ viewport: VIEWPORT, hasTouch: true });

/** A typed word that must survive scrolling and expanding (ruling cell 3). */
const DRAFT_WORD = "外滩";

async function openWizard(page: Page) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
    localStorage.removeItem("citynexus.v8.journey_id");
  }, journeyId);
  await page.goto("/v8", { waitUntil: "load" });
  await page.waitForTimeout(1_000);
}

async function typeDraft(page: Page) {
  const city = page.getByTestId("trial-city");
  await expect(city).toBeVisible({ timeout: 15_000 });
  await city.fill(DRAFT_WORD);
  expect(await city.inputValue(), node("draft", "the fixture word did not land in the field")).toBe(DRAFT_WORD);
}

async function probe(page: Page, selector: string, shape: string) {
  return page.evaluate(
    ([sel, scrollSel, ctaSel, shapeName]) => {
      const el = document.querySelector(sel);
      const scroller = document.querySelector(scrollSel) as HTMLElement;
      const cta = document.querySelector(ctaSel) as HTMLElement;
      if (!el || !scroller || !cta) return { shape: shapeName, present: false } as never;
      const box = el.getBoundingClientRect();
      const cx = Math.round(box.left + box.width / 2);
      const cy = Math.round(box.top + box.height / 2);
      const bar = cta.getBoundingClientRect();
      const sr = scroller.getBoundingClientRect();
      const landed = document.elementFromPoint(cx, cy);
      /* "On screen" means inside the scroll container's own box.  Content the container clipped
         away is not covered by chrome -- measured on this very candidate, the 高级设置 summary sits
         below the clip edge at rest and elementFromPoint at its centre returns the CTA's 下一步
         button, which an unfiltered viewport test reads as "chrome covers the control". */
      const inScrollport =
        box.height > 0 &&
        cy >= Math.max(1, sr.top + 1) &&
        cy <= Math.min(window.innerHeight - 1, sr.bottom - 1);
      return {
        shape: shapeName,
        present: true,
        box: [Math.round(box.width), Math.round(box.height)],
        centre: [cx, cy],
        paneScrollTop: Math.round(scroller.scrollTop),
        windowScrollY: Math.round(window.scrollY),
        inScrollport,
        insideCtaBand: cy >= bar.top && cy <= bar.bottom,
        hitTag: landed ? landed.tagName.toLowerCase() : "NONE",
        hitText: landed ? String(landed.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 20) : "",
        hitsItself: !!landed && (landed === el || el.contains(landed)),
        hitsCta: !!landed && cta.contains(landed),
      };
    },
    [selector, SCROLL, CTA, shape] as const,
  );
}

/**
 * The three shapes Q73 names. `rolledBy` is a real mouse wheel gesture from the middle of the
 * scroll container (cell 2's human path) -- the pane is what moves, and the displacement is
 * recorded as a precondition reading only, never as the reachability verdict.
 */
async function shapes(page: Page, selector: string) {
  await page.evaluate((s) => {
    (document.querySelector(s) as HTMLElement).scrollTop = 0;
  }, SCROLL);
  await page.waitForFunction(() => {
    const host = document.querySelector(".cn-onboarding");
    if (!host) return false;
    const running = (host as HTMLElement).getAnimations({ subtree: false });
    return running.length === 0 || running.every((a) => a.playState === "finished");
  }, { timeout: 5_000 }).catch(() => undefined);
  const atRest: any = await probe(page, selector, "at-rest");
  /* The cursor has to be over the scroll container: the section's own top band is padding, and a
     wheel there scrolls nothing -- measured, the first run rolled 0px from (0,0). */
  await page.evaluate((s) => {
    const r = (document.querySelector(s) as HTMLElement).getBoundingClientRect();
    return [Math.round(r.left + r.width / 2), Math.round(r.top + Math.min(r.height / 2, 300))];
  }, SCROLL).then(([x, y]) => page.mouse.move(x, y));
  let rolledBy = 0;
  for (let i = 0; i < 30; i += 1) {
    await page.mouse.wheel(0, 240);
    await page.waitForTimeout(70);
    rolledBy = await page.evaluate((s) => Math.round((document.querySelector(s) as HTMLElement).scrollTop), SCROLL);
    if (rolledBy > 0) break;
  }
  const afterOneRoll: any = await probe(page, selector, "after-one-roll");
  const atEnd: any = await page
    .evaluate(([s]) => {
      const sc = document.querySelector(s) as HTMLElement;
      sc.scrollTop = sc.scrollHeight;
      return true;
    }, [SCROLL] as const)
    .then(async () => probe(page, selector, "at-far-end"));
  return { atRest, afterOneRoll, atEnd, rolledBy };
}

/** The verdict Q73 asks for: on-screen in at least one shape, and never covered in any shape. */
function verdict(g: { atRest: any; afterOneRoll: any; atEnd: any }) {
  const seen = [g.atRest, g.afterOneRoll, g.atEnd].filter((s) => s.present && s.inScrollport);
  return {
    shapesSeen: seen.length,
    covered: seen.filter((s) => !s.hitsItself).map((s) => ({
      shape: s.shape,
      centre: s.centre,
      landed: `<${s.hitTag}> "${s.hitText}"`,
      insideCtaBand: s.insideCtaBand,
    })),
  };
}

async function expectReachable(id: string, g: any, label: string) {
  const v = verdict(g);
  expect(v.shapesSeen > 0, node(id, `${label} 在三种形状里一次都没有落进滚动口：${JSON.stringify([g.atRest, g.afterOneRoll, g.atEnd])}`)).toBe(true);
  expect(
    v.covered.length,
    node(
      id,
      `${label} 落在滚动口内的形状里有 ${v.covered.length} 种命不到自己（被 chrome 盖住）：${JSON.stringify(v.covered)}`,
    ),
  ).toBe(0);
  return v;
}

function writeEvidence(name: string, payload: Record<string, unknown>) {
  writeFileSync(
    evidencePath(name),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0261 + ORD-0264 (re-derived under ORD-0338 甲 / Q73)",
        file: FILE,
        run: process.env.CITYNEXUS_E2E_RUN_ID ?? "dev",
        tree_sha: process.env.CNX_TREE_SHA ?? "CNX_TREE_SHA-unset",
        viewport: VIEWPORT,
        ...payload,
      },
      null,
      1,
    ),
  );
}

test("ord0261-sticky-bar-must-not-cover-the-summary-row — 条在任何形状下都不许盖住 summary 行", async ({ page }) => {
  test.setTimeout(240_000);
  await openWizard(page);
  await typeDraft(page);

  const summary = await shapes(page, ".trial-advanced > summary");
  const v = await expectReachable("ord0261", summary, "「高级设置」的 summary 行");
  // Positive control (ruling cell 5): a genuinely free control must still pass, so the gate cannot
  // drift into "every disclosure must be big and centred".
  const control = await shapes(page, ".trial-supported > summary");
  const cv = await expectReachable("ord0261", control, "正控「看支持的目的地」");
  // The gesture really moved this tree -- a reading, and the precondition of the shapes above.
  expect(summary.rolledBy, node("ord0261", "滚轮没让面板动过，三种形状其实是同一种")).toBeGreaterThan(0);
  expect(
    summary.atRest.windowScrollY,
    node("ord0261", `窗口仍在滚（scrollY=${summary.atRest.windowScrollY}）—— 滚动容器已经不是 .cn-onboarding__scroll`),
  ).toBe(0);
  writeEvidence("ord0261-shapes-reading.json", {
    arm: "unmodified-candidate",
    summary: { shapes: [summary.atRest, summary.afterOneRoll, summary.atEnd], rolledBy: summary.rolledBy, verdict: v },
    control: { shapes: [control.atRest, control.afterOneRoll, control.atEnd], verdict: cv },
  });
});

test("ord0264-opener-is-scroll-reachable-and-tappable — 高级入口在任何形状下都点得动，且不伤草稿", async ({ page }) => {
  test.setTimeout(300_000);
  await openWizard(page);
  await typeDraft(page);

  // Cell 1, locality: unchanged by ORD-0338 -- the opener must live inside the step-1 card it opens.
  const locality = await page.evaluate(() => {
    const opener = document.querySelector('[data-testid="trial-show-advanced"]');
    const stepCard = document.querySelector('[data-testid="trial-step1-body"], .trial-advanced')?.closest(
      '[data-testid="trial-step1-body"]',
    );
    const details = document.querySelector(".trial-advanced");
    return {
      openerPresent: !!opener,
      insideStep1Card: !!opener && !!stepCard && stepCard.contains(opener),
      adjacentToItsDetails:
        !!opener &&
        !!details &&
        (details.contains(opener) ||
          details.nextElementSibling === opener ||
          opener.nextElementSibling === details ||
          details.parentElement === opener.parentElement),
    };
  });
  expect(locality.openerPresent, node("ord0264", "the opener button is not rendered at all")).toBe(true);
  expect(locality.insideStep1Card, node("ord0264", `入口不在它所属的 step-1 卡片里：${JSON.stringify(locality)}`)).toBe(true);
  expect(locality.adjacentToItsDetails, node("ord0264", `入口不挨着它要展开的 details：${JSON.stringify(locality)}`)).toBe(true);

  // Cells 2+3, re-derived: reachable == tappable in every shape it is on screen, not "it scrolled".
  const g = await shapes(page, '[data-testid="trial-show-advanced"]');
  const v = await expectReachable("ord0264", g, "高级入口");

  // The promise in one user-visible act: from wherever the pane happens to be, the tap opens it.
  await page.getByTestId("trial-show-advanced").click();
  await expect(page.locator(".trial-advanced"), node("ord0264", "点过之后 details 没有 open")).toHaveAttribute("open", "");
  expect(
    await page.getByTestId("trial-city").inputValue(),
    node("ord0264", "the typed draft did not survive the scroll and the click"),
  ).toBe(DRAFT_WORD);

  writeEvidence("ord0264-shapes-reading.json", {
    arm: "unmodified-candidate",
    locality,
    shapes: [g.atRest, g.afterOneRoll, g.atEnd],
    rolledBy: g.rolledBy,
    verdict: v,
    draftWord: DRAFT_WORD,
  });
  await page.screenshot({
    path: evidencePath(`ui-overnight/ord0264-reachable-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
  });
});

test("ord0264-arm-chrome-overlay-must-turn-the-verdict-red — 臂：把条退回覆盖层，判据必须当场红", async ({ page }) => {
  test.setTimeout(300_000);
  await openWizard(page);
  await typeDraft(page);

  // Control round first: unmodified candidate must be GREEN, or the arm proves nothing.
  const before = await shapes(page, ".trial-advanced > summary");
  const vb = verdict(before);
  expect(vb.covered.length, node("ord0264-arm", `未改动就已有 ${JSON.stringify(vb.covered)} 处被盖，控制轮不绿，整批作废`)).toBe(0);

  // Single variable: the bar becomes an overlay again (no padding is added back anywhere).
  await page.addStyleTag({
    content:
      ".cn-onboarding { display: block !important; height: auto !important; min-height: 100vh !important; padding-bottom: 32px !important; }"
      + ".cn-onboarding__scroll { overflow: visible !important; }"
      + ".trial-sticky-cta { position: sticky !important; bottom: 0 !important; z-index: 5 !important;"
      + " background: linear-gradient(to top, var(--v8-canvas) 70%, transparent) !important; }",
  });
  await page.waitForTimeout(300);
  const after = await shapes(page, ".trial-advanced > summary");
  const va = verdict(after);
  writeEvidence("ord0264-arm-overlay-reading.json", {
    arm: "K_BAR_BACK_TO_OVERLAY",
    css: "sticky bar + block pane, no reserve added back",
    control: { shapes: [before.atRest, before.afterOneRoll, before.atEnd], verdict: vb },
    mutant: { shapes: [after.atRest, after.afterOneRoll, after.atEnd], verdict: va },
  });
  expect(
    va.covered.length > 0 || va.shapesSeen === 0,
    node(
      "ord0264-arm",
      `把条退回覆盖层之后判据仍判绿（${JSON.stringify(va)}）—— 那这三枚形状读数量的就不是这条机制`,
    ),
  ).toBe(true);
});
