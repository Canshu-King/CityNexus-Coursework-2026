import httpx
import pytest

from citynexus_mcp.client import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    REQUEST_REPLAN_TIMEOUT_SECONDS,
    CityNexusAgentClient,
)


def test_agent_client_preserves_server_owned_error_detail():
    def respond(request):
        return httpx.Response(
            409,
            json={"error": {"code": "CONFLICT", "message": "AGENT_IDEMPOTENCY_RESPONSE_SCHEMA_UNAVAILABLE"}},
            request=request,
        )

    client = CityNexusAgentClient(
        base_url="http://127.0.0.1:8000",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ValueError, match="^AGENT_IDEMPOTENCY_RESPONSE_SCHEMA_UNAVAILABLE$"):
        client.call("request_replan", {"request_id": "hermes-test-request"})


def test_request_replan_gets_backend_generation_timeout_budget():
    class RecordingClient:
        def __init__(self):
            self.calls = []

        def post(self, url, *, json, headers, timeout):
            self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return httpx.Response(200, json={"tool": "request_replan"})

    transport = RecordingClient()
    client = CityNexusAgentClient(base_url="http://127.0.0.1:8000", client=transport)
    client.call("request_replan", {"request_id": "hermes-test-request"})
    client.call("inspect_twin", {"request_id": "hermes-test-request-2"})

    assert transport.calls[0]["timeout"] == REQUEST_REPLAN_TIMEOUT_SECONDS
    assert transport.calls[1]["timeout"] == DEFAULT_TOOL_TIMEOUT_SECONDS
