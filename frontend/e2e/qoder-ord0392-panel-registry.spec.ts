/* ORD-0392 · the panel registry decides what shows on a screen, so this gate judges the table
 * against the rendered screen, not against a description of the refactor.
 *
 * R1 the table is well-formed: entry counts, unique ids, one surface per entry, a predicate and a
 *    render each (a silently dropped entry is the failure mode of a refactor like this);
 * R2 on the authoritative screen the DOM order of the declared testids equals the table order --
 *    the property this order buys is that the table, not the shell, decides what appears;
 * R3 the predicates are honoured at runtime: the version list is absent on Today, present after the
 *    real phone entry (drawer -> 「恢复与执行」), and absent again on the way back;
 * R4 the shell no longer carries the per-panel `{route === "x" && <Panel/>}` chain, so a lane that
 *    adds a panel edits the table and not a 4 494-line file three lanes touched tonight.
 *
 * The table is read from its source text rather than imported: importing it pulls the panel
 * modules, and one of them imports `maplibre-gl.css`, which the Node side of a Playwright worker
 * cannot evaluate. Parsing is strict -- a missing field fails the arm instead of quietly yielding
 * a shorter list.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const FILE = "e2e/qoder-ord0392-panel-registry.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const table = readFileSync(resolve(process.cwd(), "src/app/v8PanelRegistry.tsx"), "utf-8");
const shell = readFileSync(resolve(process.cwd(), "src/app/V8Experience.tsx"), "utf-8");

type Row = { id: string; surface: string; testId: string | null };

function entriesOf(exportName: string): Row[] {
  const from = table.indexOf(`export const ${exportName}`);
  expect(from, node("stage", `表里找不到 export const ${exportName}`)).toBeGreaterThan(-1);
  const body = table.slice(from, table.indexOf("\n];", from));
  const rows: Row[] = [];
  for (const chunk of body.split(/\n  \{\n/).slice(1)) {
    const id = /\bid: "([^"]+)"/.exec(chunk)?.[1];
    const surface = /\bsurface: "([^"]+)"/.exec(chunk)?.[1];
    const testIdRaw = /\btestId: (null|"[^"]+")/.exec(chunk)?.[1];
    expect(id, node("stage", `${exportName} 里有一条没有 id`)).toBeTruthy();
    expect(surface, node("stage", `${exportName}/${id} 没有 surface`)).toBeTruthy();
    expect(
      testIdRaw,
      node("stage", `${exportName}/${id} 没有 testId 字段（没有就要写 null，不能省略）`),
    ).toBeTruthy();
    expect(/\bwhen:/.test(chunk), node("stage", `${exportName}/${id} 没有 when 谓词`)).toBe(true);
    expect(/\brender:/.test(chunk), node("stage", `${exportName}/${id} 没有 render`)).toBe(true);
    rows.push({
      id: id as string,
      surface: surface as string,
      testId: testIdRaw === "null" ? null : (testIdRaw ?? "").replace(/"/g, ""),
    });
  }
  return rows;
}

const routeRows = entriesOf("routePanels");
const tripsRows = entriesOf("tripsTabPanels");
const authRows = entriesOf("authoritativePanels");

/** Which journey the device is on is boot state, not a constant: read the scene the deployment
 *  names, the way `qoder-ord0386-plan-version-history.spec.ts` does. */
