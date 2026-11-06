/**
 * ORD-0366 — the share-link action has to be one hit-testable tap away on the phone.
 *
 * ORD-0357 shipped the control inside the journey work bench, and the commander's own phone walk
 * (`live_checks/patrol/copy_link_0357_r4.json`) had to open the bench and switch a tab to reach it,
 * after first landing on a same-named node with no size at all. Two claims follow, and both are
 * measured by hit-testing a real point rather than by asking the DOM whether something exists:
 *
 *   I1  every node on Today carrying this label has a real box AND answers `elementFromPoint` at its
 *       own centre. A zero-size look-alike is the failure this cell exists to catch.
 *   T1/T2  one tap at that centre writes the CURRENT journey's absolute link into the clipboard, read
 *       back through `navigator.clipboard.readText()` at scroll poses 0 / middle / bottom. The journey
 *       id is taken from two independent places in the running app and must agree before the clipboard
 *       is compared to it -- a placeholder uuid passing a tail check is the other failure mode.
 *   B1  the work bench entry still does the same job (this order may not trade Today for the bench).
 *
 * The two mutations are run outside this file by `review/n3_0366_mutations_r1.py`: M1 sets the outcome
 * text without ever calling the clipboard, M2 copies a hard-coded placeholder uuid. Both must turn T1
 * and B1 red; a green run under either mutation means the assertion was vacuous.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0366-share-link-one-tap.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
const LABEL = "复制行程链接";
const PLACEHOLDERISH = /^(0{8}|placeholder|dummy|example|test)[\w-]*$/i;

type Readings = Record<string, unknown>;
const readings: Readings = {};

type Sweep = {
  label: string;
  matches: Array<{
    tag: string;
    cls: string;
    x: number;
    y: number;
    w: number;
    h: number;
    display: string;
    visibility: string;
    hit_self: boolean;
    hit_what: string;
  }>;
  zero_size: number;
  unhit: number;
  viewport: { w: number; h: number };
  scroll_y: number;
  page_height: number;
};

/**
 * The label, not the test id: an entry the traveller can read is what "reachable" is about, and the
 * defect being gated is a node that carries the label but cannot be struck.
 */
async function sweepLabel(page: Page): Promise<Sweep> {
  return page.evaluate((wanted: string) => {
    const nameOf = (el: Element) =>
      (el.getAttribute("aria-label") || (el.textContent ?? "")).trim();
    const hits = Array.from(document.querySelectorAll("*")).filter((el) => nameOf(el) === wanted);
    const matches = hits.map((el) => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const struck = r.width > 0 && r.height > 0 ? document.elementFromPoint(cx, cy) : null;
      const tagOf = (n: Element | null) =>
        n ? `${n.tagName.toLowerCase()}.${(n.getAttribute("class") || "").split(" ")[0]}` : "none";
      return {
        tag: el.tagName.toLowerCase(),
        cls: (el.getAttribute("class") || "").slice(0, 60),
        x: Math.round(r.left),
        y: Math.round(r.top),
        w: Math.round(r.width),
        h: Math.round(r.height),
        display: cs.display,
        visibility: cs.visibility,
        hit_self: !!struck && (struck === el || el.contains(struck)),
        hit_what: tagOf(struck),
      };
    });
    return {
      label: wanted,
      matches,
      zero_size: matches.filter((m) => m.w <= 0 || m.h <= 0).length,
      unhit: matches.filter((m) => !m.hit_self).length,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      scroll_y: Math.round(window.scrollY),
      page_height: document.documentElement.scrollHeight,
    } as unknown as Sweep;
  }, LABEL);
}

async function boot(page: Page, journeyId: string) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  page.setDefaultTimeout(30_000);
  await page.setViewportSize({ width: 390, height: 844 });
}

/**
 * Two independent statements of "the current journey": what the shell adopted at boot, and what the
 * server says the stage is. They must agree before the clipboard is judged against either.
 */
async function currentJourney(page: Page): Promise<{ stored: string; staged: string; origin: string }> {
  const staged = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const stored = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id") ?? "");
  /* Read off the page: the expected string is never assembled from a port this file guessed. */
  const origin = await page.evaluate(() => window.location.origin);
  return { stored: stored.trim(), staged: String(staged.journey_id ?? "").trim(), origin };
}

/** Strike the centre point with the mouse: a locator click would succeed on a node a real finger cannot reach. */
async function strike(page: Page, x: number, y: number) {
  await page.mouse.click(x, y);
}

async function clipboardText(page: Page): Promise<string> {
  return page.evaluate(async () => {
    try {
      return await navigator.clipboard.readText();
    } catch {
      return "__unreadable__";
    }
  });
}

