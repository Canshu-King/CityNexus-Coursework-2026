/* ORD-0497 -- the one place that turns the footprint door's answer into a state the map can paint.
 *
 * Why a module and not a `useState` inside `MapPanel`: the door this reads is
 * `GET /api/v1/journeys/{id}/checkins` (`checkins.ts`), and that answer is already held privately
 * by two surfaces (`TripItinerary.tsx`, `FootprintPanel.tsx`). A third copy inside the map
 * would be a third footprint state, and the third one is where a stale local list starts disagreeing
 * with the server. So the map holds a *read*, never a footprint of its own: one GET per mount, no
 * cache, nothing carried across surfaces -- which is also what makes "did the map guess?" answerable.
 *
 * The arms are four, and they are not the same fact:
 *   read        -- the server answered, with a list that may be empty;
 *   refused     -- the server answered and would not serve the read;
 *   unreachable -- no usable answer arrived at all;
 *   not_asked   -- this surface has no journey, so nobody was asked.
 * `refused`/`unreachable`/`not_asked` paint NO stamp claim: `TripItinerary.tsx` returns silently
 * on a non-`read` answer and then renders every stop as 打卡, which is the guessing this order
 * forbids. The map must not inherit that habit, so the caller gets the arm and the painter is told
 * to stay silent in three of the four.
 */

import { useCallback, useEffect, useState } from "react";
import { mapCopy } from "../../i18n/mapPanelCopy";
import { readCheckins, type CheckinStamp } from "./checkins";

export type FootprintState =
  | { kind: "not_asked" }
  | { kind: "asking" }
  | { kind: "read"; stamps: CheckinStamp[] }
  | { kind: "refused" }
  | { kind: "unreachable" };

/**
 * How a stamp names the stop it is on. The server stores this decision (`target_key`,
 * `backend/app/schemas.py`), and `TripItinerary.tsx` looks stamps up with the same
 * spelling; this is the one copy of the form so a fourth surface cannot invent a fifth.
 */
export const activityTargetKey = (activityId: string) => `activity:${activityId}`;

/** The stamp for one plan activity, or null. Only ever called on a `read` arm. */
export function stampForActivity(
  state: FootprintState,
  activityId: string | null,
): CheckinStamp | null {
  if (state.kind !== "read" || !activityId) return null;
  const key = activityTargetKey(activityId);
  return state.stamps.find((stamp) => stamp.target_key === key) ?? null;
}

/**
 * The day a stamp was made, as it is painted under the pin.
 *
 * Date and no clock, on purpose: `e2e/_mapLabelRules.ts` forbids a time painted on the map glass
 * (`clock-time`, `/\b\d{1,2}:\d{2}\b/`), because a pin carrying a clock reads as a promise about a
 * value the planner may not hold -- and `e2e/qoder-map-ord0403-named-control-and-labels.spec.ts`
 * enforces that list against the canvas's own rendered text. A stamp day is still the server's
 * `stamped_at`, just rendered to the resolution the surface is allowed to speak.
 */
export function formatStampDay(locale: string, iso: string): string {
  const parts = new Intl.DateTimeFormat(locale, { month: "2-digit", day: "2-digit" }).formatToParts(
    new Date(iso),
  );
  const pick = (type: "month" | "day") => parts.find((part) => part.type === type)?.value ?? "";
  const month = pick("month");
  const day = pick("day");
  return locale.startsWith("zh") ? `${Number(month)}月${Number(day)}日` : `${month}/${day}`;
}

/** One read, on mount and whenever the journey changes. Nothing is kept between journeys. */
export function useFootprints(journeyId: string | null): FootprintState {
  const [state, setState] = useState<FootprintState>({ kind: "asking" });

  const load = useCallback(async () => {
    if (!journeyId) {
      setState({ kind: "not_asked" });
      return;
    }
    setState({ kind: "asking" });
    const answer = await readCheckins(journeyId);
    if (answer.kind === "read") setState({ kind: "read", stamps: answer.stamps });
    else if (answer.kind === "refused") setState({ kind: "refused" });
    else setState({ kind: "unreachable" });
  }, [journeyId]);

  useEffect(() => {
    void load();
  }, [load]);

  return state;
}

/**
 * The sentence the surface owes the traveller for this arm. Lives here rather than in the component
 * so the words and the arm cannot drift apart: `count` only ever interpolates into the `read` arm,
 * and the empty `read` arm still names itself as *this read*, not as a fact about the traveller.
 */
export function footprintWords(locale: string, state: FootprintState): string {
  switch (state.kind) {
    case "read":
      return state.stamps.length === 0
        ? mapCopy(locale, "footprintNone")
        : mapCopy(locale, "footprintStamped").replace("%s", String(state.stamps.length));
    case "refused":
      return mapCopy(locale, "footprintRefused");
    case "unreachable":
      return mapCopy(locale, "footprintUnreachable");
    case "not_asked":
      return mapCopy(locale, "footprintNotAsked");
    case "asking":
      return mapCopy(locale, "footprintAsking");
  }
}

/** Whether the map may paint a stamp claim at all on this arm. Three arms say no. */
export function footprintIsAnswered(state: FootprintState): boolean {
  return state.kind === "read";
}
