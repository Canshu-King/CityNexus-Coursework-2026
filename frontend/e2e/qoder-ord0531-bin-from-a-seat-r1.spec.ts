/**
 * ORD-0531 -- the recycle bin, read as rendered text from two seats.
 *
 * The order asks for both directions on the screen and not from data attributes, so every assertion
 * below reads `innerText`.  The world is seeded through the product's own doors on this lane's stack
 * (see `seed_r1.py` beside the lane receipts), and the ids and titles come from that seed file rather
 * than being hardcoded here: a stale uuid in a spec reads as a pass against a row that no longer exists.
 *
 *  - V1 the traveller's own binned trip: named as theirs, and the 找回 action is there.
 *  - V2 a trip that belongs to somebody else where this traveller only holds an ADMIN seat: named as
 *    another's, the role sentence is rendered, and there is no action on that row.
 *  - V3 a trip belonging to a third traveller with no seat at all: its title does not appear.
 *  - V4 the two directions in one read: two rows, one action.
 *  - V5 the same screen from the owner's seat: both of their rows carry an action.
 *  - V6 no identifier in what the traveller is shown.
 *  - V7 what the screen does when it has no traveller to ask about.
 *  - V9 the two refusal sentences told apart, on the door this screen actually asks (CMDR 02:35 ①补:
 *       the whole-bin URL another lane stubs is no longer the one this screen calls).
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";

const SEED = JSON.parse(
  readFileSync("D:/moodle/_qoder_6a_ord0531_20261001/seed_r1.json", "utf8"),
) as {
  trips: Record<string, string>;
  titles: Record<string, string>;
  viewer_journey_for_B: string;
  viewer_journey_for_A: string;
};

const EVIDENCE = "D:/moodle/_qoder_6a_ord0531_20261001/e2e";
const UUID = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
const readings: Record<string, unknown> = {};

test.setTimeout(180_000);

function note(name: string, body: unknown) {
  readings[name] = body;
  console.log(`O531 ${name} ${JSON.stringify(body)}`);
}

async function openBinAs(page: Page, viewerJourneyId: string | null) {
  await page.addInitScript((id) => {
    if (id) localStorage.setItem("citynexus.v8.journey_id", id);
    else localStorage.removeItem("citynexus.v8.journey_id");
  }, viewerJourneyId);
  await page.goto("/v8/trash", { waitUntil: "load" });
  await expect(page.getByTestId("journey-trash-panel").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1_200);
  return page.getByTestId("journey-trash-panel").first();
}

/** The rendered rows, each with the text of its own title, owner line and any action. */
async function rowTexts(panel: import("@playwright/test").Locator) {
  const out: Array<{ title: string; all: string }> = [];
  for (const row of await panel.getByTestId("trash-row").all()) {
    out.push({
      title: (await row.getByTestId("trash-row-title").innerText()).trim(),
      all: (await row.innerText()).replace(/\s+/g, " ").trim(),
    });
  }
  return out;
}

