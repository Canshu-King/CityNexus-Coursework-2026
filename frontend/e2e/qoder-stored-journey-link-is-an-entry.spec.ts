/**
 * ORD-0351 · 一条指向已存行程的链接，要么打开那一趟，要么具名地说它打不开 —— 不许是同一个壳假装什么都没发生。
 *
 * 改前量到的形状（`--land` 前那一次读数就是它）：`frontend/src/main.tsx` 的 `App()` 只认 `/legacy`，
 * 其余路径一律渲染同一个壳；壳里 `routeFromPath`（app/V8Experience.tsx）把不认识的第一段判成 Today
 * （:472 的注释就是那句 "an unknown path is Today"），而 boot 只读 `localStorage`（:731）。全树写 URL 的
 * 只有 navigate()（:844），它的 11 枚调用点喂的全是路由名 —— 也就是没有任何代码把行程 id 放进地址栏，
 * 也没有任何代码把它读回来。旅客把 `/v8/<id>` 发给同事，同事看到的是自己设备上那一趟（或新建向导），
 * 地址栏却仍写着另一趟。
 *
 * 修的形状：入口只把地址栏那枚 id 交给**既有**的那条读路径（壳的 boot），不在 `main.tsx` 里造第二套
 * 状态词汇、也不造第二个失败屏 —— 具名状态、按腿归因、离线副本的实测与选择器都在壳那一侧，
 * `lib/failureAttribution.ts` 是唯一分类口（R14-P）。所以本文件量的是"腿分不分得清"，不是句子写得好不好。
 *
 * 形制：CITYNEXUS_E2E_AUTHORITATIVE=1（自己的前端端口 + 自己的后端端口 + 自己的 `_*.db`），
 * 行程由 `POST /api/v1/journeys` 现造，0 次 demo/reset，0 读写 *_live.db / *_preview.db；
 * 390x844 手机视口；每格一枚自己的读数件（CITYNEXUS_EVIDENCE_DIR 指到检出树之外）。
 */
import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { desktopRoutes, mobileRoutes } from "../src/routes/v8Routes";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-stored-journey-link-is-an-entry.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;
const V8_JOURNEY_KEY = "REDACTED_FOR_PUBLIC_RELEASE";
const REFERENCE_KEYS = [V8_JOURNEY_KEY, "citynexus.demo.journey_id", "citynexus.demo.agent_run_id"];
/** `ROUTE_ALIASES` in app/V8Experience.tsx — kept here as data so arm E can name what it checked. */
const ALIAS_NAMES = ["today", "copilot", "wallet", "itinerary", "stays", "home"];
const GHOST = randomUUID();

const zh = (s: string) => s;

type Read = { url: string; status: number };

/** Every `/experience` read the page itself made, with the id it asked for. */
function watchJourneyReads(page: Page): Read[] {
  const reads: Read[] = [];
  page.on("response", (response) => {
    if (/\/api\/v1\/journeys\/[^/]+\/experience(\?|$)/.test(response.url())) {
      reads.push({ url: response.url(), status: response.status() });
    }
  });
  return reads;
}

async function createJourney(request: import("@playwright/test").APIRequestContext, cityCode: string, title: string) {
  const traveler = await request.post(`${API}/api/v1/travelers`, {
    data: { display_name: "ORD-0351 traveller", locale: "zh-CN", timezone: "Asia/Shanghai" },
  });
  expect(traveler.ok(), `SETUP：POST /travelers -> ${traveler.status()}`).toBe(true);
  const journey = await request.post(`${API}/api/v1/journeys`, {
    data: {
      traveler_id: (await traveler.json()).id,
      title,
      city_code: cityCode,
      timezone: "Asia/Shanghai",
      start_at: "2026-10-09T18:00:00+08:00",
      end_at: "2026-10-11T15:00:00+08:00",
      budget: { amount_minor: 120000, currency: "CNY" },
    },
  });
  expect(journey.ok(), `SETUP：POST /journeys -> ${journey.status()} ${(await journey.text()).slice(0, 160)}`).toBe(true);
  return (await journey.json()).id as string;
}

/**
 * Start from a document that holds nothing, then navigate to `path` as a real page load.
 * The empty precondition is what makes the URL the only carrier of the id: if the shell shows a
 * journey here, it can only have learned it from the address bar.
 */
