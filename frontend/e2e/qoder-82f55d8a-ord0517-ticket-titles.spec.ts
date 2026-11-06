/**
 * ORD-0517 (lane 82f55d8a) -- a ticket-fold card may not use a raw id or an internal anchor marker
 * as its title.
 *
 * Measured on the base generation (`9dea5caf…`), phone 390x844 and desktop 1280x900, zh-CN and en,
 * against this lane's own sqlite:
 *   - `wallet-commitment` printed `policy.semantic_key` -- `v12:<journey uuid>:arrival-anchor` -- and,
 *     when that was absent, `"承诺 " + id.slice(0, 8)`: a truncated primary key standing in for a name.
 *   - `wallet-reservation` printed `confirmation_ref` as its title, and `bookingTypeLabel(item.type)`
 *     read a key the wallet payload never carries, so the type word was an empty string.
 *   - `inbox-pending-row` preferred `extracted.confirmation_ref` over the `extracted.title` the server
 *     had actually extracted.
 *   - `COMMITMENT_STATUS` knew no `CANCELLED`, so a retired anchor painted the labelled-raw channel
 *     (`这台服务器写的：CANCELLED`).
 *
 * Two nodes, because the order asks for two different proofs:
 *   N1 the names on screen are the ones the server wrote, and no machine token reaches the painted text;
 *   N2 with no name in the payload the card states that fact and grows nothing else -- an identifier,
 *      a truncation of one, or a label invented from the locale.
 *
 * The mutant that judges N1 is applied to product source, not to this file: masking the place-name
 * read has to make N1 fail, or N1 only proves that a string was copied.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-82f55d8a-ord0517-ticket-titles.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "dev";

const clean = (value: string | null | undefined) =>
  String(value ?? "").replace(/\s+/g, " ").trim();

/* Machine vocabulary, in the two shapes this product emits it: an underscore-joined enum, and a
   single all-caps word taken from the server's own value set. A spaced all-caps English phrase from
   the dictionary (`STAY EVIDENCE`) is not in either, and is reported separately below. */
const SCREAMING = /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g;
const SERVER_ENUM_WORDS =
  /\b(?:RAIL|FLIGHT|HOTEL|COACH|FERRY|TICKET|RESERVATION|BOOKING|ORDER|PAYMENT|APPOINTMENT|UNKNOWN_REALTIME|ON_TIME|DELAYED|CANCELLED|ACTIVE|CONFIRMED|CANDIDATE|PLANNED|STRUCTURED_JSON|DETERMINISTIC|NEEDS_REVIEW|READY_FOR_CONFIRMATION|ARRIVAL|DEPARTURE|MID_TRIP|AUTO_APPLY|APPLY_WITH_NOTICE|REQUIRES_CONFIRMATION|APPLIED|MOCK_BOOKING)\b/g;
const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g;
const ANCHOR_KEY = /\bv12:[^\s]+/g;
const TRUNCATED_ID = /(?:承诺|Commitment)\s+[0-9a-f]{6,}\b/g;

async function openBookings(page: Page, journeyId: string) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/bookings", { waitUntil: "load" });
  await expect(
    page.getByTestId("v13-2-memory"),
    node("boot", "the ticket folder never painted its own panels"),
  ).toBeVisible({ timeout: 30_000 });
}

