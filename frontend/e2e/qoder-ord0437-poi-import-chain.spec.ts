import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0437 — the paste-a-note door of the POI entry chain.
 *
 * Every assertion below reads the sentence that is actually painted. `data-phase` is recorded as a
 * reading, never used as the judge: a card whose text still says 正在读 while the attribute says
 * success is the bug this file exists to catch.
 *
 * What the arms grade, in the order the shapes were asked for:
 *   day-before   — the automatic landing is disclosed before any press, and the day named there is
 *                  the day the write reports.
 *   screenshot   — the second input mode is answered by a sentence that says it is not in this
 *                  build, not by a control that quietly does nothing.
 *   link-named   — a pasted link comes back named (form + host) and unread; the text around it is
 *                  still parsed, so "we do not fetch" cannot degrade into "we found nothing".
 *   three-states — loading, error and success occupy one region and 重试 stays mounted in the
 *                  error arm and the success arm. The error arm is reached by aborting the parse
 *                  request (transport fault, no fabricated body), and that is written into the
 *                  reading so nobody mistakes it for a provider outage.
 *   verify       — the 核对警告 sits above the matched list for as long as the list is on screen.
 *   added        — 已加入 only appears once the record is read back from the server, and it survives
 *                  a reload, which client state cannot fake.
 *   replay       — a second tap on the same selection reports 本来就在 rather than a second success.
 *   exits        — the three destinations, as of ORD-0524 one exclusive radiogroup: a parse selects
 *                  nothing, 全选可加的行 is the control that does, 加进计划 and 收藏 each own their door
 *                  and lose it the moment the other is chosen, and 只存不排 is presented and refused in
 *                  painted text rather than in a tooltip a touch screen never shows.
 */

const FILE = "e2e/qoder-ord0437-poi-import-chain.spec.ts";
type Reading = Record<string, unknown>;
const rows: Reading[] = [];
const push = (arm: string, v: Reading) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

/* Where an arm's seconds actually go. The suite reported a 3.0 m timeout on `day-before` after its
   graded cells had already printed, with no locator call log, so the budget was being spent
   somewhere other than an assertion. The steps are named, and the last line is recorded when the
   body returns -- a small body_done against a 3.0 m report puts the seconds in the fixtures, not in
   this file's awaits. */
let armStart = 0;
const stepTimes: Record<string, number> = {};
const mark = (name: string) => {
  stepTimes[name] = armStart === 0 ? 0 : Date.now() - armStart;
  return stepTimes[name];
};

test.beforeEach(() => {
  armStart = Date.now();
});

test.afterEach(() => {
  const done = Date.now() - armStart;
  console.log(`${FILE} :: timing — ${JSON.stringify({ body_done_ms: done, steps: stepTimes })}`);
  Object.keys(stepTimes).forEach((k) => delete stepTimes[k]);
});

/* Both readers below carry an explicit short timeout, and that is not decoration.
   Measured on run r6: `day-before` finished its own steps at 3 898 ms (`after_reading_disclosure`)
   and the body ended at 180 406 ms (`after_reading_phase`) -- 176.5 s of it inside one
   `getAttribute` on a card that does not exist before a press. Playwright auto-waits a missing
   locator until the test deadline, and `.catch(() => null)` only converts the rejection that
   arrives *after* that wait, so "not on screen yet" was being reported as a 3-minute answer.
   `added and replay` died the same way on `poi-import-readback-failed`, which is absent by design
   when the read-back succeeded. The arms that read a present element stayed green throughout. */
const painted = (page: Page, testId: string) =>
  page.getByTestId(testId).first().innerText({ timeout: 2_000 })
    .then((t) => t.replace(/\s+/g, " ").trim()).catch(() => null);

/* ORD-0463 asks for coordinates, not only words. One page.evaluate over the named ids: a locator-based
   reader would auto-wait on a cell that is absent by design (that trap cost 176.5 s in arm day-before),
   whereas this returns null for whatever is not on screen right now. Rects are viewport pixels from
   getBoundingClientRect, with width and height, so a later reader can re-check the placement itself. */
const rectsOf = (page: Page, ids: string[]) =>
  page.evaluate((want) => {
    const out: Record<string, { top: number; left: number; w: number; h: number } | null> = {};
    for (const id of want) {
      const el = document.querySelector(`[data-testid="${id}"]`);
      if (!el) {
        out[id] = null;
        continue;
      }
      const b = el.getBoundingClientRect();
      out[id] = { top: Math.round(b.top), left: Math.round(b.left), w: Math.round(b.width), h: Math.round(b.height) };
    }
    return out;
  }, ids);

const phaseOf = (page: Page) =>
  page.locator("[data-testid='poi-import-card']").first()    .getAttribute("data-phase", { timeout: 2_000 }).catch(() => null);

/** The plan as the server holds it, read straight off the projection. Used wherever the arm has to
 * tell "the component said 已加入" apart from "the record is on the plan". */
