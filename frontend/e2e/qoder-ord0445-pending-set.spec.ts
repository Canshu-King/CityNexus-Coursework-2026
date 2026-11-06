/**
 * ORD-0445 -- 「待规划」 as its own data state, judged on the screen.
 *
 * Arms, in the order the order asks for them:
 *  A1 the set is countable and the number on the line is the number of rows rendered;
 *  A2 the filter sheet is a list of facets built from the rows, each with its own count, plus 全部;
 *  A3 filtering to one facet makes the list smaller and not empty, and the count line says so;
 *  A4 重置 returns the list to exactly what it was;
 *  A5 the Chinese arm and the English arm are each read off the rendered text, sentence by sentence;
 *  A6 中文臂不得比修前多字: the same screen without this panel (the tip's own bundle, built on this
 *     lane's tree with only the registry entry removed) has to produce byte-identical text;
 *  A7 the empty set is a state with a door: 「这一程没有待规划的地点」 plus an action that lands
 *     somewhere that names itself -- and no 「暂无数据」 anywhere;
 *  A8 the panel does not escape its card at 320/360/390/414 and its touch targets meet 44 px;
 *  A9 the sentence naming where a place goes when a day stops holding it is on the screen;
 *  A10 a door that never answers is named as that, and is not allowed to read as an empty set.
 *
 * Assertions read rendered text, never a `data-*` attribute, because a traveller reads the sentence.
 * Where a machine token is shown at all it is inside the technical-details disclosure, and that is
 * asserted as a separate reading rather than used as evidence for anything above it.
 *
 * Nothing here is mocked. The data comes from two real doors on this lane's own backend (:8011, over
 * a read-only snapshot of the shared preview database), and the states are produced by product calls:
 * `POST /journeys/{id}/real-city/discover` put three places into the pending set, and a journey with
 * no plan and no captures is the empty arm. A10 is the one arm that injects a fault, and it does so
 * at the transport (the browser refuses to send the request) because the state it judges -- 「没读到」
 * -- cannot be produced on demand otherwise; it is labelled as instrument-injected in the receipt.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0445-pending-set.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const RUN = process.env.CITYNEXUS_E2E_RUN_ID ?? "local";

/* Both journeys are named by the runner so this file holds no id of its own and cannot be quietly
   re-pointed at somebody else's data. */
const SET_JOURNEY = process.env.CITYNEXUS_N2B_JOURNEY ?? "";
const EMPTY_JOURNEY = process.env.CITYNEXUS_N2B_EMPTY_JOURNEY ?? "";
const BEFORE_URL = process.env.CITYNEXUS_N2B_BEFORE_URL ?? "";

const readings: Record<string, unknown> = {};
const consoleErrors: Record<string, string[]> = {};

function armOf(title: string): string {
  return /^([A-Z]\d+)/.exec(title)?.[1] ?? title.slice(0, 2);
}

test.afterEach(({}, info) => {
  const arm = armOf(info.title);
  const body: Record<string, unknown> = {
    nodeid: `${info.file}:${info.line} > ${info.title}`,
    test_status: info.status,
    console_errors: consoleErrors[arm] ?? [],
    journeys: { set: SET_JOURNEY, empty: EMPTY_JOURNEY, before_url: BEFORE_URL },
  };
  if (info.status !== "passed") {
    body.error_head = String(info.error?.message ?? "").replace(/\s+/g, " ").slice(0, 900);
  }
  readings[arm] = { ...(readings[arm] as object), ...body };
  writeFileSync(
    evidencePath(`ord0445/${RUN}__${arm}.json`),
    JSON.stringify({ run_id: RUN, arm, spec: FILE, reading: readings[arm] }, null, 1),
    "utf-8",
  );
});

async function boot(page: Page, journeyId: string, locale: string, w = 390, h = 844) {
  consoleErrors[armOf(test.info()?.title ?? "")] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const arm = armOf(test.info()?.title ?? "");
    consoleErrors[arm] = (consoleErrors[arm] ?? []).concat(message.text().slice(0, 200));
  });
  await page.addInitScript(
    ([jid, loc]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.v8.journey_id", jid);
      localStorage.setItem("citynexus.locale", loc);
    },
    [journeyId, locale],
  );
  page.setDefaultTimeout(30_000);
  await page.setViewportSize({ width: w, height: h });
}

