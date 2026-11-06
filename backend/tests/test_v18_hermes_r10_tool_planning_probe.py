from scripts.hermes_r10_tool_planning_probe import (
    _acceptance,
    _post_success_reads,
    _semantic_replan,
)


def _trial(*, calls=4, post_reads=0, redundant_reads=0, total=80, response_bytes=400):
    return {
        "usage": {"total_tokens": total},
        "mcp_call_count": calls,
        "measurement": {"cumulative_tool_response_bytes": response_bytes},
        "post_success_reads": {
            "list_replans_after_success": post_reads,
            "redundant_list_replans_after_success": redundant_reads,
        },
        "semantic_replan": {"proposal_contract_version": "hermes-replan-proposal-view.v1"},
        "assertions": {
            "real_model": True,
            "completed": True,
            "request_replan_observed": True,
            "proposal_contract_present": True,
            "semantic_replan_present": True,
            "native_event_stream_nonempty": True,
            "loopback_guard_installed": True,
            "no_non_loopback_connection_attempt": True,
            "no_mcp_errors": True,
            "no_mutation_tool": True,
            "journey_authority_unchanged": True,
            "no_booking_commitment_payment_mutation": True,
        },
    }


def test_r10_records_redundant_post_success_list_replans():
    trace = [
        {
            "kind": "mcp.response",
            "tool_name": "request_replan",
            "request_id": "request-1",
            "response": {"proposal_views": [{"proposal_id": "proposal-1"}]},
        },
        {
            "kind": "mcp.response",
            "tool_name": "list_replans",
            "request_id": "list-1",
            "response": {"proposals": [{"id": "proposal-1"}]},
        },
    ]

    result = _post_success_reads(trace)

    assert result["list_replans_after_success"] == 1
    assert result["redundant_list_replans_after_success"] == 1


def test_r10_semantic_projection_removes_volatile_identity_but_keeps_truth():
    trace = [{
        "kind": "adapter.result",
        "tool_name": "request_replan",
        "result": {
            "decision": "ALLOW_AUTO",
            "request_id": "request-1",
            "proposal_contract_version": "hermes-replan-proposal-view.v1",
            "proposal_views": [{
                "id": "proposal-1",
                "status": "PRESENTED",
                "hard_constraints_passed": True,
                "locked_commitments_preserved": True,
                "diff": {"added": ["place-city-park"]},
            }],
            "proposals": [{"id": "proposal-1", "status": "PRESENTED"}],
        },
    }]

    result = _semantic_replan(trace)

    assert result is not None
    assert result["proposal_contract_version"] == "hermes-replan-proposal-view.v1"
    assert result["proposal_count"] == 1
    assert result["projection"]["proposal_views"][0]["status"] == "PRESENTED"
    assert "id" not in result["projection"]["proposal_views"][0]


def test_r10_acceptance_requires_post_success_read_reduction():
    baseline = {
        "stats": {
            "total_tokens": {"median": 100},
            "mcp_response_bytes": {"median": 1000},
            "mcp_call_count": {"median": 8},
            "post_success_list_replans": {"median": 1},
            "redundant_post_success_list_replans": {"median": 1},
        }
    }
    variant = {
        "stats": {
            "total_tokens": {"median": 80},
            "mcp_response_bytes": {"median": 700},
            "mcp_call_count": {"median": 6},
            "post_success_list_replans": {"median": 0},
            "redundant_post_success_list_replans": {"median": 0},
        },
        "trials": [_trial(calls=6, post_reads=0, redundant_reads=0)],
    }

    result = _acceptance(baseline, variant)

    assert result["median_post_success_list_replans_decreased"] is True
    assert result["median_redundant_post_success_list_replans_decreased"] is True
    assert result["decision"] == "ACCEPTED"
