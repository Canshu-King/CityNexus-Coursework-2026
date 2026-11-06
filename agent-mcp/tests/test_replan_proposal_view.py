from __future__ import annotations

import pytest

from citynexus_mcp.schemas import HermesReplanProposalView


def proposal_payload():
    return {
        "id": "proposal-id",
        "proposed_plan_version_id": "plan-id",
        "status": "PRESENTED",
        "hard_constraints_passed": True,
        "locked_commitments_preserved": True,
        "utility_score": 0.72,
        "diff": {
            "candidate_family": "REPLACE",
            "kept": ["place:a"],
            "moved": [{"semantic_key": "place:a", "delta_minutes": 10}],
            "removed": [],
            "added": ["place:b"],
            "cost_delta_minor": 120,
            "travel_delta_min": -4,
            "end_time_delta_min": 6,
            "reason_codes": ["MINIMAL_DISRUPTION"],
            "preserved_protected": ["commitment:c4"],
            "after_feasibility_envelope": {
                "budget_remaining_minor": 880,
                "journey_end_slack_min": 35,
                "preserved_commitment_count": 1,
                "risk_reason_codes": [],
            },
            "verified_evidence": {
                "commitment_preservation": {"preserved": True, "protected_count": 1}
            },
        },
    }


def test_replan_proposal_view_keeps_required_semantic_fields():
    view = HermesReplanProposalView.from_payload(proposal_payload())

    assert view.proposal_id == "proposal-id"
    assert view.proposed_plan_version_id == "plan-id"
    assert view.candidate_family == "REPLACE"
    assert view.added == ("place:b",)
    assert view.cost_delta_minor == 120
    assert view.protected_preservation == {
        "preserved_protected": ["commitment:c4"],
        "preserved": True,
        "protected_count": 1,
    }
    assert view.feasibility_envelope["journey_end_slack_min"] == 35
    assert view.as_dict()["moved"][0]["delta_minutes"] == 10


def test_replan_proposal_view_does_not_turn_missing_evidence_into_defaults():
    payload = proposal_payload()
    del payload["diff"]["cost_delta_minor"]

    with pytest.raises(ValueError, match="cost_delta_minor"):
        HermesReplanProposalView.from_payload(payload)


def test_replan_proposal_view_requires_budget_slack_and_risk_envelope():
    payload = proposal_payload()
    del payload["diff"]["after_feasibility_envelope"]["risk_reason_codes"]

    with pytest.raises(ValueError, match="feasibility envelope"):
        HermesReplanProposalView.from_payload(payload)
