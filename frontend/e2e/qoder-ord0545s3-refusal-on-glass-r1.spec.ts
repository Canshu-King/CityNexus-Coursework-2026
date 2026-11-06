/* ORD-0545 slice 3 -- a refusal sentence the family table owns has to be read off real glass.
 *
 * Slice 2 moved the POI-import sentences into `poiImportCopy.ts` and proved with three counter-arms
 * that the seven refusal names can no longer be orphaned at build time.  What it could NOT show, and
 * named NOT_REACHED_BY_LANE, is one of those seven reaching the screen: the earlier attempt loaded the
 * panel against a deleted journey and the panel rendered no refusal at all, so the sentence stayed
 * unmeasured on glass.
 *
 * This slice finds a refusal the traveller's own next step really produces, using nothing but product
 * doors on this lane's private stack:
 *
 *   the create door stores `active_plan_version_id = None` (backend/app/main.py, create_journey), so a
 *   trip that was made but never planned is a real state, not an invented one;
 *   the import's read door still resolves pasted text for it (200, an item RESOLVED), and the write
 *   door refuses with HTTP 409 and error.reason_code == NO_ACTIVE_PLAN (main.py's plan guard), because
 *   there is no plan to add to;
 *   the panel maps that code through FAILURE_SENTENCE to the table's `errNoActivePlan` pair.
 *
 * Three arms, each reading rendered text rather than attributes:
 *   R1  Chinese glass: the sentence is exactly the table's zh half; the technical line reads HTTP 409;
 *       the machine words NO_ACTIVE_PLAN and errNoActivePlan appear 0 times on the screen.
 *   R2  English glass: the same walk gets the table's en half.
 *   R3  the sentence promises nothing moved -- so re-read the journey from the server afterwards and
 *       require the plan pointer to still be null; a refusal that silently wrote would fail here.
 *
 * Non-vacuity lives in review/ord0545/refusal_glass_counterarm_r1.py: it repoints FAILURE_SENTENCE's
 * NO_ACTIVE_PLAN entry at another REAL table key (so the build stays green) and requires R1 to go red.
 */
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const COLLECTION_PREREQ: string[] = [];

const CANARY_TRIP = "22222222-2222-4222-8222-222222222222";
const PASTED = "Central Food Hall";
const TABLE = "frontend/src/features/poiimport/poiImportCopy.ts";
const BACKEND = `http://127.0.0.1:${process.env.CITYNEXUS_ORD0545_BACKEND_PORT ?? "4911"}`;
const EVIDENCE = path.join(process.cwd(), "..", "review", "ord0545", "readouts");
const TAG = process.env.CITYNEXUS_ORD0545_TAG ?? "r1";
const BASE_TIP = process.env.CITYNEXUS_ORD0545_BASE_TIP ?? "";
if (!/^[0-9a-f]{40}$/.test(BASE_TIP)) COLLECTION_PREREQ.push("CITYNEXUS_ORD0545_BASE_TIP must be the 40-hex tip this was measured on");

const repoRoot = path.join(process.cwd(), "..");

