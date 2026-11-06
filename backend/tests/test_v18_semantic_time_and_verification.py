from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Event, Journey, WebObservation
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import CopilotInterpretation
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def _future_interpretation(subject: str = "flight cancellation") -> CopilotInterpretation:
    semantic = {
        "intent": "DISRUPTION_REPORT",
        "assertion_status": "ASSERTED",
        "effective_time": "FUTURE",
        "event_type": "SCHEDULE_DELAY",
        "requested_action": "REQUEST_REPLAN",
        "confidence": 0.96,
        "reasoning_summary": "asserted future disruption",
        "entities": {"subject_text": subject, "temporal_expression": "tomorrow"},
        "constraints": [],
        "create_event": True,
    }
    return CopilotInterpretation(
        "DISRUPTION_REPORT", 0.96, "asserted future disruption", semantic["entities"],
        "deepseek", False, semantic,
    )


def test_future_event_uses_authoritative_activity_start(monkeypatch):
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    target_time = "2026-10-11T19:00:00+08:00"
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        journey.end_at = target_time
        session.commit()
    monkeypatch.setattr(main_module, "_copilot_fallback_or_ai", lambda *_: _future_interpretation())

    body = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "明天的航班已经取消了", "locale": "zh"},
    ).json()

    assert body["event"]["occurred_at"] == target_time
    assert body["event"]["affected_activity_id"] is None
    assert body["event"]["target_resolution"] == "AUTHORITATIVE_JOURNEY_END"


def test_ambiguous_future_event_requires_review_and_does_not_bind_current(monkeypatch):
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        journey.end_at = "2026-10-12T19:00:00+08:00"
        session.commit()
    monkeypatch.setattr(main_module, "_copilot_fallback_or_ai", lambda *_: _future_interpretation("unmatched subject"))

    body = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "后天某项安排确定关闭", "locale": "zh"},
    ).json()

    assert body["event"]["status"] == "REVIEW_REQUIRED"
    assert body["event"]["requires_review"] is True
    assert body["event"]["affected_activity_id"] is None
    assert body["event"]["occurred_at"] is None
    assert body["action"]["type"] == "REVIEW_FUTURE_EVENT_TARGET"
    with SessionLocal() as session:
        assert session.scalar(select(Event).where(Event.journey_id == journey_id)) is None


def test_verify_fact_runs_bounded_source_capture_without_event(monkeypatch):
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    interpretation = CopilotInterpretation(
        "VERIFY_FACT", 0.9, "unverified", {"subject_text": "museum closure"}, "deepseek", False,
        {
            "assertion_status": "UNVERIFIED",
            "effective_time": "FUTURE",
            "entities": {"subject_text": "museum closure"},
            "create_event": False,
        },
    )
    fake = SimpleNamespace(
        model="deepseek-test",
        complete=lambda *_args, **_kwargs: SimpleNamespace(
            text="official venue notice",
            web_observations=[{
                "url": "https://museum.example/notice",
                "domain": "museum.example",
                "title": "Venue notice",
                "content": "The venue notice requires human review.",
            }],
        ),
    )
    monkeypatch.setattr(main_module, "_copilot_fallback_or_ai", lambda *_: interpretation)
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)

    body = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "朋友说明天博物馆关闭，是真的吗？", "locale": "zh"},
    ).json()

    assert body["event"] is None
    assert body["verification"]["status"] == "EVIDENCE_CAPTURED_REVIEW_REQUIRED"
    assert body["verification"]["event_created"] is False
    with SessionLocal() as session:
        assert session.scalar(select(Event).where(Event.journey_id == journey_id)) is None
        assert session.scalar(select(WebObservation).where(WebObservation.journey_id == journey_id)) is not None
