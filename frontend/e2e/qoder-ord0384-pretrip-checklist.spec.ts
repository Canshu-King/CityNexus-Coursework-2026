/**
 * ORD-0384 -- the pre-trip checklist may only show what a contract anchors, and must prove the anchor.
 *
 * The order was re-scoped on a measurement: of nine candidate classes, only two name a field that a
 * contract declares on the response the item actually reads. So this file judges the claims that survive
 * the narrowing, plus the one only the narrowing makes possible:
 *
 *   Y1  every item on screen is true in the response, and every anchored response condition that is true
 *       has an item -- co-presence, both directions;
 *   Y2  put the field in the response and its item appears; take it out and the item goes; put it back
 *       and it returns -- completion is a projection of the field, not a checkbox;
 *   Y3  a clean response yields no items, one honest line, and a strikable action, and that line claims
 *       nothing about checks this screen cannot make;
 *   Y4  every item names its endpoint, its field AND its contract anchor on screen, and the anchor is
 *       verified against the real `contracts/openapi.yaml` -- the line it prints has to declare that
 *       field, so a stale or invented line number goes red here instead of shipping;
 *   Y5  no un-anchored class ever reaches the DOM. The list comes from
 *       `review/n3_0384_unanchored_list_r2.py`, which measures both OpenAPI artifacts in the repo, so
 *       this cell re-reads the same measurement the panel was narrowed by;
 *   Y6  the band that can read "一切正常" never renders underneath a list of open items;
 *   Y7  no raw network reading and no smuggled sentence reaches the DOM or an attribute (Q64=①/Q70/Q71).
 *
 * The truth table is written from the payload fields **inside this file**, not from the product module:
 * the panel and the module agree by construction, so comparing them would be self-proof and would stay
 * green through any mutation of the rules.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { evidencePath } from "./_qoderEvidence";
import { ANCHORED_ITEM_KEYS, UNANCHORED_CLASSES, UNANCHORED_ITEM_KEYS } from "./_qoderUnanchoredClasses";

const FILE = "e2e/qoder-ord0384-pretrip-checklist.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
// this project is ESM (`"type": "module"`), so `__dirname` does not exist -- `import.meta.url` is what
// survives the loader, which is the convention the other specs already use.
const HERE = dirname(fileURLToPath(import.meta.url));
const CONTRACT_PATH = resolve(HERE, "..", "..", "contracts", "openapi.yaml");

type Bag = Record<string, any>;

/** The two anchored conditions, read straight off the response. Independent of the product module. */
const TRUTH: Record<string, (exp: Bag) => boolean> = {
  plan_feasibility_open: (e) =>
    Array.isArray(e.experience_flags?.feasibility_failures) && e.experience_flags.feasibility_failures.length > 0,
  recovery_needs_decision: (e) => e.recovery?.requires_user_decision === true,
};

const readings: Record<string, unknown> = {};

