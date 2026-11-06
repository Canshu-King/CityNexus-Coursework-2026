/* ORD-0439: rank the plans a traveller can choose from, using only fields this product computes.

   Three rules, because a comparison screen lies most easily in exactly three places:
    1. a value is either read from the payload or reported as absent -- never defaulted to 0, which is
       how "price unknown" silently becomes "cheapest";
    2. a dimension only breaks a tie when it actually differs across the compared plans. If every plan
       scores the same, or none carries the field, the dimension is still shown and the screen says it
       could not separate them, instead of inventing an order;
    3. evidence quality is reported as our own named state. A `SYNTHETIC_DEFAULT` leg is counted as NOT
       hard-eligible and never as a provider result -- the line ORD-0369 had to draw on the replan
       screen is the same line here.
*/

export const HARD_FEASIBLE_QUALITIES = ["EXPLICIT_FROZEN_FIXTURE", "LIVE_PROVIDER_CAPTURE"] as const;

export type RankDim = "cost" | "walk" | "protected" | "evidence";

/** The declared priority. Reordering this array reorders the screen, and the screen says which
 *  dimensions it actually used, so a silent priority change is not possible. */
export const RANK_DIMS: RankDim[] = ["cost", "walk", "evidence", "protected"];

export type AbsentReason = "priceUnknown" | "walkNotMeasured" | "protectedNotMarked" | "noFacts";

export type Cell =
  | { kind: "value"; number: number; partial?: boolean }
  | { kind: "absent"; reason: AbsentReason };

export type Evidence = { hard: number; total: number; worstQuality: string | null };

export type RankedPlan = {
  id: string;
  versionNo: number;
  cells: Record<RankDim, Cell>;
  evidence: Evidence;
};

type Fact = { walking_min?: number | null; fact_quality?: string | null };
type Activity = { protected?: boolean | null };

export type ComparablePlan = {
  id: string;
  version_no: number;
  total_cost?: { amount_minor?: number | null; currency?: string } | null;
  mobility_evidence?: { facts?: Fact[] } | null;
  activities?: Activity[] | null;
};

function factsOf(plan: ComparablePlan): Fact[] {
  return plan.mobility_evidence?.facts ?? [];
}

function costCell(plan: ComparablePlan): Cell {
  const minor = plan.total_cost?.amount_minor;
  return typeof minor === "number" ? { kind: "value", number: minor } : { kind: "absent", reason: "priceUnknown" };
}

function walkCell(plan: ComparablePlan): Cell {
  const facts = factsOf(plan);
  if (facts.length === 0) return { kind: "absent", reason: "noFacts" };
  const measured = facts.filter((f) => typeof f.walking_min === "number");
  if (measured.length === 0) return { kind: "absent", reason: "walkNotMeasured" };
  const sum = measured.reduce((acc, f) => acc + (f.walking_min as number), 0);
  // A sum over some of the legs is not the trip's walking time, so the cell carries that difference.
  return { kind: "value", number: sum, partial: measured.length < facts.length };
}

function protectedCell(plan: ComparablePlan): Cell {
  const acts = plan.activities ?? [];
  if (acts.length === 0) return { kind: "absent", reason: "noFacts" };
  const anyMarked = acts.some((a) => typeof a.protected === "boolean");
  if (!anyMarked) return { kind: "absent", reason: "protectedNotMarked" };
  return { kind: "value", number: acts.filter((a) => a.protected === true).length };
}

export function evidenceOf(plan: ComparablePlan): Evidence {
  const facts = factsOf(plan);
  const hard = facts.filter((f) => (HARD_FEASIBLE_QUALITIES as readonly string[]).includes(String(f.fact_quality))).length;
  const worst = facts.map((f) => f.fact_quality).find((q) => q && !(HARD_FEASIBLE_QUALITIES as readonly string[]).includes(q)) ?? null;
  return { hard, total: facts.length, worstQuality: worst };
}

export function rankPlans(plans: ComparablePlan[]): {
  rows: RankedPlan[];
  comparable: Record<RankDim, boolean>;
  usedDims: RankDim[];
} {
  const rows: RankedPlan[] = plans.map((plan) => {
    const evidence = evidenceOf(plan);
    return {
      id: plan.id,
      versionNo: plan.version_no,
      evidence,
      cells: {
        cost: costCell(plan),
        walk: walkCell(plan),
        protected: protectedCell(plan),
        evidence: evidence.total === 0
          ? { kind: "absent", reason: "noFacts" }
          : { kind: "value", number: evidence.hard },
      },
    };
  });

  const differs = (dim: RankDim) => {
    const seen = new Set<number>();
    for (const row of rows) {
      const cell = row.cells[dim];
      if (cell.kind === "value") seen.add(cell.number);
    }
    return seen.size > 1;
  };

  const comparable: Record<RankDim, boolean> = {
    cost: differs("cost"),
    walk: differs("walk"),
    evidence: differs("evidence"),
    protected: differs("protected"),
  };
  const usedDims = RANK_DIMS.filter((dim) => comparable[dim]);

  rows.sort((a, b) => {
    for (const dim of usedDims) {
      const x = a.cells[dim];
      const y = b.cells[dim];
      if (x.kind !== "value" || y.kind !== "value" || x.number === y.number) continue;
      // More hard evidence is better; everything else is a cost, so lower is better.
      return dim === "evidence" ? y.number - x.number : x.number - y.number;
    }
    return a.versionNo - b.versionNo;
  });

  return { rows, comparable, usedDims };
}