/** A journey of this node's own, and real reservations behind it: the read path, not a fixture. */
async function seedOwnTickets(page: Page, stamp: string) {
  const traveler = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0517 lane", locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  const journey = await (
    await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: traveler.id,
        title: "天津周末",
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
  const journeyId = String(journey.id);

  const imports: Array<{ draft: string; ref: string; title: string | null }> = [];
  const cases = [
    {
      reservation_type: "RAIL",
      title: "天津西 → 北京南 高铁",
      confirmation_ref: `ORD0517NAMED${stamp}`,
      origin: { name: "天津西站" },
      destination: { name: "北京南站" },
      scheduled_departure_at: "2026-10-09T18:40:00+08:00",
      scheduled_arrival_at: "2026-10-09T19:32:00+08:00",
      amount_minor: 5480,
      currency: "CNY",
    },
    {
      reservation_type: "FLIGHT",
      confirmation_ref: `ORD0517PLACE${stamp}`,
      origin: { location: "滨海国际机场" },
      destination: { location: "首都机场" },
      scheduled_departure_at: "2026-10-10T09:10:00+08:00",
      scheduled_arrival_at: "2026-10-10T10:35:00+08:00",
    },
    {
      reservation_type: "HOTEL",
      title: "海河悦榕庄 湖景房",
      confirmation_ref: `ORD0517PENDIN${stamp}`,
    },
  ];
  for (const reservation of cases) {
    const created = await (
      await page.request.post(`/api/v1/journeys/${journeyId}/inbox/import`, {
        data: { artifact_type: "STRUCTURED_JSON", payload: { reservation } },
      })
    ).json();
    const draft = created.draft ?? created;
    imports.push({
      draft: String(draft.id),
      ref: String(reservation.confirmation_ref),
      title: reservation.title ?? null,
    });
    /* A draft the server still holds open cannot be confirmed; that is the queue's own state. */
    if ((draft.unresolved_fields ?? []).length === 0) {
      await page.request.post(
        `/api/v1/journeys/${journeyId}/inbox/${draft.id}/confirm`,
        { data: {} },
      );
    }
  }
  return { journeyId, imports };
}

async function painted(page: Page) {
  const scan = await scanScreenText(page);
  const text = scan.visual;
  return {
    scan,
    text,
    screaming: [...new Set(text.match(SCREAMING) ?? [])],
    enums: [...new Set(text.match(SERVER_ENUM_WORDS) ?? [])],
    uuids: [...new Set(text.match(UUID) ?? [])],
    anchorKeys: [...new Set(text.match(ANCHOR_KEY) ?? [])],
    truncatedIds: [...new Set(text.match(TRUNCATED_ID) ?? [])],
  };
}

async function cardTitles(page: Page, testId: string, expected: number, nodeId: string) {
  /* The shell's wallet read lands after the panels that mark the page as booted, so the count is
     waited for rather than sampled -- sampling it measured 0 cards on a page that had two. */
  await expect(
    page.getByTestId(testId),
    node(nodeId, `${testId} never reached ${expected} card(s) on screen, so nothing was measured`),
  ).toHaveCount(expected, { timeout: 30_000 });
  const cards = page.getByTestId(testId);
  const count = await cards.count();
  const titles: string[] = [];
  for (let index = 0; index < count; index += 1) {
    const card = cards.nth(index);
    await card.scrollIntoViewIfNeeded({ timeout: 5_000 }).catch(() => null);
    /* The heading is read as painted text inside this card -- not a `data-*` mirror of it. */
    titles.push(clean(await card.locator("h2").first().innerText()));
  }
  return { count, titles };
}

