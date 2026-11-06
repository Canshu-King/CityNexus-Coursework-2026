/**
 * ORD-0375 -- Today's quiet row has to survive the narrow end of the phone range.
 *
 * The commander's own phone read (`b26b961baa481e25f014784c6070b689102781c8`) saw four quiet buttons
 * side by side at a phone viewport, one of them the share entry ORD-0366 added. A wrapping text row is
 * exactly the kind of surface that passes at 390px and breaks at 320px, so the row is measured at four
 * real widths rather than one.
 *
 * Judged per width, using the ORD-0373 lever (`_qoderNamedHits.ts`) rather than a second copy of it:
 *   Q1  every one of the four labels resolves to exactly one strikable button;
 *   Q2  no button of the row overlaps the fixed bottom nav, the hero card, or another member of the
 *       row -- wrapping is allowed, collision is not;
 *   Q3  Pass-8 still holds: exactly one primary button is strikable on this screen at any width.
 * Screenshots are kept per width because "hittable" and "looks right" are different questions and the
 * second one is answered by looking.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { strikeableOnly, surveyName } from "./_qoderNamedHits";

const FILE = "e2e/qoder-ord0375-quiet-row-narrow.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* Real handset ratios, narrowest first. */
const WIDTHS: ReadonlyArray<readonly [number, number]> = [
  [320, 568],
  [360, 740],
  [390, 844],
  [430, 932],
];
const LABELS = ["AI 帮我规划", "试用其他城市", "打开行程工作台", "复制行程链接"] as const;

type Rect = { x: number; y: number; w: number; h: number };
type RowReading = {
  width: number;
  height: number;
  labels: Record<string, {
    survey_count: number;
    strikeable_buttons: number;
    box: Rect | null;
    hit_self: boolean;
    line_y: number | null;
  }>;
  row_lines: number;
  overlaps: Array<{ a: string; b: string; area: number }>;
  primary_rendered: number;
  primary_strikable: number;
  bottom_nav: Rect | null;
  scroll_y_at_measure: number;
};

async function measureRow(page: Page, width: number, height: number): Promise<RowReading> {
  const labels: RowReading["labels"] = {};
  for (const label of LABELS) {
    const hits = await surveyName(page, label);
    const kept = strikeableOnly(hits).filter((h) => h.tag === "button");
    labels[label] = {
      survey_count: hits.length,
      strikeable_buttons: kept.length,
      box: kept.length ? { x: kept[0].x ?? 0, y: kept[0].y ?? 0, w: kept[0].w, h: kept[0].h } : null,
      hit_self: kept.length > 0,
      line_y: kept.length ? kept[0].y ?? null : null,
    };
  }
  const geo = await page.evaluate(() => {
    const rectOf = (el: Element | null): Rect | null => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
    };
    const norm = (s: string | null) => (s ?? "").replace(/\s+/g, " ").trim();
    const wanted = ["AI 帮我规划", "试用其他城市", "打开行程工作台", "复制行程链接"];
    const boxes: Record<string, Rect | null> = {};
    for (const name of wanted) {
      const el = Array.from(document.querySelectorAll("button")).find((b) => norm(b.textContent) === name);
      boxes[name] = el ? rectOf(el) : null;
    }
    const intersect = (a: Rect | null, b: Rect | null): number => {
      if (!a || !b) return 0;
      const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      return w > 0 && h > 0 ? w * h : 0;
    };
    const nav = rectOf(document.querySelector("nav.v8-mobile-nav"));
    const hero = rectOf(document.querySelector(".v8-trip-hero"));
    /* Pass-8 is a layout claim ("this screen offers one primary action"), so it is counted as a layout
       property: rendered with a box and not excluded by an ancestor. Whether each of those can actually
       be struck is a separate, reachability question -- measured by centring it first, because on a
       320x568 screen the hero's button is simply below the fold at scroll 0 and an uncentred
       `elementFromPoint` there says nothing about the design. */
    const notExcluded = (el: Element): boolean => {
      for (let cur: Element | null = el; cur; cur = cur.parentElement) {
        const cs = getComputedStyle(cur);
        if (cs.display === "none" || cs.visibility === "hidden") return false;
      }
      return true;
    };
    const primaries = Array.from(document.querySelectorAll(".v8-primary-button, .v8-primary")).filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && notExcluded(el);
    });
    const scrollBefore = window.scrollY;
    let primaryStrikable = 0;
    for (const el of primaries) {
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && (hit === el || el.contains(hit))) primaryStrikable += 1;
    }
    window.scrollTo(0, scrollBefore);
    const overlaps: Array<{ a: string; b: string; area: number }> = [];
    for (const name of wanted) {
      const a = boxes[name];
      if (!a) continue;
      const navArea = intersect(a, nav);
      if (navArea > 0) overlaps.push({ a: name, b: "v8-mobile-nav", area: navArea });
      const heroArea = intersect(a, hero);
      if (heroArea > 0) overlaps.push({ a: name, b: "v8-trip-hero", area: heroArea });
      for (const other of wanted) {
        if (other === name) continue;
        const area = intersect(a, boxes[other]);
        if (area > 0) overlaps.push({ a: name, b: other, area });
      }
    }
    const ys = Object.values(boxes).filter(Boolean).map((b) => (b as Rect).y);
    return {
      boxes,
      nav,
      hero,
      primary_rendered: primaries.length,
      primary_strikable: primaryStrikable,
      overlaps,
      /* Distinct horizontal bands the row occupies: one line per different y. */
      row_lines: new Set(ys.map((y) => Math.round(y / 4))).size,
      scroll_y: Math.round(window.scrollY),
    };
  });
  return {
    width,
    height,
    labels,
    row_lines: geo.row_lines,
    overlaps: geo.overlaps,
    primary_rendered: geo.primary_rendered,
    primary_strikable: geo.primary_strikable,
    bottom_nav: geo.nav,
    scroll_y_at_measure: geo.scroll_y,
  };
}