function writeEvidence(name: string, payload: unknown) {
  fs.mkdirSync(EVIDENCE, { recursive: true });
  fs.writeFileSync(
    path.join(EVIDENCE, `${name}_${TAG}.json`),
    JSON.stringify(
      { generation: { tag: TAG, base_tip: BASE_TIP, recorded_at: new Date().toISOString() }, ...(payload as object) },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
}

/** The table's own pair for one name, taken from the shipped bytes rather than retyped here. */
function tablePair(key: string): { zh: string; en: string } {
  const text = fs.readFileSync(path.join(repoRoot, TABLE), "utf-8");
  const open = text.indexOf(`  ${key}: {`);
  if (open === -1) throw new Error(`${key} is not in ${TABLE}`);
  const close = text.indexOf("\n  }", open);
  const block = text.slice(open, close);
  const half = (which: "zh" | "en") => {
    const at = block.indexOf(`${which}: "`);
    if (at === -1) throw new Error(`${key} has no ${which} half`);
    const from = at + which.length + 3;
    return block.slice(from, block.indexOf('"', from));
  };
  return { zh: half("zh"), en: half("en") };
}

async function createUnplannedJourney(request: any): Promise<Record<string, unknown>> {
  const canary = await request.get(`${BACKEND}/api/v1/journeys/${CANARY_TRIP}`);
  expect(canary.status(), "the canary trip must be readable").toBe(200);
  const canaryBody = await canary.json();
  const now = new Date();
  const later = (days: number) => new Date(now.getTime() + days * 86400_000).toISOString();
  const created = await request.post(`${BACKEND}/api/v1/journeys`, {
    data: {
      traveler_id: canaryBody.traveler_id,
      title: `ORD-0545 slice 3 -- made but never planned (${TAG})`,
      city_code: canaryBody.city_code,
      timezone: canaryBody.timezone,
      start_at: later(40),
      end_at: later(42),
      budget: { amount_minor: 90000, currency: canaryBody.budget?.currency ?? "CNY" },
      planning_mode: "balanced",
      party_size: 2,
    },
  });
  const body = await created.json();
  expect(created.status(), "the product's own create door must answer 201").toBe(201);
  const readBack = await request.get(`${BACKEND}/api/v1/journeys/${body.id}`);
  const rb = await readBack.json();
  // The antecedent of the whole slice: this is a real DRAFT with no plan pointer, measured here.
  expect(readBack.status()).toBe(200);
  expect(rb.active_plan_version_id, "the created journey must really have no active plan").toBeNull();
  return {
    journey_id: body.id,
    traveler_id: canaryBody.traveler_id,
    city_code: canaryBody.city_code,
    journey_status: rb.status,
    active_plan_version_id: rb.active_plan_version_id,
  };
}

async function walkToRefusal(page: any, request: any, locale: string) {
  const journey = await createUnplannedJourney(request);
  await page.addInitScript(
    ([id, which]) => {
      localStorage.setItem("citynexus.v8.journey_id", id);
      localStorage.setItem("citynexus.locale", which);
    },
    [String(journey.journey_id), locale],
  );
  await page.goto("/v8/import");
  await expect(page.getByTestId("poi-import")).toBeVisible({ timeout: 40_000 });

  await page.getByTestId("poi-import-text").fill(PASTED);
  const parseAnswered = page.waitForResponse((r: any) => r.url().includes("/poi-import/parse"), { timeout: 30_000 });
  await page.getByTestId("poi-import-parse").click();
  const parseHttp = await parseAnswered;
  const parseBody = await parseHttp.json().catch(() => null);
  const items = (parseBody?.items ?? []) as { status: string; addable?: boolean; place_id?: string | null }[];
  await expect(page.getByTestId("poi-import-results")).toBeVisible({ timeout: 30_000 });
  // If the read door does not hand back something addable, this walk is not the claimed path at all.
  const addableRows = items.filter((i) => i.addable && i.place_id);
  expect(
    addableRows.length,
    `no addable row came back: ${JSON.stringify(items.map((i) => i.status))}`,
  ).toBeGreaterThan(0);
  await page.getByTestId("poi-card-select").first().click();

  const planExit = page.getByTestId("poi-import-exit-plan");
  await expect(planExit).toHaveAttribute("data-picked", "true");
  const landing = await page.getByTestId("poi-import-landing-day").innerText();

  const answered = page.waitForResponse((r: any) => r.url().includes("/poi-import/add"), { timeout: 30_000 });
  await page.getByTestId("poi-import-add").click();
  const http = await answered;
  const envelope = await http.json().catch(() => null);

  const sentenceCell = page.getByTestId("poi-import-readback-failed");
  await expect(sentenceCell, "the refusal sentence has to be on the glass").toBeVisible({ timeout: 20_000 });
  // The sentence is a text node; the technical line is a nested <small>, which is excluded deliberately.
  const sentence = await sentenceCell.evaluate((el: Element) =>
    Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => (n.textContent ?? "").replace(/\s+/g, " "))
      .join("")
      .trim(),
  );
  const detail = await page.getByTestId("poi-import-readback-detail").innerText();
  const screen = await page.evaluate(() => document.body.innerText);
  // Which box recites what: the refusal cell is this slice's claim, the rest of the screen is reported
  // with its carrier rather than folded into a green.
  const census = await page.evaluate(() => {
    const re = /\b[A-Z][A-Z_]{4,}\b/g;
    const carriers: Record<string, string[]> = {};
    const walk = (el: Element) => {
      for (const node of Array.from(el.childNodes)) {
        if (node.nodeType === 3) {
          for (const m of (node.textContent ?? "").matchAll(re)) {
            let box: Element | null = el;
            let named = "";
            while (box && !named) {
              named = box.getAttribute?.("data-testid") ?? "";
              box = box.parentElement;
            }
            (carriers[m[0]] ??= []).push(named || el.tagName.toLowerCase());
          }
        } else if (node.nodeType === 1) {
          walk(node as Element);
        }
      }
    };
    walk(document.body);
    return Object.fromEntries(
      Object.entries(carriers).map(([word, boxes]) => [word, [...new Set(boxes)].sort()]),
    );
  });
  const cellText = await sentenceCell.innerText();
  const wordsInCell = cellText.match(/\b[A-Z][A-Z_]{4,}\b/g) ?? [];

  return {
    journey,
    locale,
    pasted: PASTED,
    landing_day_line: landing,
    add_http_status: http.status(),
    parse_http_status: parseHttp.status(),
    parse_item_statuses: items.map((i) => i.status),
    addable_rows: addableRows.length,
    reason_code: envelope?.error?.reason_code ?? null,
    sentence_on_glass: sentence,
    technical_line: detail,
    screen_characters: screen.length,
    poi_import_key_names_on_screen: (screen.match(/\bpoiImport\.[a-zA-Z0-9]+\b/g) ?? []).sort(),
    table_key_names_on_screen: (screen.match(/\berr[A-Za-z]+/g) ?? []).sort(),
    machine_words_on_screen: Object.keys(census).sort(),
    // Two channels, named separately because they do not measure the same thing: `innerText` carries
    // CSS text-transform (an English label typed "Already" arrives as ALREADY), while the carrier walk
    // above reads textContent and only sees tokens that are literally upper-case in the DOM.
    machine_words_by_rendered_text: [...new Set((screen.match(/\b[A-Z][A-Z_]{4,}\b/g) ?? []))].sort(),
    machine_word_carriers: census,
    machine_words_in_refusal_cell: [...new Set(wordsInCell)].sort(),
    refusal_cell_text: cellText,
    uuids_on_screen: (screen.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g) ?? []).sort(),
    whole_screen_text_sha256: createHash("sha256").update(screen, "utf8").digest("hex"),
  };
}

