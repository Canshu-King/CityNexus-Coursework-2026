/**
 * One shared reading of how much of a plan's budget is actually evidenced.
 *
 * "At least one priced stop" is not a budget verdict.  One known price beside
 * thirteen unknown ones does not verify a total, and a stop with no provider
 * price is not a free stop.
 *
 *   NONE    — no reliable price evidence at all; the total is unknown.
 *   PARTIAL — some prices are known; the total is still unknown, so no
 *             "within / near / over budget" verdict may be shown.
 *   FULL    — every budget-relevant component carries reliable evidence; only
 *             here may a budget verdict be shown.
 *
 * A zero amount can be *known free*, which is real evidence — it must not fall
 * back to "unknown" merely because the number is zero.
 *
 * Contract: docs/BUDGET_EVIDENCE_CONTRACT.md
 */

export type BudgetEvidenceState = "NONE" | "PARTIAL" | "FULL";

export type CostEvidenceLike = {
  cost_status?: string | null;
  estimated_cost?: { amount_minor?: number | null } | null;
};

/** Statuses that count as reliable price evidence, including known-free. */
const KNOWN_STATUSES = new Set(["KNOWN", "KNOWN_FREE", "KNOWN_PAID"]);

/**
 * Whether an item is *verified* evidence — an explicit KNOWN status.
 *
 * A bare positive amount with no status is displayable but not proof (it may be
 * an unverified legacy figure), so it must not by itself license a FULL trip
 * verdict (V2-T4 / B4 regression 4).
 */
export function isVerifiedCostEvidence(item: CostEvidenceLike): boolean {
  const status = String(item.cost_status ?? "").toUpperCase();
  return KNOWN_STATUSES.has(status);
}

/**
 * Whether one activity carries reliable price evidence.
 *
 * An explicit status wins.  `UNKNOWN` is never evidence.  Without a status we
 * fall back to the amount, and only a *positive* amount counts: an absent or
 * zero amount with no status is not proof of anything.
 */
export function hasReliableCostEvidence(item: CostEvidenceLike): boolean {
  const status = String(item.cost_status ?? "").toUpperCase();
  if (KNOWN_STATUSES.has(status)) return true;
  if (status === "UNKNOWN") return false;
  return (item.estimated_cost?.amount_minor ?? 0) > 0;
}

export type BudgetEvidenceCoverage = {
  state: BudgetEvidenceState;
  knownCount: number;
  /** Items carrying an explicit KNOWN status; gates FULL independently of known. */
  verifiedCount: number;
  unknownCount: number;
  /** Components that are not verified (unknown OR displayable-but-unverified). */
  unverifiedCount: number;
  totalCount: number;
  /** Sum of the known amounts, in minor units. */
  knownMinor: number;
};

/** Classify a plan's cost evidence into NONE / PARTIAL / FULL. */
export function budgetEvidenceCoverage(
  activities: readonly CostEvidenceLike[],
): BudgetEvidenceCoverage {
  const totalCount = activities.length;
  let knownCount = 0;
  let verifiedCount = 0;
  let knownMinor = 0;
  for (const item of activities) {
    if (hasReliableCostEvidence(item)) {
      knownCount += 1;
      knownMinor += item.estimated_cost?.amount_minor ?? 0;
    }
    if (isVerifiedCostEvidence(item)) verifiedCount += 1;
  }
  const unknownCount = totalCount - knownCount;
  const unverifiedCount = totalCount - verifiedCount;
  let state: BudgetEvidenceState = "NONE";
  if (totalCount > 0 && knownCount > 0) {
    // FULL only when every component is displayable-known AND carries an
    // explicit verified status — a statusless positive amount keeps it PARTIAL.
    state = knownCount === totalCount && verifiedCount === totalCount ? "FULL" : "PARTIAL";
  }
  return {
    state,
    knownCount,
    verifiedCount,
    unknownCount,
    unverifiedCount,
    totalCount,
    knownMinor,
  };
}

/** Whether a final "within / near / over budget" verdict may be shown. */
export function budgetVerdictAllowed(coverage: BudgetEvidenceCoverage): boolean {
  return coverage.state === "FULL";
}

/**
 * A budget-relevant component group feeding the whole-trip aggregation.
 *
 * Activities alone are NOT the trip budget: a stay/hotel and confirmed
 * bookings/transport are also cost-bearing.  Each source is labelled so the UI
 * can be honest about what a verdict covers (V2-T4 / B4).
 */
export type BudgetComponentSource = {
  label: string;
  items: readonly CostEvidenceLike[];
};

/**
 * Aggregate every budget-relevant component into one whole-trip coverage.
 *
 * FULL — and therefore a "trip budget verified" verdict — is reached only when
 * every component across every source carries reliable evidence.  Fourteen
 * priced activities beside an unknown hotel stays PARTIAL, never FULL: a known
 * activity total is not a known trip budget (B4).
 */
export function tripBudgetEvidence(
  sources: readonly BudgetComponentSource[],
): BudgetEvidenceCoverage {
  const all = sources.flatMap((source) => [...source.items]);
  return budgetEvidenceCoverage(all);
}

/**
 * The wording scope a coverage state licenses.
 *
 * Only the whole-trip aggregation may be described as a *trip budget*.  A
 * coverage computed from activities alone must be described as *activity costs*,
 * so an activity-only FULL never masquerades as "the whole trip budget is
 * verified" (B4 — humans invented qualifiers for exactly this moment).
 */
export function budgetClaimScope(coverage: BudgetEvidenceCoverage, wholeTrip: boolean) {
  if (!wholeTrip) return "ACTIVITY_COSTS" as const;
  return coverage.state === "FULL" ? ("TRIP_BUDGET" as const) : ("ACTIVITY_COSTS" as const);
}
