from __future__ import annotations

import httpx

from app.agent.runtime import _server_completion_contract
from app.agent.orchestrator import HermesJourneyAgent
from app.core.config import settings as runtime_settings
from dataclasses import replace
import json


def test_r25_defer_is_a_typed_non_mutating_conclusion_after_comparing_proposal() -> None:
    trace = [
        {
            "tool_name": "request_replan",
            "status": "COMPLETED",
            "result": {"proposal": {"id": "proposal-current-window"}},
        },
        {
            "tool_name": "conclude_recovery",
            "status": "COMPLETED",
            "result": {
                "requested_outcome": "NO_ACTION",
                "horizon_decision": "DEFER",
                "reason": "Preserve optional budget for a higher-value later window.",
            },
        },
    ]

    assert _server_completion_contract(trace, run_status="COMPLETED") == {
        "kind": "NO_ACTION",
        "source": "server_trace",
    }


def test_r25_plain_no_action_still_requires_no_server_proposal() -> None:
    trace = [
        {
            "tool_name": "conclude_recovery",
            "status": "COMPLETED",
            "result": {"requested_outcome": "NO_ACTION"},
        }
    ]
    assert _server_completion_contract(trace, run_status="COMPLETED")["kind"] == "NO_ACTION"


def test_r25_model_timeout_is_machine_visible_before_real_fallback(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "cn:glm-5.3-flash":
            raise httpx.ReadTimeout("bounded test timeout", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "READY"}}]},
        )

    monkeypatch.setattr(
        "app.agent.orchestrator.settings",
        replace(
            runtime_settings,
            hermes_api_url="http://wb2api.test/v1/chat/completions",
            hermes_api_key="test-key",
            hermes_model="cn:glm-5.3-flash",
            hermes_model_fallbacks=("cn:deepseek-v4.1-flash",),
        ),
    )
    agent = HermesJourneyAgent(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert agent._complete([])["content"] == "READY"
    assert calls == ["cn:glm-5.3-flash", "cn:deepseek-v4.1-flash"]
    assert agent.external_attempts[0]["status"] == "FAILED_EXTERNAL"
    assert agent.external_attempts[0]["reason_code"] == "MODEL_TIMEOUT"
    assert agent.external_attempts[0]["model"] == "cn:glm-5.3-flash"
