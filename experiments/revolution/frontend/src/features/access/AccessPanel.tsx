import { useCallback, useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { formatV8Date } from "../../lib/v8Formatting";
import {
  readActivity,
  readJourneys,
  readSharingDoors,
  RejectionWithFacts,
  SHARING_DOOR_SUFFIXES,
  type ActivityEvent,
  type DoorProbe,
  type JourneySummary,
} from "./accessDoors";
import {
  accessCopyFor,
  fillAccess,
  type AccessCopyKey,
} from "./accessCopy";
import {
  actionNamesAMember,
  memberActionKey,
  ROLE_COLLABORATOR,
  ROLE_OWNER,
  VISIBILITY_PRIVATE,
  VISIBILITY_PUBLIC,
  permissionConfirmationKey,
  removalConfirmationKey,
  ROLE_LADDER,
  VISIBILITY_LADDER,
  visibilitySwitchKey,
  invitationQuotaKey,
} from "./accessVocabulary";
import "./access.css";

/**
 * ORD-0436 -- 成员与权限, the screen, on the tree that exists tonight.
 *
 * Mounted through the panel registry (`app/v8PanelRegistry.tsx`, surface "route", key "access"), so
 * this folder and three lines elsewhere are the whole change and the shell stays shut -- the same
 * route ORD-0392/ORD-0402 slice 2 took for `/v8/trash`.
 *
 * Two halves, and they are separated on purpose because the server is not finished:
 *  - read from this server right now: the trip list (`GET /api/v1/journeys`) and what happened to
 *    the chosen trip (`GET /api/v1/journeys/{id}/agent-feed`). Both exist on this tree.
 *  - the sharing state itself -- roles, visibility, invitations -- has no door on this tree yet.
 *    ORD-0434 builds it. This screen therefore shows that half as the *decision table it will use*,
 *    says out loud that the door did not answer, and offers no control that would do nothing
 *    (ORD-0385: copy may not exceed the mechanism). The rows are not buttons and not switches.
 *
 * The one thing that must not happen here is an empty roster read as "nobody shares this trip". That
 * is a different fact from "this server did not answer", and the two get different sentences.
 */

type TripsState =
  | { phase: "loading" }
  | { phase: "ready"; rows: JourneySummary[] }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

type ActivityState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; rows: ActivityEvent[] }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

type AccessRead =
  | { phase: "loading" }
  | { phase: "absent" }
  | { phase: "partial"; probes: DoorProbe[]; expiries: string[]; rosterSize: number | null }
  | { phase: "present"; probes: DoorProbe[]; expiries: string[]; rosterSize: number | null }
  | { phase: "failed"; facts: RawFacts | null; attribution: Attribution };

const joinPaths = (paths: readonly string[], zh: boolean) => paths.join(zh ? "、" : ", ");

/** The two moves this screen has a gain-or-loss sentence for. Each row is an input to
 *  `visibilitySwitchKey`, so the line below is that function's answer, not a second sentence. */
const SWITCH_CASES: readonly { from: string | null; to: string | null }[] = [
  { from: VISIBILITY_PRIVATE, to: VISIBILITY_PUBLIC },
  { from: VISIBILITY_PUBLIC, to: VISIBILITY_PRIVATE },
];

/** The confirmation table's cases. Each row is an input to `permissionConfirmationKey`, so what a
 *  traveller reads here is that function's answer, not a second copy of the sentence. */
const CONFIRMATION_CASES: readonly {
  whenKey: AccessCopyKey;
  change: Parameters<typeof permissionConfirmationKey>[0];
}[] = [
  {
    whenKey: "access.whenOtherGainsOnly",
    change: { actorRole: ROLE_OWNER, newRole: ROLE_COLLABORATOR, targetsAnotherMember: true },
  },
  {
    whenKey: "access.whenActorLosesStanding",
    change: { actorRole: ROLE_OWNER, newRole: ROLE_OWNER, targetsAnotherMember: true },
  },
  {
    whenKey: "access.whenRoleUnread",
    change: { actorRole: null, newRole: "ADMIN", targetsAnotherMember: true },
  },
];

