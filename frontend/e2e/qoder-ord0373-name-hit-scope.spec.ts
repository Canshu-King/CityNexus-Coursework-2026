/**
 * ORD-0373 -- a name that resolves is not a name you can strike.
 *
 * P1  the viewport sweep this order was decided from, kept as a guard: the desktop sidebar is visible
 *     and strikable above 680px and `display:none` at or below it, and at the narrow end the accessible
 *     name query already returns exactly one visible node while the text query does not.
 * N1  on the real screen at 390x844: a naive `.first()` on the text query aims at a node that answers
 *     no strike, while the shared lever aims at the one that does. Both halves are asserted, so the
 *     lever cannot pass by quietly matching what the naive query already matched.
 * N2  a synthetic fixture, so the filter is shown to be the thing doing the work rather than the page
 *     happening to cooperate: one hidden twin and one visible node under the same name.
 *
 * The lever lives in `e2e/_qoderNamedHits.ts` so the next lane gets it without re-deriving it.
 * Counter-example: `review/n3_0373_mutations_r1.py` removes the filter from `strikeableOnly` and
 * requires N1 and N2 to go red.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { strikeableByName, strikeableLocator, strikeableOnly, surveyName } from "./_qoderNamedHits";

const FILE = "e2e/qoder-ord0373-name-hit-scope.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
const LABEL = "行程工作台";

type Readings = Record<string, unknown>;
const readings: Readings = {};

test.afterEach(({}, info) => {
  const arm = /^([PN]\d)/.exec(info.title)?.[1] ?? info.title.slice(0, 2);
  const prior: Readings = (readings[arm] ?? {}) as Readings;
  const body: Readings = { ...prior };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 700);
  }
  writeFileSync(
    evidencePath(`ord0373/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

async function boot(page: Page, id: string, w: number, h: number) {
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  await page.setViewportSize({ width: w, height: h });
}

test("P1_probe_name_resolution_per_viewport — 逐视口量：侧栏可见性、按名字查到几个、first 落到谁", async ({ page }) => {
  test.setTimeout(300_000);
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, "the stage named no journey").toBe(true);

  const rows: Record<string, unknown> = {};
  for (const [w, h] of [[390, 844], [680, 900], [681, 900], [960, 900], [1440, 900]] as const) {
    await boot(page, id, w, h);
    await page.goto("/v8/hub", { waitUntil: "load" });
    await expect(page.getByTestId("today-hero"), `Today never rendered at ${w}px`).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(400);
    /* One frame per viewport, because the claim is about what the layout actually does at each width --
       the numbers alone would not show a sidebar that renders but is pushed off screen. */
    await page.screenshot({ path: evidencePath(`ord0373/${RUN}__P1_${w}x${h}.png`) });

    const dom = await page.evaluate((name: string) => {
      const norm = (s: string | null) => (s ?? "").replace(/\s+/g, " ").trim();
      const describe = (el: Element) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const struck = r.width > 0 && r.height > 0 ? document.elementFromPoint(cx, cy) : null;
        let excludedBy: string | null = null;
        for (let cur: Element | null = el; cur; cur = cur.parentElement) {
          const c = getComputedStyle(cur);
          if (c.display === "none") { excludedBy = `display:none@${cur.tagName.toLowerCase()}.${(cur.getAttribute("class") || "").split(" ")[0]}`; break; }
          if (c.visibility === "hidden") { excludedBy = `visibility:hidden@${cur.tagName.toLowerCase()}`; break; }
        }
        return {
          tag: el.tagName.toLowerCase(),
          cls: norm(el.getAttribute("class")).slice(0, 40),
          w: Math.round(r.width),
          h: Math.round(r.height),
          in_viewport: r.width > 0 && r.height > 0 && cy >= 0 && cy <= window.innerHeight,
          excluded_by: excludedBy,
          hit_self: !!struck && (struck === el || el.contains(struck)),
        };
      };
      const all = Array.from(document.querySelectorAll("*")).filter((el) => norm(el.textContent) === name);
      const sidebar = document.querySelector("aside.v8-sidebar");
      const mobileNav = document.querySelector("nav.v8-mobile-nav");
      const burger = document.querySelector(".v8-mobile-menu");
      const box = sidebar?.getBoundingClientRect();
      return {
        dom_matches: all.map(describe),
        dom_match_count: all.length,
        sidebar: sidebar
          ? { display: getComputedStyle(sidebar).display, w: Math.round(box!.width), h: Math.round(box!.height) }
          : null,
        mobile_nav: mobileNav ? { display: getComputedStyle(mobileNav).display, buttons: mobileNav.querySelectorAll("button").length } : null,
        burger: burger ? { display: getComputedStyle(burger).display, w: Math.round(burger.getBoundingClientRect().width) } : null,
      };
    }, LABEL);

    /* What the two Playwright query families actually return -- the point of the whole probe: a role
       query already respects the accessibility tree, a text query does not. */
    const byRoleCount = await page.getByRole("button", { name: LABEL }).count();
    const byTextCount = await page.getByText(LABEL, { exact: true }).count();
    let firstTextVisible: boolean | null = null;
    if (byTextCount > 0) {
      firstTextVisible = await page.getByText(LABEL, { exact: true }).first().isVisible().catch(() => false);
    }
    rows[`${w}x${h}`] = { ...dom, by_role_count: byRoleCount, by_text_count: byTextCount, first_text_visible: firstTextVisible };
  }

  readings.P1 = { label: LABEL, route: "/v8/hub", rows };
  /* The sweep is asserted, not just recorded, so the decision this order rests on cannot rot silently:
     the narrow end hides the desktop sidebar and the accessible-name query is already right there,
     while the wide end really does use it. */
  for (const [key, raw] of Object.entries(rows)) {
    const r = raw as {
      sidebar: { display: string; w: number; h: number } | null;
      by_role_count: number;
      by_text_count: number;
      first_text_visible: boolean | null;
      dom_matches: Array<{ w: number; h: number; hit_self: boolean; excluded_by: string | null }>;
    };
    const narrow = parseInt(key.split("x")[0], 10) <= 680;
    const strikeable = strikeableOnly(r.dom_matches as never).length;
    if (narrow) {
      expect(r.sidebar?.display, node("P1", `${key}: the desktop sidebar is rendering at a width the layout says is mobile`)).toBe("none");
      expect(strikeable, node("P1", `${key}: 「${LABEL}」 became strikable at a narrow viewport, which contradicts the measurement this order was decided from`)).toBe(0);
      expect(r.by_role_count, node("P1", `${key}: the accessible-name query no longer returns exactly the one visible entry, so the a11y tree changed`)).toBe(1);
      expect(r.first_text_visible, node("P1", `${key}: the text query's first node became visible -- if this is red because someone removed the hidden twin, that is a fix, and this cell should be re-read not restored`)).toBe(false);
    } else {
      expect(r.sidebar?.display, node("P1", `${key}: the desktop sidebar stopped rendering at a desktop width`)).toBe("flex");
      expect(strikeable, node("P1", `${key}: the desktop nav entry is no longer strikable at a desktop width, so option (a) would not have been safe`)).toBeGreaterThan(0);
    }
  }
});

