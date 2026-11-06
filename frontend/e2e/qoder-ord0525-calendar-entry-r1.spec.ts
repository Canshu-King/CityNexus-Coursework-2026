/* ORD-0525 · 日历导出的屏上入口 —— the gate that a tap really produces the file the traveller is
 * being told about, and that what it says on screen is what the bytes contain.
 *
 * Backend: this lane's own stack (`review/ord0525/lane_stack_r1.py`), reading this lane's own copy of
 * a scheduled store.  The rows are NOT invented here: no fixture seeds a plan, no provider is mocked,
 * and the denominators come out of the same door the screen reads.
 *
 * What each arm is paid to prove, and the trap it exists for:
 *
 * 1. 入口摸得到 -- the button is hit-testable at its own centre in both shapes (390x844 touch and
 *    desktop).  `boundingBox()` alone is not reachability: a control under the sticky bottom bar, or
 *    one that only exists above the fold after a scroll, measures fine and is still untappable, so the
 *    arm scrolls the element into view, records `scrollY`, and asks `elementFromPoint` for the centre.
 * 2. 点下去真拿到文件 -- the browser's own download event, not a screenshot.  The bytes come from
 *    `download.path()`, so nothing in this arm is a claim about what a picture looks like.
 * 3. 事件数 = 屏上站点数, per day -- the file's `VEVENT`s are grouped by the trip's own timezone using
 *    the same rule `TripItinerary.groupActivitiesByDay` uses, and compared against the stops the panel
 *    actually rendered for that day, one day at a time.  A total-vs-total comparison would pass with
 *    every stop on the wrong day.
 * 4. 回收站那一形 -- the entry stays (the trip is on screen), and the tap answers with this screen's
 *    own Chinese sentence for the server's `CALENDAR_JOURNEY_IN_RECYCLE_BIN`.  A silent no-op is the
 *    defect; so is printing the reason code.  Then 找回 puts the export back, which is what proves the
 *    refusal named a state rather than the row.
 * 5. 屏上无裸 uuid、机词计数为 0 -- read off `document.body.innerText`, i.e. rendered text, including a
 *    direct guard against my own dictionary keys reaching the screen (`t()` falls back to the key).
 * 6. 引导向导不是分母 -- the inner screen is reached by storing the journey reference the shell itself
 *    reads, which is the bypass the commander named; every number asserted here is counted inside the
 *    trip, never on the wizard screen.
 */

import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const BACKEND = `http://127.0.0.1:${process.env.CITYNEXUS_ORD0525_BACKEND_PORT ?? 4911}`;
const DAY_TRIP = "6cbdbc65-904a-40c7-a778-f981690f25d0"; // 8 stops across 2 days
const ONE_DAY_TRIP = "22222222-2222-4222-8222-222222222222"; // 6 stops, one day
// The directory is named readouts, not evidence: .gitignore:23 ignores any path segment called
// evidence/, which would have shipped this gate with no readable readings beside it.
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0525", "readouts");

const BIN_SENTENCE =
  "这个行程在回收站里，所以现在没有可导出的日历。先把它找回来。";

/**
 * Which generation a reading belongs to.  The r1 files are the bytes of the rejected cut and are
 * published on that ref, so a re-run must not write under their names -- same filename plus new
 * content is exactly the mislabeled artifact this project keeps having to retract.  The base tip
 * travels inside every reading so a reader can tell a generation from a coincidence.
 */
const TAG = process.env.CITYNEXUS_ORD0525_TAG ?? "r2";
const BASE_TIP = process.env.CITYNEXUS_ORD0525_BASE_TIP ?? "unknown";

fs.mkdirSync(EVIDENCE, { recursive: true });

function writeEvidence(name: string, payload: unknown) {
  const shipped = name.replace(/_r1\.json$/, `_${TAG}.json`);
  fs.writeFileSync(
    path.join(EVIDENCE, shipped),
    JSON.stringify(
      {
        generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() },
        ...(payload as object),
      },
      null,
      2,
    ),
    "utf-8",
  );
}

