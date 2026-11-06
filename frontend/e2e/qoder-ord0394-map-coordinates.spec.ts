/**
 * ORD-0394 -- the map gets its coordinates from the server, on real data, in the system they are stored in.
 *
 * What the order replaced: six consumer sites tested `activity.metadata.longitude/latitude`, a pair the
 * backend has never emitted (0 of 140 activity rows across the two stores this lane opened carry a
 * latitude). So every one of those branches had only ever taken the "no coordinate" path on real data,
 * and a green gate that fed the payload from a fixture hid it. These cells therefore read the shipping
 * route and the painted screen -- never a fixture coordinate.
 *
 *   Z1  every activity in the `/experience` body names a coordinate or names why it cannot, row by row,
 *       with the coordinate system beside the number; a fabricated fallback would fail the distinctness
 *       check, and a dropped row would fail the count;
 *   Z2  on a 390x844 phone viewport against that same real response: the map is present, at least two
 *       markers are painted, at least two stop rows are hit-testable, and selecting works in both
 *       directions (row -> pin, pin -> row).
 *
 * The counter-example for "a place whose coordinate is gone" is not a fixture: `places.latitude` is NOT
 * NULL in the model and in the live schema, so the honest unjoinable state is a place row that is
 * absent while an activity still names it. Z3 deletes that row from this attempt's own `_cmdr<run>.db`
 * through `e2e/tools/ord0394_store_edit.py`, which refuses any other path.
 */
import { expect, test, type Page } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

type Shipped = {
  id: string;
  title: string;
  place_id: string | null;
  state: string | null;
  reason: string | null;
  latitude: number | null;
  longitude: number | null;
  coordinate_system: string | null;
};

async function journeyId(page: Page): Promise<string> {
  /* The id comes from the running instance's own scene answer.  A hard-coded uuid would make the
     cell green only for one seed and red for every honest reset -- the same trap as a fixture
     coordinate. */
  const res = await page.request.get("/api/v1/demo/scene");
  if (res.status() !== 200) {
    throw new Error(`no scene on this instance: /api/v1/demo/scene answered ${res.status()}`);
  }
  const scene = await res.json();
  const id = scene?.journey_id ?? null;
  if (!id) throw new Error(`the scene names no journey: ${JSON.stringify(scene).slice(0, 160)}`);
  console.log(`JOURNEY from the running instance: ${id} demo_mode=${scene?.demo_mode} city=${scene?.city_code ?? "-"}`);
  return String(id);
}

async function readActivities(page: Page): Promise<Shipped[]> {
  const experience = `/api/v1/journeys/${await journeyId(page)}/experience`;
  const res = await page.request.get(experience);
  expect(res.status(), `${experience} must answer, got ${res.status()}`).toBe(200);
  const body = await res.json();
  const activities = body?.active_plan?.activities;
  expect(
    activities,
    `the scene's journey has no active plan in ${experience}: top keys ${Object.keys(body ?? {}).join(",")}`,
  ).toBeTruthy();
  return activities.map((a: any) => ({
    id: String(a.id),
    title: String(a.title),
    place_id: a.place_id ?? null,
    state: a.location?.state ?? null,
    reason: a.location?.reason ?? null,
    latitude: a.location?.latitude ?? null,
    longitude: a.location?.longitude ?? null,
    coordinate_system: a.location?.coordinate_system ?? null,
  }));
}

