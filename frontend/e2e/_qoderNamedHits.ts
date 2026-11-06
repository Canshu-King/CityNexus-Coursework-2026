/**
 * ORD-0373 -- the lever that turns "a name resolves" into "a name resolves to something you can hit".
 *
 * Measured on this repo (`review/evidence/ord0373/cmdr014606n3__P1_probe.json`): at 390x844 and 680x900
 * a `getByRole("button", {name})` query for the desktop nav's label returns 1 node and it is the visible
 * one, while `getByText(name)` returns a node whose `isVisible()` is false -- the desktop sidebar's own
 * button, sitting at 0x0 inside an `aside.v8-sidebar` that a media query has switched to `display:none`.
 * A `.first()` on the text query therefore aims a click at a node no finger, and no screen reader, can
 * ever reach, and the failure surfaces as a timeout that reads like a product defect.
 *
 * These helpers make the filter explicit so the next lane does not re-derive it: survey by name, then
 * keep only what answers `elementFromPoint` at its own centre.
 */
import { expect, type Locator, type Page } from "@playwright/test";

export type NamedHit = {
  index: number;
  tag: string;
  cls: string;
  x: number;
  y: number;
  w: number;
  h: number;
  excluded_by: string | null;
  hit_self: boolean;
};

/**
 * Every element whose own text is exactly this name, each judged by striking its own centre.
 * Deliberately DOM-level: the point is to see the nodes a text query would hand back, including the
 * ones the accessibility tree already excludes.
 */
export async function surveyName(page: Page, name: string): Promise<NamedHit[]> {
  return page.evaluate((wanted: string) => {
    const norm = (s: string | null) => (s ?? "").replace(/\s+/g, " ").trim();
    const out: NamedHit[] = [];
    Array.from(document.querySelectorAll("*"))
      .filter((el) => norm(el.textContent) === wanted)
      .forEach((el, index) => {
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const struck = r.width > 0 && r.height > 0 ? document.elementFromPoint(cx, cy) : null;
        let excludedBy: string | null = null;
        for (let cur: Element | null = el; cur; cur = cur.parentElement) {
          const cs = getComputedStyle(cur);
          if (cs.display === "none") {
            excludedBy = `display:none@${cur.tagName.toLowerCase()}.${(cur.getAttribute("class") || "").split(" ")[0]}`;
            break;
          }
          if (cs.visibility === "hidden") {
            excludedBy = `visibility:hidden@${cur.tagName.toLowerCase()}`;
            break;
          }
        }
        out.push({
          index,
          tag: el.tagName.toLowerCase(),
          cls: norm(el.getAttribute("class")).slice(0, 40),
          /* Position, not just size: a survey that can only say "this has a box" cannot answer whether
             that box sits under the fixed bottom nav or on a colliding band, which is what ORD-0375 asks. */
          x: Math.round(r.left),
          y: Math.round(r.top),
          w: Math.round(r.width),
          h: Math.round(r.height),
          excluded_by: excludedBy,
          hit_self: !!struck && (struck === el || el.contains(struck)),
        });
      });
    return out;
  }, name);
}

/** The filter itself, kept apart from the browser so a test can prove it is what does the work. */
export function strikeableOnly(hits: NamedHit[]): NamedHit[] {
  return hits.filter((h) => h.w > 0 && h.h > 0 && h.excluded_by === null && h.hit_self);
}

/**
 * Resolve a name to something strikable, or say exactly why it cannot. A name that resolves to zero
 * strikeable nodes is reported with the survey, because "the query returned something" is not the
 * question -- "the query returned a node you can hit" is.
 */
export async function strikeableByName(page: Page, name: string): Promise<NamedHit[]> {
  const hits = await surveyName(page, name);
  const kept = strikeableOnly(hits);
  expect(
    kept.length,
    `「${name}」 resolves to ${hits.length} node(s) but 0 you can strike: ${JSON.stringify(hits)}`,
  ).toBeGreaterThan(0);
  return kept;
}

/**
 * A locator restricted to the nodes that survive the survey, so an action never aims at a hidden twin.
 * The candidates are walked, not indexed: the DOM survey and Playwright's text query do not return the
 * same set (the probe measured 2 against 1 for one label), so mapping an index across them would pick
 * the wrong node on exactly the day someone changes how the label is nested.
 */
export async function strikeableLocator(page: Page, name: string): Promise<Locator> {
  await strikeableByName(page, name);
  const candidates = page.getByText(name, { exact: true });
  const count = await candidates.count();
  for (let i = 0; i < count; i += 1) {
    const candidate = candidates.nth(i);
    const box = await candidate.boundingBox().catch(() => null);
    if (!box || box.width <= 0 || box.height <= 0) continue;
    const struck = await candidate.evaluate((el: Element) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !!hit && (hit === el || el.contains(hit));
    });
    if (struck) return candidate;
  }
  expect(false, `the survey found a strikeable 「${name}」 but no Playwright candidate survived the strike`);
  return candidates.first();
}
