"""Witness for the shape the live product actually answers with.

The adapter's original test fixture put the *same full object* into both `proposal` and
`proposals`, so it never met the real response: the product sends a twelve-field
`proposal` plus a four-field `proposals` summary. Feeding that summary to the
twelve-field validator made every live `request_replan` fail, while the fleet stayed
green. These two cases pin both halves of the corrected contract:

  * a summary row may not destroy a call that has a full proposal to project, but it
    must be reported rather than dropped silently;
  * when nothing is viewable, the call still fails closed — an incomplete projection is
    never handed out as if it were usable.
"""
from __future__ import annotations

import copy

from citynexus_mcp.server import CityNexusMCPServer
from citynexus_mcp.schemas import MCPToolCall

FULL_CANDIDATE = {
    "id": "proposal-full",
    "proposed_plan_version_id": "plan-id",
    "status": "PRESENTED",
    "hard_constraints_passed": True,
    "locked_commitments_preserved": True,
    "utility_score": 0.75,
    "diff": {
        "candidate_family": "REPLACE",
        "kept": ["place:a"],
        "moved": [],
        "removed": [],
        "added": ["place:b"],
        "cost_delta_minor": 50,
        "travel_delta_min": -3,
        "end_time_delta_min": 4,
        "reason_codes": ["MINIMAL_DISRUPTION"],
        "preserved_protected": [],
        "after_feasibility_envelope": {
            "budget_remaining_minor": 950,
            "journey_end_slack_min": 25,
            "preserved_commitment_count": 0,
            "risk_reason_codes": [],
        },
        "verified_evidence": {
            "commitment_preservation": {"preserved": True, "protected_count": 0}
        },
    },
}

# Exactly what the product puts in `proposals` today.
SUMMARY_ROW = {
    "id": "proposal-summary",
    "status": "PRESENTED",
    "hard_constraints_passed": True,
    "locked_commitments_preserved": True,
}


class ShapedClient:
    """Stands in for the HTTP client, returning one canned response body."""

    def __init__(self, body: dict) -> None:
        self.body = body

    def call(self, tool_name: str, payload: dict) -> dict:
        return copy.deepcopy(self.body)


def _call(body: dict):
    server = CityNexusMCPServer(ShapedClient(body))
    return server.call(
        "request_replan",
        MCPToolCall("hermes-shape-0001", "hermes", {"journey_id": "journey-id"}),
    )


def test_a_summary_row_must_not_destroy_a_call_that_has_a_full_proposal():
    result = _call({
        "tool": "request_replan",
        "proposal": FULL_CANDIDATE,
        "proposals": [SUMMARY_ROW],
    })

    assert result["proposal_contract_version"] == "hermes-replan-proposal-view.v1"
    assert [v["proposal_id"] for v in result["proposal_views"]] == ["proposal-full"]
    assert result["proposal_views"][0]["feasibility_envelope"]["budget_remaining_minor"] == 950
    rejected = result["proposal_views_rejected"]
    assert [r["id"] for r in rejected] == ["proposal-summary"], (
        "the summary row must be named, not silently dropped"
    )
    assert "required fields" in rejected[0]["reason"]


def test_no_viewable_row_still_fails_closed():
    try:
        _call({"tool": "request_replan", "proposals": [SUMMARY_ROW]})
    except ValueError as exc:
        assert "required fields" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a four-field summary must never be projected as usable")


def test_missing_proposals_key_does_not_raise_a_bare_keyerror():
    # The product may answer with only the singular proposal; the old code indexed
    # result["proposals"] and died with KeyError instead of a decision.
    result = _call({"tool": "request_replan", "proposal": FULL_CANDIDATE})
    assert [v["proposal_id"] for v in result["proposal_views"]] == ["proposal-full"]
    assert "proposal_views_rejected" not in result