function armOf(title: string): string {
  return /^([XY]\d)/.exec(title)?.[1] ?? title.slice(0, 2);
}

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const prior = (readings[arm] ?? {}) as Record<string, unknown>;
  const body: Record<string, unknown> = { ...prior };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 700);
  }
  writeFileSync(
    evidencePath(`ord0384/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

async function stageJourneyId(page: Page): Promise<string> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("boot", "the stage named no journey, so there is nothing to check against"));
  return id;
}

async function boot(page: Page, id: string, w = 390, h = 844) {
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  page.setDefaultTimeout(30_000);
  await page.setViewportSize({ width: w, height: h });
}

/** What the screen rendered, as keys. */
async function renderedKeys(page: Page): Promise<string[]> {
  return page.$$eval('[data-testid="pretrip-item"]', (els) =>
    els.map((el) => el.getAttribute("data-cnx-key") ?? "?"),
  );
}

/** The same response the app read, read again from the test's own connection. */
async function truthExp(page: Page, id: string): Promise<Bag> {
  return (await (await page.request.get(`/api/v1/journeys/${id}/experience`)).json()) as Bag;
}

const EXPERIENCE_ROUTE = "**/api/v1/journeys/*/experience";

/**
 * Rewrite the experience body on its way back. One leg only, because the panel reads one payload now --
 * the first version patched a local object and then fulfilled the *unpatched* body of the other leg, so
 * every "clean response" cell was silently judging a dirty one.
 */
function patchExp(patch: (exp: Bag) => void) {
  return async (route: Route) => {
    const res = await route.fetch();
    const body = await res.json().catch(() => null);
    if (!body || typeof body !== "object") return route.fallback();
    patch(body);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  };
}

/** Make both anchored conditions false, field by field, touching nothing else. */
const MAKE_CLEAR = (exp: Bag) => {
  exp.experience_flags = { ...(exp.experience_flags ?? {}), feasibility_failures: [] };
  exp.recovery = null;
};
/** Make both true. The values are synthetic and labelled as such, so a reviewer can tell a constructed
 *  condition from a server answer without opening the receipt. */
const MAKE_OPEN = (exp: Bag) => {
  exp.experience_flags = {
    ...(exp.experience_flags ?? {}),
    feasibility_failures: ["ORD0384_SYNTHETIC_FEASIBILITY_FAILURE"],
  };
  exp.recovery = { requires_user_decision: true, state: "ORD0384_SYNTHETIC" };
};

async function settle(page: Page, id: string, route = "/v8/hub") {
  const arrived = page.waitForResponse((r) => r.url().includes(`/journeys/${id}/experience`) && r.status() === 200);
  await page.goto(route, { waitUntil: "load" });
  await arrived;
  await expect(page.getByTestId("pretrip-checklist"), node("boot", `the checklist band is missing on ${route}`))
    .toBeVisible({ timeout: 30_000 });
}

async function expectKeysInclude(page: Page, keys: string[], where: string) {
  await expect
    .poll(() => renderedKeys(page), { message: node(where, `the panel never showed ${JSON.stringify(keys)}`), timeout: 20_000 })
    .toEqual(expect.arrayContaining(keys));
}

async function expectKeysExclude(page: Page, keys: string[], where: string) {
  await expect
    .poll(async () => (await renderedKeys(page)).filter((k) => keys.includes(k)), {
      message: node(where, `the panel never dropped ${JSON.stringify(keys)}`),
      timeout: 20_000,
    })
    .toEqual([]);
}

/* ---------------------------------------------------- Y1: co-presence, both directions, on the stage */

test("Y1_every_item_is_true_and_every_anchored_true_condition_has_an_item — 屏上每条都对得上响应，响应里成立的锚点条件每条都在屏上", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y1";
  const id = await stageJourneyId(page);
  await boot(page, id);
  await settle(page, id);
  const exp = await truthExp(page, id);
  const expected = Object.entries(TRUTH).filter(([, holds]) => holds(exp)).map(([k]) => k);
  /* A response arriving is not a screen re-rendering; settle before judging either direction. */
  await expect
    .poll(() => renderedKeys(page), { message: node(arm, `the panel never settled on ${JSON.stringify(expected)}`), timeout: 20_000 })
    .toEqual(expect.arrayContaining(expected));
  const keys = await renderedKeys(page);
  const missing = expected.filter((k) => !keys.includes(k));
  const invented = keys.filter((k) => !(TRUTH[k]?.(exp) ?? false));
  const offList = keys.filter((k) => !ANCHORED_ITEM_KEYS.includes(k));
  readings[arm] = {
    journey_id: id,
    rendered_keys: keys,
    truth_table_keys: expected,
    rendered_but_false: invented,
    true_but_absent: missing,
    rendered_keys_outside_the_anchored_set: offList,
    anchored_set_size: ANCHORED_ITEM_KEYS.length,
    unanchored_set_size: UNANCHORED_ITEM_KEYS.length,
    feasibility_failures: exp.experience_flags?.feasibility_failures ?? null,
    recovery_requires_user_decision: exp.recovery?.requires_user_decision ?? null,
    /* Say out loud which direction this run actually exercised. On the unpatched stage neither anchored
       condition is true, so "every true condition has an item" compares two empty sets and proves only
       the other direction. The positive direction is carried by Y2, which constructs both fields. */
    negative_direction_exercised: true,
    positive_direction_vacuous: expected.length === 0,
    positive_direction_carried_by: expected.length === 0 ? "Y2" : "Y1",
  };
  await page.getByTestId("pretrip-checklist").scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y1_today.png`) });
  expect(missing, node(arm, `the response states anchored conditions with no item on screen: ${JSON.stringify(missing)} (rendered ${JSON.stringify(keys)})`)).toEqual([]);
  expect(invented, node(arm, `the screen lists items the response does not support: ${JSON.stringify(invented)} (truth table ${JSON.stringify(expected)})`)).toEqual([]);
  expect(offList, node(arm, `items rendered outside the anchored set ${JSON.stringify(ANCHORED_ITEM_KEYS)}: ${JSON.stringify(offList)}`)).toEqual([]);
});