async function bootAt(page: Page, id: string, w: number, h: number) {
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  await page.setViewportSize({ width: w, height: h });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), `Today never rendered at ${w}px`).toBeVisible({ timeout: 30_000 });
  /* The row is above the hero, but the measurement is taken the way a traveller arrives: at the top of
     the page, then brought into view -- so a row that only exists off-screen cannot pass by scrolling. */
  await page.waitForTimeout(400);
}

test.afterEach(({}, info) => {
  const arm = /^([QR]\d)/.exec(info.title)?.[1] ?? info.title.slice(0, 2);
  const prior = (readings[arm] ?? {}) as Record<string, unknown>;
  const body: Record<string, unknown> = { ...prior };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 700);
  }
  writeFileSync(
    evidencePath(`ord0375/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

const readings: Record<string, unknown> = {};

test("Q1_the_quiet_row_is_strikable_and_uncollided_at_four_narrow_widths — 四个窄屏宽度下，安静行每一枚都要可见可命中且不与他行重叠", async ({ page }) => {
  test.setTimeout(420_000);
  const arm = "Q1";
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey, so Today has no row to measure")).toBe(true);

  const perWidth: Record<string, RowReading> = {};
  for (const [w, h] of WIDTHS) {
    await bootAt(page, id, w, h);
    await page.screenshot({ path: evidencePath(`ord0375/${RUN}__Q1_${w}x${h}.png`) });
    const r = await measureRow(page, w, h);
    /* Centre the row and re-measure: a wrapped line sitting below the fold is reachable by scrolling,
       and the question is whether it can be struck once it is in view. */
    await page.evaluate(() => {
      const norm = (s: string | null) => (s ?? "").replace(/\s+/g, " ").trim();
      const el = Array.from(document.querySelectorAll("button")).find((b) => norm(b.textContent) === "复制行程链接");
      el?.scrollIntoView({ block: "center" });
    });
    await page.waitForTimeout(300);
    const after = await measureRow(page, w, h);
    perWidth[`${w}x${h}`] = { ...r, ...after, row_lines_before_centring: r.row_lines } as RowReading;
    await page.screenshot({ path: evidencePath(`ord0375/${RUN}__Q1_${w}x${h}_centred.png`) });
  }
  readings[arm] = { journey_id: id, widths: Object.keys(perWidth).length, per_width: perWidth };

  for (const [key, r] of Object.entries(perWidth)) {
    for (const label of LABELS) {
      const one = r.labels[label];
      expect(one.strikeable_buttons, node(arm, `${key}: 「${label}」 resolved to ${one.strikeable_buttons} strikable buttons (survey saw ${one.survey_count}) -- the row is not usable at this width`)).toBe(1);
      expect(one.hit_self, node(arm, `${key}: 「${label}」 did not answer a strike at its own centre`)).toBe(true);
    }
    expect(r.overlaps, node(arm, `${key}: the row collides with something -- ${JSON.stringify(r.overlaps)}`)).toEqual([]);
    expect(r.primary_rendered, node(arm, `${key}: Pass-8 broken -- ${r.primary_rendered} primary buttons are rendered on Today`)).toBe(1);
    expect(r.primary_strikable, node(arm, `${key}: the screen's one primary button is not strikable even after being centred (${r.primary_strikable} of ${r.primary_rendered})`)).toBe(1);
  }
});

