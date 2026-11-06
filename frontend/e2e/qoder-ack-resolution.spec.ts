/**
 * T3 — acknowledgement vs resolution semantics (docs/ACK_RESOLUTION_CONTRACT.md).
 *
 * `ACKNOWLEDGED != RESOLVED`.  A disruption the traveller has merely seen must
 * stay actionable; only RESOLVED, IGNORED, or an accepted replan linked to that
 * event closes it.  Today and Live must agree on the same event.
 *
 * LAYER: unit.  The shared rule lives in a dependency-free module both surfaces
 * import, so this pins the contract's logic.  A rendered Today/Live integration
 * test (mocking the experience API so the real components paint) is tracked as a
 * remaining evidence gap for v2 in the handoff — it is NOT claimed here.
 */

import { expect, test } from "@playwright/test";

test.describe("T3 acknowledgement vs resolution (unit)", () => {
  test("acknowledged-only stays open; only linked acceptance resolves", async ({ page }) => {
    // Serve the module through the Vite dev server so it is transpiled the
    // same way the application sees it.
    await page.goto("/");
    const rules = await page.evaluate(async () => {
      const mod = await import("/src/lib/disruptionState.ts");
      const event = { id: "e1", status: "ACKNOWLEDGED", resolved_at: null };
      const open = mod.isDisruptionOpen(event, new Set());
      const ackOnly = mod.isDisruptionAcknowledgedOnly(event, new Set());
      return {
        acknowledgedOnlyOpen: open,
        acknowledgedOnlyFlagged: ackOnly,
        // acknowledged-only is by construction a subset of the open set; the old
        // UI branch required open===0 too, which could never co-hold.
        acknowledgedOnlyIsOpenSubset: open && ackOnly,
        linkedAcceptanceResolves: mod.isDisruptionOpen(event, new Set(["e1"])),
        resolvedIsClosed: mod.isDisruptionOpen(
          { id: "e2", status: "RESOLVED", resolved_at: null },
          new Set(),
        ),
        ignoredIsClosed: mod.isDisruptionOpen(
          { id: "e3", status: "IGNORED", resolved_at: null },
          new Set(),
        ),
        detectedIsOpen: mod.isDisruptionOpen(
          { id: "e4", status: "DETECTED", resolved_at: null },
          new Set(),
        ),
        unrelatedAcceptanceDoesNotResolve: mod.isDisruptionOpen(event, new Set(["other"])),
        resolvedAtCloses: mod.isDisruptionOpen(
          { id: "e5", status: "ACKNOWLEDGED", resolved_at: "2026-09-18T00:00:00Z" },
          new Set(),
        ),
      };
    });

    expect(rules.acknowledgedOnlyOpen).toBe(true);
    expect(rules.acknowledgedOnlyFlagged).toBe(true);
    // The V2 fix drops the impossible `openDisruptions.length === 0` guard so the
    // "已知晓，仍待处理" notice is reachable while the event is still open.
    expect(rules.acknowledgedOnlyIsOpenSubset).toBe(true);
    expect(rules.linkedAcceptanceResolves).toBe(false);
    expect(rules.resolvedIsClosed).toBe(false);
    expect(rules.ignoredIsClosed).toBe(false);
    expect(rules.detectedIsOpen).toBe(true);
    expect(rules.unrelatedAcceptanceDoesNotResolve).toBe(true);
    expect(rules.resolvedAtCloses).toBe(false);
  });
});
