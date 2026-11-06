from app.operator_projection import classify_operator_state


def test_operator_off_without_enrollment_or_mission():
    assert classify_operator_state(enabled=False, mission_active=True) == "OFF"
    assert classify_operator_state(enabled=True, mission_active=False) == "OFF"


def test_operator_waits_for_exact_pending_approval():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            guardian_action_status="REQUIRE_CONFIRMATION",
            agent_status="WAITING_FOR_APPROVAL",
            pending_status="PENDING",
            approval_status="PENDING",
            material_event_open=True,
        )
        == "WAITING_FOR_APPROVAL"
    )


def test_operator_applying_during_continuation():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            pending_status="RESUMING",
            approval_status="APPROVED",
            material_event_open=True,
        )
        == "APPLYING"
    )


def test_operator_recovered_after_exact_continuation():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            guardian_action_status="REQUIRE_CONFIRMATION",
            pending_status="RESUMED",
            approval_status="CONSUMED",
            active_plan_changed=True,
        )
        == "RECOVERED"
    )


def test_operator_investigates_open_incident():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            guardian_status="RUNNING",
            agent_status="RUNNING",
            material_event_open=True,
        )
        == "INVESTIGATING"
    )


def test_operator_needs_attention_when_no_safe_recovery_exists():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            guardian_status="COMPLETED",
            guardian_action_status="NO_FEASIBLE_RECOVERY",
            material_event_open=True,
        )
        == "NEEDS_ATTENTION"
    )


def test_operator_returns_to_watching_when_idle():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            guardian_status="COMPLETED",
            guardian_action_status="NO_ACTION",
            material_event_open=False,
        )
        == "WATCHING"
    )


def test_operator_degraded_is_visible():
    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            degraded=True,
        )
        == "DEGRADED"
    )
