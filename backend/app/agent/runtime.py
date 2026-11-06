from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.gateway import AgentToolGateway
from app.agent.mission import mission_payload
from app.agent.runtime_identity import NATIVE_DEEPSEEK_TOOL_LOOP, runtime_identity
from app.agent.tool_catalog import RUNTIME_MCP_ALLOWLIST_V2
from app.core.audit import record_audit
from app.core.config import settings
from app.agent.ai_availability import (
    ai_availability_state,
    ai_state_from_failure_reason,
    reason_sentence,
)
from app.db.models import AgentRun, AgentRunStep, Journey, JourneyMission
from app.execution.clock import execution_clock
from app.execution.traveler_twin import refresh_snapshot, snapshot_payload
from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.observability.tracing import persist_span
from app.schemas import AgentToolCall


class AgentRuntimeUnavailable(RuntimeError):
    """The configured model boundary cannot be used for this run."""


_INSPECTION_TOOLS = {"inspect_journey", "inspect_twin", "list_replans"}
_MUTATION_TOOLS = RUNTIME_MCP_ALLOWLIST_V2 - _INSPECTION_TOOLS - {"search_city_services"}


def _property(kind: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": kind, **extra}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            },
        },
    }


def citynexus_tool_definitions() -> list[dict[str, Any]]:
    journey = {"journey_id": _property("string", description="Server-owned Journey UUID")}
    return [
        _tool(
            "inspect_journey",
            "Inspect the persisted Journey before acting.",
            journey,
            ["journey_id"],
        ),
        _tool(
            "inspect_twin", "Inspect the current JourneyTwin projection.", journey, ["journey_id"]
        ),
        _tool(
            "list_replans",
            "List existing server-generated recovery proposals.",
            journey,
            ["journey_id"],
        ),
        _tool(
            "search_city_services",
            "Search server-owned CityService alternatives; never invent candidate IDs.",
            {
                **journey,
                "q": _property("string"),
                "category": _property("string"),
                "service_type": _property("string"),
            },
            ["journey_id"],
        ),
        _tool(
            "request_replan",
            "Generate deterministic feasible recovery proposals for the disruption.",
            {
                **journey,
                "trigger_event_id": _property("string"),
                "candidate_count": _property("integer", minimum=1, maximum=5),
            },
            ["journey_id", "trigger_event_id"],
        ),
        _tool(
            "accept_replan",
            "Accept a feasible proposal only when Gateway policy permits it.",
            {
                **journey,
                "proposal_id": _property("string"),
                "approval_id": _property("string"),
            },
            ["journey_id", "proposal_id"],
        ),
        _tool(
            "replace_activity",
            "Replace a mutable Activity through policy and deterministic feasibility.",
            {
                **journey,
                "activity_id": _property("string"),
                "replacement_activity_id": _property("string"),
                "replacement_place_id": _property(
                    "string",
                    description="Server-owned Place candidate returned by search_city_services",
                ),
                "approval_id": _property("string"),
            },
            ["journey_id", "activity_id"],
        ),
        _tool(
            "reschedule_activity",
            "Reschedule a mutable Activity through policy and deterministic feasibility.",
            {
                **journey,
                "activity_id": _property("string"),
                "start_at": _property("string"),
                "end_at": _property("string"),
                "approval_id": _property("string"),
            },
            ["journey_id", "activity_id", "start_at", "end_at"],
        ),
        _tool(
            "create_booking",
            "Create a provider booking only when the server policy permits it.",
            {
                **journey,
                "activity_id": _property("string"),
                "provider_key": _property("string"),
                "approval_id": _property("string"),
            },
            ["journey_id", "activity_id"],
        ),
        _tool(
            "cancel_refundable_booking",
            "Cancel a refundable booking only when the server policy permits it.",
            {
                **journey,
                "booking_id": _property("string"),
                "approval_id": _property("string"),
            },
            ["journey_id", "booking_id"],
        ),
        _tool(
            "request_commitment_change",
            "Request a protected Commitment change; the Gateway may require stronger proof.",
            {
                **journey,
                "commitment_id": _property("string"),
                "reason": _property("string"),
                "approval_id": _property("string"),
            },
            ["journey_id", "commitment_id", "reason"],
        ),
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _model_name() -> str:
    return settings.ai_model.removeprefix("deepseek/")


def _system_prompt(session: Session, mission: JourneyMission, journey: Journey) -> str:
    traveler_twin = snapshot_payload(refresh_snapshot(session, journey.traveler_id))
    # The agent reasons in words like "now", "tonight" and "before arrival", so it has to be told
    # which clock those words run on and in which zone -- otherwise each of them is ambiguous.  The
    # clock reports the zone it actually used, so an unusable journey timezone is stated here rather
    # than left for the model to assume.
    now, used_timezone, honoured = execution_clock.now_honest(journey.timezone)
    zone_line = (
        f"Now (the moment you are acting in): {now.isoformat()}"
        f"{'' if honoured else ' -- this is UTC, because the journey timezone could not be used'}\n"
        f"Journey time zone: {journey.timezone}"
        f"{'' if honoured else ' (unusable; every moment below is UTC)'}\n"
        f"Journey window: {journey.start_at} to {journey.end_at} in that zone.\n"
        "Name the zone whenever you name a moment. "
    )
    return (
        "You are the CityNexus Delegated Autonomous Journey Agent. "
        "Use only the eleven CityNexus tools supplied by the server. "
        "Inspect the Journey and JourneyTwin before any mutation. "
        "Never invent IDs, prices, opening hours, provider status, or commitments. "
        "The Gateway and deterministic feasibility layer are authoritative: follow their decisions. "
        "You may autonomously act on ordinary reversible changes inside both the global policy and this mission. "
        "Protected activities and commitments must remain unchanged unless the Gateway explicitly permits a stronger action. "
        "Do not reveal private chain-of-thought; final text should summarize actionable rationale and observed results.\n\n"
        f"{zone_line}"
        f"Journey ID: {journey.id}\n"
        f"Mission: {_json_text(mission_payload(mission))}\n"
        f"TravelerTwin soft context (not authority): {_json_text(traveler_twin)}\n"
        "Treat LEARNED_SOFT evidence as bounded ranking/explanation context only; "
        "never use it to expand budget, payment, Commitment, or autonomy authority.\n"
        f"Mission trigger event ID: {mission.trigger_event_id}\n"
        "The first tool call should be inspection. "
        + (
            "This is PLAN_ONLY: do not accept or execute a mutating action."
            if mission.requested_autonomy == "PLAN_ONLY"
            else "The mission permits execution only inside the server policy and envelope."
        )
    )


def _tool_args(raw_arguments: str) -> dict[str, Any]:
    parsed = json.loads(raw_arguments or "{}")
    if not isinstance(parsed, dict):
        raise TypeError("model tool arguments must be a JSON object")
    return parsed


def _result_for_model(result: Any) -> str:
    if isinstance(result, dict):
        return _json_text(result)
    return _json_text({"result": result})


def persisted_agent_run_payload(session: Session, run: AgentRun) -> dict[str, Any]:
    mission = session.get(JourneyMission, run.mission_id)
    steps = session.scalars(
        select(AgentRunStep).where(AgentRunStep.run_id == run.id).order_by(AgentRunStep.sequence_no)
    ).all()
    return {
        "run_id": run.id,
        "mission_id": run.mission_id,
        "journey_id": run.journey_id,
        "provider_key": run.provider_key,
        "runtime_identity": (
            NATIVE_DEEPSEEK_TOOL_LOOP
            if run.provider_key == "deepseek"
            else "HERMES_COMPAT_ENDPOINT"
            if run.provider_key == "hermes"
            else runtime_identity()
        ),
        "model_key": run.model_key,
        "status": run.status,
        "outcome": run.outcome,
        "failure_reason": run.failure_reason,
        # The read route answers with the same enumeration the health endpoint uses. A run that
        # stored a reason reports that reason; a run that stored none reports the current state,
        # which is a statement about the switch and the credential, not about this run.
        "ai_state": (
            ai_state_from_failure_reason(run.failure_reason)
            if run.failure_reason
            else ai_availability_state()
        ),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "mission_status": mission.status if mission else None,
        "mission": mission_payload(mission) if mission else None,
        "tool_steps": [
            {
                "id": step.id,
                "sequence_no": step.sequence_no,
                "tool_name": step.tool_name,
                "arguments": json.loads(step.arguments_json),
                "result": json.loads(step.result_json),
                "decision": step.decision,
                "status": step.status,
                "created_at": step.created_at,
                "completed_at": step.completed_at,
            }
            for step in steps
        ],
    }


class DeepSeekJourneyAgent:
    provider_key = "deepseek"
    runtime_identity = NATIVE_DEEPSEEK_TOOL_LOOP

    def __init__(self, *, client: httpx.Client | None = None):
        if not settings.ai_api_key:
            raise AgentRuntimeUnavailable("DeepSeek API key is not configured")
        self.model_key = _model_name()
        self._client = client or httpx.Client(timeout=settings.impact_timeout_seconds)
        self._owns_client = client is None
        self._responses = DeepSeekResponsesClient(
            api_key=settings.ai_api_key,
            model=self.model_key,
            base_url=settings.deepseek_api_url,
            reasoning_effort=settings.deepseek_reasoning_effort,
            timeout_seconds=settings.impact_timeout_seconds,
            client=self._client,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            if not hasattr(self, "_responses"):
                self._responses = DeepSeekResponsesClient(
                    api_key=settings.ai_api_key or "compatibility-test-key",
                    model=self.model_key,
                    base_url=settings.deepseek_api_url,
                    reasoning_effort=getattr(settings, "deepseek_reasoning_effort", "high"),
                    timeout_seconds=settings.impact_timeout_seconds,
                    client=self._client,
                )
            result = self._responses.complete(
                messages,
                tools=citynexus_tool_definitions(),
                reasoning_effort=settings.deepseek_reasoning_effort,
            )
            return {
                "content": result.text,
                "tool_calls": list(result.tool_calls),
                "_response_items": list(result.continuation_items),
            }
        except (DeepSeekResponsesError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise AgentRuntimeUnavailable("DeepSeek Agent completion failed") from exc

    def run(
        self,
        session: Session,
        *,
        journey: Journey,
        mission: JourneyMission,
        max_steps: int = 12,
    ) -> dict[str, Any]:
        now = _now()
        trace_id = str(uuid4())
        run = AgentRun(
            id=str(uuid4()),
            mission_id=mission.id,
            journey_id=journey.id,
            provider_key=self.provider_key,
            model_key=self.model_key,
            status="RUNNING",
            created_at=now,
            started_at=now,
        )
        session.add(run)
        mission.status = "RUNNING"
        mission.started_at = now
        record_audit(
            session,
            action="agent.run.started",
            entity_type="AgentRun",
            entity_id=run.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id="deepseek-agent",
            after={"run_id": run.id, "mission_id": mission.id, "model": self.model_key},
            trace_id=trace_id,
        )
        session.flush()
        persist_span(
            session,
            journey_id=journey.id,
            trace_id=trace_id,
            name="mission.compile",
            attributes={
                "mission_id": mission.id,
                "compiler_provider_key": mission.compiler_provider_key,
                "compiler_model_key": mission.compiler_model_key,
            },
        )
        persist_span(
            session,
            journey_id=journey.id,
            trace_id=trace_id,
            name="traveler_twin.lookup",
            attributes={
                "traveler_id": journey.traveler_id,
                "source": "server_persisted_snapshot",
            },
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(session, mission, journey)},
            {
                "role": "user",
                "content": mission.user_text,
            },
        ]
        inspected = False
        final_text = ""
        sequence = 0
        tool_trace: list[dict[str, Any]] = []
        seen_calls: set[tuple[str, str]] = set()
        try:
            for _ in range(max_steps):
                message = self._complete(messages)
                tool_calls = message.get("tool_calls") or []
                content = message.get("content") or ""
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="deepseek.call",
                    attributes={
                        "provider_key": self.provider_key,
                        "model_key": self.model_key,
                        "tool_call_count": len(tool_calls),
                    },
                )
                if not tool_calls:
                    final_text = str(content)
                    break
                assistant_message = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                    "_response_items": message.get("_response_items") or [],
                }
                messages.append(assistant_message)
                for call in tool_calls:
                    function = call.get("function") or {}
                    tool_name = str(function.get("name") or "")
                    call_id = str(call.get("id") or f"tool-call-{sequence}")
                    arguments = _tool_args(str(function.get("arguments") or "{}"))
                    signature = (tool_name, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                    if signature in seen_calls:
                        result = {
                            "tool": tool_name,
                            "error": "tool loop repetition stopped",
                            "decision": "STOP_REPETITION",
                        }
                        sequence += 1
                        step_now = _now()
                        step = AgentRunStep(
                            id=str(uuid4()), run_id=run.id, sequence_no=sequence,
                            tool_name=tool_name, arguments_json=_json_text(arguments),
                            result_json=_json_text(result), decision="STOP_REPETITION",
                            status="TOOL_ERROR", created_at=step_now, completed_at=step_now,
                        )
                        session.add(step)
                        tool_trace.append({"sequence_no": sequence, "tool_name": tool_name, "arguments": arguments, "result": result, "decision": "STOP_REPETITION", "status": step.status})
                        final_text = "Agent stopped a repeated tool request before another domain action."
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _result_for_model(result)})
                        continue
                    seen_calls.add(signature)
                    sequence += 1
                    arguments.setdefault("journey_id", journey.id)
                    arguments["request_id"] = f"agent-run-{run.id}-{sequence}"
                    if tool_name == "request_replan" and not arguments.get("trigger_event_id"):
                        arguments["trigger_event_id"] = mission.trigger_event_id
                    if tool_name not in RUNTIME_MCP_ALLOWLIST_V2:
                        result: dict[str, Any] = {
                            "tool": tool_name,
                            "error": "tool is outside the CityNexus V2 allowlist",
                        }
                    elif tool_name in _MUTATION_TOOLS and not inspected:
                        result = {
                            "tool": tool_name,
                            "error": "inspect_journey or inspect_twin is required before mutation",
                        }
                    elif mission.requested_autonomy == "PLAN_ONLY" and tool_name in {
                        "accept_replan",
                        "replace_activity",
                        "reschedule_activity",
                        "create_booking",
                        "cancel_refundable_booking",
                        "request_commitment_change",
                    }:
                        result = {
                            "tool": tool_name,
                            "error": "mission is PLAN_ONLY; proposal generation is allowed but mutation is not",
                        }
                    else:
                        try:
                            payload = AgentToolCall.model_validate(arguments)
                            result = AgentToolGateway(session).dispatch(tool_name, payload)
                            if tool_name in _INSPECTION_TOOLS:
                                inspected = True
                        except HTTPException as exc:
                            result = {
                                "tool": tool_name,
                                "error": str(exc.detail),
                                "status_code": exc.status_code,
                            }
                        except (TypeError, ValueError, KeyError) as exc:
                            result = {
                                "tool": tool_name,
                                "error": str(exc),
                                "status_code": 422,
                            }
                    step_now = _now()
                    step = AgentRunStep(
                        id=str(uuid4()),
                        run_id=run.id,
                        sequence_no=sequence,
                        tool_name=tool_name,
                        arguments_json=_json_text(arguments),
                        result_json=_json_text(result),
                        decision=str(result.get("decision")) if isinstance(result, dict) else None,
                        status="COMPLETED" if "error" not in result else "TOOL_ERROR",
                        created_at=step_now,
                        completed_at=step_now,
                    )
                    session.add(step)
                    tool_trace.append(
                        {
                            "sequence_no": sequence,
                            "tool_name": tool_name,
                            "arguments": arguments,
                            "result": result,
                            "decision": result.get("decision")
                            if isinstance(result, dict)
                            else None,
                            "status": step.status,
                            "created_at": step_now,
                            "completed_at": step_now,
                        }
                    )
                    record_audit(
                        session,
                        action="agent.run.tool",
                        entity_type="AgentRun",
                        entity_id=run.id,
                        journey_id=journey.id,
                        actor_type="AI_AGENT",
                        actor_id="deepseek-agent",
                        after={
                            "run_id": run.id,
                            "sequence_no": sequence,
                            "tool": tool_name,
                            "decision": result.get("decision")
                            if isinstance(result, dict)
                            else None,
                            "status": "TOOL_ERROR" if "error" in result else "COMPLETED",
                        },
                        trace_id=trace_id,
                    )
                    persist_span(
                        session,
                        journey_id=journey.id,
                        trace_id=trace_id,
                        name="agent.tool_call",
                        attributes={
                            "tool_name": tool_name,
                            "status": step.status,
                            "decision": result.get("decision")
                            if isinstance(result, dict)
                            else None,
                        },
                    )
                    if tool_name == "search_city_services":
                        persist_span(
                            session,
                            journey_id=journey.id,
                            trace_id=trace_id,
                            name="real_city.discovery",
                            attributes={"tool_name": tool_name},
                        )
                    if tool_name == "request_replan":
                        persist_span(
                            session,
                            journey_id=journey.id,
                            trace_id=trace_id,
                            name="cp_sat.solve",
                            attributes={
                                "solver_mode": arguments.get("solver_mode"),
                                "proposal_id": result.get("proposal", {}).get("id")
                                if isinstance(result.get("proposal"), dict)
                                else None,
                            },
                        )
                    if tool_name in _MUTATION_TOOLS:
                        persist_span(
                            session,
                            journey_id=journey.id,
                            trace_id=trace_id,
                            name="gateway.policy_decision",
                            attributes={
                                "tool_name": tool_name,
                                "decision": result.get("decision")
                                if isinstance(result, dict)
                                else None,
                            },
                        )
                        if isinstance(result, dict) and result.get("executed"):
                            persist_span(
                                session,
                                journey_id=journey.id,
                                trace_id=trace_id,
                                name="domain.mutation",
                                attributes={"tool_name": tool_name, "executed": True},
                            )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _result_for_model(result),
                        }
                    )
                session.flush()
                if final_text:
                    break
            else:
                final_text = "Agent reached the configured tool-step limit."
            run.status = "COMPLETED" if final_text else "PARTIAL"
            mission.status = "COMPLETED" if run.status == "COMPLETED" else "PARTIAL"
            run.outcome = final_text or "Tool loop completed without a final summary."
        except AgentRuntimeUnavailable as exc:
            run.status = "FAILED_EXTERNAL"
            mission.status = "DEGRADED"
            run.failure_reason = str(exc)
            mission.failure_reason = str(exc)
            raise
        finally:
            completed = _now()
            run.completed_at = completed
            mission.completed_at = completed
            record_audit(
                session,
                action="agent.run.completed",
                entity_type="AgentRun",
                entity_id=run.id,
                journey_id=journey.id,
                actor_type="AI_AGENT",
                actor_id="deepseek-agent",
                after={
                    "run_id": run.id,
                    "mission_id": mission.id,
                    "status": run.status,
                    "outcome": run.outcome,
                    "failure_reason": run.failure_reason,
                },
                trace_id=trace_id,
            )
            session.commit()
        return {
            "run_id": run.id,
            "mission_id": mission.id,
            "status": run.status,
            "provider_key": run.provider_key,
            "runtime_identity": self.runtime_identity,
            "model_key": run.model_key,
            "outcome": run.outcome,
            "failure_reason": run.failure_reason,
            "trace_id": trace_id,
            "tool_steps": tool_trace,
        }


def unavailable_run_payload(
    session: Session, *, journey: Journey, mission: JourneyMission
) -> dict[str, Any]:
    now = _now()
    run = AgentRun(
        id=str(uuid4()),
        mission_id=mission.id,
        journey_id=journey.id,
        provider_key="deepseek",
        model_key=_model_name(),
        status="SKIPPED_EXTERNAL_DEPENDENCY",
        # r2 stored this fixed literal no matter why the door above declined. The word now comes
        # from the same table `/health` reads, so a run can never claim a missing credential while
        # the operator's switch is what the server actually reports.
        failure_reason=reason_sentence(ai_availability_state()),
        created_at=now,
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    mission.status = "SKIPPED_EXTERNAL_DEPENDENCY"
    mission.failure_reason = run.failure_reason
    mission.completed_at = now
    record_audit(
        session,
        action="agent.run.skipped",
        entity_type="AgentRun",
        entity_id=run.id,
        journey_id=journey.id,
        actor_type="AI_AGENT",
        actor_id="deepseek-agent",
        after={"run_id": run.id, "mission_id": mission.id, "reason": run.failure_reason},
    )
    session.commit()
    return persisted_agent_run_payload(session, run)
