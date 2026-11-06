/**
 * ORD-0126 补项 -- the two halves this lane left open when it signed ①②③.
 *
 * ③ said "any non-2xx must show a named failure at the action".  It shipped, but a 403 was folded
 *    into the 401 sentence: 「这一趟需要重新登录才能读 / 登录之后这一趟还在」.  A 403 is the server
 *    saying *it knows who you are and this step is refused* -- telling that traveller to sign in
 *    again promises a repair that cannot arrive, which is the same defect class as calling a 404 a
 *    network failure (R14-S).  Nodes A and B keep the two refusals apart.
 *
 * ④ said "the journey id shown and the journey id submitted must be the same one".  The submit
 *    paths already take `data.journey.id` rather than re-reading `citynexus.v8.journey_id`, but
 *    nothing measured it -- so the property could rot on the next refactor.  Node C overwrites the
 *    stored reference with a different, well-formed id after the screen has loaded, then reads the
 *    uuid of the request the traveller's own next action produces.
 *
 * All three read rendered text and the browser's own request log; no `data-*` attribute decides a
 * verdict here (the structural fields are asserted *in addition*, because R14 requires them).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-a-refusal-says-what-it-is.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const OTHER_JOURNEY = "77777777-7777-4777-8777-777777777777";

async function seededJourneyId(page: Page): Promise<string> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("seed", "the stage named no journey")).toBe(true);
  return id;
}

/** Boot the shell with a stored journey, letting every request through except the stubbed one. */
async function boot(page: Page, journeyId: string, stub: () => Promise<{ status: number; body: unknown }>) {
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.route("**/api/v1/journeys/*/experience", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const answered = await stub();
    await route.fulfill({
      status: answered.status,
      contentType: "application/json",
      body: JSON.stringify(answered.body),
    });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("failure-view")).toBeVisible({ timeout: 30_000 });
}

/** The server's own refusal shape, as `v8Request` reads it (`{error:{code}}`). */
function refusal(code: string) {
  return { error: { code, message: "refused by the preview guard" } };
}

test("ord0126d-a-403-says-permission-and-not-login — 403 不许被说成「重新登录」", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0126d-a-403-says-permission-and-not-login";
  const journeyId = await seededJourneyId(page);
  await boot(page, journeyId, async () => ({ status: 403, body: refusal("PREVIEW_GUARDED") }));

  const view = page.getByTestId("failure-view");
  const text = (await view.innerText()).replace(/\s+/g, " ");
  expect(
    text,
    node(NODE, `a 403 did not say what a 403 says: "${text.slice(0, 260)}"`),
  ).toContain("这一步这台服务器不允许");
  expect(text, node(NODE, `the refusal did not quote the status back: "${text.slice(0, 200)}"`)).toContain("403");
  expect(
    text,
    node(NODE, `the refusal did not quote the server's own code: "${text.slice(0, 200)}"`),
  ).toContain("PREVIEW_GUARDED");
  // The two sentences a 403 may never borrow.  A bare word ban would miss the point -- the correct
  // sentence *mentions* signing in precisely to say it will not help -- so what is asserted is the
  // exit and the headline the 401 owns.
  expect(
    text,
    node(NODE, `a 403 was given the 401's headline: "${text.slice(0, 260)}"`),
  ).not.toContain("这一趟需要重新登录才能读");
  expect(
    /网络不可用|网络问题所/.test(text),
    node(NODE, `a 403 was rewritten as a network problem: "${text.slice(0, 260)}"`),
  ).toBe(false);
  await expect(
    page.getByTestId("failure-sign-in"),
    node(NODE, "a 403 offered 重新登录 as the thing to do -- a repair that cannot arrive"),
  ).toHaveCount(0);
  // The structural fields must agree with the prose (R14: one classification, four consumers).
  expect(
    await view.getAttribute("data-attribution-reason"),
    node(NODE, "the rendered sentence and the structural reason disagree"),
  ).toBe("FORBIDDEN");
  expect(
    await view.getAttribute("data-recovery-action"),
    node(NODE, "a refusal offered no executable next step"),
  ).toBe("choose-journey");
  const exit = page.getByTestId("failure-choose-journey");
  await expect(exit, node(NODE, "the refusal's exit is not on screen")).toBeVisible();
  await exit.focus();
  expect(
    await page.evaluate(() => document.activeElement?.getAttribute("data-testid")),
    node(NODE, "the refusal's exit cannot take focus"),
  ).toBe("failure-choose-journey");
  writeFileSync(
    evidencePath("ui-overnight/ord0126d-403.json"),
    JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0126 ③ 补", node: NODE, status: 403, serverCode: "PREVIEW_GUARDED", text: text.slice(0, 400) }, null, 2) + "\n",
    "utf-8",
  );
});

