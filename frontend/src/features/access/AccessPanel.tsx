import { useCallback, useEffect, useState } from "react";
import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import { formatV8Date } from "../../lib/v8Formatting";
import {
  changeMemberRole,
  createShareGrant,
  leaveJourney,
  readActivity,
  readJourneys,
  readJourneyReference,
  readMembers,
  readMemberRoleLimits,
  readRoleRules,
  readShareGrants,
  readSharingDoors,
  readTraveller,
  RejectionWithFacts,
  revokeShareGrant,
  SHARING_DOOR_SUFFIXES,
  withdrawMember,
  type ActivityEvent,
  type DoorProbe,
  type JourneyMemberRow,
  type JourneyOrderChoice,
  type JourneySummary,
  type MemberRoleLimits,
  type RoleRule,
  type ShareGrant,
} from "./accessDoors";
import {
  accessCopyFor,
  fillAccess,
  type AccessCopyKey,
} from "./accessCopy";
import {
  actionNamesAMember,
  adminCapState,
  isScopeKey,
  memberActionKey,
  moveableRoleChoices,
  roleLabelKey,
  ROLE_COLLABORATOR,
  ROLE_OWNER,
  VISIBILITY_LINK_READ,
  VISIBILITY_PRIVATE,
  VISIBILITY_PUBLIC,
  permissionConfirmationKey,
  refusalSentenceKey,
  removalConfirmationKey,
  ROLE_LADDER,
  VISIBILITY_LADDER,
  visibilitySwitchKey,
  invitationQuotaKey,
  DEFAULT_JOURNEY_ORDER,
  JOURNEY_ORDERS_NOT_ANSWERED,
  JOURNEY_SORT_LADDER,
  sortLabelKey,
} from "./accessVocabulary";
import { journeyOrderVerdict, type JourneyOrderReason } from "./journeyOrderVerdict";

/** ORD-0518: the verdict's reason in the traveller's language. Every reason has a sentence in both
 *  languages here; a reason this file has no sentence for is a bug in this map, and the panel says
 *  nothing rather than quoting the module's identifier. */
const ORDER_REASON_COPY: Record<JourneyOrderReason, AccessCopyKey> = {
  no_rows: "access.orderReasonNoRows",
  no_created_at_in_row: "access.orderReasonNoCreatedAt",
  no_updated_at_in_row: "access.orderReasonNoUpdatedAt",
  row_without_start_at: "access.orderReasonRowWithoutStart",
  compared_start_at: "access.orderReasonStartAt",
  compared_title: "access.orderReasonTitle",
  title_folds_differently: "access.orderReasonFoldDivergent",
};
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
  /** The rows only. ORD-0518 tried for a door-side vouch (a response header naming the order) and
   *  gave it up when that meant touching the door, the contract and the generated client -- all of
   *  which another order is mid-flight on. What the screen says about the order is therefore decided
   *  from these rows, in `journeyOrderVerdict.ts`, and says it cannot check when they carry nothing
   *  to check. */
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

