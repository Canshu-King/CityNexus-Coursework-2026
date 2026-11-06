from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field

from .client import CityNexusAgentClient
from .schemas import (
    V1_TOOL_NAMES,
    V2_TOOL_NAMES,
    HermesReplanProposalView,
    MCPToolCall,
    MCPToolDescriptor,
)


RequestId = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        description=(
            "Opaque per-call idempotency identity; use 8-128 characters "
            "and do not reuse it for different arguments."
        ),
    ),
]

TOOL_DESCRIPTIONS = {
    "inspect_journey": "Read the persisted Journey state.",
    "inspect_twin": "Read the JourneyTwin projection.",
    "list_replans": (
        "List previously persisted Replan proposals. Use this for existing proposals; "
        "request_replan already returns newly generated proposal information."
    ),
    "search_city_services": (
        "Search server-owned city service facts only when the current server proposal or "
        "context lacks enough facts; do not repeatedly search an unchanged query."
    ),
    "request_replan": (
        "Request a proposal without activating a Plan. The response contains the newly "
        "generated proposal information; an immediate list_replans reread is normally "
        "redundant after a successful call."
    ),
    "accept_replan": "Ask the server policy and deterministic gate to accept a proposal.",
    "replace_activity": "Replace a mutable Activity through the server policy and feasibility gate.",
    "reschedule_activity": "Reschedule a mutable Activity through the server policy and feasibility gate.",
    "create_booking": "Create a provider booking when the server policy permits it.",
    "cancel_refundable_booking": "Cancel a refundable booking when the server policy permits it.",
    "request_commitment_change": "Request a protected Commitment change through the approval gate.",
}


def _agent_id() -> str:
    """Return the local principal, with the native MCP guard as default."""
    return os.getenv("CITYNEXUS_AGENT_ID", "mcp-client")


class CityNexusMCPServer:
    """Safe CityNexus tool adapter used by the SDK-backed MCP runtime."""

    def __init__(self, client: CityNexusAgentClient | None = None):
        self.client = client or CityNexusAgentClient()

    def list_tools(self) -> list[dict[str, str]]:
        return [
            MCPToolDescriptor(name=name, description=TOOL_DESCRIPTIONS[name]).as_dict()
            for name in V1_TOOL_NAMES
        ]

    def list_tools_v2(self) -> list[dict[str, str]]:
        return [
            MCPToolDescriptor(name=name, description=TOOL_DESCRIPTIONS[name]).as_dict()
            for name in V2_TOOL_NAMES
        ]

    def call(self, tool_name: str, call: MCPToolCall) -> dict[str, Any]:
        if tool_name not in V2_TOOL_NAMES:
            raise ValueError("tool is outside the CityNexus MCP allowlist")
        result = self.client.call(tool_name, call.payload())
        if tool_name == "request_replan":
            result["proposal_contract_version"] = "hermes-replan-proposal-view.v1"
            # The product answers one request_replan with TWO shapes: `proposal`
            # carries the full twelve fields, while `proposals` is a four-field
            # summary (id / status / hard_constraints_passed /
            # locked_commitments_preserved). Feeding only the summary to a
            # twelve-field validator made every live call fail — the adapter's own
            # test fixture had been handing the full object in both slots, so the
            # fleet stayed green while the real path could not produce a view.
            #
            # So: prefer the full rows, record the ones that cannot be viewed, and
            # only raise when nothing viewable exists at all. An incomplete
            # projection is still never reported as usable.
            rows: list[dict[str, Any]] = []
            seen: set[Any] = set()
            singular = result.get("proposal")
            if isinstance(singular, dict):
                rows.append(singular)
                seen.add(singular.get("id"))
            for item in result.get("proposals") or []:
                if isinstance(item, dict) and item.get("id") not in seen:
                    rows.append(item)
                    seen.add(item.get("id"))

            views: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            last_error: ValueError | None = None
            for item in rows:
                try:
                    views.append(HermesReplanProposalView.from_payload(item).as_dict())
                except ValueError as exc:
                    last_error = exc
                    rejected.append({"id": item.get("id"), "reason": str(exc)})
            if not views:
                raise ValueError(
                    str(last_error or "request_replan returned no proposal rows to project")
                )
            result["proposal_views"] = views
            if rejected:
                result["proposal_views_rejected"] = rejected
        return result


