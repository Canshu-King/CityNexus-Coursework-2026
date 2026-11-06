/**
 * ORD-0407 -- an empty state that names a remedy has to open the door it names.
 *
 * Slice 1 is five cells, no more:
 *   the 今天 / 实时行程 / 地图 map empty state ("这个行程还没有可信的坐标。添加活动地点或开启定位后…"),
 *   the 旅行票夹 stay row ("这趟行程目前还没有可选的住宿。"),
 *   and `live-no-proposal` ("当前没有需要你决定的调整。").
 *
 * C1 measures the shape of the problem on every screen before anything is asserted: the five cells'
 * boxes, the 768 px line (the fixed `v8-mobile-nav` covers 768..844), and where every interactive
 * control on that screen sits.  The same cell runs on the tip and on this lane's head, so the cost of
 * the change -- what moved below the line because of it -- is a diff between two receipts, not a claim.
 * The unlocatable trip is built through public endpoints (`POST /travelers`, `POST /journeys` into a
 * city whose catalogue is empty on this database), so the empty states are the server's own answers --
 * no response body is rewritten anywhere in this file.
 *
 * C2..C7 then judge the copy and the hit-tests:
 *   C2  the coordinates cell names its tier in both languages on all three screens, the false promise
 *       is gone, the clause the attention band quotes is kept, and nothing grew a control;
 *   C3  the NOT_AVAILABLE claim is re-derived from `contracts/openapi.yaml` on this tree, and the other
 *       remedy the old sentence named is tested rather than asserted: the location door is called for
 *       real, and the fields this cell reads are compared before and after;
 *   C4  the票夹 row's action names its destination, is hit-testable, sits above the line, and the press
 *       lands on a screen that says what it is;
 *   C5  the good-news cell carries no control *and* the screen owns reachable controls -- the pair that
 *       makes "no action here" mean "nothing awaits a decision" rather than "you are stuck";
 *   C6  nothing that was reachable on the tip stopped being reachable, and no cell grew more than a
 *       line except the one the order asked to gain a button;
 *   C7  the new copy cannot escape the card it is written in, at 320/360/390/414.
 *
 * Assertions read rendered text, never a data attribute, because a traveller reads the sentence
 * (Q64=①/Q70/Q71).
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0407-empty-state-actions.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";
const HERE = dirname(fileURLToPath(import.meta.url));

/* The first screen ends where the commander measured it: 768 px on a 390x844 phone, the fixed bottom
   bar taking 768..844.  Read from the order's own number, and re-measured per run below. */
const HIT_FLOOR = 768;

const READ_ROUTES: Array<{ route: string; screen: string }> = [
  { route: "hub", screen: "今天" },
  { route: "live", screen: "实时行程" },
  { route: "map", screen: "地图" },
  { route: "bookings", screen: "旅行票夹" },
];

const readings: Record<string, unknown> = {};

