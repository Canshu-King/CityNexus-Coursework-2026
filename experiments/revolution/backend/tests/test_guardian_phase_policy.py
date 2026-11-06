from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent.watch_policy import journey_phase_projection


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, _statement):
        return _Rows(self._rows)


def _journey():
    return SimpleNamespace(
        id="journey-phase",
        active_plan_version_id="plan-phase",
        timezone="Asia/Tokyo",
        status="ACTIVE",
    )


def _activity(activity_id, sequence_no, status, start_at, end_at):
    return SimpleNamespace(
        id=activity_id,
        sequence_no=sequence_no,
        status=status,
        start_at=start_at,
        end_at=end_at,
    )


def test_explicit_en_route_execution_truth_wins_over_schedule():
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    rows = [
        _activity(
            "a1",
            1,
            "EN_ROUTE",
            (now + timedelta(hours=4)).isoformat(),
            (now + timedelta(hours=5)).isoformat(),
        ),
        _activity(
            "a2",
            2,
            "PLANNED",
            (now + timedelta(hours=6)).isoformat(),
            (now + timedelta(hours=7)).isoformat(),
        ),
    ]

    result = journey_phase_projection(_Session(rows), _journey(), now=now)

    assert result["journey_phase"] == "IN_TRANSIT"
    assert result["phase_authority"] == "EXECUTION_TRUTH"
    assert result["current_activity_id"] == "a1"
    assert result["monitored_activity_ids"] == ["a1", "a2"]


def test_clock_in_activity_is_control_plane_only_and_scope_is_bounded():
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    rows = [
        _activity(
            "a1",
            1,
            "PLANNED",
            (now - timedelta(minutes=15)).isoformat(),
            (now + timedelta(minutes=45)).isoformat(),
        ),
        _activity(
            "a2",
            2,
            "PLANNED",
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=2)).isoformat(),
        ),
        _activity(
            "a3",
            3,
            "PLANNED",
            (now + timedelta(hours=3)).isoformat(),
            (now + timedelta(hours=4)).isoformat(),
        ),
    ]

    result = journey_phase_projection(_Session(rows), _journey(), now=now)

    assert result["journey_phase"] == "IN_ACTIVITY"
    assert result["phase_authority"] == "CONTROL_PLANE_ONLY"
    assert result["current_activity_id"] == "a1"
    assert result["monitored_activity_ids"] == ["a1", "a2"]


def test_overnight_phase_watches_only_next_two_activities():
    now = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)  # 23:00 Asia/Tokyo
    rows = [
        _activity(
            "a1",
            1,
            "PLANNED",
            "2026-10-07T09:00:00+09:00",
            "2026-10-07T10:00:00+09:00",
        ),
        _activity(
            "a2",
            2,
            "PLANNED",
            "2026-10-07T11:00:00+09:00",
            "2026-10-07T12:00:00+09:00",
        ),
        _activity(
            "a3",
            3,
            "PLANNED",
            "2026-10-07T14:00:00+09:00",
            "2026-10-07T15:00:00+09:00",
        ),
    ]

    result = journey_phase_projection(_Session(rows), _journey(), now=now)

    assert result["journey_phase"] == "OVERNIGHT"
    assert result["monitored_activity_ids"] == ["a1", "a2"]


def test_contradictory_execution_truth_fails_to_unknown_without_authority():
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    rows = [
        _activity("a1", 1, "EN_ROUTE", now.isoformat(), (now + timedelta(hours=1)).isoformat()),
        _activity(
            "a2",
            2,
            "IN_PROGRESS",
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=2)).isoformat(),
        ),
    ]

    result = journey_phase_projection(_Session(rows), _journey(), now=now)

    assert result["journey_phase"] == "UNKNOWN"
    assert result["phase_reason_code"] == "CONTRADICTORY_EXECUTION_TRUTH"
    assert result["phase_authority"] == "CONTROL_PLANE_ONLY"
