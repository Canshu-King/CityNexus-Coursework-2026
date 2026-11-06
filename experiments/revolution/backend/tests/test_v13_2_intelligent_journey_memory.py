from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.ai.responses import ResponsesResult
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


class _SemanticDeepSeek:
    model = "deepseek-v13.2-test"

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def complete(self, messages, **_kwargs):
        user_content = messages[1]["content"]
        text = user_content if isinstance(user_content, str) else json.dumps(user_content)
        self.contexts.append(text)
        artifact_text = text.split("Artifact text:\n", 1)[-1]
        if "ambiguous" in artifact_text.lower():
            result = {
                "reservation_type": "RAIL",
                "confirmation_ref": "V132-AMBIGUOUS",
                "origin": {"name": "Beijing South", "city": "Beijing"},
                "destination": {"name": "Tianjin West", "city": "Tianjin", "latitude": 39.112, "longitude": 117.198},
                "scheduled_departure_at": "2026-10-10T08:00:00+08:00",
                "scheduled_arrival_at": "2026-10-10T09:00:00+08:00",
                "price_amount_minor": 5500,
                "currency": "CNY",
                "anchor_role": "UNKNOWN",
                "anchor_role_confidence": 0.41,
                "payment_status": "PAID",
                "refundability": "UNKNOWN",
                "suggested_commitment_level": "C4_ANCHORED",
                "semantic_target": {"kind": "STRUCTURAL_ANCHOR", "query": "unknown", "confidence": 0.41},
                "candidate_journey_match": {"confidence": 0.91, "reason": "ambiguous test"},
                "uncertain_fields": ["anchor_role"],
                "reasoning_summary": "The ticket timing is clear but its Journey role is ambiguous.",
                "field_provenance": {"anchor_role": {"source": "AI", "status": "INFERRED"}},
            }
        elif "cruise" in artifact_text.lower():
            result = {
                "reservation_type": "FERRY",
                "confirmation_ref": "V132-CRUISE",
                "origin": {"name": "Haihe Wharf", "city": "Tianjin"},
                "destination": {"name": "Haihe Cruise", "city": "Tianjin"},
                "scheduled_departure_at": "2026-10-10T15:30:00+08:00",
                "scheduled_arrival_at": "2026-10-10T16:30:00+08:00",
                "price_amount_minor": 12000,
                "currency": "CNY",
                "anchor_role": "MID_TRIP",
                "anchor_role_confidence": 0.98,
                "payment_status": "PAID",
                "refundability": "REFUNDABLE",
                "suggested_commitment_level": "C4_ANCHORED",
                "semantic_target": {
                    "kind": "ACTIVITY_OR_PLACE",
                    "query": "Haihe cruise",
                    "candidate_key": "place-river-cruise",
                    "confidence": 0.96,
                },
                "candidate_journey_match": {"confidence": 0.98, "reason": "The cruise is inside the Journey window."},
                "uncertain_fields": [],
                "reasoning_summary": "This paid Haihe cruise is a protected mid-trip activity.",
                "field_provenance": {"anchor_role": {"source": "AI", "status": "INFERRED"}},
            }
        elif "return" in artifact_text.lower() or "depart" in artifact_text.lower():
            result = {
                "reservation_type": "RAIL",
                "confirmation_ref": "V132-DEPARTURE",
                "origin": {"name": "Tianjin West", "city": "Tianjin", "latitude": 39.112, "longitude": 117.198},
                "destination": {"name": "Beijing South", "city": "Beijing"},
                "scheduled_departure_at": "2026-10-10T18:30:00+08:00",
                "scheduled_arrival_at": "2026-10-10T19:00:00+08:00",
                "price_amount_minor": 5500,
                "currency": "CNY",
                "anchor_role": "DEPARTURE",
                "anchor_role_confidence": 0.99,
                "payment_status": "PAID",
                "refundability": "UNKNOWN",
                "suggested_commitment_level": "C4_ANCHORED",
                "semantic_target": {"kind": "STRUCTURAL_ANCHOR", "query": "departure", "confidence": 0.99},
                "candidate_journey_match": {"confidence": 0.99, "reason": "The departure matches the Journey end."},
                "uncertain_fields": [],
                "reasoning_summary": "This return rail ticket is the Journey departure.",
                "field_provenance": {"anchor_role": {"source": "AI", "status": "INFERRED"}},
            }
        else:
            result = {
                "reservation_type": "RAIL",
                "confirmation_ref": "V132-ARRIVAL",
                "origin": {"name": "Beijing South", "city": "Beijing"},
                "destination": {"name": "Tianjin West", "city": "Tianjin", "latitude": 39.112, "longitude": 117.198},
                "scheduled_departure_at": "2026-10-10T08:00:00+08:00",
                "scheduled_arrival_at": "2026-10-10T09:00:00+08:00",
                "price_amount_minor": 5500,
                "currency": "CNY",
                "anchor_role": "ARRIVAL",
                "anchor_role_confidence": 0.99,
                "payment_status": "PAID",
                "refundability": "UNKNOWN",
                "suggested_commitment_level": "C4_ANCHORED",
                "semantic_target": {"kind": "STRUCTURAL_ANCHOR", "query": "arrival", "confidence": 0.99},
                "candidate_journey_match": {"confidence": 0.99, "reason": "The destination and arrival time match the Journey start."},
                "uncertain_fields": [],
                "reasoning_summary": "This inbound rail ticket enters Tianjin at the Journey start.",
                "field_provenance": {"anchor_role": {"source": "AI", "status": "INFERRED"}},
            }
        return ResponsesResult(text=json.dumps(result))


