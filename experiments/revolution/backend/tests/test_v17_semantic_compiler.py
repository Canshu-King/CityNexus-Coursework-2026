from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
import app.agent.runtime_identity as runtime_identity_module
from app.db.models import Event
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import (
    CopilotInterpretation,
    classify_copilot_message,
    disruption_event_type,
)
from app.integrations.ai.semantic_compiler import (
    SEMANTIC_ENVELOPE_SCHEMA,
    SemanticEnvelope,
    apply_semantic_policy,
    compile_semantic_envelope,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def _envelope(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "intent": "DISRUPTION_REPORT",
        "assertion_status": "ASSERTED",
        "effective_time": "NOW",
        "event_type": "TRAFFIC_DELAY",
        "requested_action": "REQUEST_REPLAN",
        "confidence": 0.94,
        "reasoning_summary": "The traveler reports an asserted traffic disruption.",
        "entities": {
            "subject_kind": "traffic",
            "subject_text": "traffic ahead",
            "temporal_expression": "now",
        },
        "constraints": [],
    }
    value.update(overrides)
    return value


class FakeSemanticClient:
    def __init__(self, envelope: dict[str, object]):
        self.envelope = envelope
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(text=json.dumps(self.envelope, ensure_ascii=False))


def _context() -> dict[str, object]:
    return {
        "journey_id": "journey-1",
        "city_code": "CN",
        "timezone": "Asia/Shanghai",
        "status": "ACTIVE",
        "current_activity_id": "activity-1",
        "plan": {
            "id": "plan-1",
            "version_no": 1,
            "status": "SELECTED",
            "currency": "CNY",
            "cost_minor": 1000,
            "activities": [
                {"id": "activity-1", "title": "Museum", "locked": False}
            ],
        },
        "protected_commitments": [],
        "preferences": [],
    }


def test_semantic_envelope_is_strict_and_policy_preserves_effective_time():
    envelope = SemanticEnvelope.from_mapping(
        _envelope(effective_time="FUTURE", event_type="SCHEDULE_DELAY")
    )
    assert envelope is not None
    decision = apply_semantic_policy(envelope)
    assert decision.create_event is True
    assert decision.event_type == "SCHEDULE_DELAY"
    assert envelope.effective_time == "FUTURE"

    invalid = dict(_envelope())
    invalid["unexpected_authority"] = "model"
    assert SemanticEnvelope.from_mapping(invalid) is None


def test_compiler_calls_deepseek_with_the_semantic_schema():
    fake = FakeSemanticClient(_envelope())
    result = compile_semantic_envelope(fake, "现在前面堵车了", {"city_code": "CN"})
    assert result is not None
    assert fake.calls[0]["response_schema"] == SEMANTIC_ENVELOPE_SCHEMA
    assert fake.calls[0]["response_schema_name"] == "citynexus_semantic_envelope"


def test_valid_deepseek_envelope_wins_when_deterministic_boundary_has_no_event(monkeypatch):
    fake = FakeSemanticClient(
        _envelope(
            intent="VERIFY_FACT",
            assertion_status="UNVERIFIED",
            effective_time="FUTURE",
            event_type="CLOSURE",
            requested_action="VERIFY_EVIDENCE",
            reasoning_summary="The closure claim is not verified yet.",
        )
    )
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)

    result = main_module._copilot_fallback_or_ai("我想少走路", _context())

    assert result.intent == "VERIFY_FACT"
    assert result.provider_key == "deepseek"
    assert result.degraded is False
    assert result.semantic_envelope["assertion_status"] == "UNVERIFIED"
    assert result.semantic_envelope["effective_time"] == "FUTURE"


def test_current_disruption_cannot_be_downgraded_by_model_route(monkeypatch):
    fake = FakeSemanticClient(
        _envelope(
            intent="VERIFY_FACT",
            assertion_status="UNVERIFIED",
            effective_time="FUTURE",
            event_type="CLOSURE",
            requested_action="VERIFY_EVIDENCE",
            reasoning_summary="The closure claim is not verified yet.",
        )
    )
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)

    result = main_module._copilot_fallback_or_ai("狮子林现在闭馆了", _context())

    assert result.intent == "DISRUPTION_REPORT"
    assert result.entities["event_type"] == "CLOSURE"
    assert result.provider_key == "deterministic"
    assert result.degraded is True


def test_recovery_status_inspection_stays_read_only_before_model_compilation(monkeypatch):
    fake = FakeSemanticClient(_envelope(event_type="CLOSURE"))
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    message = (
        "检查当前已经启动的苏州行程，确认闭馆后的恢复提案是否已成为权威计划，并报告当前站、"
        "下一站、路线来源和是否还有待处理调整；只读，不要修改。"
    )

    result = main_module._copilot_fallback_or_ai(message, _context())

    assert fake.calls == []
    assert result.provider_key == "deterministic"
    assert result.intent != "DISRUPTION_REPORT"
    assert result.semantic_envelope is None


