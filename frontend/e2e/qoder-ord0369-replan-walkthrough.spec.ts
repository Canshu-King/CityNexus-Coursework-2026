/* ORD-0369 (lane Q) — the phone chain "chosen plan -> request one change -> see the trial ->
 * accept" has to be readable, not just answered.
 *
 * Every assertion below was written after measuring the failure on a 390x844 viewport against a
 * backend this lane started (own port, own `Connection.backup()` copy of the preview DB), and each
 * one states the shape the server really returned on 2026-09-28:
 *   POST .../plans/generate      422 "No feasible plan candidates satisfy the journey constraints"
 *   POST .../recovery/acknowledge 404 "no open recovery to acknowledge"
 *   POST .../replans             422 message MOBILITY_DATA_UNAVAILABLE, one gap, evidence state
 *                                FACT_PRESENT_BUT_NOT_HARD_ELIGIBLE
 *   POST .../trip-studio/preview 200 can_accept=false, failures = 3 bare codes + 4
 *                                `MOBILITY_DATA_UNAVAILABLE:<uuid>|<uuid>` leg pairs
 *
 * Two kinds of arm, deliberately: a *reading* arm says what the traveller sees, and a *relocation*
 * arm opens the 技术详情 fold and demands the server's own words are still there. Q67 allows the
 * fold to quote the answer; hiding it instead would be the named-reason-never-arrives defect.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const FILE = "e2e/qoder-ord0369-replan-walkthrough.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();
const BASE = process.env.CNX_API_BASE_URL ?? "http://127.0.0.1:8711";

/** The backend's own sentences, copied from the measured responses above. */
const PROSE_GENERATE = "No feasible plan candidates satisfy the journey constraints";
const PROSE_STALE = "PlanVersion frozen mobility evidence expired";
/** A leg pair is payload, not an explanation: it belongs in the fold and nowhere else. */
const LEG_PAIR = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\|/;
/** A bare contract token pasted as prose is the shape this order removed. */
const BARE_TOKEN_LINE = /^[A-Z][A-Z0-9_]{4,}$/;

/** innerText excludes a closed <details>, which is what makes "not on the reading line" testable.
 *  Every relocation arm proves that mechanism before relying on it. */
async function readingText(page: Page) {
  return clean(await page.evaluate(() => document.body.innerText));
}

async function openSealedJourney(page: Page, path: string) {
  const seeded = (await (await page.request.get(`${BASE}/api/v1/demo/scene`)).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length > 0, node("stage", "the scene names no journey to open")).toBe(true);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // The shell keeps the chosen journey in this device's own storage; the sealed-demo button is the
  // documented GET-only way in (952ec40), so the walk enters the way a traveller does.
  const entry = page.getByTestId("trial-tianjin-demo");
  await expect(entry, node("stage", "the sealed-demo entry is not on the first screen")).toBeVisible({ timeout: 40_000 });
  await entry.scrollIntoViewIfNeeded();
  await entry.click();
  await page.evaluate((id) => localStorage.setItem("citynexus.v8.journey_id", id), journeyId);
  await page.goto(path, { waitUntil: "domcontentloaded" });
}

/** Fold every `<details>` closed, so a reading-line assertion cannot be satisfied by a fold that
 *  happens to be open from an earlier arm. */
async function closeFolds(page: Page) {
  await page.evaluate(() => {
    document.querySelectorAll("details").forEach((d) => {
      d.open = false;
    });
  });
}