test("Z1_experience_ships_a_coordinate_for_every_activity_or_names_the_reason - 每条 activity 有坐标或具名理由，坐标带系", async ({
  page,
}) => {
  await page.goto("/");
  const rows = await readActivities(page);
  expect(rows.length, "no activity may be dropped by the join").toBeGreaterThan(1);
  /* Same口径 as the commander's own probe (`cmdr_probes/route_coordinate_probe_r1.py` counts these
     substrings in the body and calls a route that ships none `NO_COORDINATES_ON_ANY_READ_ROUTE`). */
  const wire = await (await page.request.get(`/api/v1/journeys/${await journeyId(page)}/experience`)).text();
  /* Measured per activity, not by counting words in a serialised array: a freshly seeded store also
     puts `latitude`/`crs` inside `metadata_json` (this run's own sample below shows it), so a word
     count would double every row. The claim under test is narrower and exact -- the named
     `location` field is there for every activity, and it holds a number exactly where the row says
     it does. It also corrects this lane's landed judgment: "the backend never emits a coordinate in
     metadata" is true of the demo/reset store the shared preview runs on (0 of 140 rows) and NOT of a
     golden-seeded store, which is precisely how a seed can hide the gap on the shared surface. */
  const parsed = JSON.parse(wire);
  const shipped = parsed.active_plan.activities;
  const withLocationField = shipped.filter((a: any) => a.location && "latitude" in a.location).length;
  const withNumber = shipped.filter((a: any) => typeof a.location?.latitude === "number").length;
  const locatedCount = shipped.filter((a: any) => typeof a.location?.latitude === "number").length;
  const systemNamed = shipped.filter((a: any) => a.location?.latitude != null && a.location?.coordinate_system)
    .length;
  console.log(
    `Z1 wire口径: body_bytes=${wire.length} activities=${shipped.length}` +
      ` location_field_present=${withLocationField} numeric_latitude=${withNumber}` +
      ` coordinate_system_on_numbered=${systemNamed} sample=${JSON.stringify(shipped[0]?.location)}`,
  );
  expect(withLocationField, "every activity row must answer with a `location`, filled or named-empty").toBe(
    shipped.length,
  );
  expect(systemNamed, "a shipped coordinate must say which system it is in").toBe(withNumber);
  expect(withNumber, "the seeded plan must have located activities to ship").toBeGreaterThan(1);
  expect(locatedCount, "the parsed rows and the mapped rows must agree").toBe(rows.filter((r) => r.latitude != null).length);
  for (const row of rows) {
    // Printed per row so a reading that passes is visible, not just a boolean.
    console.log(
      `Z1 ${row.id.slice(0, 8)} place=${row.place_id ?? "NONE"} state=${row.state}` +
        ` reason=${row.reason ?? "-"} lat=${row.latitude} lng=${row.longitude} cs=${row.coordinate_system}`,
    );
    expect(
      ["READY", "HYDRATABLE", "UNKNOWN_RETRYABLE", "UNAVAILABLE"],
      `state must be one of the frozen capability tokens, got ${row.state}`,
    ).toContain(row.state);
    if (row.state === "READY") {
      expect(row.latitude, `READY without a latitude: ${row.id}`).not.toBeNull();
      expect(row.longitude, `READY without a longitude: ${row.id}`).not.toBeNull();
      expect(row.coordinate_system, `a coordinate with no system is a number, not a position: ${row.id}`).toBe(
        "WGS84",
      );
    } else {
      expect(row.reason, `${row.state} must name why, ${row.id}`).toBeTruthy();
    }
  }
  const located = rows.filter((r) => r.latitude != null);
  expect(located.length, "at least two activities must carry a coordinate for the map to be real").toBeGreaterThanOrEqual(
    2,
  );
  /* Fidelity, not a distinctness count: two activities may legitimately stand at the same place.
     What must not happen is the projection inventing a position, so each shipped coordinate is
     compared with the store's own place row for that place_id. */
  const city = (await (await page.request.get(`/api/v1/journeys/${await journeyId(page)}`)).json())?.city_code;
  /* The plain catalog view hides server-owned and structural rows; the places a materialised plan
     stands on are exactly those, so the fidelity check has to ask for the whole table. */
  const placeRes = await page.request.get(
    `/api/v1/places?city_code=${encodeURIComponent(String(city))}&include_structural=true`,
  );
  expect(placeRes.status(), "/api/v1/places must answer so the join can be checked against the row").toBe(200);
  const store: Record<string, { latitude: number; longitude: number }> = {};
  for (const p of await placeRes.json()) store[p.id] = { latitude: p.location.latitude, longitude: p.location.longitude };
  for (const row of located) {
    const held = row.place_id ? store[row.place_id] : undefined;
    expect(held, `${row.id} shipped a coordinate but the city catalog has no place ${row.place_id}`).toBeTruthy();
    expect([row.latitude, row.longitude], `shipped coordinate does not match the place row for ${row.place_id}`).toEqual([
      held!.latitude,
      held!.longitude,
    ]);
  }
  const distinct = new Set(located.map((r) => `${r.latitude},${r.longitude}`));
  expect(distinct.size, `every stop collapsed onto one point: ${[...distinct].join(" | ")}`).toBeGreaterThan(1);
  console.log(`Z1 fidelity: ${located.length} located rows all match their place row; ${distinct.size} distinct points`);
});