export function AccessPanel({
  initialJourneyId = null,
}: {
  initialJourneyId?: string | null;
} = {}) {
  const { locale, t } = useLocale();
  const zh = locale.startsWith("zh");
  const say = (key: AccessCopyKey, params: Record<string, string | number> = {}) =>
    fillAccess(accessCopyFor(locale, key), params);

  const [trips, setTrips] = useState<TripsState>({ phase: "loading" });
  /* ORD-0477: the order this list is in is state the screen holds and says out loud, because the
     order is the server's answer to a word this screen sent -- not a side effect of row order. */
  /* ORD-0518 renamed this state from `sort` to `order`, and widened it: the screen now also offers
     the order that asks for no word at all, so the value is `JourneyOrderChoice` (tier or null). */
  const [order, setOrder] = useState<JourneyOrderChoice>(DEFAULT_JOURNEY_ORDER);
  /** ORD-0527: the trip is not only picked here.  The shell already holds one
   *  (`citynexus.v8.journey_id`), and a panel that waits for a manual pick leaves a roster with rows
   *  in the store painting the empty cell -- which is what the commander measured at 390x844.  The
   *  reference is used only after the list door answers a row for it; a stale one says so. */
  const [chosen, setChosen] = useState<string | null>(null);
  const [referenceState, setReferenceState] = useState<"unknown" | "matched" | "stale">("unknown");
  const [activity, setActivity] = useState<ActivityState>({ phase: "idle" });
  const [access, setAccess] = useState<AccessRead>({ phase: "loading" });
  const [members, setMembers] = useState<JourneyMemberRow[]>([]);
  const [grants, setGrants] = useState<ShareGrant[]>([]);
  const [roleRules, setRoleRules] = useState<RoleRule[] | null>(null);
  /** What the member doors can say about seats: the cap the server enforces, and the names its
   *  registry holds for the ids this trip's roster carries. Both come from doors, never from here. */
  const [limits, setLimits] = useState<MemberRoleLimits | null>(null);
  const [names, setNames] = useState<Record<string, string | null>>({});
  const [picking, setPicking] = useState<string | null>(null);
  const [rosterFailed, setRosterFailed] = useState<Attribution | null>(null);
  const [pending, setPending] = useState<
    { kind: "grant"; id: string } | { kind: "member"; id: string }
    | { kind: "role"; id: string; role: string } | { kind: "exit"; id: string } | null
  >(null);
  const [writeState, setWriteState] = useState<{
    busy: boolean;
    refused: string | null;
    origin: "invite" | "roster";
  }>({ busy: false, refused: null, origin: "invite" });
  const [pickVisibility, setPickVisibility] = useState<string>(VISIBILITY_LINK_READ);
  const [pickExpiry, setPickExpiry] = useState<string>("");

  const loadTrips = useCallback(async (choice: JourneyOrderChoice) => {
    setTrips({ phase: "loading" });
    try {
      setTrips({ phase: "ready", rows: await readJourneys(choice) });
    } catch (cause) {
      setTrips({
        phase: "failed",
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        attribution: classifyReadFailure(cause),
      });
    }
  }, []);

  useEffect(() => {
    void loadTrips(order);
  }, [order, loadTrips]);

  const loadRoster = useCallback(async (journeyId: string) => {
    setRosterFailed(null);
    try {
      const [memberRows, grantRows, rules, seatLimits] = await Promise.all([
        readMembers(journeyId),
        readShareGrants(journeyId),
        readRoleRules(journeyId),
        readMemberRoleLimits(journeyId),
      ]);
      setMembers(memberRows);
      setGrants(grantRows);
      setRoleRules(rules);
      setLimits(seatLimits);
      // One registry read per id the roster named. A name this door did not answer stays `null`, and
      // the row says it has no name rather than falling back to the uuid beside it.
      const ids = Array.from(new Set(memberRows.map((row) => row.traveler_id)));
      const read = await Promise.all(
        ids.map(async (id) => [id, (await readTraveller(id))?.display_name ?? null] as const),
      );
      setNames(Object.fromEntries(read));
    } catch (cause) {
      setRosterFailed(classifyReadFailure(cause));
    }
  }, []);

  /** Every write path ends by re-reading the doors. The screen never edits its own copy of a row to
   *  look like the write succeeded: what appears after a tap is what the server answered next. */
  const afterWrite = useCallback(
    async (journeyId: string) => {
      setPending(null);
      setPicking(null);
      await Promise.all([loadRoster(journeyId), readActivity(journeyId).then((events) => setActivity({ phase: "ready", rows: events })).catch(() => undefined)]);
    },
    [loadRoster],
  );

  /** The refusal is named by `error.reason_code` in the body, not by the status code and not by a
   *  guess from `details`. The status alone cannot tell "already withdrawn" from "no such row". */
  const refusalKey = (cause: unknown): string => {
    if (!(cause instanceof RejectionWithFacts)) return "other";
    const envelope = cause.envelope as { error?: { reason_code?: unknown } } | undefined;
    const code = envelope?.error?.reason_code;
    return typeof code === "string" && code.length > 0 ? code : `http_${cause.http_status}`;
  };

  /** Whose seat this device may speak for: the trip's own traveller, as the list door named it.
   *  Same value ORD-0436 already signs invitations with, and the same honest limitation -- it is a
   *  claim, not a session, and the doors below check it against the member set. */
  const signer =
    (trips.phase === "ready" ? trips.rows : []).find((row) => row.id === chosen)?.traveler_id ?? null;

  const issueInvitation = useCallback(async () => {
    if (chosen === null) return;
    if (signer === null) {
      // The list door did not name an owner for this trip, so there is no signature this device is
      // willing to invent for it.
      setWriteState({ busy: false, refused: "nosigner", origin: "invite" });
      return;
    }
    setWriteState({ busy: true, refused: null, origin: "invite" });
    try {
      await createShareGrant(chosen, {
        visibility: pickVisibility,
        created_by: signer,
        expires_at: pickExpiry === "" ? null : new Date(pickExpiry).toISOString(),
      });
      setWriteState({ busy: false, refused: null, origin: "invite" });
      await afterWrite(chosen);
    } catch (cause) {
      setWriteState({ busy: false, refused: refusalKey(cause), origin: "invite" });
    }
  }, [afterWrite, chosen, pickExpiry, pickVisibility, signer]);

  const confirmWithdrawal = useCallback(async () => {
    if (chosen === null || pending === null) return;
    setWriteState({ busy: true, refused: null, origin: "roster" });
    try {
      if (pending.kind === "grant") await revokeShareGrant(chosen, pending.id);
      else if (pending.kind === "member") await withdrawMember(chosen, pending.id);
      else if (pending.kind === "exit") await leaveJourney(chosen, pending.id);
      else if (signer !== null) await changeMemberRole(chosen, pending.id, pending.role, signer);
      else {
        setWriteState({ busy: false, refused: "nosigner", origin: "roster" });
        return;
      }
      setWriteState({ busy: false, refused: null, origin: "roster" });
      await afterWrite(chosen);
    } catch (cause) {
      setWriteState({ busy: false, refused: refusalKey(cause), origin: "roster" });
    }
  }, [afterWrite, chosen, pending, signer]);

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
    await loadRoster(journeyId);
  }, [loadRoster]);

  /** ORD-0538: what an empty state is allowed to offer.  Five cells, five ways forward, and every one
   *  of them goes through a door or a router this product already has:
   *   - no trip listed          -> the shell's own 今日 (route "hub"), through the same navigate the
   *                                `poi-import` panel uses
   *   - no action recorded      -> re-read `agent-feed`, which is the read that came back empty
   *   - role door silent        -> re-read `acl/roles`
   *   - no members listed       -> move to the invitation form that is already on this screen
   *   - no invitation issued    -> issue one through `POST /share-grants`, the door below already uses
   *  None of them is a decoration: each either re-reads a door, moves the route, focuses a control that
   *  exists, or writes through a door that exists.  There is no new endpoint in this list.
   */
  const reloadActivity = useCallback(async () => {
    if (chosen === null) return;
    try {
      setActivity({ phase: "ready", rows: await readActivity(chosen) });
    } catch (cause) {
      setActivity({
        phase: "failed",
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        attribution: classifyReadFailure(cause),
      });
    }
  }, [chosen]);

  const reloadRoleRules = useCallback(async () => {
    if (chosen === null) return;
    try {
      setRoleRules(await readRoleRules(chosen));
    } catch {
      setRoleRules(null);
    }
  }, [chosen]);

  const focusInviteForm = useCallback(() => {
    const field = document.querySelector('[data-testid="access-invite-visibility"]');
    if (field === null) return;
    field.scrollIntoView({ block: "center" });
    (field as HTMLElement).focus();
  }, []);

  /** The invitation the empty state offers is the same write the form below makes, with this
   *  screen's own default visibility.  It is refused out loud when the list door named no traveller
   *  to sign with -- the screen does not invent a signature to make the button work. */
  const issueLinkGrant = useCallback(async () => {
    if (chosen === null) return;
    if (signer === null) {
      setWriteState({ busy: false, refused: "nosigner", origin: "roster" });
      return;
    }
    setWriteState({ busy: true, refused: null, origin: "roster" });
    try {
      await createShareGrant(chosen, {
        visibility: VISIBILITY_LINK_READ,
        created_by: signer,
        expires_at: null,
      });
      setWriteState({ busy: false, refused: null, origin: "roster" });
      await afterWrite(chosen);
    } catch (cause) {
      setWriteState({ busy: false, refused: refusalKey(cause), origin: "roster" });
    }
  }, [afterWrite, chosen, signer]);

  const emptyAction = (testid: string, key: AccessCopyKey, action: () => void) => (
    <button
      className="cn-access-row-action"
      data-testid={testid}
      type="button"
      onClick={action}
    >
      {say(key)}
    </button>
  );

  /** The shell's own trip reference, honoured as soon as the list door can vouch for it.  Nothing is
   *  loaded from a reference the door did not answer a row for: that is the stale-id hazard
   *  `lib/failureAttribution.ts` documents, and here it is named on screen instead of left blank. */
  useEffect(() => {
    if (trips.phase !== "ready" || chosen !== null) return;
    const reference = initialJourneyId ?? readJourneyReference();
    if (reference === null) {
      setReferenceState("unknown");
      return;
    }
    if (trips.rows.some((row) => row.id === reference)) {
      setReferenceState("matched");
      void openTrip(reference);
    } else {
      setReferenceState("stale");
    }
  }, [chosen, initialJourneyId, openTrip, trips]);

  /** Whose seat this device may speak for, and what that seat is on this trip.  Both come from
   *  doors: the traveller from the list row, the seat from the member set, the capability from the
   *  seeded ACL table.  Nothing here is inferred from a name, a colour or a guess. */
  const mySeat = signer === null ? null : members.find((row) => row.traveler_id === signer) ?? null;
  const myCapabilities =
    (roleRules ?? []).find((rule) => mySeat !== null && rule.role === mySeat.role)?.capabilities ?? null;
  const mayManageMembers = myCapabilities !== null && myCapabilities.includes("MEMBER_MANAGE");

  const rows = trips.phase === "ready" ? trips.rows : [];
  const chosenTitle =
    trips.phase === "ready" ? rows.find((row) => row.id === chosen)?.title ?? null : null;
  const activities = activity.phase === "ready" ? activity.rows : [];
  const sharing = access.phase === "present" || access.phase === "partial" ? access : null;
  const expiry = sharing && sharing.expiries.length > 0 ? sharing.expiries[0] : null;
  // No door in this product carries a *invitation* limit, so `cap` stays null and the quota line says
  // so instead of dividing the roster by a number nobody sent.
  const quotaKey = invitationQuotaKey({ used: sharing?.rosterSize ?? null, cap: null });
  /** The ADMIN cap is the other kind of "上限", and this one the server really does enforce: the two
   *  numbers come from `GET /member-role-limits`, the same answer the role door refuses on. */
  const capState = adminCapState({
    cap: limits?.max_administrators ?? null,
    used: limits?.admin_count_in_force ?? null,
  });

  const roleLabel = (role: string): string => {
    const key = roleLabelKey(role);
    return key ? say(key) : say("access.roleUnread", { role });
  };

  /** The ACL row carries a dictionary key, not a sentence (`member.scope.*`, seeded at
   *  `backend/app/db/session.py`). Looking it up here is the whole point: the words live in the one
   *  dictionary the server is allowed to name, and a key it invents is reported as unreadable
   *  rather than answered with a string this file happens to hold. */
  const scopeSentence = (key: string): string => {
    if (!isScopeKey(key)) return say("access.scopeKeyUnread", { key });
    const words = t(key);
    return words === key ? say("access.scopeKeyUnread", { key }) : words;
  };

  /** The scope sentence for a member's role, taken from the ACL rows the same trip just answered.
   *  No row, no sentence -- the screen does not fall back to a role name it read off a member row. */
  const scopeForRole = (role: string): string | null => {
    const rule = (roleRules ?? []).find((row) => row.role === role);
    return rule ? scopeSentence(rule.scope_message_key) : null;
  };

  const grantLabel = (visibility: string): string => {
    const entry = VISIBILITY_LADDER.find((row) => row.level === visibility);
    return entry ? say(entry.labelKey) : visibility;
  };

  /** `member_state` is the server's word for where this row stands. Each one it can send gets its
   *  own sentence; anything else is reported as unreadable rather than dressed up as "active". */
  const memberStateSentence = (member: JourneyMemberRow): string => {
    if (member.removed_at !== null) {
      return say("access.memberStateRemoved", { removed: formatV8Date(member.removed_at, locale) });
    }
    if (member.member_state === "MEMBER_IN_FORCE") return say("access.memberStateActive");
    // The server's word for a membership past its own deadline is `MEMBER_LAPSED`
    // (`backend/app/schemas.py`, `MemberState`); `MEMBER_EXPIRED` was never one of its answers, so
    // the branch that tested for it made every lapsed row read as "a state I cannot name".
    if (member.member_state === "MEMBER_LAPSED") return say("access.memberStateExpired");
    if (member.member_state === "MEMBER_WITHDRAWN") {
      return say("access.memberStateRemoved", { removed: formatV8Date(member.removed_at ?? "", locale) });
    }
    return say("access.memberStateUnknown", { state: member.member_state });
  };

  /* ORD-0477: the word for the order in use, taken from the ladder rather than from the token the
     server uses. `null` means this screen owns no sentence for that order, and it is said as that --
     the machine word is quoted, never worn as a label. */
  const tierWordKey = sortLabelKey(order);

  /* ORD-0518: what this screen is allowed to say about the order, decided from the rows the door
     sent and not from the button that was pressed. Three honest outcomes: the rows agree, the rows
     contradict it (and the row where it breaks is named), or these rows carry nothing to compare --
     in which case the screen says it cannot check, rather than claiming a time order.
     The verdict module returns a *reason*, not a sentence, because the reason has to be said in the
     traveller's language: an English clause interpolated into a Chinese sentence is a second defect
     on top of the one being reported. The stored column the check compared is put on the element as
     `data-compared` for the gate to read, never painted. */
  const orderVerdict = trips.phase === "ready" ? journeyOrderVerdict(rows, order) : null;
  const orderReasonKey = orderVerdict === null ? null : ORDER_REASON_COPY[orderVerdict.reason];
  const orderSentence =
    orderVerdict === null || orderReasonKey === null
      ? ""
      : orderVerdict.state === "CONSISTENT"
        ? say("access.orderChecked", { reason: say(orderReasonKey) })
        : orderVerdict.state === "INCONSISTENT"
          ? say("access.orderBroken", { row: String(orderVerdict.first_break_row ?? 0) })
          : orderVerdict.state === "NOT_VERIFIABLE"
            ? say("access.orderUnverifiable", {
                reason: say(orderReasonKey, { row: String(orderVerdict.first_break_row ?? 0) }),
              })
            : "";

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
        {/* ORD-0477 -- which order the list below is in, said in words and set by a control the
            traveller can see. The tier the screen is on is this screen's own state, so it stays
            readable while the rows are still arriving; the rows themselves are the door's answer to
            the word this screen sent. */}
        <div className="cn-access-sort" data-testid="access-sort">
          <span className="cn-access-picker-label">{say("access.sortHeading")}</span>
          <p className="cn-access-sort-current" data-testid="access-sort-current">
            {say("access.sortCurrent", { tier: tierWordKey ? say(tierWordKey) : String(order) })}
          </p>
          <div className="cn-access-sort-tiers" role="group" aria-label={say("access.sortHeading")}>
            {JOURNEY_SORT_LADDER.map((row) => (
              <button
                key={row.labelKey}
                type="button"
                className="cn-access-sort-tier"
                data-testid={`access-sort-tier-${row.choice ?? "CREATION_ORDER"}`}
                aria-pressed={row.choice === order ? "true" : "false"}
                onClick={() => setOrder(row.choice)}
              >
                {say(row.labelKey)}
              </button>
            ))}
          </div>
          {/* ORD-0518 -- what this screen asked, and what the door answered.  Two sentences on
              purpose: the second is built only off `X-CityNexus-Journey-Sort`, so a server that
              ignored the word cannot be reported on screen as having sorted anything. */}
          {trips.phase === "ready" && orderVerdict !== null && orderVerdict.state !== "NO_ROWS" ? (
            <p
              className="cn-access-order-verdict"
              data-testid="access-order-verdict"
              data-state={orderVerdict.state}
              data-reason={orderVerdict.reason}
              {...(orderVerdict.machine_field ? { "data-compared": orderVerdict.machine_field } : {})}
              {...(orderVerdict.blocked_code_point
                ? { "data-blocked-code-point": orderVerdict.blocked_code_point }
                : {})}
              role={orderVerdict.state === "INCONSISTENT" ? "alert" : "status"}
            >
              {orderSentence}
            </p>
          ) : null}
          {/* The orders the commander asked this screen to offer that the door has no field for.
              Named on the glass, because a control list that quietly omits them reads as "these are
              all the orders there are". */}
          <div className="cn-access-sort-absent" data-testid="access-sort-absent">
            <span className="cn-access-picker-label">{say("access.noTierHeading")}</span>
            {JOURNEY_ORDERS_NOT_ANSWERED.map((row) => (
              <p
                key={row.nameKey}
                className="cn-access-sort-absent-line"
                data-testid={`access-no-tier-${row.nameKey}`}
              >
                {say(row.nameKey)} — {say(row.becauseKey)}
              </p>
            ))}
          </div>
          {/* The one tier whose order surprises a Chinese-speaking traveller: characters, not pinyin.
              Said only while that tier is the one in use, so the sentence is never about a different
              order than the list below is actually in. */}
          {tierWordKey === "access.sortTitle" ? (
            <p className="cn-access-sort-note" data-testid="access-sort-note">
              {say("access.sortTitleNote")}
            </p>
          ) : null}
        </div>
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
          /* ORD-0538: this cell is wired to no action, on purpose and with numbers -- see the block.
             With nothing listed the shell paints its own onboarding (`trial-setup`), and with a
             reference the list does not know it paints the failure view (`failure-view`); the panel
             this line sits in never mounts in either shape, so an action here could not be tapped,
             tested, or refused. */
          <p data-testid="access-trips-empty">{say("access.pickEmpty")}</p>
        )}
        {/* ORD-0518: the list is rows now, not a closed `<select>`.  A collapsed control held the
            order in its DOM but showed one line of text, so "first and last trip" could not be read
            off the screen without opening it -- and a uuid was the only other thing the old option
            fell back to when a title was empty. Each row speaks its trip's name. */}
        {trips.phase === "ready" && rows.length > 0 && (
          <div className="cn-access-trips">
            <span className="cn-access-picker-label" id="access-trips-label">
              {say("access.pickLabel")}
            </span>
            {chosen === null ? (
              <p className="cn-access-picker-hint" data-testid="access-trips-hint">
                {say("access.pickNone")}
              </p>
            ) : null}
            {/* A scroll box you cannot reach with the keyboard is a trap: the region is focusable and
                named, so the same rows a thumb scrolls are reachable with arrow keys. */}
            <ol
              className="cn-access-trip-rows"
              data-testid="access-trip-roster"
              aria-labelledby="access-trips-label"
              tabIndex={0}
            >
              {rows.map((row, index) => (
                <li
                  key={row.id}
                  className="cn-access-trip-row"
                  data-testid="access-trip-row"
                  data-position={index === 0 ? "first" : index === rows.length - 1 ? "last" : "middle"}
                >
                  <button
                    type="button"
                    className="cn-access-trip-open"
                    data-testid={`access-trip-open-${index}`}
                    aria-current={row.id === chosen ? "true" : undefined}
                    onClick={() => void openTrip(row.id)}
                  >
                    {row.title && row.title.trim() !== "" ? row.title : say("access.tripUntitled")}
                  </button>
                </li>
              ))}
            </ol>
          </div>
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
          <p className="cn-access-muted" data-testid="access-activity-caveat">
            {say("access.activityNoSharingWriter")}
          </p>
          {activities.length === 0 ? (
            <p data-testid="access-activity-empty">
              {say("access.activityEmpty")}{" "}
              {emptyAction("access-empty-reload-activity", "access.emptyReloadActivity",
                () => void reloadActivity())}
            </p>
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

      <section className="cn-access-block" aria-label={say("access.roleLadderHeading")}>
        <h3 className="cn-access-h">{say("access.roleLadderHeading")}</h3>
        {/* An empty answer is an empty state too: the door said 200 with no rows, which used to fall
            through to a `<ul>` with nothing in it and no way forward. */}
        {roleRules === null || roleRules.length === 0 ? (
          <p data-testid="access-role-ladder-empty">
            {say("access.scopeDoorSilent")}{" "}
            {/* The one action that can do something here. This screen only mounts with a trip picked
                (measured in `ord0538_diag_candidate.json`: no reference or an unknown one paints the
                shell's 试行程 wizard or its failure view instead), so the picker and 今日 branches
                that were cut here first had no shape that could reach them, and they were removed
                rather than shipped as buttons nobody can press. */}
            {emptyAction("access-empty-reload-roles", "access.emptyReloadRoles",
              () => void reloadRoleRules())}
          </p>
        ) : (
          <ul className="cn-access-ladder" data-testid="access-role-ladder">
            {roleRules.map((rule) => (
              <li className="cn-access-ladder-row" data-role={rule.role} key={rule.role}>
                <span className="cn-access-role">{roleLabel(rule.role)}</span>
                <span className="cn-access-scope" data-testid="access-role-scope">
                  {scopeSentence(rule.scope_message_key)}{" "}
                  {say("access.roleCapabilityCount", { count: rule.capabilities.length })}
                </span>
                <details className="cn-access-details">
                  <summary>{say("access.capabilityTokens")}</summary>
                  <p>{rule.capabilities.join(", ")}</p>
                </details>
              </li>
            ))}
          </ul>
        )}
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

      <section className="cn-access-block" aria-label={say("access.inviteFormHeading")}>
        <h3 className="cn-access-h">{say("access.inviteFormHeading")}</h3>
        <label className="cn-access-field">
          <span className="cn-access-picker-label">{say("access.inviteFormPick")}</span>
          <select
            data-testid="access-invite-visibility"
            value={pickVisibility}
            onChange={(event) => setPickVisibility(event.target.value)}
          >
            {VISIBILITY_LADDER.map((entry) => (
              <option key={entry.level} value={entry.level}>
                {say(entry.labelKey)}
              </option>
            ))}
          </select>
        </label>
        <label className="cn-access-field">
          <span className="cn-access-picker-label">{say("access.inviteFormExpiry")}</span>
          <input
            data-testid="access-invite-expiry"
            type="datetime-local"
            value={pickExpiry}
            onChange={(event) => setPickExpiry(event.target.value)}
          />
        </label>
        <p className="cn-access-muted" data-testid="access-invite-signer">
          {say("access.inviteFormSigner")}
        </p>
        <button
          data-testid="access-invite-submit"
          type="button"
          disabled={chosen === null || writeState.busy}
          onClick={() => void issueInvitation()}
        >
          {writeState.busy ? say("access.inviteFormBusy") : say("access.inviteFormSubmit")}
        </button>
        {writeState.refused !== null && writeState.refused !== "nosigner" ? (
          <p className="cn-access-refusal" data-testid="access-write-refused">
            {(() => {
              const key = refusalSentenceKey(writeState.refused);
              return key ? say(key) : say("access.writeRefusedUnknown", { code: writeState.refused });
            })()}
          </p>
        ) : null}
      </section>

      {chosen !== null ? (
        <section className="cn-access-block" aria-label={say("access.grantHeading")}>
          <h3 className="cn-access-h">{say("access.grantHeading")}</h3>
          {grants.length === 0 ? (
            <p data-testid="access-grant-empty">
              {say("access.grantEmpty")}{" "}
              {emptyAction("access-empty-issue-link-grant", "access.emptyIssueGrant",
                () => void issueLinkGrant())}
            </p>
          ) : (
            <ul className="cn-access-rows" data-testid="access-grant-list">
              {grants.map((grant) => (
                <li
                  className="cn-access-row"
                  data-revoked={grant.revoked_at !== null}
                  data-testid="access-grant-row"
                  key={grant.id}
                >
                  <span className="cn-access-row-what">
                    {grant.revoked_at !== null
                      ? `${grantLabel(grant.visibility)} · ${say("access.grantRevoked", {
                          revoked: formatV8Date(grant.revoked_at, locale),
                        })}`
                      : grant.expires_at === null
                        ? `${grantLabel(grant.visibility)} · ${say("access.grantNoExpiry")}`
                        : `${grantLabel(grant.visibility)} · ${say("access.grantActive", {
                            expires: formatV8Date(grant.expires_at, locale),
                          })}`}
                  </span>
                  {grant.revoked_at === null ? (
                    <button
                      className="cn-access-row-action"
                      data-testid="access-grant-revoke"
                      type="button"
                      onClick={() => setPending({ kind: "grant", id: grant.id })}
                    >
                      {pending?.kind === "grant" && pending.id === grant.id
                        ? say("access.revokeConfirmButton")
                        : say("access.revokeButton")}
                    </button>
                  ) : null}
                  <details className="cn-access-details">
                    <summary>{say("access.grantTokenSummary")}</summary>
                    <p>{fillAccess(say("access.grantToken"), { token: grant.token })}</p>
                  </details>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {chosen !== null ? (
        <section className="cn-access-block" aria-label={say("access.rosterHeading")}>
          <h3 className="cn-access-h">{say("access.rosterHeading")}</h3>
          {/* ORD-0527: whose seat this device speaks for, said out loud.  An unknown identity used to
              leave the whole block silently empty; it now names itself and names the way in. */}
          <p className="cn-access-scope" data-testid="access-identity">
            {signer === null
              ? `${say("access.identityUnknown")} ${say("access.identityUnknownWay")}`
              : mySeat === null || mySeat.removed_at !== null
                ? say("access.identityNoSeat")
                : say("access.identityLine", {
                    name: names[signer] ? names[signer] as string : say("access.identityUnnamed"),
                    role: roleLabel(mySeat.role),
                  })}
          </p>
          {referenceState === "stale" ? (
            <p className="cn-access-refusal" data-testid="access-reference-stale">
              {say("access.identityStale")}
            </p>
          ) : null}
          {signer !== null && mySeat !== null && mySeat.removed_at === null && !mayManageMembers ? (
            <p className="cn-access-muted" data-testid="access-no-authority">
              {say("access.noAuthorityLine")}
            </p>
          ) : null}
          <p className="cn-access-muted" data-testid="access-admin-cap">
            {capState
              ? say(capState.key, {
                  cap: limits?.max_administrators ?? capState.free,
                  used: (limits?.max_administrators ?? 0) - capState.free,
                })
              : say("access.adminCapUnanswered")}
          </p>
          {rosterFailed !== null ? (
            <p data-testid="access-roster-failed">{copyFor(rosterFailed, zh).body}</p>
          ) : members.length === 0 ? (
            <p data-testid="access-roster-empty">
              {say("access.rosterEmpty")}{" "}
              {emptyAction("access-empty-focus-invite", "access.emptyInviteSomeone", focusInviteForm)}
            </p>
          ) : (
            <ul className="cn-access-rows" data-testid="access-member-list">
              {members.map((member) => {
                // Whose seat this device may speak for, and which roles the door could actually move
                // this seat into. Both decide whether a control is drawn at all: a button whose door
                // always refuses is the copy-exceeds-mechanism defect this file keeps naming.
                const isSelf = member.traveler_id === signer;
                const choices =
                  member.role === ROLE_OWNER
                    ? []
                    : moveableRoleChoices({
                        serverRoles: limits?.roles ?? null,
                        currentRole: member.role,
                      });
                const named = names[member.traveler_id];
                return (
                  <li className="cn-access-row" data-state={member.member_state} data-testid="access-member-row" key={member.id}>
                    <span className="cn-access-row-what">
                      {fillAccess(say("access.memberRow"), {
                        role: roleLabel(member.role),
                        name: named === undefined || named === null || named === ""
                          ? say("access.memberNameUnread")
                          : named,
                      })}{" "}
                      · {memberStateSentence(member)}
                      {scopeForRole(member.role) === null ? null : (
                        <>
                          <br />
                          {say("access.memberScopeLine")}
                          {scopeForRole(member.role)}
                        </>
                      )}
                    </span>
                    {/* ORD-0503's two doors. The role change is offered for a seat that is not the
                        caller's own and that this door can move; leaving is offered only for the
                        caller's own row, because that is the one row the self-exit door can write. */}
                    {mayManageMembers && !isSelf && choices.length > 0 ? (
                      <button
                        className="cn-access-row-action"
                        data-testid="access-member-role-change"
                        type="button"
                        disabled={writeState.busy}
                        onClick={() => setPicking(picking === member.id ? null : member.id)}
                      >
                        {say("access.roleChangeButton")}
                      </button>
                    ) : null}
                    {isSelf && member.removed_at === null ? (
                      <button
                        className="cn-access-row-action"
                        data-testid="access-member-self-exit"
                        type="button"
                        disabled={writeState.busy}
                        onClick={() => setPending({ kind: "exit", id: member.traveler_id })}
                      >
                        {say("access.selfExitButton")}
                      </button>
                    ) : null}
                    {picking === member.id ? (
                      <span className="cn-access-role-choices" data-testid="access-role-choices">
                        <span>{say("access.roleChoicesHeading")}</span>
                        {choices.map((role) => (
                          <button
                            key={role}
                            className="cn-access-row-action"
                            data-testid="access-role-option"
                            type="button"
                            onClick={() => setPending({ kind: "role", id: member.id, role })}
                          >
                            {roleLabel(role)}
                          </button>
                        ))}
                      </span>
                    ) : null}
                    {member.removed_at === null ? (
                      <button
                        className="cn-access-row-action"
                        data-testid="access-member-withdraw"
                        type="button"
                        onClick={() => setPending({ kind: "member", id: member.id })}
                      >
                        {pending?.kind === "member" && pending.id === member.id
                          ? say("access.revokeConfirmButton")
                          : say("access.revokeButton")}
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
          {writeState.origin === "roster" && writeState.refused !== null && writeState.refused !== "nosigner" ? (
            <p className="cn-access-refusal" data-testid="access-roster-refused">
              {(() => {
                const key = refusalSentenceKey(writeState.refused);
                return key ? say(key) : say("access.writeRefusedUnknown", { code: writeState.refused });
              })()}
            </p>
          ) : null}
        </section>
      ) : null}

      {pending !== null ? (
        <p className="cn-access-refusal" data-testid="access-confirm-pending">
          {pending.kind === "grant"
            ? say("access.confirmRevokeGrant")
            : pending.kind === "member"
              ? say("access.confirmRevokeMember")
              : pending.kind === "exit"
                ? say("access.confirmSelfExit")
                : say("access.roleChangePending")}
          <button data-testid="access-confirm-yes" type="button" onClick={() => void confirmWithdrawal()}>
            {say("access.revokeConfirmButton")}
          </button>
          <button data-testid="access-confirm-no" type="button" onClick={() => setPending(null)}>
            {say("access.revokeButton")}
          </button>
        </p>
      ) : null}
    </section>
  );
}
