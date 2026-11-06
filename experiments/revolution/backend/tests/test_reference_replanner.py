from app.replanning.reference_replanner import (
    Activity,
    Candidate,
    Commitment,
    choose_best,
    score_metrics,
    validate_candidate,
    weights_sum,
)


def metrics(value: float):
    return {
        "commitment_preservation": value,
        "stability": value,
        "time_reliability": value,
        "preference_fit": value,
        "mobility_fit": value,
        "budget_fit": value,
        "robustness": value,
        "opportunity_value": value,
    }


def test_weights_sum_to_one():
    assert abs(weights_sum() - 1.0) < 1e-12


def test_perfect_metrics_score_one():
    assert abs(score_metrics(metrics(1.0)) - 1.0) < 1e-12


def test_locked_commitment_is_hard_gate():
    candidate = Candidate(
        fingerprint="x",
        activities=(Activity("optional", 60, 120, 1000),),
        metrics=metrics(0.9),
    )
    result = validate_candidate(
        candidate,
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[Commitment("cruise", "LOCKED", 390)],
    )
    assert not result.passed
    assert "LOCKED_COMMITMENT_LOST:cruise" in result.failures


def test_overlap_is_rejected():
    candidate = Candidate(
        fingerprint="x",
        activities=(Activity("a", 60, 120), Activity("b", 110, 180)),
        metrics=metrics(0.8),
    )
    result = validate_candidate(
        candidate,
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[],
    )
    assert not result.passed
    assert "ACTIVITY_OVERLAP" in result.failures


def test_deterministic_tie_break_prefers_fewer_changes():
    a = Candidate("a", (Activity("x", 60, 120),), metrics(0.8), activity_changes=2)
    b = Candidate("b", (Activity("x", 60, 120),), metrics(0.8), activity_changes=1)
    assert choose_best([a, b]).fingerprint == "b"