const planTitles = (page: Page) =>
  page.evaluate(async () => {
    const id = localStorage.getItem("citynexus.v8.journey_id");
    if (!id) return [] as string[];
    const body = await (await fetch(`/api/v1/journeys/${id}/experience`)).json();
    return (body?.active_plan?.activities ?? []).map((a: { title: string }) => a.title) as string[];
  });

async function enterImport(page: Page): Promise<void> {
  await ensureDemoJourney(page);
  await page.goto("/v8/import", { waitUntil: "load" });
  await expect(page.getByTestId("poi-import").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_200);
}

/** The plan has to exist for a place to land on: the screen's own neighbour does that work. */
async function ensureActivePlan(page: Page): Promise<string> {
  const hasPlan = await page.evaluate(async () => {
    const id = localStorage.getItem("citynexus.v8.journey_id");
    if (!id) return "no-journey-id";
    const body = await (await fetch(`/api/v1/journeys/${id}/experience`)).json();
    return (body?.active_plan?.activities?.length ?? 0) > 0 ? "already" : "no-active-plan";
  });
  if (hasPlan !== "no-active-plan") return hasPlan;
  await page.evaluate(async () => {
    const id = localStorage.getItem("citynexus.v8.journey_id");
    if (!id) return;
    const plans = await (
      await fetch(`/api/v1/journeys/${id}/plans/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_count: 1 }),
      })
    ).json();
    if (plans?.[0]?.id) {
      await fetch(`/api/v1/journeys/${id}/plans/${plans[0].id}/select`, { method: "POST" });
    }
  });
  await page.goto("/v8/import", { waitUntil: "load" });
  await page.waitForTimeout(1_500);
  return "generated";
}

/** Build a second journey the way the wizard does, in a city that has room left to import into.
 *
 * The seeded city has none. Measured on this seed's database: CN-TIANJIN holds 9 place rows, of which
 * 6 are schedule nodes (arrival / check-in / stay / check-out / departure) and the remaining 3 --
 * Five Great Avenues, Tianjin Museum, Tianjin Eye -- are all already on the generated plan. So every
 * name this door can match is a name the day already carries, and an "added" arm written there could
 * only ever report 本来就在. CNX-DEMO has 8 visitable rows against a 6-stop plan, which is the slack
 * the arm needs.
 *
 * The app is pointed at the new journey through its own storage key (`citynexus.v8.journey_id`,
 * written by V8Experience.tsx when a traveller creates a trip), then reloads. Nothing here
 * touches the database directly. */
async function useSlackJourney(page: Page): Promise<{ id: string; seen: unknown }> {
  const made = await page.evaluate(async () => {
    const fetched = await (await fetch("/api/v1/journeys")).json();
    const list: { traveler_id?: string; id?: string }[] = Array.isArray(fetched)
      ? fetched
      : ((fetched as { items?: { traveler_id?: string }[] })?.items ?? []);
    const traveler = list.map((row) => row?.traveler_id).find(Boolean) ?? null;
    if (!traveler) {
      return { error: "no-traveler-id", journeys: list.length, firstKeys: Object.keys(list[0] ?? {}) };
    }
    const created = await fetch("/api/v1/journeys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        traveler_id: traveler,
        title: "ORD-0437 import slack",
        city_code: "CNX-DEMO",
        timezone: "Asia/Shanghai",
        start_at: "2026-12-05T09:00:00+08:00",
        end_at: "2026-12-07T18:00:00+08:00",
        budget: { amount_minor: 300000, currency: "CNY" },
        planning_mode: "balanced",
      }),
    });
    if (created.status !== 201) {
      return { error: "journey-create", status: created.status, body: (await created.text()).slice(0, 200) };
    }
    const journey = (await created.json()) as { id: string; city_code?: string };
    const generated = await fetch(`/api/v1/journeys/${journey.id}/plans/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_count: 1 }),
    });
    const plans = generated.ok ? await generated.json() : [];
    if (Array.isArray(plans) && plans[0]?.id) {
      await fetch(`/api/v1/journeys/${journey.id}/plans/${plans[0].id}/select`, { method: "POST" });
    }
    localStorage.setItem("citynexus.v8.journey_id", journey.id);
    return {
      id: journey.id,
      city: journey.city_code ?? null,
      generate_status: generated.status,
      candidates: Array.isArray(plans) ? plans.length : 0,
    };
  });
  expect(
    (made as { error?: string }).error,
    `the slack journey has to be built through the product's own doors: ${JSON.stringify(made)}`,
  ).toBeUndefined();
  await page.goto("/v8/import", { waitUntil: "load" });
  await expect(page.getByTestId("poi-import").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_200);
  return { id: String((made as { id?: string }).id ?? ""), seen: made };
}

/** Two place names from the city this journey actually lives in, plus a line that cannot match.
 * The paste text is built from the catalogue the parser is allowed to answer from, so the arm tests
 * a real match instead of betting on one demo city's names. What the look-up actually saw is part of
 * the reading: an empty list has to be distinguishable from a wrong city code at a glance.
 *
 * `include_structural=true` is asked for on purpose. Measured on this build: without it
 * `GET /api/v1/places?city_code=CN-TIANJIN` answers 0 rows, because every one of that city's 9 rows
 * carries a provider_key the read route filters out. The gate wants the raw table, then removes the
 * schedule nodes itself, so it is an independent reading of what should be addable rather than a
 * second copy of the product's own filter. */