async function openWithEmptyStorage(page: Page, path: string) {
  const reads = watchJourneyReads(page);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate((keys) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
    keys.forEach((k) => localStorage.removeItem(k));
  }, REFERENCE_KEYS);
  await page.goto(path, { waitUntil: "load" });
  return reads;
}

async function seedStoredId(page: Page, id: string) {
  const reads = watchJourneyReads(page);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.evaluate(([journeyId, keys]) => {
    localStorage.clear();
    keys.forEach((k: string) => localStorage.removeItem(k));
    localStorage.setItem("citynexus.locale", "zh-CN");
    localStorage.setItem("REDACTED_FOR_PUBLIC_RELEASE", journeyId as string);
  }, [id, REFERENCE_KEYS]);
  /* The read is what this helper is for, so wait for it instead of asserting on an array that a
     reload had no obligation to have filled yet -- measured: without this the harness races its own
     fixture and reports a setup failure that is not the product's. */
  const booted = page.waitForResponse(/\/api\/v1\/journeys\/[^/]+\/experience/, { timeout: 25_000 });
  await page.reload({ waitUntil: "load" });
  await booted;
  return reads;
}

function surface(page: Page) {
  return page.evaluate(() => {
    const view = document.querySelector<HTMLElement>("[data-testid='failure-view']");
    const heading = document.querySelector<HTMLElement>("[data-testid='today-heading']");
    const records = (window as unknown as { citynexusAttribution?: Array<Record<string, unknown>> })
      .citynexusAttribution ?? [];
    return {
      heading: heading?.innerText?.trim() ?? null,
      wizardShown: Array.from(document.querySelectorAll("h1")).some((h) =>
        /新建行程|Create a trip/.test(h.textContent ?? ""),
      ),
      failureViewPresent: Boolean(view),
      reason: view?.dataset.attributionReason ?? null,
      evidenceKind: view?.dataset.evidenceKind ?? null,
      httpStatus: view?.dataset.httpStatus ?? null,
      sourceLayer: view?.dataset.sourceLayer ?? null,
      recoveryAction: view?.dataset.recoveryAction ?? null,
      failureHeading:
        document.querySelector<HTMLElement>("[data-testid='failure-view'] h1")?.innerText?.trim() ?? null,
      bodyText: (document.body.innerText ?? "").replace(/\s+/g, " ").slice(0, 400),
      stored: localStorage.getItem("REDACTED_FOR_PUBLIC_RELEASE"),
      storedKeys: ["REDACTED_FOR_PUBLIC_RELEASE", "citynexus.demo.journey_id", "citynexus.demo.agent_run_id"].map(
        (k) => [k, localStorage.getItem(k)],
      ),
      telemetry: records.map((r) => ({
        reason: r.reason,
        evidence_kind: r.evidence_kind,
        http_status: r.http_status,
        context: r.context,
      })),
    };
  });
}

const NOT_FOUND_TITLE = "这台服务器上没有这一趟行程了";
const NETWORK_TITLE = "网络不可用";

function writeReading(name: string, payload: unknown) {
  const target = evidencePath("ord0351-journey-link", `${name}.json`);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, JSON.stringify(payload, null, 2) + "\n", "utf-8");
  return target;
}