test("ord0517-1-card-titles-are-the-names-the-server-wrote — 票夹卡片的标题必须是服务器给的名字", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0517-1";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const { journeyId, imports } = await seedOwnTickets(page, stamp);
  await openBookings(page, journeyId);

  const reservations = await cardTitles(page, "wallet-reservation", 2, NODE);
  expect(
    reservations.titles,
    node(NODE, `the two reservation cards read ${JSON.stringify(reservations.titles)}`),
  ).toEqual(["天津西站 → 北京南站", "滨海国际机场 → 首都机场"]);

  const commitments = await cardTitles(page, "wallet-commitment", 2, NODE);
  for (const title of commitments.titles) {
    expect(
      title,
      node(NODE, `a commitment with no name field in the payload was given the title "${title}"`),
    ).toBe("服务器没有给这项承诺的名字");
  }

  /* The reference is still reachable -- behind its own label, where ORD-0146's queue gate looks. */
  for (const item of imports.slice(0, 2)) {
    const card = page.getByTestId("wallet-reservation").filter({ hasText: item.ref });
    await expect(
      card,
      node(NODE, `the card for ${item.ref} no longer carries the reference the server confirmed`),
    ).toBeVisible();
    const refLine = card.locator('[data-testid="wallet-reservation-ref"]');
    await expect(
      refLine,
      node(NODE, `${item.ref} is on the card, but not as a labelled reference`),
    ).toContainText(`订单号 ${item.ref}`);
  }

  /* The queue row: the title the server extracted, with the reference under it. */
  const pendingRow = page.getByTestId("inbox-pending-row").filter({ hasText: imports[2].ref });
  await expect(
    pendingRow,
    node(NODE, "the un-reviewed draft left no queue row, so the row's title was never measured"),
  ).toBeVisible();
  expect(
    clean(await pendingRow.locator('[data-testid="inbox-pending-title"]').innerText()),
    node(NODE, `the queue row titled itself with something other than ${imports[2].title}`),
  ).toBe("海河悦榕庄 湖景房");

  /* Denominator and absence, in one read of the painted layer. */
  const view = await painted(page);
  expect(
    view.scan.visualLeaves,
    node(NODE, "the painted scan found no text at all, so every absence below is vacuous"),
  ).toBeGreaterThan(40);
  for (const [fact, found] of [
    ["SCREAMING_SNAKE enum", view.screaming],
    ["server enum word", view.enums],
    ["uuid", view.uuids],
    ["anchor key", view.anchorKeys],
    ["truncated id used as a name", view.truncatedIds],
  ] as const) {
    expect(found, node(NODE, `a ${fact} reached the painted text: ${JSON.stringify(found)}`)).toEqual([]);
  }

  const technical = page.getByTestId("wallet-commitment-technical");
  const technicalCount = await technical.count();
  expect(
    technicalCount,
    node(NODE, "the raw machine identity has nowhere on this screen to be read"),
  ).toBeGreaterThan(0);
  expect(
    await technical.first().locator("code").count(),
    node(NODE, "the technical fold opened on no value at all"),
  ).toBeGreaterThan(0);

  /* Desktop is the same renderer at another width, and the fold is judged where it is drawn. */
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.waitForTimeout(800);
  const wide = await cardTitles(page, "wallet-reservation", 2, NODE);
  expect(wide.titles, node(NODE, "the desktop layout titles its cards differently")).toEqual(
    reservations.titles,
  );

  writeFileSync(
    evidencePath(`ord0517/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0517",
        node: NODE,
        journeyId,
        run: RUN,
        reservationTitles: reservations.titles,
        commitmentTitles: commitments.titles,
        queueTitle: "海河悦榕庄 湖景房",
        technicalFolds: technicalCount,
        paintedLeaves: view.scan.visualLeaves,
        machineTokensOnScreen: {
          screaming: view.screaming,
          enums: view.enums,
          uuids: view.uuids,
          anchorKeys: view.anchorKeys,
          truncatedIds: view.truncatedIds,
        },
        desktopTitles: wide.titles,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});

test("ord0517-2-a-ticket-with-no-name-says-nameless-and-invents-nothing — 没有名字时不许长出假标题", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0517-2";
  const { journeyId } = await seedOwnTickets(page, String(Math.floor(Math.random() * 1e6)).padStart(6, "0"));

  /* The nameless projection, through the component's own read: a reservation whose place objects
     carry only coordinates, and a commitment whose policy carries only machine fields. Both ids are
     long enough that the base generation's `slice(0, 8)` would have painted their heads. */
  await page.route("**/api/v1/journeys/*/wallet", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        journey_id: journeyId,
        offline_ready: false,
        reservations: [
          {
            id: "33333333-4444-4aaa-8bbb-cccccccccccc",
            reservation_type: "CRUISE_FERRY",
            confirmation_ref: null,
            origin: { lat: 39.1 },
            destination: { lat: 39.9 },
            scheduled_departure_at: "2026-10-10T08:00:00+08:00",
            scheduled_arrival_at: "2026-10-10T09:00:00+08:00",
            realtime_status: "UNKNOWN_REALTIME",
            commitment_id: null,
            price: { amount_minor: null, currency: null, status: "UNKNOWN" },
          },
        ],
        commitments: [
          {
            id: "55555555-6666-4aaa-8bbb-dddddddddddd",
            kind: "BOOKING",
            level: "C4_Anchored",
            status: "ACTIVE",
            external_ref: "mock-booking:stub:confirmed",
            monetary_value: { amount_minor: null, currency: null },
            monetary_value_status: "UNSTATED",
            policy: {
              semantic_key: "v12:33333333-4444-4aaa-8bbb-cccccccccccc:arrival-anchor",
              anchor_role: "ARRIVAL",
              user_locked: true,
              fixed_schedule: true,
              scheduled_start_at: "2026-10-10T08:00:00+08:00",
              scheduled_end_at: "2026-10-10T09:00:00+08:00",
            },
          },
          /* No anchor key at all: on the base generation this is the row that fell through to
             `"承诺 " + id.slice(0, 8)`, so its head (`77777777`) is what the absence checks below
             are really watching for. */
          {
            id: "77777777-8888-4aaa-8bbb-eeeeeeeeeeee",
            kind: "BOOKING",
            level: "C3_Guaranteed",
            status: "CONFIRMED",
            external_ref: "provider-ref-88",
            monetary_value: { amount_minor: 8800, currency: "CNY" },
            monetary_value_status: "KNOWN",
            policy: { user_locked: false, fixed_schedule: false },
          },
        ],
      }),
    }),
  );
  await openBookings(page, journeyId);

  const reservations = await cardTitles(page, "wallet-reservation", 1, NODE);
  const commitments = await cardTitles(page, "wallet-commitment", 2, NODE);
  /* One assertion over all three titles, because the point of this arm is what every card of a
     nameless payload paints -- separate expects would stop at the first one and hide the truncated
     identifier this order is named for. */
  expect(
    { reservation: reservations.titles[0], commitments: commitments.titles },
    node(NODE, "a payload with no name must name the gap on every card"),
  ).toEqual({
    reservation: "服务器没有给这单预订的名字",
    commitments: ["服务器没有给这项承诺的名字", "服务器没有给这项承诺的名字"],
  });

  const view = await painted(page);
  expect(
    view.scan.visualLeaves,
    node(NODE, "nothing was painted, so the absences below would prove nothing"),
  ).toBeGreaterThan(20);
  for (const [fact, found] of [
    ["uuid", view.uuids],
    ["anchor key", view.anchorKeys],
    ["truncated id used as a name", view.truncatedIds],
    ["SCREAMING_SNAKE enum", view.screaming],
    ["server enum word", view.enums],
  ] as const) {
    expect(found, node(NODE, `with no name to show the card reached for a ${fact}: ${JSON.stringify(found)}`)).toEqual([]);
  }
  expect(
    ["33333333", "55555555", "77777777"].some((head) => view.text.includes(head)),
    node(NODE, "the head of an identifier stood in for a name"),
  ).toBe(false);

  /* The machine identity is not gone, it is folded: opening the fold has to show it. */
  const fold = page.getByTestId("wallet-commitment-technical").first();
  await expect(
    fold,
    node(NODE, "the raw values this card stopped printing have no fold"),
  ).toBeVisible();
  await fold.locator("summary").click();
  const opened = clean(await fold.innerText());
  expect(
    opened.includes("55555555-6666-4aaa-8bbb-dddddddddddd"),
    node(NODE, `the technical fold reads "${opened.slice(0, 160)}" and does not carry the id`),
  ).toBe(true);
  expect(
    opened.includes("v12:") && opened.includes("arrival-anchor"),
    node(NODE, "the anchor key the title used to carry was dropped rather than folded"),
  ).toBe(true);

  writeFileSync(
    evidencePath(`ord0517/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0517",
        node: NODE,
        journeyId,
        run: RUN,
        reservationTitle: reservations.titles[0],
        commitmentTitles: commitments.titles,
        paintedLeaves: view.scan.visualLeaves,
        machineTokensOnScreen: view,
        foldedTechnicalText: opened.slice(0, 300),
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});
