from scripts.hermes_r19_runtime_fallback_reliability_probe import (
    BLACKHOLE_VALHALLA_URL,
    _proposal_boundary_observation,
    _runtime_fallback_observation,
)


def test_r19_uses_loopback_only_provider_failure_fixture():
    assert BLACKHOLE_VALHALLA_URL == "http://127.0.0.1:1"


def test_r19_detects_degraded_synthetic_fallback():
    trace = [{"kind": "mcp.response", "tool_name": "request_replan", "response": {"proposal": {"diff": {"verified_evidence": {"mobility": {"snapshot": {"degraded": True, "facts": [{"route_provenance": "SYNTHETIC_FIXTURE"}]}}}}}}}]
    observed = _runtime_fallback_observation(trace)
    assert observed["runtime_fallback_observed"] is True
    assert observed["synthetic_fallback_route_observed"] is True
    assert observed["no_live_provider_claim"] is True


def test_r19_records_transport_gap_without_losing_persisted_proposal_truth():
    proposal = {
        "id": "proposal-1",
        "journey_id": "journey-1",
        "trigger_event_id": "event-1",
        "base_plan_version_id": "plan-1",
        "proposed_plan_version_id": "plan-2",
        "status": "PRESENTED",
        "hard_constraints_passed": True,
        "locked_commitments_preserved": True,
        "utility_score": 0.5,
        "score_breakdown": {},
        "diff": {},
        "explanation": {},
    }
    trace = [
        {"kind": "mcp.request", "tool_name": "request_replan"},
        {
            "kind": "mcp.response",
            "tool_name": "list_replans",
            "response": {"proposals": [proposal]},
        },
    ]
    observed = _proposal_boundary_observation(
        trace,
        {"replan_observation": {"proposal_ids_persisted_after": ["proposal-1"]}},
    )
    assert observed["server_proposal_contract_observed"] is True
    assert observed["request_replan_transport_gap"] is True
    assert observed["finding"] == "REQUEST_REPLAN_RESPONSE_TIMEOUT_WITH_PERSISTED_PROPOSALS"
