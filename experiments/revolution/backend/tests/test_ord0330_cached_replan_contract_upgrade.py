"""ORD-0330 witness: a cached request_replan answer must arrive carrying the current contract.

The mainline builds `proposals` in the legacy four-field shape, writes that body into the
AGENT_IDEMPOTENCY_RESPONSE cache, and replays it for a reused request_id. Before ORD-0330 the replay
handed that legacy body straight back to the caller. The first call therefore pins the shape that is
actually cached; the second call, which can only be served from the cache, must come back carrying the
current contract fields. Remove the upgrade arm at the call site and this test goes red instead of
silently passing on the first call's shape.
"""
from __future__ import annotations

from uuid import uuid4

from test_agent_runtime_gate import _agent_call, _make_proposal

LEGACY_REPLAN_PROPOSAL_FIELDS = {
    "id",
    "status",
    "hard_constraints_passed",
    "locked_commitments_preserved",
}
CURRENT_REPLAN_PROPOSAL_FIELDS = {
    "id",
    "journey_id",
    "trigger_event_id",
    "base_plan_version_id",
    "proposed_plan_version_id",
    "status",
    "hard_constraints_passed",
    "locked_commitments_preserved",
    "utility_score",
    "score_breakdown",
    "diff",
    "explanation",
}


def _proposals_of(body: object) -> list[dict[str, object]]:
    proposals = body["proposals"]
    assert isinstance(proposals, list) and proposals, body
    return proposals


def test_first_request_replan_answer_is_the_legacy_shape_that_gets_cached():
    journey_id, event_id, _ = _make_proposal()
    response = _agent_call(
        "request_replan",
        journey_id=journey_id,
        trigger_event_id=event_id,
        request_id=f"ord0330-fresh-{uuid4()}",
    )
    assert response.status_code in (200, 201), response.text
    proposal = _proposals_of(response.json())[0]
    assert set(proposal) == LEGACY_REPLAN_PROPOSAL_FIELDS, sorted(proposal)


def test_replayed_request_replan_answer_carries_the_current_contract():
    journey_id, event_id, _ = _make_proposal()
    request_id = f"ord0330-replay-{uuid4()}"

    first = _agent_call(
        "request_replan", journey_id=journey_id, trigger_event_id=event_id, request_id=request_id
    )
    assert first.status_code in (200, 201), first.text
    first_proposal = _proposals_of(first.json())[0]

    second = _agent_call(
        "request_replan", journey_id=journey_id, trigger_event_id=event_id, request_id=request_id
    )
    assert second.status_code in (200, 201), second.text
    second_proposal = _proposals_of(second.json())[0]

    assert CURRENT_REPLAN_PROPOSAL_FIELDS.issubset(second_proposal), sorted(second_proposal)
    assert second_proposal["id"] == first_proposal["id"]
    assert second_proposal["status"] == first_proposal["status"]
    assert second_proposal["hard_constraints_passed"] == first_proposal["hard_constraints_passed"]
    assert second_proposal["locked_commitments_preserved"] == first_proposal[
        "locked_commitments_preserved"
    ]
    assert second_proposal["journey_id"] == journey_id
    assert second_proposal["trigger_event_id"] == event_id


def test_replayed_answer_still_refuses_a_different_request_under_the_same_id():
    journey_id, event_id, _ = _make_proposal()
    request_id = f"ord0330-conflict-{uuid4()}"

    first = _agent_call(
        "request_replan",
        journey_id=journey_id,
        trigger_event_id=event_id,
        candidate_count=3,
        request_id=request_id,
    )
    assert first.status_code == 200, first.text

    conflict = _agent_call(
        "request_replan",
        journey_id=journey_id,
        trigger_event_id=event_id,
        candidate_count=1,
        request_id=request_id,
    )
    assert conflict.status_code == 409, conflict.text
