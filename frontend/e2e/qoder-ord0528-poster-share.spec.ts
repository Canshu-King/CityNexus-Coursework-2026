/**
 * ORD-0528 — the poster travels with sharing, judged on real artifacts.
 *
 *   S1  what the download actually sends: every request the page makes between pressing 成长图并保存
 *       and receiving the file. The claim "the download outlet has no connection to the share family"
 *       is read off this list, not off a grep.
 *   S2  分享这张图: the sentence on screen has to be the *server's* list door agreeing that a row
 *       exists. The arm reads the door again from a second, unrelated browser context and compares
 *       the id the traveller's screen printed with the rows the server names.
 *   S3  the two shapes of one grant: the same token opened bare is the trip (map, plan line), opened
 *       with the poster tail is the picture (day blocks, downloadable, no map). Neither may wear the
 *       other's face, and the poster one has to say so in words on the page.
 *   S4  the two counterexamples, both run for real:
 *       X1 no server confirmation -> the sheet must not read "已分享" (three shapes of "no": the write
 *          refused, the write answered with no token, the list door does not carry the row);
 *       X2 run against the bundle whose receiver ignores the address tail, the shape arm goes red --
 *          which is the same ruler, and it only says "distinguished" if it can say "confused".
 *   S5  screen reads, one pass at 390x844 and one at desktop, reading `innerText`.
 *
 * The ruler is generation-agnostic on purpose: point `CITYNEXUS_ORD0528_FRONT` at the base build and
 * the same file reports what the base generation does and does not have.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const FRONT = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0528_FRONT)}`;
const GEN = (process.env.CITYNEXUS_ORD0528_GEN ?? "").trim();
const EVIDENCE = (process.env.CITYNEXUS_ORD0528_EVIDENCE ?? "").trim();
const AUDIT = (process.env.CITYNEXUS_ORD0528_AUDIT ?? "").trim();
const PYTHON = (process.env.CITYNEXUS_ORD0528_PY ?? "").trim();
for (const [name, value] of [
  ["CITYNEXUS_ORD0528_FRONT", process.env.CITYNEXUS_ORD0528_FRONT],
  ["CITYNEXUS_ORD0528_GEN", GEN],
  ["CITYNEXUS_ORD0528_EVIDENCE", EVIDENCE],
  ["CITYNEXUS_ORD0528_AUDIT", AUDIT],
  ["CITYNEXUS_ORD0528_PY", PYTHON],
] as const) {
  if (!value) COLLECTION_PREREQ.push(`[ord0528] ${name} must be named by the runner`);
}
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

function write(name: string, body: unknown) {
  writeFileSync(`${EVIDENCE}/${name}.json`, JSON.stringify(body, null, 1), "utf8");
  console.log(`O528 ${GEN} ${name} ${JSON.stringify(body).slice(0, 900)}`);
}

function audit(path: string): any {
  const out = execFileSync(PYTHON, [AUDIT, path], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(out.trim().split("\n")[0]);
}

/** The traveller's own context: their stored journey, their locale, nothing else. */
async function travellerContext(browser: Browser, locale: "zh-CN" | "en" = "zh-CN") {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
  await context.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    [locale, JOURNEY_ID] as const,
  );
  return context;
}

async function openSheet(page: Page, locale: "zh-CN" | "en" = "zh-CN") {
  await page.goto(`${FRONT}/v8/trips`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(900);
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });
  return page.getByTestId("trip-poster");
}

/** A guest context shares nothing with the traveller's: no storage, no service-worker scope state,
 *  no in-memory grant list. If a claim survives that, it came over the wire. */
async function guestContext(browser: Browser, locale: "zh-CN" | "en" = "zh-CN") {
  return browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale });
}

const tokenOf = (link: string) => {
  const match = /\/v8\/shared\/([^/]+)(?:\/([^/]+))?$/.exec(link.trim());
  return match ? { token: match[1], form: match[2] ?? "" } : null;
};

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("ORD-0528 S1 the download sends nothing to the share family", async ({ browser }) => {
  const context = await travellerContext(browser);
  const page = await context.newPage();
  await openSheet(page);

  const calls: string[] = [];
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/api/")) calls.push(`${request.method()} ${url.replace(FRONT, "")}`);
  });
  const downloadPromise = page.waitForEvent("download", { timeout: 90_000 });
  await page.getByTestId("poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE}/S1_${GEN}_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  const buffer = readFileSync(saved);

  write("S1_download_request_inventory", {
    api_calls_during_export: calls,
    api_call_count: calls.length,
    share_family_calls: calls.filter((c) => /share|grant|poster/i.test(c)).length,
    suggested_filename: download.suggestedFilename(),
    bytes: buffer.length,
    ihdr: { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) },
    pixels: audit(saved),
  });
  expect(calls.length, "the export is drawn on this device and asks the server for nothing").toBe(0);
  expect(buffer.length).toBeGreaterThan(1000);
  await context.close();
});

