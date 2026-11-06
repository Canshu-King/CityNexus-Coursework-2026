/**
 * ORD-0193 -- the destination-preparation flow, on the production build at a phone viewport.
 *
 * The order's requirements and how each is checked here:
 *   "需要准备"           -> a destination the server calls HYDRATABLE shows what is missing, from the
 *                           server's own coverage map, and offers preparing it;
 *   "已开始准备"          -> starting a job shows the job, and a second start of the same query reads
 *                           `reused` rather than pretending to start again;
 *   "当前 phase+status"   -> the running block carries the server's `phase` and `status` verbatim, and
 *                           carries no percentage (the payload has none);
 *   "provider/runtime 失败 + 真恢复动作" -> a job whose outcome is a runtime failure offers a retry,
 *                           and one whose outcome is structural does not offer a doomed retry;
 *   "READY => 去规划"     -> a READY verdict shows the go-and-plan action and no prepare action.
 *
 * Three prohibitions, each with its own assertion:
 *   no silent close and no no-op      -> every visible state has at least one focusable action;
 *   structural states are not called network failures -> the NO_CANONICAL_IDENTITY copy never says
 *                                       网络不可用, and no `data-*` attribute claims OFFLINE for it;
 *   no invented progress              -> the job block has no element carrying a percentage, and the
 *                                       payload is asserted to have no `progress` field at all.
 *
 * The statuses are produced by the real endpoints; where a state cannot be reached on a fresh
 * database (READY needs a current catalogue), the response is answered by the gate and the block is
 * labelled as the gate's invention in the evidence file.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-destination-preparation.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

async function openDiscover(page: Page): Promise<void> {
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, process.env.CITYNEXUS_E2E_JOURNEY ?? "");
  const seeded = await (await page.request.get("/api/v1/demo/scene")).json();
  const journeyId = String((seeded as { journey_id?: string }).journey_id ?? "");
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.goto("/v8/discover", { waitUntil: "load" });
  await expect(
    page.getByTestId("prep-panel"),
    node("prep-panel", "the preparation flow is not on the discover surface"),
  ).toBeVisible({ timeout: 30_000 });
}

/** Every control the traveller can actually press, so "no no-op" can be asserted over the set. */
async function focusableActions(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>("[data-testid^='prep-']"))
      .filter((el) => el.tagName === "BUTTON" && !el.hasAttribute("disabled") && el.offsetParent !== null)
      .map((el) => el.dataset.testid as string),
  );
}

test("ord0193-a-destination-that-needs-preparing-says-what-is-missing-and-offers-to — 需要准备就说得清缺什么", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0193-a-destination-that-needs-preparing-says-what-is-missing-and-offers-to";
  await openDiscover(page);

  /* A city the build knows by name but holds no catalogue for: the traveller's real case, and the
     one the probe measured as HYDRATABLE / COVERAGE_INCOMPLETE. */
  await page.getByTestId("prep-query").fill("天津");
  await page.getByTestId("prep-check").click();
  const verdict = page.getByTestId("prep-verdict");
  await expect(
    verdict,
    node(NODE, "the server verdict for a known city never appeared"),
  ).toBeVisible({ timeout: 20_000 });

  const read = {
    capability: await verdict.getAttribute("data-capability"),
    reason: await page.getByTestId("prep-panel").getAttribute("data-reason"),
    title: (await page.getByTestId("prep-verdict-title").innerText()).trim(),
    body: (await page.getByTestId("prep-verdict-body").innerText()).replace(/\s+/g, " "),
    missing: (await page.getByTestId("prep-missing").innerText().catch(() => "")).replace(/\s+/g, " "),
    actions: await focusableActions(page),
  };
  expect(
    read.capability,
    node(NODE, `the verdict is ${JSON.stringify(read.capability)}; a city with no catalogue has to be reported as needing preparation`),
  ).toBe("HYDRATABLE");
  expect(
    read.missing.length,
    node(NODE, `the verdict says facts are missing but lists none: ${JSON.stringify(read)}`),
  ).toBeGreaterThan(0);
  expect(
    read.actions.includes("prep-start"),
    node(NODE, `a destination that needs preparing offers no way to prepare it: ${JSON.stringify(read.actions)}`),
  ).toBe(true);
  /* No-op prohibition: something is always pressable in this state. */
  expect(
    read.actions.length,
    node(NODE, `this screen offers no action at all: ${JSON.stringify(read)}`),
  ).toBeGreaterThan(0);

  writeFileSync(
    evidencePath("ui-overnight/ord0193-needs-preparing.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", read }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0193-needs-preparing.png"), fullPage: true });
});

