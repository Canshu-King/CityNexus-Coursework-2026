import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0477 — the trip list on `/v8/access` had one order and no way to ask for another.
 *
 * Measured at the authoritative tip `dcb90cccc68b81f2317b28db19051dedb9f06c80` before this file
 * existed, `GET /api/v1/journeys` answered in `journeys.created_at, journeys.id` order and
 * the screen that lists trips -- `features/access/AccessPanel.tsx`, the only consumer of that door on
 * this tree (`readJourneys` at `features/access/accessDoors.ts`) -- rendered a `<select>` whose option
 * order was therefore whatever the store kept.  A traveller who wanted the next departure first had no
 * control to ask with, and no sentence on screen said which order was in force.
 *
 * So the door takes a named tier and the screen says the tier out loud.  What these arms judge:
 *
 *   sort-control-{loc}        the three tiers are on the glass as three words a traveller reads, one
 *                             and only one reports `aria-pressed=true`, and the server's machine tokens
 *                             (`RECENT_UPDATE` ...) do not appear as labels;
 *   sort-reorders-{loc}       switching a tier changes the *DOM order* of the trip rows, and each of
 *                             the three orders equals a sequence computed here from the titles and
 *                             departure instants this file chose -- not from the door's own answer.
 *                             The three expectations are asserted pairwise different, so an arm that
 *                             quietly compares one list with itself fails instead of passing;
 *   sort-asked-by-word-{loc}  the reorder is the server's, not a local `Array.sort`: the request the
 *                             screen made carries `?sort=<word>`, the current-tier sentence changes,
 *                             and the name-tier's "characters, not pinyin" note appears only under the
 *                             tier it describes;
 *   sort-empty-{loc}          every trip goes in the bin through the product's own DELETE door, and
 *                             what reads back is the named empty sentence -- not the network-failure
 *                             sentence, and not an empty picker that looks like a loading glitch.
 *                             Each trip is brought back with 找回 afterwards;
 *   sort-geometry-{w}x{h}-{loc}
 *                             the tier control is reachable where it stands -- hit-testable at its own
 *                             centre, above the frozen 768 px floor, inside the viewport -- and it
 *                             costs this screen no reachable control.  The cost is measured by
 *                             subtraction on one build: the census is taken with the block in layout
 *                             and again with it removed, and anything reachable only in the second
 *                             reading was pushed out by this order's own control;
 *   sort-refusal-over-http    an unknown word is answered by name over the wire (422,
 *                             `JOURNEY_SORT_NOT_RECOGNISED`, the word and the supported list in
 *                             `error.details`) and is not answered with a list.
 *
 * Nothing is mocked, no response body is rewritten, no arm is skipped, and no assertion reads a data
 * attribute in place of the words on the glass.  One evidence file per arm, written inside the arm.
 */

const FILE = "e2e/qoder-ord0477-journey-list-sort.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
const DIR = "ord0477-sort";

const HIT_FLOOR = 768;
const SHAPES: Array<[number, number]> = [
  [390, 844],
  [320, 653],
];
/* The two readings this screen has to survive: the traveller's words, and the words' width.
   `citynexus.locale` is honoured only as exactly "zh-CN" or "en" -- the guard is the
   `stored === "zh-CN" || stored === "en"` test on the `citynexus.locale` read inside
   `LocaleProvider` in `src/i18n/LocaleContext.tsx`, and `TripLocale` is the union it returns -- so
   an arm that writes "zh" gets the browser's language instead and measures the wrong dictionary --
   which is what the first cut of this file did.  Named by symbol rather than by line number on
   purpose: line numbers move every generation, and a drifted anchor silently describes whatever
   line lands there.  Guarded by `qoder-a68b0631-label-anchors-not-line-numbers-r1`. */
const ARMS = [
  { code: "zh", locale: "zh-CN", recent: "最近更新", departure: "出发日期", title: "名称" },
  { code: "en", locale: "en", recent: "Recently updated", departure: "Departure date", title: "Name" },
] as const;

const RECENT = "RECENT_UPDATE";
const DEPARTURE = "DEPARTURE_DATE";
const TITLE = "TITLE";

/** Every trip an arm creates carries that arm's own prefix, so the two locale arms cannot read each
 *  other's rows in a store the seed and the previous arm both write to. */
