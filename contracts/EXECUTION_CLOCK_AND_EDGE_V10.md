# Execution Clock + Edge Truth V10

NOW, NEXT, leave-by, C4, offline, push, and PIC use one runtime `ExecutionClock` truth. It resolves Journey timezone, server time, latest foreground location timestamp, actual Activity transitions, planned times, and provider freshness. The clock is freezeable for deterministic tests.

Persisted transitions are `NOT_STARTED`, `EN_ROUTE`, `ARRIVED`, `IN_PROGRESS`, `COMPLETED`, and `SKIPPED` when timestamps are available.

- C4 countdown is scheduled C4 time minus `execution_clock.now`, never planned current Activity start.
- Edge risk and Autopilot state derive from Journey/Guardian/enrollment truth.
- Offline cache covers the next 24 hours from actual now and does not present stale planned Activities as future items unless execution state requires it.

