# ORT-01..20 Concrete Test Matrix

Use these as real tests in the repository. Adapt helper names to the implementation, but do not weaken the assertions.

| ID | Setup | Action | Required assertion |
|---|---|---|---|
| ORT-01 | Golden Journey advanced to disrupted Tech Art; first 3 activities terminal | generate solver replan | every terminal semantic key/start/end/status is unchanged in every materialized candidate |
| ORT-02 | paid River Cruise C4 at 15:30 | generate top-K | every candidate contains River Cruise at exactly 15:30; no solver assignment can omit it |
| ORT-03 | inject a frozen MobilitySnapshot plus provider object that raises on any network call | solve + gate | zero provider calls; all travel minutes come from snapshot facts |
| ORT-04 | monkeypatch generator to return impossible overlap / missing C4 candidate | generate replan | gate rejects it; no PRESENTED proposal for that candidate |
| ORT-05 | same problem twice, workers=1, fixed seed | solve twice | assignment fingerprints/order identical |
| ORT-06 | candidate opening window starts after event time | solve | selected start is inside opening window or candidate absent |
| ORT-07 | all replacements infeasible but drop allowed | solve | drop may appear; C4 never dropped; if drop disallowed -> no solver candidate |
| ORT-08 | replacement cost exceeds remaining budget | solve | expensive option absent; gate independently reports/rejects budget violation if maliciously materialized |
| ORT-09 | simulate ImportError / ORToolsUnavailable | runtime generate | current deterministic heuristic replan still returns a valid proposal |
| ORT-10 | no live internet/providers/AI | backend Golden | Closure recovery still completes end-to-end |
| ORT-11 | OR-Tools enabled | inspect proposal diff/evidence | generator.kind=ORTOOLS_CP_SAT and feasibility_authority=CITYNEXUS_DETERMINISTIC_GATE |
| ORT-12 | same frozen input | generate twice from isolated DB reset | stable post-gate candidate ordering by semantic assignment, not UUID |
| ORT-13 | request K=3 with >=3 feasible choices/drop | solve | fingerprints unique; no same option repeated with cosmetic start-minute variants |
| ORT-14 | force UNKNOWN/timeout/MODEL_INVALID | runtime generate | no solver assignment promoted; controlled fallback or controlled no-solver result |
| ORT-15 | build problem | `json.dumps(problem.to_payload())` | serializes; payload contains no ORM/session/http/provider objects |
| ORT-16 | generate valid proposal; mutate authoritative plan/constraints before accept | accept | existing accept-time gate rejects stale/infeasible proposal |
| ORT-17 | candidate_count=1 | generate | only one solver-generated candidate PlanVersion persists; no hidden extras |
| ORT-18 | include malicious/infeasible solver candidates | finish request | rejected temporary PlanVersions/Activities are deleted/rolled back; no orphan CANDIDATE rows |
| ORT-19 | inspect evidence | generate | stable problem_fingerprint + exact frozen mobility snapshot id persisted |
| ORT-20 | `CITYNEXUS_SOLVER=deterministic` | run Golden twice | existing deterministic behavior remains available and stable |

## Extra regression probes

- Weather-cleared Event cannot have its old proposal accepted (already protected; keep regression).
- Solver must not turn time-bounded WEATHER semantics into a new rule that the deterministic gate does not understand.
- Solver must not call Open-Meteo, Valhalla, LiteLLM, database, or FastAPI dependencies.


