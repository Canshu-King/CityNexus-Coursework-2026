/**
 * ORD-0541 -- the share outlet is idempotent, judged on the requests it makes and the rows the server
 * keeps.
 *
 * The finding, in the words it reached the ledger in: pressing 分享这张图 again minted another grant.
 * Nothing on the traveller's screen said so, and both links worked, so the only evidence that the trip
 * had been shared twice was a roster that kept growing. The commander's ruling for what may be reused
 * is verbatim 「`visibility` 恰为 `LINK_READ` 才算可复用链接」, which is what A3 measures from the other
 * side: a standing row at another tier is not this outlet's link, even though it is a row on the same
 * journey.
 *
 *   A1  a press that writes, then a press that does not: the second one asks the create door for
 *       nothing, hands back the same address, and leaves the server's roster the size it was;
 *   A2  reuse is not "never write" -- withdraw the standing link through the server's own revoke door
 *       and the next press mints exactly one grant again;
 *   A3  a standing row whose tier is not `LINK_READ` is not reused (PUBLIC / PRIVATE / INVITEES_ONLY),
 *       and neither is a `LINK_READ` row whose answer carries no token or already carries a
 *       revocation;
 *   A4  when the list door refuses, nothing is written at all -- the press that cannot ask is not a
 *       press that may mint;
 *   A5  the two confirmations are different sentences, in both shipped languages and both shapes of
 *       screen, with no key name on either.
 *
 * Each arm withdraws the links an earlier arm left standing before it presses, and every press is made
 * on a sheet that has not pressed before. Both choices are about the reading, not the product: an
 * outlet that reuses what stands answers differently on an empty roster than on a full one, and a
 * second press inside the same sheet would have to guess which repaint it was looking at -- which is
 * exactly how the first cut of this file lost a reading it could not explain.
 *
 * One ruler, three bundles: the candidate is this order's build, the base generation writes before it
 * asks, and the mutant has the tier clause removed from the reuse predicate. What each generation
 * actually scored is in `review/ord0541/run/*` and in the delivery block -- an arm that is green on all
 * three is not measuring idempotency.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test, type Browser, type Page } from "@playwright/test";

const COLLECTION_PREREQ: string[] = [];

const JOURNEY_ID = "22222222-2222-4222-8222-222222222222";
const FRONT = `http://127.0.0.1:${Number(process.env.CITYNEXUS_ORD0541_FRONT)}`;
const GEN = (process.env.CITYNEXUS_ORD0541_GEN ?? "").trim();
const EVIDENCE = (process.env.CITYNEXUS_ORD0541_EVIDENCE ?? "").trim();
for (const [name, value] of [
  ["CITYNEXUS_ORD0541_FRONT", process.env.CITYNEXUS_ORD0541_FRONT],
  ["CITYNEXUS_ORD0541_GEN", GEN],
  ["CITYNEXUS_ORD0541_EVIDENCE", EVIDENCE],
] as const) {
  if (!value) COLLECTION_PREREQ.push(`[ord0541] ${name} must be named by the runner`);
}
if (EVIDENCE) mkdirSync(EVIDENCE, { recursive: true });

const DOOR = `/api/v1/journeys/${JOURNEY_ID}/share-grants`;
const GRANT_ID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
/** The two confirmation sentences, taken from the dictionary lines this order ships: 新建 names the
 *  write it did, 复用 names the write it did not. The arms match on the words a traveller reads, not on
 *  a `data-*` attribute, because the claim under test is the one on the screen. */
const WROTE = /确实写入|wrote a new grant/;
const REUSED = /没有再写入|wrote nothing/;

function write(name: string, body: unknown) {
  writeFileSync(`${EVIDENCE}/${name}.json`, JSON.stringify(body, null, 1), "utf8");
  console.log(`O541 ${GEN} ${name} ${JSON.stringify(body).slice(0, 900)}`);
}

/** Open the traveller's poster sheet, count this page's calls to the grant door, and keep whatever the
 *  page itself reports as an error. A press whose promise never settles is a different fact from a press
 *  whose promise rejected, and without this second channel the first one is only a timeout. */
