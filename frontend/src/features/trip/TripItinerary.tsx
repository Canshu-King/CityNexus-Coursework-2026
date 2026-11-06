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

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { readCheckins, stampStop } from "../checkin/checkins";
import type { CheckinRefusalFacts } from "../checkin/checkins";
import {
  CHECKIN_COPY,
  CHECKIN_REFUSAL_COPY,
  formatStampedMoment,
  wordsFor,
} from "../checkin/checkinCopy";
import "../checkin/footprint.css";
import { calendarRefusalKey, exportJourneyCalendar } from "./calendarExport";
import { CollectInto } from "../collections/CollectInto";
import { roleLabel, disruptionLabel } from "../../i18n/travelerLabels";
import {
  dayOfGapCopy,
  journeyProgress,
  progressContractCopy,
} from "../progress/journeyProgress";
import {
  catalogueCountSentence,
  readCityCatalogue,
  type CatalogueReadback,
} from "../planning/catalogueReadback";
import type { V8Data } from "../../app/v8Types";

export type Activity = V8Data["active_plan"] extends null
  ? never
  : NonNullable<V8Data["active_plan"]>["activities"][number];

export type DayGroup = {
  key: string;      // YYYY-MM-DD
  label: string;    // human label
  activities: Activity[];
};

/**
 * ORD-0465: exported, not copied.  The share-receiving screen groups the same stops into the same
 * days, and a second implementation of "which calendar day does this instant fall on, in the trip's
 * own timezone" is a second answer to that question -- the two would agree until a trip crossed a
 * date line, and only one of them would be wrong.
 */
