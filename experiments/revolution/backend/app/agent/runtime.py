from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.ai_availability import (
    ai_availability_state,
    ai_state_from_failure_reason,
    reason_sentence,
)
from app.agent.gateway import AgentToolGateway
from app.agent.mission import mission_payload
from app.agent.runtime_identity import NATIVE_DEEPSEEK_TOOL_LOOP, runtime_identity
from app.agent.tool_catalog import RUNTIME_MCP_ALLOWLIST_V2
from app.core.audit import record_audit
from app.core.config import settings
from app.db.models import (
    AgentRun,
    AgentRunStep,
    Journey,
    JourneyMission,
    RecoveryEpoch,
)
from app.recovery_epoch import (
    COMMITTED,
    RECOVERY_EPOCH_SUPERSEDED,
    RecoveryEpochSuperseded,
    ensure_recovery_epoch,
    validate_recovery_epoch_binding,
)
from app.execution.traveler_twin import refresh_snapshot, snapshot_payload
from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.observability.tracing import persist_span
from app.schemas import AgentToolCall


class AgentRuntimeUnavailable(RuntimeError):
    """The configured model boundary cannot be used for this run."""


def reconcile_stale_agent_runs(
    session: Session, *, timeout_seconds: int = 900, now: datetime | None = None
) -> dict[str, int]:
    """Close model work left RUNNING by a process restart."""

    clock = now or datetime.now(timezone.utc)
    cutoff = clock.timestamp() - max(1, int(timeout_seconds))

    def old_enough(value: str | None) -> bool:
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() <= cutoff
        except (TypeError, ValueError):
            return True

    reason = "PROCESS_RESTARTED_DURING_AGENT_RUN"
    interrupted = 0
    missions_aborted = 0
    runs = session.scalars(select(AgentRun).where(AgentRun.status == "RUNNING")).all()
    for run in runs:
        if not old_enough(run.started_at or run.created_at):
            continue
        run.status = "INTERRUPTED"
        run.failure_reason = reason
        run.outcome = json.dumps(
            _structured_failure_outcome(
                status="INTERRUPTED",
                reason_code="PROCESS_RESTARTED_DURING_AGENT_RUN",
                user_summary="The Agent run was interrupted when the server restarted.",
                recommended_action="Start a new mission after inspecting the current Journey state.",
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
        run.completed_at = clock.isoformat()
        mission = session.get(JourneyMission, run.mission_id)
        if mission is not None and mission.status in {"CREATED", "RUNNING"}:
            mission.status = "ABORTED"
            mission.failure_reason = reason
            mission.completed_at = clock.isoformat()
            missions_aborted += 1
        interrupted += 1

    # A crash can happen after a mission is marked RUNNING but before its
    # AgentRun row is flushed.
    missions = session.scalars(
        select(JourneyMission).where(JourneyMission.status == "RUNNING")
    ).all()
    for mission in missions:
        if not old_enough(mission.started_at or mission.created_at):
            continue
        live_run = session.scalar(
            select(AgentRun).where(
                AgentRun.mission_id == mission.id,
                AgentRun.status == "RUNNING",
            )
        )
        if live_run is not None:
            continue
        mission.status = "ABORTED"
        mission.failure_reason = reason
        mission.completed_at = clock.isoformat()
        missions_aborted += 1

    if interrupted or missions_aborted:
        session.commit()
    return {"agent_runs_interrupted": interrupted, "missions_aborted": missions_aborted}


_INSPECTION_TOOLS = {"inspect_journey", "inspect_twin", "list_replans"}
_NON_DOMAIN_TOOLS = {"search_city_services", "discover_recovery_options", "research_place_sources", "conclude_recovery"}
_MUTATION_TOOLS = RUNTIME_MCP_ALLOWLIST_V2 - _INSPECTION_TOOLS - _NON_DOMAIN_TOOLS
_RECOVERY_CONCLUSIONS = {
    "MUTATION_EXECUTED",
    "APPROVAL_PENDING",
    "NO_ACTION",
    "UNRESOLVED",
}


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
            "inspect_twin",
            "Inspect the current JourneyTwin, server-owned standby portfolio, and current trigger compatibility.",
            {**journey, "trigger_event_id": _property("string")},
            ["journey_id"],
        ),
        _tool(
            "list_replans",
            "List existing server-generated recovery proposals.",
            {**journey, "trigger_event_id": _property("string")},
            ["journey_id"],
        ),
        _tool(
            "conclude_recovery",
            "Record the server-owned recovery conclusion after inspection and any chosen action. Prose alone is not a machine-resolved outcome.",
            {
                **journey,
                "outcome": _property(
                    "string",
                    enum=[
                        "MUTATION_EXECUTED",
                        "APPROVAL_PENDING",
                        "NO_ACTION",
                        "UNRESOLVED",
                        "CLARIFICATION_REQUIRED",
                    ],
                ),
                "reason": _property("string"),
                "horizon_decision": _property(
                    "string", enum=["TAKE_NOW", "DEFER", "NO_ACTION"]
                ),
                "question": _property("string", description="One human-value question; never a provider fact request."),
                "option_keys": _property("array", items=_property("string")),
                "preference_dimension": _property("string"),
            },
            ["journey_id", "outcome"],
        ),
        _tool(
            "apply_clarified_horizon_choice",
            "Apply the server-derived current-vs-later answer. The server resolves the exact current activity; the model cannot provide an activity ID.",
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
            "discover_recovery_options",
            "Expand the current recovery surface with a bounded provider-backed city search. Describe the kind of alternative that would help; the server derives the anchor, radius, provider identity, and Place records.",
            {
                **journey,
                "trigger_event_id": _property("string"),
                "mission_id": _property("string"),
                "q": _property("string"),
                "category": _property("string"),
                "radius_m": _property("integer", minimum=250, maximum=5000),
                "discovery_limit": _property("integer", minimum=1, maximum=8),
            },
            ["journey_id", "trigger_event_id", "mission_id", "q"],
        ),
        _tool(
            "research_place_sources",
            "Research up to five non-authoritative source leads for one server-owned Place. The server builds the query; results never become facts by themselves.",
            {**journey, "place_id": _property("string")},
            ["journey_id", "place_id"],
        ),
        _tool(
            "acquire_place_facts",
            "Acquire provider-backed facts for one existing Place; missing facts remain UNKNOWN.",
            {
                **journey,
                "place_id": _property("string"),
                "source_url": _property("string"),
                "source_observation_id": _property("string", description="Server-owned non-authoritative WebObservation reference"),
                "source_class": _property(
                    "string",
                    enum=[
                        "OFFICIAL_WEB",
                        "OFFICIAL_OPERATOR",
                        "OFFICIAL_TICKETING",
                        "MUNICIPAL_AUTHORITY",
                    ],
                    description="Use only a verified official or municipal source class; never use OSM/provider as an official source.",
                ),
            },
            ["journey_id", "place_id"],
        ),
        _tool(
            "estimate_place_facts",
            "Estimate opening and cost for strategy comparison. In the coursework lab, the Agent may provide bounded hypotheses when peer evidence is absent; each remains ESTIMATED_AGENT, never canonical/provider truth, and can support selection only inside the explicit isolated lab estimate envelope after live mobility and deterministic gates pass.",
            {
                **journey,
                "place_id": _property("string"),
                "estimated_open_minutes": _property("integer", minimum=0, maximum=1440),
                "estimated_close_minutes": _property("integer", minimum=0, maximum=1440),
                "estimated_cost_minor": _property("integer", minimum=0),
            },
            ["journey_id", "place_id"],
        ),
        _tool(
            "generate_plans",
            "Generate deterministic candidate plans from server-owned Place IDs. This creates candidates only; it never selects or activates a plan.",
            {
                **journey,
                "place_ids": _property("array", items=_property("string")),
                "candidate_count": _property("integer", minimum=1, maximum=5),
                "planning_mode": _property("string"),
            },
            ["journey_id", "place_ids"],
        ),
        _tool(
            "request_replan",
            "Generate deterministic feasible recovery proposals for the disruption.",
            {
                **journey,
                "trigger_event_id": _property("string"),
                "candidate_count": _property("integer", minimum=1, maximum=5),
                "place_ids": _property(
                    "array",
                    items=_property("string"),
                    description="Optional bounded Place hints from search_city_services; the server revalidates every fact.",
                ),
            },
            ["journey_id", "trigger_event_id"],
        ),
        _tool(
            "request_collective_decision",
            "Create a server-derived shared member decision only when presented grounded proposals leave a real human value trade-off. The server chooses the options and required members; the model supplies no candidate IDs or social policy.",
            {
                **journey,
                "mission_id": _property("string"),
                "trigger_event_id": _property("string"),
            },
            ["journey_id", "mission_id", "trigger_event_id"],
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
            "activate_standby",
            "Activate one server-owned STANDBY branch through its dedicated Gateway boundary; never use generic select_plan.",
            {**journey, "standby_plan_id": _property("string"), "trigger_event_id": _property("string"), "approval_id": _property("string")},
            ["journey_id", "standby_plan_id", "trigger_event_id"],
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


def _resolved_mission_intent(mission: JourneyMission) -> dict[str, Any]:
    try:
        resolved = json.loads(mission.resolved_intent_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return resolved if isinstance(resolved, dict) else {}


def _resolved_collective_proposal_id(mission: JourneyMission) -> str | None:
    """Return the server-resolved proposal selected by authenticated members."""
    collective = _resolved_mission_intent(mission).get("collective_decision")
    if not isinstance(collective, dict):
        return None
    proposal_id = str(collective.get("selected_proposal_id") or "").strip()
    return proposal_id or None


def _collective_mediation_requested(mission: JourneyMission) -> bool:
    mediation = _resolved_mission_intent(mission).get("collective_mediation")
    return (
        isinstance(mediation, dict)
        and mediation.get("kind") == "DISAGREEMENT_MEDIATION"
    )


def _acceptance_requested(mission: JourneyMission) -> bool:
    """Whether server-owned mission state requires the Agent to finish acceptance."""
    text = str(mission.user_text or "").lower()
    if mission.requested_autonomy == "PLAN_ONLY":
        return False
    if "不接受" in text or "do not accept" in text:
        return False
    if _resolved_collective_proposal_id(mission):
        return True
    return (
        "accept_replan" in text
        and any(token in text for token in ("接受", "选择", "select", "accept"))
    )


def _system_prompt(session: Session, mission: JourneyMission, journey: Journey) -> str:
    traveler_twin = snapshot_payload(refresh_snapshot(session, journey.traveler_id))
    try:
        objective_codes = set(json.loads(mission.objective_codes_json or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        objective_codes = set()
    return (
        "You are the CityNexus Delegated Autonomous Journey Agent. "
        "Use only the CityNexus tools supplied by the server. "
        "Inspect the Journey and JourneyTwin before any mutation. "
        "Never invent IDs, prices, opening hours, provider status, or commitments. "
        "The Gateway and deterministic feasibility layer are authoritative: follow their decisions. "
        "You may autonomously act on ordinary reversible changes inside both the global policy and this mission. "
        "Protected activities and commitments must remain unchanged unless the Gateway explicitly permits a stronger action. "
        "A mutation is not complete merely because you described it: only the corresponding CityNexus tool response is evidence that it happened. "
        "If inspect_twin returns decision_sensitivity.decision_sensitive=true, do not guess the traveller's value choice. "
        "Call conclude_recovery with outcome CLARIFICATION_REQUIRED, one concise human-value question, and the exact server option_keys. "
        "Ask no question about opening, cost, or mobility; acquire those facts yourself. If decision_sensitive is false, do not ask. "
        "Use the server-returned action tools when their current facts and authority permit; never claim that a mutation happened without a tool receipt. "
        "For every mutating tool, summarize the returned executed/decision/result fields and never substitute model prose for the Gateway receipt. "
        "Do not reveal private chain-of-thought; final text should summarize actionable rationale and observed results.\n\n"
        f"Journey ID: {journey.id}\n"
        f"Mission: {_json_text(_model_mission_payload(mission))}\n"
        f"TravelerTwin soft context (not authority): {_json_text(traveler_twin)}\n"
        "Treat LEARNED_SOFT evidence as bounded ranking/explanation context only; "
        "never use it to expand budget, payment, Commitment, or autonomy authority.\n"
        "The current disruption, if any, is discoverable from the server-owned JourneyTwin and event inspection; "
        "do not assume or repeat an event identifier from mission text.\n"
        "If inspection exposes multiple actionable events, do not use recency as relevance: choose one by passing its event_id to inspect_twin or list_replans. "
        "If a child mission contains an explicit TAKE_LATER answer, call apply_clarified_horizon_choice to enact the server-derived skip before concluding MUTATION_EXECUTED; do not merely conclude NO_ACTION. "
        "If a child mission contains an explicit TAKE_NOW answer, do not call apply_clarified_horizon_choice: that tool is only for TAKE_LATER. Keep the current activity and conclude NO_ACTION with horizon_decision TAKE_NOW; the normal execution state machine continues the current activity. "
        "If the bounded remaining-opportunity horizon shows a feasible current window but a higher-value later window would consume the same optional budget, you may choose DEFER by calling conclude_recovery with outcome NO_ACTION and horizon_decision DEFER only before a clarification answer exists. "
        "At the end of the recovery attempt, call conclude_recovery with exactly one server-owned outcome: "
        "MUTATION_EXECUTED, APPROVAL_PENDING, NO_ACTION, CLARIFICATION_REQUIRED, or UNRESOLVED; prose alone never resolves the recovery mission.\n"
        "The first tool call should be inspection. "
        "After inspection, choose the safest available CityNexus action within the server-returned capabilities and authority envelope; do not assume an existing future is sufficient, and do not mutate when no intervention is needed. "
        "After real Place IDs are returned, you may acquire or estimate their facts and call generate_plans; generation creates candidates only and never selects or activates them. "
        "Issue at most one tool call per assistant turn. "
        "If a tool reports SEARCH_SATURATION or SEARCH_BATCH_LIMIT, stop searching and choose a different available action; use real Place IDs already returned by the completed search as candidate hints for acquire_place_facts or generate_plans, rather than repeating the same query or changing only the query spelling. "
        "For a shared Journey, inspect_journey and inspect_twin expose separate in-force members, role capabilities, journey-scoped preferences, and compact learned-soft context. Never treat the owner TravelerTwin as the group. "
        "Use the server-returned per-member fit matrix and mediation classification: seek a grounded compromise before asking people, never majority-vote, and never ask a member about opening, cost, mobility, or provider identity. "
        "A compromise candidate must be revalidated by CityNexus for every member; member IDs and Place IDs in tool results are evidence to evaluate, not permission to mutate. "
        "For shared Journeys, treat the server-returned member fit, mediation classification, and collective authority receipt as the authority boundary. Preserve every member's explicit choice, never majority-vote or invent member IDs, and use available tools to gather facts or present unresolved authority without prescribing a tool order. "
        + (
            "For R61 re-grounding, use only proposals returned for this current RecoveryEpoch. A stale prior proposal or member response is history, never a current option. Open a new collective decision only when current grounded proposals return HUMAN_TRADEOFF_REQUIRED; otherwise use the typed NO_ACTION conclusion to record the server-derived current result. This mission is PLAN_ONLY and must never mutate the Journey. "
            if "COLLECTIVE_RECOVERY_REGROUND" in objective_codes
            else ""
        )
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


def _model_mission_payload(mission: JourneyMission) -> dict[str, Any]:
    """Keep server-owned trigger identity out of model-visible mission text."""

    payload = dict(mission_payload(mission))
    payload.pop("trigger_event_id", None)
    return payload


def _compact_replan_for_model(item: Any) -> Any:
    """Keep proposal choice facts while removing route geometry from model context.

    The gateway persists the complete evidence receipt in ``AgentRunStep``.  The
    model only needs the proposal identity, deterministic score, feasibility
    deltas, and provider/provenance summaries to choose the next tool.  Feeding
    every captured polyline back into the next model turn made a real recovery
    finish with an empty response before it could call ``accept_replan``.
    """
    if isinstance(item, list):
        return [_compact_replan_for_model(value) for value in item]
    if not isinstance(item, dict):
        return item
    if "snapshot_id" in item and isinstance(item.get("facts"), list):
        fact_fields = {
            "origin_key",
            "destination_key",
            "minutes",
            "provider",
            "captured_at",
            "degraded",
            "fallback_reason",
            "route_provenance",
            "fact_quality",
            "mode",
            "time_basis",
            "distance_m",
            "walking_distance_m",
            "walking_min",
        }
        return {
            key: value
            for key, value in item.items()
            if key != "facts"
        } | {
            "facts": [
                {key: value for key, value in fact.items() if key in fact_fields}
                for fact in item["facts"]
                if isinstance(fact, dict)
            ]
        }
    projected = dict(item)
    for key in (
        "route_geometry",
        "route_geometry_json",
        "encoded_polyline",
        "raw_response",
    ):
        projected.pop(key, None)
    # The persisted diff can contain full activity and evidence receipts.  The
    # model needs the proposal identity and the server's feasibility/collective
    # decision, not a second copy of route geometry or every raw observation.
    # Keep only the bounded fields that can change the next tool decision.
    if isinstance(projected.get("diff"), dict):
        source_diff = projected["diff"]
        projected["diff"] = {
            key: _compact_replan_for_model(source_diff.get(key))
            for key in (
                "changed_activity_ids",
                "added_activity_ids",
                "removed_activity_ids",
                "fit_subject_place_ids",
                "fit_subject_tags",
                "fit_subject_basis",
                "cost_delta_minor",
                "duration_delta_min",
                "travel_delta_min",
                "candidate_family",
                "verified_evidence",
            )
            if key in source_diff
        }
    diff = projected.get("diff")
    if isinstance(diff, dict):
        compact_diff = dict(diff)
        compact_diff.pop("opportunity_window_evidence", None)
        evidence = compact_diff.get("verified_evidence")
        if isinstance(evidence, dict):
            compact_evidence = {
                key: value
                for key, value in evidence.items()
                if key not in {"mobility_snapshot", "route_geometry", "route_geometry_json"}
            }
            compact_diff["verified_evidence"] = compact_evidence
        compact_diff = {key: _compact_replan_for_model(value) for key, value in compact_diff.items()}
        projected["diff"] = compact_diff
    explanation = projected.get("explanation")
    if isinstance(explanation, dict):
        projected["explanation"] = {
            key: value
            for key, value in explanation.items()
            if key in {"summary", "reasons", "impact_type", "impact_provider", "source"}
        }
    return {
        key: _compact_replan_for_model(value)
        for key, value in projected.items()
        if key not in {
            "route_geometry",
            "route_geometry_json",
            "encoded_polyline",
            "raw_response",
            "score_breakdown",
        }
    }


def _result_for_model(result: Any) -> str:
    if isinstance(result, dict):
        projected = dict(result)
        if projected.get("tool") in {"list_replans", "request_replan"} and isinstance(projected.get("proposals"), list):
            projected["proposals"] = [
                _compact_replan_for_model(item) for item in projected["proposals"]
            ]
        if projected.get("tool") == "list_replans":
            # Collective receipts are server truth, but the full persisted
            # matrix is not a useful model prompt. Preserve every decision
            # identity and fit/fairness value while dropping duplicate plan
            # payloads and raw evidence.
            for field in ("candidate_fit_matrix", "proposal_collective_decisions"):
                value = projected.get(field)
                if isinstance(value, dict):
                    projected[field] = {
                        str(key): _compact_collective_option(item)
                        for key, item in value.items()
                        if isinstance(item, dict)
                    }
                elif isinstance(value, list):
                    projected[field] = [
                        _compact_collective_option(item)
                        for item in value
                        if isinstance(item, dict)
                    ]
        if projected.get("tool") in {"list_replans", "request_replan"} and "proposal" in projected:
            projected["proposal"] = _compact_replan_for_model(projected["proposal"])
        return _json_text(projected)
    return _json_text({"result": result})


def _compact_collective_option(item: dict[str, Any]) -> dict[str, Any]:
    """Keep the collective choice receipt small and decision-complete."""

    fields = {
        "candidate_id", "proposal_id", "plan_id", "status",
        "fit_subject_place_ids", "fit_subject_tags", "fit_subject_basis",
        "member_fits", "fairness", "classification", "decision_relevant_member_ids",
    }
    return {key: item[key] for key in fields if key in item}


def _tool_result_advances_state(tool_name: str, result: Any) -> bool:
    """Return whether a tool result created a new domain observation epoch.

    Inspection calls are intentionally repeatable, but a repeat after a
    successful domain action must not be mistaken for an infinite loop. The
    runtime only advances the epoch on server-owned evidence of change; model
    prose and failed tool calls never reset the repetition guard.
    """

    if not isinstance(result, dict) or result.get("error"):
        return False
    if tool_name == "request_replan":
        proposal = result.get("proposal")
        return isinstance(proposal, dict) and bool(proposal.get("id"))
    return tool_name in _MUTATION_TOOLS and result.get("executed") is True


def _same_state_replay(
    tool_name: str,
    prior_result: Any,
    *,
    replayed_from_sequence_no: int,
    state_epoch: int,
) -> dict[str, Any] | None:
    """Replay a prior server-owned observation without turning it into failure.

    Only pure inspection tools are eligible.  The replay is explicit machine
    evidence that the world has not advanced; it neither re-executes a mutation
    nor tells the model which strategy to choose next.
    """

    if tool_name not in _INSPECTION_TOOLS:
        return None
    if not isinstance(prior_result, dict) or prior_result.get("error"):
        return None
    replay = dict(prior_result)
    replay["_runtime_replay"] = {
        "same_state_replay": True,
        "state_changed": False,
        "state_epoch": state_epoch,
        "replayed_from_sequence_no": replayed_from_sequence_no,
    }
    return replay


def _server_completion_contract(
    tool_steps: list[dict[str, Any]], *, run_status: str
) -> dict[str, Any]:
    """Classify recovery completion from persisted server/tool facts only."""

    if run_status == "SUPERSEDED":
        latest = next(
            (
                item.get("result")
                for item in reversed(tool_steps)
                if isinstance(item.get("result"), dict)
                and item["result"].get("code")
                == RECOVERY_EPOCH_SUPERSEDED
            ),
            {},
        )
        return {
            "kind": "SUPERSEDED",
            "source": "server_trace",
            "reason_code": RECOVERY_EPOCH_SUPERSEDED,
            "stale_recovery_epoch_id": latest.get(
                "stale_recovery_epoch_id"
            ),
            "current_recovery_epoch_id": latest.get(
                "current_recovery_epoch_id"
            ),
        }

    active_mutations = {
        "accept_replan",
        "activate_standby",
        "replace_activity",
        "reschedule_activity",
        "create_booking",
        "cancel_refundable_booking",
        "request_commitment_change",
        "apply_clarified_horizon_choice",
    }
    executed = any(
        item.get("tool_name") in active_mutations
        and isinstance(item.get("result"), dict)
        and item["result"].get("executed") is True
        for item in tool_steps
    )
    approval_pending = any(
        isinstance(item.get("result"), dict)
        and (
            item["result"].get("decision") in {"REQUIRE_CONFIRMATION", "REQUIRE_STEP_UP"}
            or item["result"].get("approval_required") is True
        )
        for item in tool_steps
    )
    # A failed request_replan is an attempted strategy, not a live proposal.
    # Only a server-created proposal blocks a typed NO_ACTION conclusion.
    proposal_created = any(
        item.get("tool_name") == "request_replan"
        and isinstance(item.get("result"), dict)
        and isinstance(item["result"].get("proposal"), dict)
        and bool(item["result"]["proposal"].get("id"))
        for item in tool_steps
    )
    conclusions = [
        item.get("result", {}).get("requested_outcome")
        for item in tool_steps
        if item.get("tool_name") == "conclude_recovery"
        and isinstance(item.get("result"), dict)
        and item.get("status") != "TOOL_ERROR"
    ]
    requested = conclusions[-1] if conclusions else None
    r61_conclusion = next(
        (
            item.get("result", {}).get("r61_completion")
            for item in reversed(tool_steps)
            if item.get("tool_name") == "conclude_recovery"
            and isinstance(item.get("result"), dict)
            and item.get("status") != "TOOL_ERROR"
            and isinstance(item.get("result", {}).get("r61_completion"), dict)
        ),
        None,
    )
    if (
        requested == "NO_ACTION"
        and isinstance(r61_conclusion, dict)
        and r61_conclusion.get("state")
        in {
            "RECOVERY_REGROUNDED_NO_ACTION",
            "RECOVERY_REGROUNDED_DOMINANT",
            "RECOVERY_REGROUND_NO_FEASIBLE_OPTION",
        }
        and r61_conclusion.get("plan_unchanged") is True
        and r61_conclusion.get("mutation_count") == 0
    ):
        return {
            "kind": "NO_ACTION",
            "source": "server_trace",
            "r61_state": r61_conclusion["state"],
            "incident_key": r61_conclusion.get("incident_key"),
            "recovery_epoch_id": r61_conclusion.get("recovery_epoch_id"),
            "collective_classification": r61_conclusion.get("collective_classification"),
            "current_proposal_ids": r61_conclusion.get("current_proposal_ids") or [],
        }
    if requested == "MUTATION_EXECUTED" and executed:
        return {"kind": requested, "source": "server_trace"}
    if requested == "APPROVAL_PENDING" and approval_pending:
        return {"kind": requested, "source": "server_trace"}
    defer_conclusion = next(
        (item.get("result", {}).get("horizon_decision") for item in tool_steps
         if item.get("tool_name") == "conclude_recovery"
         and isinstance(item.get("result"), dict)
         and item.get("status") != "TOOL_ERROR"),
        None,
    )
    if requested == "NO_ACTION" and defer_conclusion == "DEFER" and not executed and not proposal_created:
        return {
            "kind": "UNRESOLVED",
            "source": "server_trace",
            "reason_code": "DEFER_WITHOUT_EXECUTION_EFFECT",
        }
    if requested == "NO_ACTION" and defer_conclusion == "DEFER" and proposal_created and not executed:
        return {"kind": requested, "source": "server_trace"}
    if requested == "NO_ACTION" and not executed and not approval_pending and not proposal_created:
        return {"kind": requested, "source": "server_trace"}
    if requested == "UNRESOLVED":
        return {"kind": requested, "source": "server_trace"}
    if any(
        item.get("tool_name") == "request_collective_decision"
        and isinstance(item.get("result"), dict)
        and item["result"].get("mission_status") == "WAITING_FOR_MEMBERS"
        and item["result"].get("clarification", {}).get("clarification_id")
        for item in tool_steps
    ):
        return {"kind": "CLARIFICATION_REQUIRED", "source": "server_trace", "mission_status": "WAITING_FOR_MEMBERS"}
    if requested == "CLARIFICATION_REQUIRED" and any(
        item.get("tool_name") == "conclude_recovery"
        and isinstance(item.get("result"), dict)
        and item["result"].get("mission_status") == "WAITING_FOR_USER"
        and item["result"].get("clarification", {}).get("clarification_id")
        for item in tool_steps
    ):
        return {"kind": requested, "source": "server_trace", "mission_status": "WAITING_FOR_USER"}
    if executed:
        return {"kind": "MUTATION_EXECUTED", "source": "server_trace"}
    if approval_pending:
        return {"kind": "APPROVAL_PENDING", "source": "server_trace"}
    no_progress = next(
        (
            item
            for item in reversed(tool_steps)
            if item.get("decision") == "NO_PROGRESS"
            or (
                isinstance(item.get("result"), dict)
                and item["result"].get("status") == "NO_PROGRESS"
            )
        ),
        None,
    )
    if no_progress is not None:
        return {
            "kind": "NO_PROGRESS",
            "source": "server_trace",
            "reason_code": "SAME_STATE_STRATEGY_STALLED",
            "tool_name": no_progress.get("tool_name"),
        }
    return {
        "kind": "UNRESOLVED",
        "source": "server_trace",
        "reason_code": "NO_TYPED_RECOVERY_CONCLUSION"
        if run_status == "COMPLETED"
        else "RUN_NOT_RESOLVED",
    }


def _strategy_neutral_terminal_message(
    completion: dict[str, Any] | None = None,
) -> str:
    """Describe the missing terminal contract without prescribing strategy."""

    kind = str((completion or {}).get("kind") or "UNRESOLVED")
    return (
        "CityNexus terminal contract is not yet satisfied "
        f"({kind}): no server-owned typed terminal outcome exists for this "
        "mission. Continue with any allowed strategy, or finish only when "
        "the server can record a valid terminal outcome. No specific next "
        "tool is required."
    )


def _terminal_agent_status(
    tool_steps: list[dict[str, Any]], *, has_final_text: bool, clarification: bool
) -> tuple[str, str]:
    """Derive persisted statuses from server-owned completion facts, never prose alone."""

    run_status = "COMPLETED" if has_final_text else "PARTIAL"
    completion = _server_completion_contract(tool_steps, run_status=run_status)
    if completion.get("r61_state"):
        run_status = "COMPLETED"
    waiting_for_approval = completion["kind"] == "APPROVAL_PENDING"
    if waiting_for_approval:
        run_status = "WAITING_FOR_APPROVAL"
    elif completion["kind"] in {"UNRESOLVED", "NO_PROGRESS"}:
        run_status = "PARTIAL"
    waiting_for_members = completion.get("mission_status") == "WAITING_FOR_MEMBERS"
    mission_status = (
        "WAITING_FOR_APPROVAL"
        if waiting_for_approval
        else "WAITING_FOR_MEMBERS"
        if waiting_for_members
        else "WAITING_FOR_USER"
        if clarification
        else "COMPLETED"
        if run_status == "COMPLETED"
        else "PARTIAL"
    )
    return run_status, mission_status


def persisted_agent_run_payload(session: Session, run: AgentRun) -> dict[str, Any]:
    mission = session.get(JourneyMission, run.mission_id)
    steps = session.scalars(
        select(AgentRunStep).where(AgentRunStep.run_id == run.id).order_by(AgentRunStep.sequence_no)
    ).all()
    step_payloads = [
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
    ]
    return {
        "run_id": run.id,
        "mission_id": run.mission_id,
        "journey_id": run.journey_id,
        "recovery_epoch_id": run.recovery_epoch_id,
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
        "completion_contract": _server_completion_contract(
            step_payloads, run_status=run.status
        ),
        "outcome": _structured_run_outcome(run.outcome),
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
        "tool_steps": step_payloads,
    }


def _structured_run_outcome(value: str | None) -> str | dict[str, Any] | None:
    """Decode durable machine outcomes while preserving ordinary model prose."""

    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    return decoded if isinstance(decoded, dict) and decoded.get("status") else value


def _structured_failure_outcome(
    *,
    status: str,
    reason_code: str,
    user_summary: str,
    recommended_action: str,
) -> dict[str, str]:
    return {
        "status": status,
        "reason_code": reason_code,
        "user_summary": user_summary,
        "recommended_action": recommended_action,
    }


class DeepSeekJourneyAgent:
    provider_key = "deepseek"
    runtime_identity = NATIVE_DEEPSEEK_TOOL_LOOP
    # This is the server-owned identity used for calls emitted by this local
    # tool loop. Native MCP callers use an explicit mcp-client identity.
    agent_id = "citynexus-agent"

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
        recovery_epoch = ensure_recovery_epoch(session, journey)
        run = AgentRun(
            id=str(uuid4()),
            mission_id=mission.id,
            journey_id=journey.id,
            provider_key=self.provider_key,
            model_key=self.model_key,
            status="RUNNING",
            created_at=now,
            started_at=now,
            recovery_epoch_id=recovery_epoch.id,
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
            after={
                "run_id": run.id,
                "mission_id": mission.id,
                "model": self.model_key,
                "recovery_epoch_id": recovery_epoch.id,
                "recovery_epoch_generation": recovery_epoch.generation,
            },
            trace_id=trace_id,
        )
        session.flush()
        # The run envelope must survive a process/request interruption before
        # the first external model call. A flush only assigns IDs inside the
        # current transaction; it is not durable evidence.
        session.commit()
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
        # Do not hold the Journey/AgentRun session transaction across an
        # external provider response.  The durable RUNNING envelope and its
        # pre-call trace are already committed above; releasing this read
        # transaction lets a concurrent server-owned provider observation open
        # the next recovery epoch while this Agent is waiting on the model.
        session.commit()
        inspected = False
        final_text = ""
        recovery_superseded = False
        sequence = 0
        tool_trace: list[dict[str, Any]] = []
        # A repeated inspection is only a loop while the domain state is
        # unchanged. request_replan/accepted mutations advance this epoch so
        # the model can inspect newly-created server facts before deciding.
        state_epoch = 0
        seen_calls: set[tuple[str, str, int]] = set()
        replayable_results: dict[
            tuple[str, str, int], tuple[int, dict[str, Any]]
        ] = {}
        tool_counts: dict[str, int] = {}
        terminal_supervisor_turns = 0

        def preempt_if_recovery_epoch_advanced(
            *, model_response_in_hand: bool = False
        ) -> bool:
            """Stop this exact run when server-owned Journey reality moved on.

            The run is never retargeted.  This guard executes both immediately
            before an external model call and immediately after its response so
            a shock that lands while the provider is thinking cannot leak a
            stale tool call into the current recovery generation.
            """

            nonlocal sequence, recovery_superseded, final_text
            if not run.recovery_epoch_id:
                return False
            try:
                validate_recovery_epoch_binding(
                    session,
                    journey.id,
                    run.recovery_epoch_id,
                )
            except RecoveryEpochSuperseded as exc:
                recovery_superseded = True
                final_text = (
                    "Recovery stopped because the server-owned Journey reality "
                    "advanced to a newer recovery epoch."
                )
                sequence += 1
                step_now = _now()
                result = {
                    "tool": "recovery_epoch_guard",
                    **exc.facts,
                    "decision": RECOVERY_EPOCH_SUPERSEDED,
                    "executed": False,
                    "model_response_discarded": model_response_in_hand,
                }
                step = AgentRunStep(
                    id=str(uuid4()),
                    run_id=run.id,
                    sequence_no=sequence,
                    tool_name="recovery_epoch_guard",
                    arguments_json=_json_text(
                        {
                            "bound_recovery_epoch_id": run.recovery_epoch_id,
                        }
                    ),
                    result_json=_json_text(result),
                    decision=RECOVERY_EPOCH_SUPERSEDED,
                    status="SUPERSEDED",
                    created_at=step_now,
                    completed_at=step_now,
                )
                session.add(step)
                tool_trace.append(
                    {
                        "sequence_no": sequence,
                        "tool_name": "recovery_epoch_guard",
                        "arguments": {
                            "bound_recovery_epoch_id": run.recovery_epoch_id,
                        },
                        "result": result,
                        "decision": RECOVERY_EPOCH_SUPERSEDED,
                        "status": "SUPERSEDED",
                        "created_at": step_now,
                        "completed_at": step_now,
                    }
                )
                record_audit(
                    session,
                    action="agent.run.recovery_epoch_superseded",
                    entity_type="AgentRun",
                    entity_id=run.id,
                    journey_id=journey.id,
                    actor_type="SYSTEM",
                    actor_id="recovery-epoch",
                    after={
                        "run_id": run.id,
                        "mission_id": mission.id,
                        "status": "SUPERSEDED",
                        **exc.facts,
                    },
                    trace_id=trace_id,
                )
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="agent.recovery_epoch_guard",
                    attributes={
                        "status": "SUPERSEDED",
                        "bound_recovery_epoch_id": run.recovery_epoch_id,
                        "current_recovery_epoch_id": exc.facts.get(
                            "current_recovery_epoch_id"
                        ),
                    },
                )
                session.commit()
                return True
            # validate_recovery_epoch_binding reads current reality and therefore
            # opens a transaction.  Close it before any provider wait.
            session.commit()
            return False

        try:
            for _ in range(max_steps):
                batch_search_seen = False
                # Includes replay/no-progress paths that bypass the normal
                # tool-result commit.  Every external call must start with no
                # database transaction held by this model run.
                session.commit()
                if preempt_if_recovery_epoch_advanced():
                    break
                message = self._complete(messages)
                for receipt in getattr(self, "external_attempts", [])[len(
                    [item for item in tool_trace if item.get("tool_name") == "external_model_call"
                ]):]:
                    sequence += 1
                    step_now = _now()
                    result = {
                        **receipt,
                        "run_id": run.id,
                        "mission_id": mission.id,
                    }
                    session.add(AgentRunStep(
                        id=str(uuid4()), run_id=run.id, sequence_no=sequence,
                        tool_name="external_model_call",
                        arguments_json=_json_text({"provider": receipt.get("provider"), "model": receipt.get("model")}),
                        result_json=_json_text(result), decision="FAILED_EXTERNAL",
                        status="FAILED_EXTERNAL", created_at=step_now, completed_at=step_now,
                    ))
                    tool_trace.append({
                        "sequence_no": sequence, "tool_name": "external_model_call",
                        "arguments": {"provider": receipt.get("provider"), "model": receipt.get("model")},
                        "result": result, "decision": "FAILED_EXTERNAL", "status": "FAILED_EXTERNAL",
                        "created_at": step_now, "completed_at": step_now,
                    })
                session.commit()
                # The external call may have overlapped a new provider shock.
                # Discard its response before even a read-only stale tool is
                # executed; a fresh run owns the newer recovery generation.
                if preempt_if_recovery_epoch_advanced(
                    model_response_in_hand=True
                ):
                    break
                run.model_key = self.model_key
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
                    completion = _server_completion_contract(
                        tool_trace,
                        run_status="COMPLETED",
                    )
                    if (
                        completion.get("kind") == "UNRESOLVED"
                        and terminal_supervisor_turns < 1
                    ):
                        # The server may refuse to call prose "done", but it
                        # must not prescribe the model's next tool. Give the
                        # Agent one bounded free-strategy turn with only the
                        # missing terminal fact.
                        terminal_supervisor_turns += 1
                        messages.append(
                            {
                                "role": "assistant",
                                "content": str(content),
                            }
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": _strategy_neutral_terminal_message(
                                    completion
                                ),
                            }
                        )
                        continue
                    final_text = str(content)
                    break
                assistant_message = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                    "_response_items": message.get("_response_items") or [],
                }
                if message.get("reasoning_content"):
                    assistant_message["reasoning_content"] = message["reasoning_content"]
                messages.append(assistant_message)
                for call in tool_calls:
                    function = call.get("function") or {}
                    tool_name = str(function.get("name") or "")
                    call_id = str(call.get("id") or f"tool-call-{sequence}")
                    raw_arguments = str(function.get("arguments") or "{}")
                    search_batch_limited = tool_name == "search_city_services" and batch_search_seen
                    if tool_name == "search_city_services":
                        batch_search_seen = True
                    try:
                        arguments = _tool_args(raw_arguments)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        # A streamed provider can terminate a tool-call fragment
                        # mid-JSON.  Never execute guessed arguments and never let
                        # one malformed model frame turn the whole AgentRun into
                        # an unhandled HTTP 500; return a machine-visible tool
                        # error so the model can repair or stop.
                        sequence += 1
                        arguments = {"_raw_arguments": raw_arguments[:1000]}
                        result = {
                            "tool": tool_name,
                            "error": "MALFORMED_TOOL_ARGUMENTS",
                            "detail": type(exc).__name__,
                            "decision": "REJECT_MALFORMED_ARGUMENTS",
                        }
                        step_now = _now()
                        step = AgentRunStep(
                            id=str(uuid4()), run_id=run.id, sequence_no=sequence,
                            tool_name=tool_name, arguments_json=_json_text(arguments),
                            result_json=_json_text(result), decision=result["decision"],
                            status="TOOL_ERROR", created_at=step_now, completed_at=step_now,
                        )
                        session.add(step)
                        tool_trace.append({
                            "sequence_no": sequence, "tool_name": tool_name,
                            "arguments": arguments, "result": result,
                            "decision": result["decision"], "status": "TOOL_ERROR",
                            "created_at": step_now, "completed_at": step_now,
                        })
                        messages.append({
                            "role": "tool", "tool_call_id": call_id,
                            "content": _result_for_model(result),
                        })
                        continue
                    signature = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                    repetition_key = (tool_name, signature, state_epoch)
                    if repetition_key in seen_calls:
                        replay_source = replayable_results.get(repetition_key)
                        replay = (
                            _same_state_replay(
                                tool_name,
                                replay_source[1],
                                replayed_from_sequence_no=replay_source[0],
                                state_epoch=state_epoch,
                            )
                            if replay_source is not None
                            else None
                        )
                        if replay is not None:
                            sequence += 1
                            step_now = _now()
                            step = AgentRunStep(
                                id=str(uuid4()),
                                run_id=run.id,
                                sequence_no=sequence,
                                tool_name=tool_name,
                                arguments_json=_json_text(arguments),
                                result_json=_json_text(replay),
                                decision="REPLAY_SAME_STATE",
                                status="COMPLETED",
                                created_at=step_now,
                                completed_at=step_now,
                            )
                            session.add(step)
                            tool_trace.append(
                                {
                                    "sequence_no": sequence,
                                    "tool_name": tool_name,
                                    "arguments": arguments,
                                    "result": replay,
                                    "decision": "REPLAY_SAME_STATE",
                                    "status": "COMPLETED",
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
                                    "decision": "REPLAY_SAME_STATE",
                                    "status": "COMPLETED",
                                    "replayed_from_sequence_no": replay_source[0],
                                    "state_epoch": state_epoch,
                                },
                                trace_id=trace_id,
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": _result_for_model(replay),
                                }
                            )
                            continue

                        result = {
                            "tool": tool_name,
                            "status": "NO_PROGRESS",
                            "decision": "NO_PROGRESS",
                            "reason": "same request and state produced no new domain fact",
                        }
                        sequence += 1
                        step_now = _now()
                        step = AgentRunStep(
                            id=str(uuid4()), run_id=run.id, sequence_no=sequence,
                            tool_name=tool_name, arguments_json=_json_text(arguments),
                            result_json=_json_text(result), decision="NO_PROGRESS",
                            status="COMPLETED", created_at=step_now, completed_at=step_now,
                        )
                        session.add(step)
                        tool_trace.append({"sequence_no": sequence, "tool_name": tool_name, "arguments": arguments, "result": result, "decision": "NO_PROGRESS", "status": step.status})
                        final_text = "No new domain fact was produced for the repeated request."
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _result_for_model(result)})
                        continue
                    seen_calls.add(repetition_key)
                    sequence += 1
                    arguments.setdefault("journey_id", journey.id)
                    arguments.setdefault("mission_id", mission.id)
                    arguments["request_id"] = f"agent-run-{run.id}-{sequence}"
                    arguments.setdefault("agent_id", self.agent_id)
                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                    search_saturated = (
                        tool_name == "search_city_services"
                        and tool_counts[tool_name] > 3
                    )
                    if search_batch_limited:
                        result = {
                            "tool": tool_name,
                            "error": "SEARCH_BATCH_LIMIT",
                            "decision": "STOP_SEARCH_BATCH",
                            "message": "Only one city search is executed per assistant turn; choose a different action on the next turn.",
                        }
                    elif search_saturated:
                        result = {
                            "tool": tool_name,
                            "error": "SEARCH_SATURATION",
                            "decision": "STOP_SEARCH_SATURATION",
                            "message": "Search budget exhausted for this run; choose a different strategy or source.",
                        }
                    elif tool_name not in RUNTIME_MCP_ALLOWLIST_V2:
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
                        "activate_standby",
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
                            if tool_name == "conclude_recovery" and isinstance(
                                arguments.get("reason"), str
                            ) and len(arguments["reason"]) > 500:
                                # The model may explain a long decision, but the
                                # persisted typed conclusion has a strict schema
                                # boundary.  Preserve a bounded receipt instead
                                # of spending another model turn on a 422.
                                arguments["reason"] = arguments["reason"][:497] + "..."
                            payload = AgentToolCall.model_validate(arguments)
                            result = AgentToolGateway(
                                session, bound_run_id=run.id
                            ).dispatch(tool_name, payload)
                            if tool_name in _INSPECTION_TOOLS:
                                inspected = True
                        except HTTPException as exc:
                            detail = exc.detail if isinstance(exc.detail, dict) else None
                            if (
                                exc.status_code == 409
                                and isinstance(detail, dict)
                                and detail.get("code") == RECOVERY_EPOCH_SUPERSEDED
                            ):
                                # The provider/model worked; the world moved.
                                # Preserve that causal distinction instead of
                                # downgrading a stale strategy to TOOL_ERROR.
                                result = {
                                    "tool": tool_name,
                                    **detail,
                                    "status_code": exc.status_code,
                                    "decision": RECOVERY_EPOCH_SUPERSEDED,
                                }
                                recovery_superseded = True
                            else:
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
                        status=(
                            "SUPERSEDED"
                            if recovery_superseded
                            and isinstance(result, dict)
                            and result.get("decision") == RECOVERY_EPOCH_SUPERSEDED
                            else "COMPLETED"
                            if "error" not in result
                            else "TOOL_ERROR"
                        ),
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
                            "status": step.status,
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
                    if tool_name == "discover_recovery_options":
                        persist_span(
                            session,
                            journey_id=journey.id,
                            trace_id=trace_id,
                            name="real_city.recovery_discovery",
                            attributes={
                                "tool_name": tool_name,
                                "status": result.get("status")
                                if isinstance(result, dict)
                                else None,
                                "candidate_count": len(result.get("candidate_place_ids", []))
                                if isinstance(result, dict)
                                and isinstance(result.get("candidate_place_ids"), list)
                                else 0,
                            },
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
                    if (
                        tool_name in _INSPECTION_TOOLS
                        and isinstance(result, dict)
                        and not result.get("error")
                    ):
                        replayable_results[repetition_key] = (
                            sequence,
                            dict(result),
                        )
                    if _tool_result_advances_state(tool_name, result):
                        state_epoch += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _result_for_model(result),
                        }
                    )
                    if recovery_superseded:
                        final_text = (
                            "Recovery stopped because the server-owned reality "
                            "advanced to a newer recovery epoch."
                        )
                        break
                    # An executed mutation is itself a server-owned terminal
                    # recovery fact.  Do not ask the same model turn to reason
                    # again against a reality it just changed; any further work
                    # belongs to a fresh inspection/run in the new reality.
                    if (
                        tool_name in _MUTATION_TOOLS
                        and isinstance(result, dict)
                        and result.get("executed") is True
                    ):
                        bound_epoch = (
                            session.get(RecoveryEpoch, run.recovery_epoch_id)
                            if run.recovery_epoch_id
                            else None
                        )
                        if (
                            bound_epoch is not None
                            and bound_epoch.status == COMMITTED
                        ):
                            final_text = (
                                "The server-authorized Journey mutation "
                                "committed this recovery epoch; the run is "
                                "complete."
                            )
                            session.commit()
                            break
                    # Human approval is not a reason to keep asking the model.
                    # Persist the already-selected action and suspend this same
                    # AgentRun until the server receives the approval.
                    from app.agent.continuation import approval_waiting, capture_pending_action

                    if approval_waiting(result):
                        capture_pending_action(
                            session,
                            run=run,
                            mission=mission,
                            step=step,
                            tool_name=tool_name,
                            arguments=arguments,
                            result=result,
                        )
                        final_text = "Agent is waiting for the required human approval."
                        break
                    # A collective decision request is itself a durable,
                    # server-owned mission transition. Do not spend another
                    # model turn asking for a prose conclusion: a later
                    # timeout must not downgrade WAITING_FOR_MEMBERS into a
                    # failed AgentRun.
                    if (
                        tool_name == "request_collective_decision"
                        and isinstance(result, dict)
                        and result.get("mission_status") == "WAITING_FOR_MEMBERS"
                        and result.get("clarification", {}).get("clarification_id")
                    ):
                        final_text = "Collective decision is waiting for the required member responses."
                        break
                    # Each tool result is a durable boundary.  Do not let one
                    # model turn's inspection batch hold SQLite's writer lock
                    # while another independent recovery run is processing its
                    # own current epoch.
                    session.commit()
                session.flush()
                if final_text:
                    break
            else:
                final_text = "Agent reached the configured tool-step limit."
            clarification = any(
                item.get("tool_name") == "conclude_recovery"
                and isinstance(item.get("result"), dict)
                and item["result"].get("requested_outcome") == "CLARIFICATION_REQUIRED"
                and item["result"].get("mission_status") == "WAITING_FOR_USER"
                for item in tool_trace
            )
            if recovery_superseded:
                run.status = "SUPERSEDED"
                mission.status = "SUPERSEDED"
                run.failure_reason = RECOVERY_EPOCH_SUPERSEDED
                mission.failure_reason = RECOVERY_EPOCH_SUPERSEDED
                run.outcome = json.dumps(
                    _structured_failure_outcome(
                        status="SUPERSEDED",
                        reason_code=RECOVERY_EPOCH_SUPERSEDED,
                        user_summary=(
                            "This Agent run became stale because the Journey "
                            "advanced to a newer server-owned recovery epoch."
                        ),
                        recommended_action=(
                            "Inspect the current Journey reality and start or "
                            "join recovery in the current epoch."
                        ),
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            else:
                run.status, mission.status = _terminal_agent_status(
                    tool_trace,
                    has_final_text=bool(final_text),
                    clarification=clarification,
                )
                run.outcome = (
                    final_text
                    or "Tool loop completed without a final summary."
                )
        except AgentRuntimeUnavailable as exc:
            for receipt in getattr(self, "external_attempts", []):
                if any(
                    item.get("tool_name") == "external_model_call"
                    and item.get("result", {}).get("model") == receipt.get("model")
                    and item.get("result", {}).get("elapsed_ms") == receipt.get("elapsed_ms")
                    for item in tool_trace
                ):
                    continue
                sequence += 1
                step_now = _now()
                result = {**receipt, "run_id": run.id, "mission_id": mission.id}
                session.add(AgentRunStep(
                    id=str(uuid4()), run_id=run.id, sequence_no=sequence,
                    tool_name="external_model_call",
                    arguments_json=_json_text({"provider": receipt.get("provider"), "model": receipt.get("model")}),
                    result_json=_json_text(result), decision="FAILED_EXTERNAL",
                    status="FAILED_EXTERNAL", created_at=step_now, completed_at=step_now,
                ))
                tool_trace.append({
                    "sequence_no": sequence, "tool_name": "external_model_call",
                    "arguments": {"provider": receipt.get("provider"), "model": receipt.get("model")},
                    "result": result, "decision": "FAILED_EXTERNAL", "status": "FAILED_EXTERNAL",
                })
            run.status = "FAILED_EXTERNAL"
            mission.status = "DEGRADED"
            run.failure_reason = str(exc)
            run.outcome = json.dumps(
                _structured_failure_outcome(
                    status="FAILED_EXTERNAL",
                    reason_code=(
                        ai_state_from_failure_reason(str(exc))
                        or "AGENT_RUNTIME_UNAVAILABLE"
                    ),
                    user_summary="The configured Agent provider did not complete this mission.",
                    recommended_action="Retry with a configured provider or continue with deterministic tools.",
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
            mission.failure_reason = str(exc)
            raise
        finally:
            completed = _now()
            suspended = run.status == "WAITING_FOR_APPROVAL"
            if suspended:
                # A suspended run is intentionally not terminal.  Its durable
                # continuation will append the resumed tool step and set the
                # completion timestamp after the approved mutation executes.
                run.completed_at = None
                mission.completed_at = None
            else:
                run.completed_at = completed
                mission.completed_at = completed
            record_audit(
                session,
                action="agent.run.suspended" if suspended else "agent.run.completed",
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
                    "waiting_for_approval": suspended,
                },
                trace_id=trace_id,
            )
            session.commit()
        return {
            "run_id": run.id,
            "mission_id": mission.id,
            "status": run.status,
            "completion_contract": _server_completion_contract(
                tool_trace, run_status=run.status
            ),
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
    recovery_epoch = ensure_recovery_epoch(session, journey)
    availability = ai_availability_state()
    outcome = {
        "status": "BLOCKED",
        "reason_code": availability,
        "user_summary": "The configured Agent runtime did not execute this mission.",
        "recommended_action": "Enable a configured Agent runtime or continue with deterministic tools.",
    }
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
        failure_reason=reason_sentence(availability),
        outcome=json.dumps(outcome, ensure_ascii=False, sort_keys=True),
        created_at=now,
        started_at=now,
        completed_at=now,
        recovery_epoch_id=recovery_epoch.id,
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