async function openSheet(browser: Browser, locale = "zh-CN", viewport = { width: 390, height: 844 }) {
  const context = await browser.newContext({ viewport, hasTouch: viewport.width < 500, locale });
  await context.addInitScript(
    ([language, journey]) => {
      localStorage.setItem("citynexus.locale", language);
      localStorage.setItem("citynexus.v8.journey_id", journey);
    },
    [locale, JOURNEY_ID] as const,
  );
  const page = await context.newPage();
  const calls: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(DOOR)) calls.push(`${request.method()} ${request.url().replace(FRONT, "")}`);
  });
  page.on("pageerror", (error) => errors.push(String(error?.message ?? error)));
  await page.goto(`${FRONT}/v8/trips`, { waitUntil: "load", timeout: 90_000 });
  await expect(page.getByTestId("trip-poster-bar").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(700);
  await page.getByTestId("poster-open").click();
  await expect(page.getByTestId("trip-poster")).toBeVisible({ timeout: 20_000 });
  return {
    context,
    page,
    calls,
    errors,
    posts: () => calls.filter((call) => call.startsWith("POST")).length,
  };
}

/** Press 分享这张图 once, on a sheet that has not pressed before, and read back what it claimed. */
async function press(page: Page) {
  await page.getByTestId("poster-share").click();
  const result = page.getByTestId("poster-share-result");
  await expect(result).toBeVisible({ timeout: 30_000 });
  const sentence = (await result.innerText()).trim();
  const link = (
    await page.getByTestId("poster-share-link").inputValue({ timeout: 3_000 }).catch(() => "")
  ).trim();
  return {
    state: await result.getAttribute("data-share-state"),
    sentence,
    grant_id: GRANT_ID.exec(sentence)?.[0] ?? null,
    link,
    link_present: await page.getByTestId("poster-share-link").count(),
    key_echo: /\bposter\.[A-Za-z0-9_.]+/.test(sentence),
  };
}

/** The roster, read by this instrument rather than through the sheet's own sentence. `page.request` is
 *  an API context: `page.route` does not cover it, so a stubbed press cannot fake this read. */
async function roster(page: Page): Promise<any[]> {
  const answer = await page.request.get(`${FRONT}${DOOR}`);
  const body = await answer.json();
  return Array.isArray(body) ? body : [];
}

const tokenOf = (link: string) => /\/v8\/shared\/([^/]+)\/([^/]+)$/.exec(link.trim())?.[1] ?? null;

/** The rows this outlet may hand over, as the server itself describes them right now. */
const standingLinks = (rows: any[]) =>
  rows.filter((row) => row.revoked_at === null && row.visibility === "LINK_READ");

/** Withdraw every standing link this journey has, through the server's own revoke door -- the product's,
 *  not an instrument reaching into the store -- and read the answers back rather than assuming the rows
 *  went away. This is the arms' starting condition, not a product claim: an outlet that reuses what
 *  stands answers differently on an empty roster than on a full one, and an arm whose reading depends on
 *  which grants the last run happened to leave behind is not measuring this order. */
async function revokeAllStanding(page: Page) {
  const standing = standingLinks(await roster(page));
  const statuses: Record<string, number> = {};
  for (const row of standing) {
    statuses[row.id] = (await page.request.post(`${FRONT}${DOOR}/${row.id}/revoke`)).status();
  }
  return { revoked: statuses, standing_left: standingLinks(await roster(page)).length };
}

/** Fake only the press's own asking: the read-back after a real write reaches the real door, so what an
 *  arm confirms is always a row the server actually made. */
async function fakeFirstAsk(page: Page, rows: unknown[]) {
  let fakes_left = 1;
  await page.route(`**${DOOR}`, (route) => {
    if (route.request().method() === "GET" && fakes_left > 0) {
      fakes_left -= 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
    }
    return route.continue();
  });
}

/** Refuse every page-level call to the grant door. `page.request` is not covered by this, which is what
 *  lets an arm's own set-up read and revoke through the real door. */