function armOf(title: string): string {
  return /^([A-Z]\d)/.exec(title)?.[1] ?? title.slice(0, 2);
}

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const prior = (readings[arm] ?? {}) as Record<string, unknown>;
  const body: Record<string, unknown> = { ...prior };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 700);
  }
  writeFileSync(
    evidencePath(`ord0407/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, ...body }, null, 1),
    "utf-8",
  );
});

async function boot(page: Page, id: string, locale: string, w = 390, h = 844) {
  await page.addInitScript(
    ([jid, loc]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", jid);
      localStorage.setItem("citynexus.locale", loc);
    },
    [id, locale],
  );
  page.setDefaultTimeout(30_000);
  await page.setViewportSize({ width: w, height: h });
}

async function stageJourneyId(page: Page): Promise<string> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("boot", "the stage named no journey, so there is nothing to read"));
  return id;
}

/**
 * A trip this lane names, built only from public product endpoints, in a city whose catalogue is empty
 * on this database -- which is the state the order is about.  Nothing is patched: the traveller has no
 * plan, so there are no located activities and no located stay offers, and the map empty state is the
 * server's own answer rather than a rewritten response body.  What the server said about the trip on
 * creation is written into the receipt so the construction is checkable.
 */
async function createUnlocatableJourney(
  page: Page,
): Promise<{ journey_id: string; server_says: Record<string, unknown> }> {
  const API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;
  const traveler = await (
    await page.request.post(`${API}/api/v1/travelers`, {
      data: { display_name: "N3 ORD-0407 traveller", locale: "zh-CN", timezone: "Asia/Shanghai" },
    })
  ).json();
  const journey = await (
    await page.request.post(`${API}/api/v1/journeys`, {
      data: {
        traveler_id: traveler.id,
        title: "N3-ORD0407 无坐标行程",
        city_code: "CN-XIAMEN",
        timezone: "Asia/Shanghai",
        start_at: "2026-10-09T18:00:00+08:00",
        end_at: "2026-10-11T15:00:00+08:00",
        budget: { amount_minor: 120000, currency: "CNY" },
      },
    })
  ).json();
  const exp = await (
    await page.request.get(`${API}/api/v1/journeys/${journey.id}/experience`)
  ).json();
  return {
    journey_id: journey.id,
    server_says: {
      city_code: journey.city_code,
      has_active_plan: !!exp.active_plan,
      activities_len: (exp.active_plan?.activities ?? []).length,
      options_len: (exp.options ?? []).length,
      offers_len: (exp.offers ?? []).length,
      experience_catalog_state: exp.catalog_state ?? exp.catalog ?? null,
      experience_top_level_keys: Object.keys(exp).sort(),
    },
  };
}

/**
 * Every interactive control on the screen with its box, its centre, whether the centre is inside the
 * hittable region, and whether `elementFromPoint` at that centre returns the control itself or one of
 * its descendants.  Name is the rendered text -- what a traveller would read, not a test id.
 */
async function controlCensus(page: Page) {
  return page.evaluate((floor: number) => {
    const sel = "button, a[href], input, select, textarea, [role=button], [data-testid=pretrip-empty-action]";
    const items = Array.from(document.querySelectorAll(sel));
    const seen: Record<string, number> = {};
    return items
      .map((el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return null;
        const cx = Math.round(r.left + r.width / 2);
        const cy = Math.round(r.top + r.height / 2);
        const hit = document.elementFromPoint(cx, cy);
        const text = (el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 60);
        const testid = el.getAttribute("data-testid");
        const name = text || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "(no label)";
        /* Most controls on these screens carry no test id, so identity is (name, n-th of that name in
           document order).  Matching on test id alone made every unlabelled control look "gone", which
           is how the first C6 run reported 38 losses on a screen where nothing had moved. */
        const bare = testid ?? name;
        seen[bare] = (seen[bare] ?? 0) + 1;
        const key = testid ?? `${name}#${seen[bare]}`;
        return {
          key,
          testid,
          name,
          box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          centre: [cx, cy],
          centre_above_floor: cy <= floor,
          hit_test: hit ? hit === el || el.contains(hit) || hit.contains(el) : false,
          hit_element: hit ? `${hit.tagName}.${hit.className}`.slice(0, 60) : null,
        };
      })
      .filter((x) => x !== null);
  }, HIT_FLOOR);
}

/** The five cells this order is about, wherever they are on the screen. */
async function cellBoxes(page: Page) {
  return page.evaluate(() => {
    const wanted = [
      ["map-empty-body", '[data-testid="v8-map-empty"] p'],
      ["map-empty-panel", '[data-testid="v8-map-empty"]'],
      ["bookings-no-stay", '[data-testid="bookings-no-stay"]'],
      ["bookings-no-stay-sub", '[data-testid="bookings-no-stay"] p'],
      ["live-no-proposal", '[data-testid="live-no-proposal"]'],
    ] as const;
    const out: Record<string, unknown> = {};
    for (const [key, selector] of wanted) {
      const el = document.querySelector(selector);
      if (!el) {
        out[key] = null;
        continue;
      }
      const r = el.getBoundingClientRect();
      out[key] = {
        box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
        bottom: Math.round(r.bottom),
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 120),
        controls_inside: el.querySelectorAll("button, a[href], [role=button]").length,
      };
    }
    out.scroll_y = Math.round(window.scrollY);
    out.document_height = Math.round(document.documentElement.scrollHeight);
    return out;
  });
}