/* ---------------------------------------------- Y2: the field moves and the item follows, both ways */

test("Y2_construct_the_field_the_item_appears_remove_it_the_item_goes — 造出锚点字段条目就来，撤掉就走，再造再来", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y2";
  const id = await stageJourneyId(page);
  const both = ["plan_feasibility_open", "recovery_needs_decision"];
  const rounds: Array<Record<string, unknown>> = [];

  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_OPEN));
  await boot(page, id);
  await settle(page, id);
  await expectKeysInclude(page, both, arm);
  rounds.push({ round: "constructed_open", keys: await renderedKeys(page) });
  await page.getByTestId("pretrip-checklist").scrollIntoViewIfNeeded();
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y2_open.png`) });

  /* Take the same fields back out. Nothing on the panel was clicked -- there is nothing to click. */
  await page.unroute(EXPERIENCE_ROUTE);
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_CLEAR));
  await settle(page, id);
  await expectKeysExclude(page, both, arm);
  rounds.push({ round: "cleared", keys: await renderedKeys(page) });

  /* And back once more, so what is being shown is the field and not the reload. */
  await page.unroute(EXPERIENCE_ROUTE);
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_OPEN));
  await settle(page, id);
  await expectKeysInclude(page, both, arm);
  rounds.push({ round: "constructed_open_again", keys: await renderedKeys(page) });
  await page.unroute(EXPERIENCE_ROUTE);
  const truth = await truthExp(page, id);
  readings[arm] = { journey_id: id, rounds, last_read_feasibility_failures: truth.experience_flags?.feasibility_failures };
  expect(rounds.map((r) => r.round), node(arm, "the three rounds did not all run")).toEqual([
    "constructed_open", "cleared", "constructed_open_again",
  ]);
});

/* ------------------------------------------- Y3: the empty state carries an action and tells no lie */

test("Y3_clean_response_gives_no_items_one_honest_line_and_a_strikable_action — 全干净时：0 条目、一句实话、一个打得开的动作", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y3";
  const id = await stageJourneyId(page);
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_CLEAR));
  await boot(page, id);
  await settle(page, id);
  await expectKeysExclude(page, [...ANCHORED_ITEM_KEYS], arm);
  const empty = page.getByTestId("pretrip-empty");
  await expect(empty, node(arm, "no items rendered and no empty-state block either, so the band is a silent hole")).toBeVisible();
  const line = (await empty.textContent() ?? "").replace(/\s+/g, " ").trim();
  const action = page.getByTestId("pretrip-empty-action");
  /* Bring it into view BEFORE the strike test: an action below the fold answers no `elementFromPoint`,
     and that is a fact about the viewport, not about whether a traveller can reach it. */
  await action.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  const box = await action.boundingBox();
  const struck = box
    ? await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return !!el && !!el.closest('[data-testid="pretrip-empty-action"]');
      }, { x: box.x + box.width / 2, y: box.y + box.height / 2 })
    : false;
  /* What the line must never say: that everything about the trip is fine, or that anything was checked
     and passed. It may only report the two anchored findings. */
  const overreach = ["一切正常", "全部通过", "已确认无误", "所有检查", "都已就绪", "没有问题", "all clear", "everything is fine", "all checks"]
    .filter((s) => line.toLowerCase().includes(s.toLowerCase()));
  readings[arm] = {
    journey_id: id,
    line,
    line_length: line.length,
    action_label: (await action.textContent() ?? "").trim(),
    action_box: box,
    action_struck: struck,
    overreach_phrases_present: overreach,
    names_feasibility_check: line.includes("可行性失败"),
    names_recovery_check: line.includes("恢复"),
  };
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y3_empty.png`) });
  expect(overreach, node(arm, `the empty state claims more than the two anchored checks: ${JSON.stringify(overreach)} in ${JSON.stringify(line)}`)).toEqual([]);
  expect(box, node(arm, "the empty state has no action at all")).not.toBeNull();
  expect(struck, node(arm, "the empty state's action is rendered but cannot be hit at its own centre")).toBe(true);
  expect(Boolean(readings[arm].names_feasibility_check) && Boolean(readings[arm].names_recovery_check),
         node(arm, `the empty line does not name the two checks it actually covers, so "nothing outstanding" reads as a clean bill: ${JSON.stringify(line)}`)).toBe(true);
});

