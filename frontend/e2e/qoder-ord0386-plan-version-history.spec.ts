/* ORD-0386 (lane Q) · the version-history screen, judged on rendered text at 390x844
 *
 * Nothing here invents a journey. Every arm opens the authoritative section of the journey this
 * deployment already points at (`GET /api/v1/demo/scene`), and every expectation is computed from
 * the same live `GET /api/v1/journeys/{id}/plans` read the panel will make -- so an arm fails if the
 * screen describes rows the server did not send, or drops rows it did.
 *
 * Three arms mutate that real payload, and each exists because this deployment has no such row to
 * walk into:
 *   H3 `created_at: null` -- every stored version here has a write time (9 of 9), so the "no time
 *                            recorded" branch is only reachable by taking the value away;
 *   H6 the three sources -- `backend/app/schemas.py` declares INITIAL / REPLAN / MANUAL, but
 *                            this journey holds only MANUAL rows;
 *   H5 HTTP 500            -- the read has to actually fail for the failure sentence to be judged.
 * Mutation is single-variable, applied to the real response fetched with `route.fetch()`: no field
 * is added, no row is invented, and the control rows keep their real values, so an arm cannot pass
 * by being blank everywhere. Every arm states how many rows it expects and waits for the list to
 * settle there before reading text -- a `count()` taken straight after the panel appears is the
 * loading state, not the screen (measured: 5 of 6 arms read 0 rows that way on the first pass).
 *
 * The retention cap is NOT judged here -- a stub could only repeat what it was handed. It is
 * measured against real writes in `backend/tests/test_qoder_ord0386_plan_version_history.py`
 * (test_3). This file only checks that the number on screen is the number of rows shown.
 *
 * Not the acceptance gate: `playwright.config.ts` stays the only thing that can sign one.
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { formatV8Date } from "../src/lib/v8Formatting";
import {
  historyCopy,
  fillHistory,
  type PlanVersionCopyKey,
} from "../src/features/studio/planVersionHistoryCopy";

const FILE = "e2e/qoder-ord0386-plan-version-history.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const clean = (v: string | null | undefined) => String(v ?? "").replace(/\s+/g, " ").trim();

/** The traveller's locale is pinned by the config, and the spec reads the same copy table the panel
 *  does, so rewording the screen moves the expectation instead of silently breaking the gate. */
const LOCALE = "zh-CN";
const say = (key: PlanVersionCopyKey, params: Record<string, string | number> = {}) =>
  fillHistory(historyCopy(LOCALE, key), params);
const SOURCE_KEYS: Record<string, PlanVersionCopyKey> = {
  INITIAL: "history.sourceInitial",
  REPLAN: "history.sourceReplan",
  MANUAL: "history.sourceManual",
};

type Row = {
  id: string;
  version_no: number;
  source: string;
  status: string;
  created_at?: string | null;
  parent_plan_version_id?: string | null;
  is_current?: boolean;
};
type Rendered = { version: string; source: string; created: string; parent: string; current: number };
type PlansCall = { method: string; idempotency_key: boolean };
type Panel = ReturnType<Page["getByTestId"]>;

async function sceneJourneyId(page: Page) {
  const res = await page.request.get("/api/v1/demo/scene");
  expect(res.status(), node("stage", `demo/scene 没有回答，实读=${res.status()}`)).toBe(200);
  const id = String(((await res.json()) as { journey_id?: string }).journey_id ?? "");
  expect(id.length, node("stage", "舞台没有已点名的旅程")).toBeGreaterThan(0);
  return id;
}

async function livePlans(page: Page, journeyId: string) {
  const res = await page.request.get(`/api/v1/journeys/${journeyId}/plans`);
  expect(res.status(), node("stage", `GET plans 没有回答，实读=${res.status()}`)).toBe(200);
  const rows = (await res.json()) as Row[];
  expect(Array.isArray(rows), node("stage", "plans 返回的不是一个列表")).toBe(true);
  // The screen re-sorts by version_no, so expectations are built from the same sort rather than
  // from whatever order this one response happened to arrive in.
  return [...rows].sort((a, b) => a.version_no - b.version_no);
}

