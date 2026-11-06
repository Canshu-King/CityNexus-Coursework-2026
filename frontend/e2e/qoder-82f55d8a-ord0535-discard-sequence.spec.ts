/**
 * ORD-0535 (lane 82f55d8a) -- a trip that was dropped, brought back and dropped again is still ONE
 * row in the bin, and that row has to say *which* discard the traveller is looking at.
 *
 * Measured on the base generation (`7862c6de5565c02c20ec2dccaa9da18960603e06`), on this lane's own
 * sqlite, phone 390x844 and desktop 1280x900, zh-CN and en:
 *   - `GET /api/v1/recycle-bin/journeys` answers ONE row per trip carrying only the LATEST
 *     `deleted_at` (measured: rows_for_this_journey = 1 after the second discard), so the row's own
 *     sentence 「丢弃于 …」 is the last moment and nothing else;
 *   - the same trip's `GET /api/v1/journeys/{id}/history` answers TWO `journey.deleted` events with a
 *     `journey.undeleted` between them, and the discard events' `facts` were `{}` -- the door wrote
 *     the moment into `journeys.deleted_at` and never into the log, so even the log could not name
 *     the earlier instant;
 *   - the row rendered no ordinal and offered no control: measured `[role=radiogroup], select,
 *     input[type=radio]` count = 0 on every row, and the painted text of all four screens said
 *     nothing about which discard it was.
 *
 * Three nodes, because the order asks for two counter-tests on top of the real flow, and each cell
 * needs its own lever:
 *   N1 the acceptance walk: 丢弃 -> 找回 -> 再丢弃, and the row then names which discard it is while
 *      the bin still answers one row whose instant is this one's;
 *   N2 the number on screen follows the log it was read from (one event -> "first time", zero
 *      events -> the named gap, never "第 0 次" and never silence);
 *   N3 the earlier instant is the moment the column held, not the audit line's own timestamp -- with
 *      the pre-change rows as the control, which must fall to the recorded-time sentence.
 *
 * The mutants that judge these live in product source, not in this file: see the block for the two
 * single-variable edits (mask the discard count, and re-point the earlier moment at `timestamp`).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-82f55d8a-ord0535-discard-sequence.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "dev";
const clean = (value: string | null | undefined) => String(value ?? "").replace(/\s+/g, " ").trim();

type BinRow = { id: string; title: string; deleted_at: string | null; bin_state?: string };
type HistoryEvent = { event_type: string; timestamp: string | null; facts?: Record<string, unknown> };

async function makeJourney(page: Page, travelerId: string, title: string): Promise<string> {
  const body = await (
    await page.request.post("/api/v1/journeys", {
      data: {
        traveler_id: travelerId,
        title,
        city_code: "CN-TIANJIN",
        timezone: "Asia/Shanghai",
        start_at: "2026-10-09T18:00:00+08:00",
        end_at: "2026-10-11T15:00:00+08:00",
        budget: { amount_minor: 120000, currency: "CNY" },
        stay_budget: { amount_minor: 70000, currency: "CNY" },
        party_size: 2,
        occupancy: { adults: 2, rooms: 1 },
        arrival_anchor: { at: "2026-10-09T18:00:00+08:00", "location": "Tianjin West Station" },
        departure_anchor: { at: "2026-10-11T15:00:00+08:00", "location": "Tianjin Binhai Airport" },
      },
    })
  ).json();
  return String(body.id);
}

/** The bin as the server holds it, read straight off the anonymous door this screen uses. */
async function readBin(page: Page): Promise<BinRow[]> {
  return (await (await page.request.get("/api/v1/recycle-bin/journeys")).json()) as BinRow[];
}

async function readHistory(page: Page, journeyId: string): Promise<HistoryEvent[]> {
  const rows = (await (await page.request.get(`/api/v1/journeys/${journeyId}/history`)).json()) as Array<{
    event_type: string;
    timestamp: string | null;
    facts?: Record<string, unknown>;
  }>;
  return rows.filter(
    (row) => row.event_type === "journey.deleted" || row.event_type === "journey.undeleted",
  );
}

async function openTrash(page: Page, sessionJourneyId: string) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, sessionJourneyId);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/trash", { waitUntil: "load", timeout: 30_000 });
}

function rowFor(page: Page, journeyId: string) {
  /* The row is addressed by the journey the server put in the bin, so no assertion here can be
     satisfied by whichever row happened to render first. */
  return page.locator(`[data-testid="trash-row"][data-cnx-journey="${journeyId}"]`);
}

