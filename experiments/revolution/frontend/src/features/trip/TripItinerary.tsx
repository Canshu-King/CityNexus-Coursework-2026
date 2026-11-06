/**
 * Trip itinerary — the answer to "我整个旅行是什么？每天怎么安排？"
 *
 * Built from the authoritative active plan's activities, grouped by the
 * traveller's local date.  It deliberately does NOT depend on a stay option
 * being selected: a journey with a plan always has an itinerary.
 *
 * Long trips use a week selector rather than a 39-item horizontal day strip
 * (design corrections §8).  Missing data is shown as missing.
 */

import { useMemo, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { roleLabel, disruptionLabel } from "../../i18n/travelerLabels";
import {
  dayOfGapCopy,
  journeyProgress,
  progressContractCopy,
} from "../progress/journeyProgress";
import type { V8Data } from "../../app/v8Types";

type Activity = V8Data["active_plan"] extends null
  ? never
  : NonNullable<V8Data["active_plan"]>["activities"][number];

type DayGroup = {
  key: string;      // YYYY-MM-DD
  label: string;    // human label
  activities: Activity[];
};

function localDateKey(iso: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(iso));
  } catch {
    return iso.slice(0, 10);
  }
}

function clock(iso: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(new Date(iso));
  } catch {
    return iso.slice(11, 16);
  }
}

function minutesBetween(start: string, end: string): number | null {
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, Math.round((b - a) / 60000));
}

