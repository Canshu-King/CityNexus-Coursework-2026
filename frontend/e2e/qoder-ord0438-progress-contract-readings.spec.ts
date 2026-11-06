/**
 * ORD-0438 — is "已走到第几天 / 已完成几处" already computed twice, and do the empty states read as
 * contracts or as statements?
 *
 * Nothing here writes product bytes. The arms read the same payload the screens read and then quote
 * what each screen actually painted, so the divergence claim is a measurement rather than my reading
 * of two source files.
 *
 *   P1 denominators  -- the server's `temporal_shape.day_count` (which Today quotes, and which
 *                       ORD-0063 ruled must not be re-derived) against the itinerary's own
 *                       client-side grouping of `activities[]` by calendar day; plus the two cursor
 *                       sources (`twin.current_activity_id` vs `active_plan.current_activity_id`)
 *                       and the terminal/completed counts behind "已完成几处".
 *   P2 empty shape   -- the one empty state this data state really renders (the pre-trip checklist),
 *                       quoted verbatim together with whether it carries an action.
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

const TERMINAL = ["COMPLETED", "SKIPPED", "CANCELLED"];

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0438", name + ".json");
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O438 " + name + " " + JSON.stringify(body));
}

/** The journey's own calendar day, so the grouping matches what the screens do. */
const dayKeyInZone = (iso: string, tz: string) => {
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(d);
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "?";
  return `${g("year")}-${g("month")}-${g("day")}`;
};

test("ORD-0438 P1 the day denominators and the cursor sources, both screens against one payload", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  const journeyId = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
  expect(journeyId, "the sealed demo journey has to exist for the denominators to mean anything").toBeTruthy();

  const todayWhen = await page.getByTestId("today-when").innerText();
  const heroMeta = await page.locator(".cn-hero-meta").first().innerText().catch(() => "");

  const api = await page.evaluate(async (id: string) => {
    const exp = await (await fetch(`/api/v1/journeys/${id}/experience`)).json();
    const twin = await (await fetch(`/api/v1/journeys/${id}/twin`)).json();
    return { exp, twin };
  }, journeyId as string);

  const exp = api.exp as Record<string, any>;
  const twin = api.twin as Record<string, any>;
  const journey = exp.journey ?? {};
  const plan = exp.active_plan ?? null;
  const acts: any[] = plan?.activities ?? [];
  const tz = journey.timezone || "UTC";
  const keys = Array.from(new Set(acts.map((a) => dayKeyInZone(a.start_at, tz)))).sort();
  const serverDayCount = journey.temporal_shape?.day_count ?? null;
  const completed = acts.filter((a) => TERMINAL.includes(String(a.status)));
  const currentOnTwin = twin?.current_activity_id ?? null;
  const currentOnPlan = plan?.current_activity_id ?? null;
  const twinExec = (twin?.context as any)?.execution ?? null;

  const read = {
    journey_id: journeyId,
    timezone: tz,
    activity_count: acts.length,
    server_temporal_shape_day_count: serverDayCount,
    client_grouped_day_count: keys.length,
    client_day_keys: keys,
    denominators_equal: serverDayCount === keys.length,
    terminal_status_count: completed.length,
    terminal_statuses_seen: Array.from(new Set(completed.map((a) => String(a.status)))),
    remaining_non_terminal: acts.length - completed.length,
    cursor_on_twin: currentOnTwin,
    cursor_on_active_plan: currentOnPlan,
    cursors_equal: currentOnTwin === currentOnPlan,
    twin_cursor_status: twinExec?.cursor_status ?? null,
    plan_cursor_status: plan?.cursor_status ?? null,
    twin_execution_now: twinExec?.now_activity_id ?? null,
    twin_execution_next: twinExec?.next_activity_id ?? null,
    screen_today_when: todayWhen.replace(/\s+/g, " ").trim(),
    screen_hero_meta: heroMeta.replace(/\s+/g, " ").trim(),
  };

  await openScreen(page, "/v8/trips", ["trip-itinerary"]);
  const itineraryH1 = await page.getByTestId("trip-itinerary").locator("h1").first().innerText();
  read.screen_itinerary_h1 = itineraryH1.replace(/\s+/g, " ").trim();

  write("P1_denominators", read);
  expect(read.activity_count, "the sealed plan has to carry activities for a denominator to exist").toBeGreaterThan(0);
});

test("ORD-0438 P2 the empty state this data state really renders", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  const read = await page.evaluate(() => {
    const panel = document.querySelector<HTMLElement>("[data-testid='pretrip-checklist']");
    const empty = document.querySelector<HTMLElement>("[data-testid='pretrip-empty']");
    const action = document.querySelector<HTMLElement>("[data-testid='pretrip-empty-action']");
    const heading = panel?.querySelector("h2");
    return {
      panel_present: !!panel,
      item_count_attr: panel?.getAttribute("data-count") ?? null,
      heading_text: (heading?.textContent ?? "").trim(),
      empty_present: !!empty,
      empty_sentence: empty ? (empty.textContent ?? "").replace(/\s+/g, " ").trim() : null,
      empty_has_action: !!action,
      action_label: action ? (action.textContent ?? "").trim() : null,
      counts_read_as_contract: (() => {
        const s = empty ? (empty.textContent ?? "") : "";
        return /之后|再|先|去|到.{0,6}看|unlock|after|go to|open|review/i.test(s);
      })(),
    };
  });
  write("P2_empty_state", read);
  expect(read.panel_present, "the checklist panel has to be on the screen for its empty state to be quotable")
    .toBe(true);
});
