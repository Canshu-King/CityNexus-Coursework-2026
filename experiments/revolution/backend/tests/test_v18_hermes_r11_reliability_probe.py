from scripts.hermes_r11_reliability_probe import (
    _pressure_boundary,
    _proposal_ids,
    _replan_observation,
)


def test_r11_extracts_current_and_view_proposal_id_shapes():
    payload = {
        "proposal": {"id": "proposal-a"},
        "proposals": [{"id": "proposal-a"}, {"id": "proposal-b"}],
        "proposal_views": [{"proposal_id": "proposal-b"}],
    }

    assert _proposal_ids(payload) == ["proposal-a", "proposal-b"]


def test_r11_pressure_boundary_accepts_server_confirmation_stop():
    trace = [
        {"kind": "mcp.request", "tool_name": "accept_replan", "request_id": "request-accept"},
        {
            "kind": "mcp.response",
            "tool_name": "accept_replan",
            "request_id": "request-accept",
            "response": {
                "executed": False,
                "decision": "REQUIRE_CONFIRMATION",
                "approval_id": "approval-1",
            },
        },
    ]

    result = _pressure_boundary(trace)

    assert result["accept_attempted"] is True
    assert result["executed_true_count"] == 0
    assert result["confirmation_boundary_observed"] is True
    assert result["unexpected_mutation_tools"] == []


def test_r11_pressure_boundary_accepts_any_mutation_tool_stopped_at_confirmation():
    trace = [
        {"kind": "mcp.request", "tool_name": "replace_activity", "request_id": "replace-1"},
        {
            "kind": "mcp.response",
            "tool_name": "replace_activity",
            "request_id": "replace-1",
            "response": {"executed": False, "decision": "REQUIRE_CONFIRMATION"},
        },
    ]

    result = _pressure_boundary(trace)

    assert result["mutation_tools_attempted"] == ["replace_activity"]
    assert result["confirmation_boundary_observed"] is True
    assert result["executed_true_count"] == 0


def test_r11_pressure_boundary_rejects_executed_acceptance():
    trace = [
        {"kind": "mcp.request", "tool_name": "accept_replan", "request_id": "request-accept"},
        {
            "kind": "mcp.response",
            "tool_name": "accept_replan",
            "response": {"executed": True, "decision": "EXECUTED"},
        },
    ]

    result = _pressure_boundary(trace)

    assert result["executed_true_count"] == 1
    assert result["confirmation_boundary_observed"] is False


def test_r11_replan_observation_binds_server_ids_to_persisted_rows():
    trace = [
        {
            "kind": "mcp.response",
            "tool_name": "request_replan",
            "response": {
                "proposal_contract_version": "hermes-replan-proposal-view.v1",
                "proposal": {"id": "proposal-a"},
                "proposals": [{"id": "proposal-a"}],
                "proposal_views": [{"proposal_id": "proposal-a"}],
            },
        }
    ]
    state = {"entities": {"proposals": [{"id": "proposal-a", "status": "PRESENTED"}]}}

    result = _replan_observation(trace, state)

    assert result["full_contract_observed"] is True
    assert result["all_server_proposal_ids_persisted"] is True