/* ------------------------------------------------------------------ N1: the trap, on the real screen */

test("N1_at_phone_width_the_text_query_aims_at_a_node_nothing_can_hit — 390 下朴素按名字查会打到隐藏孪生，杠杆不会", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "N1";
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(arm, "Today never rendered")).toBeVisible({ timeout: 30_000 });

  const hits = await surveyName(page, LABEL);
  const kept = strikeableOnly(hits);
  const naive = page.getByText(LABEL, { exact: true }).first();
  const naiveVisible = await naive.isVisible().catch(() => false);
  const naiveBox = await naive.boundingBox().catch(() => null);
  const roleCount = await page.getByRole("button", { name: LABEL }).count();
  const roleVisible = await page.getByRole("button", { name: LABEL }).first().isVisible().catch(() => false);
  /* The lever must refuse rather than hand back something unstriking. */
  let leverThrew = false;
  try {
    await strikeableByName(page, LABEL);
  } catch {
    leverThrew = true;
  }
  readings[arm] = {
    viewport: "390x844",
    label: LABEL,
    survey_count: hits.length,
    strikeable_count: kept.length,
    excluded_by: hits.map((h) => h.excluded_by),
    naive_first_visible: naiveVisible,
    naive_first_box: naiveBox,
    role_count: roleCount,
    role_first_visible: roleVisible,
    lever_refused_unstriking: leverThrew,
  };
  expect(hits.length, node(arm, `no node carries 「${LABEL}」 at all, so this cell has nothing to judge`)).toBeGreaterThan(0);
  expect(naiveVisible, node(arm, `the naive text query no longer lands on a hidden node (box ${JSON.stringify(naiveBox)}); the trap this lever exists for may be gone -- re-read P1 before restoring this assertion`)).toBe(false);
  expect(kept.length, node(arm, `the survey says ${kept.length} strikable nodes for 「${LABEL}」 at phone width, which contradicts the recorded sweep`)).toBe(0);
  /* The *reason* is pinned, not just the outcome: size and strike already drop these nodes, so an
     assertion that only checks the outcome stays green even when the ancestor walk is deleted -- which
     is what mutation M2 measured. Naming the excluding ancestor is what makes that walk load-bearing. */
  expect(
    Array.from(new Set(hits.map((h) => h.excluded_by))),
    node(arm, `these nodes are unstriking, but not because an ancestor hides them -- the survey read ${JSON.stringify(hits.map((h) => h.excluded_by))}`),
  ).toEqual(["display:none@aside.v8-sidebar"]);
  expect(roleCount, node(arm, "the accessible-name query no longer returns exactly one entry, so the scoping advice this cell encodes is out of date")).toBe(1);
  expect(roleVisible, node(arm, "the one node the accessible-name query returns is not visible either, so Today has no working entry under this name")).toBe(true);
  expect(leverThrew, node(arm, "the lever returned a node instead of refusing -- it is no longer filtering, and every caller would click blind")).toBe(true);
});

