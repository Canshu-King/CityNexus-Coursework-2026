import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import {
  FOLD_CONTEXT_DIVERGENT,
  FOLD_DIVERGENT_RANGES,
  foldUntouchableCodePoint,
} from "../src/features/access/journeyOrderVerdict";

/**
 * ORD-0518 -- the trip list says what it is sorted by, in words a traveller reads off the glass.
 *
 * ORD-0477 put three tiers on the door and a control on the screen. Two things were still missing,
 * and both are what this file judges:
 *
 *   1. the sentence on the screen was built from what the screen *asked*, not from what the server
 *      *sent*. Against a door that ignores `sort` -- an older deployment, a proxy that rewrites the
 *      query -- it would have gone on printing "现在按出发日期排。" over a list in some other order.
 *      There is no door-side vouch to read (no response header, and no order field in the row
 *      shape), and asking for one means editing `backend/app/main.py`, `contracts/openapi.yaml` and
 *      `frontend/src/generated/api.ts` while two other orders are mid-flight on those exact files.
 *      So the screen checks the rows it was given, in `features/access/journeyOrderVerdict.ts`, and
 *      says `NOT_VERIFIABLE` by name when a row carries nothing to compare -- which is two of the
 *      four orders, because this door sends no `created_at` and no `updated_at` in a row;
 *   2. the list was a closed `<select>`. Its order lived in the DOM and one line of text was on the
 *      glass, so "the first trip and the last trip" could not be read without opening the control,
 *      and an untitled row fell back to printing a uuid. It is rows now, each one speaking its
 *      trip's name.
 *
 * Arms:
 *   order-control-{loc}      four named orders on the glass (创建序 / 最近更新 / 出发日期 / 名称), one
 *                            in use, and the two orders this door cannot answer said as absent --
 *                            抵达时间序 and 已存序, each with the reason;
 *   order-rows-named-{loc}   first and last row read as trip names for all four orders, and the pair
 *                            swaps when the order swaps (measured from the rows' own innerText);
 *   order-verified-{loc}     the verdict sentence: checked row by row where the rows allow it,
 *                            "cannot check" where they do not, and never a claimed time order
 *                            without the rows behind it;
 *   order-fold-r1            the bound the TITLE check claims for itself: every code point this browser
 *                            folds differently than the server's `casefold` is on the deny-list, and
 *                            every character a trip on this tree is actually named with is checkable;
 *   order-geometry-{w}x{h}-{loc}
 *                            the control and the verdict are on the first screen and hit-testable at
 *                            their own centre, every row can be brought to a hit-testable position,
 *                            and adding them costs this screen no reachable control (a subtraction
 *                            reading on one build).  The line a tap may land above is measured from the
 *                            bar painted at that shape, and asserted to be the frozen 768 px on the
 *                            phone -- see `reachFloor`.
 *
 * Nothing is mocked and no response body is rewritten; assertions read rendered text. One evidence
 * file per arm, written inside the arm, before the judgement.
 */

const FILE = "e2e/qoder-0518-trip-order-said-out-loud.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
const DIR = "ord0518-order";
const HIT_FLOOR = 768;
const SHAPES: Array<[number, number]> = [
  [390, 844],
  [1280, 900],
];

const ARMS = [
  {
    code: "zh",
    locale: "zh-CN",
    words: ["创建序", "最近更新", "出发日期", "名称"],
    checked: "逐行核过",
    unverifiable: "核不了",
    arrival: "抵达时间序",
    saved: "已存序",
    departurePhrase: "出发时间",
    titlePhrase: "行程名",
  },
  {
    code: "en",
    locale: "en",
    words: ["Creation order", "Recently updated", "Departure date", "Name"],
    checked: "Checked row by row",
    unverifiable: "cannot check",
    arrival: "Arrival time",
    saved: "Saved order",
    departurePhrase: "departure time",
    titlePhrase: "trip's name",
  },
] as const;

/** The four orders this screen offers, in the order the ladder lists them. `null` sends no `sort`. */
const CHOICES = ["CREATION_ORDER", "RECENT_UPDATE", "DEPARTURE_DATE", "TITLE"] as const;

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
      /* a profile that refuses localStorage is reported by the arms that need it */
    }
  }, locale);
  await openScreen(page, "/v8/access", ["access-panel"]);
  await expect(
    page.getByTestId("access-panel"),
    node("S0", `${locale}: /v8/access never mounted the panel -- the reading would be of the shell`),
  ).toBeVisible();
}

