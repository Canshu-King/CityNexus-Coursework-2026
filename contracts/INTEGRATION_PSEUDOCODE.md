# Solver Runtime V1 Integration Pseudocode

This pseudocode is based on the reviewed Weather Hardening checkpoint. Adapt names to the live repository; do not copy blindly.

```python
# existing generate_replan(...)
...
mobility_capture = capture_runtime_mobility_snapshot(...)
mobility = mobility_capture.snapshot

solver_specs = []
solver_attempt = None

if solver_mode != "deterministic" and _supports_solver_v1(
    trigger_event=trigger_event,
    affected=affected,
    replacement_choices=replacement_choices,
    current_execution=current_execution,
    future_anchor=future_anchor,
):
    problem = build_replan_solver_problem(
        # values only
        journey=journey,
        affected=affected,
        current_execution=current_execution,
        replacement_choices=replacement_choices,
        future_anchor=future_anchor,
        effective_event_time=effective_event_time,
        mobility=mobility,  # read facts only; do not pass provider client
        source_activities=source,
        safety_buffer_min=15,
    )

    solver_attempt = generate_solver_candidates(
        mode=solver_mode,
        problem=problem,
        candidate_count=candidate_count,
    )

    for candidate in solver_attempt.candidates:
        if candidate.dropped:
            solver_specs.append(
                CandidateSpec(
                    family="F1_OPTIONAL_REMOVAL",
                    replacement_choice=None,
                    candidate_remove=True,
                    explicit_candidate_start_at=None,
                    solver_provenance=solver_attempt.evidence_for(candidate),
                )
            )
        else:
            choice = replacement_by_stable_key[candidate.option_key]
            solver_specs.append(
                CandidateSpec(
                    family="F4_ORTOOLS_WINDOW_REPLACEMENT",
                    replacement_choice=choice,
                    candidate_remove=False,
                    explicit_candidate_start_at=(
                        journey_start + timedelta(minutes=candidate.start_min)
                    ),
                    solver_provenance=solver_attempt.evidence_for(candidate),
                )
            )

# If solver did not run, failed in a controlled way, or yielded zero usable specs,
# use the current deterministic F0/F1/F2/F3 construction unchanged.
specs = solver_specs or build_existing_heuristic_specs(...)

for generation_rank, spec in enumerate(specs[:candidate_count]):
    proposed = PlanVersion(... CANDIDATE ...)
    copied_rows = copy_base_rows(...)

    if spec.replacement_choice:
        # existing replacement operation
        row = replace_affected_row(...)
        if spec.explicit_candidate_start_at is not None:
            row["start_at"] = spec.explicit_candidate_start_at.isoformat()
            row["end_at"] = (
                spec.explicit_candidate_start_at
                + timedelta(minutes=spec.replacement_choice.duration_min)
            ).isoformat()

    if spec.candidate_remove:
        ... existing F1 behavior ...

    # existing frozen mobility travel_from_prev calculation
    ...

    session.flush()

    # NON-NEGOTIABLE AUTHORITY
    gate = validate_plan_feasibility(
        session,
        journey,
        proposed,
        additional_constraints=disruption_constraints,
        include_active_c4=True,
        mobility_provider=mobility,
    )
    if not gate.passed:
        delete_candidate_rows_and_plan(...)
        continue

    diff = diff_plans(...)
    travel_delta = same_snapshot_candidate_route - same_snapshot_base_route
    score = score_replan_candidate(...)

    verified_evidence = {
        ... existing evidence ...,
        "generator": spec.solver_provenance or {
            "kind": "DETERMINISTIC_HEURISTIC",
            "rank": generation_rank,
        },
        "feasibility_authority": "CITYNEXUS_DETERMINISTIC_GATE",
    }

    create_presented_proposal(...)
```

## Important failure rule

A solver exception must not silently bypass data-integrity failures. Only controlled solver conditions should fall back:

```text
ORToolsUnavailable
INFEASIBLE
UNKNOWN / configured timeout
MODEL_INVALID
zero assignments
all assignments rejected by post-gate validation
```

Unexpected programming exceptions should be visible in tests/logging rather than converted into a seemingly successful heuristic result without diagnostics.


