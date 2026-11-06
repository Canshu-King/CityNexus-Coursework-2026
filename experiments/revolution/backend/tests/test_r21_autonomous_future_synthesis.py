from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock, patch

from app.agent.ai_availability import ai_availability_state
from app.agent.gateway import AgentToolGateway, _latest_actionable_event, _resolve_actionable_event
from app.agent.guardian import _guardian_terminal_status
from app.agent.mission_plan import build_mission_plan
from app.agent.predictive import _material_difference_fingerprint
from app.agent.runtime import (
    _model_mission_payload,
    _server_completion_contract,
    _system_prompt,
    _terminal_agent_status,
    reconcile_stale_agent_runs,
)
from app.core.config import settings as runtime_settings
from app.db.models import AgentRun, Journey, JourneyMission, Traveler
from app.db.session import SessionLocal
from app.schemas import AgentToolCall


def test_r21_recovery_envelope_is_capability_neutral():
    plan = build_mission_plan(autonomy="EXECUTE_WITHIN_BOUNDS").as_dict()
    assert [step["id"] for step in plan["steps"][:4]] == [
        "inspect_journey",
        "inspect_twin",
        "inspect_commitments",
        "evaluate_recovery_options",
    ]
    assert all(
        step["tool_name"] not in {"activate_standby", "request_replan"}
        for step in plan["steps"]
    )
    assert "activate_standby" in plan["allowed_tools"]
    assert "request_replan" in plan["allowed_tools"]


def test_r21_material_fingerprint_prunes_only_execution_equivalents():
    base = [
        {
            "place_id": "p1",
            "activity_type": "VISIT",
            "start_at": "2026-10-02T09:00:00+00:00",
            "end_at": "2026-10-02T10:00:00+00:00",
        },
        {
            "place_id": "p2",
            "activity_type": "VISIT",
            "start_at": "2026-10-02T10:30:00+00:00",
            "end_at": "2026-10-02T11:30:00+00:00",
        },
    ]
    same = [dict(item) for item in base]
    retimed = [dict(item) for item in base]
    retimed[1]["start_at"] = "2026-10-02T11:00:00+00:00"
    assert _material_difference_fingerprint(base) == _material_difference_fingerprint(same)
    assert _material_difference_fingerprint(base) != _material_difference_fingerprint(retimed)


def test_r21_stale_running_rows_reconcile_with_named_reason():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=1)).isoformat()
    traveler_id, journey_id, mission_id, run_id = (str(uuid4()) for _ in range(4))
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="R21 stale-run test",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=old,
                updated_at=old,
            )
        )
        session.flush()
        session.add(
            Journey(
                id=journey_id,
                traveler_id=traveler_id,
                title="R21 stale-run test",
                city_code="R21-TEST",
                timezone="Asia/Shanghai",
                start_at=old,
                end_at=(now + timedelta(days=1)).isoformat(),
                budget_currency="CNY",
                budget_amount_minor=100000,
                planning_mode="balanced",
                status="ACTIVE",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=old,
                updated_at=old,
            )
        )
        session.flush()
        session.add(
            JourneyMission(
                id=mission_id,
                journey_id=journey_id,
                traveler_id=traveler_id,
                user_text="R21 stale-run test",
                status="RUNNING",
                requested_autonomy="EXECUTE_WITHIN_BOUNDS",
                created_at=old,
                started_at=old,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=run_id,
                mission_id=mission_id,
                journey_id=journey_id,
                provider_key="HERMES_COMPAT_ENDPOINT",
                model_key="cn:glm-5.3-flash",
                status="RUNNING",
                created_at=old,
                started_at=old,
            )
        )
        session.commit()
        assert reconcile_stale_agent_runs(session, timeout_seconds=60, now=now) == {
            "agent_runs_interrupted": 1,
            "missions_aborted": 1,
        }
        run = session.get(AgentRun, run_id)
        mission = session.get(JourneyMission, mission_id)
        assert run.status == "INTERRUPTED"
        assert mission.status == "ABORTED"
        assert run.failure_reason == "PROCESS_RESTARTED_DURING_AGENT_RUN"
        assert json.loads(run.outcome) == {
            "status": "INTERRUPTED",
            "reason_code": "PROCESS_RESTARTED_DURING_AGENT_RUN",
            "user_summary": "The Agent run was interrupted when the server restarted.",
            "recommended_action": "Start a new mission after inspecting the current Journey state.",
        }
        session.delete(run)
        session.delete(mission)
        session.delete(session.get(Journey, journey_id))
        session.delete(session.get(Traveler, traveler_id))
        session.commit()


def test_r21_hermes_compat_is_not_reported_as_ops_disabled(monkeypatch):
    monkeypatch.setattr(
        "app.agent.ai_availability.settings",
        replace(
            runtime_settings,
            ai_enabled=False,
            ai_api_key=None,
            hermes_api_url="http://127.0.0.1:7863/v1/chat/completions",
            hermes_api_key="process-only-test-key",
        ),
    )
    assert ai_availability_state() == "CONFIGURED_UNVERIFIED"


