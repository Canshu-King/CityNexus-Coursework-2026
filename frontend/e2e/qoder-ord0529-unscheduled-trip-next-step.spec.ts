/**
 * ORD-0529 — the "no itinerary yet" cell has to say what is missing and hand over a way through.
 *
 * Read before this order: arriving on `/v8/trips` with a trip that holds no schedule painted
 * `trip-itinerary-empty`, which was honest and a dead end.  Two real conditions on this lane's own
 * store drive the arms -- a trip in a city the server holds no places for, and a trip whose chosen
 * plan carries no stop on any day -- and neither is manufactured by a stub: both are rows this
 * backend answers from (`seed_0529_r1.py`), read through `GET /api/v1/journeys/{id}/experience` and
 * `GET /api/v1/places?city_code=`.
 *
 *   A1 the empty catalogue is named as a class of fact, and no machine word or identifier reaches it
 *   A2 the cell's primary action is a door: tapping it lands on the import screen, not on a message
 *   A3 a city that does hold places gets the other sentence and the other primary action
 *   A4 "we could not read it", "zero" and "n" are three different answers, never two
 *   A5 a chosen plan with nothing scheduled does not get told it has no chosen plan
 *   A6 nothing here pretends a schedule exists: the day-by-day surface stays absent
 *   A7 on a phone the action is reachable and the centre of it is itself
 *   A8 on a desktop the same two sentences are read, and the door opens the same way
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { evidencePath } from "./_qoderEvidence";
import { catalogueCountSentence } from "../src/features/planning/catalogueReadback";

const ZERO_PLACE_JOURNEY = "1b849dc1-4488-4136-858d-7445dac19f63"; // CN-XIAMEN, 0 places, no plan
const EMPTY_PLAN_JOURNEY = "011a2238-0a45-4840-9405-c28c098662e8"; // CNX-DEMO, 8 places, plan with 0 stops

const MACHINE_WORD = /[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+/;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0529", name + ".json");
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O529GATE " + name + " " + JSON.stringify(body));
}

/** Land on the trip's itinerary tab with this journey as the shell's stored reference. */
async function openTrip(page: Page, journeyId: string) {
  await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
  await page.goto("/v8/trips", { waitUntil: "load" });
  await expect(page.getByTestId("trip-itinerary-empty")).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500); // the catalogue read-back is a request, not a render frame
}

async function cellText(page: Page) {
  const why = await page.getByTestId("trip-itinerary-empty-why").innerText();
  const catalogue = await page.getByTestId("trip-itinerary-empty-catalogue").innerText();
  return { why, catalogue, all: `${why}\n${catalogue}` };
}

test("A1 空目录那格说出缺的是哪一类事实，且无机词与编号上屏", async ({ page }) => {
  await openTrip(page, ZERO_PLACE_JOURNEY);
  const { catalogue, all } = await cellText(page);
  expect(catalogue).toContain("一个地点都还没有");
  expect(MACHINE_WORD.test(all)).toBe(false);
  expect(UUID.test(all)).toBe(false);
  write("A1_empty_catalogue_named", { catalogue, machine_word: false, uuid: false });
});

test("A2 那格的主动作是一扇门：点了就到导入屏，不是又一句话", async ({ page }) => {
  await openTrip(page, ZERO_PLACE_JOURNEY);
  const action = page.getByTestId("trip-itinerary-empty-action");
  await expect(action).toBeVisible();
  expect((await action.innerText()).trim().length).toBeGreaterThan(1);
  await action.click();
  await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 20_000 });
  write("A2_action_opens_the_door", { landed: "poi-import" });
});

test("A3 有地点的城是另一句实话，主动作也就换成另一件能做的事", async ({ page }) => {
  await openTrip(page, EMPTY_PLAN_JOURNEY);
  const { catalogue } = await cellText(page);
  expect(catalogue).toContain("8");
  const primary = page.getByTestId("trip-itinerary-empty-action");
  expect(await primary.innerText()).toContain("AI");
  await expect(page.getByTestId("trip-itinerary-empty-import")).toBeVisible();
  write("A3_city_with_places", { catalogue, primary: await primary.innerText() });
});

test("A4 读不到、零个、n 个是三格答案，不许两格并成一格", async () => {
  const unreadable = catalogueCountSentence({ source: "unreadable", placeCount: null, httpStatus: 503 }, true);
  const zero = catalogueCountSentence({ source: "server", placeCount: 0, httpStatus: 200 }, true);
  const eight = catalogueCountSentence({ source: "server", placeCount: 8, httpStatus: 200 }, true);
  expect(new Set([unreadable, zero, eight]).size).toBe(3);
  expect(zero).not.toContain("没能");
  expect(unreadable).not.toMatch(/\b0\b/);
  expect(eight).toContain("8");
  write("A4_three_distinct_answers", { unreadable, zero, eight });
});

test("A5 有方案却没排上站点，那句不许说成「还没有选定的方案」", async ({ page }) => {
  await openTrip(page, EMPTY_PLAN_JOURNEY);
  const { why } = await cellText(page);
  expect(why).toContain("还没有站点被排在");
  expect(why).not.toContain("还没有选定的方案");
  write("A5_plan_without_a_day", { why });
});

test("A6 这里不假装已经排好：逐日那一屏仍然不在", async ({ page }) => {
  for (const journeyId of [ZERO_PLACE_JOURNEY, EMPTY_PLAN_JOURNEY]) {
    /* Not `openTrip`: that helper waits for the empty cell, so a build that wrongly renders the
       day-by-day surface would fail on the helper instead of on the assertion this arm is about.
       Wait for either cell, then make the day-by-day one carry the verdict. */
    await page.addInitScript((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
    await page.goto("/v8/trips", { waitUntil: "load" });
    await expect(page.getByTestId("trip-itinerary-empty").or(page.getByTestId("trip-itinerary")).first())
      .toBeVisible({ timeout: 25_000 });
    await page.waitForTimeout(1_500);
    await expect(page.getByTestId("trip-itinerary")).toHaveCount(0);
    const text = (await page.getByTestId("trip-itinerary-empty").innerText()).replace(/\s+/g, "");
    for (const successWord of ["已排好", "逐日安排如下", "行程已完成"]) {
      expect(text.includes(successWord)).toBe(false);
    }
  }
  write("A6_no_fake_success", { journeys: 2, success_words: 0 });
});

test("A7 手机上那枚动作滚进视口后，中心命中的就是它自己", async ({ page }) => {
  await openTrip(page, ZERO_PLACE_JOURNEY);
  const action = page.getByTestId("trip-itinerary-empty-action");
  await action.scrollIntoViewIfNeeded();
  const box = await action.boundingBox();
  expect(box).not.toBeNull();
  const hit = await page.evaluate(
    ([x, y]) => {
      const el = document.elementFromPoint(x, y);
      return el ? `${el.tagName}|${el.getAttribute("data-testid") ?? ""}` : "none";
    },
    [box!.x + box!.width / 2, box!.y + box!.height / 2],
  );
  expect(hit.endsWith("|trip-itinerary-empty-action")).toBe(true);
  write("A7_phone_hit", { hit, box });
});

test("A8 桌面那一屏读到同样的两句，门也开在同一处", async ({ page }) => {
  await page.setViewportSize({ width: 1_280, height: 800 });
  await openTrip(page, ZERO_PLACE_JOURNEY);
  const { catalogue, why } = await cellText(page);
  expect(catalogue).toContain("一个地点都还没有");
  expect(why.length).toBeGreaterThan(8);
  await page.getByTestId("trip-itinerary-empty-action").click();
  await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 20_000 });
  write("A8_desktop", { catalogue, why });
});