test.describe("ORD-0369 the stale-evidence card a phone traveller hits first", () => {
  test("A1 中文半边：卡片不再整张英文，且服务器的原话不在阅读行上", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    const card = page.getByTestId("recovery-required-panel");
    await expect(card, node("A1", "证据过期卡片没有出现在权威屏上")).toBeVisible({ timeout: 30_000 });
    await closeFolds(page);

    await expect(page.getByTestId("recovery-required-title"), node("A1", "标题不是这台设备该说的那句中文")).toHaveText(
      "这趟行程的出行证据需要重新同步",
    );
    const text = await readingText(page);
    for (const prose of [PROSE_GENERATE, PROSE_STALE]) {
      expect(text.includes(prose), node("A1", `服务器的原话仍在阅读行上：${prose}`)).toBe(false);
    }
    // The refusal list is still announced, as a count this device can stand behind.
    const count = page.getByTestId("recovery-failure-count");
    await expect(count, node("A1", "拒绝条数没有上屏")).toBeVisible();
    const stated = Number((clean(await count.innerText()).match(/\d+/) ?? [])[0]);
    expect(Number.isFinite(stated), node("A1", `条数句子里没有数字，实读=${clean(await count.innerText())}`)).toBe(true);
  });

  test("A2 移件：技术详情打开后，服务器的原话必须还在（ relocation 不是删除）", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    await expect(page.getByTestId("recovery-required-panel")).toBeVisible({ timeout: 30_000 });
    await closeFolds(page);
    const closed = await readingText(page);
    expect(closed.includes(PROSE_STALE), node("A2", "关掉折叠后原话仍在阅读行上")).toBe(false);

    await page.getByTestId("recovery-failure-technical-toggle").click();
    const opened = await readingText(page);
    expect(opened.includes(PROSE_STALE), node("A2", "打开技术详情后仍然找不到服务器原话")).toBe(true);
    // Q70: the element quoting the answer names which envelope field the words are.
    const field = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="recovery-failure"]');
      const host = el?.closest("[data-cnx-field]");
      return host?.getAttribute("data-cnx-field") ?? null;
    });
    expect(field, node("A2", "引用原话的元素没有具名它引的是哪个字段")).toBe("flags.feasibility_failures");
  });

  test("A3 按「重新生成可选方案」：422 用本地句子承担，不冒充网络也不圆场", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    const button = page.getByTestId("recovery-replan");
    await expect(button, node("A3", "生成按钮不在屏上")).toBeVisible({ timeout: 30_000 });
    await button.scrollIntoViewIfNeeded();
    await button.click();

    const message = page.getByTestId("recovery-message");
    await expect(message, node("A3", "按下去之后屏上没有回执句")).toBeVisible({ timeout: 30_000 });
    const said = clean(await message.innerText());
    expect(said, node("A3", `回执句不是具名 422 的那句中文，实读=${said}`)).toBe(
      "服务器拒绝了这次生成（HTTP 422，具名 VALIDATION_ERROR）。没有任何方案被改动。",
    );
    await closeFolds(page);
    expect(
      (await readingText(page)).includes(PROSE_GENERATE),
      node("A3", "服务器这句原话跑到了阅读行上"),
    ).toBe(false);
    // The recitable number is the server's or the screen says there is none -- never our own.
    await page.getByTestId("recovery-raw-technical-toggle").click();
    const receipt = clean(await page.getByTestId("recovery-raw-facts-receipt").innerText());
    expect(/服务器回执号：CNX-|服务器未给回执号/.test(receipt), node("A3", `回执行既不是编号也不是具名的缺失，实读=${receipt}`)).toBe(true);
  });

  test("A4 配对：确认按钮只在真有恢复决定可确认时出现（不是被删掉）", async ({ page }) => {
    await page.route("**/api/v1/journeys/*/experience", async (route: Route) => {
      const real = await route.fetch();
      const body = (await real.json()) as Record<string, unknown>;
      // Only this one field is flipped; everything else is the live answer.
      body.recovery = {
        ...(typeof body.recovery === "object" && body.recovery !== null ? body.recovery : {}),
        state: "RECOVERY_REQUIRED",
        capability_code: "STEP_FREE_REQUIRED",
        no_alternative: false,
        acknowledged: false,
        failures: [],
      };
      await route.fulfill({ response: real, body: JSON.stringify(body) });
    });
    await openSealedJourney(page, "/v8/authoritative");
    await expect(
      page.getByTestId("recovery-acknowledge"),
      node("A4", "把恢复状态改成开着之后，确认按钮仍然不在——A1 的缺席就不是状态驱动"),
    ).toBeVisible({ timeout: 30_000 });
    expect(
      await page.getByTestId("recovery-note").count(),
      node("A4", "有可确认的恢复时，那句「没有等待确认的决定」不该出现"),
    ).toBe(0);
  });
});

