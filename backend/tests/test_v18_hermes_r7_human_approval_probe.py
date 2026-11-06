from __future__ import annotations

from pathlib import Path

from scripts.hermes_r7_human_approval_probe import (
    _accept_response,
    _model_metrics,
    _pending_accept,
    _semantic_accept,
    _write_mcp_wrapper,
)


def test_r7_wrapper_is_real_python_and_records_response_size(tmp_path: Path):
    path = tmp_path / "wrapper.py"
    _write_mcp_wrapper(path)
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    assert "response_serialized_bytes" in source
    assert "approve_agent_action" not in source


def test_r7_pending_accept_requires_server_approval_id():
    run = {
        "mcp_calls": [
            {"kind": "mcp.request", "tool_name": "accept_replan",
             "request": {"request_id": "blocked-1", "proposal_id": "proposal-1"}},
            {"kind": "mcp.response", "tool_name": "accept_replan",
             "response": {"executed": False, "decision": "REQUIRE_CONFIRMATION",
                          "approval_id": "approval-1", "status": "PENDING",
                          "proposal_id": "proposal-1", "request_id": "blocked-1"}},
        ]
    }
    pending = _pending_accept(run)
    assert pending == {
        "approval_id": "approval-1",
        "proposal_id": "proposal-1",
        "request_id": "blocked-1",
        "decision": "REQUIRE_CONFIRMATION",
        "approval_status": "PENDING",
        "response": run["mcp_calls"][1]["response"],
    }


def test_r7_pending_accept_rejects_missing_approval():
    run = {"mcp_calls": [{"kind": "mcp.response", "tool_name": "accept_replan",
                          "response": {"executed": False, "decision": "REQUIRE_CONFIRMATION"}}]}
    assert _pending_accept(run) is None


def test_r7_semantic_accept_projection_excludes_volatile_request_id():
    left = {
        "tool": "accept_replan", "request_id": "request-a", "decision": "REQUIRE_CONFIRMATION",
        "reason_codes": ["USER_APPROVAL_RECEIPT_CONSUMED"], "executed": True,
        "approval_id": "approval-1", "status": "CONSUMED", "user_confirmed": False,
        "result": {"proposal": {"id": "proposal-1", "status": "ACCEPTED"},
                   "journey": {"status": "ACTIVE", "active_plan_version_id": "plan-2"}},
    }
    right = {**left, "request_id": "request-b"}
    assert _semantic_accept(left) == _semantic_accept(right)


def test_r7_metrics_include_mcp_byte_measurements():
    metrics = _model_metrics({
        "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        "mcp_calls": [
            {"kind": "mcp.request"},
            {"kind": "mcp.response", "response_serialized_bytes": 17},
            {"kind": "mcp.response", "response_serialized_bytes": 23},
        ],
    })
    assert metrics["total_tokens"] == 8
    assert metrics["mcp_request_count"] == 1
    assert metrics["largest_mcp_response_bytes"] == 23
    assert metrics["cumulative_mcp_response_bytes"] == 40


def test_r7_accept_response_observes_only_server_response():
    run = {"mcp_calls": [
        {"kind": "mcp.response", "tool_name": "accept_replan",
         "response": {"executed": False}},
        {"kind": "mcp.response", "tool_name": "accept_replan",
         "response": {"executed": True}},
    ]}
    assert _accept_response(run, executed=False) == {"executed": False}
    assert _accept_response(run, executed=True) == {"executed": True}
