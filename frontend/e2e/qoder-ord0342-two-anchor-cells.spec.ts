/**
 * ORD-0342 — the two Q64 census cells in `PlanWithAiSheet.tsx`, closed under Q70/Q71.
 *
 * The ruler (`ui_exposure_taint_ast_r3.cjs`, published name r6, blob
 * 6a251d3a6bea70329e06581384a81d5ef54c96a6) named exactly two cells here on the authority tip:
 *   cell A  `:1048`  `<code data-testid="plan-ai-reason">` -- 技术详情 quotes the answer's own words;
 *   cell B  `:1021`  `<p>{copy.body}</p>` -- the traveller's line names a 原因编号.
 *
 * Both already do what Q67=① and Q68 require: the sentence sits under an attribution label inside the
 * fold, and the code slot holds a code or says it has none.  What they did NOT do is Q70: an element
 * carrying a field said nothing about WHICH contract field it was.  So every arm here judges the anchor,
 * and it judges it as an equality on a value read out of the DOM -- never `attribute in html`:
 *   POSITIVE  the carrier names the exact envelope key its own words came from (`error.message`,
 *             `detail`, `mission.failure_reason`, `reason_code`, `code`, `message_key`);
 *   NEGATIVE  a line this device composed names no field, and an absent code anchors nothing --
 *             无量则不贴, because an anchor for a field nobody sent is an invented fact;
 *   Q71       the answer's words may not ride into `data-*` / `aria-*` as a way of "not rendering" them,
 *             so the attribute sweep is run over every pair on the page, with a control proving the
 *             sweep can see the element under test at all.
 *
 * Cell A anchors the field NAME only and deliberately carries no `data-cnx-value`; cell B anchors
 * name+value, and its arm asserts the anchored value is the very string the sentence shows.  A mutant
 * that copies the sentence into `data-cnx-value`, one that always emits an anchor, and one that names
 * `reason_code` whatever answered are each killed by a different arm here (see `ord0342_run_arms`).
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0342-two-anchor-cells.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

const jsonBody = (o: unknown) => JSON.stringify(o);
const clean = (s: string) => s.replace(/\s+/g, " ").trim();

/* One distinctive sentence per shape, so a reading says which leg carried it.  None of them is a machine
   code, and the uppercase form is what the pre-Q68 fallback used to print -- kept for that check. */
const SENT_MESSAGE = "ord0342_error_message_服务器原话甲";
const SENT_DETAIL = "ord0342_detail_服务器原话乙";
const SENT_MISSION = "ord0342_mission_failure_reason_服务器原话丙";
const CODE_REASON = "zz_ord0342_reason_code";
const CODE_ENUM = "zz_ord0342_plain_code";
const CODE_KEY = "zz_ord0342.message.key";

type Readings = Record<string, unknown>;
const readings: Readings = {};

/** Q71: `data-*` / `aria-*` are part of the screen, and `textContent` cannot see them. */
async function domReading(page: Page): Promise<{ body: string; attrs: string; attr_pairs: number }> {
  return page.evaluate(() => {
    const pairs = Array.from(document.querySelectorAll("*")).flatMap((el) =>
      Array.from(el.attributes)
        .filter((a) => a.name.startsWith("data-") || a.name.startsWith("aria-"))
        .map((a) => `${el.tagName.toLowerCase()}|${a.name}|${a.value}`),
    );
    return {
      body: document.body.textContent ?? "",
      attrs: pairs.join("\n"),
      attr_pairs: pairs.length,
    };
  });
}

/** The two cells' own carriers, read from the live elements rather than from the page's source text. */
async function cellReadings(page: Page) {
  const card = clean(await page.getByTestId("plan-ai-result").textContent());
  const codeEl = page.getByTestId("plan-ai-reason");
  const reasonText = (await codeEl.textContent().catch(() => "")) ?? "";
  /* the body paragraph is the card's second element: `<b>title</b>` then `<p>body</p>` before the fold */
  const shape = await page.evaluate(() => {
    const cardEl = document.querySelector('[data-testid="plan-ai-result"]');
    if (!cardEl) return null;
    const kids = Array.from(cardEl.children);
    const p = kids.find((k) => k.tagName === "P") ?? null;
    const code = document.querySelector('[data-testid="plan-ai-reason"]');
    const attrsOf = (el: Element | null) =>
      el ? Array.from(el.attributes).map((a) => `${a.name}|${a.value}`).sort().join("\n") : "";
    return {
      p_text: p ? p.textContent ?? "" : "",
      p_attrs: attrsOf(p),
      p_cnx_field: p?.getAttribute("data-cnx-field") ?? null,
      p_cnx_value: p?.getAttribute("data-cnx-value") ?? null,
      code_attrs: attrsOf(code),
      code_cnx_field: code?.getAttribute("data-cnx-field") ?? null,
      code_cnx_value: code?.getAttribute("data-cnx-value") ?? null,
      has_p: p !== null,
      has_code: code !== null,
    };
  });
  return { card, reasonText, shape };
}

