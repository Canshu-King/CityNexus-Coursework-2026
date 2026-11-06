from app.operator_attention import (
    AttentionCandidate,
    attention_from_operator_projection,
    choose_primary_attention,
)


def test_no_unresolved_source_means_no_attention():
    assert attention_from_operator_projection({}) is None


def test_partial_transaction_beats_approval():
    item = attention_from_operator_projection({
        "operations": {"status": "PARTIALLY_APPLIED", "plan_id": "tx1"},
        "approval": {"required": True, "approval_id": "ap1"},
    })
    assert item is not None
    assert item["kind"] == "TRANSACTION_PARTIAL_ATTENTION"


def test_approval_beats_route_check():
    item = attention_from_operator_projection({
        "approval": {"required": True, "approval_id": "ap1", "action_label": "Review"},
        "flow": {
            "state": "PREPARING_TO_LEAVE",
            "route": {"state": "ROUTE_NEEDED"},
            "next": {"id": "a2"},
        },
    })
    assert item is not None
    assert item["kind"] == "APPROVAL_REQUIRED"


def test_collective_attention_is_only_for_viewer_who_has_not_responded():
    pending = attention_from_operator_projection({
        "collective": {
            "state": "WAITING_FOR_MEMBERS",
            "viewer_has_responded": False,
            "viewer_member_id": "m1",
            "mission_id": "mission1",
            "decision_epoch": "e1",
        }
    })
    assert pending is not None
    assert pending["kind"] == "COLLECTIVE_RESPONSE_REQUIRED"

    done = attention_from_operator_projection({
        "collective": {
            "state": "WAITING_FOR_MEMBERS",
            "viewer_has_responded": True,
            "viewer_member_id": "m1",
            "mission_id": "mission1",
            "decision_epoch": "e1",
        }
    })
    assert done is None


def test_intent_clarification_becomes_one_primary_attention():
    item = attention_from_operator_projection({
        "intents": [{
            "id": "intent1",
            "status": "NEEDS_CLARIFICATION",
            "evaluation_state": "NEEDS_CLARIFICATION",
        }],
        "flow": {
            "state": "READY_TO_COMPLETE",
            "current": {"id": "a1"},
        },
    })
    assert item is not None
    assert item["kind"] == "INTENT_CLARIFICATION_REQUIRED"
    assert item["source_ref"]["intent_id"] == "intent1"


def test_arrival_milestone_can_be_primary_when_nothing_more_important_blocks():
    item = attention_from_operator_projection({
        "flow": {
            "state": "ARRIVAL_READY",
            "current": {"id": "a1"},
            "route": {"state": "AVAILABLE"},
        }
    })
    assert item is not None
    assert item["kind"] == "CONFIRM_ARRIVAL"
    assert item["action_label"] == "I’m here"


def test_route_check_is_lower_priority_preparation():
    item = attention_from_operator_projection({
        "flow": {
            "state": "PREPARING_TO_LEAVE",
            "route": {"state": "ROUTE_NEEDED"},
            "next": {"id": "a2"},
        }
    })
    assert item is not None
    assert item["kind"] == "ROUTE_CHECK_REQUIRED"


def test_choose_primary_is_stable():
    rows = [
        AttentionCandidate("ROUTE_CHECK_REQUIRED", 30, False, "b", "Route", "Route"),
        AttentionCandidate("APPROVAL_REQUIRED", 10, True, "a", "Approval", "Approval"),
    ]
    assert choose_primary_attention(rows).kind == "APPROVAL_REQUIRED"


def test_degraded_brain_does_not_hide_actionable_attention():
    item = attention_from_operator_projection({
        "brain": {"state": "TEMPORARILY_UNAVAILABLE", "headline": "Planning unavailable."},
        "flow": {
            "state": "READY_TO_COMPLETE",
            "current": {"id": "a1"},
        },
    })
    assert item is not None
    assert item["kind"] == "CONFIRM_COMPLETE"


def test_degraded_brain_is_visible_when_nothing_else_needs_user():
    item = attention_from_operator_projection({
        "brain": {"state": "TEMPORARILY_UNAVAILABLE", "headline": "Planning unavailable."},
    })
    assert item is not None
    assert item["kind"] == "OPERATOR_DEGRADED"
