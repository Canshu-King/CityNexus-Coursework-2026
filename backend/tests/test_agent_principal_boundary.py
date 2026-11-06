from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import AuditLog
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def test_configured_agent_token_is_required_and_body_agent_id_is_not_trusted():
    previous = settings.agent_token
    object.__setattr__(settings, "agent_token", "server-test-token")
    try:
        ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
        journey_id = ids["journey_id"]
        body = {
            "request_id": f"principal-{uuid4()}",
            "agent_id": "attacker-selected-identity",
            "journey_id": journey_id,
        }
        assert client.post("/api/v1/agent/tools/inspect_journey", json=body).status_code == 401
        assert (
            client.post(
                "/api/v1/agent/tools/inspect_journey",
                json=body,
                headers={"X-CityNexus-Agent-Token": "wrong-token"},
            ).status_code
            == 401
        )
        accepted = client.post(
            "/api/v1/agent/tools/inspect_journey",
            json=body,
            headers={"X-CityNexus-Agent-Token": "server-test-token"},
        )
        assert accepted.status_code == 200, accepted.text
    finally:
        object.__setattr__(settings, "agent_token", previous)


def test_agent_audit_actor_is_server_principal_not_request_agent_id():
    previous = settings.agent_token
    object.__setattr__(settings, "agent_token", "server-test-token")
    try:
        ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
        plans = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/plans/generate", json={"candidate_count": 1}
        ).json()
        assert (
            client.post(
                f"/api/v1/journeys/{ids['journey_id']}/plans/{plans[0]['id']}/select"
            ).status_code
            == 200
        )
        assert client.post(f"/api/v1/journeys/{ids['journey_id']}/start").status_code == 200
        response = client.post(
            "/api/v1/agent/tools/request_replan",
            json={
                "request_id": f"principal-replan-{uuid4()}",
                "agent_id": "forged-audit-actor",
                "journey_id": ids["journey_id"],
                "trigger_event_id": client.post(
                    f"/api/v1/journeys/{ids['journey_id']}/events",
                    json={"event_type": "TRAFFIC_DELAY", "payload": {"delay_minutes": 10}},
                ).json()["id"],
            },
            headers={"X-CityNexus-Agent-Token": "server-test-token"},
        )
        assert response.status_code in {200, 201}, response.text
        with SessionLocal() as session:
            audit = session.scalar(
                select(AuditLog)
                .where(AuditLog.action == "agent.request_replan")
                .order_by(AuditLog.created_at.desc())
            )
            assert audit is not None
            assert audit.actor_id == settings.agent_principal_id
            assert audit.actor_id != "forged-audit-actor"
    finally:
        object.__setattr__(settings, "agent_token", previous)