async function refuseAllDoors(page: Page) {
  await page.route(`**${DOOR}`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: { reason_code: "INTERNAL" } }),
    }),
  );
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test("ORD-0541 A1 a press that writes, then a press that does not", async ({ browser }) => {
  const write_arm = await openSheet(browser);
  const reset = await revokeAllStanding(write_arm.page);
  const rows_before = await roster(write_arm.page);

  const first = await press(write_arm.page);
  const posts_on_first_press = write_arm.posts();
  const rows_after_first = await roster(write_arm.page);
  await write_arm.context.close();

  /* The reuse is asked on a sheet that has not pressed yet, so the sentence read here can only be this
     press's answer. The state being measured lives on the server, not in the page. */
  const reuse_arm = await openSheet(browser);
  const second = await press(reuse_arm.page);
  const posts_on_second_press = reuse_arm.posts();
  const rows_after_second = await roster(reuse_arm.page);

  const reading = {
    roster_reset_by_this_arm: reset,
    rows_before: rows_before.length,
    rows_after_first: rows_after_first.length,
    rows_after_second: rows_after_second.length,
    first_press_door_calls: write_arm.calls,
    second_press_door_calls: reuse_arm.calls,
    first_press_page_errors: write_arm.errors,
    second_press_page_errors: reuse_arm.errors,
    posts_on_first_press,
    /** The number this order is about: a second press that writes leaves another grant behind. */
    posts_on_second_press,
    first,
    second,
    same_grant_id: first.grant_id !== null && first.grant_id === second.grant_id,
    same_link: first.link !== "" && first.link === second.link,
    same_token: tokenOf(first.link) !== null && tokenOf(first.link) === tokenOf(second.link),
    /** The row the screen handed over, as the server itself describes it. */
    row_the_screen_named: rows_after_second.find((row) => row.id === second.grant_id) ?? null,
  };
  write("A1_write_then_reuse", reading);

  expect(reading.roster_reset_by_this_arm.standing_left, "the arm started from no standing link").toBe(0);
  /* `rows_before` is the roster's length, and the door keeps withdrawn rows in that answer on purpose
     (`backend/app/main.py:4346`), so it is recorded and not asserted: on a second trip over the same
     store it is a count of history, not a count of what could be reused. The first cut of this line
     compared that number to zero and went red on a correct product. */
  expect(reading.first_press_page_errors, "the writing press threw nothing").toEqual([]);
  expect(posts_on_first_press, "the press that found nothing standing wrote exactly one grant").toBe(1);
  expect(first.state, "and confirmed it through the read-back").toBe("CONFIRMED");
  expect(WROTE.test(first.sentence), "with the words that say a write happened").toBe(true);
  expect(reading.second_press_page_errors, "the reusing press threw nothing").toEqual([]);
  expect(second.state, "the next press confirms too").toBe("CONFIRMED");
  expect(reading.posts_on_second_press, "but it made no write call at all").toBe(0);
  expect(reading.rows_after_second, "and the roster is the size the first press left it").toBe(
    reading.rows_after_first,
  );
  expect(reading.same_link, "the address handed over is the same address").toBe(true);
  expect(reading.same_token, "the token inside it is the same token").toBe(true);
  expect(reading.same_grant_id, "named by the same row of the server's own list").toBe(true);
  expect(REUSED.test(second.sentence), "and the sheet says in words that it wrote nothing").toBe(true);
  expect(first.key_echo || second.key_echo, "neither sentence is a key name").toBe(false);
  expect(second.link_present, "a reused link is still a link: the field that carries it is present")
    .toBe(1);
  expect(reading.row_the_screen_named?.revoked_at, "and the row it names is standing").toBeNull();
  await reuse_arm.context.close();
});

test("ORD-0541 A2 withdrawing the standing link makes the next press write again", async ({ browser }) => {
  const setup_arm = await openSheet(browser);
  await revokeAllStanding(setup_arm.page);
  const wrote = await press(setup_arm.page);
  const posts_on_setup_press = setup_arm.posts();
  const setup_link = wrote.link;
  await setup_arm.context.close();

  /* The link the set-up press handed over is withdrawn through the door the product itself uses, and the
     press under test is asked on a fresh sheet -- so what this arm reads is the outlet's answer to
     "nothing stands any more", not its memory of the press before. */
  const arm = await openSheet(browser);
  const revoke = (await arm.page.request.post(`${FRONT}${DOOR}/${wrote.grant_id}/revoke`)).status();
  const rows_after_revoke = await roster(arm.page);
  const row_now = rows_after_revoke.find((row) => row.id === wrote.grant_id) ?? null;

  const again = await press(arm.page);
  const posts_on_press_after_revoke = arm.posts();
  const reading = {
    posts_on_setup_press,
    revoke_http_status: revoke,
    row_named_by_screen_after_revoke: row_now,
    standing_after_revoke: standingLinks(rows_after_revoke).length,
    press_door_calls: arm.calls,
    press_page_errors: arm.errors,
    posts_on_press_after_revoke,
    wrote,
    again,
    link_changed: setup_link !== again.link,
  };
  write("A2_revoke_then_write", reading);

  expect(posts_on_setup_press, "the set-up press wrote the link this arm withdraws").toBe(1);
  expect(reading.press_page_errors, "the press under test threw nothing").toEqual([]);
  expect(revoke, "the revoke door answered").toBe(200);
  expect(row_now?.revoked_at, "and the roster says that row is withdrawn").not.toBeNull();
  expect(reading.standing_after_revoke, "with nothing link-readable left standing").toBe(0);
  expect(reading.posts_on_press_after_revoke, "the next press writes exactly one grant").toBe(1);
  expect(again.state, "and confirms it the same way as any other write").toBe("CONFIRMED");
  expect(WROTE.test(again.sentence), "with the words that say a write happened").toBe(true);
  expect(reading.link_changed, "and the address is a new one, not a withdrawn link").toBe(true);
  await arm.context.close();
});