/** The contract promises version_no order; a mutation that indexes by position says so out loud
 *  instead of trusting this one response to have arrived sorted. */
const byVersion = (rows: Row[]) => [...rows].sort((a, b) => a.version_no - b.version_no);

async function waitForRows(panel: Panel, n: number, arm: string) {
  try {
    await expect(panel.getByTestId("history-row")).toHaveCount(n, { timeout: 20_000 });
  } catch {
    const got = await panel.getByTestId("history-row").count();
    throw new Error(node(arm, `列表没有停在 ${n} 行，实读=${got} 行；屏上="${clean(await panel.innerText()).slice(0, 200)}"`));
  }
}

async function waitForFailed(panel: Panel, arm: string) {
  try {
    await expect(panel.getByTestId("history-failed")).toBeVisible({ timeout: 20_000 });
  } catch {
    throw new Error(node(arm, `读失败没有上屏；屏上="${clean(await panel.innerText()).slice(0, 200)}"`));
  }
}

async function openStudio(
  page: Page,
  journeyId: string,
  opts: { mutate?: (rows: Row[]) => unknown[]; failWith?: number; expectRows: number | "failed"; arm: string },
) {
  await page.addInitScript(
    ([id, loc]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", id as string);
      localStorage.setItem("citynexus.locale", loc as string);
    },
    [journeyId, LOCALE],
  );
  await page.setViewportSize({ width: 390, height: 844 });

  const calls: PlansCall[] = [];
  page.on("request", (req) => {
    if (/\/api\/v1\/journeys\/[^/]+\/plans(\?|$)/.test(req.url())) {
      calls.push({
        method: req.method(),
        idempotency_key: Object.keys(req.headers()).some((h) => h.toLowerCase() === "idempotency-key"),
      });
    }
  });

  const mutating = Boolean(opts.mutate || opts.failWith);
  if (mutating) {
    let handled = 0;
    await page.route("**/api/v1/journeys/*/plans", async (route: Route) => {
      handled += 1;
      if (opts.failWith) {
        await route.fulfill({
          status: opts.failWith,
          contentType: "application/json",
          body: JSON.stringify({ error: { code: "READ_FAILED" } }),
        });
        return;
      }
      const real = await route.fetch();
      expect(real.status(), node("stage", "被改动的那条读没有真回答过")).toBe(200);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(opts.mutate!((await real.json()) as Row[])),
      });
    });
  }

  await page.goto("/v8/authoritative", { waitUntil: "load" });
  const panel = page.getByTestId("plan-version-history");
  await expect(panel, node("stage", "版本历史面板没有挂在 authoritative 这一段里")).toBeVisible({
    timeout: 30_000,
  });
  if (opts.expectRows === "failed") {
    await waitForFailed(panel, opts.arm);
  } else {
    await waitForRows(panel, opts.expectRows, opts.arm);
  }
  if (mutating) {
    expect(calls.length, node("stage", "改动没有经过这条读，下面所有读数都是空转")).toBeGreaterThan(0);
  }
  return { panel, calls };
}

async function readRows(panel: Panel): Promise<Rendered[]> {
  const rows = panel.getByTestId("history-row");
  const n = await rows.count();
  const out: Rendered[] = [];
  for (let i = 0; i < n; i += 1) {
    const r = rows.nth(i);
    out.push({
      version: clean(await r.getByTestId("history-version").innerText()),
      source: clean(await r.getByTestId("history-source").innerText()),
      created: clean(await r.getByTestId("history-created").innerText()),
      parent: clean(await r.getByTestId("history-parent").innerText()),
      current: await r.getByTestId("history-current").count(),
    });
  }
  return out;
}

