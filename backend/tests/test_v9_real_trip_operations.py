from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Journey, JourneyLeg, JourneyTwin, PlanVersion
from app.db.session import SessionLocal
from app.integrations.china.amap import gcj02_to_wgs84, provider_for_region, wgs84_to_gcj02
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

_SIMULATOR_SPEC = importlib.util.spec_from_file_location(
    "edge_companion_simulator",
    Path(__file__).resolve().parents[2] / "tools" / "edge_companion_simulator.py",
)
assert _SIMULATOR_SPEC and _SIMULATOR_SPEC.loader
_SIMULATOR = importlib.util.module_from_spec(_SIMULATOR_SPEC)
_SIMULATOR_SPEC.loader.exec_module(_SIMULATOR)
parse_pic_message = _SIMULATOR.parse_pic_message
snapshot_to_uart = _SIMULATOR.snapshot_to_uart

client = TestClient(main_module.app)


def _create_trip(city_code: str = "CN-TIANJIN") -> tuple[dict[str, str], str]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    city = "Tianjin" if city_code == "CN-TIANJIN" else "Beijing"
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": f"{city} real-world operations",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T08:00:00+08:00",
            "end_at": "2026-10-11T15:00:00+08:00",
            "budget": {"amount_minor": 120000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 70000, "currency": "CNY"},
            "party_size": 2,
            "occupancy": {"adults": 2, "rooms": 1},
            "arrival_anchor": {
                "at": "2026-10-09T08:00:00+08:00",
                "location": f"{city} West Station",
            },
            "departure_anchor": {
                "at": "2026-10-11T15:00:00+08:00",
                "location": f"{city} Airport",
            },
        },
    )
    assert response.status_code == 201, response.text
    return ids, response.json()["id"]