/* -------------------------------------- Y4: the printed anchor has to exist in the real contract */

test("Y4_every_item_prints_endpoint_field_and_anchor_and_the_anchor_resolves — 每条写出端点、字段与锚点，且锚点那一行真的在契约里声明了这个字段", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y4";
  const id = await stageJourneyId(page);
  const contractText = readFileSync(CONTRACT_PATH, "utf-8");
  const contractLines = contractText.split("\n");
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_OPEN));
  await boot(page, id);
  await settle(page, id);
  await expectKeysInclude(page, [...ANCHORED_ITEM_KEYS], arm);
  const items = page.locator('[data-testid="pretrip-item"]');
  const count = await items.count();
  const perItem: Array<Record<string, unknown>> = [];
  for (let i = 0; i < count; i += 1) {
    const el = items.nth(i);
    const key = (await el.getAttribute("data-cnx-key")) ?? "?";
    const endpoint = ((await el.locator('[data-cnx-field="basis_endpoint"]').textContent()) ?? "").trim();
    const field = ((await el.locator('[data-cnx-field="basis_field"]').textContent()) ?? "").trim();
    const anchor = ((await el.locator("[data-cnx-anchor]").first().textContent()) ?? "").trim();
    const m = /^(contracts\/openapi\.yaml):(\d+)$/.exec(anchor);
    let anchorLine: string | null = null;
    let anchorResolves = false;
    if (m) {
      anchorLine = (contractLines[Number(m[2]) - 1] ?? "").trim().slice(0, 140);
      const leaf = field.split(".").pop() ?? field;
      anchorResolves = !!anchorLine && new RegExp(`(^|[{,\\s])${leaf}\\s*:`).test(anchorLine);
    }
    /* Read it where a traveller would: centred, sampled across the line, and whatever is on top has to
       be this panel. The global Copilot dock floats over the lower right of Today. */
    const occ = await page.evaluate((idx) => {
      const it = (document.querySelectorAll('[data-testid="pretrip-item"]') as NodeListOf<HTMLElement>)[idx];
      if (!it) return { error: "item vanished", covered_points: ["unmeasured"], overflowing_lines: ["unmeasured"] };
      const code = it.querySelector('[data-cnx-field="basis_field"]') as HTMLElement | null;
      if (!code) return { error: "no basis element", covered_points: ["unmeasured"], overflowing_lines: ["unmeasured"] };
      code.scrollIntoView({ block: "center" });
      const r = code.getBoundingClientRect();
      const panel = document.querySelector(".cn-pretrip");
      /* `elementsFromPoint` cannot see text that overflows its own box -- the glyphs sit outside the
         element, so nothing is on top of them and the sample comes back clean while the line is clipped.
         Measure the overflow directly, on every prose line in the item. */
      const overflowing: string[] = [];
      for (const sel of [".cn-pretrip-title", ".cn-pretrip-why", ".cn-pretrip-basis", ".cn-pretrip-anchor"]) {
        for (const el of Array.from(it.querySelectorAll(sel))) {
          /* `scrollWidth - clientWidth` is not enough: on an `overflow: visible` block the engine can
             report the two as equal while the glyphs still sit outside the card. Measure the rendered
             text range against the element's own box, and against the card, and take the worst. */
          const range = document.createRange();
          range.selectNodeContents(el);
          const textRight = range.getBoundingClientRect().right;
          const box = el.getBoundingClientRect();
          /* Compare against the *section*, not the item: with `overflow-wrap: normal` the grid item
             widens to fit the token, so text-right equals its own box-right and the comparison is blind.
             The thing a traveller sees escape is the card border, i.e. the section's box. */
          const card = (el.closest(".cn-pretrip") ?? el).getBoundingClientRect();
          const over = Math.max(el.scrollWidth - el.clientWidth, textRight - box.right, textRight - card.right);
          if (over > 1) overflowing.push(`${sel}:+${Math.round(over)}px`);
        }
      }
      const covered: string[] = [];
      for (const f of [0.08, 0.3, 0.5, 0.7, 0.92]) {
        const x = r.left + r.width * f;
        const y = r.top + r.height / 2;
        const top = (document.elementsFromPoint(x, y) as HTMLElement[])[0];
        const mine = !!top && (top === panel || (!!panel && panel.contains(top)));
        if (!mine) covered.push(`${x.toFixed(0)},${y.toFixed(0)}:${top ? top.tagName.toLowerCase() + "." + String(top.className).slice(0, 36) : "none"}`);
      }
      return { rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) },
               covered_points: covered, overflowing_lines: overflowing };
    }, i);
    perItem.push({ key, endpoint, field, anchor, anchor_line_content: anchorLine,
                   anchor_resolves: anchorResolves, occlusion: occ });
  }
  readings[arm] = { journey_id: id, contract_bytes: Buffer.byteLength(contractText, "utf-8"),
                    contract_line_count: contractLines.length, items: perItem };
  expect(perItem.length, node(arm, `only ${String(perItem.length)} items rendered, so the anchor check would be thin (anchored set is ${String(ANCHORED_ITEM_KEYS.length)})`)).toBe(ANCHORED_ITEM_KEYS.length);
  for (const it of perItem) {
    expect(String(it.endpoint), node(arm, `item ${it.key} does not state its endpoint on screen`)).toContain("/api/v1/journeys/");
    expect(it.field, node(arm, `item ${it.key} does not state its field on screen`)).toBeTruthy();
    expect(it.anchor, node(arm, `item ${it.key} prints no resolvable anchor: ${JSON.stringify(it.anchor)}`)).toMatch(/^contracts\/openapi\.yaml:\d+$/);
    expect(it.anchor_resolves, node(arm, `item ${it.key} prints ${it.anchor} but that line does not declare \`${String(it.field).split(".").pop()}\`; the line reads ${JSON.stringify(it.anchor_line_content)}`)).toBe(true);
    expect((it.occlusion as any).covered_points ?? ["unmeasured"], node(arm, `item ${it.key}'s basis line is covered at the reading position by ${JSON.stringify((it.occlusion as any).covered_points)}`)).toEqual([]);
    expect((it.occlusion as any).overflowing_lines ?? ["unmeasured"], node(arm, `item ${it.key} has prose clipped by its own box: ${JSON.stringify((it.occlusion as any).overflowing_lines)}`)).toEqual([]);
  }
  await page.getByTestId("pretrip-checklist").scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y4_anchors.png`) });
});

/* --------------------------------------- Y5: the un-anchored classes must never reach the screen */

test("Y5_no_unanchored_class_ever_reaches_the_dom — 没锚的类一条都不许出现在屏上", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y5";
  const id = await stageJourneyId(page);
  /* The worst case for this cell: a response shaped so that all seven un-anchored rules WOULD have
     fired under the first version. If none of their names appears now, the narrowing holds under the
     very conditions that used to produce them. */
  await page.route(EXPERIENCE_ROUTE, patchExp((exp) => {
    exp.trip = {
      ...(exp.trip ?? {}),
      stay_budget: { declared: null, amount_minor: 0, currency: "CNY" },
      arrival_anchor: {},
      departure_anchor: {},
    };
    exp.selected_option_id = null;
    exp.active_plan = null;
    exp.experience_flags = { ...(exp.experience_flags ?? {}), feasibility_failures: [] };
    exp.recovery = null;
  }));
  await boot(page, id);
  await settle(page, id);
  await page.waitForTimeout(400);
  const dom = await page.evaluate((classes: typeof UNANCHORED_CLASSES) => {
    const body = document.body.innerText;
    const attrs = Array.from(document.querySelectorAll("*")).flatMap((el) =>
      Array.from(el.attributes).map((a) => `${a.name}=${a.value}`),
    );
    const haystack = `${body}\n${attrs.join("\n")}`;
    const hits = classes
      .map((c) => ({ key: c.key, field: c.field,
                     needles_found: [c.key, c.field].filter((needle) => !!needle && haystack.includes(needle)) }))
      .filter((h) => h.needles_found.length > 0);
    return {
      unanchored_hits: hits,
      rendered_keys: Array.from(document.querySelectorAll('[data-testid="pretrip-item"]'))
        .map((el) => el.getAttribute("data-cnx-key") ?? "?"),
    };
  }, UNANCHORED_CLASSES);
  const offList = dom.rendered_keys.filter((k) => !ANCHORED_ITEM_KEYS.includes(k));
  readings[arm] = { journey_id: id, unanchored_keys_total: UNANCHORED_ITEM_KEYS.length,
                    unanchored_classes_total: UNANCHORED_CLASSES.length,
                    rendered_keys: dom.rendered_keys, rendered_keys_outside_the_anchored_set: offList,
                    unanchored_names_found_in_dom: dom.unanchored_hits };
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y5_narrowed.png`) });
  expect(dom.unanchored_hits, node(arm, `un-anchored classes still reach the DOM: ${JSON.stringify(dom.unanchored_hits)}`)).toEqual([]);
  expect(offList, node(arm, `the panel rendered keys outside the anchored set: ${JSON.stringify(offList)}`)).toEqual([]);
});