test("Z2_phone_arms_paint_and_select_on_real_data - 390x844 里标点与双向选中", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  /* The map is its own surface on the phone, reached from the bottom nav -- a reading taken on "/"
     would be a reading of the Today page, not of the map this order is about. */
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(2500);
  const markers = page.locator(".v9-map-marker");
  expect(await markers.count(), "the map painted fewer than two markers from real data").toBeGreaterThanOrEqual(2);
  const stops = page.locator('[data-testid^="map-stop-"]');
  const stopCount = await stops.count();
  expect(stopCount, "fewer than two stop rows are offered").toBeGreaterThanOrEqual(2);
  /* Requirement 3 asks for these two counts by name, and a passing assertion prints nothing, so the
     numbers have to be said out loud or the cell proves itself to nobody but the runner. */
  console.log(
    `Z2 phone 390x844: panel_visible=${await page.getByTestId("v8-maplibre-panel").first().isVisible()}` +
      ` markers=${await markers.count()} stops=${stopCount}`,
  );

  // forward: a row selects its pin.  A pin carries no "selected" class -- it answers with
  // aria-pressed (MapPanel.tsx), which is also what a screen reader is told.
  const second = stops.nth(1);
  const stopId = (await second.getAttribute("data-testid") ?? "").replace("map-stop-", "");
  await second.click();
  const pressedMarkers = await page.locator('.v9-map-marker[aria-pressed="true"]').count();
  expect(pressedMarkers, `pressing stop row ${stopId} pressed no marker`).toBeGreaterThanOrEqual(1);
  const rows = await readActivities(page);
  const target = rows.find((r) => r.id === stopId);
  expect(target, `stop row ${stopId} is not an activity the server answered with`).toBeTruthy();
  console.log(`Z2 forward row->pin: stop=${stopId.slice(0, 8)} title=${target!.title} lat=${target!.latitude}`);

  // reverse: pressing the pin selects the row back
  await page.locator(".v9-map-marker").first().click({ force: true });
  await page.waitForTimeout(500);
  const pressed = await page
    .locator('[data-testid^="map-stop-"][aria-pressed="true"]')
    .count();
  console.log(`Z2 reverse pin->row: aria-pressed rows = ${pressed}`);
  expect(pressed, "pressing a marker did not select a stop row").toBeGreaterThanOrEqual(1);
});

/**
 * Z3 -- the counter-example on a real store. `places.latitude` is NOT NULL in the model and in the
 * live schema, so "clear a place's coordinate" is not a state this product can hold; the honest
 * unjoinable case is the place row being gone while the activity still names it. The row is deleted
 * from **this attempt's own** `_cmdr<run>.db` by `e2e/tools/ord0394_store_edit.py`, which refuses any
 * other path. No payload is mocked and no fixture coordinate is involved.
 */
