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
    ToolDescriptor(AgentAction.REQUEST_REPLAN, "request_replan", True, False, False),
    ToolDescriptor(AgentAction.ACCEPT_REPLAN, "accept_replan", True, False, False),
)

TYPED_TOOLS = (
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
    return [item.as_dict() for item in TOOLS]


def tool_catalog_v2() -> list[dict[str, object]]:
    return [item.as_dict() for item in (*TOOLS, *TYPED_TOOLS)]
