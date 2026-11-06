/**
 * ORD-0310 · 无键写腿的客户端期限在屏幕上到底说了什么（Q54=① 的形制）
 *
 * 缺陷不是"没超时"这一抽象说法，而是超时之后屏幕被允许说的话。`agent/commands` 这一支不读
 * `Idempotency-Key`（K-2 在 `backend/tests/test_qoder_ord0310_keyless_leg_receipts.py` 里量：带一把
 * 形制正确的键，`idempotency_keys` 仍 +0 行，而 `journey_missions` +3 行），所以期限到期时客户端
 * 唯一能负责的说法是"这笔结果未知、且不能靠同一把键补问"。任何把它写成"已取消/没有发生/再点一次"
 * 的句子，都是本门要红的形状——那正是 ORD-0288 立单时的那句话。
 *
 * 三枚节点，各自自证：
 *   K1  挂死的那一发：期限真的到点（用秒表量，不声明）、屏幕出现"结果未知"与"不能靠同一把键补问"、
 *       渲染出来的秒数 == 常数换算、归因标签必须是"这台设备自己的读数"、技术详情仍在、
 *       禁句缺席、并且**没有第二发 POST**（客户端不许自动补发）。
 *   K2  正控：服务器两秒内正常答完 ⇒ 完全不碰这枚期限（期限不许把好的一端读成未知）。
 *   K3  反控：旅行者自己按"取消" ⇒ 仍是 CANCELLED_AFTER_SEND，绝不许变成 UNKNOWN_*；
 *       这一枚是"期限冒充用户"的杀手：把 wrapper 里那句 firedHere 判定拆掉，K3 就红。
 *
 * 秒数与计时器同源这件事不是靠读代码断言的：K1 同时看屏幕上写的数字和实际等待时长，两者都要对上
 * 常数；把 `KEYLESS_WRITE_DEADLINE_MS` 改小的反突变臂会再各跑一次（数字跟着动、且更早触发）。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";
import { KEYLESS_WRITE_DEADLINE_MS, KEYLESS_WRITE_DEADLINE_SECONDS } from "../src/lib/keylessWriteDeadline";

const FILE = "e2e/qoder-ord0310-keyless-write-deadline.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();

const USER_SENTENCE = "帮我排三天，喜欢美食和特色景点。";
const SERVER_LABEL_ZH = "服务器给出的原因";
/** 这一支被禁止出现的四种说法（每一种都是一次真实的用户伤害，不是措辞洁癖）。 */
const FORBIDDEN = ["已取消", "没有发生", "再试一次", "连不上服务端"];

type ArmId = "K1" | "K2" | "K3";
const readings: Record<string, unknown> = {};

async function openSheet(page: Page, arm: ArmId) {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const journeyId = String(scene.journey_id ?? "");
  expect(journeyId.length > 0, node(arm, "舞台没点出旅程")).toBe(true);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(arm, "首页没渲染")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await page.getByTestId("plan-ai-input").fill(USER_SENTENCE);
  return { journeyId };
}

/** Read the result card the way a traveller sees it: rendered text first, attributes second.
 *  The card is read TWICE on purpose: the first pass is what shows before opening anything, and the
 *  second is after 技术详情 is expanded. A forbidden sentence that only appears inside the detail block
 *  is still a forbidden sentence -- reading the collapsed card alone is how this gate almost passed
 *  an arm that wrote 「这一笔没有发生…再试一次」 into the device note. */
async function readCard(page: Page, arm: ArmId) {
  const result = page.getByTestId("plan-ai-result");
  const collapsedText = clean(await result.innerText());
  const status = (await result.getAttribute("data-result-status")) ?? "";
  const details = page.getByTestId("plan-ai-detail-toggle");
  const blockAttached = (await details.count()) > 0;
  let label = "";
  let origin = "";
  let reason = "";
  if (blockAttached) {
    await details.click();
    label = clean(await page.getByTestId("plan-ai-detail-kind").innerText());
    origin = (await page.getByTestId("plan-ai-detail-kind").getAttribute("data-reason-origin")) ?? "";
    reason = clean(await page.getByTestId("plan-ai-reason").innerText());
  }
  const expandedText = clean(await result.innerText());
  return { status, collapsed_text: collapsedText, card_text: expandedText,
    detail_block_attached: blockAttached, label, origin, reason };
}

