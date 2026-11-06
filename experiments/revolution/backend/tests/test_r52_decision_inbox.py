from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import Journey, JourneyIntent, JourneyMission
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import CopilotInterpretation
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)
_TEST_SECRET = "r52-quiet-inbox-test-secret-0123456789abcdef"


def test_one_bounded_intent_answer_resumes_the_same_lineage(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    traveler_id = None
    try:
        plans = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
        assert plans.status_code == 201, plans.text
        plan = plans.json()[0]
        assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
        assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            traveler_id = journey.traveler_id
            current_activity_id = journey.current_activity_id
        headers = {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}
        assert client.post(f"/api/v1/journeys/{journey_id}/operator/start", headers=headers, json={"interval_seconds": 3600}).status_code == 200

        fallback = CopilotInterpretation(
            intent="PREFERENCE_CHANGE", confidence=0.42, reason="deterministic fallback",
            entities={}, provider_key="deterministic", degraded=True,
            semantic_envelope={"entities": {}, "constraints": [], "confidence": 0.42},
        )
        compile_calls: list[str] = []
        monkeypatch.setattr(main_module, "_copilot_fallback_or_ai", lambda command, _context: (compile_calls.append(command) or fallback))

        class BlockedRuntime:
            calls = 0

            def run(self, session, *, journey, mission):
                self.calls += 1
                raise main_module.AgentRuntimeUnavailable("provider unavailable")

            def close(self):
                pass

        runtime = BlockedRuntime()
        monkeypatch.setattr(main_module, "configured_journey_agent", lambda **_kwargs: runtime)
        original = "I'm tired. Make the rest of today easier and keep walking low."
        submitted = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=headers,
            json={"command": original, "requested_autonomy": "PLAN_ONLY"},
        )
        assert submitted.status_code == 200, submitted.text
        first = submitted.json()
        intent_id = first["intent"]["id"]
        assert first["outcome"] == "INTENT_STEERING_NEEDS_CLARIFICATION"
        assert compile_calls == [original]

        inbox1 = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=headers)
        inbox2 = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=headers)
        assert inbox1.status_code == inbox2.status_code == 200
        assert inbox1.json()["attention"]["kind"] == "INTENT_CLARIFICATION_REQUIRED"
        assert inbox1.json()["attention"] == inbox2.json()["attention"]
        assert [option["value"] for option in inbox1.json()["attention"]["options"]] == ["SLOWER_PACE", "LESS_WALKING", "FEWER_STOPS"]

        answered = client.post(
            f"/api/v1/journeys/{journey_id}/operator/intents/{intent_id}/clarification",
            headers=headers,
            json={"answer": "LESS_WALKING"},
        )
        assert answered.status_code == 200, answered.text
        result = answered.json()["intent_clarification_result"]
        assert result["outcome"] == "DECISION_INBOX_BRAIN_BLOCKED_AFTER_RESPONSE"
        assert runtime.calls == 1
        duplicate = client.post(
            f"/api/v1/journeys/{journey_id}/operator/intents/{intent_id}/clarification",
            headers=headers,
            json={"answer": "LESS_WALKING"},
        )
        assert duplicate.status_code == 409

        with SessionLocal() as session:
            rows = session.scalars(select(JourneyIntent).where(JourneyIntent.journey_id == journey_id)).all()
            assert len(rows) == 1
            persisted = rows[0]
            assert persisted.id == intent_id
            assert persisted.status == "ACTIVE"
            assert persisted.scope_kind == "REST_OF_TODAY"
            assert json.loads(persisted.constraints_json) == [{"kind": "WALKING_LIMIT", "preference": "LOW"}]
            envelope = json.loads(persisted.semantic_envelope_json)
            assert envelope["r52_clarification"]["answer"] == "LESS_WALKING"
            assert envelope["r52_clarification"]["status"] == "ANSWERED"
            assert len(session.scalars(select(JourneyMission).where(JourneyMission.journey_id == journey_id, JourneyMission.is_standing.is_(False))).all()) == 1
            assert session.get(Journey, journey_id).current_activity_id == current_activity_id
    finally:
        if traveler_id:
            client.post(f"/api/v1/journeys/{journey_id}/operator/stop", headers={"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"})
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
