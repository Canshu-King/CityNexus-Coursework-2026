from __future__ import annotations

from dataclasses import dataclass

from app.agent.contracts import AgentAction


@dataclass(frozen=True)
class ToolDescriptor:
    action: AgentAction
    tool_name: str
    mutates_domain_truth: bool
    changes_money: bool
    changes_c4: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.tool_name,
            "action": self.action.value,
            "mutates_domain_truth": self.mutates_domain_truth,
            "changes_money": self.changes_money,
            "changes_c4": self.changes_c4,
        }


TOOLS = (
    ToolDescriptor(AgentAction.INSPECT_JOURNEY, "inspect_journey", False, False, False),
    ToolDescriptor(AgentAction.INSPECT_TWIN, "inspect_twin", False, False, False),
    ToolDescriptor(AgentAction.LIST_REPLANS, "list_replans", False, False, False),
    ToolDescriptor(
        AgentAction.SEARCH_CITY_SERVICES,
        "search_city_services",
        False,
        False,
        False,
    ),
    ToolDescriptor(
        AgentAction.DISCOVER_RECOVERY_OPTIONS,
        "discover_recovery_options",
        False,
        False,
        False,
    ),
    ToolDescriptor(
        AgentAction.RESEARCH_PLACE_SOURCES,
        "research_place_sources",
        False,
        False,
        False,
    ),
    ToolDescriptor(AgentAction.REQUEST_REPLAN, "request_replan", True, False, False),
    ToolDescriptor(AgentAction.ACCEPT_REPLAN, "accept_replan", True, False, False),
    ToolDescriptor(AgentAction.ACTIVATE_STANDBY, "activate_standby", True, False, False),
)

TYPED_TOOLS = (
    ToolDescriptor(AgentAction.REQUEST_COLLECTIVE_DECISION, "request_collective_decision", True, False, False),
    ToolDescriptor(AgentAction.CONCLUDE_RECOVERY, "conclude_recovery", False, False, False),
    ToolDescriptor(AgentAction.APPLY_CLARIFIED_HORIZON_CHOICE, "apply_clarified_horizon_choice", True, False, False),
    ToolDescriptor(AgentAction.ACQUIRE_PLACE_FACTS, "acquire_place_facts", True, False, False),
    ToolDescriptor(AgentAction.ESTIMATE_PLACE_FACTS, "estimate_place_facts", False, False, False),
    ToolDescriptor(AgentAction.GENERATE_PLANS, "generate_plans", True, False, False),
    ToolDescriptor(AgentAction.REPLACE_ACTIVITY, "replace_activity", True, False, False),
    ToolDescriptor(AgentAction.RESCHEDULE_ACTIVITY, "reschedule_activity", True, False, False),
    ToolDescriptor(AgentAction.CREATE_BOOKING, "create_booking", True, True, False),
    ToolDescriptor(
        AgentAction.CANCEL_REFUNDABLE_BOOKING,
        "cancel_refundable_booking",
        True,
        True,
        False,
    ),
    ToolDescriptor(
        AgentAction.REQUEST_COMMITMENT_CHANGE,
        "request_commitment_change",
        True,
        False,
        True,
    ),
)

TOOL_BY_NAME = {item.tool_name: item for item in (*TOOLS, *TYPED_TOOLS)}
RUNTIME_MCP_ALLOWLIST_V1 = frozenset(item.tool_name for item in TOOLS)
RUNTIME_MCP_ALLOWLIST_V2 = frozenset(item.tool_name for item in (*TOOLS, *TYPED_TOOLS))


def tool_catalog() -> list[dict[str, object]]:
    # The unversioned V1 surface is a compatibility contract.  R27 additions
    # are available through the typed V2 catalog and must not silently change
    # the legacy endpoint's exact set.
    legacy_names = {
        "inspect_journey",
        "inspect_twin",
        "list_replans",
        "search_city_services",
        "request_replan",
        "accept_replan",
    }
    return [item.as_dict() for item in TOOLS if item.tool_name in legacy_names]


def tool_catalog_v2() -> list[dict[str, object]]:
    return [item.as_dict() for item in (*TOOLS, *TYPED_TOOLS)]
