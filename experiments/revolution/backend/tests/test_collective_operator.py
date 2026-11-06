from types import SimpleNamespace

from app.collective_operator import classify_collective_operator_state
from app.operator_projection import attach_collective_recovery_projection


def test_single_traveller_is_not_collective():
    assert (
        classify_collective_operator_state(
            member_count=1,
            status=None,
        )
        == "SINGLE_TRAVELLER"
    )


def test_waiting_for_members_is_visible():
    assert (
        classify_collective_operator_state(
            member_count=3,
            status="WAITING_FOR_MEMBERS",
            response_count=1,
        )
        == "WAITING_FOR_MEMBERS"
    )


def test_disagreement_enters_mediation_not_majority_resolution():
    assert (
        classify_collective_operator_state(
            member_count=3,
            status="MEDIATION_REQUIRED",
            response_count=3,
        )
        == "MEDIATING"
    )


def test_resolved_without_receipt_is_not_yet_authorized():
    assert (
        classify_collective_operator_state(
            member_count=2,
            status="RESOLVED",
            response_count=2,
            authority_receipt_present=False,
        )
        == "COLLECTIVE_READY"
    )


def test_authority_receipt_is_distinct_terminal_boundary():
    assert (
        classify_collective_operator_state(
            member_count=2,
            status="RESOLVED",
            response_count=2,
            authority_receipt_present=True,
        )
        == "COLLECTIVE_AUTHORIZED"
    )


def test_stale_collective_decision_never_looks_authorized():
    assert (
        classify_collective_operator_state(
            member_count=2,
            status="STALE",
            response_count=2,
            authority_receipt_present=True,
            stale=True,
        )
        == "COLLECTIVE_STALE"
    )


def test_group_recovery_links_only_its_own_shared_decision_and_does_not_claim_application():
    mission_rows = {
        "mediation-child": SimpleNamespace(id="mediation-child", parent_mission_id="recovery-root"),
    }
    session = SimpleNamespace(get=lambda _model, key: mission_rows.get(str(key)))
    recovery = {"state": "RECOVERY_OPTIONS_READY", "can_request_planning": True}
    collective = {
        "state": "COLLECTIVE_AUTHORIZED",
        "mission_id": "mediation-child",
        "required_count": 2,
        "response_count": 2,
        "waiting_for_member_count": 0,
        "viewer_has_responded": True,
        "authority_receipt_id": "receipt-current",
    }

    assert attach_collective_recovery_projection(
        session, recovery, collective, "recovery-root"
    ) is True
    assert recovery["state"] == "COLLECTIVE_AUTHORIZED"
    assert recovery["can_request_planning"] is False
    assert recovery["collective_decision"]["authority_receipt_id"] == "receipt-current"
    assert "not changed" in recovery["headline"]
    assert "applied" not in recovery["headline"].lower()

    unrelated = {"state": "RECOVERY_OPTIONS_READY", "can_request_planning": True}
    assert attach_collective_recovery_projection(
        session, unrelated, collective, "different-incident"
    ) is False
    assert "collective_decision" not in unrelated