def test_r21_model_context_does_not_leak_trigger_identity_or_strategy_hint(monkeypatch):
    mission = SimpleNamespace(
        id="mission-r21-neutral",
        trigger_event_id="event-secret-to-model-context",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
    )
    journey = SimpleNamespace(id="journey-r21-neutral", traveler_id="traveler-r21-neutral")
    monkeypatch.setattr("app.agent.runtime.refresh_snapshot", lambda session, traveler_id: {})
    monkeypatch.setattr("app.agent.runtime.snapshot_payload", lambda snapshot: {})
    monkeypatch.setattr(
        "app.agent.runtime.mission_payload",
        lambda value: {
            "id": value.id,
            "requested_autonomy": value.requested_autonomy,
            "trigger_event_id": value.trigger_event_id,
        },
    )

    assert "trigger_event_id" not in _model_mission_payload(mission)
    prompt = _system_prompt(None, mission, journey)

    assert "event-secret-to-model-context" not in prompt
    assert "candidate_count to 3" not in prompt
    assert "solver_mode to deterministic" not in prompt
    assert "discoverable from the server-owned JourneyTwin" in prompt


def test_r21_inspect_twin_discovers_latest_actionable_event_without_mission_binding():
    journey = SimpleNamespace(id=str(uuid4()))
    event = SimpleNamespace(
        id="event-r21-newest",
        event_type="CLOSURE",
        provenance="USER_REPORTED",
        severity="MAJOR",
        affected_activity_id="activity-r21-affected",
    )
    session = MagicMock()
    session.get.return_value = journey
    session.scalars.return_value.all.return_value = [event]
    savepoint = MagicMock()
    session.begin_nested.return_value = savepoint
    request = AgentToolCall(
        request_id="r21-event-discovery",
        agent_id="hermes-compat",
        journey_id=journey.id,
    )

    with patch("app.main._twin_snapshot", return_value={"latest_event_id": event.id}), patch(
        "app.agent.predictive.standby_portfolio", return_value=[]
    ):
        result = AgentToolGateway(session).inspect_twin(request)

    assert result["current_event"] == {
        "id": event.id,
        "event_type": "CLOSURE",
        "provenance": "USER_REPORTED",
        "severity": "MAJOR",
        "affected_activity_id": "activity-r21-affected",
    }
    session.scalars.assert_called_once()


def test_r21_latest_event_scope_is_server_selected_when_mission_has_no_event():
    event = SimpleNamespace(id="event-r21-current")
    session = MagicMock()
    session.scalars.return_value.all.return_value = [event]

    assert _latest_actionable_event(session, "journey-r21-current") is event
    session.scalars.assert_called_once()


def test_r21_multiple_actionable_events_require_explicit_selection():
    events = [
        SimpleNamespace(
            id="event-r21-newer",
            event_type="TRAFFIC_DELAY",
            severity="MAJOR",
            affected_activity_id="activity-newer",
            provenance="PROVIDER_VERIFIED",
        ),
        SimpleNamespace(
            id="event-r21-older",
            event_type="CLOSURE",
            severity="CRITICAL",
            affected_activity_id="activity-older",
            provenance="USER_REPORTED",
        ),
    ]
    session = MagicMock()
    session.scalars.return_value.all.return_value = events

    selected, available, error = _resolve_actionable_event(
        session, "journey-r21-current"
    )

    assert selected is None
    assert available == events
    assert error == "EVENT_SELECTION_REQUIRED"
    assert _latest_actionable_event(session, "journey-r21-current") is None


def test_r21_completion_contract_does_not_treat_observation_as_resolution():
    from app.agent.runtime import _server_completion_contract

    steps = [
        {"tool_name": "inspect_journey", "result": {}, "status": "COMPLETED"},
        {"tool_name": "inspect_twin", "result": {}, "status": "COMPLETED"},
        {"tool_name": "list_replans", "result": {"total": 0}, "status": "COMPLETED"},
    ]
    assert _server_completion_contract(steps, run_status="COMPLETED") == {
        "kind": "UNRESOLVED",
        "source": "server_trace",
        "reason_code": "NO_TYPED_RECOVERY_CONCLUSION",
    }


def test_r21_model_prose_cannot_persist_a_completed_mission():
    steps = [
        {
            "tool_name": "inspect_journey",
            "result": {"journey_id": "journey-r21-prose-only"},
            "status": "COMPLETED",
        },
        {
            "tool_name": "list_replans",
            "result": {"proposals": []},
            "status": "COMPLETED",
        },
    ]
    assert _terminal_agent_status(steps, has_final_text=True, clarification=False) == (
        "PARTIAL",
        "PARTIAL",
    )


def test_r21_guardian_does_not_close_an_unresolved_model_run():
    assert _guardian_terminal_status(
        action_status="UNRESOLVED", agent_status="PARTIAL"
    ) == ("PARTIAL", "PARTIAL")
    assert _guardian_terminal_status(
        action_status="EXECUTED", agent_status="COMPLETED"
    ) == ("COMPLETED", "COMPLETED")


def test_r21_no_action_completion_is_machine_visible():
    from app.agent.runtime import _server_completion_contract

    steps = [
        {"tool_name": "inspect_twin", "result": {}, "status": "COMPLETED"},
        {
            "tool_name": "conclude_recovery",
            "result": {"requested_outcome": "NO_ACTION"},
            "status": "COMPLETED",
        },
    ]
    assert _server_completion_contract(steps, run_status="COMPLETED") == {
        "kind": "NO_ACTION",
        "source": "server_trace",
    }