function dayLabel(key: string, index: number, locale: string): string {
  const date = new Date(`${key}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return key;
  try {
    return new Intl.DateTimeFormat(locale.startsWith("zh") ? "zh-CN" : "en-GB", {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(date);
  } catch {
    return `Day ${index + 1}`;
  }
}

export function TripItinerary({
  data,
  onOpenCopilot,
  onOpenActivity,
}: {
  data: V8Data;
  onOpenCopilot: () => void;
  onOpenActivity: (activityId: string) => void;
}) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const plan = data.active_plan;
  const tz = data.journey.timezone || "UTC";

  const days = useMemo<DayGroup[]>(() => {
    const activities = plan?.activities ?? [];
    const buckets = new Map<string, Activity[]>();
    for (const activity of [...activities].sort((a, b) => a.start_at.localeCompare(b.start_at))) {
      const key = localDateKey(activity.start_at, tz);
      const list = buckets.get(key) ?? [];
      list.push(activity);
      buckets.set(key, list);
    }
    return [...buckets.entries()].map(([key, items], index) => ({
      key,
      label: dayLabel(key, index, locale),
      activities: items,
    }));
  }, [plan, tz, locale]);

  /* ORD-0438: one owner for the two counts a traveller reads as progress.  The day total comes from the
     server's projection, the same number Today quotes, so this screen no longer re-derives a second one
     from its own grouping; the grouping stays in charge of what it was always the only reader of --
     which day holds the cursor. */
  const progress = useMemo(
    () =>
      journeyProgress({
        serverDayCount: data.journey.temporal_shape?.day_count ?? null,
        days: days.map((day) => ({ key: day.key, activityIds: day.activities.map((a) => a.id) })),
        activities: (plan?.activities ?? []).map((a) => ({ id: a.id, status: a.status })),
        currentActivityId: plan?.current_activity_id ?? null,
      }),
    [data.journey.temporal_shape, days, plan],
  );

  const [activeDay, setActiveDay] = useState(0);

  if (!plan || days.length === 0) {
    return (
      <section className="v8-page" data-testid="trip-itinerary-empty">
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{zh ? "行程" : "Trips"}</p>
            <h1>{zh ? "还没有可看的行程" : "No itinerary yet"}</h1>
            <p className="v8-lede">
              {zh
                ? "这个行程还没有选定的方案。先让 AI 帮你规划，选定方案后这里会显示逐日安排。"
                : "This trip has no chosen plan yet. Ask the AI to plan it, then pick a plan and the day-by-day itinerary appears here."}
            </p>
          </div>
        </div>
        <button type="button" className="v8-primary-button" onClick={onOpenCopilot}>
          {zh ? "AI 帮我规划" : "Plan my trip with AI"}
        </button>
      </section>
    );
  }

  // Long trips: a week strip rather than one chip per day.
  const weeks = Math.ceil(days.length / 7);
  const showWeeks = days.length > 7;
  const [week, setWeek] = useState(0);
  const visible = showWeeks ? days.slice(week * 7, week * 7 + 7) : days;

  return (
    <section className="v8-page" data-testid="trip-itinerary">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">{zh ? "行程" : "Trips"}</p>
          <h1 data-testid="trip-day-total">
            {progress.day_total === null
              ? zh
                ? "天数未知"
                : "length unknown"
              : zh
                ? `${progress.day_total} 天行程`
                : `${progress.day_total}-day itinerary`}
          </h1>
          <p className="v8-lede">
            {zh
              ? "逐日安排。已确认的预订会被标记为受保护。"
              : "Day by day. Confirmed bookings are marked as protected."}
          </p>
          <p className="v8-lede" data-testid="trip-progress-contract">
            {progressContractCopy(progress, zh)}
          </p>
          {dayOfGapCopy(progress, zh) && (
            <p className="v8-lede" data-testid="trip-progress-daygap">
              {dayOfGapCopy(progress, zh)}
            </p>
          )}
        </div>
      </div>

      {showWeeks && weeks > 1 && (
        <div className="trip-week-strip" data-testid="trip-week-strip">
          {Array.from({ length: weeks }, (_, index) => (
            <button
              key={index}
              type="button"
              className={index === week ? "active" : ""}
              onClick={() => {
                setWeek(index);
                setActiveDay(0);
              }}
            >
              {zh ? `第 ${index + 1} 周` : `Week ${index + 1}`}
            </button>
          ))}
        </div>
      )}

      <div className="trip-day-strip" data-testid="trip-day-strip">
        {visible.map((day, index) => {
          const globalIndex = showWeeks ? week * 7 + index : index;
          return (
            <button
              key={day.key}
              type="button"
              className={globalIndex === activeDay ? "active" : ""}
              data-testid={`trip-day-${globalIndex}`}
              onClick={() => setActiveDay(globalIndex)}
            >
              <span>{day.label}</span>
              <small>
                {zh ? `${day.activities.length} 项` : `${day.activities.length} stops`}
              </small>
            </button>
          );
        })}
      </div>

      {visible.map((day, index) => {
        const globalIndex = showWeeks ? week * 7 + index : index;
        if (globalIndex !== activeDay) return null;
        return (
          <div className="trip-day-detail" key={day.key} data-testid="trip-day-detail">
            {day.activities.map((activity) => {
              const duration = minutesBetween(activity.start_at, activity.end_at);
              const isCurrent = plan.current_activity_id === activity.id;
              const disruption = plan.disruptions?.find(
                (event) => event.affected_activity_id === activity.id,
              );
              const disrupted = Boolean(disruption);
              return (
                <article
                  key={activity.id}
                  className={`trip-stop${activity.protected ? " protected" : ""}${
                    isCurrent ? " current" : ""
                  }${disrupted ? " disrupted" : ""}`}
                  data-testid="trip-stop"
                  role="button"
                  tabIndex={0}
                  aria-label={`${zh ? "查看详情" : "View details"}: ${activity.title}`}
                  onClick={() => onOpenActivity(activity.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onOpenActivity(activity.id);
                    }
                  }}
                >
                  <div className="trip-stop-time">
                    <b>{clock(activity.start_at, tz)}</b>
                    <small>{clock(activity.end_at, tz)}</small>
                  </div>
                  <div className="trip-stop-body">
                    <b>{activity.title}</b>
                    <div className="trip-stop-meta">
                      {duration != null && (
                        <span>
                          {duration >= 60
                            ? zh
                              ? `${Math.floor(duration / 60)} 小时${duration % 60 ? ` ${duration % 60} 分` : ""}`
                              : `${Math.floor(duration / 60)}h${duration % 60 ? ` ${duration % 60}m` : ""}`
                            : zh
                              ? `${duration} 分钟`
                              : `${duration} min`}
                        </span>
                      )}
                      {activity.type && activity.type !== "visit" && (
                        <span className="trip-stop-type">{roleLabel(activity.type, zh)}</span>
                      )}
                      {activity.protected && (
                        <span className="trip-stop-protected" data-testid="trip-stop-protected">
                          {zh ? "已确认 · 受保护" : "Confirmed · protected"}
                        </span>
                      )}
                      {disrupted && (
                        <span className="trip-stop-disrupted">
                          {disruption
                            ? disruptionLabel(disruption.type, zh)
                            : zh
                              ? "受影响"
                              : "Affected"}
                        </span>
                      )}
                      {isCurrent && (
                        <span className="trip-stop-current">{zh ? "当前" : "Now"}</span>
                      )}
                      <span className="trip-stop-more" aria-hidden="true">
                        {zh ? "详情 ›" : "Details ›"}
                      </span>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        );
      })}
    </section>
  );
}
