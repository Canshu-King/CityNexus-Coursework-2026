from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.departure_activation import (
    departure_activation_projection,
    departure_cue_fingerprint,
)


def _readiness(now, *, minutes=40, state="READY"):
    return {
        "state": state,
        "must_leave_by": (now + timedelta(minutes=minutes)).isoformat(),
        "blocking_reasons": [],
    }


def test_not_due_band():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=45),
        activity_status="ACTIVE",
        now=now,
    )
    assert result["state"] == "NOT_DUE"
    assert result["can_confirm_departure"] is True


def test_leave_soon_band():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=20),
        activity_status="ACTIVE",
        now=now,
    )
    assert result["state"] == "LEAVE_SOON"
    assert result["recommended_action"] == "CONFIRM_WHEN_LEAVING"


def test_due_now_band():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=5),
        activity_status="ACTIVE",
        now=now,
    )
    assert result["state"] == "DUE_NOW"
    assert result["recommended_action"] == "CONFIRM_DEPARTURE"


def test_overdue_clock_does_not_claim_execution():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=-12),
        activity_status="ACTIVE",
        now=now,
    )
    assert result["state"] == "OVERDUE"
    assert result["can_confirm_departure"] is True
    assert result["authority_required"] == "OPERATOR_CONFIRMED_EXECUTION"


def test_blocked_readiness_has_no_normal_leave_cta():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=5, state="BLOCKED"),
        activity_status="ACTIVE",
        now=now,
    )
    assert result["state"] == "BLOCKED"
    assert result["can_confirm_departure"] is False
    assert result["recommended_action"] == "CHECK_BLOCKER"


def test_en_route_suppresses_predeparture_activation():
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    result = departure_activation_projection(
        _readiness(now, minutes=-1),
        activity_status="EN_ROUTE",
        now=now,
    )
    assert result["state"] == "ALREADY_EN_ROUTE"
    assert result["can_confirm_departure"] is False


def test_same_activation_band_has_stable_cue_identity():
    first = departure_cue_fingerprint(
        journey_id="j1",
        activity_id="a1",
        activation_state="DUE_NOW",
        readiness_state="READY_WITH_UNCERTAINTY",
        blocker_codes=[],
    )
    second = departure_cue_fingerprint(
        journey_id="j1",
        activity_id="a1",
        activation_state="due_now",
        readiness_state="ready_with_uncertainty",
        blocker_codes=[],
    )
    assert first == second


def test_material_band_change_changes_cue_identity():
    due = departure_cue_fingerprint(
        journey_id="j1",
        activity_id="a1",
        activation_state="DUE_NOW",
        readiness_state="READY",
    )
    overdue = departure_cue_fingerprint(
        journey_id="j1",
        activity_id="a1",
        activation_state="OVERDUE",
        readiness_state="READY",
    )
    assert due != overdue
