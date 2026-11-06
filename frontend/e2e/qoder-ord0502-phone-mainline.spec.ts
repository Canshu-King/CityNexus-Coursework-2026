/* ORD-0502 — the phone mainline as ONE walk that names the link that broke.
 *
 * Seven links are named in the order's sentence (the order says 六格 and then lists seven; this file
 * walks all seven and says so in the summary rather than quietly merging two of them):
 *
 *   C1 hub 拿到行程            C2 粘贴导入地点        C3 落到那一天
 *   C4 打卡                    C5 足迹屏读到那一枚    C6 长图导出成图
 *   C7 分享 token 第二上下文打开
 *
 * One cell is not in the order's list: C2b walks 确定性导入, the door C2's own screen names when the
 * AI extractor is not configured.  It is measured and judged like the rest (its red counts in the
 * chain line) because without it C3..C6 have nothing to stand on at this generation; C2 itself is
 * still judged on the paste, exactly as the order words it.
 *
 * Rules this file is written against, from the order:
 *   - verdicts come from `innerText`, never from a `data-*` attribute;
 *   - every tap is hit-tested at the element's own centre with `document.elementFromPoint` after
 *     scrolling it into view, so a node hidden behind the bottom bar cannot pass as clickable;
 *   - the walk runs at a real 390 px phone width;
 *   - a failed link names its leg: the route it called and the `http_status=` it saw;
 *   - C4 may record `NOT_REACHED_UI` when the phone has no entry point, but that is never allowed to
 *     read as green -- it lands in the summary as an incomplete chain with the cell named.
 *
 * Nothing here mocks: the only HTTP calls are the ones the product's own screens make.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const OUT = process.env.CITYNEXUS_ORD0502_OUT ?? "D:/moodle/_ord0502";
const RUN_ID = process.env.CITYNEXUS_E2E_RUN_ID ?? "unrun";
const BACKEND = `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? "")}`;
const PHONE = { width: 390, height: 844 };
/* Playwright hands every test a fresh context, and the shell's only handle on a journey is a
   localStorage key (`app/V8Experience.tsx`).  Without a handoff C1's journey is invisible to
   the cells after it and they time out on a screen that never renders -- which is what the previous
   three generations of this run reported.  `beforeAll` below creates the journey through the
   product's own wizard and saves it here; the import must NOT blank this file, because a worker
   that restarts after a timeout would otherwise erase the very handoff it is about to read. */
mkdirSync(OUT, { recursive: true });
const STATE = path.join(OUT, `state-${RUN_ID}.json`);
const JOURNEY_KEY = "REDACTED_FOR_PUBLIC_RELEASE";
/* The run's own readings file: `readings` lives in module memory, so a worker that is killed and
   restarted mid-walk (Playwright does that on a test timeout) arrives with none of them. */
const WALK = path.join(OUT, `walk-${RUN_ID}.json`);
/* A string the backend's own extractor is proven to accept -- lifted verbatim from
 * `backend/tests/test_v10_flagship_e2e.py`, which asserts http_status=201 and status READY on it. */
const PASTE = "V10-RAIL-42 rail confirmation Tianjin West";

type Leg = { name: string; method: string; route: string; http_status: number | "NOT_SEEN" };
type Reading = {
  cell: string;
  state: "PASS" | "FAIL" | "NOT_REACHED_UI";
  route: string;
  viewport_px: number;
  rendered: string;
  hit: Record<string, unknown>;
  legs: Leg[];
  failure: string | null;
};

const readings: Reading[] = [];
const legOf = (cell: string): Leg[] => (readings.find((r) => r.cell === cell)?.legs ?? []);

function record(reading: Reading): void {
  readings.push(reading);
  console.log(
    `[ord0502][${RUN_ID}] ${reading.cell} ${reading.state} route=${reading.route} ` +
      `hit=${JSON.stringify(reading.hit)} legs=${reading.legs
        .map((l) => `${l.method} ${l.route} http_status=${l.http_status}`)
        .join(" | ")}` +
      (reading.failure ? ` failure=${reading.failure}` : ""),
  );
}

async function inner(page: Page, selector: string): Promise<string> {
  /* The order's rule: read what is rendered. `innerText` is layout-aware, so a node the browser did
     not paint reads as the empty string -- which is what we want to fail on.
     The wait is bounded: an unbounded `innerText()` on a selector that never appears sits silent
     until the test timeout fires, and the cell's verdict then reads as a timeout instead of as
     "this text is not on this screen". */
  return (await page.locator(selector).first().innerText({ timeout: 10_000 })).replace(/\s+/g, " ").trim();
}

