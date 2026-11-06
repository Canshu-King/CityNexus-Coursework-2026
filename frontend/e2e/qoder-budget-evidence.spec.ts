/**
 * T4 — budget evidence coverage (docs/BUDGET_EVIDENCE_CONTRACT.md; V2-T4 / B4).
 *
 * LAYER: unit.  This drives the pure functions through `page.evaluate(import
 * .../budgetEvidence.ts)` — real logic, but NOT a rendered component test.  Per
 * docs/TEST_AND_EVIDENCE_GATE.md it is labelled unit, not E2E.  Rendered Today/
 * Live and rendered budget-screen integration is tracked as a remaining evidence
 * gap in the handoff (no jsdom/RTL harness exists in this project).
 *
 * "At least one priced stop" is not a budget verdict.  One known price beside
 * thirteen unknown ones does not verify a total, a zero amount can be *known
 * free* (real evidence), and — B4 — fourteen priced activities beside an unknown
 * hotel is NOT a verified trip budget.
 */

import { expect, test } from "@playwright/test";

test.describe("T4 budget evidence coverage (unit)", () => {
  test("NONE / PARTIAL / FULL are distinct, and only FULL allows a verdict", async ({ page }) => {
    await page.goto("/");
    const result = await page.evaluate(async () => {
      const mod = await import("/src/lib/budgetEvidence.ts");
      const unknown = { cost_status: "UNKNOWN", estimated_cost: { amount_minor: 0 } };
      const paid = (amount) => ({
        cost_status: "KNOWN_PAID",
        estimated_cost: { amount_minor: amount },
      });
      const free = { cost_status: "KNOWN_FREE", estimated_cost: { amount_minor: 0 } };
      const unknownList = (n) => Array.from({ length: n }, () => unknown);

      const none = mod.budgetEvidenceCoverage(unknownList(14));
      const oneOf14 = mod.budgetEvidenceCoverage([paid(3500), ...unknownList(13)]);
      const thirteenOf14 = mod.budgetEvidenceCoverage([...Array.from({ length: 13 }, () => paid(1000)), unknown]);
      const allKnown = mod.budgetEvidenceCoverage([paid(1000), free, paid(2000)]);
      const empty = mod.budgetEvidenceCoverage([]);

      return {
        noneState: none.state,
        noneVerdict: mod.budgetVerdictAllowed(none),
        oneOf14State: oneOf14.state,
        oneOf14Verdict: mod.budgetVerdictAllowed(oneOf14),
        oneOf14Unknown: oneOf14.unknownCount,
        thirteenState: thirteenOf14.state,
        thirteenVerdict: mod.budgetVerdictAllowed(thirteenOf14),
        allKnownState: allKnown.state,
        allKnownVerdict: mod.budgetVerdictAllowed(allKnown),
        knownFreeCounts: mod.hasReliableCostEvidence(free),
        zeroWithNoStatus: mod.hasReliableCostEvidence({ estimated_cost: { amount_minor: 0 } }),
        emptyState: empty.state,
      };
    });

    expect(result.noneState).toBe("NONE");
    expect(result.noneVerdict).toBe(false);
    expect(result.oneOf14State).toBe("PARTIAL");
    expect(result.oneOf14Verdict).toBe(false);
    expect(result.oneOf14Unknown).toBe(13);
    expect(result.thirteenState).toBe("PARTIAL");
    expect(result.thirteenVerdict).toBe(false);
    expect(result.allKnownState).toBe("FULL");
    expect(result.allKnownVerdict).toBe(true);
    expect(result.knownFreeCounts).toBe(true);
    expect(result.zeroWithNoStatus).toBe(false);
    expect(result.emptyState).toBe("NONE");
  });

  test("B4: all activities priced but an unknown hotel/booking stays PARTIAL (no trip verdict)", async ({
    page,
  }) => {
    await page.goto("/");
    const result = await page.evaluate(async () => {
      const mod = await import("/src/lib/budgetEvidence.ts");
      const paid = (amount) => ({ cost_status: "KNOWN_PAID", estimated_cost: { amount_minor: amount } });
      const unknown = { cost_status: "UNKNOWN", estimated_cost: { amount_minor: 0 } };

      // 14 activities, all priced -> activity coverage is FULL...
      const activitiesOnly = mod.budgetEvidenceCoverage(
        Array.from({ length: 14 }, () => paid(1000)),
      );
      // ...but the whole trip adds an unknown hotel and an unknown transport
      // booking, so the trip budget must NOT be FULL.
      const trip = mod.tripBudgetEvidence([
        { label: "activities", items: Array.from({ length: 14 }, () => paid(1000)) },
        { label: "stay", items: [unknown] },
        { label: "transport", items: [unknown] },
      ]);
      // And once the hotel + transport are known, the trip can be FULL.
      const tripComplete = mod.tripBudgetEvidence([
        { label: "activities", items: [paid(1000)] },
        { label: "stay", items: [paid(40000)] },
        { label: "transport", items: [{ cost_status: "KNOWN_FREE", estimated_cost: { amount_minor: 0 } }] },
      ]);
      // A positive amount with NO status is displayable but not *verified*, so
      // an all-statusless plan must not reach FULL (B4 regression 4).
      const statuslessOnly = mod.budgetEvidenceCoverage([
        { estimated_cost: { amount_minor: 5000 } },
        { estimated_cost: { amount_minor: 8000 } },
      ]);

      return {
        activitiesOnlyState: activitiesOnly.state,
        tripState: trip.state,
        tripVerdict: mod.budgetVerdictAllowed(trip),
        tripUnknown: trip.unknownCount,
        tripCompleteState: tripComplete.state,
        tripCompleteVerdict: mod.budgetVerdictAllowed(tripComplete),
        statuslessOnlyState: statuslessOnly.state,
        statuslessOnlyVerdict: mod.budgetVerdictAllowed(statuslessOnly),
        // B4 wording scope: an activity-only FULL must not be called trip budget.
        activityScopeClaim: mod.budgetClaimScope(activitiesOnly, false),
        tripScopeClaimWhenPartial: mod.budgetClaimScope(trip, true),
        tripScopeClaimWhenFull: mod.budgetClaimScope(tripComplete, true),
      };
    });

    // Activity coverage is FULL on its own...
    expect(result.activitiesOnlyState).toBe("FULL");
    // ...but the whole-trip aggregation is PARTIAL while hotel + transport unknown.
    expect(result.tripState).toBe("PARTIAL");
    expect(result.tripUnknown).toBe(2);
    expect(result.tripVerdict).toBe(false);
    // Only after stay + transport are known does the trip reach FULL.
    expect(result.tripCompleteState).toBe("FULL");
    expect(result.tripCompleteVerdict).toBe(true);
    // An all-statusless (no explicit KNOWN status) plan is displayable but not
    // verified -> PARTIAL, and never a trip-budget verdict (B4 regression 4).
    expect(result.statuslessOnlyState).toBe("PARTIAL");
    expect(result.statuslessOnlyVerdict).toBe(false);
    // Wording scope guards: activity-only is never a trip-budget claim; a
    // partial trip is never a trip-budget claim; only a full trip may say so.
    expect(result.activityScopeClaim).toBe("ACTIVITY_COSTS");
    expect(result.tripScopeClaimWhenPartial).toBe("ACTIVITY_COSTS");
    expect(result.tripScopeClaimWhenFull).toBe("TRIP_BUDGET");
  });
});
