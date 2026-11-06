from app.collective_mediation import (
    classify_group_decision,
    collective_mutation_decision,
    fairness_receipt,
    plan_collective_fit_subject,
)


def _candidate(candidate_id: str, a: float, b: float, *, status: str = "GROUNDED_FEASIBLE"):
    return {
        "candidate_id": candidate_id,
        "status": status,
        "member_fits": {
            "member-a": {"effective_fit": a},
            "member-b": {"effective_fit": b},
        },
    }


def test_r27_fairness_receipt_does_not_hide_a_severe_loser_in_mean():
    candidates = [_candidate("garden", 1.0, 0.0), _candidate("craft", 0.0, 1.0)]
    fairness_receipt(candidates)
    assert candidates[0]["fairness"]["minimum_member_fit"] == 0.0
    assert candidates[0]["fairness"]["unacceptable_member_ids"] == ["member-b"]
    assert classify_group_decision(candidates)["classification"] == "HUMAN_TRADEOFF_REQUIRED"


def test_r27_compromise_is_available_only_when_worst_member_is_protected():
    candidates = [_candidate("garden", 1.0, 0.2), _candidate("craft", 0.2, 1.0), _candidate("shared", 0.7, 0.7)]
    result = classify_group_decision(candidates)
    assert result["classification"] == "COMPROMISE_AVAILABLE"
    assert result["selected_candidate_id"] == "shared"


def test_r27_unknown_world_facts_never_become_a_group_preference_question():
    candidates = [_candidate("unknown", 0.9, 0.9, status="REVALIDATION_REQUIRED")]
    assert classify_group_decision(candidates)["classification"] == "INSUFFICIENT_WORLD_FACTS"


def test_r27_fit_subject_excludes_unchanged_shared_and_locked_activities():
    base = [
        {"sequence_no": 1, "place_id": "hotel", "activity_type": "LODGING", "locked": False},
        {"sequence_no": 2, "place_id": "garden", "activity_type": "VISIT", "locked": False},
        {"sequence_no": 3, "place_id": "commitment", "activity_type": "C4", "locked": True},
    ]
    candidate = [
        {"sequence_no": 1, "place_id": "hotel", "activity_type": "LODGING", "locked": False},
        {"sequence_no": 2, "place_id": "craft", "activity_type": "VISIT", "category": "museum", "tags": ["craft"], "locked": False},
        {"sequence_no": 3, "place_id": "commitment", "activity_type": "C4", "locked": True},
    ]
    result = plan_collective_fit_subject(base, candidate)
    assert result == {
        "fit_subject_place_ids": ["craft"],
        "fit_subject_tags": ["craft", "museum"],
        "fit_subject_basis": "PLAN_DIFF",
    }


def test_r27_collective_gate_rejects_unresolved_tradeoff_and_wrong_compromise():
    mediation = {
        "classification": "HUMAN_TRADEOFF_REQUIRED",
        "candidate_ids": ["a", "b"],
    }
    assert collective_mutation_decision(mediation, "a")["code"] == "COLLECTIVE_TRADEOFF_UNRESOLVED"
    mediation = {
        "classification": "COMPROMISE_AVAILABLE",
        "candidate_ids": ["shared"],
        "selected_candidate_id": "shared",
    }
    assert collective_mutation_decision(mediation, "garden")["code"] == "COLLECTIVE_CANDIDATE_NOT_ACCEPTABLE_COMPROMISE"
    assert collective_mutation_decision(mediation, "shared")["allowed"] is True