const MARK = "ORD0477";
const mark = (code: string) => `${MARK}-${code}`;

function write(arm: string, readings: unknown): string {
  const path = evidencePath(DIR, `${RUN}_${arm}.json`);
  writeFileSync(path, JSON.stringify({ file: FILE, arm, run_id: RUN, readings }, null, 1));
  return path;
}

async function enterAccess(page: Page, locale: string): Promise<void> {
  await page.addInitScript((loc) => {
    try {
      localStorage.setItem("citynexus.locale", loc);
    } catch {
      /* a profile that refuses localStorage is reported by the arms that need the journey id */
    }
  }, locale);
  // the route panel needs the shell to have a journey to boot against; the seed project's
  // storageState carries the one it made through the product's own doors.
  await openScreen(page, "/v8/access", ["access-panel"]);
}

/** Proof that the subject of the reading is actually on the glass, not the loading shell. */
async function assertSubjectOnScreen(page: Page, locale: string): Promise<void> {
  await expect(
    page.getByTestId("access-panel"),
    node("S0", `${locale}: /v8/access never mounted the panel -- the reading would be of the shell`),
  ).toBeVisible();
}

async function tierWords(page: Page): Promise<Array<{ tier: string; word: string; pressed: string | null }>> {
  return page.$$("[data-testid^='access-sort-tier-']").then(async (handles) =>
    Promise.all(
      handles.map(async (handle) => ({
        tier: (await handle.getAttribute("data-testid"))!.replace("access-sort-tier-", ""),
        word: (await handle.textContent()) ?? "",
        pressed: await handle.getAttribute("aria-pressed"),
      })),
    ),
  );
}

/** The trip rows in the order the door put them, as a traveller's screen reader would read them.
 *  ORD-0518 replaced the closed `<select>` with visible rows, so this reads the roster; the arms
 *  below still judge the door's order, which is what did not change. */
async function optionTitles(page: Page): Promise<string[]> {
  return page.$$eval("[data-testid='access-trip-row']", (rows) =>
    rows.map((row) => (row.textContent ?? "").trim()),
  );
}

async function currentTierSentence(page: Page): Promise<string> {
  return (await page.getByTestId("access-sort-current").textContent()) ?? "";
}

async function tapTier(page: Page, tier: string): Promise<void> {
  await page.click(`[data-testid='access-sort-tier-${tier}']`);
  await page
    .waitForFunction(
      () => document.querySelectorAll("[data-testid='access-trips-loading']").length === 0,
      undefined,
      { timeout: 20_000 }
    )
    .catch(() => undefined);
  await page.waitForTimeout(1_200);
}

type TripSpec = { title: string; start: string };

/** Create a trip through the product's own door, so the row the list orders is a row the product wrote. */
async function createTrip(
  request: APIRequestContext,
  travelerId: string,
  code: string,
  spec: TripSpec,
  dayOffset = 2,
): Promise<string> {
  const end = new Date(new Date(spec.start).getTime() + dayOffset * 86_400_000).toISOString();
  const created = await request.post("api/v1/journeys", {
    data: {
      traveler_id: travelerId,
      title: `${mark(code)} ${spec.title}`,
      city_code: "CN-TIANJIN",
      timezone: "Asia/Shanghai",
      start_at: spec.start,
      end_at: end,
      budget: { amount_minor: 90000, currency: "CNY" },
      planning_mode: "balanced",
    },
  });
  const body = await created.json().catch(() => null);
  expect(
    created.status(),
    node("S1", `creating "${spec.title}" answered ${created.status()}: ${JSON.stringify(body)}`),
  ).toBe(201);
  return body.id as string;
}

async function seedTraveler(request: APIRequestContext, code: string): Promise<string> {
  const created = await request.post("api/v1/travelers", {
    data: { display_name: `${mark(code)} traveller`, locale: "zh-CN", timezone: "Asia/Shanghai" },
  });
  expect(created.status(), node("S1", `the traveller door answered ${created.status()}`)).toBe(201);
  return (await created.json()).id as string;
}

/** The frozen reachability census (same control set, same floor, same identity rule as the geometry
 *  gates already on this line), taken over the access screen. */
