/**
 * ORD-0436 -- the decisions behind the 成员与权限 screen, with no JSX and no server call.
 *
 * Why this is data and functions rather than strings at the point of use: the order's hardest item
 * is "改权限的确认句按『有没有伤到自己』分两条写". Two sentences sitting next to each other in a
 * component make it easy to pick the wrong one and impossible to tell that you did; a function with
 * one branch per case can be judged on the inputs, and `backend/tests/
 * test_qoder_ord0436_access_vocabulary.py` reads this file's branches against named cases.
 *
 * Every return value is a key into `accessCopy.ts` or `null`. `null` is the honest answer and the
 * panel must render "this screen has no sentence for that" for it -- the failure mode this whole
 * layer exists to prevent is a sentence that describes a change the server never said happened.
 *
 * Shape: `D:/moodle/_apk_analysis_pi_travel/RELATION/relation_and_copy_findings.md` section 1.4.
 */
import type { AccessCopyKey } from "./accessCopy";
import type { JourneyOrderChoice } from "./accessDoors";

export const ROLE_OWNER = "OWNER" as const;
export const ROLE_ADMIN = "ADMIN" as const;
export const ROLE_COLLABORATOR = "COLLABORATOR" as const;

/** The ladder, in the order a traveller reads it (widest first). Each entry carries the key of the
 *  one sentence that says what the role can do -- the shape's "角色档位到一句作用域". */
/** Only the role's *name* is this screen's word. What a role may do is no longer written here at
 *  all: the server's ACL row carries `scope_message_key`, and the sentence lives in the shared
 *  dictionary under exactly that key. Two homes for one utterance is what this replaces. */
export const ROLE_LADDER: readonly { role: string; labelKey: AccessCopyKey }[] = [
  { role: ROLE_OWNER, labelKey: "access.roleOwner" },
  { role: ROLE_ADMIN, labelKey: "access.roleAdmin" },
  { role: ROLE_COLLABORATOR, labelKey: "access.roleCollaborator" },
];

/** The scope keys this product's seeded ACL table is allowed to name (`backend/app/db/session.py`).
 *  Anything else the door sends is reported as unreadable rather than looked up blindly. */
export const SCOPE_KEYS = ["member.scope.owner", "member.scope.admin", "member.scope.collaborator"] as const;

export function isScopeKey(value: unknown): value is string {
  return typeof value === "string" && (SCOPE_KEYS as readonly string[]).includes(value);
}

/** The label this screen uses for a role name the door sent. */
export function roleLabelKey(role: string | null | undefined): AccessCopyKey | null {
  const entry = ROLE_LADDER.find((row) => row.role === role);
  return entry ? entry.labelKey : null;
}

/** Standing, highest first. `null` is "this server named no role", which is lower-bounded at zero
 *  so a missing field can never make a change look self-harming. */
const RANK: Record<string, number> = {
  [ROLE_OWNER]: 3,
  [ROLE_ADMIN]: 2,
  [ROLE_COLLABORATOR]: 1,
};

/** The four levels are the server's, not this screen's: ORD-0434's sharing layer writes its domain
 *  as `ShareVisibility = Literal["PRIVATE", "LINK_READ", "INVITEES_ONLY", "PUBLIC"]`, and a fifth
 *  level this product has no name for ("仅成员可见") is what the first cut of this ladder offered.
 *  A ladder row the write door cannot be set to is a control that does nothing, so the row is gone
 *  rather than kept and hedged. */
export const VISIBILITY_PRIVATE = "PRIVATE" as const;
export const VISIBILITY_LINK_READ = "LINK_READ" as const;
export const VISIBILITY_INVITEES_ONLY = "INVITEES_ONLY" as const;
export const VISIBILITY_PUBLIC = "PUBLIC" as const;

/** Audiences from alone-in-the-world to everyone. Each level owns exactly one consequence sentence
 *  ("可见性逐档一句后果"); the sentence is about who can open the trip, not about the setting name. */
export const VISIBILITY_LADDER: readonly {
  level: string;
  labelKey: AccessCopyKey;
  consequenceKey: AccessCopyKey;
  audience: "self" | "holders" | "invitees" | "everyone";
}[] = [
  {
    level: VISIBILITY_PRIVATE,
    labelKey: "access.visPrivate",
    consequenceKey: "access.visPrivateConsequence",
    audience: "self",
  },
  {
    level: VISIBILITY_LINK_READ,
    labelKey: "access.visLinkRead",
    consequenceKey: "access.visLinkReadConsequence",
    audience: "holders",
  },
  {
    level: VISIBILITY_INVITEES_ONLY,
    labelKey: "access.visInviteesOnly",
    consequenceKey: "access.visInviteesOnlyConsequence",
    audience: "invitees",
  },
  {
    level: VISIBILITY_PUBLIC,
    labelKey: "access.visPublic",
    consequenceKey: "access.visPublicConsequence",
    audience: "everyone",
  },
];