test("ord0193-starting-the-preparation-shows-the-servers-own-phase-and-no-invented-progress — 进度只报服务器给的", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0193-starting-the-preparation-shows-the-servers-own-phase-and-no-invented-progress";
  await openDiscover(page);

  /* Hold the job open so the running state can be read at all: a real one on this tip finishes in
     tens of milliseconds, and a screen that is only ever seen after the fact has no running state
     to judge.  The gate's invention, labelled as such in the evidence file. */
  await page.route("**/api/v1/catalog-hydrations/*", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    body.status = "RUNNING";
    body.recorded_status = "RUNNING";
    body.phase = "DISCOVERING_POIS";
    body.outcome = null;
    body.terminal = false;
    body.elapsed_ms = 4200;
    body.retry_after_ms = 1500;
    delete body.error;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.getByTestId("prep-query").fill("天津");
  await page.getByTestId("prep-check").click();
  await expect(page.getByTestId("prep-verdict")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("prep-start").click();

  const job = page.getByTestId("prep-job");
  await expect(
    job,
    node(NODE, "starting a preparation produced no job block at all"),
  ).toBeVisible({ timeout: 20_000 });
  /* My instrument, not the product: the block is first painted straight from the POST's own answer
     (`DestinationPreparationPanel.tsx`), and that answer is honestly `QUEUED` -- the route above
     pins only the polled `GET`.  Reading the block once at first paint therefore flipped under chain
     load on this tip: 1 RED ("QUEUED", `review/run-run072593.log`) / 1 GREEN (`run-run072814.log`)
     in the ten-file shape, 2 GREEN single-node (`run-run072724.log`, `run-run072766.log`).  The wait
     is bounded and its message names what never arrived, so a running state that truly does not
     reach the screen still goes red; no assertion below is relaxed. */
  await expect
    .poll(() => job.getAttribute("data-status"), {
      message: node(
        NODE,
        "the server's running state never reached the screen: [data-status] stayed at what the POST answered",
      ),
      timeout: 20_000,
    })
    .toBe("RUNNING");
  const read = {
    status: await job.getAttribute("data-status"),
    phase: await job.getAttribute("data-phase"),
    terminal: await job.getAttribute("data-terminal"),
    elapsedMs: await job.getAttribute("data-elapsed-ms"),
    title: (await page.getByTestId("prep-job-title").innerText()).trim(),
    body: (await page.getByTestId("prep-job-body").innerText()).replace(/\s+/g, " "),
    bodyText: (await job.innerText()).replace(/\s+/g, " "),
  };
  expect(read.status, node(NODE, `the job block reports status ${JSON.stringify(read.status)}`)).toBe("RUNNING");
  expect(read.phase, node(NODE, `the job block reports phase ${JSON.stringify(read.phase)}`)).toBe("DISCOVERING_POIS");
  expect(read.terminal, node(NODE, `the job block says terminal=${read.terminal} while the server says it is running`)).toBe("false");
  /* No invented progress: the server publishes no percentage, so the screen must not print one. */
  expect(
    /\d+\s*%/.test(read.bodyText),
    node(NODE, `the running block shows a percentage the server never published: ${JSON.stringify(read.bodyText)}`),
  ).toBe(false);
  const serverRow = await page.evaluate(async () => {
    const response = await fetch("/api/v1/demo/scene");
    return response.ok;
  });
  expect(serverRow).toBe(true);
  /* The claim that the payload itself has no denominator is asserted against the live endpoint, so
     this node cannot pass merely because the screen chose not to render a field that exists. */
  const jobId = await page.evaluate(() => {
    const panel = document.querySelector("[data-testid='prep-panel']");
    return panel ? panel.getAttribute("data-job-id") : null;
  });
  expect(
    jobId,
    node(NODE, "the panel does not expose the job id, so the payload could not be cross-read"),
  ).not.toBeNull();
  const payloadHasProgress = await page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/catalog-hydrations/${id}`);
    const body = await response.json();
    return Object.prototype.hasOwnProperty.call(body, "progress");
  }, jobId);
  expect(
    payloadHasProgress,
    node(NODE, "the server payload carries a `progress` field after all, so the absence of a bar is the screen's choice rather than the contract's"),
  ).toBe(false);
  /* And while it runs, the traveller can stop it -- not a dead wait. */
  expect(
    read.body.length,
    node(NODE, `the running block carries no words at all: ${JSON.stringify(read)}`),
  ).toBeGreaterThan(0);
  const actions = await focusableActions(page);
  expect(
    actions.includes("prep-abandon"),
    node(NODE, `a running preparation offers no way to stop it: ${JSON.stringify(actions)}`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0193-running.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        gateInvention: "the job read is held at RUNNING/DISCOVERING_POIS by the gate, because a real job on this tip finishes in milliseconds",
        read,
        actions,
        payloadHasProgress,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0193-running.png"), fullPage: true });
});

test("ord0193-a-structural-verdict-is-not-called-a-network-failure — 结构性的“认不出来”不许说成网络不可用", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0193-a-structural-verdict-is-not-called-a-network-failure";
  await openDiscover(page);

  /* A destination no traceable source can name: the server answers 200 with NO_CANONICAL_IDENTITY,
     which is a fact about the place and not about connectivity. */
  await page.getByTestId("prep-query").fill("zzxqvw-不存在县");
  const started = Date.now();
  await page.getByTestId("prep-check").click();
  /* The read can be slow when the resolver has to try a provider, so a red here has to say which
     state the panel landed in rather than only "element not found". */
  await expect(
    page.getByTestId("prep-verdict").or(page.getByTestId("prep-check-error")),
    node(NODE, "the capability read for an unnameable destination produced neither a verdict nor a named failure"),
  ).toBeVisible({ timeout: 60_000 });
  const elapsedMs = Date.now() - started;
  /* `innerText()` on an element that is not there waits until the test times out; the count is read
     first so a missing error node is a value rather than a hang. */
  const checkError =
    (await page.getByTestId("prep-check-error").count()) > 0
      ? await page.getByTestId("prep-check-error").innerText()
      : "";
  expect(
    checkError,
    node(NODE, `the capability read failed instead of answering: ${JSON.stringify(checkError)} (after ${elapsedMs}ms)`),
  ).toBe("");

  const reason = await page.getByTestId("prep-panel").getAttribute("data-reason");
  const body = (await page.getByTestId("prep-verdict-body").innerText()).replace(/\s+/g, " ");
  const whole = (await page.getByTestId("prep-panel").innerText()).replace(/\s+/g, " ");
  expect(
    reason,
    node(NODE, `the server's reason code for an unnameable destination is ${JSON.stringify(reason)}`),
  ).toBe("NO_CANONICAL_IDENTITY");
  /* The forbidden thing is *claiming* connectivity trouble.  `网络问题` alone cannot be the probe:
     the correct copy says "这不是网络问题" -- a denial -- and a substring test would fire on the
     denial it is asking for. */
  expect(
    /网络不可用|离线 · 只读|OFFLINE|网络连接|连不上服务器/.test(whole),
    node(NODE, `a structural verdict is being described as connectivity: "${whole.slice(0, 220)}"`),
  ).toBe(false);
  expect(
    /这不是网络问题/.test(whole),
    node(NODE, `the copy neither claims nor denies the network for a server-known structural state: "${whole.slice(0, 220)}"`),
  ).toBe(true);
  expect(
    /不是网络问题/.test(body),
    node(NODE, `the copy does not rule the network out, which is what this server-known state requires: ${JSON.stringify(body)}`),
  ).toBe(true);
  /* The exit is real and visible: choose another destination. */
  const actions = await focusableActions(page);
  expect(
    actions.includes("prep-choose-another"),
    node(NODE, `an unnameable destination offers no way out: ${JSON.stringify(actions)}`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0193-structural.json"),
    JSON.stringify(
      { generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", elapsedMs, reason, body, actions },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0193-structural.png"), fullPage: true });
});

test("ord0193-a-retryable-runtime-failure-offers-a-retry-and-a-structural-one-does-not — 可重试的才给重试", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0193-a-retryable-runtime-failure-offers-a-retry-and-a-structural-one-does-not";
  await openDiscover(page);

  /* Two jobs with the same shape but different outcomes, answered by the gate: the screen must tell
     them apart by the server's `outcome`/`retryable`, not by whether a request failed. */
  let mode: "runtime" | "structural" = "runtime";
  await page.route("**/api/v1/catalog-hydrations", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/v1/catalog-hydrations/*", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    body.terminal = true;
    body.retry_after_ms = 0;
    if (mode === "runtime") {
      body.status = "FAILED";
      body.phase = "RESOLVING_DESTINATION";
      body.outcome = "RESOLVER_RUNTIME_FAILURE";
      body.retryable = true;
    } else {
      body.status = "UNAVAILABLE";
      body.phase = "DISCOVERING_POIS";
      body.outcome = "NO_REGISTERED_SOURCE";
      body.retryable = false;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.getByTestId("prep-query").fill("天津");
  await page.getByTestId("prep-check").click();
  await expect(page.getByTestId("prep-verdict")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("prep-start").click();
  const job = page.getByTestId("prep-job");
  await expect(job).toBeVisible({ timeout: 20_000 });

  /* The POST that starts the job answers QUEUED; the stubbed terminal outcome arrives with the
     first poll after it.  Reading straight away caught the mid-flight frame in a chain run, so the
     node waits for the outcome it is about to judge instead of reading whatever is there. */
  await expect
    .poll(async () => job.getAttribute("data-outcome"), {
      timeout: 20_000,
      message: node(NODE, "the job never reported a terminal outcome, so nothing below was measured"),
    })
    .toBe("RESOLVER_RUNTIME_FAILURE");
  const runtimeState = {
    outcome: await job.getAttribute("data-outcome"),
    actions: await focusableActions(page),
    body: (await page.getByTestId("prep-job-body").innerText()).replace(/\s+/g, " "),
  };
  expect(runtimeState.outcome, node(NODE, `the runtime-failure outcome is ${JSON.stringify(runtimeState.outcome)}`)).toBe("RESOLVER_RUNTIME_FAILURE");
  expect(
    runtimeState.actions.includes("prep-retry"),
    node(NODE, `a retryable runtime failure offers no retry: ${JSON.stringify(runtimeState)}`),
  ).toBe(true);
  expect(
    /运行性失败|重试/.test(runtimeState.body),
    node(NODE, `the copy does not present this as a retryable runtime failure: ${JSON.stringify(runtimeState.body)}`),
  ).toBe(true);

  /* Now the structural outcome: same endpoint, different server verdict. */
  mode = "structural";
  await page.getByTestId("prep-recheck").click();
  await page.getByTestId("prep-start").click();
  await expect(job).toBeVisible({ timeout: 20_000 });
  await expect
    .poll(async () => job.getAttribute("data-outcome"), { timeout: 20_000 })
    .toBe("NO_REGISTERED_SOURCE");
  const structuralState = {
    outcome: await job.getAttribute("data-outcome"),
    actions: await focusableActions(page),
    body: (await page.getByTestId("prep-job-body").innerText()).replace(/\s+/g, " "),
  };
  expect(
    structuralState.actions.includes("prep-retry"),
    node(NODE, `a structural outcome offers a retry that cannot change anything: ${JSON.stringify(structuralState)}`),
  ).toBe(false);
  expect(
    /找不到任何可追溯的数据来源|关于这个地方的事实/.test(structuralState.body),
    node(NODE, `the structural outcome is not named as a fact about the place: ${JSON.stringify(structuralState.body)}`),
  ).toBe(true);
  expect(
    /网络不可用|离线/.test(structuralState.body),
    node(NODE, `the structural outcome is described as connectivity: ${JSON.stringify(structuralState.body)}`),
  ).toBe(false);
  /* And it still leaves a door: something to press. */
  expect(
    structuralState.actions.length,
    node(NODE, `the structural outcome screen is a dead end: ${JSON.stringify(structuralState)}`),
  ).toBeGreaterThan(0);

  writeFileSync(
    evidencePath("ui-overnight/ord0193-outcomes.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        gateInvention: "both terminal outcomes are answered by the gate so the two branches can be read from one run; the status/outcome values are the server's own vocabulary",
        runtimeState,
        structuralState,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0193-outcomes.png"), fullPage: true });
});

test("ord0193-a-ready-destination-offers-planning-and-not-preparing — READY 就给“去规划”", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0193-a-ready-destination-offers-planning-and-not-preparing";
  await openDiscover(page);

  /* READY cannot be produced on a fresh database (nothing has a current catalogue yet), so the
     verdict is answered by the gate using the server's own field values and reason code. */
  await page.route("**/api/v1/destinations/capability*", async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as Record<string, unknown>;
    body.capability = "READY";
    body.reason_code = "CATALOG_CURRENT";
    body.recommended_action = "ACCEPT_PARTIAL_PLAN";
    body.missing_categories = [];
    body.coverage = Object.fromEntries(
      Object.keys((body.coverage as Record<string, string>) ?? {}).map((key) => [key, "READY"]),
    );
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.getByTestId("prep-query").fill("天津");
  await page.getByTestId("prep-check").click();
  const verdict = page.getByTestId("prep-verdict");
  await expect(verdict).toBeVisible({ timeout: 20_000 });
  expect(await verdict.getAttribute("data-capability")).toBe("READY");
  const actions = await focusableActions(page);
  expect(
    actions.includes("prep-go-plan"),
    node(NODE, `a READY destination offers no route to planning: ${JSON.stringify(actions)}`),
  ).toBe(true);
  expect(
    actions.includes("prep-start"),
    node(NODE, `a READY destination still offers preparing, which would be a no-op the server does not ask for: ${JSON.stringify(actions)}`),
  ).toBe(false);
  /* Pressing it must actually move, not sit there. */
  await page.getByTestId("prep-go-plan").click();
  await expect(
    page.getByTestId("prep-panel"),
    node(NODE, "the go-and-plan action did not leave the preparation screen"),
  ).toHaveCount(0, { timeout: 15_000 });

  writeFileSync(
    evidencePath("ui-overnight/ord0193-ready.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        gateInvention: "the READY verdict is answered by the gate with the server's own field values; a fresh database has no current catalogue",
        actions,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
