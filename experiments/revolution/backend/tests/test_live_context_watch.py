from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.live_context_watch import _place_receipt, classify_opening_hours, observation_is_fresh
from app.db.models import Activity, Event, Journey, Place
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers

client = TestClient(main_module.app)


def activity(start="2026-10-08T09:00:00+09:00", end="2026-10-08T10:00:00+09:00"):
    return SimpleNamespace(id="a1", start_at=start, end_at=end)


def place():
    return SimpleNamespace(id="p1", external_id="overpass:node/1", name="Exact Venue")


def test_opening_hours_open_compatible_uses_journey_timezone():
    result = classify_opening_hours(
        activity=activity(),
        place=place(),
        opening={"status": "NORMALIZED_VALID", "intervals": [{"days": ["Th"], "open": "08:00", "close": "18:00"}]},
        timezone_name="Asia/Tokyo",
    )
    assert result["status"] == "OPEN_COMPATIBLE"


def test_explicit_closed_is_material_conflict():
    result = classify_opening_hours(
        activity=activity(), place=place(), opening={"status": "CLOSED", "raw": "closed", "intervals": []}, timezone_name="Asia/Tokyo"
    )
    assert result["status"] == "CLOSED_CONFLICT"
    assert result["reason_code"] == "EXPLICIT_CLOSED_STATE"


def test_outside_verified_hours_is_conflict_but_unknown_is_not():
    outside = classify_opening_hours(
        activity=activity(), place=place(), opening={"status": "OFFICIAL_WEB_VERIFIED", "intervals": [{"days": ["Th"], "open": "11:00", "close": "18:00"}]}, timezone_name="Asia/Tokyo"
    )
    unknown = classify_opening_hours(
        activity=activity(), place=place(), opening={"status": "RAW_UNPARSED", "raw": "maybe open", "intervals": []}, timezone_name="Asia/Tokyo"
    )
    assert outside["status"] == "OUTSIDE_VERIFIED_HOURS"
    assert unknown["status"] == "UNKNOWN"


def test_activity_extending_past_close_is_outside_verified_hours():
    result = classify_opening_hours(
        activity=activity("2026-10-08T23:30:00+09:00", "2026-10-09T00:30:00+09:00"),
        place=place(),
        opening={"status": "NORMALIZED_VALID", "intervals": [{"days": ["Th"], "open": "22:00", "close": "24:00"}]},
        timezone_name="Asia/Tokyo",
    )
    assert result["status"] == "OUTSIDE_VERIFIED_HOURS"


def test_cross_midnight_opening_must_cover_full_activity_interval():
    compatible = classify_opening_hours(
        activity=activity("2026-10-08T23:30:00+09:00", "2026-10-09T00:30:00+09:00"),
        place=place(),
        opening={"status": "NORMALIZED_VALID", "intervals": [{"days": ["Th"], "open": "22:00", "close": "02:00"}]},
        timezone_name="Asia/Tokyo",
    )
    partial = classify_opening_hours(
        activity=activity("2026-10-08T15:00:00+09:00", "2026-10-08T16:00:00+09:00"),
        place=place(),
        opening={"status": "NORMALIZED_VALID", "intervals": [{"days": ["Th"], "open": "10:00", "close": "15:30"}]},
        timezone_name="Asia/Tokyo",
    )
    assert compatible["status"] == "OPEN_COMPATIBLE"
    assert partial["status"] == "OUTSIDE_VERIFIED_HOURS"



def test_after_midnight_activity_can_use_previous_days_overnight_opening():
    result = classify_opening_hours(
        activity=activity("2026-10-09T00:30:00+09:00", "2026-10-09T01:30:00+09:00"),
        place=place(),
        opening={
            "status": "NORMALIZED_VALID",
            "intervals": [{"days": ["Th"], "open": "22:00", "close": "02:00"}],
        },
        timezone_name="Asia/Tokyo",
    )
    assert result["status"] == "OPEN_COMPATIBLE"
    assert result["verified_opening_interval"]["opening_day"] == "2026-10-08"

def test_stale_observation_cannot_override_newer_evidence():
    assert observation_is_fresh("2026-10-08T01:00:00+00:00", "2026-10-08T00:59:59+00:00")
    assert not observation_is_fresh("2026-10-08T00:59:59+00:00", "2026-10-08T01:00:00+00:00")
    assert not observation_is_fresh("2026-10-08T01:00:00", "2026-10-08T00:59:59+00:00")


