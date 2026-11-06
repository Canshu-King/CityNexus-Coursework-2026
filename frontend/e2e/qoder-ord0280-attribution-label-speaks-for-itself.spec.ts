/**
 * ORD-0280 — a sentence this screen composed may not sit under the label 「服务器给出的原因」.
 *
 * `PlanWithAiSheet.tsx` had three refusal legs that each fell back to a client-invented
 * `HTTP <status>` string, and one label that rendered whatever `failureReason` held.  So when the
 * backend answered with no sentence at all, the traveller opened 技术详情 and read OUR guess under a
 * heading that claims the words came from the server.  That is the same trust defect ORD-0262 closed
 * for the copilot ("服务器给出的原因" carrying a self-made `HTTP 500`), in a different sheet, and the
 * commander's 定形 block named all three sites: `:515` agent/commands, `:573` GET /places,
 * `:629` POST /plans/generate (coordinates on `45b0a5e`).
 *
 * The fix is `simulationAdapter.ts`'s existing two-branch model, not a new vocabulary: the
 * sentence carries who said it (`from: "server" | "device"`), and the label is chosen from that.
 *
 * Seven arms, because "one gate covers three sites" is exactly how a partial fix gets shipped green:
 *   A1/A2 agent/commands  - no sentence / server sentence
 *   B1/B2 GET /places     - no sentence / server sentence
 *   C1/C2 plans/generate  - no sentence / server sentence
 *   D     the 4th site this file turned up (`<2 places`): a count this screen measured, which must
 *         read as a device statement even though it contains no `HTTP`.
 * Every arm reads the RENDERED label text and asserts it agrees with `data-reason-origin` in BOTH
 * directions, so a green gate cannot be produced by an attribute that disagrees with the sentence.
 *
 * Also asserted, per site: when the server DID send a sentence, the shown words are that sentence
 * verbatim (equality, not containment), so the fix cannot quietly start preferring our own prose.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0280-attribution-label-speaks-for-itself.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();

const SERVER_LABEL_ZH = "服务器给出的原因";
const SERVER_LABEL_EN = "Reason the server gave";
const USER_SENTENCE = "帮我排三天，喜欢美食和特色景点。";

const json = (status: number, body: unknown) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

/** A refusal with the server's own sentence in the envelope. */
const saidEnvelope = (code: string, message: string) => ({
  error: { code, message, message_key: "request.invalid", details: null },
});
/** A refusal with the status line and nothing else: the shape that used to be dressed up. */
const muteEnvelope = (code: string) => ({ error: { code, message: "", message_key: null, details: null } });

interface Arms {
  commands?: { status: number; body: unknown };
  places?: { status: number; body: unknown };
  generate?: { status: number; body: unknown };
}

/**
 * Boot the hub with a stored journey and stub only the legs an arm names.  `GET /plans` answers `[]`
 * on purpose: a KNOWN-but-empty baseline is what lets the run reach the fallback (the code refuses to
 * attribute new plans when the baseline is unreadable), and it is the server's real shape for "this
 * journey has nothing yet".
 */
async function bootAndAsk(page: Page, arm: Arms, NODE: string) {
  const sceneRes = await page.request.get("/api/v1/demo/scene");
  const scene = (await sceneRes.json()) as { journey_id?: string };
  const journeyId = String(scene.journey_id ?? "");
  expect(journeyId.length > 0, node(NODE, "the stage named no journey")).toBe(true);

  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });

  const seen: string[] = [];
  const route = (match: string, key: keyof Arms, method?: string) =>
    page.route(match, async (r: Route) => {
      if (method && r.request().method() !== method) {
        await r.fallback();
        return;
      }
      seen.push(`${r.request().method()} ${r.request().url().replace(/^https?:\/\/[^/]+/, "")}`);
      const forced = arm[key];
      if (!forced) {
        await r.fallback();
        return;
      }
      await r.fulfill(json(forced.status, forced.body));
    });

  await route("**/api/v1/journeys/*/agent/commands", "commands", "POST");
  await route("**/api/v1/places**", "places");
  await route("**/api/v1/journeys/*/plans/generate", "generate", "POST");

  /* The baseline read must answer KNOWN-and-empty, otherwise the fallback branch never runs and arms
     B/C silently measure arm A. */
  await page.route("**/api/v1/journeys/*/plans", async (r: Route) => {
    if (r.request().method() !== "GET") {
      await r.fallback();
      return;
    }
    seen.push("GET /plans(baseline)");
    await r.fulfill(json(200, []));
  });

  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(NODE, "the hub never rendered")).toBeVisible({
    timeout: 30_000,
  });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet"), node(NODE, "the sheet did not open")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByTestId("plan-ai-input").fill(USER_SENTENCE);
  await page.getByTestId("plan-ai-submit").click();
  const result = page.getByTestId("plan-ai-result");
  await expect(result, node(NODE, "the sheet never answered")).toBeVisible({ timeout: 120_000 });

  await page.getByTestId("plan-ai-detail-toggle").click();
  const kind = page.getByTestId("plan-ai-detail-kind");
  const reason = page.getByTestId("plan-ai-reason");
  await expect(reason, node(NODE, "no failure line rendered under 技术详情 at all")).toBeVisible();
  const out = {
    status: (await result.getAttribute("data-result-status")) ?? "",
    label: clean(await kind.innerText()),
    origin: (await kind.getAttribute("data-reason-origin")) ?? "",
    reason: clean(await reason.innerText()),
    legs: seen,
  };
  return out;
}

/**
 * The judgement every arm shares, as arithmetic on the two readings.
 *
 * `server` arms: label == the attribution label, origin == "server", reason == the server's sentence
 * byte-for-byte.  `device` arms: label != the attribution label (in either language), origin ==
 * "device", and the status reading is still shown -- removing the number was never the point, lying
 * about who said it was.
 */