test("ord0126d-b-401-still-says-sign-in — 两条拒绝不许塌成一句", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0126d-b-401-still-says-sign-in";
  const journeyId = await seededJourneyId(page);
  await boot(page, journeyId, async () => ({ status: 401, body: refusal("UNAUTHENTICATED") }));

  const view = page.getByTestId("failure-view");
  const text = (await view.innerText()).replace(/\s+/g, " ");
  expect(
    text,
    node(NODE, `a 401 no longer offers the sign-in it actually needs: "${text.slice(0, 260)}"`),
  ).toContain("重新登录");
  expect(
    text,
    node(NODE, `a 401 was folded into the 403 sentence: "${text.slice(0, 260)}"`),
  ).not.toContain("这一步这台服务器不允许");
  expect(
    await view.getAttribute("data-attribution-reason"),
    node(NODE, "the 401's structural reason is not UNAUTHORIZED"),
  ).toBe("UNAUTHORIZED");
});

test("ord0126d-c-the-screen-submits-the-journey-it-shows — 屏上那趟和提交那趟必须同一个", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0126d-c-the-screen-submits-the-journey-it-shows";
  const journeyId = await seededJourneyId(page);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  /* The action measured below is the AI planning sheet's submit: the shell hands it
     `data.journey.id` (`V8Experience.tsx`), so the uuid in its POST is the uuid on screen.
     Two earlier attempts are recorded in the ledger block -- the hub's stay retry never renders on
     a seeded journey that already has options, and no read-only navigation issues a request at all
     once the screen is loaded, so neither could carry the comparison. */
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-heading")).toBeVisible({ timeout: 30_000 });

  /* The device now remembers a *different* trip.  A submit path that re-reads storage would send
     this uuid; one that follows the screen sends the uuid on screen. */
  await page.evaluate((other) => localStorage.setItem("citynexus.v8.journey_id", other), OTHER_JOURNEY);

  const submitted: string[] = [];
  page.on("request", (request) => {
    const match = request.url().match(/\/api\/v1\/journeys\/([0-9a-fA-F-]{36})\//);
    if (match) submitted.push(`${request.method()} ${match[1]}`);
  });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("plan-ai-input").fill("雨天能去的室内文化场所");
  await page.getByTestId("plan-ai-submit").click();
  await expect
    .poll(() => submitted.filter((line) => line.startsWith("POST")).length, {
      message: node(NODE, "the action produced no POST at all, so the comparison would be vacuous"),
      timeout: 20_000,
    })
    .toBeGreaterThan(0);

  const posted = submitted.filter((line) => line.startsWith("POST"));
  const ids = [...new Set(posted.map((line) => line.split(" ")[1]))];
  expect(
    ids,
    node(
      NODE,
      `the screen shows journey ${journeyId} but the traveller's action submitted ${JSON.stringify(ids)} while the device remembered ${OTHER_JOURNEY}: ${JSON.stringify(posted)}`,
    ),
  ).toEqual([journeyId]);
  writeFileSync(
    evidencePath("ui-overnight/ord0126d-id-source.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0126 ④",
        node: NODE,
        journeyOnScreen: journeyId,
        storedReferenceAfterLoad: OTHER_JOURNEY,
        submittedRequests: posted,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
