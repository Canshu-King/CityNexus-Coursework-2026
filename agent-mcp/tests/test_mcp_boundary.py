from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from citynexus_mcp.schemas import V1_TOOL_NAMES, V2_TOOL_NAMES, MCPToolCall
from citynexus_mcp.server import CityNexusMCPServer, TOOL_DESCRIPTIONS


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, tool_name, payload):
        self.calls.append((tool_name, payload))
        return {"tool": tool_name, "received": payload}


def test_mcp_discovery_is_exactly_the_safe_allowlist():
    server = CityNexusMCPServer(FakeClient())
    assert [item["name"] for item in server.list_tools()] == list(V1_TOOL_NAMES)
    assert not {"terminal", "file", "browser", "payment", "refund", "set_policy"} & set(
        V1_TOOL_NAMES
    )


def test_mcp_delegates_to_citynexus_agent_api_only():
    fake = FakeClient()
    server = CityNexusMCPServer(fake)
    result = server.call(
        "inspect_twin",
        MCPToolCall(
            request_id="mcp-request-1",
            agent_id="hermes",
            arguments={"journey_id": "journey-1"},
        ),
    )
    assert result["tool"] == "inspect_twin"
    assert fake.calls == [
        (
            "inspect_twin",
            {
                "request_id": "mcp-request-1",
                "agent_id": "hermes",
                "journey_id": "journey-1",
            },
        )
    ]


def test_mcp_v2_exposes_typed_actions_without_changing_v1_discovery():
    fake = FakeClient()
    server = CityNexusMCPServer(fake)
    names = [item["name"] for item in server.list_tools_v2()]
    assert names == list(V2_TOOL_NAMES)
    result = server.call(
        "reschedule_activity",
        MCPToolCall(
            request_id="mcp-v2-request-1",
            agent_id="hermes",
            arguments={
                "journey_id": "journey-1",
                "activity_id": "activity-1",
                "start_at": "2026-10-10T10:00:00+08:00",
                "end_at": "2026-10-10T11:00:00+08:00",
            },
        ),
    )
    assert result["tool"] == "reschedule_activity"


def test_context_guidance_describes_server_owned_replan_and_search_facts():
    assert "newly generated proposal information" in TOOL_DESCRIPTIONS["request_replan"]
    assert "immediate list_replans reread is normally redundant" in TOOL_DESCRIPTIONS[
        "request_replan"
    ]
    assert "previously persisted Replan proposals" in TOOL_DESCRIPTIONS["list_replans"]
    assert "do not repeatedly search an unchanged query" in TOOL_DESCRIPTIONS[
        "search_city_services"
    ]


def test_mcp_rejects_unknown_arguments_and_tools():
    server = CityNexusMCPServer(FakeClient())
    try:
        server.call(
            "inspect_twin",
            MCPToolCall("mcp-request-2", "hermes", {"url": "http://evil.invalid"}),
        )
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown arguments must fail closed")

    try:
        server.call("raw_sql", MCPToolCall("mcp-request-3", "hermes", {}))
    except ValueError as exc:
        assert "allowlist" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown tools must fail closed")


def test_request_replan_emits_a_validated_full_fidelity_proposal_view():
    candidate = {
        "id": "proposal-id",
        "proposed_plan_version_id": "plan-id",
        "status": "PRESENTED",
        "hard_constraints_passed": True,
        "locked_commitments_preserved": True,
        "utility_score": 0.75,
        "diff": {
            "candidate_family": "REPLACE",
            "kept": ["place:a"],
            "moved": [],
            "removed": [],
            "added": ["place:b"],
            "cost_delta_minor": 50,
            "travel_delta_min": -3,
            "end_time_delta_min": 4,
            "reason_codes": ["MINIMAL_DISRUPTION"],
            "preserved_protected": [],
            "after_feasibility_envelope": {
                "budget_remaining_minor": 950,
                "journey_end_slack_min": 25,
                "preserved_commitment_count": 0,
                "risk_reason_codes": [],
            },
            "verified_evidence": {
                "commitment_preservation": {"preserved": True, "protected_count": 0}
            },
        },
    }

    class ProposalClient(FakeClient):
        def call(self, tool_name, payload):
            return {"tool": tool_name, "proposal": candidate, "proposals": [candidate]}

    server = CityNexusMCPServer(ProposalClient())
    result = server.call(
        "request_replan",
        MCPToolCall(
            "hermes-r4-request-0001",
            "hermes",
            {"journey_id": "journey-id", "trigger_event_id": "event-id"},
        ),
    )

    assert result["proposal_contract_version"] == "hermes-replan-proposal-view.v1"
    assert result["proposal_views"][0]["proposed_plan_version_id"] == "plan-id"
    assert result["proposal_views"][0]["feasibility_envelope"]["budget_remaining_minor"] == 950


def test_request_replan_rejects_incomplete_candidate_projection():
    class IncompleteClient(FakeClient):
        def call(self, tool_name, payload):
            return {"tool": tool_name, "proposals": [{"id": "proposal-id", "diff": {}}]}

    server = CityNexusMCPServer(IncompleteClient())
    try:
        server.call(
            "request_replan",
            MCPToolCall("hermes-r4-request-0002", "hermes", {"journey_id": "journey-id"}),
        )
    except ValueError as exc:
        assert "required fields" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("incomplete candidate projections must not be reported as usable")


def test_real_mcp_stdio_protocol_discovers_only_citynexus_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        package_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        existing_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(package_root) + (
            os.pathsep + existing_path if existing_path else ""
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "citynexus_mcp.server"],
            cwd=package_root,
            env=environment,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            names = [item.name for item in result.tools]
            assert names == list(V1_TOOL_NAMES)
            assert all(
                item.inputSchema.get("type") == "object" for item in result.tools
            )

    anyio.run(exercise)


def test_real_mcp_v2_stdio_protocol_discovers_coherent_inspect_plan_act_surface():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        package_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        existing_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(package_root) + (
            os.pathsep + existing_path if existing_path else ""
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "citynexus_mcp.server", "--v2"],
            cwd=package_root,
            env=environment,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            names = [item.name for item in result.tools]
            assert names == list(V2_TOOL_NAMES)
            for item in result.tools:
                request_id = item.inputSchema["properties"]["request_id"]
                assert request_id["minLength"] == 8
                assert request_id["maxLength"] == 128

    anyio.run(exercise)