def _import_confirmed_arrival(journey_id: str) -> dict:
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={
            "artifact_type": "STRUCTURED_JSON",
            "payload": {
                "reservation": {
                    "reservation_type": "RAIL",
                    "provider_key": "user-confirmed-rail",
                    "confirmation_ref": "G12345-V9",
                    "origin": {"name": "Shanghai Hongqiao"},
                    "destination": {
                        "name": "Tianjin West Station",
                        "latitude": 39.1366,
                        "longitude": 117.1696,
                    },
                    "scheduled_departure_at": "2026-10-09T03:30:00+08:00",
                    "scheduled_arrival_at": "2026-10-09T08:00:00+08:00",
                    "price_amount_minor": 55300,
                    "currency": "CNY",
                    "price_status": "KNOWN",
                }
            },
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["status"] == "READY_FOR_CONFIRMATION"
    confirmed = client.post(f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["commitment"]["level"] == "C4_Anchored"
    return confirmed.json()["reservation"]


def test_v9_flagship_real_world_trip_operations_e2e() -> None:
    ids, journey_id = _create_trip()
    reservation = _import_confirmed_arrival(journey_id)

    luggage = client.put(
        f"/api/v1/journeys/{journey_id}/operations/luggage",
        json={
            "baggage_present": True,
            "baggage_size_class": "MEDIUM",
            "luggage_drop_required": True,
        },
    )
    assert luggage.status_code == 200, luggage.text

    generated = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate")
    assert generated.status_code == 200, generated.text
    option = generated.json()["top_k"][0]
    assert all("from_key" in item and "to_key" in item for item in option["leg_mode_map"])
    selected = client.post(f"/api/v1/journeys/{journey_id}/stay-options/{option['id']}/select")
    assert selected.status_code == 200, selected.text
    first_plan_id = selected.json()["plan_version_id"]
    assert selected.json()["deterministic_feasibility"] == "PASSED"
    selected_experience = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
    assert any(item["protected"] for item in selected_experience["active_plan"]["activities"])

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == first_plan_id)
            .order_by(Activity.sequence_no)
        ).all()
        legs = session.scalars(
            select(JourneyLeg)
            .where(JourneyLeg.plan_version_id == first_plan_id)
            .order_by(JourneyLeg.sequence_no)
        ).all()
        roles = [json.loads(item.metadata_json)["role"] for item in activities]
        assert roles.index("LUGGAGE_DROP") < roles.index("ACTIVITY") < roles.index("CHECK_IN")
        assert len(legs) == len(activities) - 1
        assert all(json.loads(item.geometry_json)["geometry"]["coordinates"] for item in legs)
        assert all(item.crs == "WGS84" and item.provider_key for item in legs)
        assert all(
            not (item.cost_status == "UNKNOWN" and item.cost_amount_minor == 0) for item in legs
        )
        twin = session.get(JourneyTwin, journey_id)
        assert twin is not None and twin.active_plan_version_id == first_plan_id

    policy = client.put(
        f"/api/v1/travelers/{ids['traveler_id']}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 5000,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 20,
            "allowed_activity_types": ["transport"],
            "allowed_booking_types": [],
            "protected_commitment_levels": ["C3_Guaranteed", "C4_Anchored"],
            "allow_auto_commitment_management": False,
            "allow_auto_when_provider_degraded": False,
            "allow_auto_on_critical_event": False,
        },
    )
    assert policy.status_code == 200, policy.text
    recovery = client.post(
        f"/api/v1/journeys/{journey_id}/stay-options/{option['id']}/autopilot/recover",
        json={"state": "RAIN"},
    )
    assert recovery.status_code == 200, recovery.text
    recovered = recovery.json()
    assert recovered["decision"] == "ALLOW_AUTO"
    assert recovered["executed"] is True
    assert recovered["post_action_gate"] == "PASSED"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.active_plan_version_id != first_plan_id
        first_plan = session.get(PlanVersion, first_plan_id)
        assert first_plan is not None and first_plan.status == "SUPERSEDED"
        twin = session.get(JourneyTwin, journey_id)
        assert twin is not None and twin.active_plan_version_id == journey.active_plan_version_id
        active_plan_id = journey.active_plan_version_id

    offline = client.get(f"/api/v1/journeys/{journey_id}/offline-snapshot")
    assert offline.status_code == 200, offline.text
    snapshot = offline.json()["snapshot"]
    assert snapshot["active_plan_version_id"] == active_plan_id
    assert snapshot["read_only"] is True and snapshot["financial_actions_allowed"] is False
    assert snapshot["reservations"][0]["confirmation_ref"] == reservation["confirmation_ref"]

    device = client.post(
        f"/api/v1/journeys/{journey_id}/edge/devices",
        json={"display_name": "PIC16F877A companion"},
    )
    assert device.status_code == 201, device.text
    edge = device.json()
    headers = {"X-Edge-Token": edge["device_token"]}
    edge_snapshot = client.get(
        f"/api/v1/edge/devices/{edge['device_id']}/journey-snapshot", headers=headers
    )
    assert edge_snapshot.status_code == 200, edge_snapshot.text
    assert edge_snapshot.json()["active_plan_version_id"] == active_plan_id
    action = client.post(
        f"/api/v1/edge/devices/{edge['device_id']}/actions",
        headers=headers,
        json={"action": "ACKNOWLEDGE", "payload": {}},
    )
    assert action.status_code == 201, action.text
    forbidden = client.post(
        f"/api/v1/edge/devices/{edge['device_id']}/actions",
        headers=headers,
        json={"action": "CREATE_BOOKING", "payload": {}},
    )
    assert forbidden.status_code in {403, 422}
    observation = client.post(
        f"/api/v1/edge/devices/{edge['device_id']}/observations",
        headers=headers,
        json={"observation_type": "RAIN", "payload": {"pressed": True}},
    )
    assert observation.status_code == 201, observation.text
    wire = snapshot_to_uart(edge_snapshot.json())
    assert "NOW|" in wire and "NEXT|" in wire and "MODE|" in wire and "C4|" in wire
    kind, payload = parse_pic_message("ACK|REPLAN")
    assert kind == "action" and payload["action"] == "ACKNOWLEDGE"
    notifications = client.get(f"/api/v1/journeys/{journey_id}/notifications").json()
    assert {item["kind"] for item in notifications["notifications"]} >= {
        "RESERVATION_CONFIRMED",
        "AUTOPILOT_PLAN_CHANGED",
    }