const STRUCTURAL_CATEGORIES = [
  "arrival", "departure", "check_in", "check_out", "stay", "luggage_drop", "transit",
];

async function pasteText(page: Page): Promise<{ text: string; names: string[]; why: unknown }> {
  /* The list is passed in as an argument: a page function is serialized and cannot read module
     scope, so reaching for a constant from inside it would throw in the browser, not here. */
  const seen = await page.evaluate(async (schedule) => {
    const id = localStorage.getItem("citynexus.v8.journey_id");
    if (!id) return { id: null, city: null, rows: 0, names: [] as string[], titles: [] as string[] };
    const experience = await (await fetch(`/api/v1/journeys/${id}/experience`)).json();
    const city = experience?.journey?.city_code ?? null;
    let rows: { name: string; category: string; provider_key?: string }[] = [];
    if (city) {
      const fetched = await (
        await fetch(`/api/v1/places?city_code=${encodeURIComponent(city)}&include_structural=true`)
      ).json();
      rows = Array.isArray(fetched) ? fetched : (fetched?.items ?? []);
    }
    const titles = (experience?.active_plan?.activities ?? []).map((a: { title: string }) => a.title);
    const isSchedule = (category: string) => schedule.includes((category ?? "").toLowerCase());
    const addable = rows.filter((r) => !isSchedule(r.category) && !titles.includes(r.name));
    return {
      id,
      city,
      rows: rows.length,
      providers: Array.from(new Set(rows.map((r) => r.provider_key ?? ""))),
      categories: Array.from(new Set(rows.map((r) => r.category))),
      dropped_as_schedule: rows.filter((r) => isSchedule(r.category)).map((r) => r.name),
      dropped_as_already_on_plan: rows.filter((r) => titles.includes(r.name)).map((r) => r.name),
      names: addable.slice(0, 2).map((r) => r.name),
      titles: titles.slice(0, 2),
    };
  }, STRUCTURAL_CATEGORIES);
  expect(seen.id, "the seeded journey pointer has to be in the session for this arm to run").toBeTruthy();
  const names = seen.names as string[];
  expect(names.length, `the city catalogue gave nothing addable back: ${JSON.stringify(seen)}`)
    .toBeGreaterThanOrEqual(2);
  return {
    text: [names[0], names[1], "https://mp.weixin.qq.com/s/ORD0437NOTE 这条链接没有抓", "咕咕镇不存在这个地方"].join("\n"),
    names,
    why: seen,
  };
}

async function parse(page: Page, text: string): Promise<void> {
  await page.getByTestId("poi-import-text").fill(text);
  await page.getByTestId("poi-import-parse").click();
  await expect(page.getByTestId("poi-import-card")).toBeVisible({ timeout: 20_000 });
}

test("day-before — the automatic landing is said before anything is pressed", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  mark("after_viewport");
  await enterImport(page);
  mark("after_enter_import");
  test.setTimeout(180_000);
  const planState = await ensureActivePlan(page);
  mark("after_ensure_active_plan");
  const disclosure = await painted(page, "poi-import-day-disclosure");
  mark("after_reading_disclosure");
  const scopeHint = await painted(page, "poi-import-scope-hint");
  const phase = await phaseOf(page);
  mark("after_reading_phase");
  push("day-before", { disclosure, scopeHint, cardPhaseBeforeAnyPress: phase, planState, steps: { ...stepTimes } });
  expect(disclosure, "the day the import would land on has to be on screen before the press").not.toBeNull();
  expect(disclosure!.length).toBeGreaterThan(0);
  expect(scopeHint, "what this door can read is said before the paste, not after a refusal").toBeTruthy();
  // the card region only exists once the traveller has asked for something
  expect(phase, "no result card before a press").toBeNull();
});

test("screenshot-mode — the second input form is refused in words", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterImport(page);
  await page.getByTestId("poi-import-mode-screenshot").click();
  await page.waitForTimeout(400);
  const words = await painted(page, "poi-import-mode-unsupported");
  const rects = await rectsOf(page, ["poi-import-mode-unsupported", "poi-import-mode-screenshot"]);
  await page.screenshot({ path: evidencePath("ord0437", "screenshot_mode.png") });
  push("screenshot-mode", { words, rects });
  expect(words, "a mode that is not built has to say so").not.toBeNull();
  expect(words).toMatch(/截图|screenshot/i);
});

