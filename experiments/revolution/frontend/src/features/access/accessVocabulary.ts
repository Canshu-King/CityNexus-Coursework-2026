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

export const ROLE_OWNER = "OWNER" as const;
export const ROLE_ADMIN = "ADMIN" as const;
export const ROLE_COLLABORATOR = "COLLABORATOR" as const;

/** The ladder, in the order a traveller reads it (widest first). Each entry carries the key of the
 *  one sentence that says what the role can do -- the shape's "角色档位到一句作用域". */
export const ROLE_LADDER: readonly {
  role: typeof ROLE_OWNER | typeof ROLE_ADMIN | typeof ROLE_COLLABORATOR;
  labelKey: AccessCopyKey;
  scopeKey: AccessCopyKey;
}[] = [
  { role: ROLE_OWNER, labelKey: "access.roleOwner", scopeKey: "access.roleOwnerScope" },
  { role: ROLE_ADMIN, labelKey: "access.roleAdmin", scopeKey: "access.roleAdminScope" },
  {
    role: ROLE_COLLABORATOR,
    labelKey: "access.roleCollaborator",
    scopeKey: "access.roleCollaboratorScope",
  },
];

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