export type PermissionChange = {
  /** The role the person tapping holds right now. */
  actorRole: string | null;
  /** The role the tap is about to give away. */
  newRole: string | null;
  /** True when the tap is about someone else's row, false when it is about the actor's own row. */
  targetsAnotherMember: boolean;
};

/**
 * Which confirmation sentence a permission change deserves.
 *
 * The split is "有没有伤到自己", and it is about the person tapping, not about arithmetic on two
 * people's rows. Two things hurt the actor:
 *  - the tap is on the actor's own row (`targetsAnotherMember` false);
 *  - the tap hands ownership of the trip to somebody else. That one is irreversible from inside the
 *    screen: afterwards the actor is no longer the owner and cannot take the role back, which is
 *    exactly the case the wider sentence exists to warn about. It is not a demotion of the other
 *    person's row, and reading it that way is the mistake this function made first.
 *
 * Anything else is the other person's gain, and says only that. An unread role on either side is
 * NOT treated as harmless: the warning sentence is what the screen owes a traveller when the
 * comparison cannot be made.
 *
 * A change this function cannot read at all answers with `access.confirmUnanswered` rather than
 * `null`, so the screen never has to hold a sentence of its own.
 */
export function permissionConfirmationKey(change: PermissionChange): AccessCopyKey {
  if (change.newRole === null || change.newRole === undefined) return "access.confirmUnanswered";
  if (!change.targetsAnotherMember) return "access.confirmBothChange";
  if (change.actorRole === ROLE_OWNER && change.newRole === ROLE_OWNER) {
    return "access.confirmBothChange";
  }
  const actor = change.actorRole === null ? null : (RANK[change.actorRole] ?? null);
  const next = RANK[change.newRole] ?? null;
  if (actor === null || next === null) return "access.confirmBothChange";
  return "access.confirmOtherGains";
}

/** Removing a member always says what the other person loses, and never dresses it as a change of
 *  degree: the audience sentence above is about roles, this one is about access ending. */
export function removalConfirmationKey(): AccessCopyKey {
  return "access.confirmRemoval";
}

export type VisibilitySwitch = { from: string | null; to: string | null };

/**
 * The private-to-public and public-to-private sentences, one per direction, each about the *other*
 * party's gain or loss.
 *
 * Any other move (members-only to link, invitees to public, a level read as unknown) answers with
 * `access.switchUnanswered`: there is no honest gain-or-loss sentence for it on this tree, and
 * inventing one would claim a fact about an audience change nobody computed.
 */
export function visibilitySwitchKey(switchValue: VisibilitySwitch): AccessCopyKey {
  const { from, to } = switchValue;
  if (from === null || to === null || from === undefined || to === undefined) {
    return "access.switchUnanswered";
  }
  if (from === VISIBILITY_PRIVATE && to === VISIBILITY_PUBLIC) {
    return "access.switchPrivateToPublic";
  }
  if (from === VISIBILITY_PUBLIC && to === VISIBILITY_PRIVATE) {
    return "access.switchPublicToPrivate";
  }
  return "access.switchUnanswered";
}

/**
 * The invitation sentences.
 *
 * `used` and `cap` are what the server said; either one missing keeps its own sentence from
 * claiming a number, and a cap that has not been reached is `null` rather than a reassurance --
 * "you can still invite people" is a promise about the next request, not a reading of this one.
 */
export function invitationQuotaKey(read: {
  used: number | null;
  cap: number | null;
}): AccessCopyKey | null {
  if (read.used === null || read.cap === null) return null;
  return read.used >= read.cap ? "access.inviteLimitReached" : null;
}

/** A TTL only ever comes from the server's own invitation receipt. `null` here means the screen has
 *  to say it did not read one, and an hour count invented on the device would be the exact
 *  fabrication this order is about. */
export function invitationTtlPresent(ttlHours: number | null | undefined): boolean {
  return typeof ttlHours === "number" && Number.isFinite(ttlHours) && ttlHours > 0;
}

/**
 * What an audit action means, in words.
 *
 * Keys are the server's own action names. Today's real ones are `journey.deleted` and
 * `journey.undeleted` (`backend/app/main.py`, the ORD-0383 and ORD-0399 doors); the member,
 * visibility and invitation names are the ones ORD-0434's sharing layer will write -- they sit here
 * so the activity stream says a sentence the moment those rows exist, and nothing before. An action
 * this table does not know returns `null`, which the panel renders as "this server reported an
 * action I have no sentence for", together with the raw token.
 */
