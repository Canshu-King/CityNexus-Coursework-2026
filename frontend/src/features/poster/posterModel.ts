/**
 * ORD-0472 — what one long picture of this journey contains.
 *
 * The model reads the same `V8Data` object the itinerary tab renders and reuses that screen's own
 * day grouping, so every stop on the poster is a stop the traveller can already see on the panel.
 * It asks the network nothing and it invents no day, no name and no time: a count the server does
 * not answer with is carried as `null` and shown as unknown, never filled in here.
 *
 * No copy lives in this file. Sentences belong to `i18n/LocaleContext.tsx` (both sides), and the
 * one day-total sentence is imported from `features/progress/journeyProgress.ts`, which is already
 * its only owner.
 */
import { cityCodeLabel } from "../../lib/displayVocabulary";
import {
  journeyProgress,
  progressContractCopy,
  type JourneyProgress,
} from "../progress/journeyProgress";
import { clock, dayLabel, groupActivitiesByDay, localDateKey } from "../trip/TripItinerary";
import type { V8Data } from "../../app/v8Types";

export type PosterStop = {
  /** Start and end in the trip's own zone, formatted the way the itinerary formats them. */
  from: string;
  to: string;
  title: string;
};

export type PosterDay = {
  key: string;
  label: string;
  stops: PosterStop[];
};

export type TripPoster = {
  title: string;
  city: string;
  /** The server's day count for the trip shape; `null` means the shape answers nothing. */
  dayTotal: number | null;
  /** Kept whole so the poster quotes the panel's own progress sentence instead of a paraphrase.
   *  Absent on the guest side, which has no progress to quote -- see `buildPosterFromShared`. */
  progress?: JourneyProgress;
  /** The sentence itself, when the caller already has one; the guest door sends no progress. */
  progressCopy?: string;
  /** A subtitle that is not "city · N days". The guest side needs it: it has a counted number and no
   *  projection, and ORD-0542 is the finding that saying "length unknown" next to that number read
   *  as one line denying the other. */
  subtitleCopy?: string;
  days: PosterDay[];
  stopTotal: number;
};

export function buildTripPoster(data: V8Data, locale: string): TripPoster | null {
  const plan = data.active_plan;
  if (!plan) return null;
  const timeZone = data.journey.timezone || "UTC";
  const grouped = groupActivitiesByDay(plan.activities, timeZone, locale);
  if (grouped.length === 0) return null;

  const days = grouped.map((day) => ({
    key: day.key,
    label: day.label,
    stops: day.activities.map((activity) => ({
      from: clock(activity.start_at, timeZone),
      to: clock(activity.end_at, timeZone),
      title: activity.title,
    })),
  }));

  /* The headline count is the projection the panel quotes, not a recount of these buckets: a plan
     can hold fewer days than the trip is long, and the poster must not silently pick the other
     number. Same owner, same input, same answer as `TripItinerary.tsx`. */
  const progress = journeyProgress({
    serverDayCount: data.journey.temporal_shape?.day_count ?? null,
    days: grouped.map((day) => ({
      key: day.key,
      activityIds: day.activities.map((activity) => activity.id),
    })),
    activities: (plan.activities ?? []).map((activity) => ({
      id: activity.id,
      status: activity.status,
    })),
    currentActivityId: plan.current_activity_id ?? null,
  });

  return {
    title: data.journey.title,
    city: cityCodeLabel(data.journey.city_code, locale),
    dayTotal: progress.day_total,
    progress,
    progressCopy: progressContractCopy(progress, locale.startsWith("zh")),
    days,
    stopTotal: days.reduce((total, day) => total + day.stops.length, 0),
  };
}

/**
 * The sentences and rows a poster shows, built once for both ends of a share link.
 *
 * The traveller's sheet and the guest's poster screen call this and nothing else, so "what the
 * picture says" has one owner. `dayTotal` comes from the server's projection when the payload carries
 * one; where it does not, the caller says so with its own key rather than letting a counted number
 * wear the projection's clothes.
 */
export function posterLinesFor(
  poster: TripPoster,
  locale: string,
  t: (key: string, params?: Record<string, string | number>) => string,
): import("./posterCanvas").PosterLines {
  return {
    kicker: t("poster.kicker"),
    title: poster.title,
    subtitle:
      poster.subtitleCopy ??
      (poster.dayTotal === null
        ? t("poster.daysUnknown", { city: poster.city })
        : t("poster.cityAndDays", { city: poster.city, days: poster.dayTotal })),
    progress: poster.progressCopy ?? "",
    attribution: `${t("poster.attributionLabel")} · ${t("mapComp.attribution")}`,
    days: poster.days.map((day) => ({
      label: day.label,
      count: t("poster.dayStops", { n: day.stops.length }),
      stops: day.stops.map((stop) => ({
        time: `${stop.from} – ${stop.to}`,
        title: stop.title,
      })),
    })),
  };
}

/**
 * A poster read off the guest door's payload: the same days, the same times, no second calendar.
 *
 * `dayTotal` is null and stays null. The trip-length projection is a field of the traveller's own
 * door; `GET /api/v1/shared/journeys/{token}` answers with the journey body plus the four keys its
 * contract whitelist names (`frontend/src/generated/api.ts`), and the length projection is not
 * one of them. So the guest side has no projection to quote, and `posterLinesFor` is handed a
 * *counted* sentence for its progress line instead -- which says out loud that it counted.
 */
export function buildPosterFromShared(input: {
  answer: { title?: string; city_code?: string } | null;
  stops: Array<{ id: string; title: string; place_name?: string | null; start_at: string; end_at: string }>;
  timeZone: string;
  locale: string;
  t: (key: string, params?: Record<string, string | number>) => string;
}): TripPoster | null {
  const { answer, stops, timeZone, locale, t } = input;
  if (!answer || stops.length === 0) return null;
  const buckets = new Map<string, typeof stops>();
  for (const stop of [...stops].sort((a, b) => a.start_at.localeCompare(b.start_at))) {
    const key = localDateKey(stop.start_at, timeZone);
    const list = buckets.get(key) ?? [];
    list.push(stop);
    buckets.set(key, list);
  }
  const days = [...buckets.entries()].map(([key, items], index) => ({
    key,
    label: dayLabel(key, index, locale),
    stops: items.map((stop) => ({
      from: clock(stop.start_at, timeZone),
      to: clock(stop.end_at, timeZone),
      title: stop.place_name?.trim() || stop.title,
    })),
  }));
  const stopTotal = days.reduce((total, day) => total + day.stops.length, 0);
  return {
    title: answer.title ?? "",
    city: cityCodeLabel(answer.city_code ?? "", locale),
    /* Null and staying null: the length projection is a field of the traveller's own door, and
       `GET /api/v1/shared/journeys/{token}` answers with the journey body plus the four keys its
       contract whitelist names (`frontend/src/generated/api.ts`) -- the projection is not one of
       them. ORD-0542's finding was what the *wording* did with that: the subtitle used
       `poster.daysUnknown` ("trip length unknown") while the line under it printed a counted "2 days",
       so one screen denied itself. The count is now the subtitle, and the line under it says where the
       number came from. Nothing here fills in a projection the door did not send. */
    dayTotal: null,
    subtitleCopy: t("sharedPoster.counted", {
      city: cityCodeLabel(answer.city_code ?? "", locale),
      days: days.length,
      stops: stopTotal,
    }),
    progressCopy: t("sharedPoster.countedWhy", { days: days.length }),
    days,
    stopTotal,
  };
}
