from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.ai.responses import DeepSeekResponsesClient, ResponsesResult
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


def test_responses_client_can_request_json_schema_output() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp-schema",
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"role":"ARRIVAL"}'}],
                    }
                ],
            },
        )

    schema = {
        "type": "object",
        "properties": {"role": {"type": "string"}},
        "required": ["role"],
        "additionalProperties": False,
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        result = DeepSeekResponsesClient(
            api_key="test-key",
            client=transport,
            base_url="https://example.test/responses",
        ).complete(
            [{"role": "user", "content": "return json"}],
            response_schema=schema,
            response_schema_name="test_schema",
        )
    assert json.loads(result.text) == {"role": "ARRIVAL"}
    assert requests[0]["text"]["format"] == {
        "type": "json_schema",
        "name": "test_schema",
        "schema": schema,
    }


def test_vision_client_factory_uses_dedicated_vision_model(monkeypatch) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(main_module, "DeepSeekResponsesClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(
            ai_api_key="test-key",
            ai_model="deepseek-v4-flash",
            ai_vision_model="deepseek-v4-flash-vision-exp",
            deepseek_api_url="https://api.deepseek.com/responses",
            deepseek_reasoning_effort="high",
            impact_timeout_seconds=10.0,
        ),
    )
    main_module._deepseek_client(vision=True)
    assert captured["model"] == "deepseek-v4-flash-vision-exp"


class _MismatchDeepSeek:
    model = "deepseek-v4-flash"

    def complete(self, *_args, **_kwargs):
        return ResponsesResult(
            text=json.dumps(
                {
                    "reservation_type": "RAIL",
                    "confirmation_ref": "OTHER-TRIP-1",
                    "origin": {"name": "Shanghai Hongqiao", "city": "Shanghai"},
                    "destination": {"name": "Hangzhou East", "city": "Hangzhou"},
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
                    "candidate_journey_match": {
                        "match": "MISMATCH",
                        "confidence": 0.99,
                        "reason": "The cities do not match this Tianjin Journey.",
                    },
                    "uncertain_fields": [],
                    "reasoning_summary": "This artifact belongs to another trip.",
                    "field_provenance": {},
                }
            )
        )


def test_high_confidence_journey_mismatch_cannot_auto_apply(monkeypatch) -> None:
    client = TestClient(main_module.app)
    monkeypatch.setattr(main_module, "_deepseek_client", lambda **_kwargs: _MismatchDeepSeek())
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    extracted = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/text-extract",
        json={"text": "A rail confirmation for a different city pair."},
    )
    assert extracted.status_code == 201, extracted.text
    draft = extracted.json()["draft"]
    assert draft["smart_intake"]["decision"] == "REQUIRES_CONFIRMATION"
    assert "JOURNEY_MISMATCH" in draft["smart_intake"]["reasons"]

    applied = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/smart-apply"
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "REQUIRES_CONFIRMATION"