/** Drive the sheet to a refusal whose body is exactly `generateBody`.  The mission is made SUCCEEDED so
 *  the run really reaches the compose leg -- stopping it earlier answers a different question (measured
 *  in ORD-0323: a short-circuited mission never reaches the 原因编号 slot at all). */
async function askTheSheet(
  page: Page,
  opts: { generateBody?: string; generateStatus?: number; mission?: unknown; placesBody?: string } = {},
): Promise<void> {
  const scene = (await (await page.request.get("/api/v1/demo/scene")).json()) as { journey_id?: string };
  const id = String(scene.journey_id ?? "");
  expect(id.length > 0, node("boot", "the stage named no journey"));
  await page.addInitScript((jid) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", jid as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, id);
  await page.setViewportSize({ width: 390, height: 844 });
  const mission = opts.mission ?? {
    mission: { id: "ord0342-mission", status: "SUCCEEDED", failure_reason: null, reasoning_summary: null },
  };
  await page.route("**/api/v1/journeys/*/agent/commands", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    await r.fulfill({ status: 200, contentType: "application/json", body: jsonBody(mission) });
  });
  await page.route("**/api/v1/journeys/*/plans/generate", async (r: Route) => {
    if (r.request().method() !== "POST") return r.fallback();
    await r.fulfill({
      status: opts.generateStatus ?? 503,
      contentType: "application/json",
      body: opts.generateBody ?? jsonBody({}),
    });
  });
  await page.route("**/api/v1/journeys/*/plans", async (r: Route) => {
    if (r.request().method() !== "GET") return r.fallback();
    await r.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/places**", async (r: Route) => {
    await r.fulfill({
      status: 200,
      contentType: "application/json",
      body: opts.placesBody ?? jsonBody([{ id: "p1", name: "天津西站" }, { id: "p2", name: "北京南站" }]),
    });
  });
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("hub-plan-with-ai").click();
  await page.getByTestId("plan-ai-input").fill("帮我排三天，喜欢美食和特色景点。");
  await page.getByTestId("plan-ai-submit").click();
  await expect(page.getByTestId("plan-ai-result")).toBeVisible({ timeout: 60_000 });
}