test("ORD-0528 S2 the shared sentence is the server's list door, not a client boolean", async ({ browser }) => {
  const context = await travellerContext(browser);
  const page = await context.newPage();
  await openSheet(page);

  const before = await (await guestContext(browser)).request.get(
    `${FRONT}/api/v1/journeys/${JOURNEY_ID}/share-grants`,
  );
  const rowsBefore = await before.json();
  await page.getByTestId("poster-share").click();
  const result = page.getByTestId("poster-share-result");
  await expect(result).toBeVisible({ timeout: 30_000 });
  const sentence = (await result.innerText()).trim();
  const state = await result.getAttribute("data-share-state");
  const link = await page.getByTestId("poster-share-link").inputValue();

  /* Read the door back from a context that never touched the sheet. The screen's sentence has the
     grant id inside it, so this compares what a traveller can read with what the server names. */
  const guest = await guestContext(browser);
  const after = await guest.request.get(`${FRONT}/api/v1/journeys/${JOURNEY_ID}/share-grants`);
  const rowsAfter = (await after.json()) as any[];
  const printed = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(sentence);
  const parsed = tokenOf(link);

  write("S2_server_readback", {
    list_before_http_status: before.status(),
    rows_before: Array.isArray(rowsBefore) ? rowsBefore.length : null,
    list_after_http_status: after.status(),
    rows_after: rowsAfter.length,
    sentence_on_screen: sentence,
    data_share_state: state,
    grant_id_printed_on_screen: printed ? printed[1] : null,
    ids_the_server_names: rowsAfter.map((row) => row.id),
    printed_id_is_named_by_server: printed ? rowsAfter.some((row) => row.id === printed[1]) : false,
    printed_row_revoked_at: printed ? (rowsAfter.find((row) => row.id === printed[1])?.revoked_at ?? null) : null,
    link,
    token_chars: parsed ? parsed.token.length : 0,
    address_tail: parsed ? parsed.form : null,
  });

  expect(state, "the sheet only reaches CONFIRMED through the read-back").toBe("CONFIRMED");
  expect(
    rowsAfter.length,
    "the read-back carries exactly one more row than the door named before the press",
  ).toBe((Array.isArray(rowsBefore) ? rowsBefore.length : 0) + 1);
  expect(printed?.[1], "the sentence carries a grant id").toBeTruthy();
  expect(
    rowsAfter.some((row) => row.id === printed[1]),
    "and the server names that same row when asked by a stranger context",
  ).toBe(true);
  expect(parsed?.form, "the link asks for the poster shape").toBe("poster");
  expect(parsed?.token.length, "the token is the 32 characters the create door minted").toBe(32);
  /* The link is a real door, not a string that looks like one: presented, it answers 200. Asked from
     the same stranger context that read the list -- a context that never opened the sheet. */
  const presented = await guest.request.get(`${FRONT}/api/v1/shared/journeys/${parsed?.token}`);
  await guest.close();
  expect(presented.status(), "presenting the token the screen printed reads the trip").toBe(200);
  await context.close();
});

