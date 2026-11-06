from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import app.execution.readiness as readiness_module
from app.db.models import Journey, NotificationEvent, Traveler
from app.db.session import SessionLocal, ensure_schema
from app.execution.readiness import refresh_departure_readiness


def _journey() -> Journey:
    traveler_id = str(uuid4())
    journey_id = str(uuid4())
    now = datetime(2026, 10, 7, tzinfo=timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="R42 traveler",
                email=None,
                locale="en-US",
                timezone="UTC",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        session.add(
            Journey(
                id=journey_id,
                traveler_id=traveler_id,
                title="R42 activation",
                city_code="NARA",
                timezone="UTC",
                start_at=now,
                end_at=(datetime(2026, 10, 8, tzinfo=timezone.utc)).isoformat(),
                budget_currency="JPY",
                budget_amount_minor=100000,
                planning_mode="balanced",
                status="ACTIVE",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return Journey(id=journey_id, traveler_id=traveler_id, title="R42 activation", city_code="NARA", timezone="UTC", start_at=now, end_at="2026-10-08T00:00:00+00:00", budget_currency="JPY", budget_amount_minor=100000, planning_mode="balanced", status="ACTIVE", active_plan_version_id=None, current_activity_id=None, created_at=now, updated_at=now)


def test_departure_cue_reuses_same_band_and_changes_on_band_change(monkeypatch):
    ensure_schema()
    journey = _journey()
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    state = {"minutes": 20}

    def projection(_session, _journey, *, now=None):
        stamp = now or datetime.now(timezone.utc)
        return {
            "state": "READY_WITH_UNCERTAINTY",
            "target_activity_id": "activity-1",
            "target_title": "依水園",
            "must_leave_by": (stamp + timedelta(minutes=state["minutes"])).isoformat(),
            "blocking_reasons": [],
            "warning_reasons": [],
            "next_move": {"current_activity": {"id": "activity-1", "status": "ACTIVE", "title": "依水園"}},
        }

    monkeypatch.setattr(readiness_module, "departure_readiness_projection", projection)
    with SessionLocal() as session:
        first = refresh_departure_readiness(session, journey, now=now, refresh_offline=False)
        second = refresh_departure_readiness(session, journey, now=now, refresh_offline=False)
        state["minutes"] = -10
        third = refresh_departure_readiness(session, journey, now=now, refresh_offline=False)
        rows = list(session.query(NotificationEvent).filter_by(journey_id=journey.id).all())
        assert first["departure_activation"]["state"] == "LEAVE_SOON"
        assert second["departure_activation"]["cue"]["dedupe_key"] == first["departure_activation"]["cue"]["dedupe_key"]
        assert third["departure_activation"]["state"] == "OVERDUE"
        assert len(rows) == 2
        assert sorted(row.occurrence_count for row in rows) == [1, 2]


def test_blocked_activation_suppresses_confirm_cta(monkeypatch):
    ensure_schema()
    journey = _journey()
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(
        readiness_module,
        "departure_readiness_projection",
        lambda *_args, **_kwargs: {
            "state": "BLOCKED",
            "target_activity_id": "activity-1",
            "target_title": "依水園",
            "must_leave_by": (now + timedelta(minutes=5)).isoformat(),
            "blocking_reasons": ["CRITICAL_MATERIAL_EVENT"],
            "warning_reasons": [],
            "next_move": {"current_activity": {"id": "activity-1", "status": "ACTIVE", "title": "依水園"}},
        },
    )
    with SessionLocal() as session:
        result = refresh_departure_readiness(session, journey, now=now, refresh_offline=False)
        assert result["departure_activation"]["state"] == "BLOCKED"
        assert result["departure_activation"]["can_confirm_departure"] is False
        assert result["departure_activation"]["cue"]["eligible"] is True