/* ---------------------------------- Y6: the all-clear line may not deny the list above it */

test("Y6_the_all_clear_line_never_sits_under_an_open_list — 「一切正常」不能印在有待办的清单下面", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y6";
  const id = await stageJourneyId(page);
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_OPEN));
  await boot(page, id);
  await settle(page, id);
  await expectKeysInclude(page, ["plan_feasibility_open"], arm);
  const geo = await page.evaluate(() => {
    const band = document.querySelector('[data-testid="today-attention-band"]');
    const strong = document.querySelector('[data-testid="today-attention"]');
    const kicker = document.querySelector('.cn-attention-band .cn-attention-head .v8-kicker');
    const panel = document.querySelector('[data-testid="pretrip-checklist"]');
    const rect = (el: Element | null) => {
      if (!el) return null;
      const r = (el as HTMLElement).getBoundingClientRect();
      return { top: Math.round(r.top + window.scrollY), bottom: Math.round(r.bottom + window.scrollY) };
    };
    return {
      band_attention: band?.getAttribute("data-attention") ?? null,
      band_text: strong ? (strong.textContent ?? "").trim() : null,
      band_kicker_visible: !!kicker && (kicker as HTMLElement).offsetParent !== null,
      band_rect: rect(band),
      panel_rect: rect(panel),
      panel_items: document.querySelectorAll('[data-testid="pretrip-item"]').length,
    };
  });
  readings[arm] = { journey_id: id, ...geo };
  expect(geo.band_rect, node(arm, "the attention band is not on screen, so this cell proves nothing")).not.toBeNull();
  expect(geo.panel_rect, node(arm, "the checklist is not on screen, so this cell proves nothing")).not.toBeNull();
  expect(geo.panel_items, node(arm, `the checklist rendered ${String(geo.panel_items)} items, so ordering is untestable here`)).toBeGreaterThan(0);
  /* The premise is a clear band with no subject of its own. If the product starts showing the kicker,
     this cell's strictness is no longer earned and has to be re-read rather than quietly kept. */
  if (geo.band_attention === "clear" && !geo.band_kicker_visible) {
    expect(
      geo.band_rect!.bottom <= geo.panel_rect!.top,
      node(arm, `the bare all-clear "${geo.band_text}" renders at y=${String(geo.band_rect!.top)}-${String(geo.band_rect!.bottom)}, i.e. below the ${String(geo.panel_items)} open items ending at y=${String(geo.panel_rect!.bottom)} -- one screen, two answers`),
    ).toBe(true);
  }
  await page.getByTestId("pretrip-checklist").scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  await page.screenshot({ path: evidencePath(`ord0384/${RUN}__Y6_pair.png`) });
});