async function centreHit(page: Page, target: Locator): Promise<Record<string, unknown>> {
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  if (!box) return { hittable: false, why: "no boundingBox at 390 px" };
  const cx = Math.round(box.x + box.width / 2);
  const cy = Math.round(box.y + box.height / 2);
  const at = await page.evaluate(
    ({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      if (!el) return null;
      return {
        tag: el.tagName.toLowerCase(),
        testid: el.getAttribute("data-testid") ?? null,
        cls: (el as HTMLElement).className?.toString().slice(0, 60) ?? null,
      };
    },
    { x: cx, y: cy },
  );
  const owns = await target.evaluate((node, p) => {
    const hit = document.elementFromPoint(p.x, p.y);
    return !!hit && (node === hit || node.contains(hit) || hit.contains(node));
  }, { x: cx, y: cy });
  return { hittable: owns, cx, cy, box: { x: box.x, y: box.y }, at, ...({} as Record<string, unknown>) };
}

async function tap(page: Page, cell: string, target: Locator): Promise<Record<string, unknown>> {
  const hit = await centreHit(page, target);
  if (hit.hittable !== true) {
    throw new Error(`${cell}: centre hit refused the node -- ${JSON.stringify(hit)}`);
  }
  await target.click();
  return hit;
}

async function waitLeg(page: Page, leg: Leg, action: () => Promise<void>): Promise<void> {
  const watcher = page
    .waitForResponse((r) => r.url().includes(leg.route), { timeout: 30_000 })
    .then((r) => {
      leg.http_status = r.status();
    })
    .catch(() => undefined);
  await action();
  await watcher;
}

/**
 * 确定性导入 -- the door the paste screen points at by name when the extractor is not configured
 * (`JourneyInboxWorkbench.tsx` answers 「DeepSeek 提取未配置；确定性导入仍可用」; the tab is `:616`,
 * the fields `:635`-`:714`, and 提交 POSTs `/inbox/import` at `:329`).  Two cells call it: C2b measures
 * the door on its own, and C3 uses it to get a draft at all so that 落到那一天 can still be judged on a
 * deployment where the AI extractor is off.
 */
async function deterministicImport(
  page: Page,
  cell: string,
  legs: Leg[],
): Promise<{ status: number | "NOT_SEEN"; rendered: string; shown: boolean }> {
  await tap(page, cell, page.getByTestId("inbox-tab-manual"));
  await page.getByTestId("manual-type").selectOption({ value: "RAIL" });
  await page.getByTestId("manual-title").fill("V10-RAIL-42 天津西到达");
  await page.getByTestId("manual-ref").fill("V10-RAIL-42");
  await page.getByTestId("manual-from").fill("北京南");
  await page.getByTestId("manual-to").fill("天津西");
  await page.getByTestId("manual-depart").fill("2026-10-20T09:00");
  await page.getByTestId("manual-arrive").fill("2026-10-20T10:20");
  await page.getByTestId("manual-amount").fill("58");
  const leg: Leg = { name: "manual-import", method: "POST", route: "/inbox/import", http_status: "NOT_SEEN" };
  legs.push(leg);
  await waitLeg(page, leg, async () => {
    await tap(page, cell, page.getByTestId("manual-submit"));
  });
  const shown = await page
    .getByTestId("v10-inbox-draft")
    .waitFor({ timeout: 20_000 })
    .then(() => true)
    .catch(() => false);
  return { status: leg.http_status, shown, rendered: shown ? await inner(page, '[data-testid="v10-inbox-draft"]') : "" };
}

function summary(): { run_id: string; chain: string; cells: string[]; readings: Reading[] } {
  const bad = readings.filter((r) => r.state !== "PASS").map((r) => `${r.cell}:${r.state}`);
  return {
    run_id: RUN_ID,
    /* An empty denominator is the one reading that must never look like a result: a worker that
       recorded nothing used to write `chain: GREEN` over the run's own file. */
    chain: readings.length === 0 ? "INCOMPLETE NO_READINGS" : bad.length ? `INCOMPLETE ${bad.join(" ")}` : "GREEN",
    cells: readings.map((r) => r.cell),
    readings,
  };
}

/* `readings` is module state and Playwright can start a fresh worker mid-walk (it kills one on a
   test timeout), which would leave the later worker's afterAll to overwrite the run's record with
   only what IT saw.  The file is this run's, so it is read back and merged before anything writes
   or judges it. */
function mergePriorReadings(): void {
  if (!existsSync(WALK)) return;
  let prior: { readings?: Reading[] };
  try {
    prior = JSON.parse(readFileSync(WALK, "utf-8"));
  } catch {
    return;
  }
  const added = (prior.readings ?? []).filter((p) => !readings.some((r) => r.cell === p.cell));
  for (const reading of added) readings.push(reading);
  readings.sort((a, b) => a.cell.localeCompare(b.cell));
  if (added.length) {
    console.log(`[ord0502][${RUN_ID}] merged ${added.length} reading(s) written by an earlier worker`);
  }
}

test.describe("ORD-0502 phone mainline", () => {
  test.use({ viewport: PHONE, locale: "zh-CN", hasTouch: true, serviceWorkers: "block", storageState: STATE });

  test.afterAll(async () => {
    mkdirSync(OUT, { recursive: true });
    mergePriorReadings();
    const s = summary();
    writeFileSync(WALK, JSON.stringify(s, null, 2), "utf-8");
    console.log(`[ord0502][${RUN_ID}] CHAIN=${s.chain}`);
  });

  /* One device for the whole chain.  A traveller does not get a new phone between 打卡 and 足迹,
     and every cell below is written against "the journey this walk made".  The journey is created
     here, through the product's own wizard (no API call is invented: the only request is the
     POST /api/v1/journeys the 确认创建 button makes), and handed to the cells as storage state.
     If a worker restarts mid-walk, an existing handoff is reused rather than re-made. */
  test.beforeAll(async ({ browser }) => {
    if (existsSync(STATE)) {
      const prior = JSON.parse(readFileSync(STATE, "utf-8"));
      const seeded = (prior.origins ?? []).some((origin: { localStorage?: { name: string }[] }) =>
        (origin.localStorage ?? []).some((entry) => entry.name === JOURNEY_KEY),
      );
      if (seeded) {
        console.log(`[ord0502][${RUN_ID}] reusing the journey this run already seeded`);
        return;
      }
    }
    /* `browser.newContext()` inherits this suite's `test.use`, including the storage state -- and the
       state this hook exists to CREATE is not on disk yet, so the seed context has to be opened
       with an empty one rather than the file it is about to write. */
    const context = await browser.newContext({
      viewport: PHONE,
      locale: "zh-CN",
      hasTouch: true,
      serviceWorkers: "block",
      storageState: { cookies: [], origins: [] },
    });
    const page = await context.newPage();
    try {
      await page.goto("/v8/hub");
      await page.getByTestId("trial-step1-body").waitFor({ timeout: 30_000 });
      for (const [field, value] of [
        ["trial-city", "天津"],
        ["trial-arrival", "天津西站"],
        ["trial-departure", "北京南站"],
      ] as const) {
        await page.getByTestId(field).fill(value);
      }
      for (let step = 1; step <= 2; step += 1) {
        await page.getByTestId("trial-next").click();
        await page.getByTestId(`trial-step${step + 1}-body`).waitFor({ timeout: 20_000 });
      }
      const leg: Leg = { name: "seed-journey", method: "POST", route: "/api/v1/journeys", http_status: "NOT_SEEN" };
      await waitLeg(page, leg, async () => {
        await page.getByTestId("trial-create").click();
      });
      expect(leg.http_status, `the walk's own precondition failed: POST /api/v1/journeys http_status=${leg.http_status}`).toBe(201);
      await page.waitForSelector('[data-testid="today-heading"]', { timeout: 60_000 });
      await context.storageState({ path: STATE });
      console.log(`[ord0502][${RUN_ID}] precondition seeded: journey created through the wizard, ${leg.http_status}`);
    } finally {
      await context.close();
    }
  });

  test("C1 hub 拿到行程", async ({ page, request }) => {
    const legs: Leg[] = [];
    let route = "/v8/hub";
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    try {
      await page.goto("/v8/hub");
      /* Bounded, and it names which of the three screens the phone actually painted.  This cell used
         to test `today-heading` once and otherwise wait for the wizard, so a hub showing a THIRD
         thing -- a spinner, a refusal -- came back as a bare locator timeout that said nothing about
         the screen the traveller was standing on. */
      let screen = "";
      for (let tick = 0; tick < 50 && screen === ""; tick += 1) {
        if (await page.getByTestId("today-heading").count()) screen = "heading";
        else if (await page.getByTestId("trial-step1-body").count()) screen = "wizard";
        else await page.waitForTimeout(500);
      }
      if (screen === "") {
        rendered = (await page.locator("body").innerText({ timeout: 8_000 })).replace(/\s+/g, " ").trim().slice(0, 200);
        throw new Error(`C1: /v8/hub painted neither 行程 nor the wizard within 25 s; the phone shows: ${rendered}`);
      }
      if (screen === "heading") {
        rendered = await inner(page, '[data-testid="today-heading"]');
        state = "PASS";
      } else {
        /* No journey on this device -- and this generation has no "open the wizard" control to
           press: `app/V8Experience.tsx` returns the wizard itself in place of the shell when
           nothing is stored.  The walk therefore goes through the wizard's own fields
           (`features/setup/JourneyTrialSetup.tsx`, `:1011`, `:1040`), whose step-1 gate is the
           three values named at `:784-793`; 下一步/确认创建 are at `:1560`/`:1584`. */
        for (const [field, value] of [
          ["trial-city", "天津"],
          ["trial-arrival", "天津西站"],
          ["trial-departure", "北京南站"],
        ] as const) {
          const input = page.getByTestId(field);
          hit = await centreHit(page, input);
          if (hit.hittable !== true) {
            throw new Error(`C1: wizard field ${field} does not answer at its own centre -- ${JSON.stringify(hit)}`);
          }
          await input.fill(value);
        }
        for (let step = 1; step <= 2; step += 1) {
          hit = await tap(page, "C1", page.getByTestId("trial-next"));
          await page.getByTestId(`trial-step${step + 1}-body`).waitFor({ timeout: 20_000 });
        }
        const createLeg: Leg = { name: "create-journey", method: "POST", route: "/api/v1/journeys", http_status: "NOT_SEEN" };
        legs.push(createLeg);
        hit = await centreHit(page, page.getByTestId("trial-create"));
        if (hit.hittable !== true) {
          throw new Error(`C1: 确认创建 does not answer at its own centre -- ${JSON.stringify(hit)}`);
        }
        await waitLeg(page, createLeg, async () => {
          await page.getByTestId("trial-create").click();
        });
        if (createLeg.http_status === "NOT_SEEN") {
          const hint = await page.getByTestId("trial-required-hint").innerText({ timeout: 8_000 }).catch(() => "");
          throw new Error(`C1: no POST /api/v1/journeys left the phone; the screen says ${hint.slice(0, 90)}`);
        }
        await page.waitForSelector('[data-testid="today-heading"]', { timeout: 60_000 });
        rendered = await inner(page, '[data-testid="today-heading"]');
        state = createLeg.http_status === 201 || createLeg.http_status === 200 ? "PASS" : "FAIL";
        if (state === "FAIL") failure = `create-journey POST /api/v1/journeys http_status=${createLeg.http_status}`;
      }
      if (state === "PASS") {
        /* Hand the journey this cell created to the cells that follow (see STATE above). */
        await page.context().storageState({ path: STATE });
      }
      const health: Leg = { name: "hub-health", method: "GET", route: "/api/v1/health", http_status: "NOT_SEEN" };
      const res = await request.get(`${BACKEND}/api/v1/health`);
      health.http_status = res.status();
      legs.push(health);
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C1", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(state, failure ?? "C1 failed").toBe("PASS");
  });

  test("C2 粘贴导入地点", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/trips (预订 子页)";
    try {
      await page.goto("/v8/trips");
      hit = await tap(page, "C2", page.getByTestId("trips-tab-bookings"));
      await page.getByTestId("inbox-tab-paste").waitFor({ timeout: 20_000 });
      hit = await tap(page, "C2", page.getByTestId("inbox-tab-paste"));
      await page.getByPlaceholder(/粘贴铁路、航班、酒店或场地确认文本|Paste rail/).fill(PASTE);
      const extractLeg: Leg = { name: "paste-extract", method: "POST", route: "/inbox/text-extract", http_status: "NOT_SEEN" };
      legs.push(extractLeg);
      await waitLeg(page, extractLeg, async () => {
        hit = await tap(page, "C2", page.getByRole("button", { name: /提取文本草稿/ }));
      });
      const draftShown = await page
        .getByTestId("v10-inbox-draft")
        .waitFor({ timeout: 30_000 })
        .then(() => true)
        .catch(() => false);
      const statusLine = draftShown
        ? ""
        : (await page.locator(".v10-inbox-message").innerText({ timeout: 8_000 }).catch(() => "")).replace(/\s+/g, " ").trim();
      /* The draft the extractor returned lands in `v10-inbox-draft` (`JourneyInboxWorkbench.tsx`,
         fed by `showResult` at `:417`).  The panel this cell used to read, `v13-2-memory`, is the
         *confirmed-artefact* memory (`:753`) -- on a journey this walk just created it correctly
         reads 「0 个受保护事实 / 暂无已确认工件绑定到此行程」, and judging the paste by that panel
         measured the wrong screen.  When no draft arrives at all, the sentence the traveller gets is
         the paste's own status line (`:745`, `role="status"`), so that is what gets recorded. */
      rendered = draftShown ? await inner(page, '[data-testid="v10-inbox-draft"]') : statusLine;
      const namedPlace = /天津西|Tianjin West/.test(rendered);
      state = extractLeg.http_status === 201 && draftShown && namedPlace ? "PASS" : "FAIL";
      if (state === "FAIL") {
        failure =
          `paste-extract POST /inbox/text-extract http_status=${extractLeg.http_status}; draft rendered: ${draftShown}; ` +
          `the phone's own words: ${(draftShown ? rendered : statusLine).slice(0, 110) || "(none)"}`;
      }
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C2", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(state, failure ?? "C2 failed").toBe("PASS");
  });

  test("C2b 确定性导入（C2 那屏自己指的另一扇门）", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/trips (预订 子页 → 手工录入)";
    let hit: Record<string, unknown> = {};
    try {
      await page.goto("/v8/trips");
      hit = await tap(page, "C2b", page.getByTestId("trips-tab-bookings"));
      const door = await deterministicImport(page, "C2b", legs);
      rendered = door.rendered;
      const importOk = door.status === 201 || door.status === 200;
      state = importOk && door.shown && /天津西/.test(rendered) ? "PASS" : "FAIL";
      if (state === "FAIL") {
        failure =
          `manual import POST /inbox/import http_status=${door.status}; draft rendered: ${door.shown}; ` +
          `the phone's own words: ${(door.rendered || "（无）").slice(0, 110)}`;
      }
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C2b", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(state, failure ?? "C2b failed").toBe("PASS");
  });

  test("C3 落到那一天", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/trips (预订 子页 → 行程)";
    try {
      await page.goto("/v8/trips");
      await tap(page, "C3", page.getByTestId("trips-tab-bookings"));
      /* The draft C2 pasted is on the server (`GET .../inbox`, `JourneyInboxWorkbench.tsx`) and
         reaches the review panel only through its queue row: `inbox-open-review` at `:828`.  The
         walk used to wait for the anchor select directly, which never renders until that row is
         opened -- so this cell measured a screen the phone had not been asked to show. */
      let queueShown = await page
        .getByTestId("inbox-pending-row")
        .first()
        .waitFor({ timeout: 30_000 })
        .then(() => true)
        .catch(() => false);
      if (!queueShown) {
        /* The paste left nothing in the queue for a reason that belongs to C2, not to this cell.  So
           take the door the paste screen itself names, then judge 落到那一天 on the thing it was
           asked to judge: can a confirmation be anchored to a day.  Which door was used is carried
           in `rendered`, so the reading does not pretend the paste worked. */
        const queueSaid = (
          await page.getByTestId("inbox-pending-empty").innerText({ timeout: 8_000 }).catch(() => "")
        ).replace(/\s+/g, " ").trim();
        const door = await deterministicImport(page, "C3", legs);
        rendered = `粘贴留下的队列：${queueSaid || "（无）"}；改走确定性导入 POST /inbox/import http_status=${door.status}，屏上草稿=${door.shown}`;
        queueShown = await page
          .getByTestId("inbox-pending-row")
          .first()
          .waitFor({ timeout: 20_000 })
          .then(() => true)
          .catch(() => false);
      }
      if (!queueShown) {
        throw new Error(`C3: still no pending draft after the deterministic door; ${rendered.slice(0, 140)}`);
      }
      /* Two scroll shapes, because which one answers decides whose defect this is.  (a) is the
         order's rule: scroll into view, then hit-test the node's own centre.  (b) puts the node in
         the MIDDLE of the viewport, which is where a traveller who meant to press it would leave it.
         If (a) refuses while (b) is clean, the control is not unreachable -- it is parked in the band
         the fixed bar owns when the browser scrolls the bare minimum; if both refuse, the bar truly
         covers it. */
      const review = page.getByTestId("inbox-open-review").first();
      const minimal = await centreHit(page, review);
      await review.evaluate((node) => node.scrollIntoView({ block: "center" }));
      const centred = await centreHit(page, review);
      hit = { minimal_scroll: minimal, centre_scroll: centred };
      if (minimal.hittable !== true) {
        throw new Error(
          `C3: 打开审核 centre hit refused at its own centre -- ${JSON.stringify(minimal)}; with the row ` +
            `scrolled to the middle of the 390x844 viewport the same hit reads ${JSON.stringify(centred)}`,
        );
      }
      await review.click();
      await page.getByTestId("v13-2-anchor-role").waitFor({ timeout: 30_000 });
      /* 落到那一天: the rail confirmation is the arrival leg, and the option's own value is the
         machine word the server stores (`:870`), not a label this file invents. */
      await page.getByTestId("v13-2-anchor-role").selectOption({ value: "ARRIVAL" });
      const saveLeg: Leg = { name: "save-corrections", method: "PATCH", route: "/inbox/", http_status: "NOT_SEEN" };
      legs.push(saveLeg);
      await waitLeg(page, saveLeg, async () => {
        hit = await tap(page, "C3", page.getByTestId("v13-2-save-corrections"));
      });
      const confirmLeg: Leg = { name: "confirm-draft", method: "POST", route: "/confirm", http_status: "NOT_SEEN" };
      legs.push(confirmLeg);
      const apply = page.getByTestId("v13-2-smart-apply");
      const which = (await apply.count()) && !(await apply.isDisabled()) ? apply : page.getByTestId("v13-2-confirm");
      if (which === apply) {
        confirmLeg.name = "smart-apply";
        confirmLeg.route = "/inbox/";
        confirmLeg.method = "POST";
      }
      await waitLeg(page, confirmLeg, async () => {
        hit = await tap(page, "C3", which);
      });
      await page.getByTestId("trips-tab-itinerary").click();
      await page.getByTestId("trip-itinerary").waitFor({ timeout: 30_000 });
      rendered = await inner(page, '[data-testid="trip-itinerary"]');
      const landed = rendered.includes("V10-RAIL-42") || /天津西|Tianjin West/.test(rendered);
      /* `http_status` is `number | "NOT_SEEN"`, and `e2e/` sits outside `tsconfig.typecheck.json`'s
         include (`"src/**"`), so the bare `< 400` on that union never reached the gate. */
      const confirmed = typeof confirmLeg.http_status === "number" && confirmLeg.http_status < 400;
      state = landed && confirmed ? "PASS" : "FAIL";
      if (state === "FAIL") {
        failure =
          `confirm leg ${confirmLeg.method} ${confirmLeg.route} http_status=${confirmLeg.http_status}; ` +
          `save http_status=${saveLeg.http_status}; itinerary text=${rendered.slice(0, 90)}`;
      }
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C3", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(state, failure ?? "C3 failed").toBe("PASS");
  });

  test("C4 打卡（手机入口未接上则具名 NOT_REACHED_UI）", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/trips → 行程 子页";
    try {
      await page.goto("/v8/trips");
      await tap(page, "C4", page.getByTestId("trips-tab-itinerary"));
      const control = page.getByTestId("stop-checkin").first();
      await control.waitFor({ timeout: 20_000 });
      hit = await centreHit(page, control);
      if (hit.hittable !== true) {
        state = "NOT_REACHED_UI";
        failure = `the 打卡 control exists but its own centre does not answer at 390 px: ${JSON.stringify(hit)}`;
      } else {
        const stampLeg: Leg = { name: "checkin-stamp", method: "POST", route: "/api/v1/journeys/", http_status: "NOT_SEEN" };
        legs.push(stampLeg);
        await waitLeg(page, stampLeg, async () => {
          await control.click();
        });
        await page.getByTestId("stop-checkin-notice").first().waitFor({ timeout: 20_000 });
        rendered = await inner(page, '[data-testid="stop-checkin-notice"]');
        state = stampLeg.http_status === 201 || stampLeg.http_status === 200 ? "PASS" : "FAIL";
        if (state === "FAIL") failure = `checkin-stamp POST /api/v1/journeys/.../checkins http_status=${stampLeg.http_status}`;
      }
    } catch (error) {
      const message = String(error);
      if (message.includes("stop-checkin") || message.includes("centre hit refused")) {
        state = "NOT_REACHED_UI";
        failure = `no reachable 打卡 entry on the phone surface: ${message.slice(0, 140)}`;
      } else {
        failure = message.slice(0, 200);
      }
    }
    record({ cell: "C4", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    /* NOT_REACHED_UI is an accepted outcome for this cell -- and only for this cell. It is never
       allowed to become a pass: the chain verdict in C7 says INCOMPLETE C4:NOT_REACHED_UI. */
    expect(["PASS", "NOT_REACHED_UI"], failure ?? "").toContain(state);
  });

  test("C5 足迹屏读到那一枚", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/footprints";
    try {
      const readLeg: Leg = { name: "footprint-read", method: "GET", route: "/checkins", http_status: "NOT_SEEN" };
      legs.push(readLeg);
      await waitLeg(page, readLeg, async () => {
        await page.goto("/v8/footprints");
      });
      await page.getByTestId("footprint-panel").waitFor({ timeout: 20_000 });
      hit = await centreHit(page, page.getByTestId("footprint-panel"));
      if (await page.getByTestId("footprint-row").count()) {
        rendered = await inner(page, '[data-testid="footprint-tally"]');
        const rows = await inner(page, '[data-testid="footprint-row"]');
        rendered = `${rendered} / ${rows}`;
        state = readLeg.http_status === 200 ? "PASS" : "FAIL";
      } else {
        rendered = await inner(page, '[data-testid="footprint-empty"]');
        state = "NOT_REACHED_UI";
        failure = `the footprint screen answers with the empty state, so C4 never produced a stamp to read: ${rendered.slice(0, 80)}`;
      }
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C5", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(["PASS", "NOT_REACHED_UI"], failure ?? "").toContain(state);
  });

  test("C6 长图导出成图（屏上字节 == 成图 IHDR 与文件长）", async ({ page }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/trips → 长图";
    const skip = process.env.CITYNEXUS_ORD0502_MUTATE === "drop_export";
    try {
      await page.goto("/v8/trips");
      const entry = page.getByTestId("poster-open");
      const entryShown = await entry
        .waitFor({ timeout: 20_000 })
        .then(() => true)
        .catch(() => false);
      if (!entryShown) {
        /* The same discipline C4 applies to 打卡: a phone that has no entry point is named
           NOT_REACHED_UI with the sentence the screen does show, and the GUARD still keeps the chain off
           green for it.  It is not a 420 s locator timeout pretending to be a product verdict. */
        state = "NOT_REACHED_UI";
        rendered = (await page.locator(".v8-muted, .v10-muted").first().innerText({ timeout: 8_000 }).catch(() => ""))
          .replace(/\s+/g, " ")
          .trim();
        failure = `no 长图 entry on this phone surface: no poster-open control for this journey${
          rendered ? `; the screen says ${rendered.slice(0, 90)}` : ""
        }`;
      } else {
        hit = await tap(page, "C6", entry);
        await page.getByTestId("trip-poster").waitFor({ timeout: 30_000 });
        const download = page.waitForEvent("download", { timeout: 120_000 });
        hit = await tap(page, "C6", page.getByTestId("poster-download"));
        if (skip) {
          /* Counter-example knob: the export is deliberately never consumed, so any assertion that
             would accept "the bar is there" instead of "the file exists" has to go red here. */
          throw new Error("C6: export was dropped by the mutation knob");
        }
        const file = await (await download).path();
        const buffer = readFileSync(file);
        const width = buffer.readUInt32BE(16);
        const height = buffer.readUInt32BE(20);
        await page.getByTestId("poster-result").waitFor({ timeout: 30_000 });
        rendered = await inner(page, '[data-testid="poster-result"]');
        const statedBytes = Number((rendered.match(/(\d[\d,]*)\s*字节/) ?? [])[1]?.replace(/,/g, ""));
        const statedSize = (rendered.match(/(\d+)\s*×\s*(\d+)\s*像素/) ?? []).slice(1).map(Number);
        const checks = [
          statedBytes === buffer.length ? null : `bytes: screen says ${statedBytes}, file is ${buffer.length}`,
          statedSize[0] === width ? null : `width: screen says ${statedSize[0]}, IHDR says ${width}`,
          statedSize[1] === height ? null : `height: screen says ${statedSize[1]}, IHDR says ${height}`,
        ].filter(Boolean) as string[];
        state = checks.length === 0 ? "PASS" : "FAIL";
        failure = checks.length ? `C6 legs ${checks.join("; ")}` : null;
        rendered = `${rendered} (IHDR ${width}x${height}, file ${buffer.length} B)`;
      }
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    record({ cell: "C6", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(["PASS", "NOT_REACHED_UI"], failure ?? "C6 failed").toContain(state);
  });

  test("C7b 分享 token 在第二个上下文里打开", async ({ browser, page, context }) => {
    const legs: Leg[] = [];
    let rendered = "";
    let hit: Record<string, unknown> = {};
    let failure: string | null = null;
    let state: Reading["state"] = "FAIL";
    const route = "/v8/access → /v8/shared/<token>";
    try {
      await page.goto("/v8/access");
      await page.getByTestId("access-invite-submit").waitFor({ timeout: 20_000 });
      const grantLeg: Leg = { name: "share-grant", method: "POST", route: "/share-grants", http_status: "NOT_SEEN" };
      legs.push(grantLeg);
      await waitLeg(page, grantLeg, async () => {
        hit = await tap(page, "C7b", page.getByTestId("access-invite-submit"));
      });
      /* Wait for the panel's OWN roster reload to put a grant row on the screen before looking for a
         token in it: `issueInvitation` awaits `afterWrite` -> `loadRoster` (`AccessPanel.tsx`,
         `:278`), and the previous shape of this cell scanned the folds the moment the POST answered,
         so it found only the member/role folds and reported a token that had not yet arrived. */
      const rowShown = await page
        .getByTestId("access-grant-row")
        .first()
        .waitFor({ timeout: 20_000 })
        .then(() => true)
        .catch(() => false);
      if (!rowShown) {
        const emptySaid = (await inner(page, '[data-testid="access-grant-empty"]').catch(() => "")).slice(0, 90);
        throw new Error(
          `C7b: POST /share-grants answered http_status=${grantLeg.http_status} but no grant row reached the screen; ` +
            `the panel says: ${emptySaid || "（无）"}`,
        );
      }
      /* The token is inside THAT row's own fold (`AccessPanel.tsx` -- the `p` interpolates
         `grant.token`), and the same `cn-access-details` class is used by the member/role folds, so
         the scan is scoped to the grant row rather than to the first fold on the page. */
      const row = page.getByTestId("access-grant-row").first();
      const folds = await row.locator("details.cn-access-details").count();
      let token = "";
      const seen: string[] = [];
      for (let index = 0; index < folds && token.length === 0; index += 1) {
        const fold = row.locator("details.cn-access-details").nth(index);
        await fold.locator("summary").click().catch(() => undefined);
        const text = (await fold.locator("p").first().innerText({ timeout: 5_000 }).catch(() => ""))
          .replace(/\s+/g, " ")
          .trim();
        seen.push(text.slice(0, 44));
        token = (text.match(/[A-Za-z0-9_-]{20,}/) ?? [])[0] ?? "";
      }
      if (token.length === 0) {
        throw new Error(
          `no token reached the screen; the grant row's ${folds} fold(s) say: ${seen.join(" / ").slice(0, 160) || "(none)"}`,
        );
      }
      /* A second DEVICE, not a second tab: `browser.newContext()` inherits this suite's `test.use`,
         so without an explicit empty state the "guest" would arrive holding the owner's journey in
         localStorage and the token hand-over would be proven by the wrong thing. */
      const guest = await browser.newContext({
        viewport: PHONE,
        locale: "zh-CN",
        serviceWorkers: "block",
        storageState: { cookies: [], origins: [] },
      });
      const guestPage = await guest.newPage();
      const readLeg: Leg = { name: "share-read", method: "GET", route: "/api/v1/shared/journeys/", http_status: "NOT_SEEN" };
      legs.push(readLeg);
      await waitLeg(guestPage, readLeg, async () => {
        await guestPage.goto(`/v8/shared/${token}`);
      });
      await guestPage.getByTestId("shared-screen").or(guestPage.getByTestId("shared-refused")).waitFor({ timeout: 30_000 });
      /* The verdict is taken from what the SECOND context paints.  Reading the owner's own panel
         would say nothing about whether a stranger holding the token got in. */
      if (await guestPage.getByTestId("shared-screen").count()) {
        rendered = await inner(guestPage, '[data-testid="shared-screen"]');
        /* The grant bar follows the contract that landed at this tip, not the walk's old 201-only
           guess: creation answers 201 and a reused grant answers 200
           (`backend/tests/test_qoder_ord0541_share_grant_reuse_r1.py` create, `:99` reuse).  This
           walk measured 200 because a grant for the journey already existed before 生成邀请 was
           pressed -- that pre-existing grant is itself a reading and is named in `failure`. */
        const grantOk = grantLeg.http_status === 201 || grantLeg.http_status === 200;
        state = grantOk && readLeg.http_status === 200 && rendered.length > 0 ? "PASS" : "FAIL";
      } else {
        rendered = await inner(guestPage, '[data-testid="shared-refusal-code"]');
        state = "FAIL";
      }
      if (state === "FAIL") {
        failure = `share-grant POST /share-grants http_status=${grantLeg.http_status}; share-read GET /api/v1/shared/journeys/ http_status=${readLeg.http_status}; the guest's own screen says ${rendered.slice(0, 60)}`;
      }
      await guest.close();
    } catch (error) {
      failure = String(error).slice(0, 200);
    }
    void context;
    record({ cell: "C7b", state, route, viewport_px: page.viewportSize()?.width ?? 0, rendered, hit, legs, failure });
    expect(state, failure ?? "C7b failed").toBe("PASS");
  });

  /* Declared LAST because it judges the whole walk: it reads the same `readings` array every cell
     above appends to, so in the previous position it ran before any cell existed and its own
     "the walk never recorded C1" was the walk's first red -- a ruler artefact, not a product one. */
  test("GUARD the walk cannot claim green it did not earn", async () => {
    mergePriorReadings();
    const seen = readings.map((r) => r.cell);
    for (const cell of ["C1", "C2", "C2b", "C3", "C4", "C5", "C6", "C7b"]) {
      expect(seen, `the walk never recorded ${cell}`).toContain(cell);
    }
    const lying = readings.filter(
      (r) => r.state === "PASS" && (r.rendered.length === 0 || r.route.length === 0),
    );
    expect(lying, "a cell claimed PASS with no rendered text or no route").toEqual([]);
    const s = summary();
    expect(
      s.chain === "GREEN" || s.chain.startsWith("INCOMPLETE"),
      `chain verdict must name the broken cell: ${s.chain}`,
    ).toBe(true);
    if (s.chain !== "GREEN") {
      console.log(`[ord0502] chain is NOT green, so this run is reported as partial: ${s.chain}`);
    }
  });
});
