from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.discovery.orchestration import curate_candidates, deterministic_candidate_gate
from app.integrations.ai.responses import ResponsesResult
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def _confirm_structured(client: TestClient, journey_id: str, reservation: dict) -> dict:
    draft = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": reservation}},
    )
    assert draft.status_code == 201, draft.text
    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{draft.json()['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _reservation(role: str) -> dict:
    if role == "ARRIVAL":
        return {
            "reservation_type": "RAIL",
            "provider_key": "v133-test",
            "confirmation_ref": "V133-ARRIVAL",
            "origin": {"name": "Beijing South", "city": "Beijing"},
            "destination": {
                "name": "Tianjin West Station",
                "city": "Tianjin",
                "latitude": 39.112,
                "longitude": 117.198,
            },
            "scheduled_departure_at": "2026-10-10T07:55:00+08:00",
            "scheduled_arrival_at": "2026-10-10T09:00:00+08:00",
            "price_amount_minor": 5500,
            "currency": "CNY",
            "price_status": "KNOWN",
            "anchor_role": role,
        }
    if role == "DEPARTURE":
        return {
            "reservation_type": "RAIL",
            "provider_key": "v133-test",
            "confirmation_ref": "V133-DEPARTURE",
            "origin": {
                "name": "Tianjin West Station",
                "city": "Tianjin",
                "latitude": 39.112,
                "longitude": 117.198,
            },
            "destination": {"name": "Beijing South", "city": "Beijing"},
            "scheduled_departure_at": "2026-10-10T18:30:00+08:00",
            "scheduled_arrival_at": "2026-10-10T19:00:00+08:00",
            "price_amount_minor": 5500,
            "currency": "CNY",
            "price_status": "KNOWN",
            "anchor_role": role,
        }
    return {
        "reservation_type": "FERRY",
        "provider_key": "v133-test",
        "confirmation_ref": "V133-CRUISE",
        "origin": {"name": "Haihe Wharf", "city": "Tianjin"},
        "destination": {"name": "Haihe Cruise", "city": "Tianjin"},
        "scheduled_departure_at": "2026-10-10T15:30:00+08:00",
        "scheduled_arrival_at": "2026-10-10T16:30:00+08:00",
        "price_amount_minor": 12000,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": "MID_TRIP",
        "semantic_node_key": "place-river-cruise",
    }


class _DiscoveryDeepSeek:
    model = "deepseek-v4-flash-test"

    def complete(self, _messages, **kwargs):
        if kwargs.get("response_schema_name") == "citynexus_discovery_intent":
            return ResponsesResult(
                text=json.dumps(
                    {
                        "goal": "RAIN_FRIENDLY_CULTURE",
                        "anchor_key": "UPCOMING_COMMITMENT",
                        "time_window": {
                            "start_at": "2026-10-10T11:00:00+08:00",
                            "end_at": "2026-10-10T15:00:00+08:00",
                        },
                        "queries": [
                            {
                                "query": "museum indoor culture",
                                "categories": ["museum"],
                                "radius_m": 99999,
                                "limit": 99,
                            }
                        ],
                        "ranking_priorities": ["culture", "indoor", "low_walking"],
                        "avoid": ["long_outdoor_walk"],
                        "explanation": "Indoor culture before the protected cruise.",
                    }
                )
            )
        if kwargs.get("response_schema_name") == "citynexus_candidate_curation":
            return ResponsesResult(
                text=json.dumps(
                    {
                        "ranked": [
                            {
                                "candidate_id": "fabricated-place-id",
                                "semantic_fit": 0.99,
                                "reason": "not in provider set",
                                "soft_concerns": [],
                            }
                        ]
                    }
                )
            )
        return ResponsesResult(text="{}")


def test_candidate_curator_drops_fabricated_ids_and_deterministic_gate_is_hard() -> None:
    candidates = [
        {
            "id": "real-place-1",
            "name": "Indoor Museum",
            "category": "museum",
            "tags": ["culture", "indoor"],
            "opening_hours": {"status": "NORMALIZED_VALID", "open": "09:00", "close": "20:00"},
            "cost_evidence": {"status": "KNOWN", "amount_minor": 0, "currency": "CNY"},
        },
    ]
    curated = curate_candidates(
        candidates=candidates,
        intent={"ranking_priorities": ["culture"]},
        context={},
        client=_DiscoveryDeepSeek(),
    )
    assert curated["ranked"] == []
    assert curated["rejected_candidate_ids"] == ["fabricated-place-id"]
    gated = deterministic_candidate_gate(candidates, curated)
    assert gated["passed"] is False


def test_v133_golden_real_city_intelligence_replay_preserves_cruise(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_deepseek_client", lambda **_kwargs: _DiscoveryDeepSeek())
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    _confirm_structured(client, journey_id, _reservation("ARRIVAL"))
    _confirm_structured(client, journey_id, _reservation("MID_TRIP"))
    _confirm_structured(client, journey_id, _reservation("DEPARTURE"))

    response = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/intelligence",
        json={
            "goal": "I like culture, want low walking, and prefer something rain-friendly before my cruise.",
            "mode": "replay",
            "radius_m": 3200,
            "limit": 8,
            "candidate_count": 1,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["intent"]["provider"] == "DEEPSEEK_RESPONSES"
    assert body["intent"]["anchor_key"] == "UPCOMING_COMMITMENT"
    assert body["intent"]["queries"][0]["radius_m"] == 3200
    assert body["intent"]["queries"][0]["limit"] == 8
    assert body["replay"]["sealed"] is True
    assert body["feasibility"]["authority"] == "DETERMINISTIC_PROVIDER_FACT_GATE"
    assert body["selected_plan"]["status"] == "SELECTED"
    assert any(item["place_id"].startswith("real-place-") for item in body["selected_plan"]["activities"])
    assert any(item["place_id"] == "place-river-cruise" for item in body["selected_plan"]["activities"])
    evidence = body["selected_plan"]["mobility_evidence"]
    assert evidence["discovery"]["runtime_label"] == "SEALED_PROVIDER_REPLAY"
    assert evidence["discovery"]["weather"]["provider"] == "open-meteo"