async function sequenceLine(page: Page, journeyId: string): Promise<string> {
  const line = rowFor(page, journeyId).locator('[data-testid="trash-row-sequence"]');
  await expect(
    line,
    node("row", `journey ${journeyId} has no sequence line, so the row says nothing about which discard it is`),
  ).toBeVisible({ timeout: 20_000 });
  /* The row answers in two steps: the bin first, then that trip's own log. Sampling it during the
     second step reads "reading the log…", which is a real state but not the one under test -- so the
     line is waited *out of* it rather than sampled. */
  await expect
    .poll(async () => clean(await line.innerText()), {
      timeout: 20_000,
      message: node("row", `the sequence line for ${journeyId} never settled: it stayed on the in-flight sentence`),
    })
    .not.toMatch(/正在读|reading this trip/);
  return clean(await line.innerText());
}

test("ord0535-1-the-row-says-which-discard-this-is — 找回之后再丢弃，那一行要说清是第几次", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535-1";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0535 lane " + stamp, locale: "zh-CN", "timezone": "Asia/Shanghai" },
    })
  ).json();

  const session = await makeJourney(page, traveler.id, `在场的那趟 ${stamp}`);
  const once = await makeJourney(page, traveler.id, `丢过一次 ${stamp}`);
  const twice = await makeJourney(page, traveler.id, `找回后又丢 ${stamp}`);

  await page.request.delete(`/api/v1/journeys/${once}`);
  await page.request.delete(`/api/v1/journeys/${twice}`);
  await page.request.post(`/api/v1/journeys/${twice}/undelete`, { data: {} });
  const secondDiscard = await page.request.delete(`/api/v1/journeys/${twice}`);
  expect(
    secondDiscard.status(),
    node(NODE, "the second 丢弃 did not answer 204, so the flow this order names never happened"),
  ).toBe(204);

  /* The server's own pair, before the screen is read: one row for that trip, and its instant is the
     second discard's -- "屏上取到的是本次那枚" is judged against this, not against a guess. */
  const bin = await readBin(page);
  const twiceRows = bin.filter((row) => row.id === twice);
  expect(
    twiceRows.length,
    node(NODE, `the bin answers ${twiceRows.length} rows for the re-dropped trip; the row the screen draws is the one under test`),
  ).toBe(1);
  const twiceHistory = await readHistory(page, twice);
  const discards = twiceHistory.filter((event) => event.event_type === "journey.deleted");
  expect(
    discards.length,
    node(NODE, `the trip's own log holds ${discards.length} journey.deleted events, not 2`),
  ).toBe(2);
  const storedOfLast = (discards[1].facts ?? {})["deleted_at"] ?? null;
  expect(
    bin[0] && twiceRows[0].deleted_at,
    node(NODE, "the bin row carries no deleted_at, so there is no instant to compare the log against"),
  ).toBeTruthy();
  expect(
    new Date(String(twiceRows[0].deleted_at)).getTime(),
    node(NODE, `the one row's instant (${twiceRows[0].deleted_at}) is not the second discard's (${storedOfLast})`),
  ).toBe(new Date(String(storedOfLast)).getTime());

  await openTrash(page, session);
  await expect(
    page.getByTestId("journey-trash-panel"),
    node(NODE, "the recycle-bin panel never rendered"),
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    rowFor(page, twice),
    node(NODE, "the re-dropped trip never reached a row of its own"),
  ).toBeVisible({ timeout: 20_000 });

  const twiceLine = await sequenceLine(page, twice);
  const onceLine = await sequenceLine(page, once);
  expect(
    twiceLine,
    node(NODE, `the re-dropped row reads "${twiceLine}" -- it does not name which discard this is`),
  ).toContain("第 2 次被丢弃");
  expect(
    twiceLine,
    node(NODE, `the re-dropped row reads "${twiceLine}" -- it does not name the earlier discard`),
  ).toContain("上一次丢弃于");
  expect(
    onceLine,
    node(NODE, `a trip dropped once reads "${onceLine}"`),
  ).toContain("第一次被丢弃");
  expect(
    onceLine.includes("上一次"),
    node(NODE, `a trip dropped once still claims an earlier discard: "${onceLine}"`),
  ).toBe(false);

  /* Reachability, the way a thumb has to reach it: scrolled into the viewport, centre point answers
     with this row's own sequence line. */
  const line = rowFor(page, twice).locator('[data-testid="trash-row-sequence"]');
  await line.scrollIntoViewIfNeeded();
  const box = await line.boundingBox();
  expect(box, node(NODE, "the sequence line has no box, so it is not painted")).not.toBeNull();
  const owner = await page.evaluate(([x, y]) => {
    const el = document.elementFromPoint(x, y);
    const owned = el && el.closest('[data-testid="trash-row-sequence"]');
    return owned ? "own sequence line" : el ? (el.innerText || el.tagName).slice(0, 40) : "nothing";
  }, [box!.x + box!.width / 2, box!.y + box!.height / 2]);
  expect(owner, node(NODE, `the centre point of the sequence line answered with: ${owner}`)).toBe("own sequence line");

  const scan = await scanScreenText(page);
  expect(
    scan.visualLeaves,
    node(NODE, "nothing was painted, so the sentences above would be reading an empty screen"),
  ).toBeGreaterThan(20);

  writeFileSync(
    evidencePath(`ord0535/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535",
        node: NODE,
        run: RUN,
        stamp,
        journeys: { session, once, twice },
        bin_rows_for_this_trip: twiceRows.length,
        bin_deleted_at: twiceRows[0].deleted_at,
        log_discard_events: discards.length,
        last_discard_stored_at: storedOfLast,
        row_text_twice: clean(await rowFor(page, twice).innerText()),
        row_text_once: clean(await rowFor(page, once).innerText()),
        sequence_line_twice: twiceLine,
        sequence_line_once: onceLine,
        reach: owner,
        paintedLeaves: scan.visualLeaves,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});

test("ord0535-2-the-number-on-screen-follows-the-log — 那枚序号跟着流水走，不是屏上算出来的", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535-2";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0535 arm2 " + stamp, locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  const session = await makeJourney(page, traveler.id, `在场 ${stamp}`);
  const target = await makeJourney(page, traveler.id, `流水被改 ${stamp}`);
  await page.request.delete(`/api/v1/journeys/${target}`);
  const realLog = await readHistory(page, target);

  /* One journey, one log held under the arm's thumb: the row must say what THIS log answered. */
  let answered = 0;
  let events: HistoryEvent[] = realLog;
  await page.route(`**/api/v1/journeys/${target}/history`, (route) => {
    answered += 1;
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        events.map((event) => ({
          id: "arm",
          event_type: event.event_type,
          timestamp: event.timestamp,
          facts: event.facts ?? {},
        })),
      ),
    });
  });

  await openTrash(page, session);
  await expect(rowFor(page, target), node(NODE, "the binned trip drew no row")).toBeVisible({ timeout: 20_000 });
  const baseline = await sequenceLine(page, target);
  expect(
    answered,
    node(NODE, "the row never asked the log door, so any number below would be the screen's own invention"),
  ).toBeGreaterThan(0);

  /* One discard event: the row has to fall back to "first time", not repeat the count it remembered. */
  events = [{ event_type: "journey.deleted", timestamp: "2026-10-05T08:00:00+08:00", facts: { deleted_at: "2026-10-05T08:00:00+08:00" } }];
  await page.reload({ waitUntil: "load" });
  await expect(rowFor(page, target)).toBeVisible({ timeout: 20_000 });
  const oneEvent = await sequenceLine(page, target);
  expect(oneEvent, node(NODE, `a log with one discard answered "${oneEvent}"`)).toContain("第一次被丢弃");

  /* No discard event at all: the named gap, never "第 0 次", never an empty span. */
  events = [];
  await page.reload({ waitUntil: "load" });
  await expect(rowFor(page, target)).toBeVisible({ timeout: 20_000 });
  const noEvent = await sequenceLine(page, target);
  expect(
    noEvent,
    node(NODE, `a log with no discard answered "${noEvent}"`),
  ).toContain("一笔丢弃都没有");
  expect(
    /第\s*0\s*次/.test(noEvent),
    node(NODE, `the row counted to zero instead of naming the gap: "${noEvent}"`),
  ).toBe(false);

  /* A log the screen cannot read says it cannot read it. */
  await page.unroute(`**/api/v1/journeys/${target}/history`);
  await page.route(`**/api/v1/journeys/${target}/history`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({}) }),
  );
  await page.reload({ waitUntil: "load" });
  await expect(rowFor(page, target)).toBeVisible({ timeout: 20_000 });
  const refused = await sequenceLine(page, target);
  expect(
    refused,
    node(NODE, `an unreadable log answered "${refused}"`),
  ).toContain("说不清这是第几次");

  writeFileSync(
    evidencePath(`ord0535/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535",
        node: NODE,
        run: RUN,
        journey: target,
        real_log_events: realLog.length,
        baseline,
        log_reads_answered: answered,
        with_one_discard: oneEvent,
        with_no_discard: noEvent,
        with_refused_log: refused,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});

