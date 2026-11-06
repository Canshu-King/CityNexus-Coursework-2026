from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from demo_stage import demo_reset_headers
from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import TravelerPreferenceEvidence
from app.db.session import SessionLocal
from app.execution.traveler_twin import refresh_snapshot
from app.integrations.discovery.real_city import (
    canonical_cost_evidence,
    cost_evidence_from_tags,
    normalize_opening_hours,
)

client = TestClient(main_module.app)


def test_v7_1_trust_fixes_are_explicit() -> None:
    assert cost_evidence_from_tags({"fee": "12.5 USD"}, source="fixture")["currency"] == "USD"
    hours = normalize_opening_hours("Mo-Fr 09:00-18:00; Sa 10:00-14:00")
    assert hours["weekday_intervals"][0]["days"] == ["Mo", "Tu", "We", "Th", "Fr"]
    assert hours["weekday_intervals"][1]["days"] == ["Sa"]


def test_osm_fee_boolean_is_not_currency_and_adult_charge_is_authoritative() -> None:
    unknown = cost_evidence_from_tags({"fee": "yes"}, source="overpass")
    assert unknown == {
        "status": "UNKNOWN",
        "amount_minor": None,
        "currency": "CNY",
        "source": "overpass",
    }

    known = cost_evidence_from_tags(
        {"fee": "yes", "charge:adult": "1200 JPY"}, source="nominatim"
    )
    assert known == {
        "status": "KNOWN",
        "amount_minor": 1200,
        "currency": "JPY",
        "source": "nominatim",
    }

    free = cost_evidence_from_tags({"fee": "no"}, source="overpass")
    assert free == {
        "status": "KNOWN",
        "amount_minor": 0,
        "currency": None,
        "currency_agnostic": True,
        "evidence_basis": "OSM_FEE_NO",
        "source": "overpass",
    }
    contradictory = cost_evidence_from_tags(
        {"fee": "no", "charge:adult": "1200 JPY"}, source="overpass"
    )
    assert contradictory["amount_minor"] == 1200
    assert contradictory["currency"] == "JPY"


def test_cached_osm_fee_no_normalizes_old_zero_cny_without_promoting_bare_zero() -> None:
    normalized = canonical_cost_evidence(
        {"status": "KNOWN", "amount_minor": 0, "currency": "CNY"},
        {"fee": "no"},
    )
    assert normalized["currency"] is None
    assert normalized["currency_agnostic"] is True
    assert normalized["evidence_basis"] == "OSM_FEE_NO"

    bare_zero = canonical_cost_evidence(
        {"status": "UNKNOWN", "amount_minor": 0, "currency": "CNY"}, {}
    )
    assert bare_zero == {"status": "UNKNOWN", "amount_minor": 0, "currency": "CNY"}

    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    with SessionLocal() as session:
        session.add(
            TravelerPreferenceEvidence(
                id="expired-v8-evidence",
                traveler_id=ids["traveler_id"],
                journey_id=ids["journey_id"],
                evidence_type="BEHAVIORAL_SOFT",
                preference_dimension="indoor",
                direction="indoor",
                strength=1.0,
                confidence=1.0,
                source="TEST",
                evidence_count=4,
                explanation="expired evidence must not influence planning",
                related_ids_json="[]",
                created_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
                expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                status="ACTIVE",
            )
        )
        snapshot = refresh_snapshot(session, ids["traveler_id"])
        assert "indoor" not in {
            item["direction"] for item in json.loads(snapshot.learned_soft_preferences_json)
        }
        assert session.get(TravelerPreferenceEvidence, "expired-v8-evidence").status == "EXPIRED"
        session.commit()