test.beforeEach(() => {
  test.skip(COLLECTION_PREREQ.length > 0, COLLECTION_PREREQ.join(" | "));
});

test.describe("ORD-0545 slice 3 a family-table refusal, read off real glass", () => {
  test("R1 中文那一屏：没排程的行程点\"加入\"，上屏的就是表里那句 zh", async ({ page, request }) => {
    const got = await walkToRefusal(page, request, "zh-CN");
    const pair = tablePair("errNoActivePlan");
    writeEvidence("refusal_glass_zh", { ...got, table_zh: pair.zh, table_en: pair.en,
                                        sentence_equals_table_zh: got.sentence_on_glass === pair.zh });
    expect(got.reason_code, "the server must really name the reason this arm reads").toBe("NO_ACTIVE_PLAN");
    expect(got.add_http_status).toBe(409);
    expect(got.sentence_on_glass, "the glass must carry the table's Chinese half verbatim").toBe(pair.zh);
    expect(got.technical_line).toBe("HTTP 409");
    expect(got.poi_import_key_names_on_screen).toEqual([]);
    expect(got.table_key_names_on_screen, "a table key name on the glass is the ORD-0533 defect").toEqual([]);
    expect(got.machine_words_in_refusal_cell, "the refusal sentence's own cell must not recite a code").toEqual([]);
    expect(got.uuids_on_screen).toEqual([]);
  });

  test("R2 英文那一屏：同一趟走法拿到表里的 en 半，不是中文半也不是键名", async ({ page, request }) => {
    const got = await walkToRefusal(page, request, "en-GB");
    const pair = tablePair("errNoActivePlan");
    writeEvidence("refusal_glass_en", { ...got, table_zh: pair.zh, table_en: pair.en,
                                        sentence_equals_table_en: got.sentence_on_glass === pair.en });
    expect(got.reason_code).toBe("NO_ACTIVE_PLAN");
    expect(got.sentence_on_glass, "the glass must carry the table's English half verbatim").toBe(pair.en);
    expect(got.sentence_on_glass).not.toBe(pair.zh);
    expect(got.table_key_names_on_screen).toEqual([]);
    expect(got.machine_words_in_refusal_cell).toEqual([]);
  });

  test("R3 那句说\"什么都没改动\"，就用服务端回读证它", async ({ page, request }) => {
    const got = await walkToRefusal(page, request, "zh-CN");
    const after = await request.get(`${BACKEND}/api/v1/journeys/${String(got.journey.journey_id)}`);
    const body = await after.json();
    const plans = await request.get(
      `${BACKEND}/api/v1/journeys/${String(got.journey.journey_id)}/plans`,
    );
    const planRows = await plans.json().catch(() => null);
    writeEvidence("refusal_wrote_nothing", {
      journey_id: got.journey.journey_id,
      add_http_status: got.add_http_status,
      reason_code: got.reason_code,
      active_plan_version_id_after_refusal: body.active_plan_version_id,
      journey_status_after_refusal: body.status,
      plans_door_status: plans.status(),
      plan_version_count: Array.isArray(planRows) ? planRows.length : null,
    });
    expect(body.active_plan_version_id, "the refusal must not have minted a plan").toBeNull();
    expect(got.add_http_status).toBe(409);
  });
});
