from scripts.hermes_r9_mcp_context_probe import _acceptance, _read_measurement


def _trace(text_bytes=100, structured_bytes=200, complete_bytes=350):
    result = {
        "tool": "request_replan",
        "request_id": "r1",
        "proposal": {"id": "p1"},
        "proposals": [{"id": "p1"}],
        "proposal_views": [{"id": "p1"}],
        "proposal_contract_version": "hermes-replan-proposal-view.v1",
    }
    return [
        {"kind": "mcp.request", "tool_name": "request_replan", "request_id": "r1", "request": {"request_id": "r1"}, "request_json_bytes": 40},
        {"kind": "backend.http", "tool_name": "request_replan", "response_json_bytes": 500},
        {"kind": "adapter.result", "tool_name": "request_replan", "request_id": "r1", "result": result, "result_json_bytes": 600},
        {"kind": "mcp.response", "tool_name": "request_replan", "request_id": "r1", "response": result, "response_serialized_bytes": 600},
        {"kind": "mcp.callresult", "tool_name": "request_replan", "request_id": "r1", "text_bytes": text_bytes,
         "structuredContent_after_bytes": structured_bytes, "complete_calltool_result_bytes": complete_bytes,
         "structuredContent_before_sha256": "same", "structuredContent_after_sha256": "same",
         "structuredContent_matches_adapter_result": True},
    ]


def test_r9_measurement_derives_envelope_layers_and_proposal_identity():
    value = _read_measurement(_trace())

    assert value["backend_http_response_bytes"] == 500
    assert value["adapter_result_bytes"] == 600
    assert value["textcontent_bytes"] == 100
    assert value["structured_content_bytes"] == 200
    assert value["complete_calltool_result_bytes"] == 350
    component = value["request_replan_components"][0]
    assert component["proposal_equals_proposals_0"] is True
    assert component["proposal_sha256"] != component["proposals_sha256"]


def _trial(total_tokens, text_bytes, calls):
    return {
        "usage": {"total_tokens": total_tokens},
        "measurement": {"textcontent_bytes": text_bytes, "actual_tool_call_count": calls},
        "assertions": {
            "real_model": True,
            "request_replan_observed": True,
            "structured_content_unchanged": True,
            "structured_content_matches_adapter_result": True,
            "native_event_stream_nonempty": True,
            "loopback_guard_installed": True,
            "no_non_loopback_connection_attempt": True,
            "no_mutation_tool": True,
            "journey_authority_unchanged": True,
            "no_booking_commitment_payment_mutation": True,
        },
    }


def test_r9_acceptance_requires_median_tokens_text_and_non_regressed_calls():
    baseline = {"stats": {"total_tokens": {"median": 100}, "textcontent_bytes": {"median": 1000}, "actual_tool_call_count": {"median": 4}}}
    variant = {"stats": {"total_tokens": {"median": 80}, "textcontent_bytes": {"median": 700}, "actual_tool_call_count": {"median": 4}}, "trials": [_trial(80, 700, 4)]}

    assert _acceptance(baseline, variant)["decision"] == "ACCEPTED"

    worse_bytes = {"stats": {"total_tokens": {"median": 80}, "textcontent_bytes": {"median": 1200}, "actual_tool_call_count": {"median": 4}}, "trials": [_trial(80, 1200, 4)]}
    assert _acceptance(baseline, worse_bytes)["decision"] == "REJECTED"
