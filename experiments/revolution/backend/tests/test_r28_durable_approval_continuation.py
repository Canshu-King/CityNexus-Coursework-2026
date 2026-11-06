from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.continuation import capture_pending_action
from app.agent.gateway import AgentToolGateway
from app.agent.runtime import DeepSeekJourneyAgent, _acceptance_requested, _terminal_agent_status
from app.core.config import settings
from app.schemas import AgentToolCall
from test_agent_authority_v2_actions import _selected_plan
from app.db.models import (
    ActionApproval,
    AgentPendingAction,
    AgentRun,
    AgentRunStep,
    Journey,
    JourneyMission,
    PlanVersion,
    ReplanProposal,
    Traveler,
    AuditLog,
)
from app.db.session import SessionLocal


client = TestClient(main_module.app)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_pending_continuation() -> dict[str, str]:
    traveler_id = str(uuid4())
    journey_id = str(uuid4())
    mission_id = str(uuid4())
    run_id = str(uuid4())
    approval_id = str(uuid4())
    pending_id = str(uuid4())
    now = _now()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="R28 traveler",
                email=None,
                locale="en-US",
                timezone="UTC",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            Journey(
                id=journey_id,
                traveler_id=traveler_id,
                title="R28 durable approval",
                city_code="TYO",
                timezone="UTC",
                start_at=now,
                end_at=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
                budget_currency="JPY",
                budget_amount_minor=10000,
                planning_mode="balanced",
                status="ACTIVE",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        # The fixture uses scalar foreign-key ids without ORM relationships;
        # flush the parent rows before inserting approval/pending rows so
        # SQLite's production foreign-key enforcement sees the same durable
        # ordering as the API path.
        session.flush()
        session.add(
            JourneyMission(
                id=mission_id,
                journey_id=journey_id,
                traveler_id=traveler_id,
                trigger_event_id=None,
                user_text="Resume the already selected action after I approve it.",
                requested_autonomy="EXECUTE_WITHIN_BOUNDS",
                status="WAITING_FOR_APPROVAL",
                created_at=now,
                started_at=now,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=run_id,
                mission_id=mission_id,
                journey_id=journey_id,
                provider_key="hermes",
                model_key="test-model",
                status="WAITING_FOR_APPROVAL",
                outcome="Agent is waiting for the required human approval.",
                created_at=now,
                started_at=now,
                completed_at=None,
            )
        )
        session.flush()
        session.add(
            ActionApproval(
                id=approval_id,
                traveler_id=traveler_id,
                journey_id=journey_id,
                action="ACCEPT_REPLAN",
                target_type="ReplanProposal",
                target_id=str(uuid4()),
                request_fingerprint="r28-test-fingerprint",
                status="PENDING",
                required_decision="REQUIRE_CONFIRMATION",
                created_at=now,
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            )
        )
        session.add(
            AgentPendingAction(
                id=pending_id,
                approval_id=approval_id,
                run_id=run_id,
                mission_id=mission_id,
                journey_id=journey_id,
                originating_step_id=None,
                tool_name="accept_replan",
                arguments_json=(
                    '{"agent_id":"citynexus-agent","journey_id":"'
                    + journey_id
                    + '","mission_id":"'
                    + mission_id
                    + '","proposal_id":"'
                    + str(uuid4())
                    + '","request_id":"original-agent-request"}'
                ),
                status="PENDING",
                result_json="{}",
                error_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return {
        "traveler_id": traveler_id,
        "journey_id": journey_id,
        "mission_id": mission_id,
        "run_id": run_id,
        "approval_id": approval_id,
        "pending_id": pending_id,
    }


def test_r30_pending_action_binds_receipt_at_initial_suspension() -> None:
    seeded = _seed_pending_continuation()
    receipt_id = "receipt-initial-suspension"
    with SessionLocal() as session:
        run = session.get(AgentRun, seeded["run_id"])
        mission = session.get(JourneyMission, seeded["mission_id"])
        pending = session.get(AgentPendingAction, seeded["pending_id"])
        approval = session.get(ActionApproval, seeded["approval_id"])
        assert run is not None
        assert mission is not None
        assert pending is not None
        assert approval is not None
        assert pending.authority_receipt_id is None

        approval.authority_receipt_id = receipt_id
        step = AgentRunStep(
            id=str(uuid4()),
            run_id=run.id,
            sequence_no=99,
            tool_name="accept_replan",
            arguments_json="{}",
            result_json="{}",
            decision="REQUIRE_CONFIRMATION",
            status="COMPLETED",
            created_at=_now(),
            completed_at=_now(),
        )
        captured = capture_pending_action(
            session,
            run=run,
            mission=mission,
            step=step,
            tool_name="accept_replan",
            arguments={
                "agent_id": "citynexus-agent",
                "journey_id": seeded["journey_id"],
                "mission_id": seeded["mission_id"],
                "request_id": "initial-suspension-test",
            },
            result={
                "tool": "accept_replan",
                "decision": "REQUIRE_CONFIRMATION",
                "executed": False,
                "approval_id": seeded["approval_id"],
                "status": "PENDING",
                "collective_authority_receipt_id": receipt_id,
            },
        )
        assert captured is not None
        assert captured.id == seeded["pending_id"]
        assert captured.authority_receipt_id == receipt_id
        session.commit()

    with SessionLocal() as verify:
        assert (
            verify.get(AgentPendingAction, seeded["pending_id"]).authority_receipt_id
            == receipt_id
        )
        assert (
            verify.get(ActionApproval, seeded["approval_id"]).authority_receipt_id
            == receipt_id
        )


def test_r28_server_resolved_collective_selection_requires_acceptance() -> None:
    proposal_id = str(uuid4())
    mission = JourneyMission(
        id=str(uuid4()),
        journey_id=str(uuid4()),
        traveler_id=str(uuid4()),
        user_text=(
            "Resume the shared Journey using the server-resolved collective proposal "
            "and normal authority boundaries."
        ),
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
        resolved_intent_json=json.dumps(
            {"collective_decision": {"selected_proposal_id": proposal_id}}
        ),
        status="CREATED",
        created_at=_now(),
    )
    assert _acceptance_requested(mission) is True

    mission.requested_autonomy = "PLAN_ONLY"
    assert _acceptance_requested(mission) is False


def test_r28_terminal_status_is_durable_wait_not_completed() -> None:
    trace = [
        {
            "tool_name": "accept_replan",
            "result": {
                "executed": False,
                "decision": "REQUIRE_CONFIRMATION",
                "approval_id": str(uuid4()),
                "status": "PENDING",
            },
            "status": "COMPLETED",
        }
    ]
    run_status, mission_status = _terminal_agent_status(
        trace, has_final_text=True, clarification=False
    )
    assert run_status == "WAITING_FOR_APPROVAL"
    assert mission_status == "WAITING_FOR_APPROVAL"


def test_r28_approval_route_resumes_same_run_without_second_model_turn(monkeypatch) -> None:
    seeded = _seed_pending_continuation()
    calls: list[tuple[str, str]] = []

    def fake_dispatch(self, tool_name, request):
        calls.append((tool_name, request.request_id))
        assert str(request.approval_id) == seeded["approval_id"]
        assert request.request_id == f"agent-resume-{seeded['pending_id']}"
        approval = self.session.get(ActionApproval, seeded["approval_id"])
        assert approval is not None
        approval.status = "CONSUMED"
        approval.consumed_at = _now()
        self.session.commit()
        return {
            "tool": tool_name,
            "decision": "REQUIRE_CONFIRMATION",
            "executed": True,
            "approval_id": seeded["approval_id"],
            "status": "CONSUMED",
            "collective_authority_receipt_id": "receipt-continuation-test",
            "result": {
                "status": "ACCEPTED",
                "collective_authority_receipt_id": "receipt-continuation-test",
            },
        }

    monkeypatch.setattr("app.agent.gateway.AgentToolGateway.dispatch", fake_dispatch)

    approved = client.post(
        f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/"
        f"{seeded['approval_id']}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["status"] == "CONSUMED"
    assert payload["continuation"]["status"] == "RESUMED"
    assert payload["continuation"]["run_id"] == seeded["run_id"]
    assert len(calls) == 1

    with SessionLocal() as session:
        pending = session.get(AgentPendingAction, seeded["pending_id"])
        run = session.get(AgentRun, seeded["run_id"])
        mission = session.get(JourneyMission, seeded["mission_id"])
        approval = session.get(ActionApproval, seeded["approval_id"])
        assert pending is not None and pending.status == "RESUMED"
        assert run is not None and run.status == "COMPLETED" and run.completed_at
        assert mission is not None and mission.status == "COMPLETED" and mission.completed_at
        assert approval is not None and approval.status == "CONSUMED"
        assert pending.authority_receipt_id == "receipt-continuation-test"
        assert approval.authority_receipt_id == "receipt-continuation-test"
        steps = session.query(AgentRunStep).filter(AgentRunStep.run_id == seeded["run_id"]).all()
        assert (
            json.loads(steps[0].result_json)[
                "collective_authority_receipt_id"
            ]
            == "receipt-continuation-test"
        )
        assert len(steps) == 1
        assert steps[0].tool_name == "accept_replan"
        assert "agent-resume-" in steps[0].arguments_json

    replay = client.post(
        f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/"
        f"{seeded['approval_id']}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["continuation"]["replayed"] is True
    assert len(calls) == 1, "approval replay must not cause a second mutation"
    with SessionLocal() as session:
        assert session.query(AgentRunStep).filter_by(run_id=seeded["run_id"]).count() == 1


def test_r28_real_gateway_approval_resumes_same_agent_run() -> None:
    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if not item["locked"])
    mission_id = str(uuid4())
    run_id = str(uuid4())
    now = _now()

    arguments = {
        "request_id": "r28-real-gateway-pending-" + str(uuid4()),
        "agent_id": settings.agent_principal_id,
        "journey_id": journey_id,
        "mission_id": mission_id,
        "activity_id": target["id"],
        "start_at": target["start_at"],
        "end_at": target["end_at"],
    }

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        mission = JourneyMission(
            id=mission_id,
            journey_id=journey_id,
            traveler_id=journey.traveler_id,
            trigger_event_id=None,
            user_text="Keep this schedule, but execute it only after my approval.",
            requested_autonomy="EXECUTE_WITHIN_BOUNDS",
            status="RUNNING",
            created_at=now,
            started_at=now,
        )
        session.add(mission)
        session.flush()
        run = AgentRun(
            id=run_id,
            mission_id=mission_id,
            journey_id=journey_id,
            provider_key="hermes",
            model_key="r28-local-gateway-control",
            status="RUNNING",
            outcome=None,
            created_at=now,
            started_at=now,
            completed_at=None,
        )
        session.add(run)
        session.flush()

        request = AgentToolCall.model_validate(arguments)
        pending_result = AgentToolGateway(session).dispatch("reschedule_activity", request)
        assert pending_result["executed"] is False
        assert pending_result["decision"] in {"REQUIRE_CONFIRMATION", "REQUIRE_STEP_UP"}
        approval_id = str(pending_result["approval_id"])

        step = AgentRunStep(
            id=str(uuid4()),
            run_id=run_id,
            sequence_no=1,
            tool_name="reschedule_activity",
            arguments_json=__import__("json").dumps(arguments, sort_keys=True),
            result_json=__import__("json").dumps(pending_result, sort_keys=True, default=str),
            decision=str(pending_result["decision"]),
            status="COMPLETED",
            created_at=_now(),
            completed_at=_now(),
        )
        session.add(step)
        captured = capture_pending_action(
            session,
            run=run,
            mission=mission,
            step=step,
            tool_name="reschedule_activity",
            arguments=arguments,
            result=pending_result,
        )
        assert captured is not None
        pending_id = captured.id
        session.commit()

    approved = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["status"] == "CONSUMED"
    assert payload["continuation"]["status"] == "RESUMED"
    assert payload["continuation"]["run_id"] == run_id

    with SessionLocal() as session:
        pending = session.get(AgentPendingAction, pending_id)
        run = session.get(AgentRun, run_id)
        mission = session.get(JourneyMission, mission_id)
        approval = session.get(ActionApproval, approval_id)
        assert pending is not None and pending.status == "RESUMED"
        assert run is not None and run.status == "COMPLETED"
        assert mission is not None and mission.status == "COMPLETED"
        assert approval is not None and approval.status == "CONSUMED"
        steps = (
            session.query(AgentRunStep)
            .filter(AgentRunStep.run_id == run_id)
            .order_by(AgentRunStep.sequence_no)
            .all()
        )
        assert [item.tool_name for item in steps] == [
            "reschedule_activity",
            "reschedule_activity",
        ]
        assert "agent-resume-" in steps[-1].arguments_json
        assert __import__("json").loads(steps[-1].result_json)["executed"] is True


def test_r28_startup_recovers_consumed_approval_without_second_mutation(monkeypatch) -> None:
    """Reopen the app after Gateway committed but before outer finalization."""
    seeded = _seed_pending_continuation()
    base_id, proposed_id, proposal_id = (str(uuid4()) for _ in range(3))
    with SessionLocal() as session:
        for version, plan_id in enumerate((base_id, proposed_id), start=1):
            session.add(PlanVersion(
                id=plan_id, journey_id=seeded["journey_id"], version_no=version,
                planning_mode="balanced", status="SUPERSEDED" if version == 1 else "ACTIVE",
                source="INITIAL" if version == 1 else "REPLAN",
                total_cost_minor=0, currency="JPY", estimated_duration_min=60,
                created_at=_now(),
            ))
        session.flush()
        session.add(ReplanProposal(
            id=proposal_id, journey_id=seeded["journey_id"], trigger_event_id=None,
            base_plan_version_id=base_id, proposed_plan_version_id=proposed_id,
            status="ACCEPTED", hard_constraints_passed=True,
            locked_commitments_preserved=True, utility_score=0.5,
            score_breakdown_json="{}", diff_json="{}", explanation_json="{}",
            created_at=_now(), decided_at=_now(),
        ))
        journey = session.get(Journey, seeded["journey_id"])
        journey.active_plan_version_id = proposed_id
        pending = session.get(AgentPendingAction, seeded["pending_id"])
        arguments = json.loads(pending.arguments_json)
        arguments["proposal_id"] = proposal_id
        pending.arguments_json = json.dumps(arguments)
        pending.status = "RESUMING"
        pending.claimed_at = _now()
        approval = session.get(ActionApproval, seeded["approval_id"])
        approval.target_id = proposal_id
        approval.status = "CONSUMED"
        approval.approved_at = _now()
        approval.consumed_at = _now()
        approval.authority_receipt_id = "receipt-crash-recovery-test"
        session.commit()

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError("startup recovery must not perform a second mutation")

    monkeypatch.setattr(AgentToolGateway, "dispatch", must_not_dispatch)
    with TestClient(main_module.app) as restarted_client:
        assert restarted_client.get("/api/v1/health").status_code == 200
    with SessionLocal() as session:
        pending = session.get(AgentPendingAction, seeded["pending_id"])
        assert pending.status == "RESUMED"
        recovered = json.loads(pending.result_json)
        assert recovered["continuation_recovered_from_domain_truth"] is True
        assert (
            recovered["collective_authority_receipt_id"]
            == "receipt-crash-recovery-test"
        )
        assert pending.authority_receipt_id == "receipt-crash-recovery-test"
        assert session.get(AgentRun, seeded["run_id"]).status == "COMPLETED"
        assert session.get(JourneyMission, seeded["mission_id"]).status == "COMPLETED"
        assert session.get(Journey, seeded["journey_id"]).active_plan_version_id == proposed_id
        assert session.query(AgentRunStep).filter_by(run_id=seeded["run_id"]).count() == 1
    # A second boot and approval replay are read-only for the recovered lineage.
    with TestClient(main_module.app) as restarted_client:
        replay = restarted_client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/{seeded['approval_id']}/approve",
            json={"approval_method": "USER_CONFIRMATION"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["continuation"]["replayed"] is True
    with SessionLocal() as session:
        assert session.query(AgentRunStep).filter_by(run_id=seeded["run_id"]).count() == 1


def test_r28_model_loop_suspends_then_authenticated_approval_resumes() -> None:
    """Fake only model selection; run the real runtime, Gateway and HTTP route."""
    from app.traveler_principal import issue_traveler_token

    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if not item["locked"])
    mission_id = str(uuid4())
    calls = []

    class LocalModelControl(DeepSeekJourneyAgent):
        provider_key = "local-test-control"

        def __init__(self):
            self.model_key = "deterministic-tool-selection-not-external-agent"

        def _complete(self, messages):
            calls.append(len(messages))
            assert len(calls) <= 2, "waiting/approval must not spend another model turn"
            tool = "inspect_journey" if len(calls) == 1 else "reschedule_activity"
            arguments = {} if len(calls) == 1 else {
                "activity_id": target["id"], "start_at": target["start_at"], "end_at": target["end_at"],
            }
            return {"tool_calls": [{
                "id": f"local-call-{len(calls)}", "type": "function",
                "function": {"name": tool, "arguments": json.dumps(arguments)},
            }]}

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        traveler_id = journey.traveler_id
        mission = JourneyMission(
            id=mission_id, journey_id=journey_id, traveler_id=traveler_id,
            user_text="Maintain the current schedule within normal approval boundaries.",
            requested_autonomy="EXECUTE_WITHIN_BOUNDS", status="CREATED", created_at=_now(),
        )
        session.add(mission)
        session.commit()
        payload = LocalModelControl().run(session, journey=journey, mission=mission)
        run_id = payload["run_id"]
        pending = session.query(AgentPendingAction).filter_by(run_id=run_id).one()
        approval_id = pending.approval_id
        assert session.get(AgentRun, run_id).status == "WAITING_FOR_APPROVAL"
        assert session.get(AgentRun, run_id).completed_at is None
        assert mission.status == "WAITING_FOR_APPROVAL" and mission.completed_at is None
    assert len(calls) == 2
    # A new HTTP request/session represents a user returning after suspension.
    previous_secret = settings.traveler_auth_secret
    test_secret = "r28-local-runtime-control-secret-0123456789abcdef"
    object.__setattr__(settings, "traveler_auth_secret", test_secret)
    try:
        headers = {"Authorization": "Bearer " + issue_traveler_token(traveler_id)}
        route = f"/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve"
        approved = client.post(route, headers=headers, json={"approval_method": "USER_CONFIRMATION"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["continuation"]["status"] == "RESUMED"
        assert approved.json()["approved_by"] == traveler_id
        replay = client.post(route, headers=headers, json={"approval_method": "USER_CONFIRMATION"})
        assert replay.status_code == 200, replay.text
        assert replay.json()["continuation"]["replayed"] is True
        assert len(calls) == 2
        with SessionLocal() as session:
            assert session.get(AgentRun, run_id).status == "COMPLETED"
            assert session.get(JourneyMission, mission_id).status == "COMPLETED"
            assert session.get(ActionApproval, approval_id).status == "CONSUMED"
            steps = session.query(AgentRunStep).filter_by(run_id=run_id).order_by(AgentRunStep.sequence_no).all()
            assert [step.tool_name for step in steps] == ["inspect_journey", "reschedule_activity", "reschedule_activity"]
            assert json.loads(steps[-1].result_json)["executed"] is True
            mutations = session.query(AuditLog).filter_by(journey_id=journey_id, action="agent.reschedule_activity").all()
            executed = [row for row in mutations if json.loads(row.after_json)["status"] == "EXECUTED"]
            assert len(executed) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