const armOf = (title: string): string => /^N8_([AB]\d)_/.exec(title)?.[1] ?? title.split("_")[0];

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const body: Readings = { ...(readings[arm] ?? {}) };
  body.nodeid = `${info.file}:${info.line} > ${info.title}`;
  body.test_status = info.status;
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 600);
  }
  writeFileSync(
    evidencePath(`ord0342/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, ...body }, null, 1),
    "utf-8",
  );
});

/* ------------------------------------------------------------------ cell A, `:1048` 技术详情 */

test("N8_A1_the_folded_sentence_names_error_message_as_its_field — 折叠里那句原话要写明它来自 error.message", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "A1";
  await askTheSheet(page, { generateBody: jsonBody({ error: { message: SENT_MESSAGE } }) });
  const { card, reasonText, shape } = await cellReadings(page);
  const dom = await domReading(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1048",
    fold_text: reasonText,
    fold_text_equals_the_sentence: reasonText === SENT_MESSAGE,
    code_cnx_field: shape?.code_cnx_field ?? null,
    code_attrs: shape?.code_attrs ?? "",
    card_text: card,
    attribute_pairs_swept: dom.attr_pairs,
    sweep_sees_the_tested_code: dom.attrs.includes("|data-testid|plan-ai-reason"),
    sentence_in_any_attribute: dom.attrs.includes(SENT_MESSAGE),
    sentence_in_cnx_value: shape?.code_cnx_value ?? null,
  };
  /* Q67=① is unchanged by this order: the fold still holds the reply's words, whole. */
  expect(reasonText, node(arm, `技术详情不再是这句原话本身（"${reasonText.slice(0, 200)}"）`)).toBe(SENT_MESSAGE);
  expect(shape?.code_cnx_field ?? null,
    node(arm, `Q70: 承载这句话的元素没有写明它来自哪个契约字段（属性 ${String(shape?.code_attrs)}）`)).toBe("error.message");
  /* Q71: naming the field is allowed; carrying the sentence in an attribute is not. */
  expect(shape?.code_cnx_value ?? null,
    node(arm, `Q71: 原话被复制进 data-cnx-value（"${String(shape?.code_cnx_value)}"）`)).toBe(null);
  expect(dom.attrs.includes(SENT_MESSAGE),
    node(arm, `Q71: 原话出现在某个 data-/aria- 取值里（${String(dom.attr_pairs)} 对已扫）`)).toBe(false);
  /* the control that keeps the sweep from being decoration */
  expect(dom.attrs.includes("|data-testid|plan-ai-reason"),
    node(arm, `属性扫到 ${String(dom.attr_pairs)} 对却认不出被检元素，这条读数没有对象`)).toBe(true);
  expect(/服务器给出的原因|Reason the server gave/.test(card),
    node(arm, `归属标签没了，屏上的句子成了无主的话（${card.slice(0, 220)}）`)).toBe(true);
});

test("N8_A2_the_folded_sentence_from_detail_names_detail — 来自 detail 的那句要写明 detail，不能沿用上一句的名字", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "A2";
  await askTheSheet(page, { generateBody: jsonBody({ detail: SENT_DETAIL }) });
  const { reasonText, shape } = await cellReadings(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1048",
    fold_text: reasonText,
    code_cnx_field: shape?.code_cnx_field ?? null,
    code_cnx_value: shape?.code_cnx_value ?? null,
    code_attrs: shape?.code_attrs ?? "",
  };
  expect(reasonText, node(arm, `折叠里不是这句 detail 原话（"${reasonText.slice(0, 200)}"）`)).toBe(SENT_DETAIL);
  expect(shape?.code_cnx_field ?? null,
    node(arm, `Q70: 这句来自 detail，锚却写着 "${String(shape?.code_cnx_field)}"（属性 ${String(shape?.code_attrs)}）`)).toBe("detail");
  expect(shape?.code_cnx_value ?? null,
    node(arm, `Q71: detail 原话进了属性（"${String(shape?.code_cnx_value)}"）`)).toBe(null);
});

test("N8_A3_a_line_this_device_wrote_anchors_nothing — 设备自己写的那句不许借一个契约字段名", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "A3";
  /* an envelope with neither message nor detail: the note is the device's own reading. */
  await askTheSheet(page, { generateBody: jsonBody({ error: { hint: "no sentence in this envelope" } }) });
  const { card, reasonText, shape } = await cellReadings(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1048",
    fold_text: reasonText,
    code_cnx_field: shape?.code_cnx_field ?? null,
    code_attrs: shape?.code_attrs ?? "",
    card_text: card,
    origin_attribute: await page.getByTestId("plan-ai-detail-kind").getAttribute("data-reason-origin"),
  };
  expect(shape?.code_cnx_field ?? null,
    node(arm, `设备自己写的一句挂上了契约字段名 "${String(shape?.code_cnx_field)}"（属性 ${String(shape?.code_attrs)}）`)).toBe(null);
  expect(reasonText.includes("POST /plans/generate -> HTTP 503"),
    node(arm, `这一格不再说明这台设备自己的读数了（"${reasonText.slice(0, 200)}"）`)).toBe(true);
});

test("N8_A4_the_missions_stored_reason_names_mission_failure_reason — 行程存下的那句要写明 mission.failure_reason", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "A4";
  await askTheSheet(page, {
    mission: { mission: { id: "ord0342-failed", status: "FAILED", failure_reason: SENT_MISSION,
                          reasoning_summary: null } },
    generateBody: jsonBody({}),
  });
  const { reasonText, shape } = await cellReadings(page);
  const dom = await domReading(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1048",
    fold_text: reasonText,
    code_cnx_field: shape?.code_cnx_field ?? null,
    code_cnx_value: shape?.code_cnx_value ?? null,
    sentence_in_any_attribute: dom.attrs.includes(SENT_MISSION),
    attribute_pairs_swept: dom.attr_pairs,
  };
  expect(reasonText, node(arm, `折叠里不是行程存下的那句（"${reasonText.slice(0, 200)}"）`)).toBe(SENT_MISSION);
  expect(shape?.code_cnx_field ?? null,
    node(arm, `Q70: mission.failure_reason 这一枚没有锚（"${String(shape?.code_cnx_field)}"）`)).toBe("mission.failure_reason");
  /* the same Q71 leg A1 and A2 carry: this sentence reaches the fold through a different site, so an
     attribute copy of it would have gone unmeasured had the leg lived only on the envelope legs. */
  expect(shape?.code_cnx_value ?? null,
    node(arm, `Q71: 行程存下的那句被复制进 data-cnx-value（"${String(shape?.code_cnx_value)}"）`)).toBe(null);
  expect(dom.attrs.includes(SENT_MISSION),
    node(arm, `Q71: 这句出现在某个 data-/aria- 取值里（${String(dom.attr_pairs)} 对已扫）`)).toBe(false);
});

/* ------------------------------------------------------------------ cell B, `:1021` 编号那一行 */

test("N8_B1_the_number_line_names_reason_code_and_carries_the_same_string — 编号那一行：锚写的键要与屏上的编号一模一样", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "B1";
  await askTheSheet(page, { generateBody: jsonBody({ error: { reason_code: CODE_REASON } }) });
  const { card, shape } = await cellReadings(page);
  const shown = CODE_REASON.toUpperCase();
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1021",
    card_text: card,
    code_shown: shown,
    p_text: shape?.p_text ?? "",
    p_attrs: shape?.p_attrs ?? "",
    p_cnx_field: shape?.p_cnx_field ?? null,
    p_cnx_value: shape?.p_cnx_value ?? null,
    anchor_value_inside_the_line: (shape?.p_text ?? "").includes(shown),
  };
  expect(shape?.p_cnx_field ?? null,
    node(arm, `Q70: 写着编号的那一行没有说明编号来自哪个键（属性 ${String(shape?.p_attrs)}）`)).toBe("reason_code");
  expect(shape?.p_cnx_value ?? null,
    node(arm, `锚里的编号不是屏上这一行写的那个（"${String(shape?.p_cnx_value)}" / "${(shape?.p_text ?? "").slice(0, 160)}"）`)).toBe(shown);
  expect((shape?.p_text ?? "").includes(shown),
    node(arm, `锚有值而这一行没有这个编号，两者各说各的（"${(shape?.p_text ?? "").slice(0, 200)}"）`)).toBe(true);
  expect(card.includes(shown), node(arm, `屏上的编号没了（${card.slice(0, 200)}）`)).toBe(true);
});

test("N8_B2_a_plain_code_anchor_says_code_not_the_first_key_in_the_list — 三枚键各自成锚：这一枚来自 code", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "B2";
  /* both cells at once: the code keys the visible line, the detail keys the folded sentence. */
  await askTheSheet(page, { generateBody: jsonBody({ error: { code: CODE_ENUM }, detail: SENT_DETAIL }) });
  const { card, reasonText, shape } = await cellReadings(page);
  const shown = CODE_ENUM.toUpperCase();
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1021",
    card_text: card,
    p_cnx_field: shape?.p_cnx_field ?? null,
    p_cnx_value: shape?.p_cnx_value ?? null,
    p_attrs: shape?.p_attrs ?? "",
    code_cnx_field: shape?.code_cnx_field ?? null,
    fold_text: reasonText,
  };
  expect(shape?.p_cnx_field ?? null,
    node(arm, `编号来自 code，锚却写着 "${String(shape?.p_cnx_field)}"（属性 ${String(shape?.p_attrs)}）`)).toBe("code");
  expect(shape?.p_cnx_value ?? null, node(arm, `锚里的编号不等于屏上的 ${shown}`)).toBe(shown);
  expect(card.includes(shown), node(arm, `屏上没有这个编号（${card.slice(0, 200)}）`)).toBe(true);
  expect(shape?.code_cnx_field ?? null,
    node(arm, `同一屏上折叠里的句子该写 detail，却写着 "${String(shape?.code_cnx_field)}"`)).toBe("detail");
  expect(reasonText, node(arm, `折叠里不是这句 detail 原话（"${reasonText.slice(0, 160)}"）`)).toBe(SENT_DETAIL);
});

test("N8_B3_message_key_is_anchored_under_its_own_name — 第三枚键：message_key 要按自己的名字上锚", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "B3";
  await askTheSheet(page, { generateBody: jsonBody({ error: { message_key: CODE_KEY } }) });
  const { card, shape } = await cellReadings(page);
  const shown = CODE_KEY.toUpperCase();
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1021",
    card_text: card,
    p_cnx_field: shape?.p_cnx_field ?? null,
    p_cnx_value: shape?.p_cnx_value ?? null,
    p_attrs: shape?.p_attrs ?? "",
  };
  expect(shape?.p_cnx_field ?? null,
    node(arm, `message_key 这一枚的锚不对（"${String(shape?.p_cnx_field)}" / ${String(shape?.p_attrs)}）`)).toBe("message_key");
  expect(shape?.p_cnx_value ?? null, node(arm, `锚值与屏上的编号不等（"${String(shape?.p_cnx_value)}"）`)).toBe(shown);
  expect(card.includes(shown), node(arm, `屏上没有这个编号（${card.slice(0, 200)}）`)).toBe(true);
});

test("N8_B4_no_code_no_anchor — 服务器没给编号：屏上说没有，锚也不许凭空出现", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "B4";
  await askTheSheet(page, {
    generateBody: jsonBody({ error: { hint: "no machine code here" }, detail: SENT_DETAIL }),
  });
  const { card, shape } = await cellReadings(page);
  const dom = await domReading(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1021",
    card_text: card,
    p_cnx_field: shape?.p_cnx_field ?? null,
    p_cnx_value: shape?.p_cnx_value ?? null,
    p_attrs: shape?.p_attrs ?? "",
    no_code_wording_on_screen: /没有给出具体原因编号|（没给编号）|[(]none given[)]/.test(card),
    sentence_in_any_attribute: dom.attrs.includes(SENT_DETAIL),
    attribute_pairs_swept: dom.attr_pairs,
  };
  expect(shape?.p_cnx_field ?? null,
    node(arm, `没有编号却贴了锚 "${String(shape?.p_cnx_field)}"（属性 ${String(shape?.p_attrs)}）`)).toBe(null);
  expect(shape?.p_cnx_value ?? null, node(arm, `没有编号而锚里有值（"${String(shape?.p_cnx_value)}"）`)).toBe(null);
  expect(/没有给出具体原因编号|（没给编号）|[(]none given[)]/.test(card),
    node(arm, `这一屏不再说明"没有编号"了（${card.slice(0, 220)}）`)).toBe(true);
  expect(dom.attrs.includes(SENT_DETAIL),
    node(arm, `Q71: 原话出现在某个属性取值里（${String(dom.attr_pairs)} 对已扫）`)).toBe(false);
});

test("N8_B5_a_code_this_screen_invented_anchors_nothing — 本屏自造的 CATALOG 名不是服务器给的编号，不许有契约锚", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const arm = "B5";
  /* one place is below the server's own floor, so this screen names the state itself and returns early. */
  await askTheSheet(page, { placesBody: jsonBody([{ id: "p1", name: "天津西站" }]) });
  const { card, shape } = await cellReadings(page);
  readings[arm] = {
    cell: "PlanWithAiSheet.tsx:1021",
    card_text: card,
    result_status: await page.getByTestId("plan-ai-result").getAttribute("data-result-status"),
    p_cnx_field: shape?.p_cnx_field ?? null,
    p_cnx_value: shape?.p_cnx_value ?? null,
    p_attrs: shape?.p_attrs ?? "",
    p_text: shape?.p_text ?? "",
  };
  expect(readings[arm].result_status, node(arm, "这一形没有走到本屏自造编号的那一支，锚的缺席就什么都没量")).toBe(
    "BLOCKED:CATALOG_INSUFFICIENT",
  );
  expect(shape?.p_cnx_field ?? null,
    node(arm, `本屏自造的名字挂上了契约字段锚 "${String(shape?.p_cnx_field)}"（属性 ${String(shape?.p_attrs)}）`)).toBe(null);
  expect(shape?.p_cnx_value ?? null, node(arm, `自造的名字有了锚值（"${String(shape?.p_cnx_value)}"）`)).toBe(null);
});