test("link-named and states — a link is named while the text next to it is still read", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const seen: string[] = [];
  page.on("console", () => undefined);
  await enterImport(page);
  test.setTimeout(180_000);
  const slack = await useSlackJourney(page);
  const planState = await ensureActivePlan(page);
  const watch = setInterval(async () => {
    try {
      const p = await phaseOf(page);
      if (p && seen[seen.length - 1] !== p) seen.push(p);
    } catch {
      /* the page is mid-navigation */
    }
  }, 40);
  const built = await pasteText(page);
  await parse(page, built.text);
  await page.waitForTimeout(1_800);
  clearInterval(watch);
  const phase = await phaseOf(page);
  const counts = await painted(page, "poi-import-counts");
  const unsupported = await painted(page, "poi-import-unsupported");
  const warning = await painted(page, "poi-import-verify-warning");
  const retry = await painted(page, "poi-import-retry");
  const cards = await page.getByTestId("poi-candidate-card").count();
  const statuses = await page.getByTestId("poi-candidate-card").evaluateAll((els) =>
    els.map((el) => el.getAttribute("data-poi-status")),
  );
  /* The hook the shared card contract is written on: ORD-0435's card addresses state through
     `[data-picked]` on the article and a `.v8-pick-box` control, so this counts what this screen
     actually carries rather than trusting that the class was typed. */
  const pickedHook = await page.getByTestId("poi-candidate-card").evaluateAll((els) => ({
    with_data_picked: els.filter((el) => el.getAttribute("data-picked") !== null).length,
    total: els.length,
  }));
  /* One pick box per *card*, which since ORD-0524 is not the same count as one pick box on screen: the
     select-all row is built from the same shared control, so it is named out of this count instead of
     inflating it into a claim that the cards carry more controls than they do. */
  const pickBoxes = await page.locator('.v8-pick-box:not(.poi-import-select-all)').count();
  const selectAllRow = await page.locator(".poi-import-select-all").count();
  const selectedCount = await painted(page, "poi-import-selection-count");
  /* The four cells ORD-0463 lists: how many were read, what was not read and why, the shared refusal
     sentence, and the second input form. Coordinates go with the words so the sheet can be re-taken. */
  const rects = await rectsOf(page, [
    "poi-import-counts", "poi-import-unsupported", "poi-import-empty", "poi-candidate-reason",
    "poi-import-verify-warning",
  ]);
  await page.screenshot({ path: evidencePath("ord0437", "success_arm.png") });
  push("link-named-and-states", {
    planState, slack: slack.seen, catalogue: built.why, phaseSeen: seen, finalPhase: phase, counts, unsupported, verifyWarning: warning,
    retryWords: retry, cards, statuses, selectedCount, pickedHook, pickBoxes, selectAllRow, rects,
  });
  expect(phase).toBe("success");
  expect(unsupported, "the link must come back named with its host, not swallowed").toContain("mp.weixin.qq.com");
  expect(counts).toMatch(/\d/);
  expect(Number((counts!.match(/(\d+)\s*行|(\d+) lines/) ?? []).slice(1).find(Boolean)),
    "the pasted lines were segmented").toBeGreaterThanOrEqual(3);
  expect(cards, "each recognised line becomes a card").toBeGreaterThanOrEqual(3);
  expect(statuses).toContain("UNRESOLVED");
  expect(statuses).toContain("RESOLVED");
  expect(warning, "the verification warning sits above the matched list while it is on screen").toBeTruthy();
  expect(retry, "重试 stays mounted in the success arm").toBeTruthy();
  expect(selectedCount).toMatch(/\d/);
  /* Measured first, then pinned: four cards came out of one paste and only two carried a pick control,
     because the two lines we could not match are not places. So the invariant is one control per
     pickable card and none anywhere else -- not one per card. A card you cannot pick has to say why in
     words instead of just being inert. */
  const pickable = statuses.filter((s) => s === "RESOLVED").length;
  expect(pickBoxes, "the shared .v8-pick-box control appears once per pickable card").toBe(pickable);
  /* Importing ORD-0435's sheet is only a claim until the paint is measured: at this 390 px width the
     line's own narrow-screen rule gives the control `min-height: var(--cn-touch-min)` = 44 px
     (design-tokens.css). A class that reached no rule would still count as one pick box, so the
     box is measured, not named. */
  const firstBox = await page.getByTestId("poi-card-select").first().boundingBox();
  /* Ticking one card is what makes the comparison honest: the panel now arrives with nothing selected
     (ORD-0524 ① removed the pre-tick that this arm used to record), so without this click there would be
     no *checked* control on screen to compare against, and a "differs" assertion would pass simply
     because one side was missing. The click is also the only place this gate grades that the tick
     actually answers. */
  await page.locator(".v8-pick-box").first().click();
  await page.waitForTimeout(600);
  const afterOneTicked = await painted(page, "poi-import-selection-count");
  const paint = await page.evaluate(() => {
    const read = (sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) return null;
      const s = getComputedStyle(el);
      return { border: s.borderColor, shadow: s.boxShadow };
    };
    return {
      pickedCard: read('.v8-stay-card[data-picked="true"]'),
      unpickedCard: read('.v8-stay-card[data-picked="false"]'),
      checkedBox: read(".v8-pick-box.checked"),
      plainBox: read(".v8-pick-box:not(.checked)"),
    };
  });
  push("link-named-paint", { firstBoxHeight: firstBox?.height ?? null, afterOneTicked, ...paint });
  expect(firstBox?.height ?? 0, "勾选控件在窄屏要有 44 px 的可点高度").toBeGreaterThanOrEqual(44);
  expect(afterOneTicked, "勾上一枚之后计数要跟着涨").toMatch(/1/);
  expect(paint.unpickedCard, "取消勾选的那张要留在屏上").not.toBeNull();
  expect(paint.plainBox, "取消勾选的那枚控件要留在屏上").not.toBeNull();
  /* "has some box-shadow" would be true even if the shared sheet never reached this screen, so the
     claim pinned here is the difference: a picked card must be painted differently from an unpicked
     one, and a checked box differently from a plain one. Those two pairs are exactly what
     ORD-0435's sheet writes twins for. */
  expect(paint.pickedCard?.border, "选中与未选的卡片必须看得出不同")
    .not.toEqual(paint.unpickedCard?.border);
  expect(paint.checkedBox?.border, "勾上与没勾的控件必须看得出不同")
    .not.toEqual(paint.plainBox?.border);
  /* The stronger half: the picked card must carry the shared sheet's inset accent line, which is a
     rule only `discovery-batch.css` writes. If a local class were still winning, the border would
     differ but this would be `none`. */
  expect(paint.pickedCard?.shadow, "选中态要由共享样式表那条内侧强调线来画").toContain("inset");
  expect(pickedHook.with_data_picked, "每张卡都带共享的 [data-picked] 钩子").toBe(pickedHook.total);
  const inertWithoutReason = await page.getByTestId("poi-candidate-card").evaluateAll((els) =>
    els.filter(
      (el) =>
        el.getAttribute("data-poi-status") !== "RESOLVED" &&
        !el.querySelector("[data-testid='poi-candidate-reason']"),
    ).length,
  );
  expect(inertWithoutReason, "a card with no pick control has to name why").toBe(0);
});