const ACTION_KEYS: Record<string, AccessCopyKey> = {
  "journey.created": "access.action.journey.created",
  "journey.deleted": "access.action.journey.deleted",
  "journey.undeleted": "access.action.journey.undeleted",
  "journey.member_added": "access.action.journey.memberAdded",
  "journey.member_role_changed": "access.action.journey.memberRoleChanged",
  "journey.member_removed": "access.action.journey.memberRemoved",
  "journey.visibility_changed": "access.action.journey.visibilityChanged",
  "journey.invitation_sent": "access.action.journey.invitationSent",
  "journey.invitation_revoked": "access.action.journey.invitationRevoked",
};

/** Does this action name put a person in the sentence? The two member actions name someone; the
 *  others do not, and passing `{name}` to a sentence that has no placeholder is how a screen ends
 *  up showing a name the traveller never read. */
const ACTION_NAMES_A_MEMBER: readonly string[] = [
  "journey.member_added",
  "journey.member_role_changed",
  "journey.member_removed",
];

export function memberActionKey(action: string | null | undefined): AccessCopyKey | null {
  if (!action) return null;
  return ACTION_KEYS[action] ?? null;
}

export function actionNamesAMember(action: string | null | undefined): boolean {
  return !!action && ACTION_NAMES_A_MEMBER.includes(action);
}

/**
 * The named refusals this screen can actually be answered with, read off `error.reason_code` in the
 * body -- the standing rule is that a refusal is identified by that field, not by the status code
 * and not by the shape of `details`. Codes with no entry here fall through to the "the server
 * refused and this screen cannot name it" line with the code quoted.
 */
export const REFUSAL_SENTENCES: Record<string, AccessCopyKey> = {
  MEMBER_ALREADY_WITHDRAWN: "access.refuseMemberAlreadyWithdrawn",
  MEMBER_UNKNOWN: "access.refuseMemberUnknown",
  // ORD-0503's two doors. Every one of these is a code the server really answers with -- the raise
  // sites are listed in `backend/tests/test_qoder_ord0503_member_doors.py`, and each has an arm that
  // goes red when its guard is taken out -- so a sentence here is never waiting for a refusal that
  // cannot arrive, and an arrival is never left to the unnamed fallback.
  MEMBER_ACTOR_LAPSED: "access.refuseActorLapsed",
  MEMBER_ACTOR_NOT_IN_JOURNEY: "access.refuseActorNotInJourney",
  MEMBER_ADMIN_CAP_REACHED: "access.refuseAdminCapReached",
  MEMBER_MANAGE_NOT_PERMITTED: "access.refuseMemberManageNotPermitted",
  MEMBER_OWNER_CANNOT_LEAVE: "access.refuseOwnerCannotLeave",
  MEMBER_OWNER_ROLE_IMMUTABLE: "access.refuseOwnerRoleImmutable",
  MEMBER_ROLE_SELF_CHANGE_REFUSED: "access.refuseRoleSelfChange",
  MEMBER_ROLE_UNCHANGED: "access.refuseRoleUnchanged",
  MEMBER_SELF_EXIT_NOT_A_MEMBER: "access.refuseSelfExitNotAMember",
  SHARE_GRANT_ALREADY_REVOKED: "access.refuseGrantAlreadyRevoked",
  SHARE_GRANT_CREATOR_EMPTY: "access.refuseExpiryUnreadable",
  SHARE_GRANT_EXPIRY_IN_PAST: "access.refuseExpiryInPast",
  SHARE_GRANT_EXPIRY_NO_OFFSET: "access.refuseExpiryNoOffset",
  SHARE_GRANT_EXPIRY_UNREADABLE: "access.refuseExpiryUnreadable",
  SHARE_GRANT_JOURNEY_GONE: "access.refuseJourneyGone",
  SHARE_GRANT_NOT_A_MEMBER: "access.refuseNotLinkReadable",
  SHARE_GRANT_NOT_LINK_READABLE: "access.refuseNotLinkReadable",
  SHARE_GRANT_UNKNOWN: "access.refuseGrantUnknown",
};

export function refusalSentenceKey(code: string | null): AccessCopyKey | null {
  return code === null ? null : REFUSAL_SENTENCES[code] ?? null;
}

