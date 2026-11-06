from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.execution import readiness as module


def _journey(status="ACTIVE"):
    return SimpleNamespace(id="j1", status=status)


def _next_move(now, *, venue="OPEN_COMPATIBLE", route="AVAILABLE", presence="LOCATION_UNAVAILABLE"):
    return {
        "next_activity": {
            "id": "a1",
            "title": "Next stop",
            "start_at": (now + timedelta(minutes=90)).isoformat(),
        },
        "current_activity": None,
        "venue_readiness": {"state": venue},
        "route_evidence": {"state": route, "degraded": route == "DEGRADED"},
        "presence": {"state": presence},
    }


def _patch_basics(monkeypatch, now, move):
    monkeypatch.setattr(module, "next_move_projection", lambda *_a, **_k: move)
    monkeypatch.setattr(module, "_target_commitments", lambda *_a, **_k: [])
    monkeypatch.setattr(module, "_next_reservation", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "_open_material_events", lambda *_a, **_k: [])


def test_location_unavailable_pretrip_is_uncertainty_not_block(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(monkeypatch, now, _next_move(now))

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["state"] == "READY_WITH_UNCERTAINTY"
    assert result["blocking_reasons"] == []
    assert "LOCATION_UNAVAILABLE" in result["warning_reasons"]


def test_closed_venue_blocks_departure(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(
        monkeypatch,
        now,
        _next_move(now, venue="CLOSED_CONFLICT", presence="NEAR_TARGET"),
    )

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["state"] == "BLOCKED"
    assert result["recommended_action"] == "DO_NOT_DEPART"
    assert "VENUE_NOT_COMPATIBLE" in result["blocking_reasons"]


def test_unknown_venue_near_departure_needs_action(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    move = _next_move(now, venue="UNKNOWN", presence="NEAR_TARGET")
    move["next_activity"]["start_at"] = (now + timedelta(minutes=45)).isoformat()
    _patch_basics(monkeypatch, now, move)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["state"] == "ACTION_NEEDED"
    assert "VENUE_NOT_VERIFIED" in result["warning_reasons"]


def test_cancelled_transport_blocks(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(monkeypatch, now, _next_move(now, presence="NEAR_TARGET"))
    reservation = SimpleNamespace(id="r1", status="CONFIRMED", realtime_status="CANCELLED")
    monkeypatch.setattr(module, "_next_reservation", lambda *_a, **_k: reservation)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["state"] == "BLOCKED"
    assert "TRANSPORT_CANCELLED" in result["blocking_reasons"]


def test_unknown_realtime_is_uncertainty_not_cancellation(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(monkeypatch, now, _next_move(now, presence="NEAR_TARGET"))
    reservation = SimpleNamespace(
        id="r1", status="CONFIRMED", realtime_status="UNKNOWN_REALTIME"
    )
    monkeypatch.setattr(module, "_next_reservation", lambda *_a, **_k: reservation)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["state"] != "BLOCKED"
    assert "TRANSPORT_REALTIME_UNCERTAIN" in result["warning_reasons"]


def test_missing_route_minutes_never_fabricates_departure_time(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(monkeypatch, now, _next_move(now, presence="NEAR_TARGET"))

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["must_leave_by"] is None
    assert result["must_leave_by_absent_reason"] == "ROUTE_EVIDENCE_DOES_NOT_EXPOSE_TRAVEL_MINUTES"


def test_aggregate_route_minutes_without_matching_edge_never_fabricates_departure_time(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    move = _next_move(now, presence="NEAR_TARGET")
    move["route_evidence"] = {
        "state": "AVAILABLE",
        "minutes": 20,
        "edge_match": False,
        "reason_code": "NO_TRAVEL_EDGE",
    }
    _patch_basics(monkeypatch, now, move)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["must_leave_by"] is None
    assert result["must_leave_by_absent_reason"] == "NO_TRAVEL_EDGE"


def test_exact_launch_edge_computes_leave_by_with_explicit_buffer(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    move = _next_move(now, presence="NEAR_TARGET")
    move["route_evidence"] = {
        "state": "AVAILABLE",
        "kind": "LAUNCH_EDGE",
        "minutes": 20,
        "edge_match": True,
        "degraded": False,
        "reason_code": "EXACT_MOBILITY_FACT_MATCH",
    }
    move["launch_origin"] = {"source": "SELECTED_STAY", "name": "Hotel"}
    move["launch_edge"] = {"state": "LAUNCH_ROUTE_READY", "duration_min": 20}
    _patch_basics(monkeypatch, now, move)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["must_leave_by"] == (now + timedelta(minutes=60)).isoformat()
    assert result["must_leave_by_absent_reason"] is None
    assert result["safety_buffer_minutes"] == 10
    assert result["launch_origin"]["source"] == "SELECTED_STAY"


def test_degraded_launch_edge_stays_uncertain_and_has_no_leave_by(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    move = _next_move(now, presence="NEAR_TARGET")
    move["route_evidence"] = {
        "state": "DEGRADED",
        "kind": "LAUNCH_EDGE",
        "minutes": 20,
        "edge_match": True,
        "degraded": True,
        "reason_code": "EXACT_MOBILITY_FACT_MATCH",
    }
    _patch_basics(monkeypatch, now, move)

    result = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)

    assert result["must_leave_by"] is None
    assert result["must_leave_by_absent_reason"] == "ROUTE_EVIDENCE_DEGRADED"


def test_readiness_fingerprint_changes_on_material_state_but_not_clock_tick(monkeypatch):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    _patch_basics(monkeypatch, now, _next_move(now, presence="NEAR_TARGET"))
    first = module.departure_readiness_projection(SimpleNamespace(), _journey(), now=now)
    later_same_band = dict(first)
    later_same_band["minutes_until_start"] = 80
    changed = dict(first)
    changed["state"] = "ACTION_NEEDED"

    assert module.readiness_fingerprint(first) == module.readiness_fingerprint(later_same_band)
    assert module.readiness_fingerprint(first) != module.readiness_fingerprint(changed)