test("error-state — the same region carries the failure and keeps 重试", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterImport(page);
  test.setTimeout(180_000);
  const planState = await ensureActivePlan(page);
  /* Two refusal shapes, because the ruling this arm now grades is "the screen may not wear the
     answer's own sentence":
       (a) transport -- `route.abort`, so nothing answers at all and the machine token has to be ours;
       (b) a real named refusal -- 409 with `reason_code: NO_ACTIVE_PLAN`, where the sentence must
           still come from the dictionary and only the token and the status may be painted. */
  await page.route("**/poi-import/parse", (route) => route.abort());
  await parse(page, "Riverside Museum");
  await page.waitForTimeout(1_200);
  const phase = await phaseOf(page);
  const error = await painted(page, "poi-import-error");
  const transportDetail = await painted(page, "poi-import-error-detail");
  const retry = await painted(page, "poi-import-retry");
  const cardText = await painted(page, "poi-import-card");
  await page.screenshot({ path: evidencePath("ord0437", "error_arm.png") });
  await page.unroute("**/poi-import/parse");

  /* Arm (b) copies the door's own envelope: the backend test reads
     `response.json()["error"]["reason_code"]`, so the refusal lives at the top level, not under
     `detail`. My first version of this stub nested it under `detail`, which is why the panel answered
     with the unnamed sentence -- the reading caught the stub, not the product, and I am keeping both
     readings instead of quietly fixing the one that flattered me. */
  await page.route("**/poi-import/parse", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        error: { reason_code: "NO_ACTIVE_PLAN", message: "UPSTREAM PROSE THAT MUST NOT APPEAR" },
      }),
    }),
  );
  await parse(page, "Riverside Museum");
  await page.waitForTimeout(1_200);
  const refusalDetail = await painted(page, "poi-import-error-detail");
  const refusalWhy = await painted(page, "poi-import-error-why");
  const refusalCard = await painted(page, "poi-import-card");
  const rects = await rectsOf(page, [
    "poi-import-error", "poi-import-error-why", "poi-import-error-detail", "poi-import-retry",
  ]);
  await page.screenshot({ path: evidencePath("ord0437", "error_refusal_arm.png") });
  await page.unroute("**/poi-import/parse");

  push("error-state", {
    phase, error, transportDetail, refusalDetail, refusalWhy, retryWords: retry,
    cardText, refusalCardText: refusalCard, rects,
    fault: "arm (a) route.abort on /poi-import/parse; arm (b) route.fulfill 409 reason_code=NO_ACTIVE_PLAN",
  });
  expect(phase).toBe("error");
  expect(error, "the failure has to be a sentence the traveller can act on").toBeTruthy();
  expect(retry, "the retry is what makes the error arm survivable in place").toBeTruthy();
  /* Arm (a): nothing answered, so the detail may only say that -- the engine's own words
     ("Failed to fetch") belong to the answer side of the line and must not be painted. */
  expect(cardText, "the engine's sentence is not this screen's sentence").not.toContain("Failed to fetch");
  /* Arm (b): the status is an allowed field and is what the detail carries; the condition is the
     dictionary's sentence. The machine token stays inside the component -- painting it would be a
     value derived from the answer's body reaching the DOM, which is what r7b counts. */
  expect(refusalDetail, "what this device saw is the status, and that is allowed on screen")
    .toContain("409");
  expect(refusalWhy, "the sentence comes from the dictionary, not from the answer")
    .not.toContain("UPSTREAM");
  expect(refusalWhy, "a named refusal has to be answered by its own condition sentence")
    .toContain("chosen as the plan");
  expect(refusalCard, "no fragment of the answer's prose may reach the card")
    .not.toContain("PROSE THAT MUST NOT APPEAR");
  expect(refusalCard, "nor does the machine token get painted")
    .not.toContain("NO_ACTIVE_PLAN");
});