async function decide(arm: ArmId, read: Awaited<ReturnType<typeof readCard>>, extra: Record<string, unknown>) {
  const rows: { claim: string; ok: boolean; got: unknown }[] = [];
  const add = (claim: string, ok: boolean, got: unknown) => rows.push({ claim, ok, got });

  if (arm === "K1") {
    const secondsShown = (read.reason.match(/等到\s*(\d+)\s*秒/) ?? [])[1] ?? "";
    add("这一支停在具名的 UNKNOWN_*，不是 NETWORK / CANCELLED",
      read.status === "UNKNOWN_KEYLESS_WRITE_DEADLINE", read.status);
    add("句子里有「结果未知」", read.card_text.includes("结果未知"), read.card_text.slice(0, 80));
    add("句子里明说这不是「没发生」（未知 ≠ 没发生）",
      read.card_text.includes("不是没发生"), read.card_text.slice(0, 80));
    add("句子里明说这一笔不能靠同一把键补问（整句，不是提到键就算）",
      read.card_text.includes("不能靠同一把键补问"), read.card_text.slice(0, 160));
    add("屏幕上那个秒数 == 常数换算（同一个来源）",
      secondsShown === String(KEYLESS_WRITE_DEADLINE_SECONDS), { secondsShown, constant: KEYLESS_WRITE_DEADLINE_SECONDS });
    add("实际等待时长与同一个常数一致（既没提前也没有不触发）",
      (extra.elapsed_ms as number) >= KEYLESS_WRITE_DEADLINE_MS - 1_000
      && (extra.elapsed_ms as number) <= KEYLESS_WRITE_DEADLINE_MS + 20_000, extra.elapsed_ms);
    add("归因标签说的是这台设备，不是「服务器给出的原因」",
      read.origin === "device" && read.label !== SERVER_LABEL_ZH, { label: read.label, origin: read.origin });
    add("技术详情块仍在（不是靠藏起来避免说谎）", read.detail_block_attached, read.detail_block_attached);
    add("四种被禁止的说法一个都没出现",
      FORBIDDEN.every((phrase) => !read.card_text.includes(phrase)),
      FORBIDDEN.filter((phrase) => read.card_text.includes(phrase)));
    add("客户端没有自动补发第二发", (extra.post_count as number) === 1, extra.post_count);
  } else if (arm === "K2") {
    add("服务器按时答完时，这一支不被读成未知",
      read.status !== "UNKNOWN_KEYLESS_WRITE_DEADLINE" && !(read.status.startsWith("UNKNOWN")), read.status);
    add("也没被读成网络问题", read.status !== "NETWORK", read.status);
    add("没有「结果未知」那句话", !read.card_text.includes("结果未知"), read.card_text.slice(0, 80));
  } else {
    add("旅行者自己按的取消仍是取消，不是期限",
      read.status === "CANCELLED_AFTER_SEND", read.status);
    add("取消那一格没有被 UNKNOWN_* 顶替", !read.status.startsWith("UNKNOWN"), read.status);
  }
  const bad = rows.filter((r) => !r.ok);
  readings[arm] = { ...extra, read, verdict: bad.length === 0 ? "MET" : "NOT_MET",
    rows, ...(bad.length ? { failed: bad.map((b) => b.claim) } : {}) };
  expect(bad.map((b) => b.claim), node(arm, JSON.stringify({ status: read.status, rows }))).toEqual([]);
}

test("K1 无键写腿到点：屏幕说结果未知、不能补问，秒数与计时器同源", async ({ page }) => {
  test.setTimeout(KEYLESS_WRITE_DEADLINE_MS + 150_000);
  let posts = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    posts += 1;
    // 永远不答复：这就是"服务器可能还在做"的那一形，不是网络断了。
    await new Promise(() => {});
  });
  await page.route("**/api/v1/journeys/*/plans?*", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });

  await openSheet(page, "K1");
  const started = Date.now();
  await page.getByTestId("plan-ai-submit").click();
  const result = page.getByTestId("plan-ai-result");
  await expect(result, node("K1", "期限到了也没出现结局卡")).toBeVisible({
    timeout: KEYLESS_WRITE_DEADLINE_MS + 60_000,
  });
  const elapsed = Date.now() - started;
  const read = await readCard(page, "K1");
  await decide("K1", read, { elapsed_ms: elapsed, post_count: posts,
    deadline_constant_ms: KEYLESS_WRITE_DEADLINE_MS });
});

test("K2 正控：服务器两秒内答完时，这枚期限不碰正常结局", async ({ page }) => {
  let posts = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    posts += 1;
    await r.fulfill({ status: 500, contentType: "application/json",
      body: JSON.stringify({ error: { message: "这一趟的模型通道没开，没排出方案。",
        message_key: "plan.model_unavailable", code: "plan.model_unavailable", details: null } }) });
  });
  await openSheet(page, "K2");
  const started = Date.now();
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result"), node("K2", "没有结局卡"))
    .toBeVisible({ timeout: 30_000 });
  const read = await readCard(page, "K2");
  await decide("K2", read, { elapsed_ms: Date.now() - started, post_count: posts });
});

test("K3 反控：旅行者按取消时，期限不许冒充用户", async ({ page }) => {
  let posts = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    posts += 1;
    await new Promise(() => {});
  });
  await openSheet(page, "K3");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-cancel"), node("K3", "取消键没出现"))
    .toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-cancel").click();
  await expect(page.getByTestId("plan-ai-result"), node("K3", "取消后没有结局卡"))
    .toBeVisible({ timeout: 15_000 });
  const read = await readCard(page, "K3");
  await decide("K3", read, { post_count: posts });
});

test.afterAll(() => {
  // The run id is in the file name on purpose: a reverse-mutation batch runs this spec several times
  // on the same tree, and an arm's reading must survive the next arm's run to be quotable.
  const runId = process.env.CITYNEXUS_E2E_RUN_ID ?? "norunid";
  writeFileSync(evidencePath("ord0310", `keyless_deadline_arms-${runId}.json`),
    JSON.stringify({ file: FILE, runId,
      deadline_constant_ms: KEYLESS_WRITE_DEADLINE_MS, arms: readings }, null, 1));
});
