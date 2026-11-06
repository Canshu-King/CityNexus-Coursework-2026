/* ORD-0476 slice 2 -- the footprint screen.  ORD-0530 -- grouped by the day the trip was actually on.

What is on this screen and where each part comes from:
  - every row is a row the server answered at `GET /api/v1/journeys/{id}/checkins`. Nothing is
    assembled locally: no stamp appears because a button was pressed, only because the read returned
    it, and no day heading appears unless a stamp is under it;
  - `stamped_at` is the instant the server recorded (the doors take no caller-supplied time --
    `backend/app/main.py`). The DAY and the CLOCK on each row are that instant read in the
    journey's own timezone (`journey.timezone`, from the same payload), never in the device's: the
    same stamp is 09/28 18:20 in Los Angeles and 09/29 09:20 in Asia/Shanghai, and only one of those
    is the day the traveller was standing there. When the trip carries no timezone the screen says so
    out loud instead of letting the device answer quietly;
  - the target's words are resolved by the server from the row it still holds, and a stamp whose
    activity has left the plan reads back `activity_title: null` -- shown as 那一站已不在计划 rather
    than as a blank or a substituted name;
  - a stamp with no note says 没写备注, and every stamp names where it came from in words
    (`footprintSourceWords`); neither cell is ever left empty;
  - the two ways a read can fail are two sentences, not one: `refused` means the server answered and
    would not hand the set over, `unreachable` means no usable answer arrived. The reason code stays
    a value this component switches on and is never printed (`e2e/qoder-no-internal-words-or-sentinels
    .spec.ts` counts SCREAMING_SNAKE on the screen, and an empty read must not become a claim).
*/

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { readCheckins, stampLabel, type CheckinStamp } from "./checkins";
import {
  footprintCopy,
  footprintDayKey,
  footprintDayPhrase,
  footprintSourceWords,
  footprintTimePhrase,
} from "./footprintCopy";
import "./footprint.css";

type Props = { journeyId: string | null; timezone?: string | null };

/** `refused` and `unreachable` are separate arms on purpose: they are different answers, and the
 *  screen owes the traveller the difference. The reason code travels beside the arm as a value. */
type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "read"; stamps: CheckinStamp[] }
  | { kind: "refused"; reason: string }
  | { kind: "unreachable" };

type DayGroup = { key: string; when: Date; stamps: CheckinStamp[] };

export function FootprintPanel({ journeyId, timezone = null }: Props) {
  const { locale, t } = useLocale();
  const [state, setState] = useState<State>({ kind: "idle" });
  const [attempt, setAttempt] = useState(0);

  const load = useCallback(async () => {
    if (!journeyId) return;
    setState({ kind: "loading" });
    const answer = await readCheckins(journeyId);
    if (answer.kind === "read") setState({ kind: "read", stamps: answer.stamps });
    else if (answer.kind === "refused") setState({ kind: "refused", reason: answer.refusal });
    else setState({ kind: "unreachable" });
  }, [journeyId]);

  useEffect(() => {
    void load();
  }, [load, attempt]);

  /* The grouping is a pure function of the rows the server sent and the zone the trip named, so a
   * re-read cannot reorder what is already painted, and no second copy of the set exists anywhere. */
  const days = useMemo<DayGroup[]>(() => {
    if (state.kind !== "read") return [];
    const ordered = [...state.stamps].sort(
      (a, b) => Date.parse(a.stamped_at) - Date.parse(b.stamped_at),
    );
    const groups: DayGroup[] = [];
    for (const stamp of ordered) {
      const when = new Date(stamp.stamped_at);
      const key = footprintDayKey(when, timezone);
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.stamps.push(stamp);
      else groups.push({ key, when, stamps: [stamp] });
    }
    return groups;
  }, [state, timezone]);

  const stamps = state.kind === "read" ? state.stamps : [];
  const zoneNamed = Boolean(timezone);

  return (
    <section className="cn-footprints" data-testid="footprint-panel">
      <header className="cn-footprints__head">
        <div>
          <p className="cnx-eyebrow">{t("checkin.title")}</p>
          <h2>{footprintCopy(locale, "scope")}</h2>
        </div>
        <button
          type="button"
          className="cn-footprints__retry"
          onClick={() => setAttempt((n) => n + 1)}
          disabled={!journeyId}
          data-testid="footprint-retry"
        >
          {footprintCopy(locale, "retry")}
        </button>
      </header>

      {!journeyId ? (
        <p className="cn-footprints__state" data-testid="footprint-no-journey">
          {footprintCopy(locale, "noJourney")}
        </p>
      ) : state.kind === "loading" || state.kind === "idle" ? (
        <p className="cn-footprints__state" data-testid="footprint-loading">
          {footprintCopy(locale, "loading")}
        </p>
      ) : state.kind === "refused" ? (
        <p className="cn-footprints__state" data-testid="footprint-refused">
          {footprintCopy(locale, "refused")}
        </p>
      ) : state.kind === "unreachable" ? (
        <p className="cn-footprints__state" data-testid="footprint-unreachable">
          {footprintCopy(locale, "unreachable")}
        </p>
      ) : stamps.length === 0 ? (
        <p className="cn-footprints__state" data-testid="footprint-empty">
          {footprintCopy(locale, "empty")}
        </p>
      ) : (
        <>
          <p className="cn-footprints__zone" data-testid="footprint-day-zone">
            {zoneNamed
              ? footprintCopy(locale, "dayZone", { zone: timezone as string })
              : footprintCopy(locale, "noZoneHeading")}
          </p>
          {days.map((day) => (
            <div className="cn-footprints__day-group" key={day.key} data-testid="footprint-day-group">
              <h3 className="cn-footprints__day" data-testid="footprint-day">
                {footprintDayPhrase(locale, day.when, timezone)}
              </h3>
              <ul className="cn-footprints__list">
                {day.stamps.map((stamp) => (
                  <li key={stamp.id} className="cn-footprints__row" data-testid="footprint-row">
                    <span className="cn-footprints__when">{footprintTimePhrase(locale, new Date(stamp.stamped_at), timezone)}</span>
                    <span className="cn-footprints__what">
                      {stampLabel(stamp) ?? footprintCopy(locale, "targetGone")}
                    </span>
                    {stamp.note ? (
                      <span className="cn-footprints__note">{stamp.note}</span>
                    ) : (
                      <span className="cn-footprints__note cn-footprints__note--absent">
                        {footprintCopy(locale, "noteAbsent")}
                      </span>
                    )}
                    <span className="cn-footprints__source">
                      {footprintSourceWords(locale, stamp.source)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </>
      )}

      <p className="cn-footprints__tally" data-testid="footprint-tally">
        {footprintCopy(locale, "count", { count: stamps.length })}
        {state.kind === "read" && days.length > 0
          ? ` ${footprintCopy(locale, "dayCount", { count: days.length })}`
          : ""}
      </p>
    </section>
  );
}
