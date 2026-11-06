from __future__ import annotations

import dataclasses
import importlib.util

import pytest
from test_solver_reference_contract import _problem

from app.planning.ortools_replan_solver import ORToolsUnavailable, solve_replacement_candidates


def test_ortools_boundary_fails_controlled_when_dependency_is_missing():
    if importlib.util.find_spec("ortools") is not None:
        pytest.skip("ortools is installed in this environment")
    with pytest.raises(ORToolsUnavailable):
        solve_replacement_candidates(_problem(), candidate_count=2)


@pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="ortools optional dependency missing",
)
def test_real_ortools_matches_reference_feasible_option_set_for_v1_problem():
    from app.planning.reference_replan_solver import enumerate_reference_candidates

    problem = _problem()
    expected = enumerate_reference_candidates(problem, candidate_count=3)
    actual = solve_replacement_candidates(problem, candidate_count=3).candidates

    assert [item.option_key for item in actual] == [item.option_key for item in expected]
    assert [item.dropped for item in actual] == [item.dropped for item in expected]
    assert all(item.generator_kind == "ORTOOLS_CP_SAT" for item in actual)
    assert len({item.fingerprint for item in actual}) == len(actual)


@pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="ortools optional dependency missing",
)
def test_real_ortools_has_stable_semantic_tie_break_and_repeatability():
    base = _problem(allow_drop=False)
    template = base.replacement_options[0]
    option_a = dataclasses.replace(template, option_key="a", location_key="place-a")
    option_b = dataclasses.replace(template, option_key="b", location_key="place-b")
    travel = {
        (base.current_location_key, "place-a"): 20,
        ("place-a", base.next_anchor.location_key): 20,
        (base.current_location_key, "place-b"): 20,
        ("place-b", base.next_anchor.location_key): 20,
    }
    problem = dataclasses.replace(
        base,
        replacement_options=(option_b, option_a),
        travel_minutes=travel,
    )

    first = solve_replacement_candidates(problem, candidate_count=2).candidates
    second = solve_replacement_candidates(problem, candidate_count=2).candidates

    assert [item.option_key for item in first] == ["a", "b"]
    assert first == second