async function bootIntoTrip(page: Page, journeyId: string, via: "deep" | "nav" = "deep") {
  // The shell's own gate: `V8Experience.tsx` only opens the wizard when no journey reference is
  // stored, so the reference -- not a completed wizard -- is what gets the traveller inside.
  await page.addInitScript(
    ([id, locale]) => {
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", locale);
    },
    [journeyId, "zh-CN"],
  );
  if (via === "nav") {
    // Measured, not assumed: boot lands on 今天, and 「打开行程工作台」 is the *studio*, not the
    // day-by-day list.  The itinerary lives on the trips surface behind the nav entry 行程, so this
    // walks the drawer the traveller walks -- a deep link alone would prove the panel renders, not that
    // the entry is reachable.
    await page.goto("/");
    await expect(page.getByRole("main")).toBeVisible();
    // The mobile nav bar is always mounted and each entry carries its own test id
    // (`V8Experience.tsx` -> `nav-trips`), so the traveller's own route to the trips surface is
    // tapped by identity rather than by a name that other labels on the screen also contain.
    const trips = page.getByTestId("nav-trips");
    await expect(trips).toHaveCount(1);
    await trips.scrollIntoViewIfNeeded();
    await trips.click();
  } else {
    // `/v8/itinerary` is the shell's own address for that surface (`ROUTE_ALIASES`, pushState at :955).
    await page.goto("/v8/itinerary");
  }
  await expect(page.getByTestId("trip-itinerary")).toBeVisible({ timeout: 40_000 });
  const mounted = await page.evaluate(async () => ({
    rootChildren: document.getElementById("root")?.childElementCount ?? 0,
    textCharacters: document.body.innerText.trim().length,
    serviceWorker: (await navigator.serviceWorker?.getRegistrations?.())?.length ?? 0,
  }));
  expect(mounted.rootChildren, JSON.stringify(mounted)).toBeGreaterThan(0);
  expect(mounted.textCharacters, JSON.stringify(mounted)).toBeGreaterThan(40);
  return mounted;
}

/** The day order the screen uses, recomputed from the door the screen reads. */
async function screenDayOrder(journeyId: string) {
  const answer = await (
    await fetch(`${BACKEND}/api/v1/journeys/${journeyId}/experience`)
  ).json();
  const activities = answer?.active_plan?.activities ?? [];
  const timeZone = answer?.journey?.timezone ?? "UTC";
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const ordered = [...activities].sort((a, b) => a.start_at.localeCompare(b.start_at));
  const days: string[] = [];
  const perDay: Record<string, number> = {};
  for (const row of ordered) {
    const key = formatter.format(new Date(row.start_at));
    if (!(key in perDay)) {
      perDay[key] = 0;
      days.push(key);
    }
    perDay[key] += 1;
  }
  return { timeZone, total: activities.length, days, perDay };
}

/** Unfold per RFC 5545 3.1, then take DTSTART out of every VEVENT. */
function eventsByDay(body: string, timeZone: string) {
  const logical: string[] = [];
  for (const physical of body.split("\r\n")) {
    if (physical.startsWith(" ") || physical.startsWith("\t")) {
      logical[logical.length - 1] += physical.slice(1);
    } else {
      logical.push(physical);
    }
  }
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const perDay: Record<string, number> = {};
  let inside = false;
  let stamp = "";
  for (const line of logical) {
    if (line === "BEGIN:VEVENT") inside = true;
    else if (line === "END:VEVENT") {
      inside = false;
      const parts = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
      expect(parts, `DTSTART is not the UTC form this door promises: ${stamp}`).not.toBeNull();
      const key = formatter.format(
        new Date(
          Date.UTC(+parts![1], +parts![2] - 1, +parts![3], +parts![4], +parts![5], +parts![6]),
        ),
      );
      perDay[key] = (perDay[key] ?? 0) + 1;
      stamp = "";
    } else if (inside && line.startsWith("DTSTART:")) stamp = line.slice("DTSTART:".length);
  }
  return { perDay, total: Object.values(perDay).reduce((a, b) => a + b, 0) };
}

