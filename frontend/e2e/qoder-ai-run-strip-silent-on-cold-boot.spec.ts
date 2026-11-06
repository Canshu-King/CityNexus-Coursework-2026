/**
 * ORD-<待分配> — the run strip stays silent on a cold boot, and a stale record cannot wake it.
 *
 * WHY THIS FILE EXISTS. ORD-0382 (`3e75ab2b`, 2026-09-28 04:17:34 +0800) landed 1,372 lines:
 * `aiGenerationRun.ts` (735), `PlanAiRunStrip.tsx` (118), a rewrite of PlanWithAiSheet (849 changed)
 * and 61 new dictionary keys. It gives 生成中 its own surface above the router, with two doors --
 * 查看会话 (`plan-ai-run-chat`) and 继续生成 (`plan-ai-run-resume`) -- exactly the shape the reference
 * product ships as `activity_journey_planning_waiting`. Measured at the tip `db6a04cb`:
 *
 *   grep -rl "plan-ai-run-strip|plan-ai-run-line|plan-ai-run-chat|plan-ai-run-resume" e2e/  ->  0 files
 *   grep -rl "aiRun[A-Za-z]+|aiGenerationRun"                                        e2e/  ->  0 files
 *   git show --stat 3e75ab2b                                                              ->  no spec among 6 files
 *
 * So the surface itself has no gate. The reason is structural, not laziness: authoritative mode
 * refuses to start unless `CITYNEXUS_AI_ENABLED=false` and no AI key is present in the environment
 * (`playwrightEnv.ts`), so a run can never be started inside the acceptance run. This file does NOT
 * pretend to cover that face -- see NOT_COVERED below, and do not let a green here be quoted as
 * "the strip is tested".
 *
 * WHAT IS ACTUALLY AT STAKE, and why this is not a vacuous zero.
 * `unread` and `running` live in memory only: `aiGenerationRun.ts` initialises `unread: false` (192),
 * sets it at 228 and 514, and the file contains no localStorage/sessionStorage access at all. That is
 * what keeps the strip from reappearing after a reload pointing at a session that no longer exists --
 * a banner that says "生成中" over a dead run is worse than no banner. Nothing anywhere pins that,
 * so the next person who "helpfully" persists run state to survive a refresh would silently ship the
 * zombie, and every current gate would stay green.
 *
 * Arms:
 *   A1  cold boot on a screen that genuinely offers planning: `hub-plan-with-ai` is present and
 *       strikable (the positive control that makes the zero mean something), and all four strip
 *       testids count to 0. On two screens, Today and Live.
 *   A2  stale-record injection: run-shaped keys are written to localStorage, the injection itself is
 *       read back and reported (so the arm cannot pass by having failed to inject), and the strip
 *       still counts 0. If anyone moves run state into storage, this arm goes red -- that is the job.
 *
 * NOT_COVERED, stated instead of faked: while a run is actually in flight, whether the strip renders,
 * whether `plan-ai-run-chat` appears off the `agent` route (V8Experience.tsx passes it as
 * undefined precisely there, on purpose), and whether the seconds/phase row update. Those need an
 * AI-enabled run, which authoritative mode forbids by design. A lane that wants them must ask the
 * commander for a named, non-authoritative run rather than mock the state into existence.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ai-run-strip-silent-on-cold-boot.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* The strip and everything inside it. Spelled out rather than prefix-matched: a wildcard would also
   match nothing and would pass for the wrong reason if the prefix ever changed. */
const STRIP_IDS = [
  "plan-ai-run-strip",
  "plan-ai-run-line",
  "plan-ai-run-chat",
  "plan-ai-run-resume",
] as const;

/* Run-shaped keys a future persistence layer would plausibly use. Writing several is deliberate: the
   arm is "nothing revives the strip", so it must not depend on guessing one exact key name. */
const INJECTED: Record<string, string> = {
  "citynexus.airun": JSON.stringify({ running: true, journeyId: "*", phase: "request" }),
  "citynexus.ai.run": JSON.stringify({ running: true, unread: true }),
  "citynexus.v8.airun": JSON.stringify({ running: true, unread: true, watched: false }),
};

/* Per screen: the id that proves the screen mounted, and the id that proves getByTestId can count
   something non-zero *on that screen*. They are different on purpose. `hub-plan-with-ai` lives only in
   Today's quiet row, so using it as the control on Live would fail for a reason that has nothing to do
   with the strip -- and a control that fails for the wrong reason trains everyone to ignore it. */
const SCREENS = [
  {
    name: "today",
    path: "/v8/hub",
    ready: "today-hero",
    control: "hub-plan-with-ai",
    control_is_product_entry: true,
  },
  {
    name: "live",
    path: "/v8/live",
    ready: "v8-live-journey",
    control: "v8-live-journey",
    control_is_product_entry: false,
  },
] as const;

async function boot(page: Page, journeyId: string, path: string, readyId: string, inject: Record<string, string> = {}) {
  const seed = { ...inject, "citynexus.v8.journey_id": journeyId, "citynexus.locale": "zh-CN" };
  await page.addInitScript((entries) => {
    for (const [k, v] of Object.entries(entries)) localStorage.setItem(k, v);
  }, seed);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(path, { waitUntil: "load" });
  await expect(page.getByTestId(readyId), node("boot", `${path} never rendered`)).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(600);
}

