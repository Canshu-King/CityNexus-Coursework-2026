from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent import watch_policy as module


def _enrollment(*, interval=3600.0, last_pulse_at=None, enabled=True):
    return SimpleNamespace(
        enabled=enabled,
        interval_seconds=interval,
        last_pulse_at=last_pulse_at,
    )


def _journey():
    return SimpleNamespace(
        id="j1",
        active_plan_version_id="plan-1",
        timezone="Asia/Tokyo",
        status="ACTIVE",
    )


def _activity(start_at):
    return SimpleNamespace(id="a1", start_at=start_at, sequence_no=1)


def test_quiet_watch_preserves_configured_maximum(monkeypatch):
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "_next_activity", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(module, "_open_material_events", lambda *_a, **_k: [])

    result = module.adaptive_watch_policy(
        SimpleNamespace(),
        _journey(),
        _enrollment(interval=3600.0, last_pulse_at=now.isoformat()),
        now=now,
    )

    assert result["watch_state"] == "QUIET"
    assert result["effective_interval_seconds"] == 3600.0


def test_approaching_activity_shortens_watch_interval(monkeypatch):
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    start = now + timedelta(minutes=90)
    monkeypatch.setattr(
        module,
        "_next_activity",
        lambda *_a, **_k: (_activity(start.isoformat()), 90 * 60.0),
    )
    monkeypatch.setattr(module, "_open_material_events", lambda *_a, **_k: [])

    result = module.adaptive_watch_policy(
        SimpleNamespace(),
        _journey(),
        _enrollment(interval=3600.0, last_pulse_at=now.isoformat()),
        now=now,
    )

    assert result["watch_state"] == "APPROACHING"
    assert result["effective_interval_seconds"] == 300.0


def test_explicit_short_interval_is_never_silently_lengthened(monkeypatch):
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    start = now + timedelta(minutes=90)
    monkeypatch.setattr(
        module,
        "_next_activity",
        lambda *_a, **_k: (_activity(start.isoformat()), 90 * 60.0),
    )
    monkeypatch.setattr(module, "_open_material_events", lambda *_a, **_k: [])

    result = module.adaptive_watch_policy(
        SimpleNamespace(),
        _journey(),
        _enrollment(interval=30.0, last_pulse_at=now.isoformat()),
        now=now,
    )

    assert result["watch_state"] == "APPROACHING"
    assert result["effective_interval_seconds"] == 30.0


def test_new_material_incident_wakes_watch_immediately(monkeypatch):
    now = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    prior = now - timedelta(minutes=1)
    event = SimpleNamespace(id="e1", detected_at=now.isoformat())
    monkeypatch.setattr(module, "_next_activity", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(module, "_open_material_events", lambda *_a, **_k: [event])

    result = module.adaptive_watch_policy(
        SimpleNamespace(),
        _journey(),
        _enrollment(interval=3600.0, last_pulse_at=prior.isoformat()),
        now=now,
    )

    assert result["watch_state"] == "INCIDENT_ACTIVE"
    assert result["event_newer_than_last_pulse"] is True
    assert datetime.fromisoformat(result["next_due_at"]) == now
