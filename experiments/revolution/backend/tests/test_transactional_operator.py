from app.agent.transactional_operator import (
    COMPLETED,
    FAILED_SAFE,
    NEEDS_ATTENTION,
    PARTIALLY_APPLIED,
    WAITING_FOR_APPROVAL,
    RecoveryExecutionAction,
    action_fingerprint,
    classify_execution_state,
    model_supplied_transaction_fields_are_forbidden,
    provider_truth_label,
)


def test_action_identity_is_semantic_and_stable():
    action = RecoveryExecutionAction(
        kind="CREATE_BOOKING",
        target_type="Activity",
        target_id="a1",
        provider_key="mock-booking",
    )
    first = action_fingerprint(
        journey_id="j1",
        recovery_epoch_id="r1",
        action=action,
    )
    second = action_fingerprint(
        journey_id="j1",
        recovery_epoch_id="r1",
        action=action,
    )
    assert first == second


def test_different_target_or_epoch_changes_action_identity():
    a1 = RecoveryExecutionAction("CREATE_BOOKING", "Activity", "a1", "mock-booking")
    a2 = RecoveryExecutionAction("CREATE_BOOKING", "Activity", "a2", "mock-booking")
    assert action_fingerprint(journey_id="j1", recovery_epoch_id="r1", action=a1) != action_fingerprint(
        journey_id="j1", recovery_epoch_id="r1", action=a2
    )
    assert action_fingerprint(journey_id="j1", recovery_epoch_id="r1", action=a1) != action_fingerprint(
        journey_id="j1", recovery_epoch_id="r2", action=a1
    )


def test_waiting_for_approval_is_not_execution():
    assert classify_execution_state(["WAITING_FOR_APPROVAL"]) == WAITING_FOR_APPROVAL


def test_partial_success_is_not_called_recovered():
    assert classify_execution_state(["CANCELLED", "FAILED"]) == PARTIALLY_APPLIED


def test_all_failed_is_safe_failure():
    assert classify_execution_state(["FAILED"]) == FAILED_SAFE


def test_unresolved_required_action_needs_attention():
    assert (
        classify_execution_state(
            ["CONFIRMED"],
            unresolved_required_actions=True,
        )
        == NEEDS_ATTENTION
    )


def test_completed_actions_can_converge():
    assert classify_execution_state(["CANCELLED", "CONFIRMED"]) == COMPLETED


def test_sandbox_truth_is_explicit():
    assert provider_truth_label("mock-booking") == "LAB_SANDBOX_PROVIDER"
    assert provider_truth_label("sandbox-hotel") == "LAB_SANDBOX_PROVIDER"
    assert provider_truth_label("live-merchant") == "EXTERNAL_PROVIDER"


def test_model_cannot_claim_provider_transaction_truth():
    assert model_supplied_transaction_fields_are_forbidden({"external_ref": "fake"}) is True
    assert model_supplied_transaction_fields_are_forbidden({"provider_success": True}) is True
    assert model_supplied_transaction_fields_are_forbidden({"activity_id": "a1"}) is False


def test_frozen_action_payload_contains_only_server_owned_target_facts():
    payload = RecoveryExecutionAction(
        "CREATE_BOOKING", "Activity", "activity-1", "mock-booking"
    ).semantic_payload()
    assert payload == {
        "kind": "CREATE_BOOKING",
        "target_type": "Activity",
        "target_id": "activity-1",
        "provider_key": "mock-booking",
        "requires_approval": True,
    }
    assert "external_ref" not in payload
    assert "provider_success" not in payload