test.describe("ORD-0407 empty states carry the door they name", () => {
  test("C1 the five cells and every control, measured screen by screen", async ({ page }) => {
    const locatedId = await stageJourneyId(page);
    const built = await createUnlocatableJourney(page);
    const screens: Record<string, unknown> = {};
    for (const [condition, id] of [
      ["located_trip", locatedId],
      ["unlocatable_trip", built.journey_id],
    ] as const) {
      for (const { route, screen } of READ_ROUTES) {
        await boot(page, id, "zh-CN");
        await page.goto(`/v8/${route}`, { waitUntil: "load" });
        /* Every screen mounts at least one `v8-*` test id, and React paints after `load`, so waiting on
           a shell class would race the first render. */
        await page.waitForSelector('[data-testid^="v8-"]', { timeout: 20_000 });
        screens[`${condition}/${screen}`] = {
          url: page.url(),
          cells: await cellBoxes(page),
          controls: await controlCensus(page),
        };
        await page.screenshot({ path: evidencePath(`ord0407/${RUN}__C1_${condition}_${route}.png`) });
      }
    }
    readings.C1 = {
      screens,
      hit_floor: HIT_FLOOR,
      unlocatable_journey: built,
      repo_head: process.env.CITYNEXUS_LANE_HEAD ?? "",
    };
    /* Invariant both before and after the change: the three screens really do carry the coordinates
       sentence, on a trip that has no coordinates.  Read the rendered text, not a test id. */
    for (const screen of ["今天", "实时行程", "地图"]) {
      const cells = (screens as any)[`unlocatable_trip/${screen}`].cells;
      expect(
        cells["map-empty-body"],
        node("C1", `${screen} did not show the coordinates empty state on the unlocatable trip`),
      ).not.toBeNull();
      expect(cells["map-empty-body"].text).toContain("这个行程还没有可信的坐标");
    }
    const bookings = (screens as any)["unlocatable_trip/旅行票夹"].cells;
    expect(bookings["bookings-no-stay-sub"].text).toContain("这趟行程目前还没有可选的住宿");
    const live = (screens as any)["unlocatable_trip/实时行程"].cells;
    expect(live["live-no-proposal"].text).toContain("当前没有需要你决定的调整");
  });

  test("C2 the coordinates cell names its own tier, on all three screens and in both languages", async ({
    page,
  }) => {
    const built = await createUnlocatableJourney(page);
    const perLocale: Record<string, unknown> = {};
    for (const locale of ["zh-CN", "en"]) {
      const arms: Record<string, any> = {};
      for (const { route, screen } of READ_ROUTES.slice(0, 3)) {
        await boot(page, built.journey_id, locale);
        await page.goto(`/v8/${route}`, { waitUntil: "load" });
        const panel = page.getByTestId("v8-map-empty");
        await expect(panel, node("C2", `${screen} had no coordinates cell`)).toHaveCount(1);
        const text = (await panel.innerText()).replace(/\s+/g, " ");
        arms[screen] = {
          text,
          /* The promise the order says has no door.  Asserting its absence is the only way to tell a
             rewritten sentence from a sentence with a correction bolted underneath it. */
          promise_gone:
            !text.includes("添加活动地点或开启定位") &&
            !text.includes("地图会显示正确的城市") &&
            !text.includes("Add located activities or enable location"),
          tier_named: text.includes("NOT_AVAILABLE"),
          opening_clause_kept: text.includes(
            locale === "zh-CN" ? "这个行程还没有可信的坐标" : "no trustworthy coordinates yet",
          ),
          controls_inside: await panel.locator("button, a[href], [role=button]").count(),
          box: await panel.boundingBox(),
        };
        expect(arms[screen].promise_gone, node("C2", `${screen}/${locale} still promises a door`)).toBe(true);
        expect(arms[screen].tier_named, node("C2", `${screen}/${locale} never names the tier`)).toBe(true);
        expect(
          arms[screen].opening_clause_kept,
          node("C2", `${screen}/${locale} lost the clause the attention band quotes`),
        ).toBe(true);
        expect(
          arms[screen].controls_inside,
          node("C2", `${screen}/${locale} invented a control for a door that does not exist`),
        ).toBe(0);
        /* Constraint ③: a thing below 768 px on the first screen has not been placed. */
        expect(
          Math.round(arms[screen].box.y + arms[screen].box.height),
          node("C2", `${screen}/${locale} the named tier sits below the 768 px first-screen line`),
        ).toBeLessThanOrEqual(HIT_FLOOR);
      }
      perLocale[locale] = arms;
      await page.screenshot({ path: evidencePath(`ord0407/${RUN}__C2_${locale}.png`) });
    }
    readings.C2 = { per_locale: perLocale };
  });

  test("C3 the NOT_AVAILABLE claim is re-derived from the contract this panel ships with", async ({
    page,
  }) => {
    /* A sentence on screen is a claim about the build. If a later lane opens the door, this cell has to
       go red so the copy is re-decided instead of quietly stale -- the same rule ORD-0384 Y4 uses for an
       anchor line number. */
    const spec = readFileSync(resolve(HERE, "..", "..", "contracts", "openapi.yaml"), "utf-8");
    const writePaths = [...spec.matchAll(/^ {2}(\/[^\s:]+):/gm)].map((m) => m[1]);
    const collectionActivityDoors = writePaths.filter((p) => /\/activities$/.test(p));
    const activityDoors = writePaths.filter((p) => p.includes("/activities/"));
    const c3: Record<string, any> = {
      contract_paths_seen_by_this_cell: writePaths.length,
      collection_level_activity_write_paths: collectionActivityDoors,
      activity_id_scoped_write_paths: activityDoors,
      location_door_exists: writePaths.includes("/journeys/{journey_id}/location"),
    };
    expect(
      collectionActivityDoors,
      node("C3", "a door to create an activity now exists -- re-decide the copy, do not re-quiet this cell"),
    ).toEqual([]);
    for (const p of activityDoors) {
      expect(p, node("C3", `${p} is not id-scoped, so it could add a place`)).toContain("{activity_id}");
    }

    /* The other remedy the old sentence named.  Sharing a position is a real door -- this build has it,
       and the shell wires it (V8Experience.tsx -> POST /journeys/{id}/location).  So the question
       is not whether it works, it is whether it reaches *this* cell, whose inputs are the plan's located
       activities and the stay offers' coordinates (MapPanel.tsx and :113).  The door is called for
       real, over HTTP, against this lane's own journey; then the panel is read again after a reload.
       No product code is stubbed and nothing is patched. */
    const built = await createUnlocatableJourney(page);
    const API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;
    const expBefore = await (await page.request.get(`${API}/api/v1/journeys/${built.journey_id}/experience`)).json();
    const locationRes = await page.request.post(`${API}/api/v1/journeys/${built.journey_id}/location`, {
      data: {
        latitude: 24.45,
        longitude: 118.13,
        accuracy_m: 30,
        captured_at: new Date().toISOString(),
      },
    });
    const expAfter = await (await page.request.get(`${API}/api/v1/journeys/${built.journey_id}/experience`)).json();
    const inputs = (exp: any) => ({
      /* ORD-0394 moved this predicate onto `location` (the server's join off the place row) and away
         from `metadata.longitude/latitude`, which the backend never sent.  Both are read here: the
         field the panel actually uses, and the one the old copy was written against. */
      located_activities_by_location: (exp.active_plan?.activities ?? []).filter(
        (a: any) => a?.location?.longitude != null && a?.location?.latitude != null,
      ).length,
      located_activities_by_metadata: (exp.active_plan?.activities ?? []).filter(
        (a: any) => a?.metadata?.longitude != null && a?.metadata?.latitude != null,
      ).length,
      located_offers: (exp.offers ?? []).filter(
        (o: any) => o?.location?.longitude != null && o?.location?.latitude != null,
      ).length,
      plan_legs: (exp.active_plan?.legs ?? []).length,
    });
    c3.location_door_http_status = locationRes.status();
    c3.experience_inputs_before = inputs(expBefore);
    c3.experience_inputs_after = inputs(expAfter);
    c3.experience_inputs_unchanged =
      JSON.stringify(inputs(expBefore)) === JSON.stringify(inputs(expAfter));
    c3.location_row_returned = locationRes.ok()
      ? ((await locationRes.json()) as { id?: string; source?: string }).source ?? null
      : null;

    await boot(page, built.journey_id, "zh-CN");
    await page.goto("/v8/live", { waitUntil: "load" });
    const panel = page.getByTestId("v8-map-empty");
    await expect(panel, node("C3", "the coordinates cell disappeared, so the premise moved")).toHaveCount(1);
    const before = (await panel.innerText()).replace(/\s+/g, " ");
    c3.share_location_control_present_on_live_without_a_plan =
      (await page.getByTestId("live-update-location").count()) > 0;
    await page.reload({ waitUntil: "load" });
    const after = (await page.getByTestId("v8-map-empty").innerText()).replace(/\s+/g, " ");
    c3.cell_text_unchanged_after_sharing_location = before === after;
    readings.C3 = c3;
    expect(
      c3.location_door_http_status,
      node("C3", "the location door itself did not answer -- that is a different defect and must not hide here"),
    ).toBe(201);
    expect(
      c3.experience_inputs_unchanged,
      node("C3", "sharing a position did change what this cell reads -- then it is a remedy and the copy is wrong"),
    ).toBe(true);
    expect(
      c3.cell_text_unchanged_after_sharing_location,
      node("C3", "the cell's own words changed after a position was shared"),
    ).toBe(true);
  });

  test("C4 the票夹 row opens the stay screen and the press lands there", async ({ page }) => {
    const built = await createUnlocatableJourney(page);
    const arms: Record<string, unknown> = {};
    for (const locale of ["zh-CN", "en"]) {
      await boot(page, built.journey_id, locale);
      await page.goto("/v8/bookings", { waitUntil: "load" });
      const action = page.getByTestId("bookings-no-stay-action");
      await expect(
        action,
        node("C4", `${locale} the 票夹 row still states a condition with no handle`),
      ).toHaveCount(1);
      const label = (await action.innerText()).trim();
      const box = await action.boundingBox();
      const centre = [Math.round(box!.x + box!.width / 2), Math.round(box!.y + box!.height / 2)];
      const hit = await page.evaluate(
        (c) => {
          const el = document.elementFromPoint(c[0], c[1]);
          const own = document.querySelector('[data-testid="bookings-no-stay-action"]');
          return {
            tag: `${el?.tagName}.${el?.className}`.slice(0, 60),
            is_own: !!own && (own === el || own.contains(el)),
          };
        },
        centre as never,
      );
      /* Constraint ①: the English arm is the same tier, same change, and it has to name where it goes. */
      const names_the_screen = locale === "zh-CN" ? label.includes("住宿选择") : /stay explorer/i.test(label);
      expect(names_the_screen, node("C4", `${locale} label does not name its destination: ${label}`)).toBe(true);
      expect(hit.is_own, node("C4", `${locale} centre is covered by ${hit.tag}`)).toBe(true);
      expect(
        Math.round(box!.y + box!.height),
        node("C4", `${locale} the action sits below the 768 px line: ${JSON.stringify(box)}`),
      ).toBeLessThanOrEqual(HIT_FLOOR);

      await action.click();
      await page.waitForURL("**/v8/stay");
      /* Read the destination's own heading rather than the URL: a route that lands on an empty shell is
         not a door. */
      const kicker = await page.locator(".v8-kicker").first().innerText();
      arms[locale] = {
        label,
        box: [Math.round(box!.x), Math.round(box!.y), Math.round(box!.width), Math.round(box!.height)],
        centre,
        hit_element: hit.tag,
        landed_url: page.url(),
        /* Read as rendered: `.v8-kicker` is `text-transform: uppercase`, so innerText comes back as
           "STAY EXPLORER · CAPTURED EVIDENCE".  Comparing case-insensitively keeps the assertion about
           the words a traveller sees, not about the CSS that dresses them. */
        landed_screen_kicker: kicker.replace(/\s+/g, " "),
      };
      expect(kicker.toLowerCase(), node("C4", `${locale} landed on a screen that does not name itself`)).toContain(
        (locale === "zh-CN" ? "住宿选择" : "Stay Explorer").toLowerCase(),
      );
      await page.screenshot({ path: evidencePath(`ord0407/${RUN}__C4_landed_${locale}.png`) });
    }
    readings.C4 = arms;
  });

  test("C5 the good-news cell carries no action on purpose, and the screen does", async ({ page }) => {
    /* The order allows this one to stay action-less but asks for the reason by name.  The reason is
       measurable: on a trip that does have a plan, the Live screen owns real actions above the line, so
       "nothing for you to decide" is not "nothing you can do". */
    const id = await stageJourneyId(page);
    await boot(page, id, "zh-CN");
    await page.goto("/v8/live", { waitUntil: "load" });
    await page.waitForSelector('[data-testid="live-no-proposal"]', { timeout: 20_000 });
    const cell = page.getByTestId("live-no-proposal");
    const box = await cell.boundingBox();
    const controls = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="live-no-proposal"]');
      const row = el?.parentElement ?? null;
      return {
        in_cell: el?.querySelectorAll("button, a[href], [role=button]").length ?? -1,
        in_row: row?.querySelectorAll("button, a[href], [role=button]").length ?? -1,
      };
    });
    const census = await controlCensus(page);
    const liveControlsAbove = census.filter(
      (c: any) => c.hit_test && c.centre[1] <= HIT_FLOOR && String(c.testid ?? "").startsWith("live-"),
    );
    const c5: Record<string, any> = {
      cell_text: (await cell.innerText()).replace(/\s+/g, " "),
      box: [Math.round(box!.x), Math.round(box!.y), Math.round(box!.width), Math.round(box!.height)],
      controls,
      live_controls_above_floor: liveControlsAbove.map((c: any) => ({ testid: c.testid, centre: c.centre })),
      why_no_action:
        "the sentence says nothing awaits a decision, so a button here would offer work the server did not report; " +
        "the screen's own live-* actions are measured above the line in this same reading",
    };
    readings.C5 = c5;
    expect(controls.in_cell, node("C5", "the good-news cell grew a control")).toBe(0);
    expect(
      liveControlsAbove.length,
      node("C5", "no live-owned action reachable above 768 px, so 'no action here' really would be 'no way forward'"),
    ).toBeGreaterThanOrEqual(1);
    for (const forbidden of ["一切正常", "没有问题", "所有检查"]) {
      expect(c5.cell_text, node("C5", "the cell overclaims")).not.toContain(forbidden);
    }
  });

  test("C6 the change costed nothing: no cell grew past one line, no reachable control was pushed out", async ({
    page,
  }) => {
    const baselineFile = resolve(HERE, "..", "..", "review", "n3_0407_evidence_r1", "controls_before_890b7cfc.json");
    const baseline = JSON.parse(readFileSync(baselineFile, "utf-8"));
    const built = await createUnlocatableJourney(page);
    const deltas: Record<string, unknown> = {};
    for (const { route, screen } of READ_ROUTES) {
      await boot(page, built.journey_id, "zh-CN");
      await page.goto(`/v8/${route}`, { waitUntil: "load" });
      await page.waitForSelector('[data-testid^="v8-"]', { timeout: 20_000 });
      const key = `unlocatable_trip/${screen}`;
      const before = baseline.screens[key];
      const expectedCells = Object.keys(before.cells).filter((k) => before.cells[k]);
      /* The first version read the boxes as soon as *any* `v8-*` id existed, so on 今天 the map panel
         had not mounted yet, both cells came back null, and the loop reported "no growth" for that
         screen -- a vacuous pass on the screen the order cares most about.  Wait for the cells the
         baseline says were there, then fail loudly if one is missing instead of skipping it. */
      await expect
        .poll(
          async () => {
            const now = await cellBoxes(page);
            return expectedCells.filter((k) => (now as any)[k]).length;
          },
          { timeout: 15_000, message: node("C6", `${screen} never showed the cells the tip showed`) },
        )
        .toBe(expectedCells.length);
      const cells = await cellBoxes(page);
      const census = await controlCensus(page);
      const growth: Record<string, unknown> = {};
      for (const [name, box] of Object.entries(before.cells as Record<string, number[] | null>)) {
        const now = (cells as any)[name];
        if (!box || !now) continue;
        const delta = now.box[3] - box[3];
        /* One cell is allowed to grow, and it is the one the order asked to gain a control:
           旅行票夹's stay row gets a button, so its card gets taller.  C4 is what proves the taller
           card still ends above the line; everywhere else a growth would be the ORD-0338 cost. */
        const gains_a_control = name === "bookings-no-stay";
        growth[name] = {
          before_height: box[3],
          after_height: now.box[3],
          delta_px: delta,
          expected_to_grow: gains_a_control,
          card_bottom_after: now.bottom,
        };
        if (!gains_a_control) {
          expect(delta, node("C6", `${screen}/${name} grew by more than one line`)).toBeLessThanOrEqual(20);
        } else {
          expect(
            now.bottom,
            node("C6", `${screen}/${name} grew past the 768 px line it has to stay inside`),
          ).toBeLessThanOrEqual(HIT_FLOOR);
        }
      }
      const lost = (before.hittable_above_floor as any[])
        .map((b) => {
          const now = census.find((c: any) => c.key === b.key);
          return !now || !now.hit_test || now.centre[1] > HIT_FLOOR
            ? { key: b.key, name: b.name, before_centre: b.centre, after: now ? now.centre : "gone" }
            : null;
        })
        .filter((x) => x !== null);
      deltas[screen] = { height_delta: growth, controls_no_longer_reachable: lost };
      expect(lost, node("C6", `${screen} lost a control that was reachable on the tip`)).toEqual([]);
    }
    readings.C6 = {
      baseline_head: baseline.tree_head,
      baseline_file: baselineFile.replace(/\\/g, "/"),
      screens: deltas,
    };
  });

  test("C7 no changed sentence escapes the box it belongs to", async ({ page }) => {
    /* ORD-0338's lesson: a long unbroken token pushed real content out of a 229 px band.  The new copy
       carries a Latin uppercase code inside Chinese punctuation, which is exactly the shape that can
       refuse to wrap. */
    const built = await createUnlocatableJourney(page);
    const widths = [320, 360, 390, 414];
    const byWidth: Record<string, unknown> = {};
    for (const w of widths) {
      const rows: Record<string, unknown> = {};
      for (const [route, selector] of [
        ["map", '[data-testid="v8-map-empty"]'],
        ["bookings", '[data-testid="bookings-no-stay"]'],
      ] as const) {
        await boot(page, built.journey_id, "zh-CN", w, 844);
        await page.goto(`/v8/${route}`, { waitUntil: "load" });
        await page.waitForSelector(selector, { timeout: 20_000 });
        rows[route] = await page.evaluate((sel: string) => {
          const host = document.querySelector(sel) as HTMLElement | null;
          if (!host) return null;
          const hb = host.getBoundingClientRect();
          let worst = 0;
          const offenders: string[] = [];
          host.querySelectorAll("*").forEach((n) => {
            const el = n as HTMLElement;
            const b = el.getBoundingClientRect();
            const over = Math.max(b.right - hb.right, hb.left - b.left, el.scrollWidth - el.clientWidth);
            if (over > worst) {
              worst = over;
              offenders.length = 0;
              offenders.push(`${el.tagName}.${el.className}`.slice(0, 50));
            }
          });
          return {
            overflow_px: Math.round(worst),
            worst_elements: offenders,
            box: [hb.left, hb.top, hb.width, hb.height].map(Math.round),
          };
        }, selector as never);
      }
      byWidth[`w${w}`] = rows;
      for (const row of Object.values(rows) as any[]) {
        expect(
          row.overflow_px,
          node("C7", `${w}px: copy escapes its card by ${row.overflow_px}px (${row.worst_elements})`),
        ).toBeLessThanOrEqual(1);
      }
      await page.screenshot({ path: evidencePath(`ord0407/${RUN}__C7_${w}.png`) });
    }
    readings.C7 = { widths, by_width: byWidth };
  });
});
