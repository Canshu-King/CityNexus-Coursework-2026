from scripts.hermes_r21_request_replan_retry_reliability_probe import _full_contract
from scripts.hermes_r22_response_loss_recovery_probe import _proposal_ids


def test_r22_accepts_a_persisted_full_proposal_after_delivery_loss():
    proposal = {
        "id": "r1",
        "journey_id": "j1",
        "trigger_event_id": "e1",
        "base_plan_version_id": "p1",
        "proposed_plan_version_id": "p2",
        "status": "PRESENTED",
        "hard_constraints_passed": True,
        "locked_commitments_preserved": True,
        "utility_score": 1,
        "score_breakdown": {},
        "diff": {},
        "explanation": "fixture",
    }
    response = {
        "proposal_contract_version": "hermes-replan-proposal-view.v1",
        "proposal": proposal,
        "proposals": [proposal],
        "proposal_views": [proposal],
    }
    assert _full_contract(response)
    assert _proposal_ids(response) == ["r1"]


def test_r22_does_not_treat_an_empty_delivery_as_a_persisted_proposal():
    assert _proposal_ids({"error": "SIMULATED_MCP_RESPONSE_LOST_AFTER_SERVER_PERSISTENCE"}) == []
