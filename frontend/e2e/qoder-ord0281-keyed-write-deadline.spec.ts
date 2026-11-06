import { expect, test } from "@playwright/test";

/* ORD-0281 (keyed half) -- a write leg that carries an `Idempotency-Key` is bounded by this device, and the
   screen says who ran out of patience.

   The measured symptom (two generations, same shape): a write inside a SQLite lock window waited out the whole
   30 s lock wait -- 32,286 / 32,328 / 32,335 ms -- while `readDeadline.ts` covered reads only. HJ-P3's shape
   block added the part nobody could see from the wrapper: most write sites never enter that wrapper at all. On
   the base I am building on (`90d29a172e186aab6e6cb138ab4583ccf93bfa68`) the keyed subset is 6 legs, 2 of them
   bare `fetch` calls in panels that never imported any deadline module. This gate covers the Live recovery
   panel's own keyed accept leg and the wrapper's rules; the other legs are named in the receipt, not silently
   averaged in.

   What makes an arm an arm here: every judgement below reads TEXT THE TRAVELLER SEES or a value the browser
   actually observed. `data-*` attributes are not evidence, and a route handler's own counter is not a server
   fact. Two rules borrowed from ORD-0279/0280 are load-bearing:
     - comparing the server's words uses equality, never `in` (a punctuation drift must show up as a mismatch);
     - a positive control proves that equality is real, by asserting one expectation the bytes cannot satisfy.

   Time discipline: the shipped bound is 60 s, which no gate can wait for honestly. T-c asks for the constant to
   be moved and the trigger to follow, so the run under test is always made with a patched constant, and this
   file's own default (`CNX_0281_BOUND_MS`) states what the tree under it is set to. The 60 s default is asserted
   separately, as a shipped-value check rather than as a wait. */

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;
const BOUND_MS = Number(process.env.CNX_0281_BOUND_MS ?? "2000");
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "";
const EVIDENCE = process.env.CITYNEXUS_EVIDENCE_DIR ?? ".";
const PRISTINE = process.env.CNX_0281_PRISTINE === "1";
const ARMS = ["TA", "TB", "TC", "TD", "K1", "K2", "K3"] as const;
type Arm = (typeof ARMS)[number];

const ACCEPT_PATH = /\/api\/v1\/replans\/[^/]+\/accept$/;
const SENTENCE_MARK = "This device stopped waiting after";
const NETWORK_BLAME = "Could not reach the server";

type Reading = {
  arm: Arm;
  clicked: number;
  settled_at: number | null;
  elapsed_ms: number | null;
  on_screen: string | null;
  /** what a wire- or module-level arm actually observed; `on_screen` stays null there so no cell can borrow
   *  a surface reading it never took (criterion 2 of bus issuecomment-5829112344). */
  observed_outcome?: string | null;
  sentence_seconds_named: number | null;
  had_network_blame: boolean | null;
  surface_not_judged?: string;
  /** the rendered words themselves, head only: a label derived from the text is not the text */
  screen_text_head?: string | null;
  server_said: "answered-200" | "never-answered" | "answered-late" | string;
  keys_seen: string[];
  verdict: string;
  note?: string;
};

const readings: Record<Arm, Partial<Reading>> = {};

/** One handler per test, so a counter cannot be borrowed from another leg's request. */
function hangOrAnswer(mode: "never" | "late" | "ok", ms = 0) {
  const seen: { key: string | null; at: number }[] = [];
  return {
    seen,
    handler: async (route: import("@playwright/test").Route) => {
      seen.push({ key: route.request().headers()["idempotency-key"] ?? null, at: Date.now() });
      if (mode === "ok") return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      if (mode === "late") {
        await new Promise((r) => setTimeout(r, ms));
        return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      }
      /* Never fulfil: the request is genuinely outstanding, which is the only shape that can tell "the
         server has not answered" from "the server answered badly". */
      return new Promise<void>(() => {});
    },
  };
}

