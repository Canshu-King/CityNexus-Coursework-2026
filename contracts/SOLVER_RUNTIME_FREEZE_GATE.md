# OR-Tools Runtime External Freeze Gate

Solver Runtime may be frozen only when all checks below pass in the real Windows repository and a fresh-extracted review checkpoint.

## ORT gates

- ORT-01 completed/terminal execution prefix is byte-for-semantic-byte unchanged after candidate materialization.
- ORT-02 every active C4 stable semantic key and scheduled start remains present; solver cannot drop/move it.
- ORT-03 solver consumes only the supplied frozen MobilitySnapshot; no provider/network call occurs inside solver or authoritative gate.
- ORT-04 an intentionally malicious solver assignment is rejected by CityNexus authoritative feasibility and never becomes PRESENTED.
- ORT-05 same frozen problem + fixed seed/workers yields reproducible assignments and stable post-gate ordering.
- ORT-06 opening hours are hard constraints in the solver and rechecked by CityNexus gate.
- ORT-07 if replacement cannot fit, optional drop may be emitted only when policy allows; C4 can never be the drop target.
- ORT-08 budget is hard in solver and rechecked by the gate.
- ORT-09 missing OR-Tools dependency or controlled solver failure retains the existing deterministic heuristic replan path.
- ORT-10 completely offline Closure Golden Scenario continues to work.
- ORT-11 proposal evidence exposes generator provenance (`ORTOOLS_CP_SAT` or fallback) while `feasibility_authority` remains `CITYNEXUS_DETERMINISTIC_GATE`.
- ORT-12 same frozen input produces stable candidate ordering after gate/scoring.
- ORT-13 Top-K solver assignments are unique by stable assignment fingerprint.
- ORT-14 UNKNOWN/timeout/MODEL_INVALID cannot yield an unverified proposal.
- ORT-15 solver problem is JSON-serializable pure data; no ORM/session/HTTP/provider objects.
- ORT-16 accept-time deterministic feasibility revalidation remains unchanged and uses proposal-persisted frozen mobility evidence.
- ORT-17 `candidate_count` is respected without generating hidden extra persisted PlanVersions.
- ORT-18 failed/rejected solver candidates leave no orphan CANDIDATE PlanVersion/Activity rows after transaction completion.
- ORT-19 solver provenance includes a stable problem fingerprint and frozen mobility snapshot id for audit.
- ORT-20 feature disabled -> output behavior remains deterministic and equivalent to current heuristic path for the Golden Scenario.

## Prior gates that must not regress

- WRH-01..06
- WRT-01..12
- Provider PRT-01..08
- Provider hardening PRH-01..05
- backend-connected Golden Scenario
- full backend pytest clean exit
- compileall
- Ruff check + format check
- OpenAPI parity
- frontend build / Playwright if frontend or API contract touched

## Hard stop

When ORT gates and inherited regressions are green, package a Solver Runtime checkpoint and stop for external review before LiteLLM/DeepSeek or MapLibre work.