async function controlCensus(page: Page) {
  return page.evaluate((floor: number) => {
    const selector = "button, a[href], input, select, textarea, [role=button]";
    const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
    const seen = new Map<string, number>();
    return nodes.map((element) => {
      const testId = element.getAttribute("data-testid");
      const name = (element.getAttribute("aria-label") ?? element.textContent ?? "")
        .trim()
        .slice(0, 40);
      const base = testId ?? name ?? "(unnamed)";
      const nth = (seen.get(base) ?? 0) + 1;
      seen.set(base, nth);
      const box = element.getBoundingClientRect();
      const centre = [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
      const atCentre = document.elementFromPoint(centre[0], centre[1]);
      return {
        key: testId ? `${base}` : `${name} #${nth}`,
        testid: testId,
        box: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
        centre,
        in_viewport:
          box.y >= 0 && box.bottom <= window.innerHeight && box.x >= 0 && box.right <= window.innerWidth,
        centre_above_floor: centre[1] <= floor,
        hit_test: !!atCentre && (atCentre === element || element.contains(atCentre)),
      };
    });
  }, HIT_FLOOR);
}

const reachable = (census: Awaited<ReturnType<typeof controlCensus>>) =>
  census.filter((c) => c.hit_test && c.centre_above_floor && c.in_viewport).map((c) => c.key);

// ------------------------------------------------------------------ the control, in both languages

for (const arm of ARMS) {
  test(`sort-control-${arm.code} — three named tiers, one in use, no machine word on the glass`, async ({
    page,
  }) => {
    await enterAccess(page, arm.locale);
    await assertSubjectOnScreen(page, arm.code);
    const words = await tierWords(page);
    const sentence = await currentTierSentence(page);
    const shot = evidencePath(DIR, `${RUN}_control_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write(`sort-control-${arm.code}`,
      { words, sentence, shot, option_order: await optionTitles(page) },
    );

    // ORD-0518 added a fourth control: 创建序, which is this door's order when no `sort` is sent
    expect(words.length, node("C1", `${arm.code}: the tier control offers ${words.length} tiers`)).toBe(4);
    expect(
      words.map((w) => w.tier).sort(),
      node("C1", `${arm.code}: the tier set is not the contract's three words plus creation order: ${JSON.stringify(words)}`),
    ).toEqual(["CREATION_ORDER", DEPARTURE, RECENT, TITLE].sort());
    expect(
      words.filter((w) => w.tier !== "CREATION_ORDER").map((w) => w.word.trim()),
      node("C1", `${arm.code}: the buttons do not read ${arm.recent}/${arm.departure}/${arm.title}: ${JSON.stringify(words)}`),
    ).toEqual([arm.recent, arm.departure, arm.title]);
    expect(
      words.filter((w) => w.pressed === "true").length,
      node("C1", `${arm.code}: exactly one tier may report itself in use: ${JSON.stringify(words)}`),
    ).toBe(1);
    expect(
      words.filter((w) => w.pressed === "true")[0]?.tier,
      node("C1", `${arm.code}: the tier in use is not the one this screen starts on`),
    ).toBe(RECENT);
    // the words a traveller reads are the dictionary's, never the server's tokens
    for (const token of [RECENT, DEPARTURE, TITLE]) {
      expect(
        words.map((w) => w.word).join(" | "),
        node("C1", `${arm.code}: the machine token ${token} reached a label`),
      ).not.toContain(token);
    }
    expect(
      sentence,
      node("C1", `${arm.code}: the current tier is not readable as rendered text: "${sentence}"`),
    ).toContain(arm.recent);
  });
}

// ------------------------------------------------------------------ the list really changes order

for (const arm of ARMS) {
  test(`sort-reorders-${arm.code} — each tier puts the same four trips in a different order`, async ({
    page,
    request,
  }) => {
    await enterAccess(page, arm.locale);
    await assertSubjectOnScreen(page, arm.code);
    const traveler = await seedTraveler(request, arm.code);

    // four rows whose three natural orders are pairwise different, and one of them edited last so
    // "recently updated" cannot be confused with "created last"
    const rows = [
      { title: "delta", start: "2026-11-06T09:00:00+08:00" },
      { title: "Alpha", start: "2026-11-03T09:00:00+08:00" }, // 01:00Z, the earliest departure
      { title: "charlie", start: "2026-11-04T08:00:00+00:00" },
      { title: "北京", start: "2026-11-05T09:00:00+08:00" },
    ];
    const ids: string[] = [];
    for (const row of rows) ids.push(await createTrip(request, traveler, arm.code, row));
    // editing the first-created row is what separates `updated_at` from `created_at`
    const edited = await request.patch(`api/v1/journeys/${ids[0]}`, {
      data: { title: `${mark(arm.code)} delta` },
    });
    expect(edited.status(), node("S1", `the edit door answered ${edited.status()}`)).toBe(200);

    const M = (name: string) => `${mark(arm.code)} ${name}`;
    const marked = rows.map((row) => M(row.title));
    const expectedByTier: Record<string, string[]> = {
      [RECENT]: [M("delta"), M("北京"), M("charlie"), M("Alpha")],
      [DEPARTURE]: [
        M("Alpha"), // 2026-11-03T01:00Z, the earliest departure of the four
        M("charlie"), // 2026-11-04T08:00Z
        M("北京"), // 2026-11-05T01:00Z
        M("delta"), // 2026-11-06T01:00Z
      ],
      [TITLE]: [M("Alpha"), M("charlie"), M("delta"), M("北京")], // case-folded characters
    };
    // the three expectations have to be three different lists or this arm measures nothing
    expect(
      new Set([expectedByTier[RECENT].join(), expectedByTier[DEPARTURE].join(), expectedByTier[TITLE].join()]).size,
      node("C2", "the three tiers were given the same expected order, so the comparison is vacuous"),
    ).toBe(3);

    const observed: Record<string, string[]> = {};
    for (const tier of [DEPARTURE, TITLE, RECENT]) {
      await tapTier(page, tier);
      observed[tier] = (await optionTitles(page)).filter((title) => marked.includes(title));
    }
    // the reading is on disk before the judgement, so a red arm still leaves what it saw
    const shot = evidencePath(DIR, `${RUN}_reorders_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write(`sort-reorders-${arm.code}`, { observed, expected: expectedByTier, ids, shot });
    for (const tier of [DEPARTURE, TITLE, RECENT]) {
      expect(
        observed[tier],
        node("C2", `${arm.code}: ${tier} answered ${JSON.stringify(observed[tier])}, expected ${JSON.stringify(expectedByTier[tier])}; all ${JSON.stringify(observed)}`),
      ).toEqual(expectedByTier[tier]);
    }
  });
}

// ------------------------------------------------------------------ the ask, in the server's own words

for (const arm of ARMS) {
  test(`sort-asked-by-word-${arm.code} — the screen asks the door, and says which tier is in use`, async ({
    page,
  }) => {
    await enterAccess(page, arm.locale);
    await assertSubjectOnScreen(page, arm.code);

    const asked: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/v1/journeys?sort=")) asked.push(request.url());
    });

    await page.click(`[data-testid='access-sort-tier-${DEPARTURE}']`);
    await page.waitForTimeout(1_600);
    const departureSentence = await currentTierSentence(page);
    const wordsAfterDeparture = await tierWords(page);
    const noteUnderDeparture = await page.getByTestId("access-sort-note").count();

    await page.click(`[data-testid='access-sort-tier-${TITLE}']`);
    await page.waitForTimeout(1_600);
    const titleSentence = await currentTierSentence(page);
    const wordsAfterTitle = await tierWords(page);
    const noteUnderTitle = await page.getByTestId("access-sort-note").textContent();

    const shot = evidencePath(DIR, `${RUN}_asked_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write(`sort-asked-by-word-${arm.code}`, {
      asked,
      departure_sentence: departureSentence,
      title_sentence: titleSentence,
      pressed_after_departure: wordsAfterDeparture.filter((w) => w.pressed === "true").map((w) => w.tier),
      pressed_after_title: wordsAfterTitle.filter((w) => w.pressed === "true").map((w) => w.tier),
      note_counts: { [DEPARTURE]: noteUnderDeparture, [TITLE]: noteUnderTitle },
      shot,
    });

    expect(
      asked.some((url) => url.includes(`sort=${DEPARTURE}`)),
      node("C3", `${arm.code}: no request carried sort=${DEPARTURE}; asked ${JSON.stringify(asked)}`),
    ).toBe(true);
    expect(
      asked.some((url) => url.includes(`sort=${TITLE}`)),
      node("C3", `${arm.code}: no request carried sort=${TITLE}; asked ${JSON.stringify(asked)}`),
    ).toBe(true);
    expect(
      departureSentence,
      node("C3", `${arm.code}: the current-tier sentence did not name the tier tapped: "${departureSentence}"`),
    ).toContain(arm.departure);
    expect(
      titleSentence,
      node("C3", `${arm.code}: the current-tier sentence did not follow the second tap: "${titleSentence}"`),
    ).toContain(arm.title);
    expect(
      titleSentence,
      node("C3", `${arm.code}: the sentence repeated the first tier instead of the second`),
    ).not.toBe(departureSentence);
    expect(
      wordsAfterTitle.filter((w) => w.pressed === "true").map((w) => w.tier),
      node("C3", `${arm.code}: the pressed state did not move with the tap: ${JSON.stringify(wordsAfterTitle)}`),
    ).toEqual([TITLE]);
    // the "not pinyin" note belongs to the name tier and to no other
    expect(noteUnderDeparture, node("C3", `${arm.code}: the name-tier note showed under ${DEPARTURE}`)).toBe(0);
    expect(
      (noteUnderTitle ?? "").length,
      node("C3", `${arm.code}: the note that names what the title tier is not did not render`)
    ).toBeGreaterThan(0);
  });
}

// ------------------------------------------------------------------ an empty collection, named

for (const arm of ARMS) {
  test(`sort-empty-${arm.code} — no trips is said as no trips, not as a failure or a glitch`, async ({
    page,
    request,
  }) => {
    await enterAccess(page, arm.locale);
    await assertSubjectOnScreen(page, arm.code);

    const listed = await request.get("api/v1/journeys?sort=DEPARTURE_DATE");
    expect(listed.status(), node("C4", `the list door answered ${listed.status()}`)).toBe(200);
    const rows = (await listed.json()) as Array<{ id: string }>;
    const binned: string[] = [];
    try {
      for (const row of rows) {
        const deleted = await request.delete(`api/v1/journeys/${row.id}`);
        expect(deleted.status(), node("C4", `delete of ${row.id} answered ${deleted.status()}`)).toBe(204);
        binned.push(row.id);
      }
      await tapTier(page, TITLE);
      const sentence = await currentTierSentence(page);
      const emptyVisible = await page.getByTestId("access-trips-empty").isVisible().catch(() => false);
      const failedVisible = await page.getByTestId("access-trips-failed").isVisible().catch(() => false);
      const rosterPresent = await page.getByTestId("access-trip-roster").count();
      const shot = evidencePath(DIR, `${RUN}_empty_${arm.code}.png`);
      await page.screenshot({ path: shot, fullPage: true });
      write(
        `sort-empty-${arm.code}`,
        { binned, empty_visible: emptyVisible, failed_visible: failedVisible, roster_present: rosterPresent, sentence, shot },
      );

      expect(emptyVisible, node("C4", `${arm.code}: zero trips did not show the named empty sentence`)).toBe(true);
      expect(failedVisible, node("C4", `${arm.code}: an empty list was reported as a failure`)).toBe(false);
      expect(
        rosterPresent,
        node("C4", `${arm.code}: a roster with no rows is not an empty state`),
      ).toBe(0);
      expect(
        sentence,
        node("C4", `${arm.code}: the tier in use stopped being readable when the list emptied: "${sentence}"`),
      ).toContain(arm.title);
    } finally {
      for (const id of binned) await request.delete(`api/v1/journeys/${id}/undelete`);
    }
  });
}

// ------------------------------------------------------------------ the control where it stands

for (const [w, h] of SHAPES) {
  for (const arm of ARMS) {
    test(`sort-geometry-${w}x${h}-${arm.code} — the tier control is reachable and costs nothing else`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: w, height: h });
      await enterAccess(page, arm.locale);
      await assertSubjectOnScreen(page, arm.code);

      const withBlock = await controlCensus(page);
      const cells = await page.evaluate(() =>
        Array.from(document.querySelectorAll<HTMLElement>("[data-testid^='access-sort-']")).map((element) => {
          const box = element.getBoundingClientRect();
          const centre = [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
          const atCentre = document.elementFromPoint(centre[0], centre[1]);
          return {
            testid: element.getAttribute("data-testid"),
            box: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
            centre,
            in_viewport: box.y >= 0 && box.bottom <= window.innerHeight,
            above_floor: centre[1] <= 768,
            hit_at_own_centre: !!atCentre && (atCentre === element || element.contains(atCentre)),
          };
        })
      );
      // the same DOM with this order's control taken out of layout: anything reachable only there was
      // pushed out by the control this order added
      await page.addStyleTag({ content: ".cn-access-sort{display:none !important}" });
      await page.waitForTimeout(700);
      const withoutBlock = await controlCensus(page);
      const withoutReachable = new Set(reachable(withoutBlock));
      const withReachable = new Set(reachable(withBlock));
      const lost = [...withoutReachable].filter((key) => !withReachable.has(key));
      const gained = [...withReachable].filter((key) => !withoutReachable.has(key));
      const shot = evidencePath(DIR, `${RUN}_geometry_${w}x${h}_${arm.code}.png`);
      await page.screenshot({ path: shot, fullPage: true });
      write(`sort-geometry-${w}x${h}-${arm.code}`, {
        viewport: [w, h],
        hit_floor_px: HIT_FLOOR,
        cells,
        controls_with_block: withBlock.length,
        controls_without_block: withoutBlock.length,
        reachable_with_block: withReachable.size,
        reachable_without_block: withoutReachable.size,
        lost,
        gained,
        shot,
      });

      for (const tier of [RECENT, DEPARTURE, TITLE]) {
        const cell = cells.find((c) => c.testid === `access-sort-tier-${tier}`);
        expect(cell, node("G1", `${w}x${h}/${arm.code}: the ${tier} button is not in the DOM`)).toBeTruthy();
        expect(cell!.in_viewport, node("G1", `${w}x${h}/${arm.code}: ${tier} is outside the viewport: ${JSON.stringify(cell)}`)).toBe(true);
        expect(cell!.above_floor, node("G1", `${w}x${h}/${arm.code}: ${tier}'s centre is below the ${HIT_FLOOR} px floor: ${JSON.stringify(cell)}`)).toBe(true);
        expect(
          cell!.hit_at_own_centre,
          node("G1", `${w}x${h}/${arm.code}: nothing answers a hit-test at ${tier}'s own centre: ${JSON.stringify(cell)}`),
        ).toBe(true);
      }
      const sentence = cells.find((c) => c.testid === "access-sort-current");
      expect(sentence, node("G1", `${w}x${h}/${arm.code}: no current-tier cell was measured`)).toBeTruthy();
      expect(
        sentence!.in_viewport && sentence!.hit_at_own_centre,
        node("G1", `${w}x${h}/${arm.code}: the tier a traveller is on cannot be read without scrolling: ${JSON.stringify(sentence)}`),
      ).toBe(true);
      expect(lost, node("G1", `${w}x${h}/${arm.code} lost a reachable control to this order's control: ${JSON.stringify({ lost, withBlock: withBlock.length, withoutBlock: withoutBlock.length })}`)).toEqual([]);
    });
  }
}

