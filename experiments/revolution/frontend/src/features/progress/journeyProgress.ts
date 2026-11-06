/**
 * ORD-0438 — the journey's progress, expressed as one contract instead of a second counting system.
 *
 * The two claims this answers, and where each number already lives:
 *   共几天        <- `journey.temporal_shape.day_count`, projected by the server.  ORD-0063 ruled the
 *                    span must not be rounded into days on the client (that called an 08:00-20:00
 *                    trip "2 天" and then divided the budget by a day nobody stored), and a null there
 *                    means the stored window cannot be ordered at all -- which shows as unknown, never
 *                    as 1.  This module keeps that rule and is the only place the itinerary's headline
 *                    gets its number from.
 *   已完成几处     <- `active_plan.activities[].status`, judged with `TERMINAL_ACTIVITY_STATES` from
 *                    `lib/executionWindow.ts` rather than a local list, because a third definition of
 *                    "this stop is over" is exactly the second system this order forbids.  "Completed"
 *                    is reported separately from "settled": a skipped or cancelled stop is over, but it
 *                    was not walked, and the traveller can tell those apart from the count.
 *   第几天        <- the day group that holds the cursor activity.  Groups come in as an argument: the
 *                    grouping rule belongs to the screen that draws the day strip, not to a second copy.
 *
 * There is deliberately no percentage.  `api/citynexus.ts:771` records the same decision on the
 * preparation surface -- the backend refuses to invent a denominator, and a percentage would be one.
 *
 * Copy lives here for the reason `lib/pretripChecklist.ts:20` gives: `LocaleContext.tsx` is under
 * concurrent edit by other lanes.
 */
import { TERMINAL_ACTIVITY_STATES } from "../../lib/executionWindow";

export type ProgressActivity = { id: string; status: string };
export type ProgressDayGroup = { key: string; activityIds: string[] };

export type JourneyProgress = {
  day_total: number | null;
  day_total_source: "server_temporal_shape" | "server_has_no_shape" | "no_activities";
  day_of: number | null;
  day_of_basis: "cursor_group" | "cursor_unreadable" | "no_cursor";
  completed_count: number;
  settled_count: number;
  planned_total: number;
};

export function journeyProgress(input: {
  serverDayCount: number | null | undefined;
  days: ProgressDayGroup[];
  activities: ProgressActivity[];
  currentActivityId: string | null | undefined;
}): JourneyProgress {
  const { serverDayCount, days, activities, currentActivityId } = input;
  const settled = activities.filter((item) => TERMINAL_ACTIVITY_STATES.has(item.status));
  const cursorGroup = currentActivityId
    ? days.findIndex((group) => group.activityIds.includes(currentActivityId))
    : -1;
  return {
    day_total: typeof serverDayCount === "number" ? serverDayCount : null,
    day_total_source: activities.length === 0
      ? "no_activities"
      : typeof serverDayCount === "number" ? "server_temporal_shape" : "server_has_no_shape",
    day_of: cursorGroup >= 0 ? cursorGroup + 1 : null,
    day_of_basis: cursorGroup >= 0
      ? "cursor_group"
      : currentActivityId ? "cursor_unreadable" : "no_cursor",
    completed_count: activities.filter((item) => item.status === "COMPLETED").length,
    settled_count: settled.length,
    planned_total: activities.length,
  };
}

/**
 * The same facts as one sentence, in the shape the order asked for: an empty counter reads as a
 * contract -- what has to happen before it moves -- and never as "no data".  The day position leads
 * whenever the server's cursor can be placed, because "已走到第几天" and "已完成几处" are two different
 * claims and a single branch that hides one behind the other would answer only half the order.  Every
 * clause names the fact that would change it, so nothing here can be read as "everything is fine".
 */
export function progressContractCopy(p: JourneyProgress, zh: boolean): string {
  if (p.planned_total === 0) {
    return zh
      ? "还没排定任何一站：选定方案之后，这里才写第几天、共几天、走完几处。"
      : "No stop is scheduled yet: once a plan is chosen this line starts saying which day, of how many, and how far you got.";
  }
  const skipped = p.settled_count - p.completed_count;
  const parts: string[] = [];
  if (p.day_total === null) {
    parts.push(zh
      ? "服务端没给出整天数（这一趟的起止排不出序），天数留空。"
      : "The server gives no whole-day count (this journey's window cannot be ordered), so the day total stays blank.");
  } else if (p.day_of === null) {
    parts.push(zh ? `共 ${p.day_total} 天。` : `${p.day_total} days.`);
  } else {
    parts.push(zh
      ? `第 ${p.day_of} 天 / 共 ${p.day_total} 天。`
      : `day ${p.day_of} of ${p.day_total}.`);
  }
  if (p.completed_count === 0) {
    parts.push(zh
      ? `${p.planned_total} 站还没走完任何一站 —— 站点状态由服务端改，改完这里自己 +1，这里没有给你打勾的地方。`
      : `none of the ${p.planned_total} stops is walked yet -- the server moves a stop's status and this count follows it, so there is nothing here to tick by hand.`);
  } else {
    parts.push(zh
      ? `已完成 ${p.completed_count}/${p.planned_total} 站`
        + (skipped > 0 ? `，另有 ${skipped} 站被跳过或取消（不算走完）。` : "。")
      : `${p.completed_count}/${p.planned_total} stops completed`
        + (skipped > 0 ? `, ${skipped} more skipped or cancelled (not counted as walked).` : "."));
  }
  return parts.join(zh ? "" : " ");
}

/** Why the day position is blank, in the traveller's words rather than a null. */
export function dayOfGapCopy(p: JourneyProgress, zh: boolean): string | null {
  if (p.day_of !== null || p.planned_total === 0) return null;
  return p.day_of_basis === "no_cursor"
    ? zh
      ? "服务端还没把这一趟定位到某一站，所以「第几天」留空 —— 走到第一站时它会自己出现。"
      : "The server has not placed this journey on a stop yet, so \"which day\" is blank -- it appears once a stop is underway."
    : zh
      ? "服务端给的当前位置不在这一代的任一站上（方案换过一代），所以「第几天」留空。"
      : "The server's current position names no stop on this plan generation (the plan moved on), so \"which day\" is blank.";
}
