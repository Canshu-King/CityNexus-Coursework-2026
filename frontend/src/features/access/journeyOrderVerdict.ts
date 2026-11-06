/**
 * ORD-0518 -- can this screen actually say the list came back in the order it asked for?
 *
 * The trip list door answers in a named order (ORD-0477), and the screen used to print that order
 * from its own state: "现在按出发日期排。" That sentence was about what the screen *asked*, not about
 * what the traveller is looking at. Against a server that ignores the word -- an older deployment, a
 * proxy that rewrote the query, a door that answers before tiers existed -- the sentence stays green
 * while the rows are in some other order. This module is the one place that decides what the screen
 * is allowed to claim, and it decides it from the rows.
 *
 * What it may read is bounded by what this door sends. `contracts/openapi.yaml` gives
 * `listJourneys` the `Journey` shape: `id`, `traveler_id`, `title`, `city_code`, `timezone`,
 * `start_at`, `end_at`, `budget`, `planning_mode`, `status`, `active_plan_version_id`,
 * `current_activity_id`. There is no `created_at` and no `updated_at` in it -- measured against the
 * shape rather than assumed -- so two of the four orders are not checkable here at all. The honest
 * answer for those is `NOT_VERIFIABLE`, said on the glass, and not a time claim inherited from the
 * button the traveller last pressed.
 *
 * The comparison itself uses the reading the contract already states for the door: instants, and a
 * stored value with no offset read as UTC. Reusing that rule is the point -- a screen that compared
 * the same strings a different way would report a mismatch that the server never made. For `TITLE`
 * the screen has to fold the name the way the server folded it, and the two engines' folds differ on
 * a bounded set of characters that was measured rather than assumed; see `FOLD_DIVERGENT_RANGES`.
 */

import type { JourneySummary } from "./accessDoors";

/** The four things this screen can say about an order it asked for. */
export type JourneyOrderVerdictState = "CONSISTENT" | "INCONSISTENT" | "NOT_VERIFIABLE" | "NO_ROWS";

/** Why the check compared what it compared, or why it compared nothing.
 *
 *  A reason, not a sentence: the screen has to say this in the traveller's language, and an English
 *  clause pasted into a Chinese sentence is a second defect on top of the one being reported. The
 *  dictionary owns the wording (`access.orderReason*`); this module owns the fact. */
export type JourneyOrderReason =
  | "no_rows"
  | "no_created_at_in_row"
  | "no_updated_at_in_row"
  | "row_without_start_at"
  | "compared_start_at"
  | "compared_title"
  | "title_folds_differently";

export type JourneyOrderVerdict = {
  state: JourneyOrderVerdictState;
  reason: JourneyOrderReason;
  /** The 1-based row where the order first breaks, or the row that carried nothing to compare. */
  first_break_row: number | null;
  /** The stored column the check actually compared, when it compared one. Read by the gate off
   *  `data-compared`, never painted: `journeys.start_at` is a machine word, and a traveller reading
   *  it on the glass would be reading this project's internals as a label. */
  machine_field: string | null;
  /** The code point that made `TITLE` unverifiable, named so a reader can check the deny-list, and
   *  kept out of the sentence for the same reason as `machine_field`. */
  blocked_code_point: string | null;
};

/** `RECENT_UPDATE` orders `journeys.updated_at` and the no-tier order reads
 *  `journeys.created_at`; neither column is in this door's row shape, so no screen reading only the
 *  rows can confirm them. Named here so the panel says "核不了" instead of borrowing a claim. */
export const ORDERS_THIS_SCREEN_CANNOT_VERIFY: readonly string[] = ["RECENT_UPDATE", "CREATION_ORDER"];