def test_agent_command_guard_rejects_model_event_for_recovery_status_inspection(monkeypatch):
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    message = (
        "检查当前已经启动的苏州行程，确认闭馆后的恢复提案是否已成为权威计划，并报告当前站、"
        "下一站、路线来源和是否还有待处理调整；只读，不要修改。"
    )
    malicious = CopilotInterpretation(
        "DISRUPTION_REPORT",
        1.0,
        "fixture model misread a historical recovery reference",
        {"event_type": "CLOSURE"},
        "deepseek",
        False,
        {"create_event": True, "event_type": "CLOSURE"},
    )
    monkeypatch.setattr(main_module, "_copilot_fallback_or_ai", lambda *_args: malicious)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(main_module.settings, ai_api_key=None, hermes_api_url=None, hermes_api_key=None),
    )

    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        json={"command": message, "requested_autonomy": "PLAN_ONLY"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["event_id"] is None
    with SessionLocal() as session:
        events = session.scalars(select(Event).where(Event.journey_id == journey_id)).all()
    assert events == []


def test_asserted_future_envelope_routes_to_existing_event_path(monkeypatch):
    fake = FakeSemanticClient(
        _envelope(
            effective_time="FUTURE",
            event_type="SCHEDULE_DELAY",
            reasoning_summary="The scheduled flight cancellation is asserted for tomorrow.",
        )
    )
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "明天的航班已经取消了", "locale": "zh"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"]["name"] == "DISRUPTION_REPORT"
    assert body["intent"]["semantic_envelope"]["effective_time"] == "FUTURE"
    assert body["event"]["event_type"] == "SCHEDULE_DELAY"
    with SessionLocal() as session:
        event = session.scalars(select(Event).where(Event.journey_id == journey_id)).one()
        payload = json.loads(event.payload_json)
    assert payload["semantic"]["effective_time"] == "FUTURE"
    assert payload["semantic"]["assertion_status"] == "ASSERTED"


def test_deterministic_outage_fallback_is_conservative_for_general_context():
    assert disruption_event_type("北京经常堵车") is None
    assert classify_copilot_message("北京经常堵车").intent != "DISRUPTION_REPORT"
    assert classify_copilot_message("听说明天博物馆可能闭馆，是真的吗？").intent == "VERIFY_FACT"


def test_unverified_model_envelope_never_creates_city_event(monkeypatch):
    fake = FakeSemanticClient(
        _envelope(
            intent="VERIFY_FACT",
            assertion_status="UNVERIFIED",
            effective_time="FUTURE",
            event_type="CLOSURE",
            requested_action="VERIFY_EVIDENCE",
            reasoning_summary="The claim needs evidence before it can affect the Journey.",
        )
    )
    monkeypatch.setattr(main_module, "_deepseek_client", lambda: fake)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "朋友说博物馆明天关闭，是真的吗？", "locale": "zh"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"]["name"] == "VERIFY_FACT"
    assert body["event"] is None
    assert body["intent"]["semantic_envelope"]["create_event"] is False


def test_runtime_identity_labels_are_explicit_and_non_secret(monkeypatch):
    fake_settings = SimpleNamespace(
        hermes_native_binary=None,
        hermes_native_proven=None,
        hermes_api_url=None,
        hermes_api_key=None,
        ai_api_key=None,
    )
    monkeypatch.setattr(runtime_identity_module, "settings", fake_settings)
    monkeypatch.setattr(runtime_identity_module, "_hermes_binary", lambda: None)
    assert runtime_identity_module.runtime_status()["runtime_identity"] == "NOT_CONFIGURED"

    fake_settings.ai_api_key = "process-only-test-key"
    assert runtime_identity_module.runtime_status()["runtime_identity"] == "NATIVE_DEEPSEEK_TOOL_LOOP"

    fake_settings.ai_api_key = None
    fake_settings.hermes_api_url = "http://hermes.example/v1"
    fake_settings.hermes_api_key = "process-only-test-key"
    assert runtime_identity_module.runtime_status()["runtime_identity"] == "HERMES_COMPAT_ENDPOINT"


def test_runtime_status_endpoint_exposes_identity_without_credentials():
    response = client.get("/api/v1/agent/runtime-status")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime_identity"] in {
        "HERMES_NATIVE_MCP",
        "NATIVE_DEEPSEEK_TOOL_LOOP",
        "HERMES_COMPAT_ENDPOINT",
        "NOT_CONFIGURED",
    }
    assert "api_key" not in json.dumps(body).lower()
