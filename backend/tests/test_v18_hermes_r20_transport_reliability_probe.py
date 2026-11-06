from scripts.hermes_r20_transport_reliability_probe import CORE_ASSERTIONS, EXPECTED_TOOLS


def test_r20_core_gate_keeps_all_transport_and_authority_assertions():
    assert "real_stdio_mcp_candidate_exists" in CORE_ASSERTIONS
    assert "mcp_contract_versioned_and_complete" in CORE_ASSERTIONS
    assert "proposal_only_one_active_plan_unchanged" in CORE_ASSERTIONS
    assert EXPECTED_TOOLS[4] == "request_replan"
    assert len(EXPECTED_TOOLS) == 11
