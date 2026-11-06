import type { V8Data } from "../app/v8Types";

export const TERMINAL_ACTIVITY_STATES = new Set(["COMPLETED", "SKIPPED", "CANCELLED"]);

export type ExecutionCursorStatus = "VALID" | "STALE" | "ABSENT_FALLBACK";

export type ExecutionWindow = {
  now: V8Data["active_plan"] extends null ? null : NonNullable<V8Data["active_plan"]>["activities"][number] | null;
  next: NonNullable<V8Data["active_plan"]>["activities"][number] | null;
  nextLeg: NonNullable<V8Data["active_plan"]>["legs"][number] | null;
  unresolved: boolean;
  cursorStatus: ExecutionCursorStatus | null;
};

/**
 * Resolve the live execution window from the authoritative cursor.
 *
 * Three states, deliberately kept distinct:
 *   - cursor null            -> pre-execution; the first non-terminal step is honest;
 *   - cursor names a step    -> that step is Now, the next non-terminal one is Next;
 *   - cursor names nothing   -> stale.  Nothing may be invented from list order, so the
 *     window is empty and `unresolved` tells the surface why.
 *
 * `cursorStatus` is exported beside the window because consumers must state *why* the
 * window is empty: "nothing is due" and "the saved position contradicts this plan" are
 * different facts for the traveller, and a panel that collapses them into one label is
 * the defect this resolver exists to prevent.  It mirrors the backend's own three-state
 * rule rather than trusting `plan.cursor_status`, so a stale cursor is caught even when a
 * payload arrives without that field.
 */
export function liveActivityWindow(plan: V8Data["active_plan"]): ExecutionWindow {
  if (!plan) {
    return { now: null, next: null, nextLeg: null, unresolved: false, cursorStatus: null };
  }
  const activities = plan.activities;
  const cursor = plan.current_activity_id ?? null;
  const index = activities.findIndex((item) => item.id === cursor);
  if (cursor !== null && index === -1) {
    return {
      now: null,
      next: null,
      nextLeg: null,
      unresolved: true,
      cursorStatus: "STALE",
    };
  }
  const cursorStatus: ExecutionCursorStatus = cursor === null ? "ABSENT_FALLBACK" : "VALID";
  const anchor =
    index === -1
      ? activities.findIndex((item) => !TERMINAL_ACTIVITY_STATES.has(item.status))
      : index;
  if (anchor === -1) {
    return { now: null, next: null, nextLeg: null, unresolved: false, cursorStatus };
  }
  const candidate = activities[anchor];
  const now =
    candidate && !TERMINAL_ACTIVITY_STATES.has(candidate.status) ? candidate : null;
  const next =
    activities
      .slice(anchor + 1)
      .find((item) => !TERMINAL_ACTIVITY_STATES.has(item.status)) ?? null;
  const nextLeg = now && next ? plan.legs[anchor] ?? null : null;
  return { now, next, nextLeg, unresolved: false, cursorStatus };
}
