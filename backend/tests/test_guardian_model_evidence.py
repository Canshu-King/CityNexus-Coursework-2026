"""Guardian model-path evidence must name the proposal that was actually applied.

The configured-Agent-runtime path can generate several candidates in one
``request_replan`` call and then accept a different one, for example when the
highest-scoring candidate breaks a protected commitment. This gate pins the
audit contract: ``GuardianRun.proposal_id``, ``evidence.mutation.proposal_id``
and ``evidence.solver`` must all describe the accepted proposal, never the first
generated one.

The runtime is stubbed so the gate stays offline and deterministic; the
proposals themselves are real rows produced by the deterministic generator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent import guardian as guardian_module
from app.agent.guardian import scan_guardian
from app.db.models import AgentRun, AgentRunStep, GuardianRun, Journey
from app.db.session import SessionLocal

client = TestClient(main_module.app)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ready_journey() -> tuple[str, str, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    traveler_id = ids["traveler_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, traveler_id, plan


def _autonomous_policy(traveler_id: str) -> None:
    response = client.put(
        f"/api/v1/travelers/{traveler_id}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 5000,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 120,
            "allowed_activity_types": ["visit", "meal", "transport"],
            "protected_commitment_levels": ["C4_Anchored"],
            "allow_auto_commitment_management": False,
            "allow_auto_when_provider_degraded": False,
            "allow_auto_on_critical_event": False,
        },
    )
    assert response.status_code == 200, response.text


def _standing_mission(journey_id: str) -> None:
    response = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "游船和晚餐别动，优先室内、少走路，最多多花五十元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200, response.text


def _trigger_event(journey_id: str, plan: dict) -> str:
    target = next(
        item
        for item in plan["activities"]
        if item.get("place_id") not in {"place-river-cruise", "place-skyline-dinner"}
    )
    created = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": target["id"],
            "provenance": "SIMULATED",
            "payload": {"reason": "evidence gate trigger"},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _proposal_ids(journey_id: str, event_id: str) -> list[str]:
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": 3, "solver_mode": "deterministic"},
    )
    assert generated.status_code == 201, generated.text
    listed = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    items = listed if isinstance(listed, list) else listed.get("items", [])
    return [item["id"] for item in items]


class _StubRuntime:
    """Replay a synthetic model trace that requests one proposal and accepts another."""

    def __init__(self, requested_id: str, accepted_id: str) -> None:
        self.requested_id = requested_id
        self.accepted_id = accepted_id

    def run(self, session, *, journey, mission):
        run_id = str(uuid4())
        session.add(
            AgentRun(
                id=run_id,
                mission_id=mission.id,
                journey_id=journey.id,
                provider_key="citynexus-guardian",
                model_key="stub-model",
                status="COMPLETED",
                created_at=_now(),
                started_at=_now(),
                completed_at=_now(),
            )
        )
        steps = [
            ("inspect_twin", {}, {"journey_id": journey.id}),
            (
                "request_replan",
                {"journey_id": journey.id, "trigger_event_id": None},
                {"proposal": {"id": self.requested_id}},
            ),
            (
                "accept_replan",
                {"journey_id": journey.id, "proposal_id": self.accepted_id},
                {
                    "decision": "ALLOW_AUTO",
                    "executed": True,
                    "reason_codes": ["DELEGATION_POLICY_MATCHED", "DETERMINISTIC_GATE_VERIFIED"],
                    "policy_version": 2,
                },
            ),
        ]
        tool_steps = []
        for index, (tool_name, arguments, result) in enumerate(steps, start=1):
            session.add(
                AgentRunStep(
                    id=str(uuid4()),
                    run_id=run_id,
                    sequence_no=index,
                    tool_name=tool_name,
                    arguments_json=guardian_module.json.dumps(arguments),
                    result_json=guardian_module.json.dumps(result),
                    status="COMPLETED",
                    created_at=_now(),
                    completed_at=_now(),
                )
            )
            tool_steps.append(
                {
                    "id": str(uuid4()),
                    "sequence_no": index,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "result": result,
                    "decision": None,
                    "status": "COMPLETED",
                }
            )
        session.flush()
        return {
            "run_id": run_id,
            "tool_steps": tool_steps,
            "outcome": "stub runtime accepted a proposal other than the first generated",
        }

    def close(self) -> None:
        return None


def test_model_path_evidence_names_the_accepted_proposal(monkeypatch):
    journey_id, traveler_id, plan = _ready_journey()
    _autonomous_policy(traveler_id)
    _standing_mission(journey_id)
    event_id = _trigger_event(journey_id, plan)

    proposal_ids = _proposal_ids(journey_id, event_id)
    assert len(proposal_ids) >= 2, "the generator must expose at least two candidates"

    requested_id, accepted_id = proposal_ids[0], proposal_ids[1]
    assert requested_id != accepted_id, "the gate is only meaningful for distinct proposals"

    monkeypatch.setattr(
        guardian_module,
        "configured_journey_agent",
        lambda: _StubRuntime(requested_id, accepted_id),
    )

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        scan_guardian(session, journey)
        session.commit()

    with SessionLocal() as session:
        run = (
            session.query(GuardianRun)
            .filter_by(journey_id=journey_id)
            .order_by(GuardianRun.created_at.desc())
            .first()
        )
        assert run is not None
        assert run.action_status == "EXECUTED"

        evidence = guardian_module.json.loads(run.evidence_json or "{}")
        accepted_proposal = session.get(guardian_module.ReplanProposal, accepted_id)
        accepted_generator = guardian_module.json.loads(accepted_proposal.diff_json or "{}")[
            "verified_evidence"
        ]["generator"]

    assert run.proposal_id == accepted_id, (
        "GuardianRun.proposal_id must name the accepted proposal, not the first generated one"
    )
    assert evidence["mutation"]["proposal_id"] == accepted_id
    assert evidence["mutation"]["executed"] is True
    assert evidence["solver"] == accepted_generator, (
        "solver evidence must describe the accepted proposal's assignment"
    )
    assert evidence["inference"]["controller"] == "configured_agent_runtime"


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import GUARDIAN_MODEL_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
