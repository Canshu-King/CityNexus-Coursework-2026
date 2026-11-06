import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { openStageEntry } from "./_qoderStageEntry";

/**
 * ORD-0087 / ORD-0084 -- a commitment the server confirmed has to be able to reach the screen, and
 * what it says there is decided by fields, not by this file's guesses.
 *
 * `wallet.commitments` was declared in the shell's own `Wallet` type and drawn nowhere: both wallet
 * surfaces mapped `wallet.reservations` only.  So AR2's second gate ("a mock commitment may not
 * wear a suit") could not be run against the product at all -- there was nothing to look at.
 *
 * Four readings, because the order's four requirements are four different failures:
 *   real chain -- reset -> 准备舞台 -> 建一个硬承诺, then the card is read off the screen;
 *   UNKNOWN     -- an amount the server does not know must read 价格未知, never a fabricated 0;
 *   non-mock    -- a provider reference that is not the mock provider's must not be labelled fake;
 *   empty       -- no commitments means no card and no "模拟承诺" anywhere (empty is not mock).
 */

const FILE = "e2e/qoder-wallet-commitments.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const clean = (value: string | null | undefined) => String(value ?? "").replace(/\s+/g, " ").trim();

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

type ServerCommitment = {
  id?: string;
  kind?: string;
  level?: string;
  status?: string;
  external_ref?: string | null;
  monetary_value?: { amount_minor?: number | null; currency?: string | null } | null;
  monetary_value_status?: string | null;
  policy?: Record<string, unknown> | null;
};

/* Two boot shapes, because the arms need opposite things and the harness already puts a journey id
   into every context through its storageState.
   - `openOnJourney` seeds the named journey *before the first paint* (unconditionally, the way every
     other gate in this suite boots).  A guarded script was wrong here: the storageState id is
     already present, so the guard silently skipped the seed and the arm booted elsewhere.
   - `openFromWizard` carries no persistent script at all: it clears once, and afterwards the stage's
     own hand-over has to survive the reload it triggers -- that is the thing being measured. */