test.describe("ORD-0369 the Trip Studio trial", () => {
  test("B1 动手之前就说清：这一版没有可移动的活动，且数字与同一屏的计数自洽", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    const studio = page.getByTestId("v12-trip-studio");
    await expect(studio, node("B1", "工作室面板不在权威屏上")).toBeVisible({ timeout: 30_000 });
    const hint = clean(await page.getByTestId("v12-studio-drag-hint").innerText());
    expect(hint, node("B1", "没有任何一句话说明这里靠拖动来改，实读=" + hint)).toBe(
      "按住一项活动，拖到新的位置，就是一次草稿改动。",
    );
    const meta = clean(await studio.locator(".v12-studio-meta").innerText());
    const activities = Number((meta.match(/(\d+) 个活动/) ?? [])[1]);
    const protecteds = Number((meta.match(/(\d+) 个受保护锚点/) ?? [])[1]);
    expect(activities > 0 && protecteds > 0, node("B1", `meta 两个计数没读到，实读=${meta}`)).toBe(true);
    const blocked = page.getByTestId("v12-studio-all-protected");
    if (activities === protecteds) {
      await expect(blocked, node("B1", "全部受保护却没有事先说明")).toBeVisible();
      const stated = Number((clean(await blocked.innerText()).match(/(\d+) 个活动/) ?? [])[1]);
      expect(stated, node("B1", `句子里的数与 meta 的数不一致：句=${stated} meta=${protecteds}`)).toBe(protecteds);
    } else {
      expect(
        await blocked.count(),
        node("B1", `并非全部受保护（${protecteds}/${activities}）却仍然报「没有可移动的活动」`),
      ).toBe(0);
    }
  });

  test("B2 试算结果：原因逐条可读，段号 UUID 与裸 token 都不在阅读行上", async ({ page }) => {
    test.skip(process.env.CNX_ORD0369_SKIP_DRAG === "1", "lane switch only");
    await openSealedJourney(page, "/v8/authoritative");
    const studio = page.getByTestId("v12-trip-studio");
    await expect(studio).toBeVisible({ timeout: 30_000 });
    await studio.scrollIntoViewIfNeeded();
    const rows = studio.locator(".v8-activity");
    await expect(rows).toHaveCount(5, { timeout: 20_000 });
    // A real pointer drag is the only edit this panel accepts (PointerSensor, distance 6).
    const from = await rows.nth(1).boundingBox();
    const to = await rows.nth(3).boundingBox();
    expect(from && to, node("B2", "两行活动量不到位置，拖动无从下手")).toBeTruthy();
    await page.mouse.move(from!.x + from!.width / 2, from!.y + from!.height / 2);
    await page.mouse.down();
    for (let k = 1; k <= 12; k += 1) {
      await page.mouse.move(from!.x + from!.width / 2, from!.y + ((to!.y + 6 - from!.y) * k) / 12);
      await page.waitForTimeout(40);
    }
    await page.mouse.up();
    await expect(page.getByTestId("v12-preview-impact"), node("B2", "拖动没有产生草稿（预览按钮仍不可点）")).toBeEnabled({ timeout: 20_000 });
    await page.getByTestId("v12-preview-impact").click();

    const preview = page.getByTestId("v12-impact-preview");
    await expect(preview, node("B2", "按了预览影响，屏上没有试算结果")).toBeVisible({ timeout: 30_000 });
    // The list has to read as an explanation, so it carries its own lead-in; asserting it here is
    // also the proof that the key is read, which is what ORD-0370's copy ruler asks.
    await expect(preview.getByTestId("v12-gate-reasons-head"), node("B2", "原因清单没有领句，读起来像系统输出")).toHaveText("这次改动不能被接受的原因：");
    const reasons = preview.getByTestId("v12-gate-reason");
    const n = await reasons.count();
    expect(n > 0, node("B2", "被拒却没有列出任何一条原因")).toBe(true);
    for (let i = 0; i < n; i += 1) {
      const line = clean(await reasons.nth(i).innerText());
      expect(LEG_PAIR.test(line), node("B2", `原因行仍是带段号 UUID 的原文：${line}`)).toBe(false);
      expect(BARE_TOKEN_LINE.test(line), node("B2", `原因行是一个裸 token，不是句子：${line}`)).toBe(false);
    }
    await closeFolds(page);
    const reading = await readingText(page);
    expect(LEG_PAIR.test(reading), node("B2", "段号 UUID 对仍然出现在阅读行上")).toBe(false);
    expect(reading.includes("MOBILITY_DATA_UNAVAILABLE"), node("B2", "裸码仍在了阅读行上")).toBe(false);
    // ...and it must still exist once unfolded: readability may not become loss.
    await page.getByTestId("v12-gate-technical-toggle").click();
    const opened = await readingText(page);
    expect(opened.includes("MOBILITY_DATA_UNAVAILABLE"), node("B2", "技术详情里没有那条原文，等于把它删了")).toBe(true);
  });

  test("B3 数字带单位：成本是钱不是最小单位，时长用本地单位", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    const studio = page.getByTestId("v12-trip-studio");
    await expect(studio).toBeVisible({ timeout: 30_000 });
    await studio.scrollIntoViewIfNeeded();
    const rows = studio.locator(".v8-activity");
    const from = await rows.nth(1).boundingBox();
    const to = await rows.nth(2).boundingBox();
    await page.mouse.move(from!.x + from!.width / 2, from!.y + from!.height / 2);
    await page.mouse.down();
    await page.mouse.move(from!.x + from!.width / 2, to!.y + 6, { steps: 10 });
    await page.mouse.up();
    await page.getByTestId("v12-preview-impact").click();
    const summary = clean(await page.getByTestId("v13-frozen-evidence-summary").innerText());
    expect(/成本变化\s*[-−]?\s*[¥￥]\s*[\d,]+\.\d{2}/.test(summary), node("B3", `成本没有换算成钱，实读=${summary}`)).toBe(true);
    expect(summary.includes("time Δ"), node("B3", `时长仍是英文，实读=${summary}`)).toBe(false);
    expect(/时间变化\s*\d+\s*分钟/.test(summary), node("B3", `时长没有本地单位，实读=${summary}`)).toBe(true);
  });

  test("B4 不能接受的按钮不许长得像能接受", async ({ page }) => {
    await openSealedJourney(page, "/v8/authoritative");
    const studio = page.getByTestId("v12-trip-studio");
    await expect(studio).toBeVisible({ timeout: 30_000 });
    const accept = page.getByTestId("v12-accept-edit");
    await expect(accept, node("B4", "接受编辑按钮不在屏上")).toBeVisible();
    expect(await accept.isDisabled(), node("B4", "这一格的前提（未试算⇒不可接受）已经不成立")).toBe(true);
    const opacity = await accept.evaluate((el) => Number(getComputedStyle(el).opacity));
    expect(opacity < 1, node("B4", `禁用的主按钮与启用时无法区分，opacity=${opacity}`)).toBe(true);
  });
});

