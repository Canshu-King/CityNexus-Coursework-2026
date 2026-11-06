from scripts.hermes_r17_provider_state_reliability_probe import (
    _provider_fixture_truth,
    _provider_observation,
    _proposal_provider_truth,
    _read_only_provider_truth,
)


def _seed(state: str = "DEGRADED") -> dict:
    return {
        "event": {
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "provenance": "SIMULATED",
            "provider_key": "demo-simulator",
            "payload": {
                "kind": "SYNTHETIC_PROVIDER_STATE_FIXTURE",
                "provider_key": "synthetic-mobility-provider",
                "provider_state": state,
                "provider_provenance": "SYNTHETIC_HARNESS_FIXTURE",
                "live_provider_evidence": False,
            },
        },
        "provider_state_contract": {
            "provider_key": "synthetic-mobility-provider",
            "provider_state": state,
            "live_provider_evidence": False,
        },
    }


def test_r17_fixture_is_explicitly_synthetic_and_not_live():
    assert _provider_fixture_truth(_seed("DEGRADED")) is True
    assert _provider_fixture_truth(_seed("UNAVAILABLE")) is True
    changed = _seed("DEGRADED")
    changed["event"]["payload"]["live_provider_evidence"] = True
    assert _provider_fixture_truth(changed) is False


def test_r17_server_response_must_surface_state_without_live_claim():
    trace = [
        {
            "kind": "mcp.response",
            "tool_name": "inspect_twin",
            "response": {
                "twin": {
                    "context": {"degraded_providers": ["synthetic-mobility-provider"]}
                }
            },
        },
        {
            "kind": "mcp.response",
            "tool_name": "inspect_journey",
            "response": {
                "event": {
                    "payload": {
                        "provider_key": "synthetic-mobility-provider",
                        "provider_state": "DEGRADED",
                    }
                }
            },
        },
    ]
    observation = _provider_observation(trace, _seed("DEGRADED"))
    assert observation["provider_state_seen"] is True
    assert observation["degraded_provider_context_seen"] is True
    assert observation["synthetic_provider_not_live"] is True


def test_r17_aggregate_degraded_projection_is_distinguished_from_exact_state():
    trace = [
        {
            "kind": "mcp.response",
            "tool_name": "inspect_twin",
            "response": {
                "twin": {
                    "projection_status": "DEGRADED",
                    "context": {
                        "feasibility_envelope": {
                            "risk_reason_codes": ["PROVIDER_DEGRADED"]
                        }
                    },
                }
            },
        }
    ]
    observation = _provider_observation(trace, _seed("UNAVAILABLE"))
    assert observation["exact_provider_state_exposed"] is False
    assert observation["aggregate_degraded_state_seen"] is True
    assert observation["provider_state_seen"] is True


def test_r17_structured_live_claim_is_not_accepted_as_synthetic_truth():
    trace = [
        {
            "kind": "mcp.response",
            "tool_name": "request_replan",
            "response": {
                "proposal": {
                    "diff": {
                        "verified_evidence": {
                            "mobility": {"snapshot": {"route_provenance": "LIVE_PROVIDER_CAPTURE"}}
                        }
                    }
                }
            },
        }
    ]
    observation = _provider_observation(trace, _seed("DEGRADED"))
    assert observation["synthetic_provider_not_live"] is False


def test_r17_proposal_and_read_only_contracts_are_distinct():
    proposal = {"response_count": 1, "full_contract_observed": True, "all_server_proposal_ids_persisted": True}
    assert _proposal_provider_truth(proposal, []) is True
    assert _proposal_provider_truth(proposal, ["accept_replan"]) is False
    before = {"entities": {"proposals": [], "plans": [], "events": []}}
    after = {"entities": {"proposals": [], "plans": [], "events": []}}
    assert _read_only_provider_truth(before, after, {"response_count": 0}, []) is True
