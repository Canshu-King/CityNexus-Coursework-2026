import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { writeFileSync } from "node:fs";
import { scanContrast } from "./_qoderContrastScan";
import { evidencePath } from "./_qoderEvidence";
import { openStageEntry } from "./_qoderStageEntry";

/**
 * ORD-0059 -- the Simulation entry on a phone, against the server's typed scene state.
 *
 * The order's whole risk is a second truth: a screen that says "now" from `Date.now()`, or that
 * claims a lifecycle the server never granted.  So every node here compares what the panel shows
 * with what the server answered at the same moment, and one node checks the *source* for a
 * frontend clock, because that claim must hold even on a day when the scene and the wall clock
 * happen to agree.
 *
 * Nothing here routes a product mutation: the actions hit the real backend on the throwaway
 * authoritative database, which is the only way "推进一个活动后屏幕上的 Now/Next 真的变了" can
 * mean what it says.
 */

const FILE = "e2e/qoder-simulation-scene.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const PANEL = "frontend/src/features/simulation/SimulationPanel.tsx";
const CLOCK = "frontend/src/lib/sceneClock.ts";
const FRONTEND_ROOT = "../";

async function sceneOf(page: Page) {
  const response = await page.request.get("/api/v1/demo/scene");
  expect(response.ok(), node("sim-scene-read", `GET /demo/scene answered ${response.status()}`)).toBe(true);
  return (await response.json()) as Record<string, string | number | boolean | null>;
}

/** The shell skips the wizard when a journey id is stored; land on Today with the stage's own id. */
async function openTodayWithStage(page: Page, journeyId: string, scheme: "light" | "dark") {
  await page.addInitScript(
    ([id]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", "zh-CN");
      localStorage.setItem("citynexus.v8.theme", "light");
    },
    [journeyId],
  );
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("sim-ball"), node("sim-entry-390-declares-stage-in-plain-words",
    "the stage ball is not rendered on Today at all")).toBeVisible({ timeout: 25_000 });
  await page.emulateMedia({ colorScheme: scheme, media: "screen" });
  await page.waitForTimeout(900);
  /* At rest the widget is one ball, so every node below has to open the window first. */
  await openStageEntry(page, "the stage gate");
}

async function openStage(page: Page, scheme: "light" | "dark" = "light") {
  const scene = await sceneOf(page);
  const journeyId = scene.journey_id;
  expect(
    typeof journeyId === "string" && journeyId.length > 0,
    node("sim-entry-390-declares-stage-in-plain-words",
      `the server says there is no scenario on stage (${JSON.stringify(scene.evidence_state)}); re-seed first`),
  ).toBe(true);
  await openTodayWithStage(page, journeyId as string, scheme);
  return scene;
}

const minute = (iso: unknown) => (typeof iso === "string" ? iso.slice(0, 16).replace("T", " ") : "");

/* Comments have to come out first: this lane's own doc block quotes `Date.now()` to explain why
   it is absent, and matching raw source would turn that explanation into a false red. */
const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");