def test_v8_flagship_stay_aware_multimodal_lifecycle() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    create = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": "Tianjin weekend, stay-aware recovery",
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T18:00:00+08:00",
            "end_at": "2026-10-11T15:00:00+08:00",
            "budget": {"amount_minor": 120000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 70000, "currency": "CNY"},
            "party_size": 2,
            "occupancy": {"adults": 2, "rooms": 1},
            "arrival_anchor": {
                "at": "2026-10-09T18:00:00+08:00",
                "location": "Tianjin West Station",
            },
            "departure_anchor": {
                "at": "2026-10-11T15:00:00+08:00",
                "location": "Tianjin Binhai Airport",
            },
        },
    )
    assert create.status_code == 201, create.text
    journey_id = create.json()["id"]

    captured = client.post(f"/api/v1/journeys/{journey_id}/stays/offers/capture")
    assert captured.status_code == 200, captured.text
    assert captured.json()["provenance"] == "CAPTURED_REPLAY"
    offers = captured.json()["offers"]
    assert len(offers) >= 3
    assert all(item["currency"] == "CNY" and "cancellation_policy" in item for item in offers)

    generated = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate")
    assert generated.status_code == 200, generated.text
    options = generated.json()["top_k"]
    assert len(options) >= 3
    assert all(item["hard_constraints_passed"] for item in options)
    assert all(item["stay"]["total_amount_minor"] <= 70000 for item in options[:3])
    assert all(item["days"] and item["stay"]["check_in_at"] for item in options)

    selected = client.post(f"/api/v1/journeys/{journey_id}/stay-options/{options[0]['id']}/select")
    assert selected.status_code == 200, selected.text
    assert selected.json()["protected_commitments_preserved"] is True

    compare = client.post(f"/api/v1/journeys/{journey_id}/mobility/compare", json={"mode": "AUTO"})
    assert compare.status_code == 200, compare.text
    assert compare.json()["facts"]
    assert compare.json()["costs_are_provider_evidence"] is True

    mode_switch = client.post(
        f"/api/v1/journeys/{journey_id}/stay-options/{options[0]['id']}/mode",
        json={"mode": "TRANSIT"},
    )
    assert mode_switch.status_code == 200, mode_switch.text
    assert mode_switch.json()["impact"]["mode_switch"]["cost_status"] == "ESTIMATED"
    assert mode_switch.json()["deterministic_feasibility"] == "PASSED"

    rain = client.post(
        f"/api/v1/journeys/{journey_id}/stay-options/{options[0]['id']}/weather",
        json={"state": "RAIN"},
    )
    assert rain.status_code == 200, rain.text
    assert rain.json()["recovery"] == "reduced_weather_exposure"
    assert rain.json()["protected_commitments_preserved"] is True
    assert "TRANSIT" in rain.json()["option"]["leg_modes"]

    experience = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert experience.status_code == 200, experience.text
    body = experience.json()
    assert body["trip"]["party_size"] == 2
    assert body["trip"]["arrival_anchor"]["location"] == "Tianjin West Station"
    assert body["selected_option_id"] == options[0]["id"]
    assert {item["mode"] for item in body["mobility_facts"]} == {
        "WALK",
        "BICYCLE",
        "TRANSIT",
        "AUTO",
        "RIDE_HAIL",
    }


def test_v8_autopilot_recovery_respects_autonomy_envelope() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    create = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": "Tianjin autonomous recovery",
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T18:00:00+08:00",
            "end_at": "2026-10-11T15:00:00+08:00",
            "budget": {"amount_minor": 120000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 70000, "currency": "CNY"},
            "party_size": 2,
            "occupancy": {"adults": 2, "rooms": 1},
            "arrival_anchor": {
                "at": "2026-10-09T18:00:00+08:00",
                "location": "Tianjin West Station",
            },
            "departure_anchor": {
                "at": "2026-10-11T15:00:00+08:00",
                "location": "Tianjin Binhai Airport",
            },
        },
    )
    journey_id = create.json()["id"]
    generated = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate")
    option_id = generated.json()["top_k"][0]["id"]

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
        f"/api/v1/journeys/{journey_id}/stay-options/{option_id}/autopilot/recover",
        json={"state": "RAIN"},
    )
    assert recovery.status_code == 200, recovery.text
    body = recovery.json()
    assert body["decision"] == "ALLOW_AUTO"
    assert body["executed"] is True
    assert body["after_mode"] == "TRANSIT"
    assert body["protected_commitments_preserved"] is True
    assert body["post_action_gate"] == "PASSED"

    advisory = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    create_advisory = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": advisory["traveler_id"],
            "title": "Tianjin confirmation boundary",
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T18:00:00+08:00",
            "end_at": "2026-10-11T15:00:00+08:00",
            "budget": {"amount_minor": 120000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 70000, "currency": "CNY"},
            "arrival_anchor": {
                "at": "2026-10-09T18:00:00+08:00",
                "location": "Tianjin West Station",
            },
            "departure_anchor": {
                "at": "2026-10-11T15:00:00+08:00",
                "location": "Tianjin Binhai Airport",
            },
        },
    )
    advisory_journey_id = create_advisory.json()["id"]
    advisory_option = client.post(
        f"/api/v1/journeys/{advisory_journey_id}/stay-options/generate"
    ).json()["top_k"][0]["id"]
    confirmation = client.post(
        f"/api/v1/journeys/{advisory_journey_id}/stay-options/{advisory_option}/autopilot/recover",
        json={"state": "RAIN"},
    )
    assert confirmation.status_code == 200, confirmation.text
    assert confirmation.json()["decision"] == "REQUIRE_CONFIRMATION"
    assert confirmation.json()["executed"] is False


def test_v8_natural_language_mission_compiles_stay_and_mobility_intent() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    command = (
        "I arrive in Tianjin on Friday evening and leave Sunday afternoon. Two people. "
        "Keep the hotel under 700 for two nights and avoid long walks. "
        "If it rains, use transit or a taxi. Handle the plan for me."
    )
    response = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/agent/commands",
        json={"command": command, "requested_autonomy": "EXECUTE_WITHIN_BOUNDS"},
    )
    assert response.status_code == 200, response.text
    mission = response.json()["mission"]
    assert mission["max_cost_delta_minor"] == 70000
    assert "MINIMIZE_WALKING" in mission["objective_codes"]
    assert mission["preferences"]["low_walking"] is True
    assert mission["requested_autonomy"] == "EXECUTE_WITHIN_BOUNDS"