test("ORD-0528 S3 one grant, two shapes that do not look alike", async ({ browser }) => {
  const context = await travellerContext(browser);
  const page = await context.newPage();
  await openSheet(page);
  await page.getByTestId("poster-share").click();
  const link = await page.getByTestId("poster-share-link").inputValue();
  const parsed = tokenOf(link);
  expect(parsed).not.toBeNull();

  const guest = await guestContext(browser);
  const guestPage = await guest.newPage();
  await guestPage.goto(`${FRONT}/v8/shared/${parsed!.token}/poster`, { waitUntil: "load", timeout: 90_000 });
  await expect(guestPage.getByTestId("shared-poster-screen")).toBeVisible({ timeout: 30_000 });
  await guestPage.waitForTimeout(1_200);
  const posterText = await guestPage.evaluate(() => document.body.innerText);
  const poster: any = {
    url_tail: parsed!.form,
    poster_screen: await guestPage.getByTestId("shared-poster-screen").count(),
    trip_screen: await guestPage.getByTestId("shared-screen").count(),
    stop_rows: await guestPage.getByTestId("shared-poster-stop").count(),
    map_present: await guestPage.getByTestId("shared-map").count(),
    plan_line_present: await guestPage.getByTestId("shared-plan-line").count(),
    form_note: (await guestPage.getByTestId("shared-poster-form-note").innerText()).trim(),
    kicker: (await guestPage.getByTestId("shared-poster-kicker").innerText()).trim(),
    inner_text_chars: posterText.length,
  };
  /* The guest's page obeys the traveller's own size bound, so it may page. Walk whatever pages it
     offers and sum them: expecting every day on one screen would be asserting that the bound does
     not exist on this side. */
  poster.paged_note = await guestPage.getByTestId("shared-poster-paged-note").innerText({ timeout: 1_000 }).catch(() => null);
  poster.page_buttons = await guestPage.getByTestId(/^shared-poster-page-\d+$/).count();
  poster.day_blocks_pages = [];
  for (let index = 0; index < Math.max(1, poster.page_buttons); index += 1) {
    if (poster.page_buttons > 0) {
      await guestPage.getByTestId(`shared-poster-page-${index + 1}`).click();
      await guestPage.waitForTimeout(350);
    }
    poster.day_blocks_pages.push(await guestPage.getByTestId(/^shared-poster-day-\d+$/).count());
  }
  poster.day_blocks = poster.day_blocks_pages.reduce((total: number, n: number) => total + n, 0);
  poster.inner_text_chars = posterText.length;
  /* Back to page one before the picture, so the PNG measured below is comparable with the block count
     on that page rather than with whichever page the walk happened to end on. */
  if (poster.page_buttons > 0) {
    await guestPage.getByTestId("shared-poster-page-1").click();
    await guestPage.waitForTimeout(350);
  }
  poster.day_blocks_on_exported_page = await guestPage.getByTestId(/^shared-poster-day-\d+$/).count();
  /* The guest's picture has to be the traveller's picture: drawn here, and measured here. */
  const downloadPromise = guestPage.waitForEvent("download", { timeout: 90_000 });
  await guestPage.getByTestId("shared-poster-download").click();
  const download = await downloadPromise;
  const saved = `${EVIDENCE}/S3_${GEN}_guest_${download.suggestedFilename()}`;
  await download.saveAs(saved);
  const buffer = readFileSync(saved);
  poster.pixels = audit(saved);
  poster.ihdr = { bytes: buffer.length, width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
  await guest.close();

  const trip = await guestContext(browser);
  const tripPage = await trip.newPage();
  await tripPage.goto(`${FRONT}/v8/shared/${parsed!.token}`, { waitUntil: "load", timeout: 90_000 });
  await expect(tripPage.getByTestId("shared-screen")).toBeVisible({ timeout: 30_000 });
  await tripPage.waitForTimeout(1_200);
  const tripText = await tripPage.evaluate(() => document.body.innerText);
  const tripShape = {
    poster_screen: await tripPage.getByTestId("shared-poster-screen").count(),
    trip_screen: await tripPage.getByTestId("shared-screen").count(),
    map_present: await tripPage.getByTestId("shared-map").count(),
    plan_line_present: await tripPage.getByTestId("shared-plan-line").count(),
    inner_text_chars: tripText.length,
  };
  await trip.close();

  /* A mistyped tail must not dead-end a valid token: `POSTER` in either case opens the poster, and a
     word this product does not know falls back to the trip screen rather than a blank page. */
  const odd = await guestContext(browser);
  const oddPage = await odd.newPage();
  await oddPage.goto(`${FRONT}/v8/shared/${parsed!.token}/POSTER`, { waitUntil: "load", timeout: 90_000 });
  const upperCount = await oddPage.getByTestId("shared-poster-screen").count();
  const stranger = await odd.newPage();
  await stranger.goto(`${FRONT}/v8/shared/${parsed!.token}/nonsense`, { waitUntil: "load", timeout: 90_000 });
  const strangerTrip = await stranger.getByTestId("shared-screen").count();
  const strangerPoster = await stranger.getByTestId("shared-poster-screen").count();
  await odd.close();

  write("S3_two_shapes", {
    poster_shape: poster,
    trip_shape: tripShape,
    texts_identical: posterText === tripText,
    uppercase_tail_opens_poster: upperCount,
    unknown_tail_opens_trip: strangerTrip,
    unknown_tail_opens_poster: strangerPoster,
  });

  expect(poster.poster_screen, "the poster tail opens the poster").toBe(1);
  expect(poster.trip_screen, "and not the trip screen wearing its testid").toBe(0);
  expect(poster.map_present, "the picture shape shows no map").toBe(0);
  expect(poster.plan_line_present, "and no plan-source line").toBe(0);
  expect(poster.day_blocks, "one day block per day the grant let the guest read").toBeGreaterThanOrEqual(2);
  /* The picture's own rows have to describe the same number of blocks the page showed: each card
     contributes its top and bottom hairline and every break between two cards adds one drawn seam,
     so `3n-1` groups is the only honest reading of n blocks. This is the guest side of the link
     ORD-0515 pinned on the traveller's side. */
  expect(
    poster.pixels.hairline_groups,
    `the exported page showed ${poster.day_blocks_on_exported_page} day blocks`,
  ).toBe(3 * poster.day_blocks_on_exported_page - 1);
  expect(poster.form_note.length, "the page says in words which shape this is").toBeGreaterThan(10);
  expect(tripShape.poster_screen, "the bare token still opens the trip, not the picture").toBe(0);
  expect(tripShape.map_present, "and that screen keeps the map the poster has no room for").toBe(1);
  expect(posterText === tripText, "the two screens are not the same reading of the same token").toBe(false);
  expect(upperCount, "POSTER in either case is the same ask").toBe(1);
  expect(strangerTrip, "a tail this product does not know falls back to the trip screen").toBe(1);
  expect(strangerPoster, "and does not open a second shape by accident").toBe(0);
  await context.close();
});

test("ORD-0528 X1 with no server confirmation the sheet does not read 'shared'", async ({ browser }) => {
  /* Four shapes of "the server did not agree". Two of them refuse the write and answer the list door
     with an empty roster; one mints a grant the read-back does not carry; the fourth is the read leg
     itself refusing -- an exception escaping there left the button disabled and the sheet stuck on
     "Asking the server…" with no sentence at all. */
  const shapes = [
    /* ORD-0541 changed what these two shapes have to carry. The outlet now asks the list door before
       it writes, so by the time X1 runs, S2/S3 have left a standing LINK_READ row on this journey and
       a press that is allowed to reuse it never reaches the create door -- the screen would read
       "shared, reusing", and this arm's judgment ("no server confirmation -> no shared claim") would
       be measuring a shape that never happened. The assertions are the same ones that landed; what
       changed is that a "write refused" press needs a roster with nothing standing on it, which is
       exactly what the same door answers on a trip nobody has shared yet. */
    {
      name: "write_refused",
      create: () => ({ status: 403, body: { error: { reason_code: "NOPE" } } }),
      list: () => ({ status: 200, body: [] }),
    },
    {
      name: "no_token",
      create: () => ({ status: 201, body: { id: "grant-without-a-token", visibility: "LINK_READ" } }),
      list: () => ({ status: 200, body: [] }),
    },
    { name: "row_not_listed", create: "real", list: () => ({ status: 200, body: [] }) },
    { name: "read_refused", create: "real", list: () => ({ status: 500, body: { error: { reason_code: "INTERNAL" } } }) },
  ];
  const readings: any[] = [];
  for (const shape of shapes) {
    const context = await travellerContext(browser);
    const page = await context.newPage();
    await openSheet(page);
    await page.route(`**/api/v1/journeys/${JOURNEY_ID}/share-grants`, (route) => {
      if (route.request().method() === "POST" && shape.create !== "real") {
        const answer = (shape.create as () => { status: number; body: unknown })();
        return route.fulfill({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) });
      }
      if (route.request().method() === "GET" && shape.list) {
        const answer = shape.list();
        return route.fulfill({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) });
      }
      return route.continue();
    });
    await page.getByTestId("poster-share").click();
    const result = page.getByTestId("poster-share-result");
    await expect(result).toBeVisible({ timeout: 30_000 });
    const sentence = (await result.innerText()).trim();
    readings.push({
      shape: shape.name,
      data_share_state: await result.getAttribute("data-share-state"),
      sentence,
      /* The number the screen quotes has to be the number the door answered. A status probe that
         reads the wrong field prints "unreachable" about a server that did answer with a code, and
         that is a false reading on the traveller's page rather than in a log. */
      status_named_on_screen: /状态码 (\d{3})|status (\d{3})/.exec(sentence)?.[1] ?? null,
      claims_shared: /已分享|链接已生成|Shared\./.test(sentence),
      link_input_present: await page.getByTestId("poster-share-link").count(),
      key_echo: /poster\.[a-zA-Z]/.test(sentence),
    });
    await context.close();
  }
  write("X1_no_server_confirmation", { readings });
  for (const reading of readings) {
    expect(reading.data_share_state, `${reading.shape} is not a confirmation`).toBe("NOT_CONFIRMED");
    expect(reading.claims_shared, `${reading.shape} must not read as shared`).toBe(false);
    expect(reading.link_input_present, `${reading.shape} has no link to hand over`).toBe(0);
    expect(reading.key_echo, `${reading.shape} speaks in sentences, not key names`).toBe(false);
  }
  /* The number the screen quotes has to be the number the door answered -- and only where a door
     actually answered with one. The two stubbed-write shapes carry their status, and the two shapes
     whose answer arrived fine have no number to quote, so they must not invent one. */
  const named = { write_refused: "403", read_refused: "500", no_token: null, row_not_listed: null };
  for (const [shape, status] of Object.entries(named)) {
    const reading = readings.find((item) => item.shape === shape);
    expect(
      reading?.status_named_on_screen,
      `${shape} quotes ${status === null ? "no number at all" : `the door's own ${status}`}`,
    ).toBe(status);
  }
});

