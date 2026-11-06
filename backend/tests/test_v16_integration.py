from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.core.money import major_to_minor
from app.db.models import Event, Journey, JourneyTwin
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import (
    _disruption_semantic_status,
    classify_copilot_message,
    disruption_event_type,
    what_if_parameters,
)
from app.planning.day_rhythm import DayRhythm, apply_day_rhythm, extract_day_rhythm
from app.stay_mobility import GraphNode, _mode_fact
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def test_v16_current_boundary_separates_real_and_non_real_statements() -> None:
    current = {
        "现在博物馆关门了": ("DISRUPTION_REPORT", "CLOSURE"),
        "火车晚点三个小时": ("DISRUPTION_REPORT", "SCHEDULE_DELAY"),
        "前面正在封路": ("DISRUPTION_REPORT", "TRAFFIC_DELAY"),
        "现在下暴雨": ("DISRUPTION_REPORT", "WEATHER"),
        "现在博物馆维修": ("DISRUPTION_REPORT", "CLOSURE"),
    }
    for message, (intent, event_type) in current.items():
        interpretation = classify_copilot_message(message)
        assert interpretation.intent == intent, message
        assert disruption_event_type(message) == event_type, message

    safe_messages = (
        "如果明天下雨会怎样？",
        "我以前遇到堵车了，类似情况怎么办？",
        "朋友说五大道关闭了，真的假的？",
        "现在路很顺畅，没有堵车",
        "我护照丢了",
        "我想少走路",
    )
    for message in safe_messages:
        assert disruption_event_type(message) is None, message
        assert classify_copilot_message(message).intent != "DISRUPTION_REPORT", message

    negated_reports = (
        "现在博物馆没有关门",
        "博物馆今天正常开放",
        "维修已经结束",
        "火车没有晚点",
        "航班没有取消",
        "道路已经恢复",
        "the museum is not closed",
        "the train is not delayed",
        "the road has reopened",
    )
    for message in negated_reports:
        assert disruption_event_type(message) is None, message

    real_reports = (
        ("现在博物馆关门了", "CLOSURE"),
        ("火车晚点三个小时", "SCHEDULE_DELAY"),
        ("航班刚刚取消", "SCHEDULE_DELAY"),
        ("前面正在封路", "TRAFFIC_DELAY"),
        ("现在下暴雨", "WEATHER"),
    )
    for message, event_type in real_reports:
        assert disruption_event_type(message) == event_type, message

    asserted_future_reports = {
        "明天的航班已经取消了": ("ASSERTED", "FUTURE", "SCHEDULE_DELAY"),
        "明天火车晚点三个小时": ("ASSERTED", "FUTURE", "SCHEDULE_DELAY"),
        "后天景区通知闭馆": ("ASSERTED", "FUTURE", "CLOSURE"),
        "tomorrow's flight has been cancelled": ("ASSERTED", "FUTURE", "SCHEDULE_DELAY"),
        "the museum is confirmed closed tomorrow": ("ASSERTED", "FUTURE", "CLOSURE"),
    }
    for message, expected in asserted_future_reports.items():
        assert _disruption_semantic_status(message) == expected[:2], message
        assert disruption_event_type(message) == expected[2], message
        assert classify_copilot_message(message).intent == "DISRUPTION_REPORT", message

    future_non_events = {
        "如果明天下雨怎么办？": ("HYPOTHETICAL", "FUTURE"),
        "明天可能下雨": ("UNVERIFIED", "FUTURE"),
        "明天会不会堵车？": ("HYPOTHETICAL", "FUTURE"),
        "听说明天博物馆可能关闭，是真的吗？": ("UNVERIFIED", "FUTURE"),
        "明天航班没有取消": ("NEGATED", "FUTURE"),
        "if it rains tomorrow, what should we do?": ("HYPOTHETICAL", "FUTURE"),
        "the train may be delayed tomorrow": ("UNVERIFIED", "FUTURE"),
        "tomorrow's flight is not cancelled": ("NEGATED", "FUTURE"),
    }
    for message, expected in future_non_events.items():
        assert _disruption_semantic_status(message) == expected, message
        assert disruption_event_type(message) is None, message

    business_preference = classify_copilot_message(
        "I am on a business trip: keep walking minimal and leave a quiet buffer before my meetings."
    )
    assert business_preference.intent == "PREFERENCE_CHANGE"
    assert disruption_event_type(
        "I am on a business trip: keep walking minimal and leave a quiet buffer before my meetings."
    ) is None


