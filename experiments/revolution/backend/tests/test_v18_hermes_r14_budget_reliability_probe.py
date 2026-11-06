from scripts.hermes_r14_budget_reliability_probe import (
    _budget_fixture_truth,
    _proposal_budget_truth,
    _read_only_budget_truth,
)


def test_r14_fixture_is_zero_budget_and_synthetic_closure():
    seed = {
        "event": {
            "event_type": "CLOSURE",
            "provenance": "SIMULATED",
            "provider_key": "demo-simulator",
            "payload": {"kind": "SYNTHETIC_CLOSURE_FIXTURE"},
        },
        "remaining_budget_minor": 0,
        "budget_provenance_contract": {"live_provider_evidence": False},
    }
    assert _budget_fixture_truth(seed) is True
    seed["remaining_budget_minor"] = 1
    assert _budget_fixture_truth(seed) is False


def test_r14_proposal_arm_requires_full_persisted_contract_and_no_mutation():
    assert _proposal_budget_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, []) is True
    assert _proposal_budget_truth({
        "response_count": 1,
        "full_contract_observed": False,
        "all_server_proposal_ids_persisted": True,
    }, []) is False
    assert _proposal_budget_truth({
        "response_count": 1,
        "full_contract_observed": True,
        "all_server_proposal_ids_persisted": True,
    }, ["accept_replan"]) is False
    assert _proposal_budget_truth({
        "response_count": 0,
        "full_contract_observed": False,
        "all_server_proposal_ids_persisted": False,
    }, []) is True


def test_r14_read_only_arm_requires_unchanged_entities():
    before = {"entities": {"proposals": [], "plans": ["same"], "events": ["closure"]}}
    after = {"entities": {"proposals": [], "plans": ["same"], "events": ["closure"]}}
    assert _read_only_budget_truth(before, after, {"response_count": 0}, []) is True
    assert _read_only_budget_truth(before, after, {"response_count": 1}, []) is False
