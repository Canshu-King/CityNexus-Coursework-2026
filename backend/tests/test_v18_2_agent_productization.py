from __future__ import annotations

from app.agent.gateway import AgentToolGateway
from app.agent.mission_plan import build_mission_plan
from app.agent.runtime_events import NORMALIZED_EVENT_TYPES, normalize_hermes_event
from app.agent.contracts import AuthorizationDecision, AuthorizationResult
from app.schemas import AgentToolCall
from app.db.session import SessionLocal


def test_v182_plan_is_explicit_and_plan_only_limits_native_surface():
    plan = build_mission_plan(
        autonomy="PLAN_ONLY",
        protected_activity_ids=["cruise-activity"],
        objective_codes=["RECOVER_FROM_CITY_DISRUPTION", "PREFER_INDOOR"],
    ).as_dict()

    assert [step["id"] for step in plan["steps"]] == [
        "inspect_journey",
        "inspect_twin",
        "inspect_commitments",
        "inspect_affected",
        "search_candidates",
        "evaluate_candidates",
        "request_replan",
        "inspect_proposal",
        "approval_boundary",
    ]
    assert plan["allowed_tools"] == [
        "inspect_journey",
        "inspect_twin",
        "list_replans",
        "search_city_services",
        "request_replan",
    ]
    assert "accept_replan" in plan["approval_required_tools"]
    assert plan["protected_activity_ids"] == ["cruise-activity"]

    with SessionLocal() as session:
        gateway = AgentToolGateway(session)
        request = AgentToolCall(request_id="native-request", agent_id="mcp-client")
        auth = AuthorizationResult(AuthorizationDecision.ALLOW_AUTO, (), 7)
        bounded = gateway._bound_authorization(request, auth)
    assert bounded.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert "CITYNEXUS_NATIVE_PLAN_ONLY_BOUNDARY" in bounded.reason_codes


def test_v182_normalized_contract_preserves_safe_tool_metadata_only():
    event = normalize_hermes_event(
        {
            "event": "tool.completed",
            "run_id": "run-golden",
            "tool": "mcp__citynexus__search_city_services",
            "timestamp": "2026-09-15T09:00:01Z",
            "duration": 0.125,
            "result": {
                "decision": "ALLOW_AUTO",
                "approval_status": "PENDING",
                "proposal_id": "proposal-1",
                "secret": "must-not-escape",
                "places": [{"name": "Indoor Museum"}],
            },
        }
    )
    assert event["event_type"] == "tool.completed"
    assert event["tool_name"] == "search_city_services"
    assert event["friendly_label"] == "Search candidate places and services"
    assert event["duration_ms"] == 125
    assert event["approval_status"] == "PENDING"
    assert event["result_summary"] == {
        "decision": "ALLOW_AUTO",
        "approval_status": "PENDING",
        "proposal_id": "proposal-1",
    }
    assert "secret" not in str(event)
    assert NORMALIZED_EVENT_TYPES.issuperset(
        {
            "semantic.interpreted",
            "mission.planned",
            "run.started",
            "tool.started",
            "tool.completed",
            "tool.failed",
            "gateway.checked",
            "guardian.checked",
            "proposal.created",
            "approval.required",
            "run.completed",
            "run.failed",
            "run.stopped",
        }
    )


def test_v182_citynexus_stage_events_keep_source_distinct_from_hermes():
    event = normalize_hermes_event(
        {
            "event_type": "mission.planned",
            "run_id": "run-golden",
            "source": "CITYNEXUS",
            "result": {"step_count": 9, "autonomy": "PLAN_ONLY"},
        }
    )
    assert event["event_type"] == "mission.planned"
    assert event["source"] == "CITYNEXUS"
    assert event["result_summary"] == {"keys": ["autonomy", "step_count"]}


def test_v182_runtime_contract_drops_token_level_model_deltas():
    assert normalize_hermes_event({"event": "message.delta", "run_id": "run-golden"}) is None