@pytest.mark.skipif(settings.ai_api_key, reason="legacy command would invoke the configured live agent")
def test_v16_legacy_command_uses_the_same_boundary() -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    safe = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "我护照丢了，请告诉我怎么办。", "requested_autonomy": "PLAN_ONLY"},
    )
    assert safe.status_code == 200, safe.text
    assert safe.json()["event_id"] is None

    real = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": "现在博物馆关门了", "requested_autonomy": "PLAN_ONLY"},
    )
    assert real.status_code == 200, real.text
    assert real.json()["event_id"]
    with SessionLocal() as session:
        events = session.scalars(select(Event).where(Event.journey_id == journey_id)).all()
        assert [item.event_type for item in events] == ["CLOSURE"]


def test_v16_day_rhythm_is_advisory_and_preserves_locked_nodes() -> None:
    rhythm = extract_day_rhythm("我们晚上很晚才睡，第二天11点才起床，不吃早餐")
    assert rhythm is not None
    assert rhythm.wake_minute == 11 * 60
    assert rhythm.bed_minute == 90
    assert rhythm.skip_breakfast is True

    nodes = [
        SimpleNamespace(
            key="arrival",
            role="ARRIVAL",
            title="Arrival",
            start_at="2026-10-10T10:00:00+08:00",
            end_at="2026-10-10T10:20:00+08:00",
            locked=True,
            metadata={},
        ),
        SimpleNamespace(
            key="early",
            role="ACTIVITY",
            title="Breakfast walk",
            start_at="2026-10-10T09:00:00+08:00",
            end_at="2026-10-10T10:30:00+08:00",
            locked=False,
            metadata={},
        ),
        SimpleNamespace(
            key="c4",
            role="ACTIVITY",
            title="Protected cruise",
            start_at="2026-10-10T10:30:00+08:00",
            end_at="2026-10-10T12:00:00+08:00",
            locked=False,
            metadata={"commitment_ids": ["c4-1"]},
        ),
        SimpleNamespace(
            key="reserved",
            role="ACTIVITY",
            title="Paid reservation",
            start_at="2026-10-10T10:30:00+08:00",
            end_at="2026-10-10T12:00:00+08:00",
            locked=True,
            metadata={"reservation_id": "reservation-1"},
        ),
        SimpleNamespace(
            key="museum",
            role="ACTIVITY",
            title="Museum",
            start_at="2026-10-10T13:00:00+08:00",
            end_at="2026-10-10T14:30:00+08:00",
            locked=False,
            metadata={},
        ),
        SimpleNamespace(
            key="night",
            role="STAY",
            title="Overnight",
            start_at="2026-10-10T22:30:00+08:00",
            end_at="2026-10-11T08:30:00+08:00",
            locked=True,
            metadata={},
        ),
        SimpleNamespace(
            key="departure",
            role="DEPARTURE",
            title="Departure",
            start_at="2026-10-11T18:00:00+08:00",
            end_at="2026-10-11T18:20:00+08:00",
            locked=False,
            metadata={},
        ),
    ]
    filtered = apply_day_rhythm(nodes, rhythm)
    keys = [item.key for item in filtered]
    assert keys == ["arrival", "c4", "reserved", "museum", "night", "departure"]
    assert filtered[1].start_at == nodes[2].start_at
    assert filtered[-1].start_at == nodes[-1].start_at
    assert filtered[-2].start_at.startswith("2026-10-11T01:30")
    assert filtered[-2].end_at.startswith("2026-10-11T11:00")


