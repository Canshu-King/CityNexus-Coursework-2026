import json

from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.ai.responses import ResponsesResult
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


class _FlagshipDeepSeek:
    model = "deepseek-flash"

    def complete(self, messages, *, enable_web_search=False, **_kwargs):
        if enable_web_search:
            return ResponsesResult(
                text="Official venue notice captured.",
                web_observations=(
                    {
                        "url": "https://venue.example/notice",
                        "domain": "venue.example",
                        "title": "Official closure notice",
                        "content": "The venue closes early today.",
                    },
                ),
            )
        content = json.dumps(
            {
                "reservation_type": "RAIL",
                "confirmation_ref": "V10-RAIL-42",
                "origin": {"name": "Shanghai Hongqiao"},
                "destination": {"name": "Tianjin West Station"},
                "scheduled_departure_at": "2026-10-09T03:30:00+08:00",
                "scheduled_arrival_at": "2026-10-09T08:00:00+08:00",
                "price_amount_minor": 55300,
                "currency": "CNY",
                "field_provenance": {
                    "confirmation_ref": {"source": "image", "status": "EXTRACTED"}
                },
            }
        )
        return ResponsesResult(text=content)


def test_v10_flagship_inbox_to_compose_web_evidence_and_execution_clock(monkeypatch):
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: _FlagshipDeepSeek())
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]

    text_draft = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/text-extract",
        json={"text": "V10-RAIL-42 rail confirmation Tianjin West"},
    )
    assert text_draft.status_code == 201 and text_draft.json()["status"] == "READY"
    draft = text_draft.json()["draft"]
    assert draft["unresolved_fields"] == []
    confirmed = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["reservation"]["confirmation_ref"] == "V10-RAIL-42"

    vision = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/vision",
        json={"image_url": "data:image/png;base64,ZmFrZQ==", "hint": "rail ticket"},
    )
    assert vision.status_code == 201 and vision.json()["draft"]["artifact_type"] == "IMAGE"

    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={
            "candidate_count": 1,
            "place_ids": ["place-old-town", "place-riverside-museum", "place-city-park"],
        },
    )
    assert composed.status_code == 201, composed.text
    assert composed.json()[0]["activities"]

    search = client.post(
        f"/api/v1/journeys/{journey_id}/web-research",
        json={"query": "official venue closure notice", "source_class": "OFFICIAL_NOTICE"},
    )
    assert search.status_code == 200 and search.json()["authority"] == "NON_AUTHORITATIVE"
    assert search.json()["observations"][0]["url"] == "https://venue.example/notice"

    clock = client.get(f"/api/v1/journeys/{journey_id}/execution-clock")
    assert clock.status_code == 200 and clock.json()["source"] == "EXECUTION_CLOCK"
    push = client.get(f"/api/v1/travelers/{ids['traveler_id']}/push-config")
    assert push.status_code == 200 and push.json()["record_fallback"] is True