async function openOnJourney(page: Page, journeyId: string, path: string) {
  await page.addInitScript(
    ([id]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", "zh-CN");
    },
    [journeyId],
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(path, { waitUntil: "load" });
}

async function openFromWizard(page: Page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate(() => {
    localStorage.clear();
    /* The card's words are locale-dependent ("模拟承诺" / "价格未知"), so the arm names the language
       it is going to read instead of inheriting whatever the context defaulted to. */
    localStorage.setItem("citynexus.locale", "zh-CN");
  });
  await page.reload({ waitUntil: "load" });
}

async function pressStageAction(page: Page, id: string) {
  /* On a phone the stage entry is collapsed by design, so open it the way a traveller would. */
  await openStageEntry(page, `pressing ${id}`);
  const row = page.getByTestId(`sim-action-${id}`);
  await row.scrollIntoViewIfNeeded();
  const reasonId = await row.getAttribute("aria-describedby");
  if (await row.getAttribute("aria-disabled")) {
    return {
      pressed: false,
      reason: reasonId ? clean(await page.locator(`#${reasonId}`).innerText()) : "(blocked, no reason element)",
    };
  }
  await row.click();
  await expect(page.getByTestId("sim-outcome")).toBeVisible({ timeout: 60_000 });
  return { pressed: true, outcome: clean(await page.getByTestId("sim-outcome").innerText()) };
}

test("ord0087-booked-commitment-is-on-the-wallet-screen — 真链路建出来的承诺要在 Wallet 上看得见", async ({ page }) => {
  test.setTimeout(420_000);
  let journeyId = "";
  /* From the wizard, through the stage's own actions: no dependence on which scenario an earlier
     node left on the stage, and no persistent storage script that would fight the hand-over. */
  await openFromWizard(page);
  const ball = page.getByTestId("sim-ball");
  await expect(
    ball,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", "the wizard has no stage ball to press"),
  ).toHaveCount(1);
  await ball.click();
  await expect(
    page.getByTestId("sim-pop"),
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      "the ball was pressed and no stage window appeared on the wizard"),
  ).toBeVisible();

  /* M4's order, measured rather than assumed: seed -> plan -> book -> start.  A booking is refused
     once the journey is executing (the server's own words, from `review/bookings-*.json`:
     "booking requires a PLANNED Journey"), so this node puts a fresh seed on the stage rather than
     pressing at whatever state an earlier node left behind. */
  const reseeded = await pressStageAction(page, "reset");
  expect(
    reseeded.pressed,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", `the stage could not be re-seeded: ${JSON.stringify(reseeded)}`),
  ).toBe(true);
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 60_000 });

  let scene = await readScene(page);
  expect(
    scene.journey_status,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `after 重置 the server reports ${JSON.stringify(scene.journey_status)}, not the fresh seed this chain starts from`),
  ).toBe("DRAFT");
  const prepared = await pressStageAction(page, "prepare");
  expect(
    prepared.pressed,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", `准备舞台 was refused: ${JSON.stringify(prepared)}`),
  ).toBe(true);
  scene = await readScene(page);
  expect(
    scene.journey_status,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `the server says ${JSON.stringify(scene.journey_status)} after prepare, and a booking needs PLANNED`),
  ).toBe("PLANNED");
  expect(
    scene.plan_version_id,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", "the server still reports no plan on the stage, so nothing can be booked"),
  ).toBeTruthy();
  journeyId = String(scene.journey_id ?? "");

  /* "不许假定任意活动可订" has a readable shape: the row says *which* activity it will send before
     anything is pressed.  A bare 「建一个硬承诺」 is a button aimed at whatever the code guessed. */
  const bookRow = page.getByTestId("sim-action-book");
  await bookRow.scrollIntoViewIfNeeded();
  const planVersionId = String(scene.plan_version_id ?? "");
  const planResponse = await page.request.get(`/api/v1/plans/${planVersionId}`);
  const planTitles: string[] = planResponse.ok()
    ? (((await planResponse.json()) as { activities?: Array<{ title?: string }> }).activities ?? [])
        .map((item) => String(item.title ?? ""))
        .filter(Boolean)
    : [];
  const announced = clean(await bookRow.innerText());
  expect(
    planTitles.some((title) => announced.includes(title)),
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `the booking row names no activity it will send (reads ${JSON.stringify(announced)}; the plan's activities are ${JSON.stringify(planTitles.slice(0, 6))})`),
  ).toBe(true);

  /* The row names the activity it is aiming at, and the control beside it moves to the next
     candidate.  The demo plan carries one bookable activity and refuses the rest by name, so a
     node that pressed the first row would be proving its own guess, not the product. */
  /* ORD-0100 ①'s visible shape: the row already points at the activity the server gave a window for,
     so the first press is the one that books.  Before this round the default was whatever the plan
     array held first, and this loop had to hunt through the candidates to find a 201. */
  await page.getByTestId("sim-pop").screenshot({
    path: evidencePath("ui-overnight/stage-walk-1-booking-row.png"),
  });
  const attempts: string[] = [];
  const http: Array<{ status: number; body: unknown }> = [];
  page.on("response", (response) => {
    if (!response.url().endsWith("/bookings")) return;
    void response
      .json()
      .then((body) => http.push({ status: response.status(), body }))
      .catch(() => http.push({ status: response.status(), body: null }));
  });
  let booked: string | null = null;
  for (let step = 0; step < 8 && !booked; step += 1) {
    /* The verdict comes from the server's own answer, not from the panel's sentence: the sentence is
       a translation and is locale-dependent, and an earlier version of this arm read past its own
       201 because it was matching Chinese words on an English screen. */
    const answered = page
      .waitForResponse((response) => response.url().endsWith("/bookings"), { timeout: 60_000 })
      .catch(() => null);
    const pressed = await pressStageAction(page, "book");
    if (!pressed.pressed) {
      attempts.push(`blocked: ${pressed.reason}`);
      break;
    }
    attempts.push(pressed.outcome);
    const response = await answered;
    if (response && response.status() === 201) {
      const created = (await response.json()) as { id?: string; status?: string; external_ref?: string };
      booked = String(created.id ?? "yes");
      expect(
        created.status,
        node("ord0087-booked-commitment-is-on-the-wallet-screen", `the server created a booking that is not CONFIRMED: ${JSON.stringify(created)}`),
      ).toBe("CONFIRMED");
      break;
    }
    const next = page.getByTestId("sim-book-next");
    if ((await next.count()) === 0) break;
    await next.click();
    await page.waitForTimeout(150);
  }
  expect(
    booked,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `no activity on this plan could be booked through the panel; the server answered ${JSON.stringify(http.slice(0, 6))} and the panel said ${JSON.stringify(attempts.slice(0, 6))}`),
  ).toBeTruthy();
  expect(
    attempts.length,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `the default target needed ${attempts.length} presses to book; ORD-0100 ① is that it needs one: ${JSON.stringify(attempts.slice(0, 4))}`),
  ).toBe(1);
  await page.getByTestId("sim-pop").screenshot({
    path: evidencePath("ui-overnight/stage-walk-2-outcome.png"),
  });

  /* Thumb-only walk, because the desktop route is not the phone's path.  `v8Routes.ts` leaves
     `bookings` off `mobileRoutes` on purpose (Wallet lives under Trips and Me), so a node that
     visited `/v8/bookings` would prove a screen no teacher can open on a 390px phone. */
  await expect
    .poll(() => page.getByTestId("nav-bookings").count(), {
      timeout: 15_000,
      intervals: [250],
      message: node("ord0087-booked-commitment-is-on-the-wallet-screen",
        "the phone bottom nav suddenly has a Wallet entry -- this walk was written for the case where it does not"),
    })
    .toBe(0);
  const trips = page.getByTestId("nav-trips");
  await expect(
    trips,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", "the phone nav has no Trips tab to walk through"),
  ).toBeVisible();
  await trips.click();
  const subTabs = page.getByTestId("trips-tabs");
  await expect(
    subTabs,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", "tapping Trips did not open the trip sections"),
  ).toBeVisible({ timeout: 30_000 });
  await subTabs.getByTestId("trips-tab-bookings").click();
  const card = page.getByTestId("wallet-commitment").first();
  await expect(
    card,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      "the commitment was created but never reaches the phone's Wallet screen (Today -> Trips -> 预订)"),
  ).toBeVisible({ timeout: 30_000 });
  const cardText = clean(await card.innerText());
  await card.screenshot({
    path: evidencePath("ui-overnight/stage-walk-3-wallet-card.png"),
  });
  const listed = await page.request.get(`/api/v1/journeys/${journeyId}/wallet`);
  const wallet = (await listed.json()) as { commitments?: ServerCommitment[] };
  expect(
    (wallet.commitments ?? []).length,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", "the server lists commitments the screen does not draw"),
  ).toBeGreaterThan(0);
  const stored = (wallet.commitments ?? [])[0] as ServerCommitment;
  expect(
    cardText,
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `a mock-provider commitment is drawn without saying it is simulated: ${JSON.stringify(cardText)}`),
  ).toContain("模拟承诺");
  expect(
    String(stored.external_ref ?? "").startsWith("mock-"),
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `the label says simulated but the server's external_ref is ${JSON.stringify(stored.external_ref)} -- the label is not field-driven`),
  ).toBe(true);
  /* The three things the traveller must be able to check before trusting a replan story: what kind
     of promise, how strong, and when it is due. */
  expect(
    cardText,
    node("ord0087-booked-commitment-is-on-the-wallet-screen", `kind/level/status are not on the card: ${JSON.stringify(cardText)}`),
  ).toContain(String(stored.level));
  const window = clean(await card.getByTestId("wallet-commitment-window").innerText());
  expect(
    /\d/.test(window),
    node("ord0087-booked-commitment-is-on-the-wallet-screen", `the card states no time window: ${JSON.stringify(window)}`),
  ).toBe(true);
  const amount = clean(await card.getByTestId("wallet-commitment-amount").innerText());
  expect(
    stored.monetary_value_status === "KNOWN" ? /\d/.test(amount) : amount.includes("价格未知"),
    node("ord0087-booked-commitment-is-on-the-wallet-screen",
      `the server's monetary_value_status is ${JSON.stringify(stored.monetary_value_status)} but the card reads ${JSON.stringify(amount)}`),
  ).toBe(true);

  /* ORD-0087 acceptance asks for the card as pictures too, at both phone widths and both themes, with
     the metadata that says what build and what kind of commitment they show. */
  const shots: Array<{ file: string; width: number; scheme: string }> = [];
  for (const width of [390, 412]) {
    for (const scheme of ["light", "dark"] as const) {
      await page.setViewportSize({ width, height: 844 });
      await page.emulateMedia({ colorScheme: scheme, media: "screen" });
      const file = `ui-overnight/wallet-commitment-${width}-${scheme}.png`;
      await page.screenshot({ path: evidencePath(file), fullPage: true });
      shots.push({ file, width, scheme });
    }
  }
  writeFileSync(
    evidencePath("ui-overnight/wallet-commitment-shots.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0087",
        /* What the pictures are of: the phone's own walk, not a URL typed by the test. */
        reachedBy: "today -> nav-trips -> trips-tab-bookings",
        cardText,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        runId: process.env.CITYNEXUS_E2E_RUN_ID ?? "unknown",
        frontendPort: process.env.CITYNEXUS_E2E_PORT ?? "unknown",
        backendPort: process.env.CITYNEXUS_BACKEND_PORT ?? "unknown",
        /* Named for the variable it reads: `aiDisabled: "false"` was this file's own lie about a run
           that had AI switched off. */
        aiEnabled: process.env.CITYNEXUS_AI_ENABLED ?? "unknown",
        demoMode: (await readScene(page)).demo_mode ?? null,
        /* The thing the label is built from: mock-booking:… means the card must say 模拟承诺. */
        mockOrReal: String(stored.external_ref ?? "").startsWith("mock-") ? "mock" : "real",
        monetaryValueStatus: stored.monetary_value_status ?? null,
        shots,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );

  writeFileSync(
    evidencePath("ui-overnight/wallet-commitment-real.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0087",
        node: "ord0087-booked-commitment-is-on-the-wallet-screen",
        attempts,
        http,
        serverCommitment: stored,
        card: cardText,
        window,
        amount,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

/**
 * A journey of their own, created through the product API.  The stub arms used to borrow whatever
 * `/demo/scene` named, and a reset left unfinished by another node can leave the stage without a
 * scenario -- the arm then boots onto the wizard and fails for a reason that has nothing to do with
 * the card it is reading.  Same shape as the seed project's own creation.
 */
async function seedOwnJourney(page: Page): Promise<string> {
  const traveler = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "Qoder wallet gate", locale: "en-GB", timezone: "Asia/Shanghai" },
    })
  ).json();
  const journey = await (
    await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: traveler.id,
        title: "Wallet gate trip",
        city_code: "CN-TIANJIN",
        timezone: "Asia/Shanghai",
        start_at: "2026-10-09T18:00:00+08:00",
        end_at: "2026-10-11T15:00:00+08:00",
        budget: { amount_minor: 120000, currency: "CNY" },
        stay_budget: { amount_minor: 70000, currency: "CNY" },
        party_size: 2,
        occupancy: { adults: 2, rooms: 1 },
        arrival_anchor: { at: "2026-10-09T18:00:00+08:00", location: "Tianjin West Station" },
        departure_anchor: { at: "2026-10-11T15:00:00+08:00", location: "Tianjin Binhai Airport" },
      },
    })
  ).json();
  return String(journey.id);
}