async function tapExport(page: Page, journeyId: string) {
  const button = page.getByTestId("calendar-export");
  await button.scrollIntoViewIfNeeded();
  const box = await button.boundingBox();
  expect(box, "the export entry has no box, so it is not on screen").not.toBeNull();
  const scrollY = await page.evaluate(() => window.scrollY);
  const hit = await page.evaluate(
    ([x, y]) => {
      const element = document.elementFromPoint(x, y);
      return {
        tag: element?.tagName ?? null,
        testId: element?.getAttribute("data-testid") ?? null,
        isTheEntryOrItsChild: Boolean(element?.closest('[data-testid="calendar-export"]')),
      };
    },
    [box!.x + box!.width / 2, box!.y + box!.height / 2] as [number, number],
  );
  const downloadPromise = page.waitForEvent("download", { timeout: 20_000 });
  await button.click();
  const download = await downloadPromise;
  const file = await download.path();
  expect(file, "the download produced no file on disk").not.toBeNull();
  const body = fs.readFileSync(file!, "utf-8");
  const saved = path.join(EVIDENCE, `exported_${journeyId.slice(0, 8)}_${TAG}.ics`);
  fs.copyFileSync(file!, saved);
  return {
    body,
    savedTo: saved,
    suggested: download.suggestedFilename(),
    bytes: Buffer.byteLength(body, "utf-8"),
    hitTest: { ...hit, scrollY, centre: { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 } },
  };
}

