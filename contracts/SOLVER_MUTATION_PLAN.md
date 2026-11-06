# Solver Runtime V1 Mutation Plan

Use this order to keep the mutation batch reviewable and to avoid turning `generate_replan()` into a second planner universe.

## S0 - Baseline and dependency smoke

- record `git status --short` / `git diff --stat`;
- run the current Weather/Provider/Golden focused gates before mutation;
- install the optional solver extra in the local environment only;
- run the reference/optional-boundary tests and confirm the real OR-Tools tests execute rather than skip.

No runtime code changes yet.

## S1 - Pure solver contract

Add/adapt:

```text
app/planning/solver_contract.py
app/planning/reference_replan_solver.py
app/planning/ortools_replan_solver.py
```

Prove:

- JSON-serializable immutable input;
- no ORM/session/http/provider imports;
- missing mobility pairs fail closed;
- real OR-Tools results agree with the small reference oracle;
- repeated equal-input solve is stable.

## S2 - Runtime policy / feature guard

Add a very small orchestration layer, for example:

```text
app/planning/solver_runtime.py
```

Responsibilities only:

- read normalized server-owned solver mode;
- call OR-Tools adapter when enabled/available;
- classify terminal status;
- return candidates + generator metadata;
- controlled fallback decision.

It must not:

- query DB;
- call providers;
- materialize PlanVersion;
- evaluate authoritative feasibility.

## S3 - Build problem from already available `generate_replan()` facts

After MobilitySnapshot capture, build the pure problem only for V1-supported circumstances:

```text
trigger in {CLOSURE, WEATHER}
affected mutable Activity exists
replacement_choices exist
current execution location exists
future durable C4 anchor exists
```

Unsupported circumstances remain on the existing deterministic heuristic path.

Do not delete F0/F1/F2/F3.

## S4 - Translate solver output into the existing materialization loop

Extend the internal candidate spec with explicit schedule/provenance fields rather than forking the persistence code.

Recommended semantics:

```text
family = F4_ORTOOLS_WINDOW_REPLACEMENT
explicit_candidate_start_at = solver-assigned start
solver_provenance = {...}
```

For drop, reuse current F1 semantics only when current policy permits.

Do not persist speculative candidates before their normal materialization turn. Respect `candidate_count`.

## S5 - Authority and cleanup adversarial tests

Before calling the integration finished, prove:

- malicious solver output still fails CityNexus gate;
- rejected candidates leave no orphan rows;
- completed prefix and C4 cannot be mutated;
- accept-time revalidation is untouched;
- same MobilitySnapshot id is visible in solver provenance and proposal mobility evidence;
- solver status UNKNOWN/MODEL_INVALID cannot create a proposal;
- OR-Tools missing/disabled returns to the old deterministic path.

## S6 - Regression and review package

Run ORT-01..20 plus inherited gates, full pytest, compileall, Ruff, OpenAPI and any touched frontend E2E. Create a clean fresh-extractable checkpoint and stop.