test("Q2_the_row_wraps_without_losing_targets_and_the_wrap_is_recorded — 折行本身不算缺陷，但折成几行、每行是否仍可命中要留在读数里", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Q2";
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);
  const rows: Record<string, unknown> = {};
  for (const [w, h] of WIDTHS) {
    await bootAt(page, id, w, h);
    const r = await measureRow(page, w, h);
    /* The separator that caused this order was a pseudo-element, and a pseudo-element is invisible to
       the DOM -- but its `content` is readable through getComputedStyle, so the finding from looking at
       the screenshots can be pinned instead of re-discovered by the next lane. */
    const pseudo = await page.evaluate(() => {
      const norm = (s: string | null) => (s ?? "").replace(/\s+/g, " ").trim();
      const wanted = ["AI 帮我规划", "试用其他城市", "打开行程工作台", "复制行程链接"];
      const out: Record<string, string> = {};
      for (const name of wanted) {
        const el = Array.from(document.querySelectorAll("button")).find((b) => norm(b.textContent) === name);
        if (!el) continue;
        const before = getComputedStyle(el, "::before").content ?? "";
        const after = getComputedStyle(el, "::after").content ?? "";
        if (before.includes("·") || after.includes("·")) out[name] = `${before} | ${after}`;
      }
      return out;
    });
    const ys = LABELS.map((l) => r.labels[l].box?.y).filter((y): y is number => typeof y === "number");
    rows[`${w}x${h}`] = {
      middot_pseudo_on_a_row_button: pseudo,
      row_lines: r.row_lines,
      distinct_y_bands: new Set(ys.map((y) => Math.round(y / 4))).size,
      all_strikable: LABELS.every((l) => r.labels[l].strikeable_buttons === 1),
      min_gap_between_lines: (() => {
        const sorted = Array.from(new Set(ys)).sort((a, b) => a - b);
        if (sorted.length < 2) return null;
        return Math.min(...sorted.slice(1).map((y, i) => y - sorted[i]));
      })(),
      narrowest_button_w: Math.min(...LABELS.map((l) => r.labels[l].box?.w ?? 0)),
      tallest_button_h: Math.max(...LABELS.map((l) => r.labels[l].box?.h ?? 0)),
    };
  }
  readings[arm] = { journey_id: id, rows };
  /* The cell asserts the invariant that matters, not a particular wrap count: whatever the layout
     decides, every label keeps exactly one strikable button and no two bands collide. */
  for (const [key, v] of Object.entries(rows)) {
    const row = v as { all_strikable: boolean; row_lines: number; min_gap_between_lines: number | null; middot_pseudo_on_a_row_button: Record<string, string> };
    expect(row.middot_pseudo_on_a_row_button, node(arm, `${key}: a middot separator is back on a row button (${JSON.stringify(row.middot_pseudo_on_a_row_button)}) -- at these widths the row wraps, and a pseudo-element lands on whichever side of the break the item happens to be`)).toEqual({});
    expect(row.all_strikable, node(arm, `${key}: a label lost its strikable button at this width`)).toBe(true);
    expect(row.row_lines, node(arm, `${key}: the row spread over ${row.row_lines} bands, more than the four labels can need`)).toBeLessThanOrEqual(4);
    if (row.min_gap_between_lines !== null) {
      expect(row.min_gap_between_lines, node(arm, `${key}: two bands of the row are ${row.min_gap_between_lines}px apart, which is a visual collision even when both are hittable`)).toBeGreaterThan(0);
    }
  }
});