/* ------------------------------------------------------------------ N2: the filter, on a fixture */

test("N2_the_filter_is_what_does_the_work_not_the_page — 合成夹具：一隐一现同名两枚，筛的必须是可命中那枚", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "N2";
  const TWIN = "ORD0373 同名孪生";
  await page.setContent(`
    <body>
      <aside style="display:none"><button id="hidden">${TWIN}</button></aside>
      <main><button id="shown" onclick="window.__struck='yes'">${TWIN}</button></main>
    </body>`);
  const hits = await surveyName(page, TWIN);
  const kept = strikeableOnly(hits);
  const naiveVisible = await page.getByText(TWIN, { exact: true }).first().isVisible().catch(() => false);
  const target = await strikeableLocator(page, TWIN);
  const targetId = await target.evaluate((el: Element) => el.id);
  await target.click();
  const struck = await page.evaluate(() => (window as unknown as Record<string, unknown>).__struck ?? "no");
  readings[arm] = {
    fixture: "one hidden twin and one visible node, same exact text",
    survey_count: hits.length,
    strikeable_count: kept.length,
    excluded_by: hits.map((h) => h.excluded_by),
    naive_first_visible: naiveVisible,
    lever_resolved_to: targetId,
    click_reached_handler: struck,
  };
  /* Four, not two: each wrapper's own text is also exactly the name, which is a second reason a text
     query under this label is ambiguous -- the hidden `<aside>` and the visible `<main>` both answer it
     alongside their buttons. Measured, not assumed: the first pass of this cell expected 2 and was red. */
  expect(hits.length, node(arm, `the fixture resolved to ${hits.length} nodes, not the 4 the survey measures`)).toBe(4);
  expect(naiveVisible, node(arm, "the naive first() now resolves to a visible node, so the fixture no longer reproduces the trap and the comparison below is vacuous")).toBe(false);
  expect(kept.length, node(arm, `the filter kept ${kept.length} of ${hits.length} nodes`)).toBe(2);
  expect(kept.every((k) => k.excluded_by === null && k.hit_self), node(arm, `something unstriking survived the filter: ${JSON.stringify(kept)}`)).toBe(true);
  expect(kept.some((k) => k.tag === "aside"), node(arm, "the hidden subtree's wrapper survived the filter, so the exclusion walk is not running")).toBe(false);
  expect(targetId, node(arm, `the lever aimed at #${targetId} instead of the visible button`)).toBe("shown");
  expect(struck, node(arm, "the click the lever aimed did not reach the visible node's handler")).toBe("yes");
});