def build_mcp_server(client: CityNexusAgentClient | None = None):
    """Build a real MCP server with only the CityNexus V1 tools registered."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised on minimal installs
        raise RuntimeError(
            "install citynexus-agent-mcp[mcp] to run the MCP server"
        ) from exc

    adapter = CityNexusMCPServer(client)
    mcp = FastMCP(
        name="citynexus",
        instructions=(
            "CityNexus exposes only server-owned Journey inspection and safe replan tools. "
            "The CityNexus HTTP gateway performs identity, policy, and deterministic feasibility checks."
        ),
    )

    def call(tool_name: str, request_id: RequestId, **arguments: Any) -> dict[str, Any]:
        # This value is only a compatibility hint. The HTTP layer resolves the
        # trusted principal from its configured credential or local boundary.
        return adapter.call(
            tool_name,
            MCPToolCall(
                request_id=request_id, agent_id=_agent_id(), arguments=arguments
            ),
        )

    @mcp.tool(name="inspect_journey", description=TOOL_DESCRIPTIONS["inspect_journey"])
    def inspect_journey(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("inspect_journey", request_id, journey_id=journey_id)

    @mcp.tool(name="inspect_twin", description=TOOL_DESCRIPTIONS["inspect_twin"])
    def inspect_twin(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("inspect_twin", request_id, journey_id=journey_id)

    @mcp.tool(name="list_replans", description=TOOL_DESCRIPTIONS["list_replans"])
    def list_replans(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("list_replans", request_id, journey_id=journey_id)

    @mcp.tool(
        name="search_city_services",
        description=TOOL_DESCRIPTIONS["search_city_services"],
    )
    def search_city_services(
        request_id: RequestId,
        journey_id: str | None = None,
        city_code: str | None = None,
        q: str | None = None,
        category: str | None = None,
        service_type: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "search_city_services",
            request_id,
            journey_id=journey_id,
            city_code=city_code,
            q=q,
            category=category,
            service_type=service_type,
        )

    @mcp.tool(name="request_replan", description=TOOL_DESCRIPTIONS["request_replan"])
    def request_replan(
        request_id: RequestId,
        journey_id: str,
        trigger_event_id: str,
        candidate_count: int = 3,
    ) -> dict[str, Any]:
        return call(
            "request_replan",
            request_id,
            journey_id=journey_id,
            trigger_event_id=trigger_event_id,
            candidate_count=candidate_count,
        )

    @mcp.tool(name="accept_replan", description=TOOL_DESCRIPTIONS["accept_replan"])
    def accept_replan(
        request_id: RequestId,
        proposal_id: str,
        journey_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "accept_replan",
            request_id,
            proposal_id=proposal_id,
            journey_id=journey_id,
            approval_id=approval_id,
        )

    return mcp


def _build_mcp2_v2_server(client: CityNexusAgentClient | None = None):
    """Expose the same V2 surface through the modern MCP 2 SDK."""

    from mcp import types
    from mcp.server import Server

    adapter = CityNexusMCPServer(client)
    common = {
        "request_id": {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
            "description": (
                "Opaque per-call idempotency identity; use 8-128 characters "
                "and do not reuse it for different arguments."
            ),
        },
        "journey_id": {"type": "string"},
    }

    def schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }

    tool_schemas = {
        "inspect_journey": schema(common, ["journey_id"]),
        "inspect_twin": schema(common, ["journey_id"]),
        "list_replans": schema(common, ["journey_id"]),
        "search_city_services": schema(
            {
                **common,
                "city_code": {"type": "string"},
                "q": {"type": "string"},
                "category": {"type": "string"},
                "service_type": {"type": "string"},
            },
            [],
        ),
        "request_replan": schema(
            {
                **common,
                "trigger_event_id": {"type": "string"},
                "candidate_count": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            ["journey_id", "trigger_event_id"],
        ),
        "accept_replan": schema(
            {
                "request_id": common["request_id"],
                "journey_id": {"type": "string"},
                "proposal_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["proposal_id"],
        ),
        "replace_activity": schema(
            {
                **common,
                "activity_id": {"type": "string"},
                "replacement_activity_id": {"type": "string"},
                "replacement_place_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["journey_id", "activity_id"],
        ),
        "reschedule_activity": schema(
            {
                **common,
                "activity_id": {"type": "string"},
                "start_at": {"type": "string"},
                "end_at": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["journey_id", "activity_id", "start_at", "end_at"],
        ),
        "create_booking": schema(
            {
                **common,
                "activity_id": {"type": "string"},
                "provider_key": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["journey_id", "activity_id"],
        ),
        "cancel_refundable_booking": schema(
            {
                **common,
                "booking_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["journey_id", "booking_id"],
        ),
        "request_commitment_change": schema(
            {
                **common,
                "commitment_id": {"type": "string"},
                "reason": {"type": "string"},
                "approval_id": {"type": "string"},
            },
            ["journey_id", "commitment_id", "reason"],
        ),
    }
    read_only = {"inspect_journey", "inspect_twin", "list_replans", "search_city_services"}
    tools = [
        types.Tool(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            inputSchema=tool_schemas[name],
            annotations=types.ToolAnnotations(readOnlyHint=name in read_only),
        )
        for name in V2_TOOL_NAMES
    ]

    async def on_list_tools(_ctx: Any, _params: Any):
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx: Any, params: Any):
        arguments = dict(params.arguments or {})
        request_id = str(arguments.pop("request_id", "") or uuid4())
        try:
            result = adapter.call(
                params.name,
                MCPToolCall(request_id=request_id, agent_id=_agent_id(), arguments=arguments),
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        text=json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
                    )
                ],
                structuredContent=result,
            )
        except (TypeError, ValueError, KeyError) as exc:
            return types.CallToolResult(content=[types.TextContent(text=str(exc))], isError=True)

    server = Server(
        "citynexus-v2",
        instructions=(
            "CityNexus V2 exposes typed Journey inspection and action tools. "
            "The HTTP gateway remains authoritative for identity, policy, provider state, "
            "and deterministic feasibility. Every supplied request_id must be 8-128 "
            "characters and unique for its argument set."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    setattr(server, "_citynexus_low_level", True)
    return server


def build_mcp_v2_server(client: CityNexusAgentClient | None = None):
    """Build the expanded MCP surface without changing the pinned V1 server."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # modern MCP 2 SDK has no FastMCP convenience module
        return _build_mcp2_v2_server(client)

    adapter = CityNexusMCPServer(client)
    mcp = FastMCP(
        name="citynexus-v2",
        instructions=(
            "CityNexus V2 exposes typed Activity, Booking, and Commitment actions. "
            "The HTTP gateway remains authoritative for identity, approval, provider state, "
            "and deterministic feasibility."
        ),
    )

    def call(tool_name: str, request_id: RequestId, **arguments: Any) -> dict[str, Any]:
        return adapter.call(
            tool_name,
            MCPToolCall(
                request_id=request_id, agent_id=_agent_id(), arguments=arguments
            ),
        )

    @mcp.tool(name="inspect_journey", description=TOOL_DESCRIPTIONS["inspect_journey"])
    def inspect_journey(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("inspect_journey", request_id, journey_id=journey_id)

    @mcp.tool(name="inspect_twin", description=TOOL_DESCRIPTIONS["inspect_twin"])
    def inspect_twin(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("inspect_twin", request_id, journey_id=journey_id)

    @mcp.tool(name="list_replans", description=TOOL_DESCRIPTIONS["list_replans"])
    def list_replans(request_id: RequestId, journey_id: str) -> dict[str, Any]:
        return call("list_replans", request_id, journey_id=journey_id)

    @mcp.tool(
        name="search_city_services",
        description=TOOL_DESCRIPTIONS["search_city_services"],
    )
    def search_city_services(
        request_id: RequestId,
        journey_id: str | None = None,
        city_code: str | None = None,
        q: str | None = None,
        category: str | None = None,
        service_type: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "search_city_services",
            request_id,
            journey_id=journey_id,
            city_code=city_code,
            q=q,
            category=category,
            service_type=service_type,
        )

    @mcp.tool(name="request_replan", description=TOOL_DESCRIPTIONS["request_replan"])
    def request_replan(
        request_id: RequestId,
        journey_id: str,
        trigger_event_id: str,
        candidate_count: int = 3,
    ) -> dict[str, Any]:
        return call(
            "request_replan",
            request_id,
            journey_id=journey_id,
            trigger_event_id=trigger_event_id,
            candidate_count=candidate_count,
        )

    @mcp.tool(name="accept_replan", description=TOOL_DESCRIPTIONS["accept_replan"])
    def accept_replan(
        request_id: RequestId,
        proposal_id: str,
        journey_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "accept_replan",
            request_id,
            proposal_id=proposal_id,
            journey_id=journey_id,
            approval_id=approval_id,
        )

    @mcp.tool(
        name="replace_activity", description=TOOL_DESCRIPTIONS["replace_activity"]
    )
    def replace_activity(
        request_id: RequestId,
        journey_id: str,
        activity_id: str,
        replacement_activity_id: str | None = None,
        replacement_place_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "replace_activity",
            request_id,
            journey_id=journey_id,
            activity_id=activity_id,
            replacement_activity_id=replacement_activity_id,
            replacement_place_id=replacement_place_id,
            approval_id=approval_id,
        )

    @mcp.tool(
        name="reschedule_activity", description=TOOL_DESCRIPTIONS["reschedule_activity"]
    )
    def reschedule_activity(
        request_id: RequestId,
        journey_id: str,
        activity_id: str,
        start_at: str,
        end_at: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "reschedule_activity",
            request_id,
            journey_id=journey_id,
            activity_id=activity_id,
            start_at=start_at,
            end_at=end_at,
            approval_id=approval_id,
        )

    @mcp.tool(name="create_booking", description=TOOL_DESCRIPTIONS["create_booking"])
    def create_booking(
        request_id: RequestId,
        journey_id: str,
        activity_id: str,
        provider_key: str = "mock-booking",
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "create_booking",
            request_id,
            journey_id=journey_id,
            activity_id=activity_id,
            provider_key=provider_key,
            approval_id=approval_id,
        )

    @mcp.tool(
        name="cancel_refundable_booking",
        description=TOOL_DESCRIPTIONS["cancel_refundable_booking"],
    )
    def cancel_refundable_booking(
        request_id: RequestId,
        journey_id: str,
        booking_id: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "cancel_refundable_booking",
            request_id,
            journey_id=journey_id,
            booking_id=booking_id,
            approval_id=approval_id,
        )

    @mcp.tool(
        name="request_commitment_change",
        description=TOOL_DESCRIPTIONS["request_commitment_change"],
    )
    def request_commitment_change(
        request_id: RequestId,
        journey_id: str,
        commitment_id: str,
        reason: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "request_commitment_change",
            request_id,
            journey_id=journey_id,
            commitment_id=commitment_id,
            reason=reason,
            approval_id=approval_id,
        )

    return mcp


def main() -> None:
    use_v2 = "--v2" in sys.argv or os.getenv("CITYNEXUS_MCP_V2", "").lower() == "true"
    server = build_mcp_v2_server() if use_v2 else build_mcp_server()
    if getattr(server, "_citynexus_low_level", False):
        import anyio
        from mcp.server.stdio import stdio_server

        async def serve() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )

        anyio.run(serve)
        return
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
