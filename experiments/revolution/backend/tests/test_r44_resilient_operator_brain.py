"""Focused R44 product-loop tests with an explicit LAB runtime simulation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.agent.guardian as guardian_module
import app.main as main_module
from app.agent.operator_brain import (
    RuntimeCandidate,
    claim_resume,
    next_retry_at,
    operator_brain_state,
    release_resume,
    safe_external_handoff,
)
from app.db.models import (
    AgentRun,
    AutopilotEnrollment,
    Event,
    GuardianRun,
    Journey,
    JourneyMission,
    OperatorRecoveryResume,
)
from app.db.session import SessionLocal
from app.recovery_epoch import ensure_recovery_epoch
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def _active_journey() -> tuple[str, dict]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan


class _LabAvailableRuntime:
    provider_key = "deepseek"
    model_key = "lab-runtime-v1"

    def run(self, session, *, journey, mission, max_steps=10):
        now = datetime.now(timezone.utc).isoformat()
        run = AgentRun(
            id=str(uuid4()),
            mission_id=mission.id,
            journey_id=journey.id,
            provider_key=self.provider_key,
            model_key=self.model_key,
            status="COMPLETED",
            outcome="LAB_RUNTIME_AVAILABILITY_SIMULATION_RESUMED",
            failure_reason=None,
            created_at=now,
            started_at=now,
            completed_at=now,
            recovery_epoch_id=None,
        )
        session.add(run)
        mission.status = "COMPLETED"
        mission.completed_at = now
        session.flush()
        return {
            "run_id": run.id,
            "mission_id": mission.id,
            "status": "COMPLETED",
            "completion_contract": {"kind": "NO_ACTION"},
            "outcome": "LAB_RUNTIME_AVAILABILITY_SIMULATION_RESUMED",
            "tool_steps": [],
        }

    def close(self):
        return None


def test_candidate_states_do_not_call_provider_and_owner_is_durable():
    state = operator_brain_state(
        configured_candidates=[
            RuntimeCandidate("deepseek", True, "DeepSeek", status="CONFIGURED_UNKNOWN")
        ]
    )
    assert state["state"] == "READY"
    assert state["runtime_candidates"][0]["status"] == "CONFIGURED_UNKNOWN"
    assert safe_external_handoff(tool_step_count=0, approval_created=False)
    assert not safe_external_handoff(tool_step_count=1, approval_created=False)
    stamp = datetime.now(timezone.utc)
    assert datetime.fromisoformat(next_retry_at(retry_count=0, now=stamp)) > stamp
    class Resume:
        status = "RUNNING"
        safe_to_resume = True
        retry_count = 0
        owner_runtime_key = "deepseek"
        owner_claim_token = "claim"
        owner_claimed_at = stamp.isoformat()
        updated_at = stamp.isoformat()
        next_retry_at = None
        blocked_reason = "MODEL_TIMEOUT"
    resume = Resume()
    release_resume(resume, retry=True, reason="MODEL_TIMEOUT", now=stamp)
    assert resume.retry_count == 1
    assert datetime.fromisoformat(resume.next_retry_at) > stamp


def test_operator_brain_blocked_lineage_reuses_incident_then_resumes(monkeypatch):
    journey_id, plan = _active_journey()
    started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        json={"interval_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    target = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={
            "state": "DISRUPTED",
            "payload": {
                "target_place_id": target["place_id"],
                "description": "LAB_FIXTURE: R44 planning brain outage",
            },
        },
    )
    assert fixture.status_code == 200, fixture.text

    first = client.post(f"/api/v1/journeys/{journey_id}/guardian/pulse")
    assert first.status_code == 200, first.text
    first_guardian_id = first.json()["guardian"]["id"]
    with SessionLocal() as session:
        resume = session.scalar(
            select(OperatorRecoveryResume).where(
                OperatorRecoveryResume.journey_id == journey_id
            )
        )
        assert resume is not None
        assert resume.safe_to_resume is True
        assert resume.retry_count == 0
        assert resume.recovery_epoch_id
        assert resume.guardian_run_id == first_guardian_id
        event_count = session.scalar(
            select(func.count()).select_from(Event).where(Event.journey_id == journey_id)
        )
        guardian_count = session.scalar(
            select(func.count()).select_from(GuardianRun).where(GuardianRun.journey_id == journey_id)
        )
        mission_count = session.scalar(
            select(func.count()).select_from(JourneyMission).where(JourneyMission.journey_id == journey_id)
        )
        session.commit()

    second = client.post(f"/api/v1/journeys/{journey_id}/guardian/pulse")
    assert second.status_code == 200, second.text
    assert second.json()["guardian"]["id"] == first_guardian_id
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.journey_id == journey_id)) == event_count
        assert session.scalar(select(func.count()).select_from(GuardianRun).where(GuardianRun.journey_id == journey_id)) == guardian_count
        assert session.scalar(select(func.count()).select_from(JourneyMission).where(JourneyMission.journey_id == journey_id)) == mission_count
        resume = session.scalar(
            select(OperatorRecoveryResume).where(OperatorRecoveryResume.journey_id == journey_id)
        )
        assert resume is not None
        resume.next_retry_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        session.commit()

    monkeypatch.setattr(
        guardian_module,
        "configured_journey_agent_candidates",
        lambda: [_LabAvailableRuntime()],
    )
    resumed = client.post(f"/api/v1/journeys/{journey_id}/guardian/pulse")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["guardian"]["id"] == first_guardian_id

    projection = client.get(f"/api/v1/journeys/{journey_id}/operator")
    assert projection.status_code == 200, projection.text
    brain = projection.json()["brain"]
    assert brain["recovery_resume"]["status"] == "RESUMED"
    assert "planning" in brain["headline"].lower() or brain["state"] in {"READY", "ACTIVE"}
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.journey_id == journey_id)) == event_count
        assert session.scalar(select(func.count()).select_from(GuardianRun).where(GuardianRun.journey_id == journey_id)) == guardian_count
        assert session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id)) == 1
        enrollment = session.get(AutopilotEnrollment, journey_id)
        assert enrollment is not None and enrollment.enabled is True

    client.post(f"/api/v1/journeys/{journey_id}/operator/stop")


def test_claim_resume_rejects_second_runtime_owner():
    journey_id, _ = _active_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        epoch = ensure_recovery_epoch(session, journey)
        row = OperatorRecoveryResume(
            id=str(uuid4()),
            journey_id=journey_id,
            recovery_epoch_id=epoch.id,
            incident_fingerprint="incident-owner",
            blocked_reason="MODEL_TIMEOUT",
            failure_classification="TEMPORARILY_FAILED",
            status="PENDING",
            next_retry_at=None,
            retry_count=0,
            safe_to_resume=True,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        session.add(row)
        session.flush()
        first = claim_resume(session, row, runtime_key="hermes")
        assert first
        session.commit()
        assert claim_resume(session, row, runtime_key="deepseek") is None
        session.rollback()