test.describe("ORD-0351 · 已存行程的链接是一个入口", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("ord0351-a-a-link-to-a-saved-trip-opens-that-trip — 真存在的 id 走通到那一趟", async ({ page, request }) => {
    const NODE = "ord0351-a-a-link-to-a-saved-trip-opens-that-trip";
    const mine = await createJourney(request, "CN-TIANJIN", "Tianjin · ORD-0351");
    const other = await createJourney(request, "CN-XIAMEN", "Xiamen · ORD-0351");
    const reads = await openWithEmptyStorage(page, `/v8/${mine}`);
    await expect(page.getByTestId("today-heading"), node(NODE, `地址栏写着 ${mine}，屏上没有出现那一趟的 Today`)).toBeVisible({
      timeout: 20_000,
    });
    const seen = await surface(page);
    /* Two journeys exist on this server, so "the shell showed *a* trip" proves nothing: the claim is
       that it showed the one the address named, and did not read the other one at all. */
    expect(
      /Tianjin|天津/.test(seen.heading ?? "") && !/Xiamen|厦门/.test(seen.heading ?? ""),
      node(NODE, `屏上那趟是 ${JSON.stringify(seen.heading)}，而链接指的是 CN-TIANJIN 那一趟`),
    ).toBe(true);
    expect(seen.wizardShown, node(NODE, "链接被当成了不存在：屏上还是新建向导")).toBe(false);
    const ids = Array.from(new Set(reads.map((r) => (r.url.match(/\/journeys\/([^/]+)\/experience/) ?? [])[1])));
    expect(ids, node(NODE, `页面读过的行程不止地址栏那一枚：${JSON.stringify(reads)}`)).toEqual([mine]);
    expect(reads.map((r) => r.status), node(NODE, `那一趟的读不是 200：${JSON.stringify(reads)}`)).toEqual([200]);
    expect(seen.stored, node(NODE, `地址栏的 id 没被 adopt 成设备当前行程：${seen.stored}`)).toBe(mine);
    expect(seen.failureViewPresent, node(NODE, "走通的一趟也配了一个失败屏，说明分类被误触发")).toBe(false);
    writeReading(NODE, {
      node: NODE,
      journeys: { this_link: mine, another_on_the_same_server: other },
      link: `/v8/${mine}`,
      heading_on_screen: seen.heading,
      wizard_shown: seen.wizardShown,
      experience_reads: reads,
      stored_reference_after: seen.stored,
      failure_view_present: seen.failureViewPresent,
    });
  });

  test("ord0351-b-a-link-the-server-denies-says-so-by-name — 不存在的 id 到具名那一屏", async ({ page, request }) => {
    const NODE = "ord0351-b-a-link-the-server-denies-says-so-by-name";
    const reads = await openWithEmptyStorage(page, `/v8/${GHOST}`);
    await expect(page.getByTestId("failure-view"), node(NODE, `死链接没有任何具名屏（读数 ${JSON.stringify(reads)}）`)).toBeVisible({
      timeout: 20_000,
    });
    const seen = await surface(page);
    const probes = await page.evaluate(async (id) => {
      const target = await fetch(`/api/v1/journeys/${id}/experience`);
      const health = await fetch("/api/v1/health");
      return { target: target.status, health: health.status, onLine: navigator.onLine };
    }, GHOST);
    expect(seen.reason, node(NODE, `具名状态是 ${JSON.stringify(seen.reason)}，而服务器对这一发的回答是 ${probes.target}`)).toBe(
      "JOURNEY_NOT_FOUND",
    );
    expect(seen.evidenceKind, node(NODE, `归因依据是 ${JSON.stringify(seen.evidenceKind)}；有回答 arrives 就是 HTTP_RESPONSE`)).toBe(
      "HTTP_RESPONSE",
    );
    expect(seen.httpStatus, node(NODE, `屏上没有带上它依据的那个状态：${JSON.stringify(seen.httpStatus)}`)).toBe("404");
    expect(seen.sourceLayer, node(NODE, `source_layer=${JSON.stringify(seen.sourceLayer)}`)).toBe("RESOURCE_STATE");
    expect(seen.recoveryAction, node(NODE, `recovery_action=${JSON.stringify(seen.recoveryAction)}`)).toBe("choose-journey");
    /* The visible sentence, not an attribute: a missing trip must not be reported as a dead network. */
    expect(seen.failureHeading, node(NODE, `屏上那句标题是 ${JSON.stringify(seen.failureHeading)}`)).toContain(NOT_FOUND_TITLE);
    expect(
      seen.bodyText.includes(NETWORK_TITLE),
      node(NODE, `死链接被说成了网络问题：${seen.bodyText}`),
    ).toBe(false);
    expect(seen.wizardShown, node(NODE, "具名屏之外还叠了一个新建向导，等于没回答")).toBe(false);
    expect(reads.some((r) => r.status === 404), node(NODE, `页面自己没读到那一发 404：${JSON.stringify(reads)}`)).toBe(true);
    expect(
      seen.telemetry.some((t) => t.reason === "JOURNEY_NOT_FOUND" && t.evidence_kind === "HTTP_RESPONSE"),
      node(NODE, `遥测里没有这条同源的分类：${JSON.stringify(seen.telemetry)}`),
    ).toBe(true);
    writeReading(NODE, {
      node: NODE,
      link: `/v8/${GHOST}`,
      in_page_probes: probes,
      machine: {
        reason: seen.reason,
        evidence_kind: seen.evidenceKind,
        http_status: seen.httpStatus,
        source_layer: seen.sourceLayer,
        recovery_action: seen.recoveryAction,
      },
      visible_heading: seen.failureHeading,
      visible_body: seen.bodyText,
      wizard_shown: seen.wizardShown,
      experience_reads: reads,
      telemetry: seen.telemetry,
    });
  });

  test("ord0351-c-an-unanswered-link-is-never-blamed-on-the-server — 发不出去这一腿不许混成找不到", async ({ page }) => {
    const NODE = "ord0351-c-an-unanswered-link-is-never-blamed-on-the-server";
    const silenced = await page.route("**/api/v1/journeys/**/experience", (route) => route.abort());
    const reads = await openWithEmptyStorage(page, `/v8/${GHOST}`);
    await expect(page.getByTestId("failure-view"), node(NODE, "这一腿没有任何具名屏")).toBeVisible({ timeout: 20_000 });
    const seen = await surface(page);
    const probes = await page.evaluate(async (id) => {
      const target = await fetch(`/api/v1/journeys/${id}/experience`).catch((e) => `rejected: ${(e as Error).name}`);
      const health = await fetch("/api/v1/health").catch(() => null);
      return { target, health: health ? health.status : null };
    }, GHOST);
    expect(seen.reason, node(NODE, `没人回答的这一腿被叫成了 ${JSON.stringify(seen.reason)}`)).not.toBe("JOURNEY_NOT_FOUND");
    expect(
      seen.failureHeading ?? "",
      node(NODE, `这一腿的屏上写着 ${JSON.stringify(seen.failureHeading)}，而服务器根本没被问到`),
    ).not.toContain(NOT_FOUND_TITLE);
    expect(seen.evidenceKind, node(NODE, `依据是 ${JSON.stringify(seen.evidenceKind)}：没有回答就不算 HTTP_RESPONSE`)).not.toBe(
      "HTTP_RESPONSE",
    );
    expect(
      seen.bodyText.includes(NOT_FOUND_TITLE),
      node(NODE, `正文里仍出现了"没有这一趟行程"：${seen.bodyText}`),
    ).toBe(false);
    writeReading(NODE, {
      node: NODE,
      link: `/v8/${GHOST}`,
      route_interception_active: silenced,
      machine: {
        reason: seen.reason,
        evidence_kind: seen.evidenceKind,
        http_status: seen.httpStatus,
        source_layer: seen.sourceLayer,
        recovery_action: seen.recoveryAction,
      },
      visible_heading: seen.failureHeading,
      visible_body: seen.bodyText,
      in_page_probes: probes,
      experience_reads: reads,
      telemetry: seen.telemetry,
    });
    await page.unroute("**/api/v1/journeys/**/experience");
  });

  test("ord0351-d-a-dead-link-empties-a-reference-not-a-trip — 死链接只作废引用，不碰别的那一趟", async ({ page, request }) => {
    const NODE = "ord0351-d-a-dead-link-empties-a-reference-not-a-trip";
    const living = await createJourney(request, "CN-XIAMEN", "Xiamen · ORD-0351 living");
    const reads = await seedStoredId(page, living);
    expect(reads.some((r) => r.status === 200), node(NODE, `SETUP：这台设备自己的那一趟没读出来：${JSON.stringify(reads)}`)).toBe(
      true,
    );
    const rowBefore = await request.get(`${API}/api/v1/journeys/${living}`);
    expect(rowBefore.status(), node(NODE, `SETUP：别的那一趟的单条读不是 200：${rowBefore.status()}`)).toBe(200);
    const journeyRowBefore = (await rowBefore.text()).trim();
    await page.goto(`/v8/${GHOST}`, { waitUntil: "load" });
    await expect(page.getByTestId("failure-view"), node(NODE, "死链接没有具名屏")).toBeVisible({ timeout: 20_000 });
    const seen = await surface(page);
    expect(seen.reason, node(NODE, `具名状态是 ${JSON.stringify(seen.reason)}`)).toBe("JOURNEY_NOT_FOUND");
    /* R14-S's designed outcome, pinned rather than assumed: the invalid reference is cleared, and the
       clearing is bounded to the three reference keys. */
    expect(
      seen.storedKeys.filter(([, v]) => v !== null),
      node(NODE, `引用没被作废，下一次刷新还会撞同一发 404：${JSON.stringify(seen.storedKeys)}`),
    ).toEqual([]);
    const untouched = await request.get(`${API}/api/v1/journeys/${living}`);
    expect(untouched.status(), node(NODE, `别的那一趟连单条读都答不出来了：${untouched.status()}`)).toBe(200);
    /* Byte-for-byte: "no other trip was touched" is a claim about the row, so it is compared with the
       row's own read from before the link was opened -- not with a status code, which a rewrite survives. */
    expect(
      (await untouched.text()).trim(),
      node(NODE, "具名屏之后，别的那一趟的行程行变了内容"),
    ).toBe(journeyRowBefore);
    const ghostStillAbsent = await request.get(`${API}/api/v1/journeys/${GHOST}`);
    expect(ghostStillAbsent.status(), node(NODE, `死链接反而在服务器上留下了一行：${ghostStillAbsent.status()}`)).toBe(404);
    writeReading(NODE, {
      node: NODE,
      device_held_before_link: living,
      link: `/v8/${GHOST}`,
      machine: { reason: seen.reason, evidence_kind: seen.evidenceKind, http_status: seen.httpStatus },
      reference_keys_after: seen.storedKeys,
      other_journey_row_read_status: untouched.status(),
      other_journey_row_unchanged: true,
      ghost_read_status_after: ghostStillAbsent.status(),
      experience_reads: reads,
    });
  });

  test("ord0351-e-route-names-are-not-journey-links — 路由名与别名不是一条待认领的行程", async ({ page, request }) => {
    const NODE = "ord0351-e-route-names-are-not-journey-links";
    const living = await createJourney(request, "CN-TIANJIN", "Tianjin · ORD-0351 held");
    const pattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const routeNames = [...new Set([...desktopRoutes, ...mobileRoutes].map((r) => r.key))].concat(ALIAS_NAMES);
    const shapeReadings = routeNames.map((name) => ({ name, uuid_shaped: pattern.test(name) }));
    /* The entry keys off shape alone, so this is the arm that says the shape rule cannot eat a route:
       every name the product navigates to must miss the pattern, and a server-issued id must hit it. */
    expect(
      shapeReadings.filter((r) => r.uuid_shaped),
      node(NODE, `路由名与 UUID 形状重合了：${JSON.stringify(shapeReadings)}`),
    ).toEqual([]);
    expect(pattern.test(living), node(NODE, `服务器发的 id 不匹配入口的形状规则：${living}`)).toBe(true);

    for (const path of ["/v8/hub", "/v8/live", "/v8/authoritative", "/v8/today", "/v8/bookings", "/v8/stays"]) {
      const reads = await seedStoredId(page, living);
      const before = (await surface(page)).stored;
      expect(before, node(NODE, `SETUP：这台设备的引用没落成 ${living}`)).toBe(living);
      const settled = page.waitForResponse(/\/api\/v1\/journeys\/[^/]+\/experience/, { timeout: 25_000 });
      await page.goto(path, { waitUntil: "load" });
      await settled;
      const seen = await surface(page);
      expect(seen.stored, node(NODE, `${path} 被当成行程链接认领了：引用现在是 ${seen.stored}`)).toBe(living);
      expect(seen.failureViewPresent, node(NODE, `${path} 上冒出了具名失败屏`)).toBe(false);
      expect(reads.length > 0, node(NODE, `${path} 这一发行程读都没发生，断言会空转`)).toBe(true);
    }
    writeReading(NODE, {
      node: NODE,
      route_names_and_aliases: shapeReadings,
      server_issued_id_matches_shape: pattern.test(living),
      paths_walked: ["/v8/hub", "/v8/live", "/v8/authoritative", "/v8/today", "/v8/bookings", "/v8/stays"],
      alias_source: "app/V8Experience.tsx:447 ROUTE_ALIASES",
      held_journey: living,
    });
  });
});