/** The trip names on the glass, in the order the door put them. Rows, not options. */
async function rowNames(page: Page): Promise<string[]> {
  return page.$$eval("[data-testid='access-trip-row']", (rows) =>
    rows.map((row) => (row.textContent ?? "").trim()),
  );
}

async function orderWords(page: Page): Promise<Array<{ choice: string; word: string; pressed: string | null }>> {
  return page.$$("[data-testid^='access-sort-tier-']").then((handles) =>
    Promise.all(
      handles.map(async (handle) => ({
        choice: (await handle.getAttribute("data-testid"))!.replace("access-sort-tier-", ""),
        word: ((await handle.textContent()) ?? "").trim(),
        pressed: await handle.getAttribute("aria-pressed"),
      })),
    ),
  );
}

async function verdict(
  page: Page,
): Promise<{ state: string | null; text: string; reason: string | null; compared: string | null; blocked: string | null }> {
  const cell = page.getByTestId("access-order-verdict");
  return {
    state: (await cell.getAttribute("data-state").catch(() => null)),
    reason: (await cell.getAttribute("data-reason").catch(() => null)),
    compared: (await cell.getAttribute("data-compared").catch(() => null)),
    blocked: (await cell.getAttribute("data-blocked-code-point").catch(() => null)),
    text: ((await cell.innerText().catch(() => "")) ?? "").trim(),
  };
}

async function pickOrder(page: Page, choice: string): Promise<void> {
  await page.click(`[data-testid='access-sort-tier-${choice}']`);
  await page
    .waitForFunction(
      () => document.querySelectorAll("[data-testid='access-trips-loading']").length === 0,
      undefined,
      { timeout: 20_000 },
    )
    .catch(() => undefined);
  await page.waitForTimeout(1_200);
}

async function seedTraveler(request: APIRequestContext, code: string): Promise<string> {
  const created = await request.post("api/v1/travelers", {
    data: { display_name: `ORD0518-${code} traveller`, locale: "zh-CN", "timezone": "Asia/Shanghai" },
  });
  expect(created.status(), node("S1", `the traveller door answered ${created.status()}`)).toBe(201);
  return (await created.json()).id as string;
}

async function createTrip(
  request: APIRequestContext,
  travelerId: string,
  code: string,
  name: string,
  start: string,
): Promise<string> {
  const end = new Date(new Date(start).getTime() + 2 * 86_400_000).toISOString();
  const created = await request.post("api/v1/journeys", {
    data: {
      traveler_id: travelerId,
      title: `O518-${code} ${name}`,
      city_code: "CN-TIANJIN",
      timezone: "Asia/Shanghai",
      start_at: start,
      end_at: end,
      budget: { amount_minor: 90000, currency: "CNY" },
      planning_mode: "balanced",
    },
  });
  expect(created.status(), node("S1", `creating ${name} -> ${created.status()} ${await created.text()}`)).toBe(201);
  return (await created.json()).id as string;
}

/** Four trips whose departure order, name order and creation order are three different sequences. */
const FIXTURE = [
  { name: "delta", start: "2026-11-06T09:00:00+08:00" },
  { name: "Alpha", start: "2026-11-03T09:00:00+08:00" }, // 2026-11-03T01:00Z, the earliest
  { name: "charlie", start: "2026-11-04T08:00:00+00:00" },
  { name: "北京", start: "2026-11-05T09:00:00+08:00" },
];
const label = (code: string, name: string) => `O518-${code} ${name}`;

// ------------------------------------------------------------------ the four orders, and the two that are not

