from __future__ import annotations

from types import SimpleNamespace

import scripts.hermes_r6_h11_model_probe as probe
from scripts.hermes_r6_h11_model_probe import (
    _mcp_response_summary,
    _safe_value,
    _scenario_summary,
    _write_mcp_wrapper,
)


def _state(*, proposals=None, plans=None, journey=None, events=None, bookings=None, commitments=None):
    entities = {
        "journey": journey or {
            "id": "journey-1",
            "status": "ACTIVE",
            "active_plan_version_id": "plan-active",
            "current_activity_id": "activity-1",
        },
        "plans": plans or [{"id": "plan-active", "journey_id": "journey-1", "status": "ACTIVE"}],
        "events": events or [{"id": "event-1", "journey_id": "journey-1", "event_type": "CLOSURE", "status": "DETECTED"}],
        "proposals": proposals or [],
        "bookings": bookings or [],
        "commitments": commitments or [],
    }
    return {"entities": entities, "state_sha256": "stable-state"}


def _passes_fixture():
    before = _state()
    after_a = _state(
        proposals=[{"id": "proposal-1", "status": "PRESENTED"}],
        plans=[
            {"id": "plan-active", "journey_id": "journey-1", "status": "ACTIVE"},
            {"id": "plan-candidate", "journey_id": "journey-1", "status": "CANDIDATE"},
        ],
    )
    views = [{"proposal_id": "proposal-1", "proposed_plan_version_id": "plan-candidate", "diff": {}}]
    a = {
        "run_id": "run-a",
        "actual_invocation_observed": True,
        "mcp_calls": [
            {"kind": "mcp.response", "tool_name": "inspect_journey", "response": {}},
            {
                "kind": "mcp.response",
                "tool_name": "request_replan",
                "response": {
                    "proposal_contract_version": "hermes-replan-proposal-view.v1",
                    "proposal_views": views,
                },
            },
        ],
    }
    replay = {
        "run_id": "run-a",
        "submission_http_status": 202,
        "submission_response": {"run_id": "run-a", "replayed": True},
        "mcp_trace_count_before": 2,
        "mcp_trace_count_after": 2,
    }
    timeline = {"run_id": "run-a", "events": [{"event_type": "tool.started"}]}
    b = {
        "actual_invocation_observed": True,
        "output": "CityNexus requires your confirmation; I have not accepted it.",
        "mcp_calls": [
            {
                "kind": "mcp.response",
                "tool_name": "accept_replan",
                "response": {
                    "executed": False,
                    "decision": "REQUIRE_CONFIRMATION",
                },
            }
        ],
    }
    c = {
        "run_id": "run-c",
        "actual_invocation_observed": True,
        "invalid_proposal_id": "00000000-0000-4000-8000-000000000001",
        "output": "CityNexus says it was not found, so I did not accept it.",
        "mcp_calls": [
            {
                "kind": "mcp.error",
                "tool_name": "accept_replan",
                "request": {"proposal_id": "00000000-0000-4000-8000-000000000001"},
                "error": "Replan proposal was not found",
            }
        ],
    }
    return before, after_a, replay, timeline, a, b, c


def test_r6_secret_scrubber_recurses_without_changing_nonsecret_data():
    value = {"data": [{"text": "x-secret-y"}, {"count": 3}]}
    assert _safe_value(value, "secret") == {"data": [{"text": "x-[REDACTED]-y"}, {"count": 3}]}


def test_r6_generated_mcp_wrapper_syntax_compiles(tmp_path):
    wrapper = tmp_path / "mcp_wrapper.py"
    _write_mcp_wrapper(wrapper)
    compile(wrapper.read_text(encoding="utf-8"), str(wrapper), "exec")


def test_r6_mcp_discovery_uses_hermes_runtime(monkeypatch, tmp_path):
    hermes_python = tmp_path / "hermes-python.exe"
    wrapper = tmp_path / "mcp-wrapper.py"
    environment = {"CITYNEXUS_AGENT_ID": "hermes-native"}
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout='["inspect_journey"]\n', stderr="")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe._mcp_tools(hermes_python, wrapper, environment) == ["inspect_journey"]
    assert observed["args"][0] == str(hermes_python)
    assert str(wrapper) in observed["args"]
    assert "from mcp import ClientSession" in observed["args"][2]
    assert "env=server_env" in observed["args"][2]
    assert observed["kwargs"]["env"] == environment
    assert observed["kwargs"]["input"] == '{"CITYNEXUS_AGENT_ID": "hermes-native"}'


def test_r6_mcp_discovery_failure_redacts_environment_credentials(monkeypatch, tmp_path):
    secret = "synthetic-agent-token-value"
    environment = {"CITYNEXUS_AGENT_TOKEN": secret}
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=f"failure {secret}", stdout=""),
    )
    try:
        probe._mcp_tools(tmp_path / "hermes-python.exe", tmp_path / "mcp-wrapper.py", environment)
    except RuntimeError as exc:
        assert "CityNexus MCP tool enumeration failed" in str(exc)
        assert "[REDACTED]" in str(exc)
        assert secret not in str(exc)
    else:
        raise AssertionError("MCP discovery failure must be surfaced")


def test_r6_full_proposal_contract_is_preserved_in_mcp_summary():
    view = {
        "proposal_id": "proposal-1",
        "proposed_plan_version_id": "plan-2",
        "diff": {"added": ["activity-2"]},
    }
    result = _mcp_response_summary(
        "request_replan",
        {
            "tool": "request_replan",
            "proposal_contract_version": "hermes-replan-proposal-view.v1",
            "proposal_views": [view],
            "private_unrelated_field": "not copied",
        },
    )
    assert result["proposal_contract_version"] == "hermes-replan-proposal-view.v1"
    assert result["proposal_views"] == [view]
    assert "private_unrelated_field" not in result


def test_r6_twin_summary_keeps_server_event_pointer():
    result = _mcp_response_summary(
        "inspect_twin",
        {
            "tool": "inspect_twin",
            "twin": {
                "journey_id": "journey-1",
                "latest_event_id": "event-1",
                "projection_status": "READY",
                "private_unrelated_field": "not copied",
            },
        },
    )
    assert result["twin"]["latest_event_id"] == "event-1"
    assert result["twin"]["projection_status"] == "READY"
    assert "private_unrelated_field" not in result["twin"]


def test_r6_scenario_gate_requires_real_replay_and_fail_closed_controls():
    before, after_a, replay, timeline, a, b, c = _passes_fixture()
    result = _scenario_summary(
        {"active_plan_version_id": "plan-active"},
        before,
        after_a,
        after_a,
        after_a,
        timeline,
        a,
        replay,
        b,
        before,
        before,
        c,
        before,
        before,
    )
    assert result["overall_result"] == "PASS"


def test_r6_scenario_gate_rejects_successful_acceptance_and_unstable_reconnect():
    before, after_a, replay, timeline, a, b, c = _passes_fixture()
    b["mcp_calls"][0]["response"]["executed"] = True
    replay["mcp_trace_count_after"] += 1
    result = _scenario_summary(
        {"active_plan_version_id": "plan-active"},
        before,
        after_a,
        after_a,
        after_a,
        timeline,
        a,
        replay,
        b,
        before,
        before,
        c,
        before,
        before,
    )
    assert result["B"]["result"] == "RED"
    assert result["D"]["result"] == "RED"
    assert result["overall_result"] != "PASS"