/**
 * Chromium's clipboard is shared by every context in one browser instance, so "it changed from what it
 * was" is not a property this file can assert across tests -- an earlier arm's write would satisfy it.
 * The clipboard is therefore primed with a sentinel that is not a journey link, and the claim is the
 * exact value after the strike. A write that never happens leaves the sentinel in place.
 */
const SENTINEL = "n3-ord0366-sentinel-not-a-journey-link";

async function primeClipboard(page: Page): Promise<string> {
  await page.evaluate((text: string) => navigator.clipboard.writeText(text), SENTINEL);
  return clipboardText(page);
}

function armOf(title: string): string {
  /* The three scroll poses are three arms: keying them all `T1` made every afterEach write the same
     file, so only the last pose survived and "all three poses are green" was never provable. */
  const pose = /^T1_today_one_strike_at_scroll_(top|middle|bottom)/.exec(title);
  if (pose) return `T1_${pose[1]}`;
  return /^([ITBM]\d)/.exec(title)?.[1] ?? title.slice(0, 2);
}

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const body: Readings = { ...(readings[arm] ?? {}) };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 600);
  }
  writeFileSync(
    evidencePath(`ord0366/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

/* ------------------------------------------------------------------ I1: the label, and whether it can be struck */

test("I1_every_named_node_on_today_is_struck_at_its_own_centre — Today 屏上每个带这个名子的节点，中心点都要真能打到（零尺寸同名节点直接红）", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "I1";
  const staged = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(staged.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey, so Today has nothing to share")).toBe(true);
  await boot(page, id);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(
    page.getByTestId("today-hero"),
    node(arm, "Today never rendered, so the sweep below would be judging an empty page"),
  ).toBeVisible({ timeout: 30_000 });
  const sweep = await sweepLabel(page);
  readings[arm] = { ...sweep, journey_id: id };
  await page.screenshot({ path: evidencePath(`ord0366/${RUN}__I1_today_pose.png`) });
  /* A reachability claim that never looked at the rendering can be satisfied by a control that is
     technically struck and visually buried, so the pose is kept as an artefact next to the numbers. */
  await page.screenshot({ path: evidencePath(`ord0366/${RUN}__I1_today_pose.png`) });
  expect(sweep.matches.length, node(arm, `no node on Today carries the label 「${LABEL}」 -- the entry is not reachable from this screen`)).toBeGreaterThan(0);
  expect(sweep.zero_size, node(arm, `a same-named node has no size (${JSON.stringify(sweep.matches.filter((m) => m.w <= 0 || m.h <= 0))}) -- it sits in the tree where a finger cannot go`)).toBe(0);
  expect(sweep.unhit, node(arm, `a same-named node did not answer elementFromPoint at its own centre (${JSON.stringify(sweep.matches.filter((m) => !m.hit_self))})`)).toBe(0);
});

/* ------------------------------------------------------------------ I2: the other same-named entries on these screens */

test("I2_no_studio_label_sits_on_these_screens_without_a_size — 这两屏上带「工作台」名子的节点都得有尺寸且打得到（零尺寸同名节点就是本单一半的由来）", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "I2";
  const staged = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(staged.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);
  const wanted = ["打开行程工作台", "行程工作台"];
  const perScreen: Readings = {};
  for (const route of ["/v8/hub", "/v8/bookings"]) {
    await boot(page, id);
    await page.goto(route, { waitUntil: "load" });
    await page.waitForTimeout(1_200);
    for (const label of wanted) {
      const sweep = await page.evaluate((name: string) => {
        const nameOf = (el: Element) => (el.getAttribute("aria-label") || (el.textContent ?? "")).trim();
        /* "In the accessibility tree" is not a property of the node alone: an ancestor with
           display:none excludes the whole subtree, and that is the difference between a node a finger
           cannot reach and a node a screen reader cannot reach. */
        const exclusion = (el: Element): string | null => {
          for (let cur: Element | null = el; cur; cur = cur.parentElement) {
            const cs = getComputedStyle(cur);
            if (cs.display === "none") return `display:none@${cur.tagName.toLowerCase()}.${(cur.getAttribute("class") || "").split(" ")[0]}`;
            if (cs.visibility === "hidden") return `visibility:hidden@${cur.tagName.toLowerCase()}`;
            if ((cur as HTMLElement).hidden) return `hidden@${cur.tagName.toLowerCase()}`;
            if (cur.getAttribute("aria-hidden") === "true") return `aria-hidden@${cur.tagName.toLowerCase()}`;
          }
          return null;
        };
        const matches = Array.from(document.querySelectorAll("*"))
          .filter((el) => nameOf(el) === name)
          .map((el) => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            const struck = r.width > 0 && r.height > 0 ? document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) : null;
            return {
              tag: el.tagName.toLowerCase(),
              cls: (el.getAttribute("class") || "").slice(0, 50),
              aria: el.getAttribute("aria-label") || "",
              path: `${el.parentElement?.tagName.toLowerCase() || ""}>${el.tagName.toLowerCase()}`,
              w: Math.round(r.width),
              h: Math.round(r.height),
              display: cs.display,
              visibility: cs.visibility,
              excluded_by: exclusion(el),
              in_a11y_tree: exclusion(el) === null,
              hit_self: !!struck && (struck === el || el.contains(struck)),
            };
          });
        return { label: name, count: matches.length, matches };
      }, label);
      perScreen[`${route}::${label}`] = sweep;
    }
  }
  readings[arm] = { journey_id: id, screens: perScreen };
  /* The claim: any node that is still in the accessibility tree under one of these names has a box and
     answers a strike at its own centre. A node the browser already excludes (display:none / hidden) is
     not "in the tree", which is the second remedy the order allows. */
  const offenders = Object.entries(perScreen).flatMap(([key, v]) => {
    const sweep = v as unknown as { matches: Array<Record<string, unknown>> };
    return sweep.matches
      .filter((m) => m.in_a11y_tree === true && ((m.w as number) <= 0 || (m.h as number) <= 0 || m.hit_self === false))
      .map((m) => ({ where: key, ...m }));
  });
  expect(offenders, node(arm, `named nodes nobody can strike: ${JSON.stringify(offenders)}`)).toEqual([]);
});

/* ------------------------------------------------------------------ T1/T2: one tap, three scroll poses */

async function tapAtPose(page: Page, pose: "top" | "middle" | "bottom") {
  await page.evaluate((p: string) => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    window.scrollTo(0, p === "top" ? 0 : p === "middle" ? Math.round(max / 2) : Math.max(0, max));
  }, pose);
  await page.waitForTimeout(200);
  const scrolledTo = await page.evaluate(() => Math.round(window.scrollY));
  /* A control sitting above the viewport is not an unreachable one -- a traveller scrolls. The claim
     under test is that after it is brought into view, nothing (fixed chrome, a sheet, zero size) stops
     the centre from being struck. So: bring it in, let the sticky bar settle, then hit-test. */
  const centred = await page.evaluate((wanted: string) => {
    const nameOf = (el: Element) => (el.getAttribute("aria-label") || (el.textContent ?? "")).trim();
    const hit = Array.from(document.querySelectorAll("*")).find(
      (el) => nameOf(el) === wanted && el.tagName.toLowerCase() === "button",
    );
    if (!hit) return false;
    hit.scrollIntoView({ block: "center" });
    return true;
  }, LABEL);
  expect(centred, node("T", `at scroll pose ${pose} no button carries the label 「${LABEL}」 -- there is nothing to bring into view`)).toBe(true);
  await page.waitForTimeout(300);
  const sweep = await sweepLabel(page);
  expect(sweep.matches.length, node("T", `the labelled control is not in the page at scroll pose ${pose}`)).toBeGreaterThan(0);
  expect(sweep.zero_size, node(armOf(`T1_${pose}`), `a same-named node has no size at scroll pose ${pose} (${JSON.stringify(sweep.matches.filter((m) => m.w <= 0 || m.h <= 0))})`)).toBe(0);
  const target = sweep.matches.find((m) => m.hit_self && m.w > 0 && m.h > 0);
  expect(!!target, node("T", `at scroll pose ${pose}, once brought into view, nothing with that label answers a strike at its own centre -- something is covering it (matches: ${JSON.stringify(sweep.matches)})`)).toBe(true);
  const before = await primeClipboard(page);
  await strike(page, target!.x + target!.w / 2, target!.y + target!.h / 2);
  await expect(
    page.getByTestId("journey-link-message").first(),
    node("T", `the strike at scroll pose ${pose} produced no feedback line -- a silent clipboard write is not a copy`),
  ).toBeVisible({ timeout: 15_000 });
  const after = await clipboardText(page);
  const message = ((await page.getByTestId("journey-link-message").first().textContent()) ?? "").trim();
  const ids = await currentJourney(page);
  return { pose, sweep, before, after, message, ids, target: target!, scrolledTo };
}

for (const pose of ["top", "middle", "bottom"] as const) {
  test(`T1_today_one_strike_at_scroll_${pose}_carries_the_current_journey_link — Today 直接一下（滚动位 ${pose}）：剪贴板里就是当前这一趟的绝对链接`, async ({ page }) => {
    test.setTimeout(300_000);
    const arm = `T1_${pose}`;
    const staged = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
    const id = String(staged.journey_id ?? "");
    expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);
    await boot(page, id);
    await page.goto("/v8/hub", { waitUntil: "load" });
    await expect(page.getByTestId("today-hero"), node(arm, "Today never rendered")).toBeVisible({ timeout: 30_000 });
    const r = await tapAtPose(page, pose);
    const expected = `${r.ids.origin}/v8/${r.ids.staged}`;
    readings[arm] = {
      pose,
      journey_id: id,
      stored_id: r.ids.stored,
      staged_id: r.ids.staged,
      page_origin: r.ids.origin,
      expected_link: expected,
      two_sources_agree: r.ids.stored === r.ids.staged,
      clipboard_before: r.before,
      clipboard_after: r.after,
      clipboard_after_length: r.after.length,
      message_text: r.message,
      struck_box: { x: r.target.x, y: r.target.y, w: r.target.w, h: r.target.h },
      scroll_y_before_centring: r.scrolledTo,
      scroll_y_at_strike: r.sweep.scroll_y,
      matched_nodes: r.sweep.matches.length,
      tail_matches_current_id: r.after.endsWith(`/${id}`),
    };
    expect(r.ids.stored, node(arm, `the id the shell adopted (${r.ids.stored}) is not the one the stage names (${r.ids.staged}) -- cannot say which journey is "current"`)).toBe(r.ids.staged);
    expect(PLACEHOLDERISH.test(r.ids.staged), node(arm, `the current journey id looks like a placeholder (${r.ids.staged})`)).toBe(false);
    expect(r.before, node(arm, `the clipboard was not primed before the strike (it read "${r.before}") -- without the sentinel nothing here distinguishes a write from a leftover`)).toBe(SENTINEL);
    expect(r.after, node(arm, `the strike left the clipboard reading "${r.after}" instead of the current journey's link ${expected} -- text without a write is the defect M1 reproduces`)).toBe(expected);
    expect(/^https?:\/\/[^\s]+\/v8\/[0-9a-fA-F-]{36}$/.test(r.after), node(arm, `the clipboard does not hold an absolute journey link (${r.after})`)).toBe(true);
    expect(r.after.endsWith(`/${r.ids.staged}`), node(arm, `the tail is not the CURRENT journey's id (${r.after}) -- M2 reproduces this shape`)).toBe(true);
    expect(r.message.includes("复制"), node(arm, `the line beside the control does not speak of a copy (${r.message})`)).toBe(true);
  });
}

