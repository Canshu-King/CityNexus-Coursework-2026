from __future__ import annotations

from types import SimpleNamespace

from app.main import _normalize_replan_schedule_time


def test_replan_preserves_existing_local_wall_time_when_zone_label_differs():
    journey = SimpleNamespace(
        start_at="2026-10-23T08:00:00+08:00",
        timezone="Asia/Tokyo",
    )

    assert _normalize_replan_schedule_time("2026-10-23T15:00:00+08:00", journey) == (
        "2026-10-23T15:00:00+08:00"
    )


def test_replan_converts_provider_instant_back_to_journey_schedule_offset():
    journey = SimpleNamespace(
        start_at="2026-10-23T08:00:00+08:00",
        timezone="Asia/Tokyo",
    )

    assert _normalize_replan_schedule_time("2026-10-23T16:00:00+09:00", journey) == (
        "2026-10-23T15:00:00+08:00"
    )
