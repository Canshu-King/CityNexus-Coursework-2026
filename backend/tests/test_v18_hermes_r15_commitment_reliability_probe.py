from copy import deepcopy

from scripts.hermes_r15_commitment_reliability_probe import (
    _commitment_truth,
    _proposal_commitment_truth,
    _read_only_commitment_truth,
)


def _c4():
    return [{
        "id": "c4-1",
        "journey_id": "j-1",
        "activity_id": None,
        "level": "C4_Anchored",
        "status": "ACTIVE",
        "monetary_value_minor": 12800,
        "policy": {
            "semantic_key": "place-riverside-museum",
            "scheduled_start_at": "2026-10-10T10:30:00+08:00",
        },
    }]


def test_r15_protected_commitment_comparison_is_exact_and_active():
    before = _c4()
    seed = {"protected_commitments_before": before}
    assert _commitment_truth(seed, before, list(before)) is True
    after = [dict(before[0], status="CANCELLED")]
    assert _commitment_truth(seed, before, after) is False


def test_r15_proposal_arm_allows_safe_abstention_or_full_persisted_contract():
    assert _proposal_commitment_truth({"response_count": 0}, []) is True
    assert _proposal_commitment_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, []) is True
    assert _proposal_commitment_truth({
        "response_count": 1,
        "full_contract_observed": False,
        "all_server_proposal_ids_persisted": True,
    }, []) is False
    assert _proposal_commitment_truth({"response_count": 0}, ["accept_replan"]) is False


def test_r15_read_only_arm_requires_unchanged_authority_and_commitment():
    before = {
        "entities": {
            "journey": {"status": "ACTIVE", "active_plan_version_id": "p1"},
            "plans": [{"id": "p1", "status": "ACTIVE"}],
            "events": [{"id": "e1", "status": "DETECTED"}],
            "proposals": [],
            "commitments": _c4(),
            "bookings": [],
            "payments": [],
        }
    }
    after = {"entities": deepcopy(before["entities"])}
    assert _read_only_commitment_truth(before, after, {"response_count": 0}, []) is True
    after["entities"]["commitments"][0]["status"] = "CANCELLED"
    assert _read_only_commitment_truth(before, after, {"response_count": 0}, []) is False
