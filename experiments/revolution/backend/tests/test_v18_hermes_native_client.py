from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from app.integrations.hermes import native_client as hermes


def test_native_runs_contract_uses_loopback_auth_and_citynexus_mcp_metadata(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "features": {"run_submission": True},
                    "endpoints": {
                        "run_events": {"path": "/v1/runs/{run_id}/events"},
                        "run_approval": {"path": "/v1/runs/{run_id}/approval"},
                    },
                },
            )
        return httpx.Response(201, json={"run_id": "run-v18", "status": "queued"})

    monkeypatch.setattr(
        hermes,
        "settings",
        SimpleNamespace(
            hermes_native_api_url="http://127.0.0.1:9191",
            hermes_api_server_key="loopback-test-key",
            impact_timeout_seconds=2,
            hermes_model="hermes-test",
            hermes_native_profile="citynexus",
        ),
    )
    client = hermes.HermesNativeClient(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.capabilities().ready is True
    run = client.create_run(
        mission_id="mission-v18",
        journey_id="journey-v18",
        input_text="Inspect this Journey.",
        instructions="Use CityNexus MCP only.",
        autonomy="PLAN_ONLY",
    )

    assert run.run_id == "run-v18"
    assert seen[0].headers["authorization"] == "Bearer loopback-test-key"
    body = json.loads(seen[1].content)
    assert body["metadata"] == {
        "citynexus_mission_id": "mission-v18",
        "citynexus_journey_id": "journey-v18",
        "autonomy": "PLAN_ONLY",
        "tool_surface": "citynexus-mcp-only",
    }
    assert "loopback-test-key" not in seen[1].content.decode()