// ------------------------------------------------------------------ the refusal, over the wire

test("sort-refusal-over-http — an unknown word is answered by name, and not with a list", async ({
  request,
}) => {
  const refused = await request.get("api/v1/journeys?sort=by_mood");
  const body = await refused.json().catch(() => null);
  write("sort-refusal-over-http", { status: refused.status(), body });
  expect(refused.status(), node("R1", `an unknown tier answered ${refused.status()}: ${JSON.stringify(body)}`)).toBe(422);
  expect(Array.isArray(body), node("R1", `an unknown tier was answered with a list: ${JSON.stringify(body)}`)).toBe(false);
  expect(body?.error?.code, node("R1", `error.code was ${JSON.stringify(body?.error)}`)).toBe("VALIDATION_ERROR");
  expect(
    body?.error?.reason_code,
    node("R1", `the refusal did not name itself: ${JSON.stringify(body?.error)}`),
  ).toBe("JOURNEY_SORT_NOT_RECOGNISED");
  expect(body?.error?.details?.sort, node("R1", `the word sent was not echoed: ${JSON.stringify(body?.error?.details)}`)).toBe("by_mood");
  expect(
    body?.error?.details?.supported,
    node("R1", `the supported tiers were not listed: ${JSON.stringify(body?.error?.details)}`),
  ).toEqual([RECENT, DEPARTURE, TITLE]);
});
