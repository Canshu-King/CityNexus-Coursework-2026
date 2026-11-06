import { useEffect, useState } from "react";

/**
 * Seconds since `startedAt`, ticking while a request is in flight.
 *
 * ORD-0047 asks long waits to be counted, not soothed.  The clock reads the wall because a
 * `setTimeout` chain drifts exactly when the tab is busy, which is the moment a wait stops
 * being honest.  Returns 0 when nothing started: a component must never show a ticking
 * number for a request it is not waiting on.
 */
export function useElapsedSeconds(startedAt: number | null): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (startedAt === null) {
      setElapsed(0);
      return;
    }
    const tick = () => setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);
  return elapsed;
}