/**
 * ORD-0503: which roles this screen may offer for a seat, and how many ADMIN seats are left.
 *
 * The candidate list comes from the server's own `roles` answer, and two exclusions are the door's
 * rules rather than this file's taste: the current role (a change that changes nothing is refused
 * with `MEMBER_ROLE_UNCHANGED`), and OWNER in either direction (`MEMBER_OWNER_ROLE_IMMUTABLE`).
 * Offering a choice the door always refuses would be the copy-exceeds-mechanism defect this project
 * already names in `accessCopy.ts`.
 */
export function moveableRoleChoices(read: {
  serverRoles: string[] | null;
  currentRole: string;
}): string[] {
  if (read.serverRoles === null) return [];
  return read.serverRoles.filter((role) => role !== read.currentRole && role !== ROLE_OWNER);
}

/** The cap line needs both numbers the server sent. A cap without the count, or a count without the
 *  cap, is `null` here so the screen says nothing about fullness instead of dividing by a number
 *  nobody answered. */
export function adminCapState(read: {
  cap: number | null;
  used: number | null;
}): { key: AccessCopyKey; free: number } | null {
  if (read.cap === null || read.used === null) return null;
  const free = Math.max(read.cap - read.used, 0);
  return { key: free === 0 ? "access.adminCapFull" : "access.adminCapOpen", free };
}

/* ------------------------------------------------------------- ORD-0477 the order of the trip list

The tiers are the door's, typed off the generated client (`operations["listJourneys"]`), so a tier the
contract renames stops this file compiling instead of living on as a control that does nothing -- the
rule `VISIBILITY_LADDER` above already states for sharing: a ladder row the server cannot be set to is
a row that lies.

The order is the order a traveller reads them in: what the store kept, then what just changed, then
what comes next, then by name. It is not the contract's enum order; the contract's list is a set, this
one is a screen. */
export const SORT_RECENT_UPDATE = "RECENT_UPDATE" as const;
export const SORT_DEPARTURE_DATE = "DEPARTURE_DATE" as const;
export const SORT_TITLE = "TITLE" as const;

/** The orders this screen offers. `choice: null` is 创建序: the order the door answers in when no
 *  `sort` is sent at all. It is offered that way rather than as a fourth word because the door has no
 *  such tier, and inventing one on the screen side would put a control on the glass that the server
 *  cannot be asked for. The other three are the door's tiers, typed off the generated client, so a
 *  tier the contract renames stops this file compiling instead of living on as a button that does
 *  nothing -- the rule `VISIBILITY_LADDER` above already states for sharing. */
export const JOURNEY_SORT_LADDER: readonly { choice: JourneyOrderChoice; labelKey: AccessCopyKey }[] = [
  { choice: null, labelKey: "access.sortCreation" },
  { choice: SORT_RECENT_UPDATE, labelKey: "access.sortRecent" },
  { choice: SORT_DEPARTURE_DATE, labelKey: "access.sortDeparture" },
  { choice: SORT_TITLE, labelKey: "access.sortTitle" },
];

/** ORD-0518: the orders the commander asked this screen to offer that the door cannot answer, said on
 *  the glass instead of quietly narrowed away.  Both are absences with a reason, not oversights:
 *  arrival time is not in this door's row shape at all (`journey_trip_contexts.arrival_anchor_json`
 *  holds it, and no door on this tree orders by it), and nothing stored here means "已存" -- the row
 *  has `created_at` and `updated_at`, and calling either of them "saved order" would be a second name
 *  for a fact that already has one.  Relabelling `start_at` (the departure) as an arrival is exactly
 *  the "假装按时间排" this order forbids, so the tier is absent and the screen says so. */
export const JOURNEY_ORDERS_NOT_ANSWERED: readonly {
  nameKey: AccessCopyKey;
  becauseKey: AccessCopyKey;
}[] = [
  { nameKey: "access.noTierArrival", becauseKey: "access.noTierArrivalBecause" },
  { nameKey: "access.noTierSaved", becauseKey: "access.noTierSavedBecause" },
];

/** The order this screen asks for first. */
export const DEFAULT_JOURNEY_ORDER: JourneyOrderChoice = SORT_RECENT_UPDATE;

/** The word for an order this screen offers, or `null` for one it has no sentence for. `null` is the
 *  honest answer and the panel says so rather than quoting the server's token as if it were a label. */
export function sortLabelKey(choice: JourneyOrderChoice): AccessCopyKey | null {
  const entry = JOURNEY_SORT_LADDER.find((row) => row.choice === choice);
  return entry ? entry.labelKey : null;
}

/** Is this value one of the orders the screen knows how to ask for? `null` is: it is 创建序. */
export function isJourneyOrderChoice(value: unknown): value is JourneyOrderChoice {
  return value === null || JOURNEY_SORT_LADDER.some((row) => row.choice === value);
}

