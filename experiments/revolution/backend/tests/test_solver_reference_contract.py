from __future__ import annotations

import dataclasses

import pytest

from app.planning.reference_replan_solver import enumerate_reference_candidates
from app.planning.solver_contract import (
    ReplanSolverProblem,
    SolverAnchor,
    SolverReplacementOption,
)


def _problem(*, budget: int = 5000, allow_drop: bool = True) -> ReplanSolverProblem:
    return ReplanSolverProblem(
        journey_id="journey-demo",
        operation_key="replan:event-closure",
        now_min=265,  # 13:25 relative to 09:00 Journey start
        journey_end_min=600,  # 19:00
        budget_remaining_minor=budget,
        current_location_key="place-tech-art",
        next_anchor=SolverAnchor(
            semantic_key="place-river-cruise",
            location_key="place-river-cruise",
            start_min=390,  # 15:30
            duration_min=60,
        ),
        replacement_options=(
            SolverReplacementOption(
                option_key="design-library",
                location_key="place-design-library",
                duration_min=60,
                cost_minor=2000,
                earliest_start_min=30,
                latest_end_min=600,
                original_start_min=260,
                preference_milli=900,
            ),
            SolverReplacementOption(
                option_key="city-park",
                location_key="place-city-park",
                duration_min=60,
                cost_minor=0,
                earliest_start_min=0,
                latest_end_min=600,
                original_start_min=260,
                preference_milli=300,
            ),
        ),
        frozen_mobility_snapshot_id="mobility-snapshot-1",
        travel_minutes={
            ("place-tech-art", "place-design-library"): 20,
            ("place-design-library", "place-river-cruise"): 20,
            ("place-tech-art", "place-city-park"): 20,
            ("place-city-park", "place-river-cruise"): 20,
            ("place-tech-art", "place-river-cruise"): 20,
        },
        completed_prefix_keys=("old-town", "museum", "food-hall"),
        safety_buffer_min=15,
        allow_drop=allow_drop,
    )


def test_reference_solver_produces_unique_ranked_candidates_and_preserves_problem():
    problem = _problem()
    before = problem.to_payload()

    candidates = enumerate_reference_candidates(problem, candidate_count=3)

    assert len(candidates) == 3
    assert [item.generator_rank for item in candidates] == [0, 1, 2]
    assert len({item.fingerprint for item in candidates}) == 3
    assert candidates[0].option_key == "design-library"
    assert candidates[0].start_min == 285  # 13:45 after frozen inbound travel
    assert candidates[0].end_min == 345
    assert candidates[0].end_min + candidates[0].outbound_travel_min + 15 <= 390
    assert problem.to_payload() == before


def test_reference_solver_budget_is_hard_and_drop_is_preferred_to_infeasible_option():
    problem = _problem(budget=0)

    candidates = enumerate_reference_candidates(problem, candidate_count=3)

    assert all(item.option_key != "design-library" for item in candidates)
    assert any(item.option_key == "city-park" for item in candidates)
    assert any(item.dropped for item in candidates)


def test_reference_solver_never_invents_missing_frozen_travel_fact():
    problem = _problem()
    broken = dataclasses.replace(
        problem,
        travel_minutes={
            ("place-tech-art", "place-river-cruise"): 20,
        },
    )

    candidates = enumerate_reference_candidates(broken, candidate_count=3)

    assert candidates
    assert all(item.dropped for item in candidates)


def test_reference_solver_rejects_duplicate_option_identity():
    option = _problem().replacement_options[0]
    with pytest.raises(ValueError, match="unique"):
        dataclasses.replace(_problem(), replacement_options=(option, option))


def test_reference_solver_is_reproducible_for_identical_frozen_input():
    first = enumerate_reference_candidates(_problem(), candidate_count=3)
    second = enumerate_reference_candidates(_problem(), candidate_count=3)

    assert first == second


def test_reference_solver_respects_opening_window_and_anchor_buffer():
    problem = _problem()
    too_late = dataclasses.replace(
        problem.replacement_options[0],
        earliest_start_min=340,
        latest_end_min=380,
    )
    constrained = dataclasses.replace(problem, replacement_options=(too_late,))

    candidates = enumerate_reference_candidates(constrained, candidate_count=2)

    assert candidates
    assert all(item.dropped for item in candidates)


def test_solver_problem_payload_is_json_serializable_and_contains_only_frozen_values():
    import json

    problem = _problem()
    encoded = json.dumps(problem.to_payload(), sort_keys=True)

    assert "mobility-snapshot-1" in encoded
    assert "place-tech-art" in encoded
    assert "Session" not in encoded
    assert "httpx" not in encoded


def test_problem_travel_mapping_is_structurally_immutable():
    problem = _problem()
    with pytest.raises(TypeError):
        problem.travel_minutes[("x", "y")] = 1  # type: ignore[index]
