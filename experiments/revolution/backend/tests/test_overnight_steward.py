from datetime import datetime, timezone

from app.agent.overnight_steward import _local_date, overnight_cycle_key, overnight_steward_state


NOW = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)  # 23:30 JST


def _activity(
    ident: str,
    *,
    status: str = "NOT_STARTED",
    start_at: str = "2026-10-08T00:00:00+00:00",
):
    return {"id": ident, "status": status, "start_at": start_at}


def test_future_local_day_enters_day_closing_before_first_cycle_finishes():
    result = overnight_steward_state(
        journey_status="ACTIVE",
        timezone_name="Asia/Tokyo",
        activities=[_activity("a2")],
        now=NOW,
        day_closed=False,
    )
    assert result["state"] == "DAY_CLOSING"
    assert result["tomorrow_activity_ids"] == ["a2"]


def test_existing_execution_truth_blocks_false_overnight_close():
    result = overnight_steward_state(
        journey_status="ACTIVE",
        timezone_name="Asia/Tokyo",
        activities=[
            _activity(
                "a1",
                status="IN_PROGRESS",
                start_at="2026-10-07T13:00:00+00:00",
            ),
            _activity("a2"),
        ],
        now=NOW,
    )
    assert result["state"] == "INACTIVE"
    assert result["reason"] == "EXECUTION_STILL_ACTIVE"


def test_closed_day_keeps_same_operator_watching_tomorrow():
    result = overnight_steward_state(
        journey_status="ACTIVE",
        timezone_name="Asia/Tokyo",
        activities=[_activity("a2"), _activity("a3", start_at="2026-10-08T03:00:00+00:00")],
        now=NOW,
        day_closed=True,
    )
    assert result["state"] == "WATCHING_TOMORROW"
    assert result["tomorrow_activity_ids"] == ["a2", "a3"]


def test_morning_ready_is_distinct_from_overnight_watch():
    result = overnight_steward_state(
        journey_status="ACTIVE",
        timezone_name="Asia/Tokyo",
        activities=[_activity("a2")],
        now=NOW,
        day_closed=True,
        tomorrow_ready=True,
    )
    assert result["state"] == "MORNING_READY"


def test_provider_degradation_is_visible_without_losing_tomorrow():
    result = overnight_steward_state(
        journey_status="ACTIVE",
        timezone_name="Asia/Tokyo",
        activities=[_activity("a2")],
        now=NOW,
        day_closed=True,
        provider_degraded=True,
    )
    assert result["state"] == "OVERNIGHT_PROVIDER_DEGRADED"
    assert result["tomorrow_activity_ids"] == ["a2"]


def test_completed_journey_never_starts_new_overnight_cycle():
    result = overnight_steward_state(
        journey_status="COMPLETED",
        timezone_name="Asia/Tokyo",
        activities=[_activity("a2")],
        now=NOW,
    )
    assert result["state"] == "INACTIVE"
    assert result["reason"] == "JOURNEY_COMPLETE"


def test_day_boundary_identity_is_stable():
    first = overnight_cycle_key(
        journey_id="j1",
        local_date="2026-10-07",
        plan_version_id="p1",
    )
    second = overnight_cycle_key(
        journey_id="j1",
        local_date="2026-10-07",
        plan_version_id="p1",
    )
    assert first == second


def test_plan_generation_changes_boundary_identity():
    assert overnight_cycle_key(
        journey_id="j1",
        local_date="2026-10-07",
        plan_version_id="p1",
    ) != overnight_cycle_key(
        journey_id="j1",
        local_date="2026-10-07",
        plan_version_id="p2",
    )


def test_day_identity_uses_journey_timezone_not_utc_date():
    # 16:30Z is already 01:30 on the next date in Tokyo.
    assert _local_date("2026-10-07T16:30:00+00:00", "Asia/Tokyo").isoformat() == "2026-10-08"


def test_boundary_key_includes_journey_local_date_and_active_plan():
    assert overnight_cycle_key(journey_id="j1", local_date="2026-10-08", plan_version_id="p1") == "overnight:j1:2026-10-08:p1"