test.describe("ORD-0059 the Simulation entry reads the stage from the server", () => {
  test("sim-entry-390-declares-stage-in-plain-words — 390 亮暗：入口可见＋人话声明＋字段名在诊断行", async ({ page }) => {
    test.setTimeout(240_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const readings: Record<string, unknown> = {};

    for (const scheme of ["light", "dark"] as const) {
      const scene = await openStage(page, scheme);
      const banner = page.getByTestId("sim-banner");
      await expect(banner, node("sim-entry-390-declares-stage-in-plain-words",
        `the stage banner is not visible at 390px/${scheme}`)).toBeVisible();
      const text = (await banner.innerText()).replace(/\s+/g, " ").trim();
      expect(
        text.length > 0 && /模拟|simulation/i.test(text),
        node("sim-entry-390-declares-stage-in-plain-words",
          `the banner at 390px/${scheme} does not say what stage it is: ${JSON.stringify(text)}`),
      ).toBe(true);
      /* ORD-0078 (b): the declaration is for a teacher, so the developer's flag string must not be
         the sentence they are handed... */
      expect(
        /demo_mode/.test(text),
        node("sim-entry-390-declares-stage-in-plain-words",
          `the banner hands a teacher a developer term instead of plain words: ${JSON.stringify(text)}`),
      ).toBe(false);
      /* ...and the flag still has to be stated somewhere, so this cannot be "fixed" by deleting
         the simulation declaration. */
      const diag = (await page.getByTestId("sim-diag").innerText()).replace(/\s+/g, " ").trim();
      expect(
        diag,
        node("sim-entry-390-declares-stage-in-plain-words",
          `the server's demo_mode answer is no longer stated anywhere: ${JSON.stringify(diag)}`),
      ).toMatch(scene.demo_mode === true ? /demo_mode=true/ : /demo_mode=false/);
      expect(
        text,
        node("sim-entry-390-declares-stage-in-plain-words",
          `banner says ${JSON.stringify(text)} but the server's demo_mode is ${JSON.stringify(scene.demo_mode)}`),
      ).toMatch(scene.demo_mode === true ? /模拟|simulation/i : /没有开模拟|not a simulation|no simulation/i);

      const box = await banner.boundingBox();
      expect(
        box && box.x >= -1 && box.x + box.width <= 391,
        node("sim-entry-390-declares-stage-in-plain-words",
          `the banner overflows 390px: ${JSON.stringify(box)}`),
      ).toBe(true);

      const ballBox = await page.getByTestId("sim-ball").boundingBox();
      expect(
        ballBox && ballBox.width >= 44 && ballBox.height >= 44,
        node("sim-entry-390-declares-stage-in-plain-words",
          `the ball's own tap target measures ${JSON.stringify(ballBox)} at 390px`),
      ).toBe(true);

      const scan = await scanContrast(page, { scheme });
      const mine = scan.rows.filter(
        (row) => (row.testId ?? "").startsWith("sim-") || row.selector.includes("cn-sim"),
      );
      expect(
        mine,
        node("sim-entry-390-declares-stage-in-plain-words",
          `the Simulation surface adds contrast failures in ${scheme}: ${JSON.stringify(mine.slice(0, 3))}`),
      ).toEqual([]);
      readings[scheme] = { text, bannerWidth: box?.width, ball: ballBox };
    }

    writeFileSync(
      evidencePath("ui-overnight/simulation-entry-390.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0059", node: "sim-entry-390-declares-stage-in-plain-words", readings }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("sim-scene-time-is-the-servers-not-the-devices — 前端自有时钟的杀点", async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const scene = await openStage(page);
    const facts = page.getByTestId("sim-scene-facts");
    await expect(facts, node("sim-scene-time-is-the-servers-not-the-devices", "no scene facts are shown at all")).toBeVisible();

    const shown = (await facts.innerText()).replace(/\s+/g, " ");
    const sceneMinute = minute(scene.effective_now);
    expect(sceneMinute, node("sim-scene-time-is-the-servers-not-the-devices",
      "the server itself answered no effective_now, so there is nothing to compare")).not.toBe("");
    expect(
      shown,
      node("sim-scene-time-is-the-servers-not-the-devices",
        `the panel shows ${JSON.stringify(shown.slice(0, 200))} instead of the server's ${sceneMinute}`),
    ).toContain(sceneMinute);

    /* Control, not decoration: the stage's own clock is a different instant from the wall clock
       right now, and the panel prints the stage's.  If a future seed moves the scene onto today,
       this comparison stops discriminating -- which is why the source check below is the standing
       half of this node. */
    const wall = await page.request.get(`/api/v1/journeys/${String(scene.journey_id)}/execution-clock`);
    const wallBody = (await wall.json()) as { now?: string };
    const wallMinute = minute(wallBody.now);
    const bannerText = (await page.getByTestId("sim-banner").innerText()).replace(/\s+/g, " ").trim();
    expect(
      bannerText.includes(sceneMinute),
      node("sim-scene-time-is-the-servers-not-the-devices",
        `the banner does not render the server's scene instant ${sceneMinute}: ${JSON.stringify(bannerText)}`),
    ).toBe(true);
    if (wallMinute && wallMinute !== sceneMinute) {
      expect(
        bannerText.includes(wallMinute),
        node("sim-scene-time-is-the-servers-not-the-devices",
          `the banner rendered the wall clock ${wallMinute} instead of the scene's ${sceneMinute}`),
      ).toBe(false);
    }

    /* The durable claim: no frontend clock in the consumer layer or the surface. */
    for (const file of [PANEL, CLOCK]) {
      const source = stripComments(readFileSync(`${FRONTEND_ROOT}${file}`, "utf-8"));
      expect(
        /Date\.now\(\)|new Date\(/.test(source),
        node("sim-scene-time-is-the-servers-not-the-devices",
          `${file} computes time in the client (${/Date\.now\(\)|new Date\(/.exec(source)?.[0]}), which is the second clock ORD-0059 forbids`),
      ).toBe(false);
    }
    expect(
      /effectiveNow|effective_now/.test(stripComments(readFileSync(`${FRONTEND_ROOT}${CLOCK}`, "utf-8"))),
      node("sim-scene-time-is-the-servers-not-the-devices",
        "sceneClock.ts never reads the server's effective_now, so what is it consuming instead?"),
    ).toBe(true);

    writeFileSync(
      evidencePath("ui-overnight/simulation-scene-time.json"),
      JSON.stringify(
        { generatedBy: "e345b9d6", order: "ORD-0059", node: "sim-scene-time-is-the-servers-not-the-devices", sceneMinute, wallMinute, shown: shown.slice(0, 240) },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });

  test("sim-scene-advances-on-server — 服务器场景真的推进了，且 Today 那行字跟着变（两者共担）", async ({ page }) => {
    test.setTimeout(240_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const scene = await openStage(page);
    const row = page.getByTestId("sim-action-advance");
    await row.scrollIntoViewIfNeeded();

    /* Positive control on the same instrument: the row's blocked/unblocked decision comes from the
       server's cursor, so if it is blocked the reading below proves nothing and says so. */
    const described = await row.getAttribute("aria-describedby");
    if (described) {
      const reason = (await page.locator(`#${described}`).innerText()).replace(/\s+/g, " ").trim();
      expect(
        reason.length > 0,
        node("sim-scene-advances-on-server",
          "the advance row is disabled and points at an id with no explanation behind it"),
      ).toBe(true);
      test.skip(true, `the stage has no activity to advance (server reason: ${reason})`);
    }

    const clean = (value: string) => value.replace(/\s+/g, " ").trim();
    const before = clean(await page.getByTestId("today-next").innerText());
    /* Read "before" through the component, not through a second fetch of my own: the panel has
       already loaded by now, so an independent fetch can return a *later* scene than the one that
       was on screen, and the final comparison would then be true without anything having visibly
       moved.  The banner is the component's own rendering of the server's instant, so both sides
       of the comparison are read the same way. */
    const beforeScene = clean(await page.getByTestId("sim-banner").innerText());
    /* One tap is one legal transition, and the server decides which one that is; on a frozen V12
       plan an activity walks NOT_STARTED -> EN_ROUTE -> ARRIVED -> IN_PROGRESS -> COMPLETED before
       the cursor moves on.  So tap until the screen's Next actually changes. */
    let taps = 0;
    let outcome = "";
    while (taps < 4 && clean(await page.getByTestId("today-next").innerText()) === before) {
      if (await row.getAttribute("aria-describedby")) break;
      await row.click();
      taps += 1;
      await expect(
        page.getByTestId("sim-outcome"),
        node("sim-scene-advances-on-server",
          "advancing an activity produced no answer from the panel at all"),
      ).toContainText(/服务器/, { timeout: 30_000 });
      outcome = clean(await page.getByTestId("sim-outcome").innerText());
      await page.waitForTimeout(1_500);
    }

    expect(
      taps > 0,
      node("sim-scene-advances-on-server", "the advance row never fired a server step"),
    ).toBe(true);
    expect(
      /出发|到达|开始这项活动|完成这项活动|depart|arrive|begin|complete/.test(outcome),
      node("sim-scene-advances-on-server",
        `the panel did not say which transition the server took: ${JSON.stringify(outcome)}`),
    ).toBe(true);
    expect(
      /游标|cursor/.test(outcome),
      node("sim-scene-advances-on-server",
        `the outcome line does not report the server's own cursor: ${JSON.stringify(outcome)}`),
    ).toBe(true);

    const after = clean(await page.getByTestId("today-next").innerText());
    const afterScene = clean(await page.getByTestId("sim-banner").innerText());
    const heroChanged = after !== before;
    /* What this node owns is two claims that used to be joined by `||`: the server's scene moved,
       and Today's line moved with it.  Both are read through the component's own rendering, the same
       string on both sides, so a format mismatch cannot make either one "change" by itself.  The
       stronger claim -- that the station Today settled on is the one the server's twin names -- is
       owned by `sim-today-hero-follows-the-stage`, and this node does not stand in for it. */
    expect(
      afterScene !== beforeScene,
      node("sim-scene-advances-on-server",
        `after ${taps} server step(s) the stage never moved: banner was ${JSON.stringify(beforeScene.slice(0, 120))}, still ${JSON.stringify(afterScene.slice(0, 120))}`),
    ).toBe(true);
    /* ORD-0088 #3: the two halves used to share one `||`, so this node was green while Today's line
       sat still through eight taps -- the reading that started the order. Each half now carries its
       own assertion: a moved scene with a frozen hero is a red, not a pass with a note. */
    expect(
      heroChanged,
      node("sim-scene-advances-on-server",
        `the server's scene moved but Today's line did not: before ${JSON.stringify(before.slice(0, 120))}, after ${JSON.stringify(after.slice(0, 120))} (banner ${JSON.stringify(afterScene.slice(0, 90))})`),
    ).toBe(true);

    writeFileSync(
      evidencePath("ui-overnight/simulation-advance.json"),
      JSON.stringify(
        {
          generatedBy: "e345b9d6",
          order: "ORD-0059",
          node: "sim-scene-advances-on-server",
          taps,
          heroChanged,
          heroBefore: before,
          heroAfter: after,
          bannerBefore: beforeScene,
          bannerAfter: afterScene,
          outcome: outcome.slice(0, 240),
          note:
            "asserted, not recorded: the banner moved and today-next moved with it (ORD-0088 #3 removed the `||`).",
        },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });

  test("sim-refusal-is-not-drawn-as-success — 具名 503 不许画成做完了", async ({ page }) => {
    test.setTimeout(240_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const scene = await openStage(page);
    await page.route("**/api/v1/journeys/*/activities/*/*", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "GENERATION_STORE_BUSY",
            message: "Another generation is writing right now; this one was not recorded. Try again.",
            message_key: "generation.store_busy",
            details: { retry_after_ms: 500 },
          },
        }),
      }),
    );
    const advance = page.getByTestId("sim-action-advance");
    await advance.scrollIntoViewIfNeeded();
    if (await advance.getAttribute("aria-describedby")) {
      test.skip(true, `no cursor on stage (${JSON.stringify(scene.basis)}); the refusal path cannot be reached`);
    }
    await advance.click();

    const refusal = page.getByTestId("sim-outcome");
    await expect(
      refusal,
      node("sim-refusal-is-not-drawn-as-success", "the panel answered a 503 with nothing at all"),
    ).toContainText(/占着|没被记录|blocked|refused/, { timeout: 30_000 });
    const said = (await refusal.innerText()).replace(/\s+/g, " ").trim();
    expect(
      /游标现在指向|cursor now|已接受|建了承诺/.test(said),
      node("sim-refusal-is-not-drawn-as-success",
        `a refused step is reported as a lifecycle success: ${JSON.stringify(said)}`),
    ).toBe(false);
    expect(
      /Another generation is writing/.test(said),
      node("sim-refusal-is-not-drawn-as-success",
        `the English server sentence is pasted into the Chinese screen (ORD-0055): ${JSON.stringify(said)}`),
    ).toBe(false);
    expect(
      await page.getByTestId("sim-outcome-note").innerText(),
      node("sim-refusal-is-not-drawn-as-success", "the refusal is not labelled as 'this did not happen'"),
    ).toMatch(/没有发生|未发生|unchanged/i);
  });

  test("sim-reset-hands-the-stage-to-today — 点完重置不该被扔回三步向导", async ({ page }) => {
    test.setTimeout(300_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await openStage(page);

    /* Take the key away first.  If the panel does not write it back from the reset response, the
       reload lands on the wizard and this node goes red -- which is the whole point: the teacher
       must reach Today without anyone hand-seeding localStorage.  It ran green in 1.7s against the real
       backend on a throwaway database (run recorded in the ledger), so the timing below is measured
       rather than guessed from how long a page usually takes. */
    await page.evaluate(() => localStorage.removeItem("citynexus.v8.journey_id"));
    const reset = page.getByTestId("sim-action-reset");
    await reset.scrollIntoViewIfNeeded();
    if (await reset.getAttribute("aria-describedby")) {
      test.skip(true, "the server refused the reset row on this deployment (demo_mode is off)");
    }

    const answered = page.waitForResponse(
      (response) => response.url().includes("/api/v1/demo/reset"),
      { timeout: 90_000 },
    );
    await reset.click();
    const body = (await (await answered).json()) as { status?: string; journey_id?: string };
    expect(
      typeof body.journey_id === "string" && body.journey_id.length > 0,
      node("sim-reset-hands-the-stage-to-today",
        `the server's reset answer carried no journey id to hand over: ${JSON.stringify(body).slice(0, 160)}`),
    ).toBe(true);

    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id")), { timeout: 30_000, intervals: [500] })
      .toBe(body.journey_id as string);

    await expect(
      page.getByTestId("today-heading"),
      node("sim-reset-hands-the-stage-to-today",
        "after 重置 the screen is still not Today, so the teacher is left holding the wizard"),
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.getByTestId("trial-setup"),
      node("sim-reset-hands-the-stage-to-today",
        "重置 put the traveller back on the three-step wizard instead of the stage it just built"),
    ).toHaveCount(0);
    await expect(
      page.getByTestId("sim-banner"),
      node("sim-reset-hands-the-stage-to-today", "Today came back but the stage declaration went with the reload"),
    ).toBeVisible();

    writeFileSync(
      evidencePath("ui-overnight/simulation-reset-handoff.json"),
      JSON.stringify(
        { generatedBy: "e345b9d6", order: "ORD-0078", node: "sim-reset-hands-the-stage-to-today", resetStatus: body.status ?? null, journeyId: body.journey_id ?? null },
        null,
        2,
      ) + "\n",
      "utf-8",
    );
  });

  test("sim-blocked-action-explains-itself-and-stays-reachable — 灰掉不算交代", async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width: 390, height: 844 });
    await openStage(page);
    const replan = page.getByTestId("sim-action-replan");
    await replan.scrollIntoViewIfNeeded();

    // On a fresh page there is no trigger event, so the replan row must be blocked *and* say why.
    await expect(
      page.getByRole("button", { name: /请求重规划/ }),
      node("sim-blocked-action-explains-itself-and-stays-reachable",
        "the blocked replan row carries no accessible description, so a screen reader gets a dead button"),
    ).toHaveCount(1);
    const reasonId = await replan.getAttribute("aria-describedby");
    expect(
      reasonId,
      node("sim-blocked-action-explains-itself-and-stays-reachable",
        "replan is blocked without pointing at the reason element"),
    ).not.toBeNull();
    const reason = (await page.locator(`#${reasonId}`).innerText()).replace(/\s+/g, " ").trim();
    expect(
      /* Any server-derived cause counts as an explanation; what this kills is a blocked row that
         blocks silently.  Since 65e184d the first cause a teacher meets is the missing plan
         rather than the missing trigger event, and both are true statements about the stage. */
      /触发|中断|trigger|ACTIVE 计划|计划版本|重规划|replan/i.test(reason),
      node("sim-blocked-action-explains-itself-and-stays-reachable",
        `the blocked replan row explains itself with ${JSON.stringify(reason)}`),
    ).toBe(true);

    /* The same instrument must be able to say "reachable": a blocked row is focusable, and an
       unblocked one sits next to it un-disabled. */
    const focus = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="sim-action-replan"]') as HTMLElement | null;
      if (!el) return { found: false, landed: false };
      el.focus();
      return { found: true, landed: document.activeElement === el };
    });
    expect(
      focus,
      node("sim-blocked-action-explains-itself-and-stays-reachable", `a blocked row cannot take focus: ${JSON.stringify(focus)}`),
    ).toEqual({ found: true, landed: true });
    const restore = page.getByTestId("sim-action-restore");
    await restore.scrollIntoViewIfNeeded();
    await expect(
      restore,
      node("sim-blocked-action-explains-itself-and-stays-reachable",
        "the control that should be available is not -- the readings above would then prove nothing"),
    ).toBeEnabled();
  });
});