def test_v132_interpreter_carries_role_confidence_and_server_context(monkeypatch) -> None:
    client = TestClient(main_module.app)
    fake = _SemanticDeepSeek()
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/text-extract",
        json={"text": "Inbound rail G132 arrives Tianjin West at the Journey start."},
    )
    assert response.status_code == 201, response.text
    draft = response.json()["draft"]
    assert draft["interpretation"]["anchor_role"] == "ARRIVAL"
    assert draft["interpretation"]["anchor_role_confidence"] == 0.99
    assert draft["smart_intake"]["decision"] == "APPLY_WITH_NOTICE"
    assert "city_code" in fake.contexts[0]

    applied = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/smart-apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "APPLIED"


def test_v132_unknown_role_requires_review_then_user_patch_can_apply(monkeypatch) -> None:
    client = TestClient(main_module.app)
    fake = _SemanticDeepSeek()
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    extracted = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/text-extract",
        json={"text": "ambiguous rail artifact"},
    )
    draft = extracted.json()["draft"]
    assert "anchor_role" in draft["unresolved_fields"]
    assert draft["smart_intake"]["decision"] == "NEEDS_REVIEW"
    blocked = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/smart-apply")
    assert blocked.json()["status"] == "NEEDS_REVIEW"

    patched = client.patch(
        f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}",
        json={"anchor_role": "ARRIVAL"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["unresolved_fields"] == []
    applied = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/smart-apply")
    assert applied.json()["status"] == "APPLIED"


def test_v132_golden_three_artifact_memory_to_authoritative_compose(monkeypatch) -> None:
    client = TestClient(main_module.app)
    fake = _SemanticDeepSeek()
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    drafts = []
    for text in (
        "Inbound rail ticket into Tianjin",
        "Paid Haihe cruise confirmation",
        "Return rail departure from Tianjin",
    ):
        extracted = client.post(
            f"/api/v1/journeys/{journey_id}/inbox/text-extract",
            json={"text": text},
        )
        assert extracted.status_code == 201, extracted.text
        draft = extracted.json()["draft"]
        applied = client.post(
            f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/smart-apply"
        )
        assert applied.status_code == 200 and applied.json()["status"] == "APPLIED", applied.text
        drafts.append(applied.json())

    memory = client.get(f"/api/v1/journeys/{journey_id}/memory")
    assert memory.status_code == 200, memory.text
    assert [item["role"] for item in memory.json()["items"]] == [
        "ARRIVAL",
        "MID_TRIP",
        "DEPARTURE",
    ]

    departure_reservation = next(
        item["reservation"]["id"]
        for item in drafts
        if item["commitment"]["policy"]["anchor_role"] == "DEPARTURE"
    )
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={
            "place_ids": ["place-city-park", "place-river-cruise"],
            "candidate_count": 1,
            "reservation_id": departure_reservation,
        },
    )
    assert composed.status_code == 201, composed.text
    plan = composed.json()["top_k"][0]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