test("H1 真行程的每一份版本都在屏上：版本号、来源、写入时间、父子链逐条对得上", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  expect(sent.length, node("H1", `这一趟的版本不足三份，实读=${sent.length}`)).toBeGreaterThanOrEqual(3);
  const withParent = sent.filter((r) => r.parent_plan_version_id).length;
  expect(withParent, node("H1", `这一趟没有父子链可判，带父版本的行数=${withParent}`)).toBeGreaterThanOrEqual(1);

  const { panel } = await openStudio(page, jid, { expectRows: sent.length, arm: "H1" });
  const shown = await readRows(panel);
  expect(
    shown.length,
    node("H1", `屏上的行数与服务器给的不一致：屏=${shown.length} 服务器=${sent.length}`),
  ).toBe(sent.length);

  const byId = new Map(sent.map((r) => [r.id, r]));
  for (let i = 0; i < sent.length; i += 1) {
    const row = sent[i];
    const got = shown[i];
    const where = `第 ${i + 1} 行（版本 ${row.version_no}）`;
    expect(got.version, node("H1", `${where} 的版本号不对，实读=${got.version}`)).toBe(
      `${say("history.version")} ${row.version_no}`,
    );
    expect(got.source, node("H1", `${where} 的来源没有说人话，实读=${got.source}`)).toBe(
      say(SOURCE_KEYS[row.source]),
    );
    expect(got.created, node("H1", `${where} 的写入时间不是本地格式，实读=${got.created}`)).toBe(
      formatV8Date(row.created_at ?? undefined, LOCALE),
    );
    const parent = row.parent_plan_version_id ? byId.get(row.parent_plan_version_id) : undefined;
    expect(got.parent, node("H1", `${where} 的父链没渲染出来，实读=${got.parent}`)).toBe(
      parent ? `${say("history.parent")} ${parent.version_no}` : say("history.noParent"),
    );
  }

  /* A time that merely "renders" is not the claim. The stored instant has to reach the traveller as a
     local moment, so the serialised form must not be what is on the line. */
  const createdTexts = shown.map((r) => r.created).join(" | ");
  expect(createdTexts.includes("+00:00"), node("H1", "屏上还是原始 ISO 串")).toBe(false);
  expect(/\d{4}-\d{2}-\d{2}T/.test(createdTexts), node("H1", "屏上还是原始 ISO 串")).toBe(false);

  const currentIdx = sent.findIndex((r) => r.is_current === true);
  expect(currentIdx, node("H1", "服务器没有指出哪一份正在跑（is_current 全为假）")).toBeGreaterThanOrEqual(0);
  const markedCount = shown.filter((r) => r.current === 1).length;
  expect(markedCount, node("H1", `「行程正在跑这一份」的标记不止一个：${markedCount}`)).toBe(1);
  expect(shown[currentIdx].current, node("H1", "标记落在了不是 is_current 的那一行上")).toBe(1);
});

test("H2 份数与保留上限那句说的就是列表里真有几份，份数一变句子跟着变", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  const { panel } = await openStudio(page, jid, { expectRows: sent.length, arm: "H2" });
  const count = clean(await panel.getByTestId("history-count").innerText());
  const retention = clean(await panel.getByTestId("history-retention").innerText());
  expect(count, node("H2", `份数句与列表不一致，实读=${count}`)).toBe(say("history.count", { n: sent.length }));
  expect(retention, node("H2", `上限句与列表不一致，实读=${retention}`)).toBe(
    say("history.retention", { n: sent.length }),
  );
  // "No limit" is the answer this order owes the traveller; a hedge would leave the cap unknown.
  expect(retention.includes("没有设保留上限"), node("H2", `没有直说没有上限，实读=${retention}`)).toBe(true);

  const drop = 3;
  const fewer = sent.length - drop;
  expect(fewer, node("H2", `这一趟不够 ${drop + 1} 份，减不出对照`)).toBeGreaterThanOrEqual(2);
  const { panel: panel2 } = await openStudio(page, jid, {
    mutate: (rows) => byVersion(rows).slice(0, rows.length - drop),
    expectRows: fewer,
    arm: "H2",
  });
  const shown2 = await readRows(panel2);
  expect(shown2.length, node("H2", `列表少 ${drop} 份后行数=${shown2.length}`)).toBe(fewer);
  expect(clean(await panel2.getByTestId("history-count").innerText()), node("H2", "句子还写着旧份数")).toBe(
    say("history.count", { n: fewer }),
  );
  expect(clean(await panel2.getByTestId("history-retention").innerText()), node("H2", "上限句还写着旧份数")).toBe(
    say("history.retention", { n: fewer }),
  );
});