test("ORD-0541 A3 a standing row at another tier is not this outlet's link", async ({ browser }) => {
  /* Every shape is the same sentence: "the list door offers a row that stands, has a token, and is not
     `LINK_READ`". The ruling names one tier this outlet may hand over, so any other tier has to reach
     the create door -- and the row it writes has to be the tier asked for, not the one it found. The
     last two shapes are the other half of the predicate: a `LINK_READ` row whose answer carries no
     token, or already carries a revocation. */
  const shapes = [
    { name: "PUBLIC", rows: [{ id: "grant-public-standing", visibility: "PUBLIC", revoked_at: null, expires_at: null, token: "p".repeat(32) }] },
    { name: "PRIVATE", rows: [{ id: "grant-private-standing", visibility: "PRIVATE", revoked_at: null, expires_at: null, token: "r".repeat(32) }] },
    { name: "INVITEES_ONLY", rows: [{ id: "grant-invitees-standing", visibility: "INVITEES_ONLY", revoked_at: null, expires_at: null, token: "i".repeat(32) }] },
    { name: "LINK_READ_without_token", rows: [{ id: "grant-no-token", visibility: "LINK_READ", revoked_at: null, expires_at: null }] },
    { name: "LINK_READ_revoked", rows: [{ id: "grant-revoked", visibility: "LINK_READ", revoked_at: "2026-10-01T00:00:00+00:00", expires_at: null, token: "v".repeat(32) }] },
  ];
  const readings: any[] = [];
  for (const shape of shapes) {
    const arm = await openSheet(browser);
    await revokeAllStanding(arm.page);
    await fakeFirstAsk(arm.page, shape.rows);
    const first = await press(arm.page);
    const rows = await roster(arm.page);
    readings.push({
      shape: shape.name,
      door_calls: arm.calls,
      page_errors: arm.errors,
      /** The arm's whole question: did this press write, or did it hand over somebody else's row? */
      posts: arm.posts(),
      state: first.state,
      sentence: first.sentence,
      reused_wording: REUSED.test(first.sentence),
      grant_id: first.grant_id,
      row_written: rows.find((row) => row.id === first.grant_id) ?? null,
      invented_row_handed_over: first.grant_id === shape.rows[0].id,
      invented_token_handed_over: tokenOf(first.link) === (shape.rows[0] as any).token,
    });
    await arm.context.close();
  }
  write("A3_tier_predicate", { readings });

  for (const reading of readings) {
    expect(reading.page_errors, `${reading.shape}: the press settled, it did not throw`).toEqual([]);
    expect(reading.state, `${reading.shape}: the write still confirms through the read-back`)
      .toBe("CONFIRMED");
    expect(reading.posts, `${reading.shape}: a row of that shape is not reusable, so this press wrote`)
      .toBe(1);
    expect(reading.reused_wording, `${reading.shape}: the sheet cannot claim it wrote nothing`).toBe(false);
    expect(reading.invented_row_handed_over, `${reading.shape}: the id on screen is not the invented row`)
      .toBe(false);
    expect(
      reading.invented_token_handed_over,
      `${reading.shape}: and neither is the token inside the link`,
    ).toBe(false);
    expect(
      reading.row_written?.visibility,
      `${reading.shape}: the row the server made is the tier this outlet asks for`,
    ).toBe("LINK_READ");
  }
});