test.describe("ORD-0369 the teaching stage's replan leg", () => {
  test("C1 请求重规划被 422 拒：说的是证据不够硬，不是网络，也不是空列表", async ({ page }) => {
    await openSealedJourney(page, "/v8/hub");
    await page.getByTestId("sim-ball").click();
    const stage = page.getByTestId("sim-panel");
    await expect(stage, node("C1", "舞台没有展开")).toBeVisible({ timeout: 20_000 });
    const event = page.getByTestId("sim-action-event");
    await event.scrollIntoViewIfNeeded();
    await event.click();
    await expect(page.getByTestId("sim-outcome"), node("C1", "触发中断后没有回执")).toBeVisible({ timeout: 30_000 });
    const replan = page.getByTestId("sim-action-replan");
    await expect(replan, node("C1", "有了中断，请求重规划这一行仍然不给按")).toBeEnabled({ timeout: 20_000 });
    await replan.scrollIntoViewIfNeeded();
    await replan.click();

    const outcome = page.getByTestId("sim-outcome");
    await expect(outcome).toBeVisible({ timeout: 40_000 });
    const said = clean(await outcome.innerText());
    expect(said.includes("MOBILITY_DATA_UNAVAILABLE"), node("C1", `句子里没有服务器给的那个码，实读=${said}`)).toBe(true);
    expect(said.includes("没有变化"), node("C1", `句子没有说清方案有没有被动过，实读=${said}`)).toBe(true);
    for (const wrong of ["网络", "连不上", "network"]) {
      expect(said.toLowerCase().includes(wrong), node("C1", `一次具名 422 被说成了「${wrong}」：${said}`)).toBe(false);
    }
    expect(said.includes("它的原话"), node("C1", `这条已经能本地命名了，却仍走兜底转述：${said}`)).toBe(false);
  });
});

/* ---- ORD-0369 addendum: the AI face, whose acceptance target is an honest "not available" ----
 * Measured against the running backend with the switch off: POST .../agent/commands still answers
 * HTTP 200, `mission.status` is COMPLETED and `reasoning_summary` is populated, while the same
 * mission carries `ai_state = "DISABLED_BY_OPS"`. Mounting that sentence under ORD-0240's
 * 「模型原始输出」 label credits a model for text the deterministic compiler wrote.
 * These three arms are stub-shaped on purpose (the sealed demo's city answers CATALOG_EMPTY, so no
 * live run can reach the success branch here); the shape each one feeds is a field the real response
 * carries, and `qoder-model-words-are-labelled.spec.ts` stays the owner of the frozen label itself.
 */
const SUMMARY = "第二天减少一个点位，保留夜景。";
const NO_MODEL_LABEL_ZH = "服务器给这句总结（这台部署的 AI 开关是关的，所以它不是模型写的）";
const MODEL_LABEL_ZH = "模型原始输出（未翻译，不代表本界面的结论）";