test("ord0535-3-the-earlier-instant-is-the-column-not-the-log-line — 上一次那枚取的是列里存的时刻", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0535-3";
  const stamp = String(Math.floor(Math.random() * 1e6)).padStart(6, "0");
  const traveler = await (
    await page.request.post("/api/v1/travelers", {
      data: { display_name: "ORD-0535 arm3 " + stamp, locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  const session = await makeJourney(page, traveler.id, `在场 ${stamp}`);
  const target = await makeJourney(page, traveler.id, `两枚时刻 ${stamp}`);
  await page.request.delete(`/api/v1/journeys/${target}`);

  /* Two clocks, deliberately days apart, so a row that picks the wrong one can be told from the
     painted text alone: `stored` is what journeys.deleted_at held, `logged` is when the log line
     itself was written. They are separate datetime.now() reads and are never interchangeable. */
  const stored = "2026-10-05T08:00:00+08:00";
  const logged = "2026-10-06T20:00:00+08:00";
  const lastStored = "2026-10-08T09:30:00+08:00";
  const lastLogged = "2026-10-08T09:31:00+08:00";
  let events: HistoryEvent[] = [
    { event_type: "journey.deleted", timestamp: logged, facts: { deleted_at: stored } },
    { event_type: "journey.undeleted", timestamp: "2026-10-06T21:00:00+08:00", facts: { deleted_at: null } },
    { event_type: "journey.deleted", timestamp: lastLogged, facts: { deleted_at: lastStored } },
  ];
  await page.route(`**/api/v1/journeys/${target}/history`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        events.map((event, index) => ({
          id: `arm-${String(index)}`,
          event_type: event.event_type,
          timestamp: event.timestamp,
          facts: event.facts ?? {},
        })),
      ),
    }),
  );

  await openTrash(page, session);
  await expect(rowFor(page, target)).toBeVisible({ timeout: 20_000 });
  const withStored = await sequenceLine(page, target);
  expect(withStored, node(NODE, `the row does not name a second discard: "${withStored}"`)).toContain("第 2 次被丢弃");
  expect(
    withStored.includes("10月5日"),
    node(NODE, `the earlier discard's stored instant (10月5日) is not on the row: "${withStored}"`),
  ).toBe(true);
  expect(
    withStored.includes("10月6日"),
    node(NODE, `the row printed the log line's own timestamp instead of the stored instant: "${withStored}"`),
  ).toBe(false);

  /* The control: rows written before this order carry no stored instant, and the row must then say
     that it is quoting the log's line -- the two branches are field-driven in both directions. */
  events = [
    { event_type: "journey.deleted", timestamp: logged, facts: {} },
    { event_type: "journey.undeleted", timestamp: "2026-10-06T21:00:00+08:00", facts: { deleted_at: null } },
    { event_type: "journey.deleted", timestamp: lastLogged, facts: {} },
  ];
  await page.reload({ waitUntil: "load" });
  await expect(rowFor(page, target)).toBeVisible({ timeout: 20_000 });
  const withoutStored = await sequenceLine(page, target);
  expect(
    withoutStored,
    node(NODE, `a pre-change log row answered "${withoutStored}"`),
  ).toContain("没有丢弃时间");
  expect(
    withoutStored.includes("10月6日"),
    node(NODE, `the fallback did not quote the log line's own moment: "${withoutStored}"`),
  ).toBe(true);

  writeFileSync(
    evidencePath(`ord0535/${NODE}-reading-${RUN}.json`),
    JSON.stringify(
      {
        generatedBy: "82f55d8a",
        order: "ORD-0535",
        node: NODE,
        run: RUN,
        journey: target,
        stored_instant: stored,
        logged_instant: logged,
        row_with_stored: withStored,
        row_without_stored: withoutStored,
      },
      null,
      1,
    ) + "\n",
    "utf8",
  );
});
