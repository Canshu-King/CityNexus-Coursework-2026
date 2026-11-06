# Solver Runtime Gap Analysis - Based on Weather Hardening Checkpoint

## Existing code that must be reused

### `app/planning/ortools_adapter.py`
`ORToolsVariantSelector` proves the optional dependency boundary but only chooses one already-generated `PlannerVariant`. It does not schedule Journey Activities.

### `app/planning/replan_candidate_problem.py`
`generate_ortools_candidates()` currently delegates to deterministic filtering/ranking. No CP-SAT problem is built and the function is not wired into `generate_replan()`.

### `app/main.py::generate_replan()`
This is the current real runtime candidate pipeline. Important reusable facts already exist before candidate materialization:

- active Journey / active base PlanVersion;
- trigger Event and effective event time;
- execution partition;
- active C4 semantic/time anchors;
- deterministic `replacement_choices`;
- current execution location;
- next future C4 anchor;
- all required mobility OD pairs;
- one operation-scoped frozen `MobilitySnapshot`;
- remaining Journey budget;
- current authoritative feasibility gate;
- ReplanDiff, feasibility envelope and verified scoring;
- proposal-persisted mobility evidence;
- accept-time revalidation.

This means the correct move is **not** a new planning subsystem. Insert OR-Tools between frozen fact preparation and existing `specs`/PlanVersion materialization.

## Minimal integration seam

Current shape:

```text
replacement_choices
+ effective_event_time
+ future_anchor
+ frozen mobility
-> heuristic specs list
-> materialize PlanVersion
-> validate_plan_feasibility
-> score/diff/proposal
```

Target V1 shape:

```text
same existing inputs
-> build ReplanSolverProblem
-> OR-Tools generator (feature/availability guarded)
-> translate SolverCandidate -> existing materialization spec
-> materialize PlanVersion
-> SAME validate_plan_feasibility
-> SAME scoring/diff/proposal
```

Fallback:

```text
OR-Tools disabled / missing / controlled failure / UNKNOWN / no post-gate candidate
-> current heuristic specs path
```

## Current code details that make this easy

`generate_replan()` already captures the OD pairs needed for every replacement candidate:

- current/previous -> replacement;
- replacement -> next Activity;
- replacement -> future C4 anchor;
- current -> future C4 anchor.

Do not capture a second mobility snapshot for OR-Tools.

## Missing runtime capabilities

1. pure immutable solver problem type;
2. real CP-SAT option presence and schedule variables;
3. explicit feature/availability boundary;
4. stable problem fingerprint/provenance;
5. Top-K distinct solver assignments;
6. controlled timeout/UNKNOWN behavior;
7. translation from solver candidate schedule to PlanVersion rows;
8. generator provenance in proposal evidence;
9. optional dependency declaration;
10. dedicated ORT adversarial tests.

## Known design debt not to mix into this mutation batch

- WEATHER currently becomes a conservative global `PLACE_UNAVAILABLE` constraint after attribution. Time-bounded weather rescheduling is valuable but requires the deterministic gate to understand temporal hazard intervals first. Keep current semantics in Solver V1.
- stale PRESENTED proposals whose weather Event later resolves are acceptance-safe but remain visible until explicit lifecycle cleanup. Do not rewrite proposal lifecycle while integrating CP-SAT.
- do not add scheduler/background workers, microservices, Kafka, Postgres-only persistence, or a second planning language/runtime.


