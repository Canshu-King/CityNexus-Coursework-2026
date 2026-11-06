# Solver Runtime Architecture Contract

## First increment: replacement-window CP-SAT

Do not begin with a general TSP or full-day route optimizer. The safest first real solver target is the disrupted mutable slot between the current execution position and the next protected C4 anchor.

```text
Current execution position
+ effective disruption time
+ deterministic replacement options
+ opening windows
+ remaining budget
+ ONE frozen MobilitySnapshot
+ next durable C4 anchor
+ safety buffer
        |
        v
ReplanSolverProblem (pure immutable values)
        |
        +--> OR-Tools CP-SAT candidate generator
        |       - option presence
        |       - start/end schedule
        |       - opening window
        |       - budget
        |       - frozen inbound/outbound travel
        |       - arrival before next C4 + safety
        |       - deterministic objective
        |
        +--> existing deterministic fallback if solver unavailable/fails
                |
                v
SolverCandidate(s)
        |
        v
materialize new PlanVersion
        |
        v
existing CityNexus authoritative feasibility
        |
        v
verified scoring + ReplanDiff
        |
        v
PRESENTED proposal
```

Activities outside the disrupted slot are copied unchanged in V1. Completed prefix remains immutable. C4 stays fixed by durable semantic/time anchor and is checked again by the existing gate.

## Why this is already a real solver

CP-SAT is choosing among alternatives **and** solving a constrained start time using opening windows, budget, frozen travel, Journey horizon, and protected-anchor arrival. It is not merely selecting a pre-scored card.

The model can later expand to optional insertion, multiple mutable activities, ordering and multi-anchor scheduling after this boundary is proven.

## Pure input rule

Solver input may contain only frozen primitive/value data. Do not pass:

- SQLAlchemy model/session;
- FastAPI Request/Depends;
- HTTP client/provider;
- live Valhalla/Open-Meteo adapter;
- mutable JourneyTwin projection.

## Determinism

Use stable sorting, `num_search_workers = 1`, fixed `random_seed`, bounded solve time and stable post-gate ranking. If time budget returns UNKNOWN, do not materialize a half-trusted assignment.

## Top-K

V1 should return distinct option assignments, not ten start-minute variants for the same place. After each accepted solver assignment, exclude that option/drop assignment and solve again until K, INFEASIBLE, UNKNOWN, or exhaustion.

## Weather note

Current WEATHER Replan conservatively translates a weather-hit place to a global `PLACE_UNAVAILABLE` disruption constraint. Keep that behavior for Solver V1 so the authoritative gate and solver stay aligned. A later Solver V2 may add a deterministic time-bounded weather constraint so the same outdoor Place can be moved outside the hazardous forecast interval. Do **not** let OR-Tools alone invent that semantic before the gate understands it too.


