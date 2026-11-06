from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import Journey, JourneyIntent, JourneyMission, Preference
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import CopilotInterpretation
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)
_TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


def test_authenticated_live_steering_persists_temporary_intent_without_mutating_twin(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    try:
        plans = client.post(
            f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
        )
        assert plans.status_code == 201, plans.text
        plan = plans.json()[0]
        assert client.post(
            f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
        ).status_code == 200
        assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            traveler_id = journey.traveler_id
            current_activity_id = journey.current_activity_id
            initial_preference_ids = set(
                session.scalars(select(Preference.id).where(Preference.traveler_id == traveler_id)).all()
            )
        token = issue_traveler_token(traveler_id)
        headers = {"Authorization": f"Bearer {token}"}
        started = client.post(
            f"/api/v1/journeys/{journey_id}/operator/start",
            headers=headers,
            json={"interval_seconds": 3600},
        )
        assert started.status_code == 200, started.text

        semantic = {
            "confidence": 0.94,
            "entities": {
                "intent_kind": "WALKING_LIMIT",
                "scope_kind": "NEXT_STOP",
                "preference_hints": {"walking_intensity": "LOW"},
                "activity_ids": ["model-must-not-control-targets"],
            },
            "constraints": [{"kind": "WALKING_LIMIT", "value": "LOW", "source": "USER"}],
            "capabilities": [{"kind": "WHEELCHAIR_USER", "source": "MODEL_GUESS"}],
        }
        interpretation = CopilotInterpretation(
            intent="PREFERENCE_CHANGE",
            confidence=0.94,
            reason="temporary Journey preference",
            entities=semantic["entities"],
            provider_key="deepseek",
            degraded=False,
            semantic_envelope=semantic,
        )
        compile_calls: list[str] = []
        monkeypatch.setattr(
            main_module,
            "_copilot_fallback_or_ai",
            lambda command, _context: (compile_calls.append(command) or interpretation),
        )

        class FakeRuntime:
            def run(self, session, *, journey, mission):
                assert mission.requested_autonomy == "PLAN_ONLY"
                assert json.loads(mission.preferences_json) == {}
                assert mission.parent_mission_id
                return {"status": "SUCCEEDED", "outcome": {"reason_code": "PLAN_ALREADY_FITS"}}

            def close(self):
                pass

        monkeypatch.setattr(main_module, "configured_journey_agent", lambda **_kwargs: FakeRuntime())
        command = "I’m tired. Make the next stop easy and keep walking low."
        response = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=headers,
            json={"command": command, "requested_autonomy": "EXECUTE_WITHIN_BOUNDS"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "INTENT_STEERING_PLAN_ALREADY_FITS"
        assert compile_calls == [command]
        intent = body["intent"]
        assert intent["source"] == "USER_TEMPORARY_INTENT"
        assert intent["created_by_traveler_id"] == traveler_id
        assert intent["scope_kind"] == "NEXT_STOP"
        assert intent["target_activity_ids"]
        assert "model-must-not-control-targets" not in intent["target_activity_ids"]
        assert intent["capabilities"] == []

        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            assert journey.current_activity_id == current_activity_id
            preferences = set(
                session.scalars(select(Preference.id).where(Preference.traveler_id == traveler_id)).all()
            )
            assert preferences == initial_preference_ids
            persisted = session.get(JourneyIntent, intent["id"])
            assert persisted is not None and persisted.status == "ACTIVE"
            child = session.get(JourneyMission, body["mission"]["id"])
            assert child is not None and child.requested_autonomy == "PLAN_ONLY"
    finally:
        client.post(f"/api/v1/journeys/{journey_id}/operator/stop", headers={"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"} if "traveler_id" in locals() else {})
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