test("ORD-0528 S5 the same sentences, read in both languages and both shapes of screen", async ({ browser }) => {
  const readings: any[] = [];
  for (const locale of ["zh-CN", "en"] as const) {
    for (const viewport of [
      { name: "phone", width: 390, height: 844 },
      { name: "desktop", width: 1280, height: 900 },
    ] as const) {
      const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, hasTouch: viewport.name === "phone", locale });
      await context.addInitScript(
        ([language, journey]) => {
          localStorage.setItem("citynexus.locale", language);
          localStorage.setItem("citynexus.v8.journey_id", journey);
        },
        [locale, JOURNEY_ID] as const,
      );
      const page = await context.newPage();
      await openSheet(page, locale);
      const sheet = (await page.getByTestId("trip-poster").innerText()).trim();
      await page.getByTestId("poster-share").click();
      const result = page.getByTestId("poster-share-result");
      await expect(result).toBeVisible({ timeout: 30_000 });
      const link = await page.getByTestId("poster-share-link").inputValue();
      const parsed = tokenOf(link)!;

      const guest = await guestContext(browser, locale);
      await guest.addInitScript((language) => localStorage.setItem("citynexus.locale", language), locale);
      const guestPage = await guest.newPage();
      await guestPage.goto(`${FRONT}/v8/shared/${parsed.token}/poster`, { waitUntil: "load", timeout: 90_000 });
      await expect(guestPage.getByTestId("shared-poster-screen")).toBeVisible({ timeout: 30_000 });
      await guestPage.waitForTimeout(1_200);
      const guestText = await guestPage.evaluate(() => document.body.innerText);
      readings.push({
        locale,
        viewport: `${viewport.width}x${viewport.height}`,
        sheet_inner_text_chars: sheet.length,
        share_sentence: (await result.innerText()).trim(),
        guest_inner_text_chars: guestText.length,
        day_blocks: await guestPage.getByTestId(/^shared-poster-day-/).count(),
        /* `t(key)` answers with the key itself when a key is missing on this side, so a machine word
           on screen is measurable: count the ones that look like a key path. */
        key_echo_hits: (sheet.match(/\b(poster|sharedPoster|shared)\.[A-Za-z0-9_.]+/g) ?? []).length
          + (guestText.match(/\b(poster|sharedPoster|shared)\.[A-Za-z0-9_.]+/g) ?? []).length,
      });
      await guest.close();
      await context.close();
    }
  }
  write("S5_screen_reads", { readings });
  for (const reading of readings) {
    expect(reading.key_echo_hits, `${reading.locale} at ${reading.viewport} leaks no key name`).toBe(0);
    expect(reading.sheet_inner_text_chars).toBeGreaterThan(60);
    expect(reading.guest_inner_text_chars).toBeGreaterThan(60);
  }
});