test("added and replay — 已加入 means the server gave the record back", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterImport(page);
  test.setTimeout(180_000);
  const slack = await useSlackJourney(page);
  const planState = await ensureActivePlan(page);
  /* The two names come from the city this journey actually lives in. Hardcoding another city's
     demo places was this arm's first failure: nothing matched, nothing was selected, the write
     button stayed disabled and the click waited out the whole budget. */
  const built = await pasteText(page);
  mark("after_slack_journey_and_catalogue");
  const twoNames = built.names.slice(0, 2).join("\n");
  await parse(page, twoNames);
  await page.waitForTimeout(1_200);
  /* ORD-0524 changed what a parse leaves selected: nothing. The row that selects every selectable row
     is what this arm now presses -- and the count line is read first, because an arm that writes with
     the pre-tick gone would just sit on a disabled button for its whole budget. */
  const atZero = await painted(page, "poi-import-selection-count");
  const addAtZeroDisabled = await page.getByTestId("poi-import-add").first()
    .isDisabled({ timeout: 2_000 }).catch(() => null);
  await page.getByTestId("poi-import-select-all").click();
  await page.waitForTimeout(500);
  const chosen = await painted(page, "poi-import-selection-count");
  const titlesBefore = await planTitles(page);
  await page.getByTestId("poi-import-add").click();
  await expect(page.getByTestId("poi-import-added")).toBeVisible({ timeout: 25_000 });
  mark("after_add_accepted");
  const added = await painted(page, "poi-import-added");
  const addedSource = await painted(page, "poi-import-added-source");
  const readBackFailure = await painted(page, "poi-import-readback-failed");
  await page.screenshot({ path: evidencePath("ord0437", "added_arm.png") });

  // the server, not the component, is the authority: re-read the projection directly
  const onServer = await planTitles(page);

  /* Reload proves the record outlived the component, not the sentence. The success line is component
     state and is expected to be gone; what has to survive is that the plan still carries the place,
     which the re-parse below then reports as 本来就在 rather than as a second success. */
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("poi-import").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_200);
  const addedAfterReload = await painted(page, "poi-import-added");
  const onServerAfterReload = await planTitles(page);

  await parse(page, twoNames);
  await page.waitForTimeout(1_000);
  await page.getByTestId("poi-import-add").click();
  await page.waitForTimeout(2_500);
  const replayAdded = await painted(page, "poi-import-added");
  const already = await painted(page, "poi-import-already-present");
  await page.screenshot({ path: evidencePath("ord0437", "replay_arm.png") });

  push("added-and-replay", {
    planState, slack: slack.seen, catalogue: built.why, names: built.names, chosen, added, addedSource, readBackFailure,
    steps: { ...stepTimes },
    addedAfterReload, replayAdded, alreadyPresent: already,
    titlesAdded: built.names.filter((n) => !titlesBefore.includes(n)),
    serverTitles: onServer,
  });
  expect(readBackFailure, "a read-back failure must not be reported as success").toBeNull();
  expect(added, "the success sentence counts what the server took").toMatch(/\d/);
  const namesPresent = built.names.filter((n) => onServer.includes(n));
  push("added-and-replay-server-names", { namesPresent, expected: built.names });
  expect(namesPresent.length, "the added places must be readable back from the server").toBeGreaterThanOrEqual(1);
  expect(onServerAfterReload, "a reload does not remove the record from the plan").toEqual(
    expect.arrayContaining(namesPresent),
  );
  expect(replayAdded, "a replay must not mint a second success sentence").toBeNull();
  expect(already, "a replay names what was already there instead").toMatch(/2/);
  expect(addedSource).toMatch(/JOURNEY_FIRST_DAY|TRAVELLER_CHOSEN/);
});

test("chinese-arm — the sentences this order adds are Chinese on the Chinese arm", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterImport(page);
  test.setTimeout(180_000);
  const slack = await useSlackJourney(page);
  const planState = await ensureActivePlan(page);
  await page.evaluate(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(1_200);
  const built = await pasteText(page);
  await parse(page, built.text);
  await page.waitForTimeout(1_500);
  const warning = await painted(page, "poi-import-verify-warning");
  const disclosure = await painted(page, "poi-import-day-disclosure");
  const unsupported = await painted(page, "poi-import-unsupported");
  const scopeHint = await painted(page, "poi-import-scope-hint");
  const title = await painted(page, "poi-import");
  await page.screenshot({ path: evidencePath("ord0437", "zh_arm.png") });
  push("chinese-arm", {
    planState, slack: slack.seen, names: built.names, warning, disclosure, unsupported, scopeHint,
    titleHead: title?.slice(0, 60),
  });
  expect(warning).toMatch(/[一-龥]/);
  expect(disclosure).toMatch(/[一-龥]/);
  expect(unsupported).toMatch(/[一-龥]/);
  expect(scopeHint, "支持范围那句在中文屏也得是中文").toMatch(/[一-龥]/);
});