test.describe.serial("ORD-0531 the bin from a seat", () => {
  test("V1..V4 both directions on B's screen", async ({ page }) => {
    const panel = await openBinAs(page, SEED.viewer_journey_for_B);
    const rows = await rowTexts(panel);
    const mine = rows.find((row) => row.title === SEED.titles.mine_B);
    const readOnly = rows.find((row) => row.title === SEED.titles.read_only_B);
    const hidden = rows.find((row) => row.title === SEED.titles.hidden_B);
    const buttons = await panel.getByTestId("trash-bring-back").count();
    const reasons = await panel.getByTestId("trash-row-readonly-reason").count();
    const panelText = (await panel.innerText()).replace(/\s+/g, " ");

    /* ORD-0531 r3 merges onto the generation where ORD-0535 landed the per-row "which discard is
       this" line. Both cells belong on one row, so the line that is not this order's own is asserted
       here too: if my merge had dropped their span, this screen would read two rows and one feature. */
    let settledSequence: string[] = [];
    await expect
      .poll(async () => {
        const texts = await panel.getByTestId("trash-row-sequence").allInnerTexts();
        return texts.filter((text) => /正在读这条行程的丢弃流水/.test(text)).length;
      }, { timeout: 20_000 })
      .toBe(0);
    settledSequence = await panel.getByTestId("trash-row-sequence").allInnerTexts();
    const sequenceCount = settledSequence.length;

    note("V1_V4_as_B", {
      row_titles: rows.map((row) => row.title),
      own_row_text: mine?.all ?? null,
      read_only_row_text: readOnly?.all ?? null,
      third_party_row_present: hidden !== undefined,
      action_buttons_on_screen: buttons,
      role_reason_sentences_on_screen: reasons,
      discard_sequence_lines_on_screen: sequenceCount,
      discard_sequence_texts: settledSequence,
      uuid_in_rendered_text: UUID.test(panelText),
      panel_text_head: panelText.slice(0, 200),
    });

    expect(mine, "B's own binned trip should be on B's screen").toBeTruthy();
    expect(mine!.all).toContain("在这台服务器上属于你");
    expect(mine!.all).toContain("找回");
    expect(readOnly, "the trip B holds an ADMIN seat on should be visible").toBeTruthy();
    expect(readOnly!.all).toContain("由这台服务器上的另一位旅客保管");
    expect(readOnly!.all).toContain("你那个角色不能把它找回来");
    expect(readOnly!.all).not.toContain("在这台服务器上属于你");
    expect(hidden, "a third traveller's discarded trip must not reach this bin").toBeFalsy();
    expect(reasons).toBe(1);
    expect(buttons).toBe(1);
    expect(UUID.test(panelText)).toBe(false);
    expect(sequenceCount, "ORD-0535's discard-sequence line must survive this lane's merge").toBe(rows.length);
    expect(settledSequence.every((text) => text.trim().length > 0), "every row's sequence line must say something").toBeTruthy();
    expect(settledSequence.every((text) => !text.includes("trash.")), "a copy key must not reach the traveller").toBeTruthy();
    await page.screenshot({ path: `${EVIDENCE}/V1_V4_as_B.png`, fullPage: true });
  });

  test("V5 the same screen from the owner's seat", async ({ page }) => {
    const panel = await openBinAs(page, SEED.viewer_journey_for_A);
    const rows = await rowTexts(panel);
    const buttons = await panel.getByTestId("trash-bring-back").count();
    note("V5_as_A", {
      row_titles: rows.map((row) => row.title),
      action_buttons_on_screen: buttons,
      third_party_row_present: rows.some((row) => row.title === SEED.titles.hidden_B),
    });
    expect(rows.some((row) => row.title === SEED.titles.mine_A)).toBe(true);
    expect(buttons).toBe(2);
    await page.screenshot({ path: `${EVIDENCE}/V5_as_A.png`, fullPage: true });
  });

  test("V7 what answers when the shell has no traveller", async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem("citynexus.v8.journey_id"), null);
    await page.goto("/v8/trash", { waitUntil: "load" });
    await page.waitForTimeout(2_500);
    const panel = page.getByTestId("journey-trash-panel");
    const panelPresent = await panel.count();
    const noViewerPresent = panelPresent
      ? await panel.getByTestId("trash-no-viewer").count()
      : 0;
    const rowsAnywhere = await page.getByTestId("trash-row").count();
    const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
    const whichSurface: Record<string, boolean> = {};
    for (const id of ["trial-setup", "v8-hub", "today-strip", "v8-trips", "v8-live"]) {
      whichSurface[id] = (await page.getByTestId(id).count()) > 0;
    }
    note("V7_no_viewer", {
      panel_rendered: panelPresent,
      named_no_viewer_state_rendered: noViewerPresent,
      bin_rows_rendered_anywhere: rowsAnywhere,
      surfaces_present: whichSurface,
      body_head: body.slice(0, 160),
    });
    /* The claim this arm holds is the leak, not the route: with no seat to ask about, no discarded
       trip of anybody's may be on the screen. Whether the shell reaches the panel at all is recorded
       above rather than asserted, because that is the shell's navigation rule, not this order's. */
    expect(rowsAnywhere).toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/V7_no_viewer.png`, fullPage: true });
  });

  test("V9 the two sentences the screen owns are told apart on the door it actually asks", async ({ page }) => {
    /* CMDR 02:35 item 1, ①补.  Lane 82f55d8a's arm stubs the whole-bin collection URL to prove
       「没答上」 is said; this screen asks the per-traveller door since ORD-0531, so that lever no
       longer reaches it.  This is the same lever aimed at the door this screen calls, and it judges the
       one line that decides which of the two sentences the traveller is shown (the 404 and 405 branch
       versus every other failure).  Both directions are read, because a constant would satisfy one. */
    const scoped = (url: URL) =>
      url.pathname.startsWith("/api/v1/travelers/") && url.pathname.endsWith("/recycle-bin/journeys");

    const refuse = async (status: number) => {
      let asked = 0;
      await page.route(scoped, (route) => {
        asked += 1;
        void route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify({ detail: "answered nothing at all" }),
        });
      });
      const panel = await openBinAs(page, SEED.viewer_journey_for_B);
      const notice = panel.getByTestId("trash-read-failed");
      await expect(notice, `a refused scoped door (http_status=${status}) must be said on screen`).toBeVisible({
        timeout: 20_000,
      });
      const text = (await notice.innerText()).replace(/\s+/g, " ").trim();
      const rows = await panel.getByTestId("trash-row").count();
      await page.unroute(scoped);
      return { asked, text, rows };
    };

    const notAnswered = await refuse(404);
    const broken = await refuse(500);
    note("V9_two_doors_two_sentences", {
      scoped_lever_hits_404: notAnswered.asked,
      scoped_lever_hits_500: broken.asked,
      sentence_when_door_refused_404: notAnswered.text.slice(0, 120),
      sentence_when_transport_broke_500: broken.text.slice(0, 120),
      rows_rendered_in_either_case: [notAnswered.rows, broken.rows],
    });

    expect(notAnswered.asked, "the screen never asked the door this lever refuses").toBeGreaterThan(0);
    expect(broken.asked, "the second refusal was never asked either").toBeGreaterThan(0);
    expect(notAnswered.text).toContain("没有答出");
    expect(notAnswered.text).not.toContain("回收站没有从这台服务器读到");
    expect(broken.text).toContain("回收站没有从这台服务器读到");
    expect(broken.text).not.toContain("没有答出");
    expect(notAnswered.rows + broken.rows, "a door that answered nothing may not render anybody's trip").toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/V9_two_sentences.png`, fullPage: true });
  });

  test("V10 the sentence the screen says when the payload names no traveller", async ({ page }) => {
    /* ORD-0531 r7 -- the gap lane 82f55d8a named in the ledger: `noViewer` had no e2e reading of the
       sentence it renders (V7 only proved no row leaks).  The lever has to be the shell's own journey
       read, because that is where the prop comes from (`v8PanelRegistry.tsx`: `travelerId={s.data?.
       journey?.traveler_id ?? null}`), so the single variable is that one field.
       Named honestly: this shape is NOT server-producible on this line today -- `journeys.traveler_id`
       is a NOT NULL foreign key and `schemas.Journey` requires the field -- so the arm judges the
       screen's declared contract for a payload that names nobody, not a state the live door answers.
       Both directions are read: the sentence the screen owns, and the two doors it must not ask. */
    const journeyId: string = SEED.viewer_journey_for_B;
    const experiencePath = `/api/v1/journeys/${journeyId}/experience`;

    const real = await page.request.get(experiencePath);
    expect(real.status(), "the shell's own journey read must answer on this stack").toBe(200);
    const body = await real.json();
    const realTraveler = body?.journey?.traveler_id ?? null;
    expect(realTraveler, "the payload this line actually answers does name a traveller").toBeTruthy();

    const stripped = { ...body, journey: { ...body.journey } };
    delete stripped.journey.traveler_id;

    /* Listener after the real read, so the read this arm makes itself is not counted as the screen's. */
    const paths: string[] = [];
    page.on("request", (request) => paths.push(new URL(request.url()).pathname));
    const pageerrors: string[] = [];
    page.on("pageerror", (error) => pageerrors.push(String(error).slice(0, 160)));
    await page.route(experiencePath, (route) =>
      void route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(stripped),
      }),
    );

    const panel = await openBinAs(page, journeyId);
    const noViewer = panel.getByTestId("trash-no-viewer");
    const askedScoped = paths.filter(
      (p) => p.startsWith("/api/v1/travelers/") && p.endsWith("/recycle-bin/journeys"),
    );
    const askedWholeBin = paths.filter((p) => p === "/api/v1/recycle-bin/journeys");
    const rows = await panel.getByTestId("trash-row").count();

    note("V10_doors_before_the_screen_speaks", {
      scoped_bin_doors_asked: askedScoped,
      whole_bin_door_asked: askedWholeBin,
      every_path_this_page_asked: paths,
      rows_rendered: rows,
    });

    /* Leg 1 -- the fallback the order forbids: with no viewer there is nobody to scope by, and the
       whole-bin door is deliberately NOT used as a substitute.  Judged before any text is read, so a
       mutant that asks a door reddens THIS leg and not the one below it. */
    expect(
      askedScoped.length + askedWholeBin.length,
      `with no traveller named the screen asked a bin door anyway: ${JSON.stringify(paths)}`,
    ).toBe(0);

    await expect(noViewer, "naming no traveller must be said on the screen, not left blank").toBeVisible({
      timeout: 20_000,
    });
    const sentence = (await noViewer.innerText()).replace(/\s+/g, " ").trim();
    const panelText = (await panel.innerText()).replace(/\s+/g, " ");

    note("V10_no_traveller_named", {
      panel_rendered: 1,
      real_payload_named_traveller: realTraveler,
      served_payload_has_traveler_id: "traveler_id" in stripped.journey,
      scoped_bin_doors_asked: askedScoped,
      whole_bin_door_asked: askedWholeBin,
      sentence_rendered: sentence,
      rows_rendered: rows,
      loading_sentence_still_on_screen: await panel.getByTestId("trash-loading").count(),
      pageerrors,
    });

    /* Leg 2 -- the sentence itself, read as rendered text. */
    expect(sentence).toContain("还不知道要问谁的回收站");
    expect(sentence).toContain("你的行程没有因此变化");
    /* Leg 3 -- it is its own state: neither the empty-bin sentence, nor either refusal sentence, nor
       the loading sentence is what the traveller is shown. */
    expect(panelText).not.toContain("回收站是空的");
    expect(panelText).not.toContain("这台服务器没有答出");
    expect(panelText).not.toContain("回收站没有从这台服务器读到");
    expect(await panel.getByTestId("trash-loading").count(), "still spinning where it should have said so").toBe(0);
    /* Leg 4 -- and nothing is shown while nobody was named. */
    expect(rows, "a bin read for no traveller may not render a row").toBe(0);
    await page.screenshot({ path: `${EVIDENCE}/V10_no_traveller_named.png`, fullPage: true });
  });

  test("V8 write the readings file", async () => {
    writeFileSync(`${EVIDENCE}/readings.json`, JSON.stringify(readings, null, 1), "utf8");
  });
});
