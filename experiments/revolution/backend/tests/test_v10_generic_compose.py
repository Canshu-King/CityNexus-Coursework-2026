from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import Place
from app.services import _coursework_facts_cover, _strategy_place_score
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def test_real_provider_strategy_signal_prefers_verified_place_over_id_order():
    verified = Place(
        id="real-place-z-verified",
        provider_key="overpass",
        external_id="overpass:way/243874128",
        name="Seven Star Park",
        category="attraction",
        opening_hours_json='{"status":"NORMALIZED_VALID","open":"06:00","close":"19:30"}',
        metadata_json='{"cost_evidence":{"status":"VERIFIED_PROVIDER","amount_minor":5500}}',
    )
    unknown = Place(
        id="real-place-a-unknown",
        provider_key="overpass",
        external_id="overpass:way/188580617",
        name="Diecai Hill Park",
        category="park",
        opening_hours_json='{"status":"UNKNOWN"}',
        metadata_json='{"cost_evidence":{"status":"UNKNOWN","amount_minor":null}}',
    )

    assert _strategy_place_score(verified, "balanced")[1] == 2
    assert _strategy_place_score(unknown, "balanced")[1] == 0


def test_coursework_facts_can_mix_provider_truth_and_agent_estimate():
    from types import SimpleNamespace
    import json

    verified = SimpleNamespace(
        id="verified-place",
        opening_hours_json=json.dumps({"status": "NORMALIZED_VALID", "open": "08:00", "close": "18:00"}),
        metadata_json=json.dumps(
            {"cost_evidence": {"status": "VERIFIED_PROVIDER", "amount_minor": 5500}}
        ),
        provider_key="overpass",
    )
    estimated = SimpleNamespace(
        id="estimated-place",
        opening_hours_json=json.dumps({"status": "UNKNOWN"}),
        metadata_json=json.dumps(
            {
                "cost_evidence": {"status": "UNKNOWN", "amount_minor": None},
                "strategy_estimate": {
                    "status": "ESTIMATED_AGENT",
                    "opening_hours": {"open_minutes": 480, "close_minutes": 1080},
                    "cost_evidence": {"status": "ESTIMATED_AGENT", "amount_minor": 3200},
                },
            }
        ),
        provider_key="overpass",
    )

    assert _coursework_facts_cover(verified)
    assert _coursework_facts_cover(estimated)


def test_v10_generic_compose_uses_explicit_pois_and_gate() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": "Explicit POI composition",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T08:00:00+08:00",
            "end_at": "2026-10-09T20:00:00+08:00",
            "budget": {"amount_minor": 30000, "currency": "CNY"},
        },
    )
    assert journey.status_code == 201, journey.text
    journey_id = journey.json()["id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={
            "candidate_count": 1,
            "place_ids": ["place-old-town", "place-riverside-museum", "place-city-park"],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()[0]
    assert plan["activities"]
    assert [item["place_id"] for item in plan["activities"]] == [
        "place-old-town",
        "place-riverside-museum",
        "place-city-park",
    ]
    assert plan["score_breakdown"]["total"] > 0


def test_v10_generic_compose_score_is_bound_to_measured_strategy_provenance() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": "室内博物馆与历史文化",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T08:00:00+08:00",
            "end_at": "2026-10-09T20:00:00+08:00",
            "budget": {"amount_minor": 30000, "currency": "CNY"},
        },
    )
    assert journey.status_code == 201, journey.text

    response = client.post(
        f"/api/v1/journeys/{journey.json()['id']}/plans/generate",
        json={
            "candidate_count": 1,
            "place_ids": ["place-old-town", "place-riverside-museum", "place-city-park"],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()[0]
    provenance = plan["explanation"]["score_provenance"]

    assert plan["explanation"]["strategy_profile"] == "indoor"
    assert plan["score_breakdown"]["mobility_fit"] == provenance["metric_values"]["mobility_fit"]
    assert plan["score_breakdown"]["preference_fit"] == provenance["metric_values"]["preference_fit"]
    assert provenance["score_sources"]["mobility_fit"] == "LIVE measured travel minutes"
    assert provenance["score_sources"]["budget_fit"] == "verified activity cost / journey budget"
    assert provenance["verified_cost_count"] == len(plan["activities"])