test("exits — the three destinations, and what each one is allowed to promise", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enterImport(page);
  test.setTimeout(180_000);
  await useSlackJourney(page);
  await ensureActivePlan(page);
  const built = await pasteText(page);
  await parse(page, built.text);
  const disabledNow = async (id: string) =>
    page.getByTestId(id).first().isDisabled({ timeout: 2_000 }).catch(() => null);
  const isDone = async (id: string) =>
    page.getByTestId(id).first().isEnabled({ timeout: 2_000 }).catch(() => null);

  /* ORD-0524 turned this arm's premise the other way round. The shape ORD-0437 measured and pinned --
     "the parse pre-ticks what it matched" -- is the defect ORD-0524 removes: a traveller who pressed
     加进计划 without reading the cards wrote every one of them. So the arm now judges both directions of
     the new shape: nothing selected on arrival and the door shut, and one control that selects every
     selectable row and opens it. */
  const countShown = await painted(page, "poi-import-selection-count");
  const shown = Number((countShown?.match(/\d+/) ?? [""])[0]);
  const addDisabledShown = await disabledNow("poi-import-add");
  const tickStateOnParse = await page.getByTestId("poi-card-select").evaluateAll((els) =>
    els.map((el) => el.getAttribute("aria-checked")));

  const selects = page.getByTestId("poi-card-select");
  const n = await selects.count();
  const selectAllWords = await painted(page, "poi-import-select-all");
  await page.getByTestId("poi-import-select-all").click();
  await page.waitForTimeout(400);
  const countAllOn = await painted(page, "poi-import-selection-count");
  const addWithAllOn = await disabledNow("poi-import-add");
  await page.getByTestId("poi-import-select-all").click();
  await page.waitForTimeout(400);
  const countEmpty = await painted(page, "poi-import-selection-count");
  const addEmptyDisabled = await disabledNow("poi-import-add");
  const addEmptyWords = await isDone("poi-import-add");

  const addWords = await painted(page, "poi-import-add");
  const exitsLead = await painted(page, "poi-import-exits-lead");
  const exitRadios = await page.getByTestId("poi-import-exits").locator('[role="radio"]').evaluateAll((els) =>
    els.map((el) => ({
      testid: el.getAttribute("data-testid"),
      aria_checked: el.getAttribute("aria-checked"),
      words: ((el as HTMLElement).innerText || "").replace(/\s+/g, " ").trim(),
    })));
  /* Moving the choice has to move the door with it -- that is what makes the three destinations
     exclusive rather than three suggestions. Each step records which write controls are mounted, so a
     screen that kept two doors open at once fails here instead of in the result. */
  const doorStates: Array<Record<string, unknown>> = [];
  for (const id of ["poi-import-exit-shelf", "poi-import-exit-store-only", "poi-import-exit-plan"]) {
    await page.getByTestId(id).first().click({ timeout: 5_000 });
    await page.waitForTimeout(1_200);
    doorStates.push({
      chosen: id,
      checked: await page.getByTestId("poi-import-exits").locator('[role="radio"][aria-checked="true"]').count(),
      add_mounted: await page.getByTestId("poi-import-add").count(),
      collect_mounted: await page.getByTestId("poi-import-collect").count(),
      shelf_rows: await page.getByTestId("poi-import-shelf").count(),
      shelf_empty: await painted(page, "poi-import-shelf-empty"),
      store_only_refused: await painted(page, "poi-import-store-only-refused"),
    });
  }
  const storeOnlyWhy = await painted(page, "poi-import-store-only-why");
  /* Bring the exits column into view first: the last untick left the page parked at the bottom of the
     candidate list, so the previous PNG showed four cards and none of what this arm judges, and an
     evidence file of the wrong region is not a reading. Rects are taken after this so the numbers in
     the reading are the numbers the screenshot shows. */
  await page.getByTestId("poi-import-exit-store-only").first().scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  /* Geometry, because text being present is not the same as text being readable. The first version of
     this fix put both reasons in `<small>` inside `.v8-boundary-card`, which blockifies b/p/span but
     not small -- the screenshot showed "…cannot promise a save.Create as a new plan" on one line. Rects
     come from getBoundingClientRect() in the page: Playwright's boundingBox() has no width/height, so
     arithmetic on it yields NaN and a check that can never fail. */
  const rects = await page.evaluate(() => {
    const r = (id: string) => {
      const el = document.querySelector(`[data-testid="${id}"]`);
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { top: Math.round(b.top), bottom: Math.round(b.bottom), left: Math.round(b.left), right: Math.round(b.right) };
    };
    return {
      exitPlan: r("poi-import-exit-plan"), exitShelf: r("poi-import-exit-shelf"),
      exitStoreOnly: r("poi-import-exit-store-only"), storeOnlyWhy: r("poi-import-store-only-why"),
    };
  });
  await page.screenshot({ path: evidencePath("ord0437", "exits_arm.png") });
  push("exits", {
    names: built.names, cardsWithSelect: n, countShown, selectedOnParse: shown, tickStateOnParse,
    selectAllWords, countAllOn, addWithAllOn, countEmpty, addEmptyDisabled,
    addWords, exitsLead, exitRadios, doorStates, storeOnlyWhy, rects,
  });

  expect(addWords, "加进计划 is the exit this screen actually performs").toBeTruthy();
  expect(shown, "a parse selects nothing on the traveller's behalf -- ORD-0524 ①").toBe(0);
  expect(tickStateOnParse.length, "every matched row carries its own tick control").toBeGreaterThan(0);
  expect(tickStateOnParse.every((v) => v === "false"), "and every one of them is unchecked").toBe(true);
  expect(addDisabledShown, "with nothing selected the write door is shut").toBe(true);
  expect(selectAllWords, "the row that selects every selectable row is on screen").toBeTruthy();
  expect((countAllOn?.match(/\d+/) ?? [""])[0]).not.toBe("0");
  expect(addWithAllOn, "selecting them all is what opens the door").toBe(false);
  expect((countEmpty?.match(/\d+/) ?? [""])[0]).toBe("0");
  expect(addEmptyDisabled, "with nothing ticked the write door has to shut again").toBe(true);
  expect(addEmptyWords, "an enabled control after 0 selected would fake a promise").toBe(false);

  /* Three destinations, one choice. 收藏 is no longer a promise this screen refuses to keep: ORD-0444
     and ORD-0510 ship both doors, so the exit is wired through them and the sentence that used to say
     「这一版还没有收藏的门」 is gone. 只存不排 stays refused because no door on this line keeps a place
     without giving it a day -- and it says so in painted text, because a tooltip never appears on a
     touch screen. */
  expect(exitsLead, "the screen says out loud that only one destination can be chosen").toBeTruthy();
  expect(exitRadios.length, "one radiogroup, three named exits").toBe(3);
  expect(exitRadios.map((e) => e.testid)).toEqual([
    "poi-import-exit-plan", "poi-import-exit-shelf", "poi-import-exit-store-only",
  ]);
  expect(exitRadios.filter((e) => e.aria_checked === "true").length, "exactly one exit is chosen").toBe(1);
  expect(exitRadios.find((e) => e.testid === "poi-import-exit-shelf")?.words).toMatch(/收藏|favour|favorite/i);
  expect(exitRadios.find((e) => e.testid === "poi-import-exit-store-only")?.words).toMatch(/只存不排|without a day/);

  const [shelfState, storeState, planState] = doorStates as Array<Record<string, unknown>>;
  expect(shelfState.checked, "choosing 收藏 leaves one exit chosen").toBe(1);
  expect(shelfState.add_mounted, "and takes the plan door off the screen").toBe(0);
  expect(shelfState.collect_mounted, "while the shelf door is mounted").toBe(1);
  expect(Number(shelfState.shelf_rows) > 0 || shelfState.shelf_empty,
    "the shelves are listed, or the absence of a shelf is named -- never a silent picker").toBe(true);
  expect(storeState.add_mounted, "只存不排 writes no plan").toBe(0);
  expect(storeState.collect_mounted, "只存不排 writes no shelf").toBe(0);
  expect(storeState.store_only_refused, "and it says that in a sentence, not by doing nothing").toBeTruthy();
  expect(planState.collect_mounted, "back on 加进计划 there is no shelf door").toBe(0);
  expect(planState.add_mounted, "and the plan door is itself again").toBe(1);

  expect(storeOnlyWhy, "an exit with no door has to say why in the open").toBeTruthy();
  expect(storeOnlyWhy).toMatch(/门|door/);
  for (const [key, label] of [["exitPlan", "加进计划"], ["exitShelf", "收藏"], ["exitStoreOnly", "只存不排"]] as const) {
    expect(rects[key], `${label} 得在屏上有自己的位置`).not.toBeNull();
  }
  expect(
    rects.exitPlan!.bottom,
    "三个去处一行一个 -- 叠在一起就是首版那个 run-on 缺陷",
  ).toBeLessThanOrEqual(rects.exitShelf!.top + 1);
  expect(rects.exitShelf!.bottom, "收藏那句也得在它自己那一行").toBeLessThanOrEqual(rects.exitStoreOnly!.top + 1);
  expect(rects.exitStoreOnly!.bottom, "拒绝那句不许压在第三个去处上").toBeLessThanOrEqual(rects.storeOnlyWhy!.top + 1);
});

test.afterAll(() => {
  writeFileSync(evidencePath("ord0437", "readings.json"), JSON.stringify({ file: FILE, rows }, null, 1));
});