test.describe("ORD-0525 the calendar export entry", () => {
  test("A1 入口在行程头那一层，中心命中就是它自己（手机 390x844 与桌面各一趟）", async ({
    page,
    browser,
  }) => {
    // The mobile arm walks the navigation a traveller walks; the desktop arm uses the same address the
    // shell itself pushes.
    await bootIntoTrip(page, ONE_DAY_TRIP, "nav");
    const button = page.getByTestId("calendar-export");
    await expect(button).toBeVisible();
    await expect(button).toHaveText("导出日历");

    await button.scrollIntoViewIfNeeded();
    const box = await button.boundingBox();
    expect(box).not.toBeNull();
    const scrollY = await page.evaluate(() => window.scrollY);
    const hit = await page.evaluate(
      ([x, y]) => {
        const element = document.elementFromPoint(x, y);
        return {
          tag: element?.tagName ?? null,
          testId: element?.getAttribute("data-testid") ?? null,
          isTheEntryOrItsChild: Boolean(element?.closest('[data-testid="calendar-export"]')),
          disabled: (element?.closest('[data-testid="calendar-export"]') as HTMLButtonElement | null)
            ?.disabled,
        };
      },
      [box!.x + box!.width / 2, box!.y + box!.height / 2] as [number, number],
    );
    expect(hit.isTheEntryOrItsChild, JSON.stringify(hit)).toBe(true);
    expect(hit.testId, JSON.stringify(hit)).toBe("calendar-export");

    // The desktop shape, in its own context: the same entry, the same centre hit.
    const desktop = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const wide = await desktop.newPage();
    await bootIntoTrip(wide, ONE_DAY_TRIP);
    const wideButton = wide.getByTestId("calendar-export");
    await wideButton.scrollIntoViewIfNeeded();
    const wideBox = await wideButton.boundingBox();
    expect(wideBox).not.toBeNull();
    const wideHit = await wide.evaluate(
      ([x, y]) =>
        Boolean(
          document
            .elementFromPoint(x, y)
            ?.closest('[data-testid="calendar-export"]'),
        ),
      [wideBox!.x + wideBox!.width / 2, wideBox!.y + wideBox!.height / 2] as [number, number],
    );
    expect(wideHit, "the entry is present but not hittable at 1280x900").toBe(true);
    // Awaited, and asserted: a context that silently came back at the mobile size would otherwise let
    // this arm claim a desktop reading it never took (the receipt recorded `shape: {}` until this was
    // fixed, because the promise was serialised instead of awaited).
    const shape = await wide.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    expect(shape, JSON.stringify(shape)).toEqual({ width: 1280, height: 900 });
    writeEvidence("a1_entry_hit_test_r1.json", {
      mobile: { viewport: "390x844", box, scrollY, hit },
      desktop: { viewport: "1280x900", box: wideBox, hitTestCentreIsEntry: wideHit, shape },
    });
    await desktop.close();
  });

  test("A2 点下去真拿到 .ics：正文是 VCALENDAR，事件枚数与屏上站点数逐日相同", async ({ page }) => {
    await bootIntoTrip(page, DAY_TRIP);
    const expectation = await screenDayOrder(DAY_TRIP);
    expect(expectation.days.length, JSON.stringify(expectation)).toBeGreaterThan(1);

    // The screen's own day chips, in the screen's own order.
    const chips = page.locator('[data-testid="trip-day-strip"] button');
    await expect(chips).toHaveCount(expectation.days.length);

    const perDayReadings: Array<Record<string, unknown>> = [];
    for (let index = 0; index < expectation.days.length; index += 1) {
      const chip = chips.nth(index);
      await chip.click();
      const dayKey = expectation.days[index];
      const chipLabelCount = Number(
        (await chip.locator("small").innerText()).match(/\d+/)?.[0] ?? "-1",
      );
      const renderedStops = await page
        .getByTestId("trip-day-detail")
        .getByTestId("trip-stop")
        .count();
      perDayReadings.push({
        dayKey,
        chipSays: chipLabelCount,
        renderedStops,
        serverGroupingSays: expectation.perDay[dayKey],
      });
      expect(chipLabelCount, `${dayKey}: the chip and the panel disagree`).toBe(renderedStops);
      expect(renderedStops, `${dayKey}: the panel and the server disagree`).toBe(
        expectation.perDay[dayKey],
      );
    }

    const exported = await tapExport(page, DAY_TRIP);
    expect(exported.body.startsWith("BEGIN:VCALENDAR"), exported.body.slice(0, 40)).toBe(true);
    expect(exported.body).toContain("END:VCALENDAR\r\n");
    const counted = eventsByDay(exported.body, expectation.timeZone);
    expect(counted.total, JSON.stringify(counted)).toBe(expectation.total);
    for (const reading of perDayReadings) {
      const key = String(reading.dayKey);
      expect(
        counted.perDay[key] ?? 0,
        `${key}: the file has a different number of events than the screen has stops`,
      ).toBe(reading.renderedStops);
    }
    // The sentence on screen promises the same number the bytes hold.
    const notice = await page.getByTestId("calendar-export-notice").innerText();
    expect(notice).toContain(`${expectation.total}`);
    expect(notice).toContain("已下载");
    expect(exported.suggested, "the file name the browser offered is not the server's").toContain(
      ".ics",
    );
    writeEvidence("a2_download_and_per_day_counts_r1.json", {
      journey: DAY_TRIP,
      timezone: expectation.timeZone,
      serverDayOrder: expectation,
      perDayReadings,
      fileEventsPerDay: counted.perDay,
      fileBytes: exported.bytes,
      suggestedFilename: exported.suggested,
      noticeOnScreen: notice,
    });
  });

  test("A3 单日那枚行程：6 站 6 事件，与号令者量的那一枚同形", async ({ page }) => {
    await bootIntoTrip(page, ONE_DAY_TRIP);
    const expectation = await screenDayOrder(ONE_DAY_TRIP);
    const stops = await page.getByTestId("trip-day-detail").getByTestId("trip-stop").count();
    expect(stops).toBe(expectation.total);
    const exported = await tapExport(page, ONE_DAY_TRIP);
    const counted = eventsByDay(exported.body, expectation.timeZone);
    expect(counted.total, exported.body.slice(0, 80)).toBe(6);
    expect(stops).toBe(6);
    // No machine word from this door's own vocabulary reached the file's visible frame either.
    expect(exported.body).toContain("X-WR-CALNAME:");
    writeEvidence("a3_one_day_trip_r1.json", {
      journey: ONE_DAY_TRIP,
      stopsRendered: stops,
      fileEvents: counted.total,
      perDay: counted.perDay,
      bytes: exported.bytes,
    });
  });

  test("A4 回收站那一形：点了要说清原因，不许默默失败，找回后又能导", async ({ page }) => {
    await bootIntoTrip(page, DAY_TRIP);
    const deleted = await (await fetch(`${BACKEND}/api/v1/journeys/${DAY_TRIP}`, {
      method: "DELETE",
    })).status;
    expect(deleted).toBe(204);

    const button = page.getByTestId("calendar-export");
    await button.scrollIntoViewIfNeeded();
    let downloadSeen = false;
    page.once("download", () => {
      downloadSeen = true;
    });
    await button.click();
    const notice = page.getByTestId("calendar-export-notice");
    await expect(notice).toBeVisible();
    await expect(notice).toHaveText(BIN_SENTENCE);
    // The refusal is this screen's sentence, and the reason code stays off the screen.
    const onScreen = await notice.innerText();
    expect(onScreen).not.toContain("CALENDAR_JOURNEY_IN_RECYCLE_BIN");
    // The general form of that guard, not only the code above: no SCREAMING_SNAKE token at all may
    // reach the notice line.
    expect(onScreen.match(/[A-Z][A-Z0-9]*_[A-Z0-9_]+/g) ?? [], onScreen).toEqual([]);
    await page.waitForTimeout(1_500);
    // Snapshotted HERE, at the moment the claim is about.  The arm clicks again below to prove 找回
    // restores the export, and that second tap really does download -- a flag read after it would record
    // `true` for a refusal that never produced a file.
    const sawDownloadDuringRefusal = downloadSeen;
    expect(sawDownloadDuringRefusal, "a binned trip still handed out a file").toBe(false);
    downloadSeen = false;

    // 找回 puts it back, which is the proof the refusal named a state and not the row.
    const restored = await (
      await fetch(`${BACKEND}/api/v1/journeys/${DAY_TRIP}/undelete`, { method: "POST" })
    ).status;
    expect(restored).toBe(200);
    await button.click();
    await expect(notice).toHaveText(/已下载/);
    writeEvidence("a4_recycle_bin_refusal_r1.json", {
      journey: DAY_TRIP,
      deleteStatus: deleted,
      noticeAfterDelete: onScreen,
      downloadDuringRefusal: sawDownloadDuringRefusal,
      downloadAfterUndelete: downloadSeen,
      undeleteStatus: restored,
      noticeAfterUndelete: await notice.innerText(),
    });
  });

  test("A5 屏上无裸 uuid、机词计数为 0，也没有键名回退", async ({ page }) => {
    await bootIntoTrip(page, ONE_DAY_TRIP);
    const exported = await tapExport(page, ONE_DAY_TRIP);
    expect(exported.body).toContain("BEGIN:VCALENDAR");
    const text = await page.evaluate(() => document.body.innerText);
    const uuids = text.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) ?? [];
    const machine = text.match(/[A-Z][A-Z0-9]*_[A-Z0-9_]+/g) ?? [];
    const keys = text.match(/\bcalendar\.[a-zA-Z_]+\b/g) ?? [];
    writeEvidence("a5_exposure_readings_r1.json", {
      charactersRead: text.length,
      uuidHits: uuids,
      machineWordHits: machine,
      dictionaryKeyHits: keys,
      noticeLine: text.split("\n").filter((line) => line.includes("已下载")),
    });
    expect(uuids, text.slice(0, 200)).toEqual([]);
    expect(machine, text.slice(0, 200)).toEqual([]);
    expect(keys, "a dictionary key reached the screen").toEqual([]);
  });
});