async function openHub(page: Page, journeyId: string, locale: string, w = 390) {
  await boot(page, journeyId, locale, w);
  await page.goto("/v8/hub", { waitUntil: "load" });
  const panel = page.getByTestId("pending-set");
  await expect(
    panel,
    node("boot", `the 待规划 panel is not on 今天 for ${journeyId.slice(0, 8)}… / ${locale}`),
  ).toHaveCount(1);
  await expect(
    panel.getByTestId("pending-loading"),
    node("boot", "the panel never stopped saying it was reading"),
  ).toHaveCount(0);
  return panel;
}

/** Everything this screen renders as numbers and rows, read off the text a traveller sees. */
async function panelReading(page: Page, panel: import("@playwright/test").Locator) {
  return panel.evaluate((root: HTMLElement) => {
    const text = (el: Element | null) =>
      (el?.textContent ?? "").replace(/\s+/g, " ").trim();
    const rows = Array.from(root.querySelectorAll('[data-testid="pending-row"]'));
    const countLine = text(root.querySelector('[data-testid="pending-count"]'));
    /* The first number on the count line, which is the claim this screen makes about how many rows
       it is showing.  Read as text, not from an attribute. */
    const firstNumber = /(\d+)/.exec(countLine.replace(/[^\d]/g, " "))?.[1] ?? null;
    return {
      count_line: countLine,
      count_line_first_number: firstNumber === null ? null : Number(firstNumber),
      rendered_rows: rows.length,
      row_names: rows.map((row) => text(row.querySelector('[data-testid="pending-row-name"]'))),
      row_causes: rows.map((row) => text(row.querySelector('[data-testid="pending-row-cause"]'))),
      row_categories: rows.map((row) => text(row.querySelector('[data-testid="pending-row-category"]'))),
      section_text: text(root),
      empty_text: text(root.querySelector('[data-testid="pending-empty"]')),
      buttons: Array.from(root.querySelectorAll("button")).map((button) => ({
        testid: button.getAttribute("data-testid") ?? "",
        label: text(button),
      })),
    };
  });
}

