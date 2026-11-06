/**
 * ORD-0253 client half -- the sheet's submission carries one transport identity, and it carries it
 * on BOTH legs.
 *
 * The reason this file exists at all is a measured absence: `PlanWithAiSheet.tsx` used to post
 * `plans/generate` with a body of exactly `{candidate_count: 3}` and headers of exactly
 * Content-Type/Accept.  The durable receipt machinery added by ORD-0272 is alive and keyed on the
 * transport header, so the phone's own press reached the route with no identity at all and left no
 * trace -- "the receipt machine works, this leg never arrives" (HJ-P3's wording, reproduced by the
 * commander's own read of the source line).
 *
 * What is judged here, from the REAL requests rather than from the source:
 *   1. both legs of one accepted press carry an `Idempotency-Key`, and it is the same value --
 *      one submission intent, one key;
 *   2. the value satisfies the server's own contract (8..128 characters after strip, so the header
 *      is not refused before it can do anything);
 *   3. a second deliberate press mints a different key, and within that press both legs agree again;
 *   4. a same-tick burst consumes nothing: the refused press never reaches the mint, so exactly one
 *      key exists and exactly one pair of legs goes out;
 *   5. the first leg's body gains no field: `AgentCommandRequest` is `extra="forbid"`, so an identity
 *      smuggled into that body would be a 422 rather than a fix.
 *
 * Replay/409 semantics are the server's half and are not claimed here.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-sheet-legs-carry-idempotency-key.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
/** The server's own rule, quoted from `commerce/validators.py::validate_idempotency_key`. */
const KEY_MIN = 8;
const KEY_MAX = 128;

type Leg = { url: string; key: string | null; bodyKeys: string[] };

/**
 * The hub with the two legs stubbed so a press runs all the way through: leg 1 answers a completed
 * mission, the catalogue read answers two places (the server's own floor), leg 2 answers an empty
 * batch.  Every request that leaves is recorded as it was actually sent.
 */
async function openSheet(page: Page, leg1: Leg[], leg2: Leg[]) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((id) => {
    if (localStorage.getItem("citynexus.storage-seeded") !== "1") {
      localStorage.clear();
      localStorage.setItem("citynexus.storage-seeded", "1");
    }
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);

  const record = (request: { url: () => string; headers: () => Record<string, string>; postData: () => string | null }, into: Leg[]) => {
    let bodyKeys: string[] = [];
    try {
      const parsed = JSON.parse(request.postData() ?? "{}");
      bodyKeys = Object.keys(parsed).sort();
    } catch {
      bodyKeys = ["<unparseable>"];
    }
    into.push({
      url: request.url(),
      key: request.headers()["idempotency-key"] ?? null,
      bodyKeys,
    });
  };

  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    record(route.request(), leg1);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mission: { id: "11111111-2222-4333-8444-555555555555", status: "COMPLETED", failure_reason: null, reasoning_summary: null },
      }),
    });
  });
  let planReads = 0;
  await page.route("**/api/v1/journeys/*/plans", async (route) => {
    planReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(planReads === 1 ? [] : [{ id: "ord0253-plan-1", status: "DRAFT" }]),
    });
  });
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) => {
    record(route.request(), leg2);
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify([]) });
  });
  await page.route("**/api/v1/places?city_code=*", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { id: "ord0253-place-1", name: "海河游船码头" },
        { id: "ord0253-place-2", name: "天津之眼" },
      ]),
    }),
  );

  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景，别排太满");
  return journeyId;
}

async function settle(page: Page) {
  await expect(page.getByTestId("plan-ai-result"), node("settle", "the result panel never appeared")).toBeVisible({
    timeout: 90_000,
  });
  await expect
    .poll(() => page.getByTestId("plan-ai-cancel").count(), {
      timeout: 60_000,
      message: node("settle", "the sheet stayed busy, so the captured headers are not final"),
    })
    .toBe(0);
}

/** Both legs of one press must exist, agree, and satisfy the server's own length rule. */
function judgeOneIntent(leg1: Leg[], leg2: Leg[], label: string) {
  expect(leg1.length, node("ord0253", `${label}: ${leg1.length} leg-1 POSTs, expected 1`)).toBe(1);
  expect(leg2.length, node("ord0253", `${label}: ${leg2.length} leg-2 POSTs, expected 1 -- the leg the server reads is the one that must carry the key`)).toBe(1);
  const first = leg1[0].key;
  const second = leg2[0].key;
  for (const [which, key] of [["leg 1 /agent/commands", first], ["leg 2 /plans/generate", second]] as const) {
    expect(
      key,
      node("ord0253", `${label}: ${which} carried ${key === null ? "no Idempotency-Key at all" : `"${key}"`}`),
    ).not.toBeNull();
    const value = String(key);
    expect(
      value.length >= KEY_MIN && value.length <= KEY_MAX,
      node("ord0253", `${label}: ${which} key is ${value.length} chars -- outside the server's 8..128 rule`),
    ).toBe(true);
    expect(UUID.test(value), node("ord0253", `${label}: ${which} key "${value}" is not a v4 UUID`)).toBe(true);
  }
  expect(
    first === second,
    node("ord0253", `${label}: the two legs of ONE press disagree (${first} vs ${second}) -- the key is per intent, not per fetch`),
  ).toBe(true);
  /* The identity travels as transport, never smuggled into a body that forbids extras. */
  expect(
    leg1[0].bodyKeys.join(","),
    node("ord0253", `${label}: leg 1's body gained a field (${leg1[0].bodyKeys.join(",")}) -- AgentCommandRequest is extra="forbid"`),
  ).toBe("command");
}

