from scripts.hermes_r8_context_probe import _measure_run


def _run(calls):
    return {
        "request": {
            "input": "closed place",
            "instructions": "read only",
            "metadata": {"autonomy": "PLAN_ONLY"},
        },
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "mcp_calls": calls,
    }


def _request(tool, request_id, payload):
    return {
        "kind": "mcp.request",
        "tool_name": tool,
        "request_id": request_id,
        "request": payload,
    }


def _response(tool, request_id, payload, size):
    return {
        "kind": "mcp.response",
        "tool_name": tool,
        "request_id": request_id,
        "response": payload,
        "response_serialized_bytes": size,
    }


def test_r8_measurement_pairs_calls_and_ranks_actual_response_bytes():
    value = _measure_run(_run([
        _request("inspect_journey", "1", {"request_id": "1", "journey_id": "j"}),
        _response("inspect_journey", "1", {"journey": {"status": "ACTIVE"}}, 1200),
        _request("inspect_twin", "2", {"request_id": "2", "journey_id": "j"}),
        _response("inspect_twin", "2", {"twin": {"status": "ACTIVE", "plans": [1]}}, 3200),
    ]))

    assert value["mcp_call_count"] == 2
    assert value["mcp_response_bytes"] == 4400
    assert value["ranked_context_cost_table"][0]["tool_name"] == "inspect_twin"
    assert value["per_call"][0]["request_json_bytes"] > 0
    assert value["per_call"][1]["cumulative_context_bytes_proxy"] > value["per_call"][0]["cumulative_context_bytes_proxy"]
    assert value["model_usage"]["total_tokens"] == 120


def test_r8_measurement_reports_overlap_and_exact_duplicate_payloads():
    payload = {"journey": {"id": "j", "status": "ACTIVE"}}
    value = _measure_run(_run([
        _request("inspect_journey", "1", {"request_id": "1", "journey_id": "j"}),
        _response("inspect_journey", "1", payload, 900),
        _request("inspect_journey", "2", {"request_id": "2", "journey_id": "j"}),
        _response("inspect_journey", "2", payload, 900),
    ]))

    assert value["duplicate_response_count"] == 1
    assert value["per_call"][1]["exact_duplicate_of_previous"] is True
    assert value["per_call"][1]["payload_token_overlap_with_previous"] > 0
    assert value["coverage"]["inspect_journey"] is True