/** A fake-data substitute going back through the same renderer, because that is where a lie would
 *  appear: an unknown price drawn as ¥0.00 reads as a free cancellation, not as unknown. */
const stubWallet = async (page: Page, commitments: unknown[]) => {
  await page.route("**/api/v1/journeys/*/wallet", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ journey_id: "stub", reservations: [], commitments, offline_ready: false }),
    }),
  );
};

test("ord0087-unknown-price-is-said-unknown — 不知道价格就不许显示 0", async ({ page }) => {
  test.setTimeout(240_000);
  const journeyId = await seedOwnJourney(page);
  expect(journeyId, "the gate could not create its own journey through the product API").toMatch(/^[0-9a-f-]{36}$/);
  await stubWallet(page, [
    {
      id: "aaaaaaaa-0000-4000-8000-000000000001",
      journey_id: journeyId,
      activity_id: null,
      kind: "BOOKING",
      level: "C3_Guaranteed",
      status: "ACTIVE",
      monetary_value: { amount_minor: 12000, currency: "CNY" },
      monetary_value_status: "ESTIMATED",
      external_ref: "mock-booking:stub:confirmed",
      policy: { semantic_key: "place-river-cruise", scheduled_start_at: "2026-10-10T15:30:00+08:00", scheduled_end_at: "2026-10-10T16:30:00+08:00", fixed_schedule: true },
    },
  ]);
  await openOnJourney(page, journeyId, "/v8/bookings");
  const card = page.getByTestId("wallet-commitment").first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  const amount = clean(await card.getByTestId("wallet-commitment-amount").innerText());
  expect(
    amount,
    node("ord0087-unknown-price-is-said-unknown", `an UNKNOWN price is drawn as ${JSON.stringify(amount)}`),
  ).toContain("价格未知");
  expect(
    /0\.00|¥0|￥0|CNY 0/.test(amount),
    node("ord0087-unknown-price-is-said-unknown", `a zero is shown for a price the server never gave: ${JSON.stringify(amount)}`),
  ).toBe(false);
});

