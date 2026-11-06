from __future__ import annotations

import pytest

from app.replanning.reference_replanner import (
    Activity,
    Candidate,
    Commitment,
    choose_best,
    score_metrics,
    validate_candidate,
    weights_sum,
)
from app.schemas import GeneratePlansRequest


def metrics(value: float) -> dict[str, float]:
    return {
        name: value
        for name in (
            "commitment_preservation",
            "stability",
            "time_reliability",
            "preference_fit",
            "mobility_fit",
            "budget_fit",
            "robustness",
            "opportunity_value",
        )
    }


def test_cnx_u_001_weights_sum_to_one():
    assert weights_sum() == pytest.approx(1.0)


def test_cnx_u_002_perfect_component_metrics():
    assert score_metrics(metrics(1.0)) == pytest.approx(1.0)


def test_cnx_u_003_metrics_are_clamped():
    assert score_metrics(metrics(2.0)) == pytest.approx(1.0)
    assert score_metrics(metrics(-1.0)) == pytest.approx(0.0)


def test_cnx_u_004_locked_commitment_missing_fails():
    candidate = Candidate("x", (Activity("optional", 60, 120),), metrics(0.9))
    result = validate_candidate(
        candidate,
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[Commitment("cruise", "LOCKED", 390)],
    )
    assert not result.passed and "LOCKED_COMMITMENT_LOST:cruise" in result.failures


def test_cnx_u_005_locked_commitment_start_changed_fails():
    candidate = Candidate("x", (Activity("cruise", 420, 480),), metrics(0.9))
    result = validate_candidate(
        candidate,
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[Commitment("cruise", "LOCKED", 390)],
    )
    assert not result.passed and "LOCKED_COMMITMENT_LOST:cruise" in result.failures


def test_cnx_u_006_overlap_fails():
    result = validate_candidate(
        Candidate("x", (Activity("a", 60, 120), Activity("b", 110, 180)), metrics(0.8)),
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[],
    )
    assert "ACTIVITY_OVERLAP" in result.failures


def test_cnx_u_007_window_fails():
    result = validate_candidate(
        Candidate("x", (Activity("a", -1, 120),), metrics(0.8)),
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[],
    )
    assert "JOURNEY_WINDOW_EXCEEDED" in result.failures


def test_cnx_u_008_budget_fails():
    result = validate_candidate(
        Candidate("x", (Activity("a", 1, 2, 10001),), metrics(0.8)),
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[],
    )
    assert "BUDGET_EXCEEDED" in result.failures


def test_cnx_u_009_unavailable_place_fails():
    result = validate_candidate(
        Candidate("x", (Activity("a", 1, 2, available=False),), metrics(0.8)),
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[],
    )
    assert "ACTIVITY_UNAVAILABLE" in result.failures


def test_cnx_u_010_equal_score_tie_break_is_deterministic():
    a = Candidate("a", (Activity("x", 60, 120),), metrics(0.8), activity_changes=2)
    b = Candidate("b", (Activity("x", 60, 120),), metrics(0.8), activity_changes=1)
    assert choose_best([a, b]).fingerprint == "b"


@pytest.mark.parametrize("mode", ["balanced", "budget", "explore", "strict_schedule"])
def test_cnx_u_019_planning_modes(mode: str):
    assert GeneratePlansRequest(planning_mode=mode).planning_mode == mode


def test_cnx_u_020_unknown_planning_mode_rejected():
    with pytest.raises(ValueError):
        GeneratePlansRequest(planning_mode="unknown")