/* ------------------------------------------------------------------ B1: the work bench entry stays working */

test("B1_the_work_bench_entry_still_writes_the_same_link — 工作台那一枚没有被换掉：同一个结果", async ({ page }) => {
  test.setTimeout(300_000);
  const arm = "B1";
  const staged = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(staged.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);
  await boot(page, id);
  await page.goto("/v8/bookings", { waitUntil: "load" });
  await expect(
    page.getByTestId("inbox-tab-paste"),
    node(arm, "the work bench never rendered, so this arm has no surface"),
  ).toBeVisible({ timeout: 30_000 });
  const button = page.getByTestId("journey-link-copy").first();
  await button.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  /* Bring it into view first, then judge -- sweeping at the resting scroll offset would grade the
     offset rather than the control, which is the mistake the first pass of this file made. */
  const sweep = await sweepLabel(page);
  expect(sweep.matches.length, node(arm, "the bench lost its own copy of the entry")).toBeGreaterThan(0);
  expect(sweep.zero_size, node(arm, `a bench-side same-named node has no size (${JSON.stringify(sweep.matches)})`)).toBe(0);
  /* The same bar Today is held to: a labelled node that answers no strike is a trap for the next
     reader, and the bench is the screen this order was written because of. */
  expect(sweep.unhit, node(arm, `a bench-side same-named node did not answer a strike at its own centre (${JSON.stringify(sweep.matches)})`)).toBe(0);
  const box = await button.boundingBox();
  expect(!!box, node(arm, "the bench control reports no box")).toBe(true);
  const before = await primeClipboard(page);
  await strike(page, (box as { x: number; y: number; width: number; height: number }).x + box!.width / 2, (box as { x: number; y: number; width: number; height: number }).y + box!.height / 2);
  await expect(
    page.getByTestId("journey-link-message").first(),
    node(arm, "the bench strike produced no feedback line"),
  ).toBeVisible({ timeout: 15_000 });
  const after = await clipboardText(page);
  const ids = await currentJourney(page);
  const expected = `${ids.origin}/v8/${ids.staged}`;
  readings.B1 = {
    journey_id: id,
    matched_nodes: sweep.matches.length,
    struck_box: box,
    page_origin: ids.origin,
    expected_link: expected,
    clipboard_before: before,
    clipboard_after: after,
    clipboard_after_length: after.length,
    two_sources_agree: ids.stored === ids.staged,
    tail_matches_current_id: after.endsWith(`/${ids.staged}`),
  };
  expect(before, node(arm, `the bench clipboard was not primed (it read "${before}") -- the sentinel is what makes a missing write visible`)).toBe(SENTINEL);
  expect(after, node(arm, `the bench strike left "${after}" instead of ${expected}`)).toBe(expected);
});