for (const arm of ARMS) {
  test(`order-control-${arm.code} — four named orders on the glass, and the two this door cannot answer`, async ({
    page,
  }) => {
    await enterAccess(page, arm.locale);
    const words = await orderWords(page);
    const current = ((await page.getByTestId("access-sort-current").innerText()) ?? "").trim();
    const absent = await page.getByTestId("access-sort-absent").innerText().catch(() => "");
    const shot = evidencePath(DIR, `${RUN}_control_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write("order-control-" + arm.code, { words, current, absent, shot });

    expect(
      words.map((row) => row.choice),
      node("C1", `${arm.code}: the offered orders are not the four: ${JSON.stringify(words)}`),
    ).toEqual([...CHOICES]);
    expect(
      words.map((row) => row.word),
      node("C1", `${arm.code}: the buttons do not read ${JSON.stringify(arm.words)}: ${JSON.stringify(words)}`),
    ).toEqual([...arm.words]);
    expect(
      words.filter((row) => row.pressed === "true").map((row) => row.choice),
      node("C1", `${arm.code}: exactly one order may report itself in use: ${JSON.stringify(words)}`),
    ).toEqual(["RECENT_UPDATE"]);
    expect(current, node("C1", `${arm.code}: the order in use is not readable as rendered text: "${current}"`))
      .toContain(arm.words[1]);
    // the two orders the door has no field for are said, with their reasons, on the same screen
    for (const named of [arm.arrival, arm.saved]) {
      expect(absent, node("C1", `${arm.code}: the screen did not name ${named} as an order it cannot offer`))
        .toContain(named);
    }
    expect(absent.length, node("C1", `${arm.code}: the absent-orders block carries no reason`))
      .toBeGreaterThan(arm.arrival.length + arm.saved.length + 10);
    for (const token of ["RECENT_UPDATE", "DEPARTURE_DATE", "TITLE"]) {
      expect(
        words.map((row) => row.word).join(" | "),
        node("C1", `${arm.code}: the machine token ${token} reached a label`),
      ).not.toContain(token);
    }
  });
}

// ------------------------------------------------------------------ first and last row, by name

for (const arm of ARMS) {
  test(`order-rows-named-${arm.code} — first and last row read as trip names, and they swap with the order`, async ({
    page,
    request,
  }) => {
    await enterAccess(page, arm.locale);
    const traveler = await seedTraveler(request, arm.code);
    const ids: string[] = [];
    for (const row of FIXTURE) ids.push(await createTrip(request, traveler, arm.code, row.name, row.start));
    // `RECENT_UPDATE` sorts `journeys.updated_at`, which every create door stamps with the moment it
    // ran. Left alone, the four rows would be newest-first purely because of the clock, and the arm
    // would pass for a reason it cannot point at. Editing one trip through the product's own PATCH
    // door makes that row the most recently touched by an action a traveller could have taken, so the
    // expected order below is a consequence of something this file did. The title is sent back
    // unchanged, so `TITLE` and the row names are untouched.
    const touched = await request.patch(`api/v1/journeys/${ids[0]}`, {
      data: { title: label(arm.code, FIXTURE[0].name) },
    });
    expect(
      touched.status(),
      node("S1", `${arm.code}: the edit door answered ${touched.status()} on ${label(arm.code, FIXTURE[0].name)} -- ${await touched.text()}`),
    ).toBe(200);
    const mine = (names: string[]) => names.filter((text) => FIXTURE.some((row) => text.includes(label(arm.code, row.name))));

    const observed: Record<string, string[]> = {};
    for (const choice of [...CHOICES]) {
      await pickOrder(page, choice);
      observed[choice] = mine(await rowNames(page));
    }
    const expected: Record<string, string[]> = {
      CREATION_ORDER: FIXTURE.map((row) => label(arm.code, row.name)),
      DEPARTURE_DATE: [
        label(arm.code, "Alpha"),
        label(arm.code, "charlie"),
        label(arm.code, "北京"),
        label(arm.code, "delta"),
      ],
      TITLE: [
        label(arm.code, "Alpha"),
        label(arm.code, "charlie"),
        label(arm.code, "delta"),
        label(arm.code, "北京"),
      ],
      RECENT_UPDATE: [
        label(arm.code, "delta"),
        label(arm.code, "北京"),
        label(arm.code, "charlie"),
        label(arm.code, "Alpha"),
      ],
    };
    const ends = Object.fromEntries(
      Object.entries(observed).map(([choice, names]) => [choice, [names[0], names[names.length - 1]]]),
    );
    const shot = evidencePath(DIR, `${RUN}_rows_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write("order-rows-named-" + arm.code, { observed, expected, ends, ids, shot });

    for (const choice of [...CHOICES]) {
      expect(observed[choice], node("R1", `${arm.code}: ${choice} painted ${JSON.stringify(observed[choice])}`))
        .toEqual(expected[choice]);
      // a row that printed a uuid instead of a name would fail the equality above; this pins the
      // other half -- every row must be readable text a traveller could say out loud
      for (const text of observed[choice]) {
        expect(/[0-9a-f]{8}-[0-9a-f]{4}/.test(text), node("R1", `${arm.code}: a row shows an id, not a name: "${text}"`))
          .toBe(false);
      }
    }
    // the pair the commander asked for: change the order, and the first and last trip change with it
    for (const choice of [...CHOICES] as Array<(typeof CHOICES)[number]>) {
      expect(ends[choice], node("R2", `${arm.code}: ${choice} ends are ${JSON.stringify(ends[choice])}, expected ${JSON.stringify([expected[choice][0], expected[choice][3]])}`))
        .toEqual([expected[choice][0], expected[choice][3]]);
    }
    expect(
      new Set(Object.values(ends).map((pair) => pair.join("||"))).size,
      node("R2", `${arm.code}: four orders produced fewer than four distinct first/last pairs: ${JSON.stringify(ends)}`),
    ).toBe(4);
  });
}

// ------------------------------------------------------------------ what the screen may claim

for (const arm of ARMS) {
  test(`order-verified-${arm.code} — checked from the rows where the rows allow it, named where they do not`, async ({
    page,
    request,
  }) => {
    await enterAccess(page, arm.locale);
    const traveler = await seedTraveler(request, arm.code + "v");
    for (const row of FIXTURE) await createTrip(request, traveler, arm.code + "v", row.name, row.start);

    const readings: Record<string, Awaited<ReturnType<typeof verdict>>> = {};
    for (const choice of [...CHOICES]) {
      await pickOrder(page, choice);
      readings[choice] = await verdict(page);
    }
    const shot = evidencePath(DIR, `${RUN}_verified_${arm.code}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    write("order-verified-" + arm.code, { readings, shot });

    // the sentence and the machine name are two different channels: what the traveller reads is the
    // dictionary's, and the stored column goes on the element for the gate
    const EXPECTED_REASON: Record<(typeof CHOICES)[number], { reason: string; compared: string; phrase: string }> = {
      DEPARTURE_DATE: { reason: "compared_start_at", compared: "journeys.start_at", phrase: arm.departurePhrase },
      TITLE: { reason: "compared_title", compared: "journeys.title", phrase: arm.titlePhrase },
      CREATION_ORDER: { reason: "no_created_at_in_row", compared: "journeys.created_at", phrase: "" },
      RECENT_UPDATE: { reason: "no_updated_at_in_row", compared: "journeys.updated_at", phrase: "" },
    };
    for (const choice of [...CHOICES]) {
      const reading = readings[choice];
      const want = EXPECTED_REASON[choice];
      expect(reading.reason, node("V3", `${arm.code}: ${choice} reported reason ${JSON.stringify(reading.reason)}, expected ${JSON.stringify(want.reason)}`))
        .toBe(want.reason);
      expect(reading.compared, node("V3", `${arm.code}: ${choice} does not say which column was compared: ${JSON.stringify(reading)}`))
        .toBe(want.compared);
      if (want.phrase !== "") {
        expect(reading.text, node("V3", `${arm.code}: ${choice} does not name what it compared in words: "${reading.text}"`))
          .toContain(want.phrase);
      }
      for (const machine of ["journeys.", "created_at", "updated_at", "start_at", "U+"]) {
        expect(
          reading.text,
          node("V3", `${arm.code}: ${choice} put the machine word ${JSON.stringify(machine)} on the glass: "${reading.text}"`),
        ).not.toContain(machine);
      }
    }

    for (const choice of ["DEPARTURE_DATE", "TITLE"] as const) {
      expect(readings[choice].state, node("V1", `${arm.code}: ${choice} did not report a row-checked order: ${JSON.stringify(readings)}`))
        .toBe("CONSISTENT");
      expect(readings[choice].text, node("V1", `${arm.code}: the checked verdict is not readable as rendered text: "${readings[choice].text}"`))
        .toContain(arm.checked);
    }
    for (const choice of ["CREATION_ORDER", "RECENT_UPDATE"] as const) {
      expect(readings[choice].state, node("V2", `${arm.code}: ${choice} claimed an order the rows cannot show: ${JSON.stringify(readings)}`))
        .toBe("NOT_VERIFIABLE");
      expect(readings[choice].text, node("V2", `${arm.code}: the screen did not name that it cannot check ${choice}: "${readings[choice].text}"`))
        .toContain(arm.unverifiable);
      // the failure this arm exists for: a time claim with nothing under it
      expect(readings[choice].text, node("V2", `${arm.code}: ${choice} still reads as a claimed time order`))
        .not.toContain(arm.checked);
    }
  });
}

// ------------------------------------------------------------------ the bound the title check claims

/**
 * The `TITLE` verdict is only worth reading if the set of names it refuses to check is the set it
 * *has* to refuse. `journeyOrderVerdict.ts` ships a table of code points and says it came from a
 * measurement; this arm repeats the measurement in the engine that actually paints the panel, with
 * the server's own `casefold` answers as the other column, and compares what the module claims with
 * what the two engines do. A table that was typed by hand, or that drifted with a Unicode update,
 * reddens here instead of quietly making the sentence on the glass a guess.
 */
test("order-fold-r1 — the deny-list the TITLE check ships is the deny-list the two engines differ on", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  // this project loads specs as ES modules, where `__dirname` does not exist (the trap
  // `e2e/qoder-e2e-config-guard.spec.ts` names); `import.meta.url` is what survives the loader
  const fixture = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "review",
    "ord0518",
    "fold_casefold_server_r1.json",
  );
  expect(
    existsSync(fixture),
    node("F0", `the server's own casefold answers are not where this arm reads them: ${fixture}`),
  ).toBe(true);
  const serverFold = JSON.parse(readFileSync(fixture, "utf8")) as Record<string, string>;

  await enterAccess(page, "zh-CN");
  const sweep = await page.evaluate(
    async (answers: Record<string, string>) => {
      const divergent: number[] = [];
      let checked = 0;
      for (let codePoint = 0; codePoint <= 0x10ffff; codePoint += 1) {
        if (codePoint >= 0xd800 && codePoint <= 0xdfff) continue;
        checked += 1;
        const character = String.fromCodePoint(codePoint);
        const here = character.toLowerCase();
        const there = Object.prototype.hasOwnProperty.call(answers, String(codePoint))
          ? answers[String(codePoint)]
          : character;
        if (here !== there) divergent.push(codePoint);
      }
      // the one the per-character sweep cannot see, measured as a whole string
      const sigma = { js: "AΣ".toLowerCase(), sent: "ασ" };
      return { checked, divergent, sigma, engine: navigator.userAgent };
    },
    serverFold,
  );

  const expand = (groups: ReadonlyArray<readonly [number, number]>) => {
    const set = new Set<number>();
    for (const [first, last] of groups) {
      for (let codePoint = first; codePoint <= last; codePoint += 1) set.add(codePoint);
    }
    return set;
  };
  const shipped = expand(FOLD_DIVERGENT_RANGES);
  const measured = new Set(sweep.divergent);
  const onlyShipped = [...shipped].filter((codePoint) => !measured.has(codePoint));
  const onlyMeasured = [...measured].filter((codePoint) => !shipped.has(codePoint));

  // the names this screen really has to order, recorded rather than judged: a refusal here is the
  // guard doing its job over a title that carries a divergent character, so it is not a red on its own
  const door = await request.get("api/v1/journeys?sort=TITLE");
  expect(door.status(), node("F0", `the trip door answered ${door.status()} -- the titles could not be read`)).toBe(200);
  const titles = ((await door.json()) as Array<{ title: string }>).map((row) => row.title);
  const blocked = titles
    .map((title) => ({ title, codePoint: foldUntouchableCodePoint(title) }))
    .filter((row) => row.codePoint !== null);

  // the guard's own boundary, said as cases rather than as a table: names this product uses have to be
  // checkable, and the handful of characters the two engines disagree on have to be refused. A guard
  // that only refuses what no trip is ever named with is doing nothing; one that refuses the trip on
  // the screen is the defect that made this file's `TITLE` arm red once already.
  const CHECKABLE = [
    "Tianjin · stay-aware weekend",
    "北京 三日",
    "São Paulo weekend",
    "İstanbul two nights",
    "Alpha — delta",
    "normal name",
  ];
  const REFUSED = ["straße", "ﬁle list", "ς", "ŉ", "AΣ", "ǰ"];

  const readings = {
    checked_code_points: sweep.checked,
    measured_divergent: sweep.divergent.length,
    shipped_ranges: FOLD_DIVERGENT_RANGES.length,
    shipped_code_points: shipped.size,
    only_in_shipped: onlyShipped,
    only_in_measured: onlyMeasured,
    context_divergent: [...FOLD_CONTEXT_DIVERGENT],
    sigma_in_page: sweep.sigma,
    engine: sweep.engine,
    titles_on_the_door: titles.length,
    titles_the_guard_refuses: blocked,
    checkable_names: CHECKABLE.map((name) => ({ name, code_point: foldUntouchableCodePoint(name) })),
    refused_names: REFUSED.map((name) => ({ name, code_point: foldUntouchableCodePoint(name) })),
  };
  write("order-fold-r1", readings);

  // every non-surrogate code point: U+0000..U+10FFFF is 1_114_112, and U+D800..U+DFFF is 2_048 of them
  expect(sweep.checked, node("F1", `the sweep covered ${sweep.checked} code points, not every non-surrogate one`))
    .toBe(1_114_112 - 2_048);
  expect(onlyShipped, node("F1", `the module refuses ${onlyShipped.length} code point(s) this engine does not: ${JSON.stringify(onlyShipped.slice(0, 12))}`))
    .toEqual([]);
  expect(
    onlyMeasured,
    node("F2", `this engine differs from the server's fold at ${onlyMeasured.length} code point(s) the module does not refuse: ${JSON.stringify(onlyMeasured.slice(0, 12))}`),
  ).toEqual([]);
  // the capital sigma is the case the per-character sweep cannot see, which is why it stands apart
  expect(
    sweep.divergent.includes(0x03a3),
    node("F2", "U+03A3 diverged per character, so the separate context entry is dead weight: check the sweep"),
  ).toBe(false);
  expect(sweep.sigma.js, node("F2", `the page folded "AΣ" to ${JSON.stringify(sweep.sigma.js)}, which is not the server's ${JSON.stringify(sweep.sigma.sent)}`))
    .not.toBe(sweep.sigma.sent);
  // the bound, from both sides: names in use are checkable, and the disagreeing characters are refused
  for (const name of CHECKABLE) {
    expect(
      foldUntouchableCodePoint(name),
      node("F3", `"${name}" is a trip name this screen refused to check -- the guard is wider than the measurement`),
    ).toBeNull();
  }
  for (const name of REFUSED) {
    expect(
      foldUntouchableCodePoint(name),
      node("F3", `"${name}" carries a character the two engines fold differently and the guard let it through`),
    ).not.toBeNull();
  }
});

// ------------------------------------------------------------------ where it all stands

/** One reading of one element at its current scroll position. */
type Cell = {
  key: string;
  box: number[];
  centre: number[];
  in_viewport: boolean;
  above_floor: boolean;
  hit_at_own_centre: boolean;
  reachable: boolean;
  text?: string;
};

/** The line a tap can land on, read off this shape rather than typed in.
 *
 *  `HIT_FLOOR` is the number lane N3 froze for 390x844, where the shell's `.v8-mobile-nav` is fixed
 *  over 768..844. It is a *phone* fact, and pasting it onto a 1280x900 desktop judged rows reachable
 *  by a bar that is `display:none` at that width. So the arm measures the bar that is actually painted
 *  over the bottom of the current shape and uses its top edge; the phone shape then asserts the
 *  measured number is the frozen one, which keeps N3's 口径 instead of quietly replacing it. */
async function reachFloor(page: Page): Promise<{ floor: number; bar: string | null; bar_box: number[] | null }> {
  return page.evaluate(() => {
    const selector = ".v8-mobile-nav";
    const bar = document.querySelector<HTMLElement>(selector);
    const style = bar ? getComputedStyle(bar) : null;
    const painted =
      !!bar &&
      !!style &&
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      bar.getBoundingClientRect().height > 0;
    if (!painted) return { floor: window.innerHeight, bar: null, bar_box: null };
    const box = bar!.getBoundingClientRect();
    return {
      floor: Math.round(box.top),
      bar: selector,
      bar_box: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
    };
  });
}

/** Measure a set of elements, scrolling each one into the reachable band only when it is not already
 *  there. Two passes, deliberately:
 *
 *  1. every element is read where it stands at scroll position 0, so "on the first screen" is one
 *     unambiguous list rather than whatever the last scroll happened to leave visible;
 *  2. anything not reachable there is scrolled to and read again, and that is what gets judged.
 *
 *  Without pass 1 a walk down a long roster looks like three rows needed scrolling, when what actually
 *  happened is that each scroll carried the next few rows into place -- a reading that flatters the
 *  screen and says nothing about it.
 */
async function measureCells(
  page: Page,
  floor: number,
): Promise<{ readings: Cell[]; at_first_screen: string[]; scrolled: string[] }> {
  return page.evaluate(async (floorPx: number) => {
    const frames = () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });
    const read = (element: HTMLElement, key: string): Cell => {
      const box = element.getBoundingClientRect();
      const centre = [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
      const at = document.elementFromPoint(centre[0], centre[1]);
      const in_viewport = box.y >= 0 && box.bottom <= window.innerHeight;
      const above_floor = centre[1] < floorPx;
      const hit_at_own_centre = !!at && (at === element || element.contains(at));
      return {
        key,
        box: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
        centre,
        in_viewport,
        above_floor,
        hit_at_own_centre,
        reachable: in_viewport && above_floor && hit_at_own_centre,
        text: (element.textContent ?? "").trim().slice(0, 80),
      };
    };
    const seen: Record<string, number> = {};
    const targets = Array.from(
      document.querySelectorAll<HTMLElement>(
        "[data-testid^='access-sort-tier-'], [data-testid='access-sort-current'], [data-testid='access-order-verdict'], [data-testid='access-trip-row']",
      ),
    ).map((element) => {
      // the rows all carry one testid, so each reading is named by its position in the roster
      const base = element.getAttribute("data-testid") ?? "";
      const index = (seen[base] = (seen[base] ?? 0) + 1) - 1;
      return { element, key: base === "access-trip-row" ? `${base}-${index}` : base };
    });
    const first = targets.map(({ element, key }) => read(element, key));
    const atFirstScreen = first.filter((cell) => cell.reachable).map((cell) => cell.key);
    const readings: Cell[] = [];
    const scrolled: string[] = [];
    for (let index = 0; index < targets.length; index += 1) {
      const before = first[index];
      if (before.reachable) {
        readings.push(before);
        continue;
      }
      targets[index].element.scrollIntoView({ block: "center", inline: "nearest" });
      await frames();
      const after = read(targets[index].element, before.key);
      after.text = before.text;
      readings.push(after);
      scrolled.push(before.key);
    }
    // back to the top so the census below is taken at the position the frozen 口径 measures at
    window.scrollTo(0, 0);
    await frames();
    return { readings, at_first_screen: atFirstScreen, scrolled };
  }, floor);
}

for (const [w, h] of SHAPES) {
  for (const arm of ARMS) {
    test(`order-geometry-${w}x${h}-${arm.code} — rows, control and verdict are reachable and cost nothing else`, async ({
      page,
      request,
    }) => {
      await page.setViewportSize({ width: w, height: h });
      await enterAccess(page, arm.locale);
      const traveler = await seedTraveler(request, arm.code + "g");
      for (const row of FIXTURE) await createTrip(request, traveler, arm.code + "g", row.name, row.start);
      await pickOrder(page, "DEPARTURE_DATE");

      const bar = await reachFloor(page);
      expect(
        bar.floor,
        node("G0", `${w}x${h}/${arm.code}: the bottom bar's top edge is ${bar.floor} px, not the frozen ${HIT_FLOOR} px this 口径 was set at: ${JSON.stringify(bar)}`),
      ).toBe(w === 390 ? HIT_FLOOR : h);

      const { readings, at_first_screen, scrolled } = await measureCells(page, bar.floor);
      const keyed = Object.fromEntries(readings.map((cell) => [cell.key, cell]));
      const rows = readings.filter((cell) => cell.key.startsWith("access-trip-row"));
      const tiers = readings.filter((cell) => cell.key.startsWith("access-sort-tier-"));

      const census = (visible: boolean) =>
        page.evaluate(
          ({ show, floorPx }: { show: boolean; floorPx: number }) => {
            const block = document.querySelector<HTMLElement>(".cn-access-sort");
            const roster = document.querySelector<HTMLElement>(".cn-access-trips");
            if (block) block.style.display = show ? "" : "none";
            if (roster) roster.style.display = show ? "" : "none";
            const nodes = Array.from(
              document.querySelectorAll<HTMLElement>("button, a[href], input, select, textarea, [role=button]"),
            );
            return nodes
              .map((element) => {
                const box = element.getBoundingClientRect();
                const centre = [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
                const at = document.elementFromPoint(centre[0], centre[1]);
                const testId = element.getAttribute("data-testid");
                const name = (element.getAttribute("aria-label") ?? element.textContent ?? "").trim().slice(0, 40);
                return {
                  key: testId ?? `${name}`,
                  ok:
                    !!at &&
                    (at === element || element.contains(at)) &&
                    centre[1] < floorPx &&
                    box.y >= 0 &&
                    box.bottom <= window.innerHeight,
                };
              })
              .filter((row) => row.ok)
              .map((row) => row.key);
          },
          { show: visible, floorPx: bar.floor },
        );
      const withBlock = await census(true);
      const withoutBlock = await census(false);
      // the subtraction reading, in the direction that means something: a control that was reachable
      // before this order's block existed and is not reachable now has been pushed out by it
      const lost = withoutBlock.filter((key) => !withBlock.includes(key));
      const gained = withBlock.filter((key) => !withoutBlock.includes(key) && !key.startsWith("access-sort-") && !key.startsWith("access-trip-"));
      const texts = Object.fromEntries(
        readings.map((cell) => [cell.key, cell.text ?? ""]),
      );
      const shot = evidencePath(DIR, `${RUN}_geometry_${w}x${h}_${arm.code}.png`);
      await page.screenshot({ path: shot, fullPage: true });
      write("order-geometry-" + w + "x" + h + "-" + arm.code, {
        viewport: [w, h],
        hit_floor_px: HIT_FLOOR,
        measured_floor_px: bar.floor,
        bottom_bar: bar,
        readings,
        on_first_screen: at_first_screen,
        needed_scrolling: scrolled,
        rows_painted: rows.length,
        rows_on_first_screen: at_first_screen.filter((key) => key.startsWith("access-trip-row")).length,
        texts,
        reachable_with: withBlock.length,
        reachable_without: withoutBlock.length,
        lost,
        gained,
        shot,
      });

      expect(
        rows.length,
        node("G1", `${w}x${h}/${arm.code}: the roster painted ${rows.length} rows`),
      ).toBeGreaterThanOrEqual(4);
      for (const cell of readings) {
        expect(
          cell.reachable,
          node(
            "G1",
            `${w}x${h}/${arm.code}: ${cell.key} is not reachable even after being scrolled into view (floor ${bar.floor} px, bar ${JSON.stringify(bar.bar_box)}): ${JSON.stringify(cell)}`,
          ),
        ).toBe(true);
      }
      // what the traveller sees without moving anything: the control, the sentence naming the order in
      // use and the verdict all have to be there, and so has the first trip
      for (const cell of [...tiers, keyed["access-sort-current"], keyed["access-order-verdict"]]) {
        expect(
          cell,
          node("G1", `${w}x${h}/${arm.code}: ${cell ? cell.key : "the control"} was not measured at all`),
        ).toBeTruthy();
        expect(
          at_first_screen.includes(cell!.key),
          node(
            "G1",
            `${w}x${h}/${arm.code}: ${cell!.key} is not on the first screen at all -- it needed scrolling to be tapped: ${JSON.stringify(cell)}`,
          ),
        ).toBe(true);
      }
      expect(
        at_first_screen.includes("access-trip-row-0"),
        node(
          "G1",
          `${w}x${h}/${arm.code}: the list's first trip is below the fold before anything is moved: ${JSON.stringify(
            keyed["access-trip-row-0"],
          )}`,
        ),
      ).toBe(true);
      expect(
        lost,
        node("G1", `${w}x${h}/${arm.code}: this order's control and roster pushed a reachable control out of the first screen: ${JSON.stringify({ lost, withBlock: withBlock.length, withoutBlock: withoutBlock.length })}`),
      ).toEqual([]);
    });
  }
}