/**
 * ORD-0518: the code points where `String.prototype.toLowerCase()` does not answer what Python's
 * `str.casefold()` answers -- the fold the server actually sorted by.
 *
 * This is a measurement, not a judgement. `review/ord0518/fold_casefold_server_r1.py` runs under the
 * server's own interpreter (`D:/moodle/CityNexus/backend/.venv`, Python 3.12.14) and records `casefold`
 * for every code point; `review/ord0518/fold_diff_browser_vs_server_r1.cjs` asks V8 the same question
 * for all 1112064 non-surrogate code points and keeps the ones where the two answers are different
 * strings: 352 code points in 49 ranges (the reading is in
 * `review/ord0518/fold_diff_browser_vs_server_r1.txt`, and `review/ord0518/fold_table_check_r1.cjs`
 * re-reads this table and compares it with that measurement). A title that carries one of them is not
 * checkable here, and the screen says so.
 *
 * The whitelist this replaces (ASCII, CJK, full-width) was too wide in the wrong direction: it passed
 * `ß`, `ﬁ` and `ς`, which are in the list below, and it rejected `·` (U+00B7), which is not -- and a
 * real trip on this tree is titled "Tianjin · stay-aware weekend", so the old guard refused to check
 * a name order that needed no refusal.
 */
export const FOLD_DIVERGENT_RANGES: readonly [number, number][] = [
  [0x00b5, 0x00b5], [0x00df, 0x00df], [0x0149, 0x0149],
  [0x017f, 0x017f], [0x01f0, 0x01f0], [0x0345, 0x0345],
  [0x0390, 0x0390], [0x03b0, 0x03b0], [0x03c2, 0x03c2],
  [0x03d0, 0x03d1], [0x03d5, 0x03d6], [0x03f0, 0x03f1],
  [0x03f5, 0x03f5], [0x0587, 0x0587], [0x13a0, 0x13f5],
  [0x13f8, 0x13fd], [0x1c80, 0x1c89], [0x1e96, 0x1e9b],
  [0x1e9e, 0x1e9e], [0x1f50, 0x1f50], [0x1f52, 0x1f52],
  [0x1f54, 0x1f54], [0x1f56, 0x1f56], [0x1f80, 0x1faf],
  [0x1fb2, 0x1fb4], [0x1fb6, 0x1fb7], [0x1fbc, 0x1fbc],
  [0x1fbe, 0x1fbe], [0x1fc2, 0x1fc4], [0x1fc6, 0x1fc7],
  [0x1fcc, 0x1fcc], [0x1fd2, 0x1fd3], [0x1fd6, 0x1fd7],
  [0x1fe2, 0x1fe4], [0x1fe6, 0x1fe7], [0x1ff2, 0x1ff4],
  [0x1ff6, 0x1ff7], [0x1ffc, 0x1ffc], [0xa7cb, 0xa7cc],
  [0xa7ce, 0xa7ce], [0xa7d2, 0xa7d2], [0xa7d4, 0xa7d4],
  [0xa7da, 0xa7da], [0xa7dc, 0xa7dc], [0xab70, 0xabbf],
  [0xfb00, 0xfb06], [0xfb13, 0xfb17], [0x10d50, 0x10d65],
  [0x16ea0, 0x16eb8],
];

/** U+03A3 GREEK CAPITAL LETTER SIGMA agrees with `casefold` one character at a time
 *  (`"Σ".toLowerCase() === "σ" === "Σ".casefold()`), so the per-code-point sweep above cannot see it.
 *  It diverges *inside a word*: JavaScript applies Unicode's Final_Sigma condition when it lowercases
 *  and Python's `casefold` does not. Measured on both interpreters with the whole string, recorded in
 *  `review/ord0518/sigma_context_server_r1.json` (the server's Python) and
 *  `review/ord0518/sigma_context_reading_r1.txt` (the two side by side): `"AΣ".toLowerCase()` is
 *  `"aς"` while `"AΣ".casefold()` is `"ασ"`. A name order over a title carrying a capital sigma
 *  therefore cannot be checked by folding each title on its own, so the capital is refused with the
 *  rest. */
export const FOLD_CONTEXT_DIVERGENT: readonly number[] = [0x03a3];

/** The first code point in `text` this screen cannot promise it folds the way the server did, or
 *  `null` when every character in it is checkable. Exported because the gate prints what blocked an
 *  arm instead of letting a reader guess. */
