import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { openStageEntry } from "./_qoderStageEntry";

/**
 * ORD-0081 ① -- measured, not assumed: can a teacher reach Today by pressing the stage's own
 * actions, without anyone hand-seeding localStorage?
 *
 * Three starting states, because the answer differs per state and each one is a screen a real
 * person can be left on:
 *   wizard   -- no journey id at all (a fresh stage; where a teacher actually starts)
 *   stale id -- a journey id that no longer resolves (the state 重置 itself used to create)
 *   on Today -- a live journey
 * One test per state, each with its own context and its own budget: an earlier version drove all
 * three from a single page, which meant a slow arm ate the shared timeout, and the unfinished reset
 * left the next spec's scenario missing -- a fixture stepping on a measurement.
 */

const FILE = "e2e/qoder-reset-from-wizard.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

type Arm = {
  state: string;
  /** Whether the stage entry itself is on this screen (the summary a teacher would open). */
  entryFound: boolean;
  controlFound: boolean;
  screenBefore: string;
  screenAfter: string;
  storedIdAfter: string | null;
  resetHttp: number | null;
};

const clean = (value: string | null | undefined) => String(value ?? "").replace(/\s+/g, " ").trim();

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

async function screenOf(page: Page) {
  if (await page.getByTestId("trial-setup").isVisible().catch(() => false)) return "wizard";
  if (await page.getByTestId("today-heading").isVisible().catch(() => false)) return "today";
  return "other";
}

async function arm(page: Page, setup: Arm["state"]): Promise<Arm> {
  const scene = await readScene(page);
  await page.setViewportSize({ width: 390, height: 844 });
  /* The arm's storage is written once, after the first paint, and nothing clears it afterwards --
     the panel's own hand-over has to survive the reload it triggers, and that is the thing under
     test.  Each arm runs in a fresh context, so no previous arm can leave residue here. */
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate(
    ([state, id]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.locale", "zh-CN");
      if (state === "stale") {
        localStorage.setItem("citynexus.v8.journey_id", "00000000-0000-4000-8000-000000000000");
      }
      if (state === "live" && id) localStorage.setItem("citynexus.v8.journey_id", id as string);
    },
    [setup, String(scene.journey_id ?? "")] as const,
  );
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(2_500);
  /* On the wizard the stage entry is deliberately collapsed (the form owns that screen), so the
     controls exist but are not on screen -- and a click there waits for visibility forever.  A
     teacher opens it the same way; if there is no summary to open, that is the finding. */
  const ball = page.getByTestId("sim-ball");
  const entryFound = (await ball.count()) > 0;
  if (entryFound) await ball.click();
  const control = page.getByTestId("sim-action-reset");
  const controlFound = (await control.count()) > 0;
  const screenBefore = await screenOf(page);
  let resetHttp: number | null = null;
  if (controlFound) {
    const answered = page
      .waitForResponse((response) => response.url().includes("/api/v1/demo/reset"), { timeout: 90_000 })
      .catch(() => null);
    await control.click({ timeout: 30_000 }).catch(() => undefined);
    const response = await answered;
    resetHttp = response ? response.status() : -1;
    /* Poll for the screen rather than sleeping: the panel reloads on its own after the answer,
       and a fixed wait here would read the shell mid-boot and call it "other". */
    await expect
      .poll(() => screenOf(page), { timeout: 60_000, intervals: [1_000] })
      .toBe("today");
  }
  return {
    state: setup,
    entryFound,
    controlFound,
    screenBefore,
    screenAfter: await screenOf(page),
    storedIdAfter: await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id")),
    resetHttp,
  };
}

const record = (setup: string, reading: Arm | null, failure?: unknown) =>
  writeFileSync(
    evidencePath(`ui-overnight/ord0081-${setup}.json`),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0081",
        node: "ord0081-reset-reaches-today-without-hand-seeded-storage",
        reading,
        failure: failure instanceof Error ? `${failure.message}\n${failure.stack ?? ""}` : null,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );

for (const setup of ["wizard", "stale", "live"] as const) {
  test(`ord0081-reset-reaches-today [${setup}] — 出发点按[重置]就该到 Today`, async ({ page }) => {
    test.setTimeout(300_000);
    let reading: Arm | null = null;
    try {
      reading = await arm(page, setup);
      record(setup, reading);
    } catch (failure) {
      record(setup, reading, failure);
      throw failure;
    }

    /* The three things the order asks for, per starting state: the control is on the screen the
       teacher is standing on, the server answered, and the screen the teacher lands on is Today
       with the journey the server named -- nobody edited storage to get there. */
    expect(
      reading.controlFound,
      node("ord0081-reset-reaches-today-without-hand-seeded-storage",
        `from ${JSON.stringify(setup)} (screen ${JSON.stringify(reading.screenBefore)}, stage entry present=${reading.entryFound}) there is no 重置 control at all`),
    ).toBe(true);
    expect(
      reading.resetHttp,
      node("ord0081-reset-reaches-today-without-hand-seeded-storage",
        `from ${JSON.stringify(setup)} the reset did not answer 200: ${JSON.stringify(reading)}`),
    ).toBe(200);
    expect(
      reading.screenAfter,
      node("ord0081-reset-reaches-today-without-hand-seeded-storage",
        `from ${JSON.stringify(setup)} the reset did not land on Today: ${JSON.stringify(reading)}`),
    ).toBe("today");
    expect(
      String(reading.storedIdAfter ?? ""),
      node("ord0081-reset-reaches-today-without-hand-seeded-storage",
        `from ${JSON.stringify(setup)} the journey id was never handed over: ${JSON.stringify(reading)}`),
    ).not.toHaveLength(0);
  });
}