export function localDateKey(iso: string, timeZone: string): string {
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

export function clock(iso: string, timeZone: string): string {
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

export function dayLabel(key: string, index: number, locale: string): string {
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

/**
 * ORD-0472: the day grouping the itinerary draws, exported rather than copied.
 *
 * The trip poster has to put the same stops on the same days the traveller is looking at.  A second
 * bucket loop would agree with this one until a trip crossed a zone boundary or a plan arrived
 * unordered, and then only one of the two screens would be wrong.
 */
export function groupActivitiesByDay(
  activities: Activity[],
  timeZone: string,
  locale: string,
): DayGroup[] {
  const buckets = new Map<string, Activity[]>();
  for (const activity of [...activities].sort((a, b) => a.start_at.localeCompare(b.start_at))) {
    const key = localDateKey(activity.start_at, timeZone);
    const list = buckets.get(key) ?? [];
    list.push(activity);
    buckets.set(key, list);
  }
  return [...buckets.entries()].map(([key, items], index) => ({
    key,
    label: dayLabel(key, index, locale),
    activities: items,
  }));
}

export function TripItinerary({
  data,
  onOpenCopilot,
  onOpenActivity,
  onNavigate,
}: {
  data: V8Data;
  onOpenCopilot: () => void;
  onOpenActivity: (activityId: string) => void;
  onNavigate: (route: string) => void;
}) {
  const { locale, t } = useLocale();
  const zh = locale.startsWith("zh");
  const plan = data.active_plan;
  const tz = data.journey.timezone || "UTC";

  /* ORD-0476 slice 2: which stops already carry a footprint, read from the server rather than
     remembered from a tap. The map is keyed by the door's own `target_key`, and every entry in it
     came out of `GET /api/v1/journeys/{id}/checkins` -- a stop never shows 已打卡 because a button was
     pressed, only because the read returned that row. */
  const journeyId = data.journey.id;
  const [stamped, setStamped] = useState<Record<string, string>>({});
  const [pendingStop, setPendingStop] = useState<string | null>(null);
  /* ORD-0500: whose answer this is.  The notice used to be ONE string held by the whole screen and
     it was rendered inside the per-stop loop, so a double-tap on 「Old Town Gate」 painted
     "this stop is already stamped" under all six stops -- a fact about one stop, said about six.
     Measured at 390x844: 6 notice nodes after one duplicate tap.  Keyed by the stop that was
     pressed, the sentence can only appear where the tap was. */
  const [stopNotices, setStopNotices] = useState<Record<string, string>>({});

  const reloadStamps = useCallback(async () => {
    const answer = await readCheckins(journeyId);
    if (answer.kind !== "read") return;
    const next: Record<string, string> = {};
    for (const stamp of answer.stamps) next[stamp.target_key] = stamp.id;
    setStamped(next);
  }, [journeyId]);

  useEffect(() => {
    void reloadStamps();
  }, [reloadStamps]);

  /** The words for one refusal, in this screen's own vocabulary, never the server's token. */
  const refusalWords = (stopTitle: string, facts: CheckinRefusalFacts): string => {
    if (facts.refusal === "CHECKIN_DUPLICATE_STAMP") {
      /* Point at the stamp that won.  The door sends the instant; the uuid it also sends is a
         machine word and stays off this screen (ORD-0251/ORD-0339). */
      return facts.existingStampedAt
        ? wordsFor(CHECKIN_COPY.duplicateAt, zh, { moment: formatStampedMoment(facts.existingStampedAt, zh) })
        : wordsFor(CHECKIN_COPY.duplicateNoMoment, zh);
    }
    const key = CHECKIN_REFUSAL_COPY[facts.refusal];
    /* `button` is this screen's own label for the control, taken from the same call the button uses
       below, so the sentence can only ever name a word the traveller can see. */
    if (key) return wordsFor(CHECKIN_COPY[key], zh, { button: t("checkin.button") });
    if (facts.status === 0) return wordsFor(CHECKIN_COPY.unreachable, zh);
    return wordsFor(CHECKIN_COPY.unexpected, zh, { status: String(facts.status) });
  };

  const stampThisStop = async (activityId: string, stopTitle: string) => {
    setPendingStop(activityId);
    setStopNotices((current) => {
      const next = { ...current };
      delete next[activityId];
      return next;
    });
    const answer = await stampStop(journeyId, activityId);
    if (answer.kind === "stamped") {
      setStamped((current) => ({ ...current, [answer.stamp.target_key]: answer.stamp.id }));
      // Read back rather than trusting the 201's own body for what the trip now holds.
      await reloadStamps();
    } else if (answer.kind === "refused" && answer.facts.refusal === "CHECKIN_DUPLICATE_STAMP") {
      /* The server's answer, taken as the fact it is: a stamp is already on file for this stop, so
         the row is re-read and shown instead of being overwritten -- and the sentence is this
         screen's own words, because a reason code or the server's message on a Chinese screen is
         the defect this project keeps having to fix. */
      setStopNotices((current) => ({
        ...current,
        [activityId]: refusalWords(stopTitle, answer.facts),
      }));
      await reloadStamps();
    } else if (answer.kind === "refused") {
      setStopNotices((current) => ({
        ...current,
        [activityId]: refusalWords(stopTitle, answer.facts),
      }));
      await reloadStamps();
    } else {
      setStopNotices((current) => ({ ...current, [activityId]: wordsFor(CHECKIN_COPY.unreachable, zh) }));
    }
    setPendingStop(null);
  };

  /* ORD-0525: 把这张行程收成能带走的日历。  What the traveller reads back is this screen's own
     dictionary sentence, picked by the refusal the client named -- never a reason code, and never a
     silent nothing when the tap produced no file.  The count in the success sentence is counted out of
     the bytes that came back, so the screen cannot promise an export the file does not contain. */
  const [calendarNotice, setCalendarNotice] = useState<string | null>(null);
  const [calendarPending, setCalendarPending] = useState(false);

  const exportCalendar = async () => {
    setCalendarPending(true);
    setCalendarNotice(null);
    const answer = await exportJourneyCalendar(journeyId);
    if (answer.kind === "exported") {
      setCalendarNotice(t("calendar.done", { events: answer.events }));
    } else if (answer.kind === "refused") {
      setCalendarNotice(t(calendarRefusalKey(answer.refusal)));
    } else {
      setCalendarNotice(t("calendar.unreachable"));
    }
    setCalendarPending(false);
  };

  const days = useMemo<DayGroup[]>(
    () => groupActivitiesByDay(plan?.activities ?? [], tz, locale),
    [plan, tz, locale],
  );

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

  /* ORD-0529: this cell used to be honest but a dead end -- it said "no itinerary yet" and offered
     nothing, so a traveller standing here had no way to the door that actually puts stops on the
     trip.  It now reads the one thing that decides whether a plan could hold stops at all -- how many
     places this server holds for this journey's city -- through ORD-0504's read-back door, and offers
     the import screen as the way through.  Nothing is invented here: the count comes from rows the
     server returned on this open, `null` (a door that did not answer) and `0` (an empty catalogue)
     are different sentences, and the itinerary itself still only renders from the plan. */
  const scheduled = Boolean(plan) && days.length > 0;
  const [catalogue, setCatalogue] = useState<CatalogueReadback | null>(null);
  const [catalogueAsking, setCatalogueAsking] = useState(false);
  useEffect(() => {
    if (scheduled) return;
    let alive = true;
    setCatalogueAsking(true);
    void readCityCatalogue(data.journey.city_code).then((answer) => {
      if (!alive) return;
      setCatalogue(answer);
      setCatalogueAsking(false);
    });
    return () => {
      alive = false;
    };
  }, [scheduled, data.journey.city_code]);

  if (!scheduled) {
    const emptyCatalogue = catalogue !== null && catalogue.source === "server" && catalogue.placeCount === 0;
    const importCopy = zh ? "去导入这趟的地点" : "Import places for this trip";
    const planCopy = emptyCatalogue
      ? zh
        ? "AI 帮我规划（要先有地点）"
        : "Plan my trip with AI (needs places first)"
      : zh
        ? "AI 帮我规划"
        : "Plan my trip with AI";
    return (
      <section className="v8-page" data-testid="trip-itinerary-empty">
        <div className="v8-page-intro compact">
          <div>
            <p className="v8-kicker">{zh ? "行程" : "Trips"}</p>
            <h1>{zh ? "还没有可看的行程" : "No itinerary yet"}</h1>
            <p className="v8-lede" data-testid="trip-itinerary-empty-why">
              {plan
                ? zh
                  ? "这一版方案里还没有站点被排在某一天上。补上站点，这一屏就按天把它们列出来。"
                  : "No stop in this plan sits on a day yet. Add stops and this screen lists them day by day."
                : zh
                  ? "这个行程还没有选定的方案。先让 AI 帮你规划，选定方案后这里会显示逐日安排。"
                  : "This trip has no chosen plan yet. Ask the AI to plan it, then pick a plan and the day-by-day itinerary appears here."}
            </p>
            <p className="v8-lede" data-testid="trip-itinerary-empty-catalogue">
              {catalogueAsking
                ? zh
                  ? "正在向服务器回读这座城有多少地点…"
                  : "Reading back from the server how many places this city holds…"
                : catalogueCountSentence(catalogue, zh)}
            </p>
          </div>
        </div>
        {/* One primary action -- the one that can actually move this trip forward -- plus the other
            door underneath it.  With an empty catalogue, planning is the dead end and importing is
            the way, so the two swap places rather than both sitting there equally loud. */}
        {emptyCatalogue ? (
          <>
            <button
              type="button"
              className="v8-primary-button"
              data-testid="trip-itinerary-empty-action"
              data-contrast-role="control"
              onClick={() => onNavigate("import")}
            >
              {importCopy}
            </button>
            <button
              type="button"
              className="v8-quiet-button"
              data-testid="trip-itinerary-empty-copilot"
              onClick={onOpenCopilot}
            >
              {planCopy}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="v8-primary-button"
              data-testid="trip-itinerary-empty-action"
              data-contrast-role="control"
              onClick={onOpenCopilot}
            >
              {planCopy}
            </button>
            <button
              type="button"
              className="v8-quiet-button"
              data-testid="trip-itinerary-empty-import"
              data-contrast-role="control"
              onClick={() => onNavigate("import")}
            >
              {importCopy}
            </button>
          </>
        )}
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
          {/* ORD-0525: the itinerary's own way out to a calendar app.  A quiet button rather than a
             primary one on purpose -- this screen already carries the traveller's primary calls to
             action, and a download is not louder than choosing what to do next. */}
          <button
            type="button"
            className="v8-quiet-button"
            data-testid="calendar-export"
            disabled={calendarPending}
            onClick={() => void exportCalendar()}
          >
            {calendarPending ? t("calendar.busy") : t("calendar.export")}
          </button>
          {calendarNotice && (
            <p className="v8-lede" data-testid="calendar-export-notice">
              {calendarNotice}
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
                  onClick={() => onOpenActivity(activity.id)}
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
                      {/* ORD-0500: this was decoration (`aria-hidden`) inside a row that claimed
                          `role="button"` while itself containing a real button -- nested interactive
                          content, which a keyboard and a screen reader both refuse.  The row still
                          answers a tap; 「详情」 is now the control it always read as. */}
                      <button
                        type="button"
                        className="trip-stop-more"
                        data-testid="trip-stop-details"
                        aria-label={`${zh ? "查看详情" : "View details"}: ${activity.title}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenActivity(activity.id);
                        }}
                      >
                        {zh ? "详情 ›" : "Details ›"}
                      </button>
                      {/* ORD-0476 slice 2: the tap that makes a footprint. It stops propagation
                          because the row around it is itself a button (「查看详情」), and a control
                          whose click is swallowed by its own row is not an entry a traveller reaches.
                          Its state -- 打卡 or 已打卡 -- comes from the read above, not from local
                          optimism. */}
                      <button
                        type="button"
                        className="cn-stop-checkin"
                        data-testid="stop-checkin"
                        data-state={
                          stamped[`activity:${activity.id}`] ? "stamped" : "fresh"
                        }
                        disabled={pendingStop === activity.id}
                        aria-label={t(
                          stamped[`activity:${activity.id}`]
                            ? "checkin.doneAria"
                            : "checkin.buttonAria",
                          { stop: activity.title },
                        )}
                        onClick={(event) => {
                          event.stopPropagation();
                          void stampThisStop(activity.id, activity.title);
                        }}
                      >
                        {stamped[`activity:${activity.id}`]
                          ? t("checkin.done")
                          : t("checkin.button")}
                      </button>
                    </div>
                    {stopNotices[activity.id] ? (
                      <p
                        className="cn-stop-checkin__notice"
                        data-testid="stop-checkin-notice"
                        role="status"
                      >
                        {stopNotices[activity.id]}
                      </p>
                    ) : null}
                    {/* ORD-0510: the way into the container ORD-0444 built.  The wrapper, not the
                        control, stops propagation -- the row around this is itself a button
                        (「查看详情」), and an entry whose click is swallowed by its own row is not an
                        entry.  Nothing here remembers the write: the line under it is drawn from a
                        fresh `GET /api/v1/collections`, the way the footprint above it is. */}
                    <div
                      className="cn-stop-collect-host"
                      onClick={(event) => event.stopPropagation()}
                      onKeyDown={(event) => event.stopPropagation()}
                    >
                      <CollectInto
                        travelerId={data.journey.traveler_id ?? null}
                        placeId={activity.place_id ?? null}
                        placeTitle={activity.title}
                      />
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