export function foldUntouchableCodePoint(text: string): number | null {
  for (const character of text) {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined) continue;
    if (FOLD_CONTEXT_DIVERGENT.includes(codePoint)) return codePoint;
    for (const [first, last] of FOLD_DIVERGENT_RANGES) {
      if (codePoint >= first && codePoint <= last) return codePoint;
    }
  }
  return null;
}

/** The same reading the door's contract states: an ISO-8601 instant, a value with no offset taken as
 *  UTC. Returns `null` for a value that is not such a string -- `null` is then reported, not sorted. */
export function screenInstant(value: unknown): number | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  const parsed = new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
  const time = parsed.getTime();
  return Number.isNaN(time) ? null : time;
}

/**
 * Judge the rows the door sent against the order the screen asked for.
 *
 * `tier === null` means the screen asked with no `sort` at all, which is the door's creation order;
 * that one is on the unverifiable list, because `created_at` is not in a row.
 */
export function journeyOrderVerdict(rows: JourneySummary[], tier: string | null): JourneyOrderVerdict {
  if (rows.length === 0) {
    return {
      state: "NO_ROWS",
      reason: "no_rows",
      first_break_row: null,
      machine_field: null,
      blocked_code_point: null,
    };
  }
  if (tier === null || ORDERS_THIS_SCREEN_CANNOT_VERIFY.includes(tier)) {
    return {
      state: "NOT_VERIFIABLE",
      reason: tier === "RECENT_UPDATE" ? "no_updated_at_in_row" : "no_created_at_in_row",
      first_break_row: null,
      machine_field: tier === "RECENT_UPDATE" ? "journeys.updated_at" : "journeys.created_at",
      blocked_code_point: null,
    };
  }
  if (tier === "DEPARTURE_DATE") {
    let previous: number | null = null;
    for (let index = 0; index < rows.length; index += 1) {
      const instant = screenInstant(rows[index].start_at);
      if (instant === null) {
        return {
          state: "NOT_VERIFIABLE",
          reason: "row_without_start_at",
          first_break_row: index + 1,
          machine_field: "journeys.start_at",
          blocked_code_point: null,
        };
      }
      if (previous !== null && instant < previous) {
        return {
          state: "INCONSISTENT",
          reason: "compared_start_at",
          first_break_row: index + 1,
          machine_field: "journeys.start_at",
          blocked_code_point: null,
        };
      }
      previous = instant;
    }
    return {
      state: "CONSISTENT",
      reason: "compared_start_at",
      first_break_row: null,
      machine_field: "journeys.start_at",
      blocked_code_point: null,
    };
  }
  // TITLE: the door folds case with Python's `casefold`, which is not JavaScript's `toLowerCase`.
  // The two part company on a bounded, measured set of characters (see `FOLD_DIVERGENT_RANGES`), and
  // where a title carries one of them this screen says it cannot verify rather than report a mismatch
  // the server never made. Everything else -- ASCII, CJK, `·`, accented Latin, Cyrillic -- the two
  // folds answer identically, and the order is checked.
  for (const row of rows) {
    const blocked = foldUntouchableCodePoint(row.title ?? "");
    if (blocked !== null) {
      return {
        state: "NOT_VERIFIABLE",
        reason: "title_folds_differently",
        first_break_row: null,
        machine_field: "journeys.title",
        blocked_code_point: "U+" + blocked.toString(16).toUpperCase().padStart(4, "0"),
      };
    }
  }
  let previousText: string | null = null;
  for (let index = 0; index < rows.length; index += 1) {
    const text = (rows[index].title ?? "").toLowerCase();
    if (previousText !== null && text < previousText) {
      return {
        state: "INCONSISTENT",
        reason: "compared_title",
        first_break_row: index + 1,
        machine_field: "journeys.title",
        blocked_code_point: null,
      };
    }
    previousText = text;
  }
  return {
    state: "CONSISTENT",
    reason: "compared_title",
    first_break_row: null,
    machine_field: "journeys.title",
    blocked_code_point: null,
  };
}