test("H3 反件：没有写入时间的那一行要说「没有记录」，不许拿别的时间顶上", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  expect(
    sent.filter((r) => !r.created_at).length,
    node("H3", "这一趟本来就有空时间，反件不成立"),
  ).toBe(0);

  const target = 1;
  const { panel } = await openStudio(page, jid, {
    mutate: (rows) => byVersion(rows).map((r, i) => (i === target ? { ...r, created_at: null } : r)),
    expectRows: sent.length,
    arm: "H3",
  });
  const shown = await readRows(panel);
  expect(shown.length, node("H3", `行数不对，实读=${shown.length}`)).toBe(sent.length);
  expect(shown[target].created, node("H3", `空时间被编成了别的东西，实读=${shown[target].created}`)).toBe(
    say("history.createdMissing"),
  );
  // the rows beside it keep their real times, so this arm is not passing on an empty screen
  const others = shown.map((r, i) => ({ r, i })).filter(({ i }) => i !== target);
  expect(others.length, node("H3", `对照行不够，实读=${others.length}`)).toBeGreaterThanOrEqual(2);
  for (const { r, i } of others) {
    expect(r.created, node("H3", `对照行 ${i + 1} 的时间也变了，实读=${r.created}`)).toBe(
      formatV8Date(sent[i].created_at ?? undefined, LOCALE),
    );
  }
});

test("H4 这一屏只读：没有可点的东西、没有写请求、也不借「恢复」那个词", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  const { panel, calls } = await openStudio(page, jid, { expectRows: sent.length, arm: "H4" });

  const pressable = await panel.locator("button, a, [role=button], input, select, summary").count();
  expect(pressable, node("H4", `只读屏上出现了 ${pressable} 个可交互元素`)).toBe(0);
  expect(calls.length, node("H4", `面板没有只发一条读，实发=${calls.length}`)).toBeGreaterThan(0);
  const writes = calls.filter((c) => c.method !== "GET").map((c) => c.method).join(",");
  expect(writes, node("H4", `版本列表这条路上出现了写请求：${writes}`)).toBe("");
  const keyed = calls.filter((c) => c.idempotency_key).length;
  expect(keyed, node("H4", `只读的列表带上了幂等键：${keyed} 次`)).toBe(0);

  /* Scoped to this panel on purpose: 恢复 already means autonomous recovery in this product
     (the dictionary entry keyed `nav.authoritative` in frontend/src/i18n/LocaleContext.tsx -- its
     English arm reads "Recovery" and its zh-CN arm 「恢复与执行」), and that copy belongs on its own
     screens. This order's rule is that *this* list never borrows the word. */
  const panelText = clean(await panel.innerText());
  expect(panelText.includes("恢复"), node("H4", `本屏出现了「恢复」，实读=${panelText.slice(0, 160)}`)).toBe(false);
  expect(panelText.includes("还原"), node("H4", "句子没有用「还原」这个词面")).toBe(true);
  expect(
    panelText.includes(say("history.noRestore")),
    node("H4", `没有说清为什么这里不做还原，实读=${panelText.slice(0, 160)}`),
  ).toBe(true);
});

test("H5 读不到列表时不能装作「这趟没有版本」，也不能停在「正在读」", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const { panel } = await openStudio(page, jid, { failWith: 500, expectRows: "failed", arm: "H5" });
  const failed = panel.getByTestId("history-failed");
  const said = clean(await failed.innerText());
  expect(said, node("H5", "失败句不是这台屏那句人话")).toBe(say("history.failed"));
  expect(said.includes("没有变化"), node("H5", `失败句没有说清当前方案有没有被动过，实读=${said}`)).toBe(true);
  await expect(panel.getByTestId("history-empty"), node("H5", "读失败的同时又报了「还没有版本」，两话互斥")).toHaveCount(0);
  await expect(panel.getByTestId("history-count"), node("H5", "读失败还是报了份数")).toHaveCount(0);
  await expect(panel.getByTestId("history-loading"), node("H5", "失败之后仍停在「正在读」")).toHaveCount(0);
});