async function openRoute(page: Page, route: string) {
  const res = await page.request.get("/api/v1/demo/scene");
  expect(res.status(), node("stage", `demo/scene 没有回答，实读=${res.status()}`)).toBe(200);
  const jid = String(((await res.json()) as { journey_id?: string }).journey_id ?? "");
  expect(jid.length, node("stage", "舞台没有已点名的旅程")).toBeGreaterThan(0);
  await page.addInitScript(
    ([id]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", "zh-CN");
    },
    [jid],
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/v8/${route}`, { waitUntil: "load" });
}

test("R1 注册表本身是完整的：条数、id 不重、面不串、每条都有谓词和渲染", async () => {
  expect(routeRows.length, node("R1", `route 面条目数变了，实读=${routeRows.length}`)).toBe(14);
  expect(tripsRows.length, node("R1", `trips-tab 面条目数变了，实读=${tripsRows.length}`)).toBe(4);
  expect(authRows.length, node("R1", `authoritative 面条目数变了，实读=${authRows.length}`)).toBe(7);
  for (const [surface, rows] of [
    ["route", routeRows],
    ["trips-tab", tripsRows],
    ["authoritative", authRows],
  ] as const) {
    const ids = rows.map((r) => r.id);
    expect(new Set(ids).size, node("R1", `${surface} 面里有重复 id：${ids.join(",")}`)).toBe(ids.length);
    for (const r of rows) {
      expect(r.surface, node("R1", `${r.id} 的 surface 写错，实读=${r.surface}`)).toBe(surface);
      if (r.testId !== null) {
        expect(/^[a-z0-9-]+$/.test(r.testId), node("R1", `${r.id} 的 testId 不像 data-testid：${r.testId}`)).toBe(true);
      }
    }
  }
  const history = readFileSync(
    resolve(process.cwd(), "src/features/studio/PlanVersionHistoryPanel.tsx"),
    "utf-8",
  );
  expect(
    history.includes('data-testid="plan-version-history"'),
    node("R1", "表里写的版本历史 testid 与面板渲染的不一致"),
  ).toBe(true);
});

test("R2 权威屏上那几块的先后，就是表里的先后", async ({ page }) => {
  await openRoute(page, "authoritative");
  await expect(
    page.getByTestId("plan-version-history"),
    node("R2", "版本历史面板没上屏，下面无从判序"),
  ).toBeVisible({ timeout: 30_000 });
  /* The four plan-gated blocks only exist once the plans read has landed; reading the order
     before that compares two blocks and calls it a pass. */
  await expect(
    page.getByTestId("v12-trip-studio"),
    node("R2", "这一屏上没有 plan 门的块：要么这趟没有已选方案，要么 plans 读还没落地"),
  ).toBeVisible({ timeout: 30_000 });
  const onScreen = await page.$$eval("[data-testid]", (ns) =>
    ns.map((n) => n.getAttribute("data-testid") as string),
  );
  /* Presence is not the claim -- a panel may legitimately render nothing on a given journey
     (`v14-preventive` holds back when there is nothing to warn about), and making it render
     something for the sake of a gate would be the wrong kind of green. The claim is order: of the
     testids the table declares, the ones that *are* on screen must appear in table order. */
  const declared = authRows.filter((r) => r.testId !== null).map((r) => r.testId as string);
  const present = declared.filter((id) => onScreen.includes(id));
  expect(present.length, node("R2", `屏上可比的块太少（${present.length}/${declared.length}），这一格就是空转`)).toBeGreaterThanOrEqual(4);
  const seen = new Set(present);
  const domOrder = onScreen.filter((id) => seen.has(id));
  expect(domOrder.join(">"), node("R2", `屏上顺序与表里不一致，屏=${domOrder.join(">")} 表=${present.join(">")}`)).toBe(
    present.join(">"),
  );
});

test("R3 谓词真的管用：今天屏没有，抽屉进「恢复与执行」才有，回来就没有", async ({ page }) => {
  const count = () => page.locator('[data-testid="plan-version-history"]').count();
  await openRoute(page, "hub");
  await page.waitForSelector(".v8-mobile-nav", { timeout: 30_000 });
  expect(await count(), node("R3", "今天屏上出现了只属于权威屏的版本列表")).toBe(0);

  await page.locator("button.v8-mobile-menu").click();
  await page.waitForSelector("#v8-nav-drawer", { timeout: 15_000 });
  await page.locator("#v8-nav-drawer button", { hasText: "恢复与执行" }).first().click();
  await page.waitForSelector('[data-testid="plan-version-history"]', { timeout: 30_000 });
  expect(await count(), node("R3", `从抽屉进来后计数不是 1，实读=${await count()}`)).toBe(1);

  await page.locator("button.v8-mobile-menu").click();
  await page.waitForSelector("#v8-nav-drawer", { timeout: 15_000 });
  await page.locator("#v8-nav-drawer button", { hasText: "今天" }).first().click();
  await page.waitForSelector(".v8-mobile-nav", { timeout: 15_000 });
  await page.waitForTimeout(800);
  expect(await count(), node("R3", `回到今天屏后面板还挂着，实读=${await count()}`)).toBe(0);
});

test("R4 外壳里那条逐面板的 if 链已经没有了，加面板只改表", async () => {
  for (const gone of [
    '{route === "authoritative" && (',
    '{route === "discover" && (',
    '{route === "intelligence" && (',
    '{tab === "itinerary" && (',
    "<RecoveryRequiredPanel",
    "<PlanVersionHistoryPanel",
    "<V12TripStudioPanel",
    "<V14PreventivePanel",
  ]) {
    expect(shell.includes(gone), node("R4", `外壳里还留着 ${gone}，挂载没搬干净`)).toBe(false);
  }
  const traversals = (shell.match(/renderPanels\(/g) || []).length;
  expect(traversals, node("R4", `外壳遍历注册表的次数不是 3，实读=${traversals}`)).toBe(3);
  for (const name of ["routePanels", "tripsTabPanels", "authoritativePanels"]) {
    expect(table.includes(`export const ${name}`), node("R4", `表里没有 ${name}`)).toBe(true);
  }
});
