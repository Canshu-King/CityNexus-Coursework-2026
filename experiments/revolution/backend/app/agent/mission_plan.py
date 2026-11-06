"""Small, user-safe mission plans for the native Hermes hand-off.

The plan is an execution envelope, not a reasoning trace.  It deliberately
contains only bounded operations that Hermes may perform through CityNexus MCP;
IDs, facts, policy decisions, and mutations remain server-owned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MissionPlanStep:
    id: str
    operation: str
    tool_name: str | None
    status: str = "PENDING"
    requires_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "tool_name": self.tool_name,
            "status": self.status,
            "requires_authority": self.requires_authority,
        }


@dataclass(frozen=True)
class AgentMissionPlan:
    """Explicit safe orchestration envelope sent to Hermes and the UI."""

    version: str
    autonomy: str
    steps: tuple[MissionPlanStep, ...]
    protected_activity_ids: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    read_only_tools: tuple[str, ...] = ()
    approval_required_tools: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "autonomy": self.autonomy,
            "steps": [step.as_dict() for step in self.steps],
            "protected_activity_ids": list(self.protected_activity_ids),
            "allowed_tools": list(self.allowed_tools),
            "read_only_tools": list(self.read_only_tools),
            "approval_required_tools": list(self.approval_required_tools),
            "authority": "CityNexus AgentToolGateway and deterministic domain services",
        }


_READ_TOOLS = (
    "inspect_journey",
    "inspect_twin",
    "list_replans",
    "search_city_services",
)
_CONCLUSION_TOOL = "conclude_recovery"
_ACTION_TOOLS = (
    "activate_standby",
    "request_replan",
    "accept_replan",
    "replace_activity",
    "reschedule_activity",
    "create_booking",
    "cancel_refundable_booking",
    "request_commitment_change",
    "apply_clarified_horizon_choice",
)
_DECISION_TOOLS = (
    "request_collective_decision",
)
_STRATEGY_TOOLS = (
    "acquire_place_facts",
    "estimate_place_facts",
    "generate_plans",
)


def build_mission_plan(
    *,
    autonomy: str = "PLAN_ONLY",
    protected_activity_ids: tuple[str, ...] | list[str] = (),
    objective_codes: tuple[str, ...] | list[str] = (),
) -> AgentMissionPlan:
    """Build a deterministic plan from the already-compiled mission envelope.

    The sequence is an authority envelope, not an action script. In
    particular, an autonomous recovery mission must not get a deterministic
    preference for standby activation over fresh replanning (or NO_ACTION).
    The UI must render actual runtime events rather than treating these steps
    as proof that Hermes executed them.
    """

    objectives = {str(item) for item in objective_codes}
    steps: list[MissionPlanStep] = [
        MissionPlanStep("inspect_journey", "Inspect current Journey", "inspect_journey"),
        MissionPlanStep("inspect_twin", "Inspect JourneyTwin", "inspect_twin"),
        # The MCP surface intentionally has no free-form commitment reader.
        # ``inspect_journey`` returns the server-owned protected commitment
        # projection, so the plan stays explicit without inventing a tool.
        MissionPlanStep("inspect_commitments", "Inspect protected commitments", "inspect_journey"),
    ]
    if "CAPABILITY_AWARE" in objectives:
        steps.insert(
            2,
            MissionPlanStep(
                "inspect_capabilities",
                "Inspect explicit traveler capabilities",
                "inspect_twin",
            ),
        )
    steps.insert(
        3,
        MissionPlanStep(
            "evaluate_recovery_options",
            "Evaluate the current reality and available recovery options",
            None,
            requires_authority=True,
        ),
    )
    if (
        "RECOVER_FROM_CITY_DISRUPTION" in objectives
        or "PREFER_INDOOR" in objectives
        or "COLLECTIVE_JOURNEY_RECOVERY" in objectives
    ):
        steps.extend(
            [
                MissionPlanStep("inspect_affected", "Inspect affected activities", "inspect_journey"),
                MissionPlanStep("search_candidates", "Search candidate places and services", "search_city_services"),
            ]
        )
    steps.extend(
        [
            MissionPlanStep("evaluate_candidates", "Evaluate candidates against constraints", None),
            MissionPlanStep("inspect_proposal", "Inspect available proposal trade-offs", "list_replans"),
            MissionPlanStep(
                "approval_boundary",
                "Wait for explicit CityNexus approval before mutation",
                None,
                requires_authority=True,
            ),
        ]
    )
    if "COLLECTIVE_JOURNEY_RECOVERY" in objectives:
        # A collective decision is not a Journey mutation. It opens the
        # existing server-owned member-response round only after grounded
        # proposals expose a real group trade-off. Keep it inside PLAN_ONLY
        # while accept/execute tools remain outside that envelope.
        steps.insert(
            -1,
            MissionPlanStep(
                "collective_decision_boundary",
                "Open one server-owned collective decision when a grounded group trade-off exists",
                "request_collective_decision",
                requires_authority=True,
            ),
        )
    if autonomy == "PLAN_ONLY":
        # Keep mutation tools visible as an explicit boundary without granting
        # Hermes permission to call them in this mission.
        steps[-1] = MissionPlanStep(
            "approval_boundary",
            "Proposal only; no mutation without user approval",
            None,
            requires_authority=True,
        )
    # Fact acquisition and candidate generation are strategy-layer actions. They
    # do not select or activate a plan, so an autonomous real-city mission may
    # use them before recovery mutation. PLAN_ONLY keeps its historical
    # proposal-only surface.
    if autonomy == "PLAN_ONLY":
        plan_only_tools = [*_READ_TOOLS]
        if objectives & {"LIVE_JOURNEY_INTENT_STEERING", "COLLECTIVE_RECOVERY_REGROUND"}:
            plan_only_tools.extend(_STRATEGY_TOOLS)
        plan_only_tools.append("request_replan")
        if "COLLECTIVE_JOURNEY_RECOVERY" in objectives:
            # R58/R59 group recovery may ask the affected members to decide,
            # but PLAN_ONLY still cannot accept or execute their choice.
            plan_only_tools.extend(_DECISION_TOOLS)
        if "COLLECTIVE_RECOVERY_REGROUND" in objectives:
            # A re-ground run needs a typed, server-owned terminal result. This
            # records NO_ACTION/dominant/no-feasible without granting mutation.
            plan_only_tools.append(_CONCLUSION_TOOL)
        allowed_tools = tuple(dict.fromkeys(plan_only_tools))
    else:
        allowed_tools = (*_READ_TOOLS, *_STRATEGY_TOOLS, *_DECISION_TOOLS, *_ACTION_TOOLS, _CONCLUSION_TOOL)
    return AgentMissionPlan(
        version="citynexus.mission-plan.v1",
        autonomy=autonomy,
        steps=tuple(steps),
        protected_activity_ids=tuple(str(item) for item in protected_activity_ids),
        allowed_tools=tuple(allowed_tools),
        read_only_tools=_READ_TOOLS,
        approval_required_tools=tuple(item for item in (*_DECISION_TOOLS, *_ACTION_TOOLS) if item != "request_replan"),
    )


def mission_plan_for_model(mission: Any) -> dict[str, Any]:
    """Return a safe plan from a JourneyMission-like object without COT."""

    import json

    def decode(name: str) -> list[str]:
        try:
            value = json.loads(getattr(mission, name, "[]") or "[]")
            return value if isinstance(value, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    plan = build_mission_plan(
        autonomy=str(getattr(mission, "requested_autonomy", "PLAN_ONLY")),
        protected_activity_ids=decode("protected_activity_ids_json"),
        objective_codes=decode("objective_codes_json"),
    )
    result = plan.as_dict()
    try:
        intent = json.loads(getattr(mission, "intent_json", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        intent = {}
    if isinstance(intent, dict) and intent.get("temporary_journey_intent"):
        result["temporary_journey_intent"] = intent["temporary_journey_intent"]
    return result