def test_receipt_binding_requires_exact_place_and_provider_entity():
    receipt = {"places": [{"place_id": "p1", "provider_entity": "overpass:node/1"}]}
    assert (
        _place_receipt(receipt, "p1", "overpass:node/1")["provider_entity"]
        == "overpass:node/1"
    )
    assert _place_receipt(receipt, "p2", "overpass:node/1") is None
    assert _place_receipt(receipt, "p1", "overpass:node/999") is None


def test_missing_exact_receipt_contributes_to_unknown_summary(monkeypatch):
    from app import live_context_watch as module

    fake_activity = SimpleNamespace(
        id="a-missing",
        plan_version_id="plan-1",
        sequence_no=1,
        status="PLANNED",
        place_id="p-missing",
        start_at="2026-10-08T09:00:00+09:00",
        end_at="2026-10-08T10:00:00+09:00",
    )
    fake_journey = SimpleNamespace(
        id="j1",
        active_plan_version_id="plan-1",
        timezone="Asia/Tokyo",
    )

    class _Scalars:
        def all(self):
            return [fake_activity]

    class _Session:
        def scalars(self, *_args, **_kwargs):
            return _Scalars()

        def get(self, model, key):
            if model is module.Place and key == "p-missing":
                return SimpleNamespace(
                    id="p-missing",
                    external_id="overpass:node/1",
                    metadata_json="{}",
                )
            return None

        def flush(self):
            return None

    monkeypatch.setattr(
        module,
        "acquire_place_facts",
        lambda *_args, **_kwargs: {
            "places": [],
            "degraded_providers": [],
        },
    )

    result = module.run_live_context_watch(_Session(), fake_journey)

    assert result["status"] == "UNKNOWN"
    assert result["material_conflicts"] == []
    assert result["unknown_or_degraded"][0]["reason_code"] == "EXACT_PLACE_EVIDENCE_MISSING"


def test_live_incident_is_refreshed_once_unknown_does_not_clear_and_compatible_resolves(monkeypatch):
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        activity = session.scalar(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id, Activity.place_id.is_not(None))
            .order_by(Activity.sequence_no)
        )
        place = session.get(Place, activity.place_id)
        place.metadata_json = "{}"
        session.commit()
        activity_id, place_id, provider_entity = activity.id, place.id, place.external_id

    state = {"opening": {"status": "CLOSED", "raw": "closed", "intervals": []}, "degraded": False}

    def acquire(_session, _journey, *, place_ids=None):
        return {
            "places": [
                {
                    "place_id": place_id,
                    "provider_entity": provider_entity,
                    "identity_evidence": {"exact_ref": provider_entity},
                    "opening_hours": state["opening"],
                    "source_binding": None,
                }
            ],
            "degraded_providers": ["overpass"] if state["degraded"] else [],
        }

    from app import live_context_watch as module
    monkeypatch.setattr(module, "acquire_place_facts", acquire)

    def watch():
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            result = module.run_live_context_watch(session, journey)
            session.commit()
            return result

    first = watch()
    assert first["status"] == "MATERIAL_CONFLICT"
    event_id = first["created_recovery_event_ids"][0]
    second = watch()
    assert second["status"] == "MATERIAL_CONFLICT"
    assert second["created_recovery_event_ids"] == []
    assert second["recovery_event_ids"] == [event_id]
    with SessionLocal() as session:
        assert len(session.scalars(select(Event).where(Event.journey_id == journey_id)).all()) == 1

    state["degraded"] = True
    degraded = watch()
    assert degraded["status"] == "PROVIDER_DEGRADED"
    assert degraded["resolved_recovery_event_ids"] == []

    state["degraded"] = False
    state["opening"] = {"status": "NORMALIZED_VALID", "intervals": [{"days": ["*"], "open": "00:00", "close": "24:00"}]}
    resolved = watch()
    assert resolved["status"] == "UNKNOWN"
    assert resolved["resolved_recovery_event_ids"] == [event_id]
    with SessionLocal() as session:
        event = session.get(Event, event_id)
        assert event.status == "RESOLVED"
