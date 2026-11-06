from datetime import datetime, timedelta, timezone

from app.execution.continuous_journey import (
    continuous_journey_state,
    milestone_notification_key,
)


NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def _state(**overrides):
    payload = {
        "journey_status": "ACTIVE",
        "activity_status": "ACTIVE",
        "presence_state": "LOCATION_UNAVAILABLE",
        "readiness_state": "READY",
        "must_leave_by": (NOW + timedelta(minutes=20)).isoformat(),
        "activity_end_at": (NOW + timedelta(hours=2)).isoformat(),
        "next_activity_id": "a2",
        "operator_needs_attention": False,
        "now": NOW,
    }
    payload.update(overrides)
    return continuous_journey_state(**payload)


def test_en_route_is_one_continuous_on_the_way_state():
    result = _state(activity_status="EN_ROUTE")
    assert result["state"] == "ON_THE_WAY"


def test_near_target_surfaces_arrival_confirmation_without_claiming_arrival():
    result = _state(activity_status="EN_ROUTE", presence_state="NEAR_TARGET")
    assert result["state"] == "ARRIVAL_READY"
    assert result["recommended_action"] == "CONFIRM_ARRIVAL"


def test_arrived_moves_to_ready_to_begin():
    result = _state(activity_status="ARRIVED")
    assert result["state"] == "READY_TO_BEGIN"
    assert result["recommended_action"] == "CONFIRM_BEGIN"


def test_activity_does_not_complete_from_clock_before_end():
    result = _state(activity_status="IN_PROGRESS")
    assert result["state"] == "IN_ACTIVITY"


def test_planned_end_only_surfaces_done_confirmation():
    result = _state(
        activity_status="IN_PROGRESS",
        activity_end_at=(NOW - timedelta(minutes=5)).isoformat(),
    )
    assert result["state"] == "READY_TO_COMPLETE"
    assert result["recommended_action"] == "CONFIRM_COMPLETE"


def test_completed_activity_hands_off_to_next_stop():
    result = _state(activity_status="COMPLETED", next_activity_id="a2")
    assert result["state"] == "BETWEEN_STOPS"
    assert result["recommended_action"] == "PREPARE_NEXT_STOP"


def test_last_activity_completion_is_day_complete():
    result = _state(
        journey_status="COMPLETED",
        activity_status="COMPLETED",
        next_activity_id=None,
    )
    assert result["state"] == "DAY_COMPLETE"


def test_due_departure_is_leave_now_not_auto_execution():
    result = _state(
        activity_status="ACTIVE",
        must_leave_by=(NOW - timedelta(minutes=1)).isoformat(),
    )
    assert result["state"] == "LEAVE_NOW"
    assert result["recommended_action"] == "CONFIRM_DEPARTURE"


def test_operator_attention_overrides_happy_path():
    result = _state(
        activity_status="ACTIVE",
        operator_needs_attention=True,
    )
    assert result["state"] == "NEEDS_ATTENTION"


def test_milestone_notification_key_is_stable_per_activity_state():
    assert milestone_notification_key(
        journey_id="j1",
        activity_id="a1",
        state="ARRIVAL_READY",
    ) == milestone_notification_key(
        journey_id="j1",
        activity_id="a1",
        state="arrival_ready",
    )