async function call<T>(
  request: import("@playwright/test").APIRequestContext,
  url: string,
  method: "GET" | "POST" = "GET",
  data?: unknown,
  retries = 6,
): Promise<T> {
  /* The authoritative single-process SQLite face genuinely answers 500 while another request holds the write
     transaction (ORD-0262's measured shape), and a demo journey can be half-built by a retryable 500.  A
     transient 500 is retried; any other failure still throws with the body, because "the product refused" is a
     reading and must not be swallowed by my convenience. */
  let last = "";
  for (let attempt = 0; attempt < retries; attempt += 1) {
    const response =
      method === "GET"
        ? await request.get(`${API}${url}`)
        : await request.post(`${API}${url}`, { data: data ?? {} });
    if (response.ok()) return (await response.json()) as T;
    last = `${url} -> HTTP ${response.status()}: ${(await response.text()).slice(0, 160)}`;
    if (response.status() !== 500) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(last);
}

type Activity = { id: string };

/** A journey the traveller is genuinely inside, through the product's own endpoints. */
async function startedJourney(request: import("@playwright/test").APIRequestContext) {
  const demo = await call<{ journey_id: string }>(request, "/api/v1/demo/reset", "POST");
  const plans = await call<{ id: string; activities: Activity[] }[]>(
    request,
    `/api/v1/journeys/${demo.journey_id}/plans/generate`,
    "POST",
    { traveler: {} },
  );
  await call(request, `/api/v1/journeys/${demo.journey_id}/plans/${plans[0].id}/select`, "POST");
  await call(request, `/api/v1/journeys/${demo.journey_id}/start`, "POST");
  return { journeyId: demo.journey_id, activities: plans[0].activities };
}

const PROPOSAL = {
  id: "ord0281-proposal",
  /* The panel's own filter is `status === "PRESENTED" || "GENERATED"` (LiveRecovery.tsx) -- a status of
     "PENDING" is not one this surface ever treats as an offer, which the first run of this file proved the
     hard way: no button rendered, and the arm measured an empty page. */
  status: "PRESENTED",
  hard_constraints_passed: true,
  locked_commitments_preserved: true,
  diff: { removed: [], added: [], moved: [] },
  explanation: { impact_model: "ROUTE_CLOSED", summary: "one stop closed" },
};

/** The Live page with one pending proposal, reached through the product's own shell. */
async function openLiveWithProposal(page: import("@playwright/test").Page, journeyId: string) {
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.locale", "en");
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.demo.journey_id", id);
  }, journeyId);
  await page.route(`**/api/v1/journeys/${journeyId}/replans`, (route) => route.fulfill({ json: [PROPOSAL] }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/live");
  const accept = page.getByTestId("live-accept").first();
  await expect(accept).toBeVisible({ timeout: 30_000 });
  return accept;
}

/** Wait for the device sentence, or for a success message, whichever the tree under test produces. */
async function waitForOutcome(page: import("@playwright/test").Page, startedAt: number, upToMs: number) {
  const deadline = startedAt + upToMs;
  for (;;) {
    const text = await page.getByTestId("live-recovery").innerText().catch(() => "");
    if (text.includes(SENTENCE_MARK)) {
      const named = Number(text.slice(text.indexOf(SENTENCE_MARK)).match(/after (\d+) s/)?.[1] ?? NaN);
      return { kind: "bound-sentence" as const, named, at: Date.now(), had_network_blame: text.includes(NETWORK_BLAME), text };
    }
    if (text.includes("Your trip has been updated")) {
      return { kind: "success" as const, named: null, at: Date.now(), had_network_blame: text.includes(NETWORK_BLAME), text };
    }
    if (Date.now() > deadline) {
      return { kind: "nothing" as const, named: null, at: Date.now(), had_network_blame: text.includes(NETWORK_BLAME), text };
    }
    await page.waitForTimeout(50);
  }
}

function record(arm: Arm, r: Partial<Reading>) {
  readings[arm] = { ...(readings[arm] ?? {}), ...r };
}

test.describe.serial("ORD-0281 keyed write deadline", () => {
  /* Serial on purpose: every case resets the same run-owned database through /demo/reset, so two workers
     would each be reading a journey the other had just wiped. Arms that do not apply to the tree under test
     are SKIPPED by name below -- a skip is a stated boundary, while a red that hides the rest is not. */
  test("ord0281-TA a keyed write answered inside the bound shows no deadline sentence", async ({
    page,
    request,
  }) => {
    const { journeyId } = await startedJourney(request);
    const leg = hangOrAnswer("ok");
    await page.route(ACCEPT_PATH, leg.handler);
    const accept = await openLiveWithProposal(page, journeyId);
    const started = Date.now();
    await accept.click();
    const out = await waitForOutcome(page, started, BOUND_MS + 4_000);
    record("TA", {
      arm: "TA", clicked: started, settled_at: out.at, elapsed_ms: out.at - started,
      on_screen: out.kind, sentence_seconds_named: out.named, had_network_blame: out.had_network_blame,
      screen_text_head: out.text.slice(0, 170),
      server_said: "answered-200", keys_seen: leg.seen.map((s) => s.key ?? ""),
      verdict: out.kind === "success" && !out.had_network_blame ? "MET" : "NOT_MET",
    });
    expect(out.kind).toBe("success");
    expect(out.text.includes(SENTENCE_MARK)).toBe(false);
    expect(out.had_network_blame).toBe(false);
  });

  test("ord0281-TB an unanswered keyed write ends at our bound and names itself, not the network", async ({
    page,
    request,
  }) => {
    test.skip(PRISTINE, "the shipped 60 s bound cannot be waited out inside a gate window; this arm needs a patched constant");
    const { journeyId } = await startedJourney(request);
    const leg = hangOrAnswer("never");
    await page.route(ACCEPT_PATH, leg.handler);
    const accept = await openLiveWithProposal(page, journeyId);
    const started = Date.now();
    await accept.click();
    const out = await waitForOutcome(page, started, BOUND_MS + 15_000);
    record("TB", {
      arm: "TB", clicked: started, settled_at: out.at, elapsed_ms: out.at - started,
      on_screen: out.kind, sentence_seconds_named: out.named, had_network_blame: out.had_network_blame,
      screen_text_head: out.text.slice(0, 170),
      server_said: "never-answered", keys_seen: leg.seen.map((s) => s.key ?? ""),
      verdict:
        out.kind === "bound-sentence" &&
        out.named === Math.round(BOUND_MS / 1000) &&
        !out.had_network_blame &&
        out.at - started >= BOUND_MS - 250
          ? "MET"
          : "NOT_MET",
    });
    /* The sentence must carry the bound that fired, must arrive no earlier than that bound, and must not
       blame the network -- that blame is what the panel did before, and it is the half of the defect the
       traveller reads. */
    expect(out.kind).toBe("bound-sentence");
    expect(out.named).toBe(Math.round(BOUND_MS / 1000));
    expect(out.at - started).toBeGreaterThanOrEqual(BOUND_MS - 250);
    expect(out.had_network_blame).toBe(false);
  });

  test("ord0281-TC the bound is one named source: halving it moves when the sentence arrives", async ({
    page,
    request,
  }) => {
    /* The driver patches the tree's own constant to half the baseline and declares both numbers:
       CNX_0281_BOUND_MS = what this tree is set to, CNX_0281_BASELINE_MS = the un-patched bound.  If the
       constant were dead -- a second inline copy, or a wrapper nobody calls -- the rendered number would stay
       at the baseline and so would the arrival time, and this arm goes red on both counts. */
    const baseline = Number(process.env.CNX_0281_BASELINE_MS ?? "0");
    test.skip(PRISTINE, "TC exists to move the constant, so it cannot run against the shipped value");
    const { journeyId } = await startedJourney(request);
    const leg = hangOrAnswer("never");
    await page.route(ACCEPT_PATH, leg.handler);
    const accept = await openLiveWithProposal(page, journeyId);
    const started = Date.now();
    await accept.click();
    const out = await waitForOutcome(page, started, baseline + 15_000);
    const elapsed = out.at - started;
    record("TC", {
      arm: "TC", clicked: started, settled_at: out.at, elapsed_ms: elapsed,
      on_screen: out.kind, sentence_seconds_named: out.named, had_network_blame: out.had_network_blame,
      screen_text_head: out.text.slice(0, 170),
      server_said: "never-answered", keys_seen: leg.seen.map((s) => s.key ?? ""),
      verdict:
        out.kind === "bound-sentence" &&
        out.named === Math.round(BOUND_MS / 1000) &&
        elapsed < baseline - 250
          ? "MET"
          : "NOT_MET",
      note: `bound_under_test=${BOUND_MS};baseline=${baseline}`,
    });
    expect(out.kind).toBe("bound-sentence");
    expect(out.named).toBe(Math.round(BOUND_MS / 1000));
    expect(elapsed).toBeLessThan(baseline - 250);
    expect(out.had_network_blame).toBe(false);
  });

  test("ord0281-TD a retry after the bound replays the same key and never claims the write failed", async ({
    /* A bound that never fires lets the answer land AFTER we gave up. This arm exists so that shape is
       measured rather than assumed: a later 200 must not be allowed to read as success. */
    page,
    request,
  }) => {
    test.skip(PRISTINE, "two aborts at the shipped 60 s would measure my timeout ceiling, not the product");
    const { journeyId } = await startedJourney(request);
    const leg = hangOrAnswer("never");
    await page.route(ACCEPT_PATH, leg.handler);
    const accept = await openLiveWithProposal(page, journeyId);
    const started = Date.now();
    await accept.click();
    const first = await waitForOutcome(page, started, BOUND_MS + 15_000);
    await accept.click();
    const second = await waitForOutcome(page, Date.now(), BOUND_MS + 15_000);
    const keys = leg.seen.map((s) => s.key ?? "");
    const text = second.text;
    record("TD", {
      arm: "TD", clicked: started, settled_at: second.at, elapsed_ms: second.at - started,
      on_screen: second.kind, sentence_seconds_named: second.named,
      had_network_blame: second.had_network_blame, screen_text_head: second.text.slice(0, 170),
      server_said: "never-answered", keys_seen: keys,
      verdict:
        first.kind === "bound-sentence" && second.kind === "bound-sentence" && keys.length === 2 &&
        keys[0] === keys[1] && keys[0].startsWith("accept-") &&
        /outcome of this write is unknown/i.test(text) && !/Your trip has been updated/i.test(text)
          ? "REPLAY_SHAPE_ONLY_NOT_EXACTLY_ONCE"
          : "NOT_MET",
    });
    expect(first.kind).toBe("bound-sentence");
    expect(keys.length).toBe(2);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[0].startsWith("accept-")).toBe(true);
    /* Equality is what makes "same key" mean something: prove the comparison can fail. */
    expect(keys[0] === "accept-" + PROPOSAL.id).toBe(true);
    /* The sentence must carry the uncertainty, and must not have been replaced by a success claim.  (The
       first version of this line forbade the phrase 'did not happen', which my own copy quotes verbatim --
       a negative control that cannot be satisfied proves nothing about the product.) */
    expect(/outcome of this write is unknown/i.test(text)).toBe(true);
    expect(/Your trip has been updated/i.test(text)).toBe(false);
  });

  test("ord0281-K1 a keyless write is passed through, not bounded, by this order", async ({ page }) => {
    /* A module specifier only resolves against a real origin: on `about:blank` the import below fails for a
       reason that has nothing to do with the product, which is exactly what the first run of this arm did. */
    test.skip(PRISTINE, "K1 races the bound at a fixed probe value; under 60 s it would measure nothing but the harness");
    await page.goto("/");
    /* Both legs hit a handler that never answers. The keyed one must be ended by our timer; the keyless one
       must still be waiting -- that difference is the whole content of Q33=①, and it can only be seen by
       racing the same request shape against a timer. */
    await page.route("**/api/v1/probe/keyless", () => new Promise<void>(() => {}));
    await page.route("**/api/v1/probe/keyed", () => new Promise<void>(() => {}));
    const probe = await page.evaluate(async (bound) => {
      const mod = await import("/src/lib/writeDeadline.ts");
      const race = async (label: string, init: RequestInit) => {
        const started = Date.now();
        return Promise.race([
          mod
            .fetchWithWriteDeadline(`/api/v1/probe/${label}`, init, bound)
            .then(() => ({ label, outcome: "answered", at: Date.now() - started }))
            .catch((error: unknown) => ({
              label,
              outcome: error instanceof Error ? error.name : "unknown",
              at: Date.now() - started,
            })),
          new Promise((r) => setTimeout(() => r({ label, outcome: "still-pending", at: Date.now() - started }), bound + 1_000)),
        ]);
      };
      const keyless = await race("keyless", { method: "POST" });
      const keyed = await race("keyed", { method: "POST", headers: { "Idempotency-Key": "k1" } });
      return {
        keyless,
        keyed,
        reads_header: mod.carriesIdempotencyKey({ method: "POST", headers: { "Idempotency-Key": "k" } }),
        rejects_absent: mod.carriesIdempotencyKey({ method: "POST" }),
      };
    }, BOUND_MS);
    record("K1", {
      arm: "K1",
      elapsed_ms: probe.keyed.at,
      on_screen: null,
      observed_outcome: `${probe.keyless.outcome}/${probe.keyed.outcome}`,
      server_said: "not-applicable",
      keys_seen: [],
      had_network_blame: null,
      surface_not_judged: "wire-level leg race; this arm never reads the panel",
      verdict:
        probe.keyless.outcome === "still-pending" &&
        probe.keyed.outcome === "WriteDeadlineMissed" &&
        probe.keyed.at >= BOUND_MS - 250 &&
        probe.keyed.at < BOUND_MS + 2_000 &&
        probe.reads_header === true &&
        probe.rejects_absent === false
          ? "MET"
          : "NOT_MET",
      note: "module-level in real browser: the product has no keyless surface that renders this sentence",
    });
    expect(probe.keyless.outcome).toBe("still-pending");
    expect(probe.keyed.outcome).toBe("WriteDeadlineMissed");
    expect(probe.reads_header).toBe(true);
    expect(probe.rejects_absent).toBe(false);
  });

  test("ord0281-K2 the shipped numbers are ordered so the client never cuts in front of the server", async ({
    page,
  }) => {
    test.skip(!PRISTINE, "K2 reads the shipped constant; on a patched tree it would be judging the patch itself");
    await page.goto("/");
    const shipped = await page.evaluate(async () => {
      const mod = await import("/src/lib/writeDeadline.ts");
      return { ...mod.writeDeadlineNumbersAreOrdered(), copy: mod.writeDeadlineCopy(false, 60_000) };
    });
    record("K2", {
      arm: "K2", on_screen: null, observed_outcome: shipped.copy.title,
      server_said: "not-applicable", keys_seen: [],
      elapsed_ms: null, sentence_seconds_named: shipped.client_ms / 1000,
      had_network_blame: null,
      surface_not_judged: "the copy builder's return value and the shipped constants, not a panel read",
      verdict:
        shipped.client_ms === 60_000 && shipped.server_ms === 45_000 && shipped.client_above_server
          ? "MET"
          : "NOT_MET",
    });
    expect(shipped.client_ms).toBe(60_000);
    expect(shipped.server_ms).toBe(45_000);
    expect(shipped.client_above_server).toBe(true);
    expect(shipped.copy.title).toBe("This device stopped waiting after 60 s");
  });

  test("ord0281-K3 the read wrapper delegates a keyed write, and leaves a keyless one alone", async ({
    page,
  }) => {
    /* Why this arm exists: the first run tried to kill the delegation inside `readDeadline.ts` and watched the
       panel arm stay GREEN -- LiveRecovery calls the write wrapper directly, so that mutant was a no-op for that
       leg, not a surviving spec.  The legs that depend on the delegation are the client ones
       (`api/citynexus.ts` -> `requestJson` -> `fetchWithReadDeadline`), so this measures that path.
       Module-level and labelled as such: the surface that renders it is the simulation console. */
    test.skip(PRISTINE, "K3 inherits the wrapper's default bound, so it needs the patched constant too");
    await page.goto("/");
    await page.route("**/api/v1/prodeleg/**", () => new Promise<void>(() => {}));
    const probe = await page.evaluate(async (bound) => {
      const read = await import("/src/lib/readDeadline.ts");
      const race = (label: string, init: RequestInit) =>
        Promise.race([
          read
            .fetchWithReadDeadline(`/api/v1/prodeleg/${label}`, init, bound)
            .then(() => "answered")
            .catch((error: unknown) => (error instanceof Error ? error.name : "unknown")),
          new Promise((r) => setTimeout(() => r("still-pending"), bound + 1_000)),
        ]);
      return {
        keyed: await race("keyed", { method: "POST", headers: { "Idempotency-Key": "k3" } }),
        keyless: await race("keyless", { method: "POST" }),
        read_name_kept: (() => {
          const e = new Error("x");
          e.name = "ReadDeadlineMissed";
          return read.isReadDeadlineMissed(e) === true && read.isReadDeadlineMissed(new Error("y")) === false;
        })(),
      };
    }, BOUND_MS);
    record("K3", {
      arm: "K3",
      on_screen: null,
      observed_outcome: `${probe.keyed}/${probe.keyless}`,
      elapsed_ms: null,
      sentence_seconds_named: null,
      had_network_blame: null,
      surface_not_judged: "module-level race through readDeadline/writeDeadline",
      server_said: "not-applicable",
      keys_seen: [],
      verdict:
        probe.keyed === "WriteDeadlineMissed" && probe.keyless === "still-pending" && probe.read_name_kept
          ? "MET"
          : "NOT_MET",
      note: "module-level: the client's own path; the read error name is not reused for writes",
    });
    expect(probe.keyed).toBe("WriteDeadlineMissed");
    expect(probe.keyless).toBe("still-pending");
    expect(probe.read_name_kept).toBe(true);
  });
});