def test_v9_generic_city_graph_unknown_cost_and_expiry_gates() -> None:
    _, journey_id = _create_trip("CN-BEIJING")
    generated = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate")
    assert generated.status_code == 200, generated.text
    option = generated.json()["top_k"][0]
    experience = client.get(f"/api/v1/journeys/{journey_id}/experience").json()
    assert "Italian Style Town" not in json.dumps(experience, ensure_ascii=False)
    assert "Temple of Heaven" in json.dumps(experience, ensure_ascii=False)
    assert "RIDE_HAIL" not in option["leg_modes"]
    unknown_paid = client.post(
        f"/api/v1/journeys/{journey_id}/stay-options/{option['id']}/mode",
        json={"mode": "RIDE_HAIL"},
    )
    assert unknown_paid.status_code == 409, unknown_paid.text

    with SessionLocal() as session:
        option_row = session.get(main_module.StayJourneyOption, option["id"])
        offer = session.get(
            main_module.AccommodationOfferSnapshot, option_row.accommodation_offer_id
        )
        offer.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        session.commit()
    expired = client.post(f"/api/v1/journeys/{journey_id}/stay-options/{option['id']}/select")
    assert expired.status_code == 422, expired.text
    assert "STAY_QUOTE_EXPIRED" in expired.text


def test_v9_ambiguous_import_provider_truth_and_coordinate_round_trip() -> None:
    _, journey_id = _create_trip()
    draft = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "TEXT", "payload": {"text": "Train to Tianjin next Friday"}},
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["status"] == "NEEDS_REVIEW"
    blocked = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft.json()['id']}/confirm")
    assert blocked.status_code == 409, blocked.text

    providers = client.get("/api/v1/providers/v9")
    assert providers.status_code == 200
    assert providers.json()["amap"]["status"] == "NOT_CONFIGURED"
    assert providers.json()["transit_realtime"]["absence_semantics"] == "UNKNOWN_REALTIME"
    assert provider_for_region("CN-TIANJIN") == "amap-web-service"
    assert provider_for_region("US-NYC") == "global-provider-mesh"
    source = (117.2000, 39.1333)
    converted = wgs84_to_gcj02(*source)
    restored = gcj02_to_wgs84(*converted)
    assert abs(restored[0] - source[0]) < 0.00002
    assert abs(restored[1] - source[1]) < 0.00002


def test_v9_long_distance_freshness_alert_and_notification_deduplication() -> None:
    ids, journey_id = _create_trip()
    reservation = _import_confirmed_arrival(journey_id)
    captured = datetime.now(timezone.utc).isoformat()
    update = {
        "estimated_arrival_at": "2026-10-09T08:25:00+08:00",
        "status": "DELAYED",
        "provider_key": "captured-rail-status",
        "captured_at": captured,
        "service_alert": "Signal fault; platform may change",
    }
    first = client.post(
        f"/api/v1/journeys/{journey_id}/reservations/{reservation['id']}/schedule-change",
        json=update,
    )
    assert first.status_code == 200, first.text
    assert first.json()["reservation"]["realtime_status"] == "DELAYED"
    assert first.json()["guardian_wakeup"] is True
    assert first.json()["journey_risk"] == "AT_RISK"
    assert "SERVICE_ALERT" in first.json()["risk_reason_codes"]
    duplicate = client.post(
        f"/api/v1/journeys/{journey_id}/reservations/{reservation['id']}/schedule-change",
        json=update,
    )
    assert duplicate.status_code == 200, duplicate.text
    notifications = client.get(f"/api/v1/journeys/{journey_id}/notifications").json()[
        "notifications"
    ]
    matching = [item for item in notifications if item["kind"] == "SCHEDULE_CHANGE"]
    assert len(matching) == 1 and matching[0]["occurrence_count"] == 2

    stale = client.post(
        f"/api/v1/journeys/{journey_id}/reservations/{reservation['id']}/schedule-change",
        json={
            **update,
            "captured_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        },
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["reservation"]["realtime_status"] == "STALE_REALTIME"

    location = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        json={
            "latitude": 39.1366,
            "longitude": 117.1696,
            "accuracy_m": 12.5,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert location.status_code == 201, location.text
    assert location.json()["accuracy_m"] == 12.5
    assert location.json()["background_tracking_claimed"] is False

    subscription = {
        "endpoint": "https://push.example.test/citynexus-device",
        "keys": {"p256dh": "captured-public-key", "auth": "captured-auth"},
    }
    push_one = client.post(
        f"/api/v1/travelers/{ids['traveler_id']}/push-subscriptions",
        json=subscription,
    )
    push_two = client.post(
        f"/api/v1/travelers/{ids['traveler_id']}/push-subscriptions",
        json=subscription,
    )
    assert push_one.status_code == 201 and push_two.status_code == 201
    assert push_one.json()["id"] == push_two.json()["id"]
