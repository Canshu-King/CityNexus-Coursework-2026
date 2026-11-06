from fastapi.testclient import TestClient
from datetime import datetime, timezone
import json
from uuid import uuid4

import app.main as main_module
from app.agent.guardian import scan_guardian
from app.db.models import AgentRun, Event, GuardianPulse, Journey
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_bounded_guardian_pulse_persists_and_reads_latest(monkeypatch):
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan_id = generated.json()[0]["id"]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    called = {"count": 0}

    def one_pulse(session, journey):
        called["count"] += 1
        now = datetime.now(timezone.utc).isoformat()
        session.add(
            GuardianPulse(
                id="pulse-test-1",
                journey_id=journey.id,
                status="COMPLETED",
                provider_key="composite-provider-mesh",
                observation_ids_json="[]",
                result_json=json.dumps({"context_watch": {"status": "NO_MATERIAL_CHANGE"}}),
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()
        return {
            "pulse_id": "pulse-test-1",
            "context_watch": {"status": "NO_MATERIAL_CHANGE"},
            "guardian": {"status": "NO_ACTION"},
            "journey_id": journey.id,
        }

    monkeypatch.setattr(main_module, "pulse_journey", one_pulse)
    response = client.post(f"/api/v1/journeys/{journey_id}/guardian/pulse")
    assert response.status_code == 200, response.text
    assert response.json()["pulse_id"] == "pulse-test-1"
    assert called["count"] == 1

    # The bounded route delegates one cycle only; the persisted read remains a
    # separate server-owned projection when the real pulse implementation is used.
    latest = client.get(f"/api/v1/journeys/{journey_id}/guardian/pulse/latest")
    assert latest.status_code == 200
    assert latest.json()["journey_id"] == journey_id
    assert latest.json()["pulse"]["id"] == "pulse-test-1"
    assert latest.json()["pulse"]["result"]["context_watch"]["status"] == "NO_MATERIAL_CHANGE"


def test_live_pulse_does_not_promote_opportunity_signal_to_recovery_agent():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan_id = generated.json()[0]["id"]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Event(
                id=str(uuid4()),
                journey_id=journey_id,
                provider_key="citynexus-twin-opportunity",
                provenance="SYSTEM_DERIVED",
                event_type="OPPORTUNITY_WINDOW",
                severity="INFO",
                status="DETECTED",
                occurred_at=now,
                detected_at=now,
                payload_json=json.dumps({"signal_kind": "OPPORTUNITY_WINDOW"}),
                created_at=now,
            )
        )
        session.commit()

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        result = scan_guardian(
            session, journey, include_opportunity_signals=False
        )
        session.commit()
        assert result["decision"] == "NO_ACTION"
        assert result["agent_run_id"] is None
        assert session.query(AgentRun).filter(AgentRun.journey_id == journey_id).count() == 0
