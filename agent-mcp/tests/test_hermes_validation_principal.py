from citynexus_mcp.server import _agent_id


def test_native_mcp_principal_is_default(monkeypatch):
    monkeypatch.delenv("CITYNEXUS_AGENT_ID", raising=False)
    assert _agent_id() == "mcp-client"


def test_isolated_validation_lane_can_name_its_principal(monkeypatch):
    monkeypatch.setenv("CITYNEXUS_AGENT_ID", "hermes-test")
    assert _agent_id() == "hermes-test"