async function submitWithMission(page: Page, mission: Record<string, unknown>, nodeId: string) {
  const seeded = (await (await page.request.get(`${BASE}/api/v1/demo/scene`)).json()) as { journey_id?: string };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length > 0, node(nodeId, "舞台没有已点名的旅程")).toBe(true);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  let commands = 0;
  let planReads = 0;
  await page.route("**/api/v1/journeys/*/agent/commands", async (route) => {
    commands += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mission: { status: "COMPLETED", failure_reason: null, reasoning_summary: SUMMARY, ...mission } }),
    });
  });
  // The sheet keeps the summary only on the branch where a plan appeared, so the reads are
  // sequenced like a run that produced one: empty baseline first, the new plan afterwards.
  await page.route("**/api/v1/journeys/*/plans", async (route) => {
    planReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(planReads === 1 ? [] : [{ id: "ord0369-plan-1", status: "DRAFT" }]),
    });
  });
  await page.route("**/api/v1/journeys/*/plans/generate", async (route) =>
    route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify([]) }),
  );
  await page.route("**/api/v1/places?city_code=*", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ id: "ord0369-place-1", name: "海河游船码头" }, { id: "ord0369-place-2", name: "天津之眼" }]),
    }),
  );
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero"), node(nodeId, "今天屏没起来，这一格就没在真屏上量过")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet"), node(nodeId, "AI 面板没有打开")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("plan-ai-input").fill("把第二天的安排改得轻松一点，不要太赶");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result"), node(nodeId, `按下后面板没有回执（commands 打了 ${commands} 次）`)).toBeVisible({ timeout: 60_000 });
  expect(commands > 0, node(nodeId, "commands 桩没有触发，这一格是空转")).toBe(true);
  const details = page.locator(".plan-ai-detail");
  if (!(await details.first().evaluate((el) => el.hasAttribute("open")))) {
    await page.getByTestId("plan-ai-detail-toggle").click();
  }
  const label = page.getByTestId("plan-ai-model-label");
  await expect(label, node(nodeId, "总结旁边没有任何归属标签")).toBeVisible({ timeout: 15_000 });
  return {
    said: clean(await label.innerText()),
    origin: await label.getAttribute("data-summary-origin"),
    quoted: clean(await page.getByTestId("plan-ai-model-output").innerText()),
  };
}

test("E1 AI 开关关着：这句总结不得算在模型头上", async ({ page }) => {
  const r = await submitWithMission(page, { ai_state: "DISABLED_BY_OPS" }, "E1");
  expect(r.said, node("E1", `开关关着仍把句子标成模型输出，实读=${r.said}`)).toBe(NO_MODEL_LABEL_ZH);
  expect(r.origin, node("E1", `归属位不是 no_model，实读=${r.origin}`)).toBe("no_model");
  expect(r.quoted, node("E1", "换了标签却把原文丢了")).toBe(SUMMARY);
});

test("E2 正控：提供方真的答过话时，ORD-0240 那句冻结标签一个字不改", async ({ page }) => {
  // PROVIDER_REFUSED is the one state in `backend/app/agent/ai_availability.py` that says the
  // provider was reached, so it is the only one allowed to keep crediting the sentence to a model.
  const r = await submitWithMission(page, { ai_state: "PROVIDER_REFUSED" }, "E2");
  expect(r.said, node("E2", `提供方答过话时标签被改写了，实读=${r.said}`)).toBe(MODEL_LABEL_ZH);
  expect(r.origin, node("E2", `归属位不是 model，实读=${r.origin}`)).toBe("model");
});

test("E3 反控：服务器没说状态时保持既有标签（未知不等于不可用）", async ({ page }) => {
  const r = await submitWithMission(page, {}, "E3");
  expect(r.said, node("E3", `没读到状态就改了既有标签，实读=${r.said}`)).toBe(MODEL_LABEL_ZH);
  expect(r.origin, node("E3", `没读到状态却声称知道了来源，实读=${r.origin}`)).toBe("not_stated");
});

test("E4 0359 的坑位：配了凭据未验证，不得被读成模型写过这句", async ({ page }) => {
  // ORD-0359 renamed READY to CONFIGURED_UNVERIFIED because "a key exists" was promising that it
  // works. Mapping that state to "model" here would re-open the same hole one level up, so this arm
  // exists to keep the rename honest at the label too.
  const r = await submitWithMission(page, { ai_state: "CONFIGURED_UNVERIFIED" }, "E4");
  expect(r.said, node("E4", `凭据未验证却被标成模型输出，实读=${r.said}`)).toBe(MODEL_LABEL_ZH);
  expect(r.origin, node("E4", `归属位不是 not_stated，实读=${r.origin}`)).toBe("not_stated");
});
