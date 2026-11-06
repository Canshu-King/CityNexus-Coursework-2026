# OR-Tools V1 Code Integration Map

Based on the current checkpoint, the lowest-risk insertion point is inside `app/main.py::generate_replan()` **after** the operation-scoped `mobility` snapshot has been captured and **before** PlanVersion candidate materialization.

## 1. Add pure modules

Recommended new files:

```text
app/planning/solver_contract.py
app/planning/reference_replan_solver.py       # semantic oracle / dev fallback reference
app/planning/ortools_replan_solver.py         # optional CP-SAT adapter
app/planning/solver_runtime.py                # feature/availability/fallback orchestration
```

The Assist package contains validated reference versions of the first three. Keep names if useful; semantic merge is allowed.

## 2. Optional dependency

Prefer an optional group, not a mandatory backend dependency:

```toml
[project.optional-dependencies]
solver = ["ortools==9.15.6755"]
```

The default offline demo must still boot and work without this extra.

## 3. Runtime setting

Use one explicit server-side setting, for example:

```text
CITYNEXUS_SOLVER=deterministic   # default
CITYNEXUS_SOLVER=ortools
CITYNEXUS_SOLVER=auto
```

Suggested semantics:

- `deterministic`: never import/use OR-Tools;
- `ortools`: attempt CP-SAT, controlled fallback if unavailable unless a test explicitly requests strict mode;
- `auto`: use OR-Tools when installed, otherwise deterministic.

Do not let a browser choose feasibility authority.

## 4. Build problem from already-frozen facts

For Closure/Weather replacement families when all of these exist:

- `affected` flexible Activity;
- `replacement_choices` non-empty;
- `current_execution` location;
- `future_anchor` C4;
- current operation `mobility` snapshot;

build a pure `ReplanSolverProblem`.

Time conversion must be deterministic integer minutes from `journey.start_at`.

### Budget

Use the same replacement budget already implied by current F3 code:

```text
journey budget
- cost of all base Activities except the affected slot
```

### Opening windows

Read each candidate Place's verified/mock catalog opening facts. Convert to the Journey date/timezone, then to integer minutes from Journey start.

### Mobility

Populate only from `mobility.facts`. Missing pair = option unavailable to solver. Do not call provider or default to zero.

### C4 anchor

Use durable stable semantic key + scheduled time. Solver sees it as a fixed protected anchor. The existing authoritative gate still rechecks it after materialization.

## 5. Translate SolverCandidate into current materialization

Do not duplicate the whole candidate persistence loop.

Recommended extension to the internal spec record:

```text
family
replacement_choice
candidate_delay
candidate_remove
window_evidence
explicit_candidate_start_at
solver_provenance
```

For an OR-Tools selected replacement:

```text
family = F4_ORTOOLS_WINDOW_REPLACEMENT
explicit_candidate_start_at = journey.start_at + candidate.start_min
```

For OR-Tools drop:

```text
family = F1_OPTIONAL_REMOVAL
candidate_remove = True
```

Current F0/F1/F2/F3 materialization remains fallback and regression anchor.

Do not smuggle the solver start through a misleading `window_opens_at` field.

## 6. Proposal provenance

Add under `verified_evidence`:

```json
{
  "generator": {
    "kind": "ORTOOLS_CP_SAT",
    "solver_status": "OPTIMAL|FEASIBLE",
    "problem_fingerprint": "...",
    "mobility_snapshot_id": "...",
    "rank": 0
  },
  "feasibility_authority": "CITYNEXUS_DETERMINISTIC_GATE"
}
```

Fallback should say `DETERMINISTIC_HEURISTIC`, not pretend OR-Tools ran.

## 7. Failure semantics

These must all remain non-fatal to the offline product:

- OR-Tools not installed;
- controlled import failure;
- INFEASIBLE;
- UNKNOWN / timeout;
- zero solver candidates;
- every solver candidate rejected by the CityNexus gate.

In all cases, use the existing heuristic path. Record a non-sensitive fallback reason in generator evidence/audit if useful.

Do not persist unverified solver output as PRESENTED.

## 8. Do not touch acceptance authority

`accept_replan()` already reconstructs proposal-persisted MobilitySnapshot and reruns `validate_plan_feasibility`. Preserve this unchanged.