export function AccessPanel() {
  const { locale, t } = useLocale();
  const zh = locale.startsWith("zh");
  const say = (key: AccessCopyKey, params: Record<string, string | number> = {}) =>
    fillAccess(accessCopyFor(locale, key), params);

  const [trips, setTrips] = useState<TripsState>({ phase: "loading" });
  const [chosen, setChosen] = useState<string | null>(null);
  const [activity, setActivity] = useState<ActivityState>({ phase: "idle" });
  const [access, setAccess] = useState<AccessRead>({ phase: "loading" });

  const loadTrips = useCallback(async () => {
    setTrips({ phase: "loading" });
    try {
      setTrips({ phase: "ready", rows: await readJourneys() });
    } catch (cause) {
      setTrips({
        phase: "failed",
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        attribution: classifyReadFailure(cause),
      });
    }
  }, []);

  useEffect(() => {
    void loadTrips();
  }, [loadTrips]);

  const openTrip = useCallback(async (journeyId: string) => {
    setChosen(journeyId);
    setActivity({ phase: "loading" });
    setAccess({ phase: "loading" });
    try {
      setActivity({ phase: "ready", rows: await readActivity(journeyId) });
    } catch (cause) {
      setActivity({
        phase: "failed",
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        attribution: classifyReadFailure(cause),
      });
    }
    try {
      const read = await readSharingDoors(journeyId);
      const answered = read.probes.filter((probe) => probe.answered).length;
      const phase = answered === 0 ? "absent" : answered === read.probes.length ? "present" : "partial";
      setAccess({ ...read, phase });
    } catch (cause) {
      setAccess({
        phase: "failed",
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        attribution: classifyReadFailure(cause),
      });
    }
  }, []);

  const rows = trips.phase === "ready" ? trips.rows : [];
  const chosenTitle =
    trips.phase === "ready" ? rows.find((row) => row.id === chosen)?.title ?? null : null;
  const activities = activity.phase === "ready" ? activity.rows : [];
  const sharing = access.phase === "present" || access.phase === "partial" ? access : null;
  const expiry = sharing && sharing.expiries.length > 0 ? sharing.expiries[0] : null;
  // No door in this product carries a member limit, so `cap` stays null and the quota line says so
  // instead of dividing the roster by a number nobody sent.
  const quotaKey = invitationQuotaKey({ used: sharing?.rosterSize ?? null, cap: null });

  return (
    <section
      className="cn-access"
      data-testid="access-panel"
      {...(chosen ? { "data-journey": chosen } : {})}
      aria-label={say("access.title")}
    >
      <header className="cn-access-head">
        <span className="v8-kicker">{say("access.title")}</span>
        <p className="v8-muted">{say("access.sub")}</p>
      </header>

      {/* ---------------------------------------------------------- read from this server */}
      <div className="cn-access-read" data-testid="access-server-read">
        {trips.phase === "loading" && <p data-testid="access-trips-loading">{say("access.pickLoading")}</p>}
        {trips.phase === "failed" && (
          <div role="alert" data-testid="access-trips-failed">
            <p>{copyFor(trips.attribution, zh).body}</p>
            {trips.facts ? (
              <details className="cn-access-details">
                <summary>{t("agent.advancedDetail")}</summary>
                <CnxMeasuredFacts facts={trips.facts} zh={zh} testId="access-trips" envelope={null} />
              </details>
            ) : null}
          </div>
        )}
        {trips.phase === "ready" && rows.length === 0 && (
          <p data-testid="access-trips-empty">{say("access.pickEmpty")}</p>
        )}
        {trips.phase === "ready" && rows.length > 0 && (
          <label className="cn-access-picker">
            <span className="cn-access-picker-label">{say("access.pickLabel")}</span>
            <select
              data-testid="access-journey-picker"
              value={chosen ?? ""}
              onChange={(event) => {
                if (event.target.value) void openTrip(event.target.value);
              }}
            >
              <option value="">{say("access.pickNone")}</option>
              {rows.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.title || row.id}
                </option>
              ))}
            </select>
          </label>
        )}
        {chosenTitle ? <p data-testid="access-chosen">{say("access.selected", { title: chosenTitle })}</p> : null}
      </div>

      {activity.phase === "loading" && <p data-testid="access-activity-loading">{say("access.activityLoading")}</p>}
      {activity.phase === "failed" && (
        <div role="alert" data-testid="access-activity-failed">
          <p>{copyFor(activity.attribution, zh).body}</p>
          {activity.facts ? (
            <details className="cn-access-details">
              <summary>{t("agent.advancedDetail")}</summary>
              <CnxMeasuredFacts
                facts={activity.facts}
                zh={zh}
                testId="access-activity"
                envelope={null}
              />
            </details>
          ) : null}
        </div>
      )}
      {activity.phase === "ready" && (
        <section className="cn-access-block" aria-label={say("access.activityHeading")}>
          <h3 className="cn-access-h">{say("access.activityHeading")}</h3>
          {activities.length === 0 ? (
            <p data-testid="access-activity-empty">{say("access.activityEmpty")}</p>
          ) : (
            <ul className="cn-access-activity" data-testid="access-activity-list">
              {activities.map((event) => {
                const key = memberActionKey(event.event_type);
                // `{name}` is only ever filled by a person this door actually named. Today the feed
                // carries `actor_id` (who tapped), not whose row changed, and the two are different
                // people -- guessing the second from the first would put a name on a sentence the
                // server did not say. So an unnamed member reads as unnamed, not as a template.
                const named = event.member_name;
                const params = actionNamesAMember(event.event_type)
                  ? { name: named ? named : say("access.memberUnnamed") }
                  : {};
                return (
                  <li key={event.id} className="cn-access-activity-row" data-event-type={event.event_type}>
                    <span className="cn-access-when">{formatV8Date(event.timestamp, locale)}</span>
                    <span className="cn-access-what">
                      {key ? say(key, params) : say("access.actionUnknown", { token: event.event_type })}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}

      {/* --------------------------------------------- the door ORD-0434 has not built yet */}
      <p className="cn-access-absent" data-testid="access-door-state">
        {access.phase === "loading"
          ? say("access.doorLoading", { paths: joinPaths(SHARING_DOOR_SUFFIXES, zh) })
          : access.phase === "absent"
            ? say("access.doorAbsent", { paths: joinPaths(SHARING_DOOR_SUFFIXES, zh) })
            : access.phase === "failed"
              ? copyFor(access.attribution, zh).body
              : access.phase === "partial"
                ? say("access.doorPartial", {
                    paths: joinPaths(access.probes.filter((p) => !p.answered).map((p) => p.path), zh),
                  })
                : say("access.doorPresent")}
      </p>

      <section className="cn-access-block" aria-label={say("access.ladderHeading")}>
        <h3 className="cn-access-h">{say("access.ladderHeading")}</h3>
        <ul className="cn-access-ladder" data-testid="access-role-ladder">
          {ROLE_LADDER.map((entry) => (
            <li key={entry.role} className="cn-access-ladder-row" data-role={entry.role}>
              <span className="cn-access-role">{say(entry.labelKey)}</span>
              <span className="cn-access-scope">{say(entry.scopeKey)}</span>
            </li>
          ))}
        </ul>
        <details className="cn-access-details" data-testid="access-role-details">
          <summary>{say("access.roleDetails")}</summary>
          <p>{say("access.roleDetailsBody")}</p>
        </details>
      </section>

      <section className="cn-access-block" aria-label={say("access.visibilityHeading")}>
        <h3 className="cn-access-h">{say("access.visibilityHeading")}</h3>
        <ul className="cn-access-ladder" data-testid="access-visibility-ladder">
          {VISIBILITY_LADDER.map((entry) => (
            <li key={entry.level} className="cn-access-ladder-row" data-level={entry.level}>
              <span className="cn-access-role">{say(entry.labelKey)}</span>
              <span className="cn-access-scope">{say(entry.consequenceKey)}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="cn-access-block" aria-label={say("access.switchHeading")}>
        <h3 className="cn-access-h">{say("access.switchHeading")}</h3>
        <ul className="cn-access-sentences" data-testid="access-switch-lines">
          {SWITCH_CASES.map((entry) => (
            <li key={`${entry.from}->${entry.to}`} data-switch={`${entry.from}->${entry.to}`}>
              {say(visibilitySwitchKey(entry))}
            </li>
          ))}
        </ul>
      </section>

      <section className="cn-access-block" aria-label={say("access.confirmHeading")}>
        <h3 className="cn-access-h">{say("access.confirmHeading")}</h3>
        <ul className="cn-access-sentences" data-testid="access-confirm-table">
          {CONFIRMATION_CASES.map((entry) => (
            <li key={entry.whenKey} className="cn-access-confirm-row" data-case={entry.whenKey}>
              <span className="cn-access-when">{say(entry.whenKey)}</span>
              <span className="cn-access-scope">
                {say(permissionConfirmationKey(entry.change))}
              </span>
            </li>
          ))}
          <li className="cn-access-confirm-row" data-case="removal">
            <span className="cn-access-when">{say("access.whenMemberRemoved")}</span>
            <span className="cn-access-scope">{say(removalConfirmationKey())}</span>
          </li>
        </ul>
      </section>

      <section className="cn-access-block" aria-label={say("access.inviteHeading")}>
        <h3 className="cn-access-h">{say("access.inviteHeading")}</h3>
        <p className="cn-access-scope" data-testid="access-invite-ttl">
          {expiry
            ? say("access.inviteExpires", { expires: formatV8Date(expiry, locale) })
            : say("access.inviteTtlMissing")}
        </p>
        <p className="cn-access-scope" data-testid="access-invite-quota">
          {quotaKey
            ? say(quotaKey)
            : sharing && sharing.rosterSize !== null
              ? say("access.inviteQuotaNoCap", { used: sharing.rosterSize })
              : say("access.inviteQuotaMissing")}
        </p>
      </section>
    </section>
  );
}