/* ------------------------- Y7: no raw network reading, no smuggled sentence (Q64=① / Q70 / Q71) */

test("Y7_no_raw_network_reading_and_no_sentence_in_a_data_hook — 屏上没有网络原文，data-cnx-field 里只有字段名", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "Y7";
  const id = await stageJourneyId(page);
  await page.route(EXPERIENCE_ROUTE, patchExp(MAKE_OPEN));
  await boot(page, id);
  await settle(page, id);
  await expectKeysInclude(page, [...ANCHORED_ITEM_KEYS], arm);
  const dom = await page.evaluate(() => {
    /* Scoped to this panel: a whole-page sweep would let another lane's text decide whether ORD-0384
       passes. The page-wide count is recorded, just not asserted. */
    const panel = document.querySelector(".cn-pretrip") ?? document.body;
    /* the element itself carries a hook too, and querySelectorAll does not return the root */
    const inPanel = (root: Element) => [root, ...Array.from(root.querySelectorAll("*"))];
    const pairs = inPanel(panel).flatMap((el) =>
      Array.from(el.attributes).filter((a) => a.name.startsWith("data-") || a.name.startsWith("aria-")).map((a) => `${el.tagName.toLowerCase()}|${a.name}|${a.value}`),
    );
    const q64 = ["http_status", "content_type", "body_bytes", "response_received", "elapsed_ms", "request_id"];
    return {
      pairs_in_panel: pairs.length,
      pairs_on_page: Array.from(document.querySelectorAll("*")).flatMap((el) =>
        Array.from(el.attributes).filter((a) => a.name.startsWith("data-") || a.name.startsWith("aria-")).map((a) => a.name),
      ).length,
      q64_in_panel: q64.filter((f) => (panel.textContent ?? "").includes(f)),
      q64_on_whole_page: q64.filter((f) => (document.body.textContent ?? "").includes(f)),
      cnx_field_values: pairs.filter((p) => p.includes("|data-cnx-field|")).map((p) => p.split("|")[2]),
    };
  });
  readings[arm] = { journey_id: id, ...dom };
  expect(dom.q64_in_panel, node(arm, `raw network readings inside the panel: ${JSON.stringify(dom.q64_in_panel)}`)).toEqual([]);
  /* Q70: a data-cnx-field hook names a contract field and nothing else -- no sentence, no value. */
  const sentences = dom.cnx_field_values.filter((v) => v.includes("：") || v.includes(" ") || /[，。]/.test(v));
  expect(sentences, node(arm, `data-cnx-field holds more than a field name: ${JSON.stringify(sentences)}`)).toEqual([]);
  expect(dom.cnx_field_values.length, node(arm, `the panel exposes ${String(dom.cnx_field_values.length)} contract hooks, expected at least the three this order names`)).toBeGreaterThanOrEqual(3);
});
