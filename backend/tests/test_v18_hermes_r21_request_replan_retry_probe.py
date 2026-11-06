from scripts.hermes_r21_request_replan_retry_reliability_probe import (
    FULL_REPLAN_FIELDS,
    _full_contract,
    _identity_projection,
    _semantic_response,
)


def test_r21_full_contract_and_semantic_projection_are_stable():
    proposal = {key: key for key in FULL_REPLAN_FIELDS}
    response = {
        "tool": "request_replan",
        "proposal_contract_version": "hermes-replan-proposal-view.v1",
        "proposals": [proposal],
        "proposal_views": [proposal],
        "request_id": "same-request",
    }
    assert _full_contract(response)
    assert _semantic_response(response)["request_id"] == "same-request"


def test_r21_identity_projection_exposes_retry_mutation_sets():
    state = {
        "entities": {
            "journey": {
                "id": "j1",
                "status": "ACTIVE",
                "active_plan_version_id": "p1",
                "current_activity_id": "a1",
            },
            "proposals": [{"id": "r1"}],
            "plans": [{"id": "p1", "status": "ACTIVE", "journey_id": "j1"}],
            "events": [{"id": "e1", "status": "DETECTED", "event_type": "CLOSURE"}],
            "idempotency_keys": [{"key": "k1"}],
        }
    }
    projected = _identity_projection(state)
    assert projected["journey"]["active_plan_version_id"] == "p1"
    assert projected["proposal_ids"] == ["r1"]
    assert projected["idempotency_keys"] == ["k1"]