/* ----------------------------------------------------------------- the pending ruling, measured.
 * Since `65e184d` one 重置 only seeds, so "a single tap to Today" became the stage's own three
 * actions.  Whether those three take a teacher from the wizard all the way into execution is a
 * question the screen can answer, so this node asks it: 重置 -> 准备舞台 -> 开始执行, nothing
 * hand-seeded, and the last claim read off the hero rather than off the panel that moved it.
 */
test("ord0081-stage-chain-from-the-wizard-reaches-execution — 三步链真能跑到执行中", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("trial-setup")).toBeVisible({ timeout: 30_000 });
  /* The widget is one ball at rest; pressing it is the first teacher action in this chain, and if
     there is nothing to press the chain does not exist. */
  const ball = page.getByTestId("sim-ball");
  await expect(
    ball,
    node("ord0081-stage-chain-from-the-wizard-reaches-execution",
      "the wizard screen has no stage ball, so no chain of real stage actions can start here"),
  ).toHaveCount(1);
  await ball.click();
  await expect(
    page.getByTestId("sim-pop"),
    node("ord0081-stage-chain-from-the-wizard-reaches-execution",
      "the ball was pressed and no stage window appeared"),
  ).toBeVisible();

  const steps: string[] = [];
  const pressed = async (id: string) => {
    /* Every step of the chain happens through the disclosure a traveller would open -- including after the
       reload that 重置 triggers, which brings the phone default back: collapsed. */
    await openStageEntry(page, `the stage chain at ${id}`);
    const row = page.getByTestId(`sim-action-${id}`);
    await row.scrollIntoViewIfNeeded().catch(() => undefined);
    const reasonId = await row.getAttribute("aria-describedby");
    if (await row.getAttribute("aria-disabled")) {
      throw new Error(
        node("ord0081-stage-chain-from-the-wizard-reaches-execution",
          `${id} was refused before it could run: ${reasonId ? JSON.stringify(clean(await page.locator(`#${reasonId}`).innerText())) : "(no reason element)"}`),
      );
    }
    await row.click();
    const outcome = page.getByTestId("sim-outcome");
    await expect(
      outcome,
      node("ord0081-stage-chain-from-the-wizard-reaches-execution", `${id} answered with nothing to read`),
    ).toBeVisible({ timeout: 90_000 });
    steps.push(`${id}: ${clean(await outcome.innerText()).slice(0, 160)}`);
  };

  await pressed("reset");
  /* Reset hands the journey over and re-boots the shell: wait for the screen it lands on instead of
     assuming a duration. */
  await expect
    .poll(() => screenOf(page), { timeout: 60_000, intervals: [1_000] })
    .toBe("today");

  let scene = await readScene(page);
  for (let step = 0; step < 6 && scene.journey_status !== "ACTIVE"; step += 1) {
    if (!scene.plan_version_id) await pressed("prepare");
    else await pressed("start");
    await page.waitForTimeout(1_200);
    scene = await readScene(page);
  }
  expect(
    scene.journey_status,
    node("ord0081-stage-chain-from-the-wizard-reaches-execution",
      `the three real stage actions did not put the journey into execution: ${JSON.stringify({ journey_status: scene.journey_status, plan_version_id: scene.plan_version_id, steps })}`),
  ).toBe("ACTIVE");

  const journeyId = String(scene.journey_id ?? "");
  const experience = await page.request.get(`/api/v1/journeys/${journeyId}/experience`);
  expect(
    experience.ok(),
    node("ord0081-stage-chain-from-the-wizard-reaches-execution", `GET /experience answered ${experience.status()}`),
  ).toBe(true);
  const projected = (await experience.json()) as {
    active_plan?: { activities?: Array<{ id: string; title: string }>; current_activity_id?: string | null };
  };
  const cursorId = projected.active_plan?.current_activity_id ?? null;
  const cursorTitle =
    projected.active_plan?.activities?.find((item) => item.id === cursorId)?.title ?? null;
  expect(
    cursorTitle,
    node("ord0081-stage-chain-from-the-wizard-reaches-execution", "the server reports no cursor to compare the hero against"),
  ).toBeTruthy();
  const heroNow = () => page.getByTestId("today-now").innerText().catch(() => "");
  try {
    await expect.poll(heroNow, { timeout: 30_000, intervals: [1_000] }).toContain(String(cursorTitle));
  } catch (failure) {
    throw new Error(
      node("ord0081-stage-chain-from-the-wizard-reaches-execution",
        `the journey executes and the cursor is on ${JSON.stringify(cursorTitle)}, but Today's 现在 reads ${JSON.stringify(clean(await heroNow()))}`),
      { cause: failure },
    );
  }

  writeFileSync(
    evidencePath("ui-overnight/ord0081-stage-chain.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0081",
        node: "ord0081-stage-chain-from-the-wizard-reaches-execution",
        steps,
        scene,
        cursorTitle,
        heroNow: clean(await heroNow()),
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
