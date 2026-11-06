/**
 * ORD-0415 (ruling, 09-29 08:27) -- this rule lives on the gate side, not in `src/lib/`.
 *
 * Measured on the tip this branch was cut from: `mapLabelTextField()` had one mention in the whole
 * repo -- its own definition -- and the module's only importers were `e2e/qoder-map-ord0403-*.spec.ts`
 * and `e2e/qoder-map-ord0412-*.spec.ts`.  No product code consulted it, so keeping it under
 * `frontend/src/lib/` claimed a runtime authority it did not have: the strings painted on the glass are
 * written literally in `components/JourneyMap.tsx` (`"text-field": ["get", "title"]`), and the v8 map
 * panel paints numbered DOM markers with no symbol layer at all.  Moving the file is the ruling's
 * option (乙); the list itself is unchanged, so the two gates still read one shared definition.
 *
 * If a later order wires a canvas label layer to `mapLabelTextField()`, that is the moment to move this
 * back -- and the `scope-declared` arm in `qoder-map-ord0412-label-scope.spec.ts` fails when someone
 * re-adds `src/lib/mapLabels.ts` without doing that.
 */

/**
 * ORD-0403 — one named producer for what may be painted on the map glass.
 *
 * The rule the order sets: on the map itself only the stop's name, its category, and its role in
 * the day (start / end / which day) may be drawn. Times, money and provider/source text stay off
 * the glass, because a pin that carries a clock or a price reads as a *promise* about a value the
 * planner may not actually have computed — and on a phone the label is the first thing covering
 * the map.
 *
 * This module is the single place that says so. `lib/rawFacts.ts` already owns a whitelist, but
 * that one is about failure readouts on a different surface; reusing its name for a different
 * domain would make two things look like one.
 *
 * `MAP_LABEL_FIELDS` is deliberately a set of *field names*, not a list of finished strings: the
 * gate asserts against the values a running screen actually paints, and a field list is what lets
 * it say which allowed field a hit came from.
 */

export const MAP_LABEL_FIELDS = ["title", "category", "role"] as const;
export type MapLabelField = (typeof MAP_LABEL_FIELDS)[number];

/**
 * ORD-0412 — the scope of `MAP_LABEL_FORBIDDEN`: which strings this rule has any authority over.
 *
 * The `source` class matches `openstreetmap`, `©` and `provider`, and that is precisely the text the
 * ORD-A compliance obligation REQUIRES on screen (basemap provider attribution, per the tile
 * licence).  The two only stay out of each other's way because this rule judges strings painted on
 * the map glass while that sentence is DOM text.  Saying so here is what stops a later reader from
 * pointing the list at the whole map panel, reading a required compliance sentence as a violation,
 * and "fixing" it by deleting the sentence.  `e2e/qoder-map-ord0412-label-scope.spec.ts` asserts
 * both halves: the scope is declared, and the two surfaces hold at the same time on one screen.
 */
export const MAP_LABEL_SCOPE = {
  /** Feed only these to `mapLabelViolations` -- label text drawn on the canvas. */
  judged: ["canvas-label-strings"],
  /** Never judged here. The basemap attribution lives in the DOM and must stay readable. */
  notJudged: ["dom-attribution", "dom-panel-text"],
  /* The obligation this carve-out protects, carried in the rule's own data: deleting it makes the
     gate red rather than quietly turning a required sentence into a reported violation. */
  becauseOf: "ORD-A basemap provider attribution must stay readable on screen",
} as const;
export type MapLabelScopeSurface = keyof typeof MAP_LABEL_SCOPE;

/** maplibre expression for the symbol layer that labels a stop on the map. */
export function mapLabelTextField(): string[] {
  return ["get", "title"];
}

/** Patterns that must never appear in text painted on the map canvas. Each carries the reason the
 *  order gives: a clock, a currency, or a provider/source attribution.  Canvas only -- see
 *  `MAP_LABEL_SCOPE`; the DOM basemap attribution is not a violation of this list. */
export const MAP_LABEL_FORBIDDEN: ReadonlyArray<{ name: string; source: string; pattern: RegExp }> = [
  { name: "clock-time", source: "start_at / end_at", pattern: /\b\d{1,2}:\d{2}\b/ },
  { name: "duration", source: "travel_from_prev_min", pattern: /\d+\s*(?:分钟|min)\b/i },
  { name: "money", source: "estimated_cost", pattern: /[¥$€£]|CNY|USD|\d+\s*元/ },
  { name: "source", source: "basemap/provider attribution", pattern: /openstreetmap|openfreemap|©|provider/i },
];

/** Which forbidden classes are present in a painted string. Empty array means the string is clean.
 *  The argument must come from `MAP_LABEL_SCOPE.judged`; passing DOM text -- in particular the
 *  basemap attribution the ORD-A gate requires to be readable -- makes a required sentence count as
 *  a violation, which is the misdelete this declaration exists to prevent. */
export function mapLabelViolations(painted: string): string[] {
  return MAP_LABEL_FORBIDDEN.filter((rule) => rule.pattern.test(painted)).map((rule) => rule.name);
}
