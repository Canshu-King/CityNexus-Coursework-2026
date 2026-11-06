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
_ACTION_TOOLS = (
    "request_replan",
    "accept_replan",
    "replace_activity",
    "reschedule_activity",
    "create_booking",
    "cancel_refundable_booking",
    "request_commitment_change",
)


def build_mission_plan(
    *,
    autonomy: str = "PLAN_ONLY",
    protected_activity_ids: tuple[str, ...] | list[str] = (),
    objective_codes: tuple[str, ...] | list[str] = (),
) -> AgentMissionPlan:
    """Build a deterministic plan from the already-compiled mission envelope.

    The sequence is intentionally stable for demos and auditing, but the UI
    must render actual runtime events rather than treating these steps as
    proof that Hermes executed them.
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
    if "RECOVER_FROM_CITY_DISRUPTION" in objectives or "PREFER_INDOOR" in objectives:
        steps.extend(
            [
                MissionPlanStep("inspect_affected", "Inspect affected activities", "inspect_journey"),
                MissionPlanStep("search_candidates", "Search candidate places and services", "search_city_services"),
            ]
        )
    steps.extend(
        [
            MissionPlanStep("evaluate_candidates", "Evaluate candidates against constraints", None),
            MissionPlanStep(
                "request_replan",
                "Request a deterministic replan proposal",
                "request_replan",
                requires_authority=True,
            ),
            MissionPlanStep("inspect_proposal", "Inspect proposal trade-offs", "list_replans"),
            MissionPlanStep(
                "approval_boundary",
                "Wait for explicit CityNexus approval before mutation",
                None,
                requires_authority=True,
            ),
        ]
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
    allowed_tools = (*_READ_TOOLS, "request_replan") if autonomy == "PLAN_ONLY" else (*_READ_TOOLS, *_ACTION_TOOLS)
    return AgentMissionPlan(
        version="citynexus.mission-plan.v1",
        autonomy=autonomy,
        steps=tuple(steps),
        protected_activity_ids=tuple(str(item) for item in protected_activity_ids),
        allowed_tools=tuple(allowed_tools),
        read_only_tools=_READ_TOOLS,
        approval_required_tools=tuple(
            item for item in _ACTION_TOOLS if item != "request_replan"
        ),
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
    return plan.as_dict()