test("ORD-0541 A4 a list door that refuses costs the press no write", async ({ browser }) => {
  const arm = await openSheet(browser);
  await refuseAllDoors(arm.page);
  const press1 = await press(arm.page);
  const reading = {
    door_calls: arm.calls,
    page_errors: arm.errors,
    posts: arm.posts(),
    state: press1.state,
    sentence: press1.sentence,
    status_named: /状态码 (\d{3})|status (\d{3})/.exec(press1.sentence)?.[1] ?? null,
    link_present: press1.link_present,
  };
  write("A4_read_refused", reading);

  /* ORD-0528 already required this sentence; ORD-0541 adds the half that costs a write. A press that
     cannot ask what stands must not mint a grant on the strength of not having asked -- that is the same
     accumulation A1 measures, arriving through a failing door instead of a working one. */
  expect(reading.page_errors, "the refusing door is answered, not thrown").toEqual([]);
  expect(reading.state, "a refusing list door is a non-confirmation").toBe("NOT_CONFIRMED");
  expect(reading.posts, "and it is a press that wrote nothing").toBe(0);
  expect(reading.status_named, "the screen quotes the number the door answered with").toBe("500");
  expect(reading.link_present, "with no link handed over").toBe(0);
  await arm.context.close();
});

test("ORD-0541 A5 the two confirmations are two sentences, in both languages and both shapes", async ({
  browser,
}) => {
  const readings: any[] = [];
  for (const locale of ["zh-CN", "en"] as const) {
    for (const viewport of [
      { name: "phone", width: 390, height: 844 },
      { name: "desktop", width: 1280, height: 900 },
    ] as const) {
      const write_arm = await openSheet(browser, locale, { width: viewport.width, height: viewport.height });
      await revokeAllStanding(write_arm.page);
      const wrote = await press(write_arm.page);
      const posts_on_first_press = write_arm.posts();
      await write_arm.context.close();

      const reuse_arm = await openSheet(browser, locale, { width: viewport.width, height: viewport.height });
      const reused = await press(reuse_arm.page);
      const posts_on_second_press = reuse_arm.posts();
      const sheet = (await reuse_arm.page.getByTestId("trip-poster").innerText()).trim();
      const box = await reuse_arm.page.getByTestId("poster-share-result").boundingBox();

      readings.push({
        locale,
        viewport: `${viewport.width}x${viewport.height}`,
        posts_on_first_press,
        posts_on_second_press,
        wrote_sentence: wrote.sentence,
        reused_sentence: reused.sentence,
        sheet_inner_text_chars: sheet.length,
        result_box: box ? { width: Math.round(box.width), height: Math.round(box.height) } : null,
        same_link: wrote.link !== "" && wrote.link === reused.link,
        sentences_are_different_words: wrote.sentence !== reused.sentence,
        wrote_words: WROTE.test(wrote.sentence),
        reused_words: REUSED.test(reused.sentence),
        page_errors: [...write_arm.errors, ...reuse_arm.errors],
        key_echo_hits: (sheet.match(/\b(poster|sharedPoster|shared)\.[A-Za-z0-9_.]+/g) ?? []).length,
      });
      await reuse_arm.context.close();
    }
  }
  write("A5_screen_reads", { readings });
  for (const reading of readings) {
    expect(reading.page_errors, `${reading.locale} at ${reading.viewport}: nothing thrown`).toEqual([]);
    expect(reading.key_echo_hits, `${reading.locale} at ${reading.viewport} leaks no key name`).toBe(0);
    expect(reading.sheet_inner_text_chars).toBeGreaterThan(60);
    expect(reading.posts_on_first_press, `${reading.locale} at ${reading.viewport}: the first press wrote`)
      .toBe(1);
    expect(reading.wrote_words, `${reading.locale} at ${reading.viewport}: and says it wrote`).toBe(true);
    expect(reading.posts_on_second_press, `${reading.locale} at ${reading.viewport}: the second press writes nothing`)
      .toBe(0);
    expect(reading.reused_words, `${reading.locale} at ${reading.viewport}: and says it did not`).toBe(true);
    expect(
      reading.sentences_are_different_words,
      `${reading.locale} at ${reading.viewport}: 新建 and 复用 are not the same sentence`,
    ).toBe(true);
    expect(reading.same_link, `${reading.locale} at ${reading.viewport}: and hands the same address back`)
      .toBe(true);
    expect(
      reading.result_box?.height,
      `${reading.locale} at ${reading.viewport}: the line is laid out, not absent`,
    ).toBeGreaterThan(0);
  }
});