test("Z3_a_place_without_a_row_answers_named_and_the_rest_keep_working - 反例：接不上的具名回答，其余行不退化", async ({
  page,
}) => {
  const dbUrl = process.env.CITYNEXUS_DATABASE_URL ?? "";
  expect(dbUrl, "the runner must hand this run its own database URL").toMatch(/^sqlite:\/\/\//);
  const before = await readActivities(page);
  const locatedBefore = before.filter((r) => r.latitude != null);
  const victim = locatedBefore.find((r) => before.filter((o) => o.place_id === r.place_id).length === 1);
  expect(victim, "no activity owns a place exclusively, so the delta would not be measurable").toBeTruthy();
  const v = victim!;
  console.log(`Z3 victim activity=${v.id.slice(0, 8)} place=${v.place_id} title=${v.title}`);

  const edit = spawnSync(
    process.env.CITYNEXUS_PYTHON ?? "python",
    [resolve(HERE, "tools", "ord0394_store_edit.py"), dbUrl, String(v.place_id)],
    { encoding: "utf-8" },
  );
  console.log(`Z3 store edit rc=${edit.status} ${(edit.stdout || "").trim()} ${(edit.stderr || "").trim()}`);
  expect(edit.status, "the store edit refused or failed: " + (edit.stderr || "")).toBe(0);

  const after = await readActivities(page);
  expect(after.length, "the join must not drop a row: the activity is still answered").toBe(before.length);
  const now = after.find((r) => r.id === v.id)!;
  expect(now.state, "an unjoinable activity must say which frozen state it is in").toBe("UNAVAILABLE");
  expect(now.reason, "and name the reason").toBe("COVERAGE_INCOMPLETE");
  expect(now.latitude, "and carry no number").toBeNull();
  const stillLocated = after.filter((r) => r.latitude != null).length;
  expect(stillLocated, "only the victim may lose its coordinate").toBe(locatedBefore.length - 1);
  /* The four fields the contract promises for a place that will not join, printed rather than only
     asserted, so the named placeholder is a reading and not just a green check mark. */
  console.log(
    `Z3 wire answer for the lost place: state=${now.state} reason=${now.reason}` +
      ` latitude=${now.latitude} longitude=${now.longitude} coordinate_system=${now.coordinate_system}`,
  );

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(2500);
  const markers = await page.locator(".v9-map-marker:not(.user)").count();
  const stops = await page.getByTestId(/^map-stop-/).count();
  console.log(`Z3 screen after: markers=${markers} stops=${stops} (was ${locatedBefore.length} located)`);
  expect(stops, "the stop rows must not collapse with the rest of the plan").toBeGreaterThanOrEqual(2);
  expect(
    await page.getByTestId("v8-map-empty").count(),
    "the whole map must not fall into its empty state because one place is gone",
  ).toBe(0);
  // The map cannot stand on the lost place, so exactly that one row leaves the stop list -- but the
  // plan itself must still name the stop, and its own card must answer with the named reason.
  expect(stops, "the stop list lost more than the one place that went away").toBe(locatedBefore.length - 1);
  /* The map cannot stand on the lost place, so exactly that one row leaves the stop list. The plan
     surface must still name the stop -- a row the map cannot pin is not a row to hide. The nav is read
     from the DOM rather than assumed, because a hard-coded item id passes on a desktop layout and times
     out on a phone. */
  expect(stops, "the stop list lost more than the one place that went away").toBe(locatedBefore.length - 1);
  const navIds = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-testid^="nav-"]')).map((el) => el.getAttribute("data-testid")),
  );
  const named: string[] = [];
  for (const nav of navIds.filter((x): x is string => !!x)) {
    await page.getByTestId(nav).click();
    await page.waitForTimeout(900);
    if ((await page.evaluate(() => document.body.innerText)).includes(v.title)) named.push(nav);
  }
  console.log(`Z3 one place gone: surfaces still naming "${v.title}" = ${named.join(",") || "NONE"}`);
  expect(named.length, "the unlocated stop vanished from every surface").toBeGreaterThan(0);

  /* Its own card must answer by name. `ActivityDetail` renders on the trips surface once a stop is
     selected (V8Experience.tsx), so that is where the unjoinable answer is read. */
  const cardHome = named.includes("nav-trips") ? "nav-trips" : named[0];
  await page.getByTestId(cardHome).click();
  await page.waitForTimeout(1000);
  /* Baseline for "does not collapse", read on the bare list: the detail card that opens below adds
     other stops' names to the same text, and a baseline taken with it open would be a bigger number
     than the list can ever reach again. */
  const pageText = await page.evaluate(() => document.body.innerText);
  const listedWithOneGone = after.filter((r) => pageText.includes(r.title)).length;
  console.log(`Z3 ${cardHome} lists ${listedWithOneGone} of ${after.length} stop titles with one place gone`);
  await page.getByText(v.title, { exact: false }).first().click();
  await page.waitForTimeout(1500);
  const card = await page.getByTestId("activity-location").innerText();
  console.log(`Z3 card answer on ${cardHome}: "${card.trim()}"`);
  expect(
    /^(暂无坐标|No coordinates)$/.test(card.trim()),
    `the card must name the unjoinable case, got ${JSON.stringify(card)}`,
  ).toBe(true);

  /* The catalog read is the existence oracle -- the same one Z1 checks fidelity against -- so this
     loop never asks the store to delete a row that is already gone. */
  const cityOf = (await (await page.request.get(`/api/v1/journeys/${await journeyId(page)}`)).json()).city_code;
  const catalog = await (
    await page.request.get(`/api/v1/places?city_code=${encodeURIComponent(String(cityOf))}&include_structural=true`)
  ).json();
  const heldIds = new Set(catalog.map((p: any) => String(p.id)));
  const toDrop = Array.from(new Set(after.map((r) => r.place_id).filter((x): x is string => !!x))).filter((id) =>
    heldIds.has(id),
  );
  console.log(`Z3 dropping ${toDrop.length} of ${after.length} activities' place rows (store holds ${heldIds.size} places)`);
  for (const id of toDrop) {
    const edit = spawnSync(
      process.env.CITYNEXUS_PYTHON ?? "python",
      [resolve(HERE, "tools", "ord0394_store_edit.py"), dbUrl, id],
      { encoding: "utf-8" },
    );
    expect(edit.status, `store edit refused for ${id}: ${edit.stderr}`).toBe(0);
  }
  const emptied = await readActivities(page);
  expect(
    emptied.filter((r) => r.latitude == null).length,
    `every place row the plan names was deleted (${toDrop.length}), so no activity may still claim a coordinate`,
  ).toBe(emptied.length);
  /* A full reload, not an in-app click: the shell holds the payload it fetched, and reading a cached
     screen after a store change would measure nothing. */
  await page.goto("/");
  await page.getByTestId("nav-map").click();
  await page.waitForTimeout(2500);
  /* `.stay` is a stay offer's own pin, which this order does not touch; the claim under test is that
     no *activity* pin survives without a coordinate. */
  const markersAfter = await page.locator(".v9-map-marker:not(.user):not(.stay)").count();
  const stopsAfter = await page.getByTestId(/^map-stop-/).count();
  console.log(
    `Z3 all places gone: empty branch=${await page.getByTestId("v8-map-empty").count()}` +
      ` pins=${markersAfter} stops=${stopsAfter}`,
  );
  /* No stop pin may survive, and the surface that lists the plan must not collapse with it.  The map
     itself keeps its own answer here: captured route geometry still gives it something to centre on
     (its empty branch counts legs, per this file's own docstring), so the assertion is about pins. */
  expect(markersAfter, "activity pins survived with no activity coordinate at all").toBe(0);
  expect(stopsAfter, "the map still offers stop rows for places that are gone").toBe(0);
  await page.getByTestId(cardHome).click();
  await page.waitForTimeout(1200);
  const plan = await page.evaluate(() => document.body.innerText);
  const stillListed = emptied.filter((r) => plan.includes(r.title)).length;
  console.log(
    `Z3 plan surface still lists ${stillListed} of ${emptied.length} stop titles` +
      ` (baseline with one place gone: ${listedWithOneGone})`,
  );
  expect(
    stillListed,
    "the plan surface lost stops the moment the map lost every coordinate",
  ).toBe(listedWithOneGone);
});