/** One file per arm: with more than one worker a shared file is a last-writer-wins contest, and the merged
    reading it produced looked like a suite where only one arm had run. */
test.afterEach(async ({}, testInfo) => {
  const fs = await import("fs");
  const path = await import("path");
  const arm = String(testInfo.title).match(/ord0281-(T[A-D]|K[0-9])/)?.[1] ?? testInfo.title;
  const dir = path.join(EVIDENCE, "ord0281");
  fs.mkdirSync(dir, { recursive: true });
  const body = {
    file: "e2e/qoder-ord0281-keyed-write-deadline.spec.ts",
    runId: RUN,
    bound_ms_under_test: BOUND_MS,
    baseline_ms_declared: Number(process.env.CNX_0281_BASELINE_MS ?? "0"),
    pristine_tree: PRISTINE,
    arm,
    status: testInfo.status,
    expectation: testInfo.expectedStatus,
    reading: readings[arm] ?? null,
  };
  fs.writeFileSync(path.join(dir, `arm_${arm}.json`), JSON.stringify(body, null, 1));
});

test.afterAll(async () => {
  const fs = await import("fs");
  const path = await import("path");
  const dir = path.join(EVIDENCE, "ord0281");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "write_deadline_arms.json"),
    JSON.stringify({
      file: "e2e/qoder-ord0281-keyed-write-deadline.spec.ts",
      runId: RUN,
      bound_ms_under_test: BOUND_MS,
      pristine_tree: PRISTINE,
      arms: readings,
    }, null, 1),
  );
});
