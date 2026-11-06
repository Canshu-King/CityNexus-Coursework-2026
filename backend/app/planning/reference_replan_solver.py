from __future__ import annotations

from app.planning.solver_contract import (
    ReplanSolverProblem,
    SolverCandidate,
    solver_candidate_fingerprint,
)


def _best_start(*, earliest: int, latest: int, original: int) -> int:
    """Minimize absolute shift with a stable earliest-side tie break."""

    if earliest > latest:
        raise ValueError("empty feasible start interval")
    return min(max(original, earliest), latest)


def enumerate_reference_candidates(
    problem: ReplanSolverProblem, *, candidate_count: int
) -> tuple[SolverCandidate, ...]:
    """Dependency-free oracle for the small V1 replacement scheduling problem.

    This is not the production optimizer. It exists so OR-Tools output can be
    tested against an independently understandable deterministic contract.
    """

    if candidate_count < 1:
        raise ValueError("candidate_count must be positive")

    raw: list[SolverCandidate] = []
    anchor = problem.next_anchor
    for option in sorted(problem.replacement_options, key=lambda item: item.option_key):
        try:
            inbound = problem.travel(problem.current_location_key, option.location_key)
            outbound = problem.travel(option.location_key, anchor.location_key)
        except KeyError:
            continue
        earliest = max(problem.now_min + inbound, option.earliest_start_min)
        latest_start = min(
            option.latest_end_min - option.duration_min,
            anchor.start_min - problem.safety_buffer_min - outbound - option.duration_min,
            problem.journey_end_min - option.duration_min,
        )
        if earliest > latest_start:
            continue
        if option.cost_minor > problem.budget_remaining_minor:
            continue
        start = _best_start(
            earliest=earliest,
            latest=latest_start,
            original=option.original_start_min,
        )
        end = start + option.duration_min
        shift = abs(start - option.original_start_min)
        raw.append(
            SolverCandidate(
                option_key=option.option_key,
                location_key=option.location_key,
                start_min=start,
                end_min=end,
                cost_minor=option.cost_minor,
                inbound_travel_min=inbound,
                outbound_travel_min=outbound,
                schedule_shift_min=shift,
                preference_milli=option.preference_milli,
                dropped=False,
                fingerprint=solver_candidate_fingerprint(problem, f"{option.option_key}@{start}"),
                generator_kind="REFERENCE_ENUMERATOR",
                solver_status="FEASIBLE",
                generator_rank=0,
            )
        )

    if problem.allow_drop:
        try:
            direct = problem.travel(problem.current_location_key, anchor.location_key)
        except KeyError:
            direct = -1
        if direct >= 0 and problem.now_min + direct + problem.safety_buffer_min <= anchor.start_min:
            raw.append(
                SolverCandidate(
                    option_key=None,
                    location_key=None,
                    start_min=None,
                    end_min=None,
                    cost_minor=0,
                    inbound_travel_min=0,
                    outbound_travel_min=direct,
                    schedule_shift_min=0,
                    preference_milli=0,
                    dropped=True,
                    fingerprint=solver_candidate_fingerprint(problem, "DROP"),
                    generator_kind="REFERENCE_ENUMERATOR",
                    solver_status="FEASIBLE",
                    generator_rank=0,
                )
            )

    raw.sort(key=lambda item: (item.objective_key, item.fingerprint))
    unique: list[SolverCandidate] = []
    seen: set[str] = set()
    for item in raw:
        if item.fingerprint in seen:
            continue
        seen.add(item.fingerprint)
        unique.append(
            SolverCandidate(
                option_key=item.option_key,
                location_key=item.location_key,
                start_min=item.start_min,
                end_min=item.end_min,
                cost_minor=item.cost_minor,
                inbound_travel_min=item.inbound_travel_min,
                outbound_travel_min=item.outbound_travel_min,
                schedule_shift_min=item.schedule_shift_min,
                preference_milli=item.preference_milli,
                dropped=item.dropped,
                fingerprint=item.fingerprint,
                generator_kind=item.generator_kind,
                solver_status=item.solver_status,
                generator_rank=len(unique),
            )
        )
        if len(unique) >= candidate_count:
            break
    return tuple(unique)
