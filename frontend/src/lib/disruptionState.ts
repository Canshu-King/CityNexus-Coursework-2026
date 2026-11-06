/**
 * One shared reading of a disruption's lifecycle state.
 *
 * The product rule is `ACKNOWLEDGED != RESOLVED`: acknowledging a disruption
 * means the traveller has seen it, not that a recovery was chosen.  Only
 * RESOLVED, IGNORED, or an accepted replan explicitly linked to the event
 * closes it.
 *
 * Both Today (V8Experience) and Live (LiveRecovery) read this module so the
 * two surfaces cannot disagree about the same event — the earlier split let
 * Today keep saying "1 项变化" while Live said there was nothing to decide.
 *
 * Contract: docs/ACK_RESOLUTION_CONTRACT.md
 */

export type DisruptionLike = {
  id: string;
  status?: string | null;
  resolved_at?: string | null;
};

/** Statuses that genuinely close a disruption. */
const RESOLVING_STATUSES = new Set(["RESOLVED", "IGNORED"]);

/** Normalise a raw status; an absent status means the event was detected. */
export function disruptionStatus(item: DisruptionLike): string {
  return String(item.status ?? "DETECTED").toUpperCase();
}

/**
 * Whether the disruption still needs a decision from the traveller.
 *
 * `acceptedTriggerIds` are the trigger events of ACCEPTED proposals: accepting
 * a replan resolves its own trigger even when the stored Event status is still
 * ACKNOWLEDGED.
 */
export function isDisruptionOpen(
  item: DisruptionLike,
  acceptedTriggerIds: ReadonlySet<string> = new Set(),
): boolean {
  if (RESOLVING_STATUSES.has(disruptionStatus(item))) return false;
  if (item.resolved_at) return false;
  if (acceptedTriggerIds.has(String(item.id))) return false;
  return true;
}

/**
 * Whether the traveller has seen the disruption but not yet resolved it.
 *
 * Such an event may reduce notification noise, but it must never be presented
 * as handled.
 */
export function isDisruptionAcknowledgedOnly(
  item: DisruptionLike,
  acceptedTriggerIds: ReadonlySet<string> = new Set(),
): boolean {
  return disruptionStatus(item) === "ACKNOWLEDGED" && isDisruptionOpen(item, acceptedTriggerIds);
}