async function readStorage(page: Page) {
  return page.evaluate(() => {
    const keys: Record<string, string> = {};
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k) keys[k] = String(localStorage.getItem(k)).slice(0, 80);
    }
    return keys;
  });
}

async function countStrip(page: Page) {
  const out: Record<string, number> = {};
  for (const id of STRIP_IDS) out[id] = await page.getByTestId(id).count();
  return out;
}

const readings: Record<string, unknown> = {};

test.afterEach(({}, info) => {
  const arm = /^(A\d)/.exec(info.title)?.[1] ?? info.title.slice(0, 2);
  const prior = (readings[arm] ?? {}) as Record<string, unknown>;
  const body: Record<string, unknown> = { ...prior };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 700);
  }
  writeFileSync(
    evidencePath(`runstrip/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

test("A1_cold_boot_leaves_the_strip_silent_on_a_screen_that_really_offers_planning — 真给了规划入口的屏上，等待条四枚必须都是 0", async ({ page }) => {
  test.setTimeout(240_000);
  const arm = "A1";
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey, so the strip's own mount condition cannot be evaluated")).toBe(true);

  const perScreen: Record<string, unknown> = {};
  for (const s of SCREENS) {
    await boot(page, id, s.path, s.ready);
    /* The positive control, and it has to be the same mechanism: getByTestId counted this id on this
       screen right now. Without it, "0 strip ids" is indistinguishable from a typo in the id string or
       a page that never mounted. */
    const control = page.getByTestId(s.control);
    const controlCount = await control.count();
    const storage = await readStorage(page);
    const counts = await countStrip(page);
    perScreen[s.name] = {
      path: s.path,
      ready_id: s.ready,
      control_testid: s.control,
      control_count: controlCount,
      strip_counts: counts,
      strip_total: Object.values(counts).reduce((a, b) => a + b, 0),
      storage_keys: Object.keys(storage).sort(),
      scroll_y: await page.evaluate(() => Math.round(window.scrollY)),
    };
    await page.screenshot({ path: evidencePath(`runstrip/${RUN}__A1_${s.name}.png`) });
    /* A screen where planning is genuinely on offer must show it, or the zero measures nothing. */
    expect(controlCount, node(arm, `${s.name}: control testid ${s.control} counted ${controlCount} -- either this screen never mounted the surface under test, or getByTestId cannot see ids here at all, and a 0 strip count would be meaningless`)).toBeGreaterThan(0);
    if (s.control_is_product_entry) {
      expect(await control.first().isVisible(), node(arm, `${s.name}: the planning entry exists but is not visible`)).toBe(true);
    }
  }
  readings[arm] = { journey_id: id, screens: perScreen };

  for (const [name, v] of Object.entries(perScreen)) {
    const row = v as { strip_counts: Record<string, number>; strip_total: number };
    expect(row.strip_total, node(arm, `${name}: the strip spoke on a cold boot (${JSON.stringify(row.strip_counts)}) -- nothing ran, so nothing may be reported`)).toBe(0);
  }
});

test("A2_a_stale_run_record_in_storage_does_not_wake_the_strip — 注入陈旧 run 记录后冷启动，等待条仍必须沉默，且注入本身要回读为证", async ({ page }) => {
  test.setTimeout(240_000);
  const arm = "A2";
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node(arm, "the stage named no journey")).toBe(true);

  /* Inject on Today, then also check Live: a persistence layer would be read wherever the app boots. */
  const rows: Record<string, unknown> = {};
  for (const s of SCREENS) {
    await boot(page, id, s.path, s.ready, INJECTED);
    const storage = await readStorage(page);
    const counts = await countStrip(page);
    const controlCount = await page.getByTestId(s.control).count();
    /* Prove the injection landed before asserting anything about its effect. This is the whole reason
       the arm is worth having: an injection that silently failed would produce a green that means
       exactly nothing. */
    const present = Object.keys(INJECTED).filter((k) => k in storage);
    expect(present.length, node(arm, `${s.name}: none of the injected run-shaped keys reached localStorage (wrote ${JSON.stringify(Object.keys(INJECTED))}, found ${JSON.stringify(Object.keys(storage))}) -- this arm did not measure what it claims to`)).toBeGreaterThan(0);
    expect(controlCount, node(arm, `${s.name}: control ${s.control} counted ${controlCount} after injection, so this screen is not the surface claimed`)).toBeGreaterThan(0);
    rows[s.name] = {
      path: s.path,
      injected_keys_present: present,
      injected_values_read_back: Object.fromEntries(present.map((k) => [k, storage[k]])),
      strip_counts: counts,
      strip_total: Object.values(counts).reduce((a, b) => a + b, 0),
      control_count: controlCount,
    };
    await page.screenshot({ path: evidencePath(`runstrip/${RUN}__A2_${s.name}.png`) });
  }
  readings[arm] = { journey_id: id, injected_key_names: Object.keys(INJECTED), screens: rows };

  for (const [name, v] of Object.entries(rows)) {
    const row = v as { strip_counts: Record<string, number>; strip_total: number };
    expect(row.strip_total, node(arm, `${name}: a stored run record revived the strip (${JSON.stringify(row.strip_counts)}) -- the strip would now advertise a session that no longer exists`)).toBe(0);
  }
});