test.describe("ORD-0445 待规划 set, filter sheet and empty state", () => {
  test("A1 the set is countable and the count line counts the rows it renders", async ({ page }) => {
    expect(SET_JOURNEY.length > 0, node("A1", "CITYNEXUS_N2B_JOURNEY was not named"));
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    const reading = await panelReading(page, panel);
    expect(
      reading.rendered_rows,
      node("A1", "the set is empty on the journey the lane prepared for it -- see prep_receipt_r1.json"),
    ).toBeGreaterThan(0);
    expect(
      reading.count_line_first_number,
      node("A1", `count line "${reading.count_line}" does not carry the row count ${reading.rendered_rows}`),
    ).toBe(reading.rendered_rows);
    for (const name of reading.row_names) {
      expect(name.length, node("A1", "a row rendered no name")).toBeGreaterThan(0);
    }
    expect(
      reading.section_text.includes("这一程没有任何一天排它"),
      node("A1", "the rows do not say why they are here"),
    ).toBe(true);
    readings.A1 = reading;
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A1_zh.png`) });
  });

  test("A2 the sheet lists 全部 plus one row per category the rows themselves name", async ({ page }) => {
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    await panel.getByTestId("pending-filter-open").click();
    const sheet = panel.getByTestId("pending-filter-sheet");
    await expect(sheet, node("A2", "the filter sheet did not open")).toHaveCount(1);

    const rows = await sheet.locator('[data-testid="pending-filter-row"]').evaluateAll((els) =>
      els.map((el) => {
        const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
        const number = /(\d+)(?!.*\d)/.exec(text)?.[1] ?? null;
        return {
          choice: el.getAttribute("data-choice") ?? "",
          text,
          number: number === null ? null : Number(number),
        };
      }),
    );
    const reading = await panelReading(page, panel);
    const total = reading.rendered_rows;
    expect(rows[0]?.choice, node("A2", "the first facet row is not 全部")).toBe("all");
    expect(
      rows[0]?.number,
      node("A2", `全部 does not carry the whole set (${rows[0]?.text})`),
    ).toBe(total);
    const facets = rows.slice(1);
    expect(facets.length, node("A2", "no category facet was built")).toBeGreaterThan(0);
    const categoryCounts = new Map<string, number>();
    for (const name of reading.row_categories) {
      categoryCounts.set(name, (categoryCounts.get(name) ?? 0) + 1);
    }
    for (const facet of facets) {
      const expected = categoryCounts.get(facet.text.replace(/\s*\d+\s*$/, "").trim()) ?? -1;
      expect(facet.number, node("A2", `facet "${facet.text}" count disagrees with the rows (${expected})`)).toBe(
        expected,
      );
      expect(facet.number ?? 0, node("A2", `facet "${facet.text}" lists zero rows`)).toBeGreaterThan(0);
    }
    readings.A2 = { rows, total, reading_categories: reading.row_categories };
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A2_sheet_zh.png`) });
  });

  test("A3 filtering to one facet makes the list smaller, not empty", async ({ page }) => {
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    const before = await panelReading(page, panel);
    await panel.getByTestId("pending-filter-open").click();
    const sheet = panel.getByTestId("pending-filter-sheet");
    const facets = (await sheet.locator('[data-testid="pending-filter-row"]').all()).slice(1);
    const chosen = facets[0];
    const choice = await chosen.getAttribute("data-choice");
    await chosen.click();
    await sheet.getByTestId("pending-filter-done").click();

    await expect(
      panel.getByTestId("pending-filter-sheet"),
      node("A3", "完成 did not close the sheet"),
    ).toHaveCount(0);
    const after = await panelReading(page, panel);
    expect(after.rendered_rows, node("A3", `filtering to ${choice} changed nothing`)).toBeLessThan(
      before.rendered_rows,
    );
    expect(
      after.rendered_rows,
      node("A3", `filtering to ${choice} emptied the list, which the sheet must not offer`),
    ).toBeGreaterThan(0);
    expect(
      after.count_line_first_number,
      node("A3", `count line "${after.count_line}" disagrees with ${after.rendered_rows} rows`),
    ).toBe(after.rendered_rows);
    for (const name of after.row_names) {
      expect(
        before.row_names.includes(name),
        node("A3", `a filtered-in row "${name}" was not in the unfiltered list`),
      ).toBe(true);
    }
    readings.A3 = { choice, before, after };
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A3_filtered_zh.png`) });
  });

  test("A4 重置 returns exactly the original list", async ({ page }) => {
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    const original = await panelReading(page, panel);
    await panel.getByTestId("pending-filter-open").click();
    const sheet = panel.getByTestId("pending-filter-sheet");
    const facets = (await sheet.locator('[data-testid="pending-filter-row"]').all()).slice(1);
    await facets[0].click();
    await sheet.getByTestId("pending-filter-done").click();
    const filtered = await panelReading(page, panel);
    expect(filtered.rendered_rows).toBeLessThan(original.rendered_rows);

    await panel.getByTestId("pending-filter-open").click();
    const reopened = panel.getByTestId("pending-filter-sheet");
    await reopened.getByTestId("pending-filter-reset").click();
    /* 重置 must be visible as a state, not only as an effect: the 全部 row is the pressed one again. */
    const pressed = await reopened
      .locator('[data-testid="pending-filter-row"][aria-pressed="true"]')
      .getAttribute("data-choice");
    expect(pressed, node("A4", `after 重置 the pressed facet is "${pressed}", not 全部`)).toBe("all");
    await reopened.getByTestId("pending-filter-done").click();
    const restored = await panelReading(page, panel);
    expect(restored.row_names, node("A4", "重置 did not return the original rows")).toEqual(
      original.row_names,
    );
    expect(restored.count_line_first_number).toBe(original.rendered_rows);
    readings.A4 = { original, filtered, restored };
  });

  test("A5 both arms read the same screen, in their own words, off the rendered text", async ({
    page,
  }) => {
    const perLocale: Record<string, unknown> = {};
    for (const locale of ["zh-CN", "en"]) {
      const panel = await openHub(page, SET_JOURNEY, locale);
      const reading = await panelReading(page, panel);
      /* Read as sentences, not as keys: a leaked key or an untranslated fallback is a defect. */
      expect(
        reading.section_text.includes("pending."),
        node("A5", `${locale}: a raw copy key reached the screen`),
      ).toBe(false);
      if (locale === "en") {
        expect(
          /[\u4e00-\u9fff]/.test(reading.section_text),
          node("A5", `en arm renders CJK: ${reading.section_text.slice(0, 160)}`),
        ).toBe(false);
        expect(reading.section_text, node("A5", "en arm lost its title")).toContain("Not on a day yet");
        expect(reading.section_text, node("A5", "en arm lost its count line")).toContain(
          "places not on a day",
        );
        expect(reading.section_text, node("A5", "en arm lost the reason")).toContain(
          "no day of this trip holds it",
        );
      } else {
        expect(reading.section_text, node("A5", "zh arm lost its title")).toContain("待规划");
        expect(reading.section_text, node("A5", "zh arm lost its count line")).toContain(
          "个地点没有排在某一天",
        );
      }
      perLocale[locale] = {
        count_line: reading.count_line,
        rendered_rows: reading.rendered_rows,
        row_names: reading.row_names,
        section_text: reading.section_text,
      };
      await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A5_${locale}.png`) });
      /* The same rows in the same order in both languages: only the words may differ. */
      if (locale === "zh-CN") readings.A5_zh_names = reading.row_names;
      else {
        expect(
          reading.row_names,
          node("A5", "the two arms list different places, so one of them is not reading the server"),
        ).toEqual(readings.A5_zh_names);
      }
    }
    readings.A5 = perLocale;
  });

  test("A6 中文臂不得比修前多字: this screen without the panel is byte-identical", async ({
    page,
  }) => {
    expect(BEFORE_URL.length > 0, node("A6", "CITYNEXUS_N2B_BEFORE_URL was not named"));
    const zh = "zh-CN";
    await boot(page, SET_JOURNEY, zh);
    await page.goto(BEFORE_URL + "/v8/hub", { waitUntil: "load" });
    await page.waitForSelector('[data-testid^="v8-"]', { timeout: 20_000 });
    await page.waitForTimeout(2_500);
    const before = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim());

    await boot(page, SET_JOURNEY, zh);
    await page.goto("/v8/hub", { waitUntil: "load" });
    await page.waitForSelector('[data-testid="pending-set"]', { timeout: 20_000 });
    await page.waitForTimeout(2_500);
    /* Excise exactly the block this order added, then compare.  Anything left over is a change to
       words that were already on the screen.  Both sides are read from the live document: a cloned
       node has no layout, so its `innerText` also un-hides the drawer labels and the comparison
       would judge two different renderings. */
    const live = await page.evaluate(() => {
      const root = document.querySelector('[data-testid="pending-set"]');
      return {
        full: document.body.innerText.replace(/\s+/g, " ").trim(),
        panel: (root?.innerText ?? "").replace(/\s+/g, " ").trim(),
        panel_found: !!root,
      };
    });
    expect(live.panel_found, node("A6", "the panel is not on the screen, so there is nothing to excise")).toBe(
      true,
    );
    const after = live.full.replace(live.panel, "").replace(/\s+/g, " ").trim();
    const delta = {
      before_chars: before.length,
      after_chars_with_panel: live.full.length,
      panel_chars: live.panel.length,
      after_chars_minus_panel: after.length,
    };
    readings.A6 = {
      ...delta,
      equal: before === after,
      first_difference: before === after ? null : firstDifference(before, after),
    };
    expect(
      before === after,
      node(
        "A6",
        `the Chinese arm gained words outside the new panel: ${JSON.stringify(
          (readings.A6 as { first_difference?: string }).first_difference,
        )}`,
      ),
    ).toBe(true);
  });

  test("A7 an empty set is a state with a door, and no 「暂无数据」", async ({ page }) => {
    expect(EMPTY_JOURNEY.length > 0, node("A7", "CITYNEXUS_N2B_EMPTY_JOURNEY was not named"));
    const panel = await openHub(page, EMPTY_JOURNEY, "zh-CN");
    const reading = await panelReading(page, panel);
    expect(reading.rendered_rows, node("A7", "the empty journey still rendered rows")).toBe(0);
    expect(
      reading.empty_text.includes("这一程没有待规划的地点"),
      node("A7", `the empty state is not the named one: "${reading.empty_text}"`),
    ).toBe(true);
    expect(
      reading.section_text.includes("暂无数据"),
      node("A7", "the screen answered with 暂无数据"),
    ).toBe(false);
    const action = panel.getByTestId("pending-empty-action");
    await expect(action, node("A7", "the empty state carries no action")).toHaveCount(1);
    const label = (await action.innerText()).trim();
    expect(
      label.includes("去城市探索"),
      node("A7", `the action does not name its destination in the nav's own word: ${label}`),
    ).toBe(true);
    /* Scroll it into the viewport first: 今天 is a long page, and a control measured at a centre
       outside the viewport hit-tests to nothing -- the ORD-0261 mistake, where "not reachable" was
       really "not scrolled there yet".  The scroll position is part of the reading. */
    await action.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const scrollY = await page.evaluate(() => Math.round(window.scrollY));
    const box = await action.boundingBox();
    const centre = [Math.round(box!.x + box!.width / 2), Math.round(box!.y + box!.height / 2)];
    const hit = await page.evaluate((point) => {
      const own = document.querySelector('[data-testid="pending-empty-action"]');
      const found = document.elementFromPoint(point[0], point[1]);
      return {
        tag: `${found?.tagName}.${found?.className}`.slice(0, 60),
        is_own: !!own && (own === found || own.contains(found)),
      };
    }, centre as never);
    expect(hit.is_own, node("A7", `the action's centre is covered by ${hit.tag}`)).toBe(true);

    await action.click();
    await page.waitForURL("**/v8/discover", { timeout: 20_000 });
    /* Read the destination's own heading and its own named panel rather than the URL: a route that
       lands on an empty shell is not a door.  The words are the measured ones -- the screen this
       route shows opens with 「准备目的地」, while the nav calls the same route 城市探索, and the
       action uses the nav's word. */
    const destination = (await page.locator(".v8-kicker").first().innerText()).replace(/\s+/g, " ");
    const landedPanel = page.getByTestId("prep-panel");
    await expect(
      landedPanel,
      node("A7", "the destination screen mounted no panel of its own, so the action landed on a shell"),
    ).toHaveCount(1);
    const landedTestids = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-testid]"))
        .map((el) => el.getAttribute("data-testid") ?? "")
        .filter((id) => id.startsWith("prep-") || id.startsWith("discovery-") || id === "city-explorer")
        .slice(0, 8),
    );
    readings.A7 = {
      ...reading,
      action_label: label,
      action_box: box,
      scroll_y_at_hit_test: scrollY,
      hit,
      landed_url: page.url(),
      landed_kicker: destination,
      landed_named_testids: landedTestids,
    };
    expect(destination.length, node("A7", "the destination screen names nothing")).toBeGreaterThan(0);
    expect(
      landedTestids.length,
      node("A7", "the destination screen shows no discovery panel, so the action does not reach one"),
    ).toBeGreaterThan(0);
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A7_empty_landed.png`) });
  });

  test("A8 the panel stays inside its card, and its touch targets stay 44 px", async ({ page }) => {
    const perWidth: Record<string, unknown> = {};
    for (const w of [320, 360, 390, 414]) {
      const panel = await openHub(page, SET_JOURNEY, "zh-CN", w);
      await panel.getByTestId("pending-filter-open").click();
      perWidth[`w${w}`] = await panel.evaluate((root: HTMLElement) => {
        const hb = root.getBoundingClientRect();
        let worst = 0;
        let offender = "";
        root.querySelectorAll("*").forEach((raw) => {
          const el = raw as HTMLElement;
          const b = el.getBoundingClientRect();
          const over = Math.max(b.right - hb.right, hb.left - b.left, el.scrollWidth - el.clientWidth);
          if (over > worst) {
            worst = Math.round(over);
            offender = `${el.tagName}.${el.className}`.slice(0, 60);
          }
        });
        const touch = Array.from(root.querySelectorAll("button")).map((button) => {
          const b = button.getBoundingClientRect();
          return { label: (button.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 24), height: Math.round(b.height) };
        });
        return { overflow_px: worst, offender, buttons: touch, box: [hb.left, hb.top, hb.width, hb.height].map(Math.round) };
      });
      await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A8_${w}.png`) });
      const row = perWidth[`w${w}`] as { overflow_px: number; offender: string };
      expect(row.overflow_px, node("A8", `${w}px: content escapes the card by ${row.overflow_px}px (${row.offender})`)).toBeLessThanOrEqual(1);
    }
    /* Amendment F-2: 44 px is the mobile contract, and 筛选 / 重置 / 完成 are this screen's primary
       actions on a phone. */
    const phone = perWidth.w390 as { buttons: Array<{ label: string; height: number }> };
    const under = phone.buttons.filter((button) => button.height < 44);
    readings.A8 = { per_width: perWidth, under_44: under };
    expect(under, node("A8", `controls below 44px: ${JSON.stringify(under)}`)).toEqual([]);
  });

  test("A9 the screen states where a place goes when a day stops holding it", async ({ page }) => {
    const arms: Record<string, unknown> = {};
    for (const locale of ["zh-CN", "en"]) {
      const panel = await openHub(page, SET_JOURNEY, locale);
      const rule = await panel.getByTestId("pending-delete-day-rule").innerText();
      const door = await panel.getByTestId("pending-no-place-door").innerText();
      arms[locale] = { rule: rule.replace(/\s+/g, " ").trim(), door: door.replace(/\s+/g, " ").trim() };
      expect(
        arms[locale].rule.length > 0,
        node("A9", `${locale}: the destination sentence is not on the screen`),
      ).toBe(true);
      for (const forbidden of ["恢复"]) {
        expect(
          String(arms[locale].rule) + String(arms[locale].door),
          node("A9", `${locale}: the screen uses ${forbidden}, which already means autonomous recovery`),
        ).not.toContain(forbidden);
      }
    }
    expect(
      String(arms["zh-CN"].door).includes("找回"),
      node("A9", "the Chinese arm does not use the pinned word 找回"),
    ).toBe(true);
    readings.A9 = arms;
  });

  test("A10 a door that never answers gives no number, and no empty state", async ({ page }) => {
    /* Instrument-injected transport failure: the browser refuses to send the mesh read.  Nothing in
       the product is stubbed and no response body is rewritten -- the state judged is 「没读到」,
       which no product door can produce on demand.  The sibling state 「答了但没带清单」 (`no_list`)
       is NOT_MEASURED here on purpose: reaching it would mean rewriting an answer, which this lane
       does not do. */
    await boot(page, SET_JOURNEY, "zh-CN");
    await page.route("**/api/v1/journeys/**/provider-mesh**", (route) => route.abort());
    await page.goto("/v8/hub", { waitUntil: "load" });
    const panel = page.getByTestId("pending-set");
    await expect(panel, node("A10", "the panel disappeared with one door missing")).toHaveCount(1);
    const unknown = panel.getByTestId("pending-set-unknown");
    await expect(unknown, node("A10", "the screen did not name the door it could not read")).toHaveCount(1);
    const line = (await unknown.innerText()).replace(/\s+/g, " ");
    expect(
      line.includes("本程捕获的地点清单没读到"),
      node("A10", `the sentence is not the named one: "${line}"`),
    ).toBe(true);
    await expect(
      panel.getByTestId("pending-count"),
      node("A10", "an unread door still printed a number"),
    ).toHaveCount(0);
    await expect(
      panel.getByTestId("pending-empty"),
      node("A10", "an unread door was answered as 「没有待规划的地点」"),
    ).toHaveCount(0);
    /* And the one action it offers has to work: 再读一次 re-issues the two reads. */
    await page.unroute("**/api/v1/journeys/**/provider-mesh**");
    await unknown.getByTestId("pending-reread").click();
    await expect(
      panel.getByTestId("pending-count"),
      node("A10", "再读一次 did not bring the set back once the door could be read"),
    ).toHaveCount(1);
    const recovered = await panelReading(page, panel);
    expect(
      recovered.rendered_rows,
      node("A10", "the recovered set is empty, so the recovery proves nothing"),
    ).toBeGreaterThan(0);
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A10_unread_door.png`) });
    readings.A10 = { line, injected: "browser refused to send GET provider-mesh", recovered };
  });
});

const API = `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

async function apiGet(path: string): Promise<{ status: number; json: any }> {
  /* Node's own fetch, not the browser's: this is a second pair of eyes on the server, taken outside
     the page so a screen cannot be graded on the evidence it chose to show. */
  const response = await fetch(API + path, { headers: { Accept: "application/json" } });
  const text = await response.text();
  let json: any = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = null;
  }
  return { status: response.status, json };
}

test.describe("ORD-0450 putting the pending set onto a day, through the doors that already exist", () => {
  test("A11 the tick is local state, the batch says what it carries, and a disabled action says why", async ({
    page,
  }) => {
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    const blocked = panel.getByTestId("pending-blocked");
    await expect(
      blocked,
      node("A11", "with nothing ticked the action gave no named reason"),
    ).toHaveCount(1);
    expect(
      (await blocked.innerText()).includes("先勾至少一个地点"),
      node("A11", `the reason is not the named one: ${await blocked.innerText()}`),
    ).toBe(true);
    await expect(panel.getByTestId("pending-place-action")).toBeDisabled();

    const experienceBefore = await apiGet(
      `/api/v1/journeys/${SET_JOURNEY}/experience`,
    );
    const plannedStops = (experienceBefore.json?.active_plan?.activities ?? []).filter(
      (item: any) => item.place_id,
    ).length;

    await panel.getByTestId("pending-tick").first().click();
    const selected = await panel.getByTestId("pending-selected").innerText();
    expect(selected.includes("已勾 1 个"), node("A11", `tick did not read back: ${selected}`)).toBe(
      true,
    );
    const batchLine = await panel.getByTestId("pending-batch-line").innerText();
    /* The batch has to carry every place the chosen plan already holds, or "add a place" is also
       "lose four".  The number on the line is compared against the server's own plan, not a belief. */
    expect(
      batchLine.includes(`已有的 ${plannedStops} 个地点`) && batchLine.includes("你勾的 1 个"),
      node(
        "A11",
        `the batch line does not match the server's plan (${plannedStops} placed stops): ${batchLine}`,
      ),
    ).toBe(true);
    await expect(panel.getByTestId("pending-place-action")).toBeEnabled();

    await panel.getByTestId("pending-tick").first().click();
    expect(
      (await panel.getByTestId("pending-selected").innerText()).includes("已勾 0 个"),
      node("A11", "the second press did not take the tick back off"),
    ).toBe(true);
    readings.A11 = { planned_stops_from_server: plannedStops, batch_line: batchLine, selected };
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A11_tick_zh.png`) });
  });

  test("A12 one real press: the answer is a named condition, and nothing was written", async ({
    page,
  }) => {
    const panel = await openHub(page, SET_JOURNEY, "zh-CN");
    const before = await apiGet(`/api/v1/journeys/${SET_JOURNEY}`);
    const plansBefore = await apiGet(`/api/v1/journeys/${SET_JOURNEY}/plans`);

    await panel.getByTestId("pending-tick").first().click();
    await panel.getByTestId("pending-place-action").click();
    const refused = panel.getByTestId("pending-write-refused");
    await expect(
      refused,
      node("A12", "the press produced neither a receipt nor a named refusal"),
    ).toHaveCount(1);
    const text = (await refused.innerText()).replace(/\s+/g, " ");
    const named = [
      "排期门没能把这一批放进本程的那一天",
      "卡住这一批的是受保护的承诺",
      "排期门没有从这一批产出一版",
    ].some((sentence) => text.includes(sentence));
    expect(named, node("A12", `the refusal is generic, not a named condition: "${text}"`)).toBe(true);
    for (const forbidden of ["操作失败", "网络不可用", "暂无数据"]) {
      expect(text, node("A12", `the refusal fell back to ${forbidden}`)).not.toContain(forbidden);
    }
    /* The token stays on the details face, where it is quotable and not required reading. */
    await panel.getByTestId("pending-write-facts-open").click();
    const tokenLine = (await panel.getByTestId("pending-write-refused").innerText()).replace(
      /\s+/g,
      " ",
    );
    expect(
      /reason_code=V13_[A-Z0-9_]+/.test(tokenLine),
      node("A12", `no server token reached the details face: ${tokenLine.slice(0, 220)}`),
    ).toBe(true);

    await expect(
      panel.getByTestId("pending-receipt"),
      node("A12", "a refused write still showed a receipt, i.e. claimed a day"),
    ).toHaveCount(0);
    await expect(
      panel.getByTestId("pending-adopt"),
      node("A12", "an adoption button appeared with nothing to adopt"),
    ).toHaveCount(0);

    const after = await apiGet(`/api/v1/journeys/${SET_JOURNEY}`);
    const plansAfter = await apiGet(`/api/v1/journeys/${SET_JOURNEY}/plans`);
    readings.A12 = {
      refusal_line: text,
      active_plan_before: before.json?.active_plan_version_id ?? null,
      active_plan_after: after.json?.active_plan_version_id ?? null,
      plan_rows_before: Array.isArray(plansBefore.json) ? plansBefore.json.length : null,
      plan_rows_after: Array.isArray(plansAfter.json) ? plansAfter.json.length : null,
    };
    expect(
      readings.A12.active_plan_after,
      node("A12", "the refusal still moved which plan the trip runs"),
    ).toBe(readings.A12.active_plan_before);
    expect(
      readings.A12.plan_rows_after,
      node("A12", "the refusal still persisted a plan version"),
    ).toBe(readings.A12.plan_rows_before);
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A12_named_refusal_zh.png`) });
  });

  test("A13 the English arm names the same condition in its own words", async ({ page }) => {
    const panel = await openHub(page, SET_JOURNEY, "en");
    await panel.getByTestId("pending-tick").first().click();
    const batchLine = await panel.getByTestId("pending-batch-line").innerText();
    expect(
      batchLine.toLowerCase().includes("places your chosen plan already holds"),
      node("A13", `en batch line lost its meaning: ${batchLine}`),
    ).toBe(true);
    await panel.getByTestId("pending-place-action").click();
    const refused = panel.getByTestId("pending-write-refused");
    await expect(refused, node("A13", "no refusal rendered in the English arm")).toHaveCount(1);
    const text = (await refused.innerText()).replace(/\s+/g, " ");
    expect(/[\u4e00-\u9fff]/.test(text), node("A13", `en arm renders CJK: ${text}`)).toBe(false);
    const named = [
      "could not fit this batch inside the trip's day",
      "protected commitment is what blocks this batch",
      "produced no version from this batch",
    ].some((sentence) => text.includes(sentence));
    expect(named, node("A13", `en refusal is generic: "${text}"`)).toBe(true);
    readings.A13 = { batch_line: batchLine, refusal_line: text };
    await page.screenshot({ path: evidencePath(`ord0445/${RUN}__A13_named_refusal_en.png`) });
  });

  test("A14 the write path is the landed door: the screen posts compose and nothing else", async ({
    page,
  }) => {
    const seen: string[] = [];
    await boot(page, SET_JOURNEY, "zh-CN");
    page.on("request", (request) => {
      if (request.method() !== "GET") seen.push(`${request.method()} ${request.url()}`);
    });
    await page.goto("/v8/hub", { waitUntil: "load" });
    const panel = page.getByTestId("pending-set");
    await expect(panel).toHaveCount(1);
    await panel.getByTestId("pending-tick").first().click();
    await panel.getByTestId("pending-place-action").click();
    await expect(panel.getByTestId("pending-write-refused")).toHaveCount(1);
    const writes = seen.filter((line) => !line.includes("offline-snapshot"));
    readings.A14 = { writes };
    expect(
      writes.some((line) => line.includes("/plans/authoritative-compose")),
      node("A14", `the press did not go through the landed compose door: ${JSON.stringify(writes)}`),
    ).toBe(true);
    expect(
      writes.filter((line) => /\/api\/v1\//.test(line)).length,
      node("A14", `this screen opened more than the one door it is allowed to: ${JSON.stringify(writes)}`),
    ).toBe(1);
  });
});

function firstDifference(a: string, b: string): { at: number; before: string; after: string } {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
  return { at: i, before: a.slice(Math.max(0, i - 40), i + 60), after: b.slice(Math.max(0, i - 40), i + 60) };
}
