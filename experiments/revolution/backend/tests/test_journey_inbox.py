from app.journey_inbox import (
    classify_inbox_impact,
    inbox_artifact_fingerprint,
    inbox_provenance_for_source,
    normalize_change_kind,
    reservation_change_fingerprint,
)


def test_same_pasted_message_reuses_artifact_identity():
    first = inbox_artifact_fingerprint(
        source_kind="PASTED_TEXT",
        normalized_text="Booking ZX42 changed to 18:10.",
    )
    second = inbox_artifact_fingerprint(
        source_kind="PASTED_TEXT",
        normalized_text="  Booking   ZX42 changed to 18:10. ",
    )
    assert first == second


def test_change_identity_is_semantic_not_run_based():
    first = reservation_change_fingerprint(
        journey_id="j1",
        confirmation_ref="ab123",
        change_kind="SCHEDULE_CHANGE",
        reservation_type="TRAIN",
        old_value="17:40",
        new_value="18:10",
    )
    second = reservation_change_fingerprint(
        journey_id="j1",
        confirmation_ref="AB123",
        change_kind="SCHEDULE_CHANGE",
        reservation_type="TRAIN",
        old_value="17:40",
        new_value="18:10",
    )
    assert first == second


def test_weak_binding_never_overwrites_reservation_truth():
    result = classify_inbox_impact(
        exact_binding=False,
        change_kind="SCHEDULE_CHANGE",
        affects_protected_commitment=True,
        schedule_delta_min=45,
    )
    assert result.state == "BINDING_REQUIRED"
    assert result.material is False


def test_protected_schedule_change_can_require_recovery():
    result = classify_inbox_impact(
        exact_binding=True,
        change_kind="SCHEDULE_CHANGE",
        affects_protected_commitment=True,
        schedule_delta_min=30,
    )
    assert result.state == "RECOVERY_REQUIRED"
    assert result.material is True


def test_small_grounded_change_can_apply_without_agent_recovery():
    result = classify_inbox_impact(
        exact_binding=True,
        change_kind="SCHEDULE_CHANGE",
        affects_protected_commitment=False,
        schedule_delta_min=10,
    )
    assert result.state == "APPLIED_NO_MATERIAL_IMPACT"
    assert result.material is False


def test_cancellation_is_material_without_deleting_history():
    result = classify_inbox_impact(
        exact_binding=True,
        change_kind="CANCELLATION",
        affects_protected_commitment=False,
        cancellation=True,
    )
    assert result.state == "RECOVERY_REQUIRED"
    assert result.material is True


def test_source_provenance_never_claims_live_provider_for_user_artifact():
    assert inbox_provenance_for_source("PASTED_TEXT") == "USER_PASTED_TEXT"
    assert inbox_provenance_for_source("IMAGE_UPLOAD") == "USER_UPLOADED_IMAGE"
    assert inbox_provenance_for_source("EMAIL_FORWARD_REPLAY") == "CAPTURED_REPLAY"
    assert inbox_provenance_for_source("LAB_MESSAGE_FIXTURE") == "LAB_FIXTURE"


def test_change_kind_aliases_are_bounded():
    assert normalize_change_kind("rescheduled") == "SCHEDULE_CHANGE"
    assert normalize_change_kind("cancelled") == "CANCELLATION"
    assert normalize_change_kind("something-weird") == "UNKNOWN"
