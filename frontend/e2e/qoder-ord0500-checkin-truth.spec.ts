/**
 * ORD-0500 — what the footprint door says back, measured on the painted screen.
 *
 * Three arms, and the order they keep:
 *  1. a refusal is about ONE stop.  Tapping the same stop twice answers
 *     `CHECKIN_DUPLICATE_STAMP` naming the row that already won, and the sentence has to appear
 *     under that stop and nowhere else.  Measured before this lane changed anything: the screen held
 *     ONE notice string and rendered it inside the per-stop loop, so a double-tap painted the same
 *     sentence under all six stops (both at 390x844 and at 1280x800).  That is a fact about one stop
 *     said about six, so this arm counts the notices and reads their owners.
 *  2. a mark exists because the door returned a row.  The count of 已打卡 on the painted screen is
 *     compared with the rows `GET /api/v1/journeys/{id}/checkins` answers, in the same page, at the
 *     same moment.  This is the arm that reddens if the state is ever taken from anywhere else --
 *     it is run against a throwaway bundle whose marks come from the plan's own activity status and
 *     it fails there with three marks over zero rows.
 *  3. no second door is built.  Exactly one 打卡 control per stop on the itinerary, which is the
 *     promise ORD-0476 slice 2 shipped; a fourth control would mean a second state machine.
 *
 * Judgements read RENDERED TEXT. `data-testid` locates structure; it is never the evidence.
 * The base URL comes from `CITYNEXUS_ORD0500_BASE`, which the config refuses unless it points at
 * 127.0.0.1:4xxx, so this file can only ever run against a bundle a lane is serving itself.
 */
import { expect, test } from "@playwright/test";
import { scanScreenText } from "./_qoderTextScan";

const FILE = "e2e/qoder-ord0500-checkin-truth.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const STAMPED_WORD = "已打卡";
const MOMENT_SHAPE = /\d{1,2}月\d{1,2}日/;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

async function seedJourney(page: import("@playwright/test").Page) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(
    8,
  );
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  return journeyId;
}

/** The stop a control belongs to, by the title painted in that row -- never by index arithmetic. */
async function stopWords(page: import("@playwright/test").Page) {
  return await page.evaluate(() => {
    const strip = (x: string | null | undefined) => String(x || "").replace(/\s+/g, " ").trim();
    const rows = Array.from(document.querySelectorAll("[data-testid='trip-stop']"));
    return rows.map((row) => {
      const title = row.querySelector(".trip-stop-body b");
      const control = row.querySelector("[data-testid='stop-checkin']");
      const notice = row.querySelector("[data-testid='stop-checkin-notice']");
      return {
        stop: title ? strip(title.textContent) : "OWNER_UNRESOLVED",
        control: control ? strip(control.textContent) : null,
        notice: notice ? strip(notice.textContent) : null,
        box: control
          ? {
              w: Math.round(control.getBoundingClientRect().width),
              h: Math.round(control.getBoundingClientRect().height),
            }
          : null,
      };
    });
  });
}

async function rowsFromTheDoor(page: import("@playwright/test").Page, journeyId: string) {
  const answer = await page.request.get(`/api/v1/journeys/${journeyId}/checkins`);
  expect(answer.status(), node("ord0500-2", "the footprint read door did not answer 200")).toBe(200);
  return (await answer.json()) as { target_key: string; stamped_at: string; id: string }[];
}

async function settled(page: import("@playwright/test").Page) {
  await expect(
    page.locator("[data-testid='stop-checkin']").first(),
    node("settle", "no stop control ever mounted"),
  ).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(2_500);
}

test("ord0500-1-a-duplicate-refusal-speaks-about-the-stop-that-was-tapped — 拒绝只说被按的那一站", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const journeyId = await seedJourney(page);
  await page.goto("/v8/trips", { waitUntil: "load" });
  await settled(page);

  const before = await stopWords(page);
  const fresh = before.find((r) => r.control && r.control.includes("打卡") &&
    !r.control.includes(STAMPED_WORD));
  expect(fresh, node("ord0500-1", "no unstamped stop with a 打卡 control on the screen")).toBeTruthy();
  const target = before.filter((r) => r.stop === fresh!.stop);
  expect(target.length, node("ord0500-1", "the stop title is not unique on this screen")).toBe(1);

  const control = page.locator(
    "[data-testid='trip-stop']").filter({ hasText: fresh!.stop }
  ).locator("[data-testid='stop-checkin']");
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(4_000);
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(4_000);

  const after = await stopWords(page);
  const withNotice = after.filter((r) => r.notice);
  const scan = await scanScreenText(page);
  /* The reading has to have had something to look at -- an empty scan is not a pass. */
  expect(scan.visualLeaves, node("ord0500-1", "the painted-text scan read no leaves at all"))
    .toBeGreaterThan(10);
  expect(scan.visual.includes(STAMPED_WORD), node("ord0500-1", "the stop shows no 已打卡 after the "
    + "door answered 201, so the sentence below is not about a stamp the screen can see")).toBe(true);
  expect(withNotice.length, node("ord0500-1", `the duplicate sentence was painted under `
    + `${withNotice.length} stops (${withNotice.map((r) => r.stop).join(", ")}); one tap makes a `
    + `statement about one stop`)).toBe(1);
  expect(withNotice[0].stop, node("ord0500-1", "the notice is under the wrong stop")).toBe(fresh!.stop);
  expect(MOMENT_SHAPE.test(String(withNotice[0].notice)), node("ord0500-1",
    `the refusal does not point at the stamp that already won -- the door sends its moment and the `
    + `screen throws it away: ${withNotice[0].notice}`)).toBe(true);
  expect(UUID.test(String(withNotice[0].notice)), node("ord0500-1",
    "an identifier reached the traveller's sentence")).toBe(false);
  expect(journeyId.length > 8 && scan.visual.includes(journeyId), node("ord0500-1",
    "this run's journey id is painted somewhere on the screen")).toBe(false);
});