@pytest.mark.parametrize(
    ("bed", "wake", "minutes"),
    ((90, 660, 570), (23 * 60 + 30, 7 * 60 + 30, 8 * 60), (30, 8 * 60, 7 * 60 + 30)),
)
def test_v16_day_rhythm_sleep_interval_has_correct_boundaries(bed: int, wake: int, minutes: int) -> None:
    rhythm = DayRhythm(wake_minute=wake, bed_minute=bed)
    assert rhythm.overnight_minutes == minutes


def test_v16_currency_minor_units_are_shared_across_mobility_and_what_if() -> None:
    assert major_to_minor(150000, "JPY") == 150000
    assert major_to_minor(150000, "KRW") == 150000
    assert major_to_minor(150000, "VND") == 150000
    assert major_to_minor(150000, "IDR") == 150000
    assert major_to_minor(1500, "CNY") == 150000

    origin = GraphNode("a", "A", "ACTIVITY", "2026-10-10T11:00:00+09:00", "2026-10-10T12:00:00+09:00", 35.68, 139.65)
    target = GraphNode("b", "B", "ACTIVITY", "2026-10-10T13:00:00+09:00", "2026-10-10T14:00:00+09:00", 35.69, 139.66)
    fact = _mode_fact("journey", None, origin, target, "TRANSIT", "JPY")
    assert fact.cost_amount_minor is None
    assert fact.cost_status == "UNKNOWN"
    cny_fact = _mode_fact("journey", None, origin, target, "TRANSIT", "CNY")
    assert cny_fact.cost_amount_minor == 400
    assert cny_fact.cost_status == "ESTIMATED"
    assert fact.currency is None
    assert what_if_parameters("多花 5000 日元", "JPY")["extra_cost_minor"] == 5000


def test_v16_creation_suggests_rooms_but_respects_an_explicit_override() -> None:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "Room tester", "locale": "en-GB", "timezone": "Asia/Tokyo"},
    ).json()
    base = {
        "traveler_id": traveler["id"],
        "title": "Tokyo room test",
        "city_code": "JP-TOKYO",
        "timezone": "Asia/Tokyo",
        "start_at": "2026-10-10T11:00:00+09:00",
        "end_at": "2026-10-12T20:00:00+09:00",
        "budget": {"amount_minor": 150000, "currency": "JPY"},
        "stay_budget": {"amount_minor": 60000, "currency": "JPY"},
        "party_size": 5,
    }
    suggested = client.post("/api/v1/journeys", json=base)
    assert suggested.status_code == 201, suggested.text
    suggested_payload = client.get(f"/api/v1/journeys/{suggested.json()['id']}/experience").json()
    assert suggested_payload["trip"]["occupancy"] == {
        "adults": 5,
        "rooms": 3,
        "rooms_source": "SUGGESTED",
    }

    explicit = client.post(
        "/api/v1/journeys",
        json={**base, "title": "Tokyo explicit room test", "occupancy": {"adults": 5, "rooms": 1}},
    )
    assert explicit.status_code == 201, explicit.text
    explicit_payload = client.get(f"/api/v1/journeys/{explicit.json()['id']}/experience").json()
    assert explicit_payload["trip"]["occupancy"] == {
        "adults": 5,
        "rooms": 1,
        "rooms_source": "USER",
    }


def test_v16_twin_cache_lock_does_not_break_the_read_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None

        def locked_flush(*_args: object, **_kwargs: object) -> None:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("twin flush", {}, Exception("database is locked"))

        monkeypatch.setattr(session, "flush", locked_flush)
        with session.no_autoflush:
            projection = main_module._twin_snapshot(session, journey)
        assert projection["journey_id"] == journey_id
        with session.no_autoflush:
            assert session.scalar(
                select(func.count()).select_from(JourneyTwin).where(JourneyTwin.journey_id == journey_id)
            ) == 0