test("ord0087-simulated-label-comes-from-the-field — 真供应商不许被误标", async ({ page }) => {
  test.setTimeout(240_000);
  const journeyId = await seedOwnJourney(page);
  expect(journeyId, "the gate could not create its own journey through the product API").toMatch(/^[0-9a-f-]{36}$/);
  await stubWallet(page, [
    {
      id: "aaaaaaaa-0000-4000-8000-000000000002",
      journey_id: journeyId,
      activity_id: null,
      kind: "BOOKING",
      level: "C4_Anchored",
      status: "ACTIVE",
      monetary_value: { amount_minor: 12000, currency: "CNY" },
      monetary_value_status: "KNOWN",
      external_ref: "provider-ref-77",
      policy: { semantic_key: "place-river-cruise", scheduled_start_at: "2026-10-10T15:30:00+08:00", scheduled_end_at: "2026-10-10T16:30:00+08:00", fixed_schedule: true },
    },
  ]);
  await openOnJourney(page, journeyId, "/v8/bookings");
  const card = page.getByTestId("wallet-commitment").first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  expect(
    await card.getAttribute("data-simulated"),
    node("ord0087-simulated-label-comes-from-the-field", "a provider-confirmed commitment is marked simulated"),
  ).toBe("false");
  expect(
    clean(await card.innerText()),
    node("ord0087-simulated-label-comes-from-the-field", "the screen calls a real provider reference simulated"),
  ).not.toContain("模拟承诺");
  expect(
    clean(await card.getByTestId("wallet-commitment-amount").innerText()),
    node("ord0087-simulated-label-comes-from-the-field", "a KNOWN amount must be shown, and it is not"),
  ).toContain("120");
});

test("ord0087-empty-list-is-not-labelled-mock — 没有承诺时不该有任何“模拟承诺”", async ({ page }) => {
  test.setTimeout(240_000);
  const journeyId = await seedOwnJourney(page);
  expect(journeyId, "the gate could not create its own journey through the product API").toMatch(/^[0-9a-f-]{36}$/);
  await stubWallet(page, []);
  await openOnJourney(page, journeyId, "/v8/bookings");
  await expect(page.getByTestId("wallet-commitment")).toHaveCount(0);
  const body = clean(await page.locator("body").innerText());
  expect(
    body.includes("模拟承诺"),
    node("ord0087-empty-list-is-not-labelled-mock", "the simulated label appears with no commitment at all"),
  ).toBe(false);
});