test("ord0500-2-the-marks-on-screen-are-the-rows-in-the-door — 屏上几枚，门里几行", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const journeyId = await seedJourney(page);
  await page.goto("/v8/trips", { waitUntil: "load" });
  await settled(page);

  const countMarks = async () => {
    const body = await page.evaluate(() => document.body.innerText || "");
    return (body.match(new RegExp(STAMPED_WORD, "g")) || []).length;
  };

  const rowsAtArrival = await rowsFromTheDoor(page, journeyId);
  const marksAtArrival = await countMarks();
  expect(marksAtArrival, node("ord0500-2", `the door answers ${rowsAtArrival.length} footprint row(s) `
    + `while the screen paints ${marksAtArrival} ${STAMPED_WORD} -- a mark may only exist because a `
    + `row does`)).toBe(rowsAtArrival.length);

  const before = await stopWords(page);
  const fresh = before.find((r) => r.control && r.control.includes("打卡") &&
    !r.control.includes(STAMPED_WORD));
  expect(fresh, node("ord0500-2", "no unstamped stop to tap on this screen")).toBeTruthy();
  const control = page.locator("[data-testid='trip-stop']").filter({ hasText: fresh!.stop })
    .locator("[data-testid='stop-checkin']");
  await control.scrollIntoViewIfNeeded();
  await control.click();
  await page.waitForTimeout(4_000);

  const rowsAfterTap = await rowsFromTheDoor(page, journeyId);
  const marksAfterTap = await countMarks();
  expect(rowsAfterTap.length, node("ord0500-2", "the tap did not add a row to the door")).toBe(
    rowsAtArrival.length + 1,
  );
  expect(marksAfterTap, node("ord0500-2", `after a real 201 the door holds ${rowsAfterTap.length} `
    + `rows and the screen paints ${marksAfterTap} marks`)).toBe(rowsAfterTap.length);
  /* The new row is about the stop that was tapped, and it is the door's own key for it -- not a
     second row for a stop that already had one. */
  const keysBefore = new Set(rowsAtArrival.map((r) => r.target_key));
  const added = rowsAfterTap.filter((r) => !keysBefore.has(r.target_key));
  expect(added.length, node("ord0500-2", `the door grew ${added.length} new target_key(s) for one `
    + `tap`)).toBe(1);
  expect(added[0].target_key.startsWith("activity:"), node("ord0500-2",
    `the new row is not keyed on an activity: ${added[0].target_key}`)).toBe(true);
  expect(Boolean(added[0].stamped_at), node("ord0500-2", "the new row carries no server instant"))
    .toBe(true);
});

test("ord0500-3-one-control-per-stop-and-no-second-one-appears — 一站一枚，不长第二套", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await seedJourney(page);
  await page.goto("/v8/trips", { waitUntil: "load" });
  await settled(page);
  const counts = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("[data-testid='trip-stop']"));
    const perRow = rows.map((r) => r.querySelectorAll("[data-testid='stop-checkin']").length);
    return { stops: rows.length, perRow, total: document.querySelectorAll(
      "[data-testid='stop-checkin']").length };
  });
  expect(counts.stops, node("ord0500-3", "no stops on this screen -- the census read nothing"))
    .toBeGreaterThan(0);
  expect(counts.total, node("ord0500-3", `${counts.total} 打卡 controls for ${counts.stops} stops`))
    .toBe(counts.stops);
  expect(counts.perRow.every((n) => n === 1), node("ord0500-3",
    `a stop carries ${counts.perRow.join(",")} controls`)).toBe(true);
  const boxes = (await stopWords(page)).filter((r) => r.box).map((r) => `${r.box!.w}x${r.box!.h}`);
  expect(boxes.length, node("ord0500-3", "no control was measured")).toBe(counts.stops);
});