function judge(
  NODE: string,
  read: { label: string; origin: string; reason: string },
  want: "server" | "device",
  serverSentence?: string,
) {
  const rows = [
    {
      claim: "label and origin agree",
      ok:
        want === "server"
          ? read.label === SERVER_LABEL_ZH && read.origin === "server"
          : read.label !== SERVER_LABEL_ZH &&
            read.label !== SERVER_LABEL_EN &&
            read.origin === "device",
      got: { label: read.label, origin: read.origin },
    },
    {
      claim: "the server's own words are passed through verbatim",
      ok: want !== "server" || read.reason === clean(serverSentence ?? ""),
      got: { reason: read.reason, expected: clean(serverSentence ?? "") },
    },
    {
      claim: "a device-composed reading is still visible (this is not a mute-the-number fix)",
      ok: want !== "device" || read.reason.length > 0,
      got: { reason: read.reason },
    },
  ];
  const bad = rows.filter((r) => !r.ok);
  expect(
    bad,
    node(NODE, `${bad.length ? "FAILED: " + bad.map((b) => b.claim).join("; ") : "ok"} :: ` +
      JSON.stringify({ want, label: read.label, origin: read.origin, reason: read.reason })),
  ).toEqual([]);
  return rows;
}

const receipts: Record<string, unknown> = {};
function remember(arm: string, want: string, read: unknown, rows: unknown) {
  receipts[arm] = { want, read: { ...({} as Record<string, unknown>), ...(read as object) }, rows };
}

test("ord0280-A1 agent/commands refused with NO server sentence does not wear the attribution label", async ({
  page,
}) => {
  const read = await bootAndAsk(page, { commands: { status: 500, body: muteEnvelope("E_INTERNAL") } }, "A1");
  remember("A1", "device", read, judge("A1", read, "device"));
  expect(read.status, node("A1", "the refused leg lost its own status name")).toBe("HTTP_500");
});

test("ord0280-A2 agent/commands refused WITH a server sentence shows it verbatim under the label", async ({
  page,
}) => {
  const sentence = "该目的地的候选目录尚未就绪，请稍后重试。";
  const read = await bootAndAsk(
    page,
    { commands: { status: 503, body: saidEnvelope("CATALOG_NOT_READY", sentence) } },
    "A2",
  );
  remember("A2", "server", read, judge("A2", read, "server", sentence));
});

test("ord0280-B1 GET /places unreadable with NO sentence is named as this device's reading", async ({
  page,
}) => {
  const read = await bootAndAsk(
    page,
    {
      commands: { status: 200, body: { mission: { id: "11111111-1111-4111-8111-111111111111" } } },
      places: { status: 502, body: muteEnvelope("UPSTREAM_UNREADABLE") },
    },
    "B1",
  );
  remember("B1", "device", read, judge("B1", read, "device"));
  expect(read.reason.includes("HTTP"), node("B1", "the status reading vanished with the label")).toBe(true);
});

test("ord0280-B2 GET /places refused with a detail sentence quotes the server", async ({ page }) => {
  const sentence = "places 目录在该城市没有物化任何地点，无法组成候选。";
  const read = await bootAndAsk(
    page,
    {
      commands: { status: 200, body: { mission: { id: "11111111-1111-4111-8111-111111111111" } } },
      places: { status: 409, body: { detail: sentence } },
    },
    "B2",
  );
  remember("B2", "server", read, judge("B2", read, "server", sentence));
});

test("ord0280-C1 plans/generate refused with NO sentence is not the server's reason", async ({ page }) => {
  const read = await bootAndAsk(
    page,
    {
      commands: { status: 200, body: { mission: { id: "11111111-1111-4111-8111-111111111111" } } },
      places: {
        status: 200,
        body: [
          { id: "22222222-2222-4222-8222-222222222222" },
          { id: "33333333-3333-4333-8333-333333333333" },
        ],
      },
      generate: { status: 422, body: muteEnvelope("VALIDATION_ERROR") },
    },
    "C1",
  );
  remember("C1", "device", read, judge("C1", read, "device"));
});

test("ord0280-C2 plans/generate refused with a message quotes it exactly", async ({ page }) => {
  const sentence = "预算与停留上限无法同时满足，未生成方案。";
  const read = await bootAndAsk(
    page,
    {
      commands: { status: 200, body: { mission: { id: "11111111-1111-4111-8111-111111111111" } } },
      places: {
        status: 200,
        body: [
          { id: "22222222-2222-4222-8222-222222222222" },
          { id: "33333333-3333-4333-8333-333333333333" },
        ],
      },
      generate: { status: 422, body: saidEnvelope("BUDGET_INFEASIBLE", sentence) },
    },
    "C2",
  );
  remember("C2", "server", read, judge("C2", read, "server", sentence));
});

test("ord0280-D the <2-places count this screen measured is a device statement too", async ({ page }) => {
  const read = await bootAndAsk(
    page,
    {
      commands: { status: 200, body: { mission: { id: "11111111-1111-4111-8111-111111111111" } } },
      places: { status: 200, body: [] },
    },
    "D",
  );
  remember("D", "device", read, judge("D", read, "device"));
  expect(
    read.reason.includes("places"),
    node("D", "the count this screen measured is no longer on screen: " + read.reason),
  ).toBe(true);
});

test.afterAll(() => {
  writeFileSync(
    evidencePath("ord0280/attribution_label_arms.json"),
    JSON.stringify({ file: FILE, arms: receipts, server_label_zh: SERVER_LABEL_ZH }, null, 1),
    "utf8",
  );
});