test("ord0253-both-legs-of-one-intent-share-one-key — 一次意图两腿同键，从真请求里读", async ({ page }) => {
  test.setTimeout(300_000);
  const leg1: Leg[] = [];
  const leg2: Leg[] = [];
  await openSheet(page, leg1, leg2);
  await page.getByTestId("plan-ai-submit").click();
  await settle(page);
  judgeOneIntent(leg1, leg2, "press 1");

  writeFileSync(
    evidencePath("ord0253-both-legs-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0253-both-legs",
        leg1: { posts: leg1.length, keyShape: "v4-uuid", bodyKeys: leg1[0]?.bodyKeys ?? [] },
        leg2: { posts: leg2.length, keyShape: "v4-uuid", bodyKeys: leg2[0]?.bodyKeys ?? [] },
        sameKeyAcrossLegs: leg1[0]?.key === leg2[0]?.key,
        keyLength: String(leg1[0]?.key ?? "").length,
        serverContract: `validate_idempotency_key: 8..128 after strip (commerce/validators.py:${KEY_MIN}..${KEY_MAX})`,
        claim: "transport shape only -- replay/409 semantics are the server's half",
      },
      null,
      1,
    ),
  );
});

test("ord0253-each-intent-mints-a-fresh-key — 第二次真按压必须是新键，且两腿仍同键", async ({ page }) => {
  test.setTimeout(300_000);
  const leg1: Leg[] = [];
  const leg2: Leg[] = [];
  await openSheet(page, leg1, leg2);

  await page.getByTestId("plan-ai-submit").click();
  await settle(page);
  /* The first intent is over; the second one starts the way a traveller would do it: close the
     sheet, open it again from the hub, type the ask, press. */
  await page.getByTestId("plan-ai-dismiss").click();
  await expect(page.getByTestId("plan-ai-sheet")).toHaveCount(0);
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("我想两天慢慢逛，晚上看夜景，别排太满");
  await page.getByTestId("plan-ai-submit").click();
  await settle(page);

  judgeOneIntent(leg1.slice(0, 1), leg2.slice(0, 1), "press 1");
  judgeOneIntent(leg1.slice(1), leg2.slice(1), "press 2");
  expect(
    leg1[0].key !== leg1[1].key,
    node("ord0253", `two deliberate presses shared ${leg1[0].key} -- a cached key is not an intent`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ord0253-fresh-per-intent-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0253-each-intent",
        presses: leg1.length,
        distinctKeys: new Set(leg1.map((l) => l.key)).size,
        perPressLegsAgree: [0, 1].map((i) => leg1[i]?.key === leg2[i]?.key),
        note: "distinct across intents, identical within one intent -- no storage, no cooldown",
      },
      null,
      1,
    ),
  );
});

test("ord0253-a-burst-consumes-no-key — 同 tick 的第二下连键都不该领", async ({ page }) => {
  test.setTimeout(300_000);
  const leg1: Leg[] = [];
  const leg2: Leg[] = [];
  await openSheet(page, leg1, leg2);

  /* The pair ORD-0246 established: both clicks arrive before React re-renders, so the synchronous
     guard -- which sits ABOVE the mint -- is what refuses the second one. */
  const disabled = await page.getByTestId("plan-ai-submit").evaluate((el) => {
    const target = el as HTMLButtonElement;
    const before = target.disabled;
    target.click();
    const during = target.disabled;
    target.click();
    return { before, during, after: target.disabled };
  });
  await settle(page);

  expect(leg1.length, node("ord0253-burst", `a same-tick pair sent ${leg1.length} leg-1 POSTs`)).toBe(1);
  expect(leg2.length, node("ord0253-burst", `a same-tick pair sent ${leg2.length} leg-2 POSTs`)).toBe(1);
  expect(
    leg1[0].key === leg2[0].key && UUID.test(String(leg1[0].key)),
    node("ord0253-burst", "the surviving pair did not carry one shared v4 key"),
  ).toBe(true);
  expect(
    disabled.before || disabled.during,
    node("ord0253-burst", "the button was already disabled -- the burst was refused by the render, not by the guard"),
  ).toBe(false);

  writeFileSync(
    evidencePath("ord0253-burst-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0253-burst",
        leg1Posts: leg1.length,
        leg2Posts: leg2.length,
        keysMinted: 1,
        disabledBefore: disabled.before,
        disabledDuring: disabled.during,
        reading: "one physical burst = one intent = one key = one pair of legs",
      },
      null,
      1,
    ),
  );
});