test("H6 契约里的三种来源都译成人话，机器码不直接落在旅客那行上", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  const codes = Object.keys(SOURCE_KEYS).sort();
  expect(codes.join(","), node("H6", `要判的来源不是三个：${codes.join(",")}`)).toBe("INITIAL,MANUAL,REPLAN");
  expect(sent.length, node("H6", `这一趟不够三行，铺不开三种来源：${sent.length}`)).toBeGreaterThanOrEqual(3);

  const { panel } = await openStudio(page, jid, {
    mutate: (rows) => byVersion(rows).map((r, i) => (i < codes.length ? { ...r, source: codes[i] } : r)),
    expectRows: sent.length,
    arm: "H6",
  });
  const shown = await readRows(panel);
  expect(shown.length, node("H6", `行数不对，实读=${shown.length}`)).toBe(sent.length);
  for (let i = 0; i < codes.length; i += 1) {
    expect(shown[i].source, node("H6", `${codes[i]} 没有译成人话，实读=${shown[i].source}`)).toBe(
      say(SOURCE_KEYS[codes[i]]),
    );
  }
  const text = clean(await panel.innerText());
  for (const code of codes) {
    expect(text.includes(code), node("H6", `机器码 ${code} 直接上了屏`)).toBe(false);
  }
});

/**
 * H7 · reachability at 390x844, judged the way ORD-0338 甲 ruled it: a hit-test, not `scrolled > 0`.
 * The Copilot pill and the tab bar are fixed overlays, so a row sitting in the bottom band really is
 * unreadable there -- the claim is therefore that each row can be brought to *some* offset where
 * nothing but the row itself answers the hit-test at its centre. Measured on the deployment: three
 * offsets (start / center / end) per row, and the bottom offset is the one the pill owns.
 */
test("H7 每一行都能滚到一个不被悬浮球和底栏挡住的位置", async ({ page }) => {
  const jid = await sceneJourneyId(page);
  const sent = await livePlans(page, jid);
  const { panel } = await openStudio(page, jid, { expectRows: sent.length, arm: "H7" });

  const r = await panel.evaluate((root) => {
    const targets: [string, Element][] = [
      ...Array.from(root.querySelectorAll('[data-testid="history-row"]')).map(
        (el, i) => [`row ${i + 1}`, el] as [string, Element],
      ),
      ...(["history-current", "history-no-restore", "history-retention"].map(
        (id) => [id, root.querySelector(`[data-testid="${id}"]`)] as [string, Element | null],
      ) as [string, Element][]),
    ];
    const clearAt = (el: Element) => {
      const b = el.getBoundingClientRect();
      const x = Math.round(b.left + Math.min(b.width / 2, b.width - 8));
      const y = Math.round(b.top + b.height / 2);
      const hit = document.elementFromPoint(x, y);
      return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
    };
    const blocked: string[] = [];
    for (const [label, el] of targets) {
      if (!el) {
        blocked.push(`${label}:missing`);
        continue;
      }
      let clean = false;
      for (const block of ["start", "center", "end"] as const) {
        el.scrollIntoView({ block });
        if (clearAt(el)) {
          clean = true;
          break;
        }
      }
      if (!clean) blocked.push(label);
      el.scrollIntoView({ block: "start" });
    }
    return { targets: targets.length, blocked };
  });

  expect(r.blocked.join(","), node("H7", `这些内容在任何滚动位置都读不到：${r.blocked.join(",")}`)).toBe("");
  expect(r.targets, node("H7", `可滚动的目标数不对，实读=${r.targets}`)).toBeGreaterThanOrEqual(sent.length + 2);
});
