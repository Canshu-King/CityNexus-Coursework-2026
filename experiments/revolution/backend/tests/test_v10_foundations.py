import json
from datetime import datetime, timezone

import httpx

from app.execution.clock import execution_clock
from app.integrations.ai.responses import DeepSeekResponsesClient
from app.integrations.inbox.ics_parser import parse_reservation_calendar


def test_v10_responses_client_normalizes_text_and_function_calls() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp-v10",
                "model": "deepseek-v4-flash",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
                    {"type": "function_call", "call_id": "call-1", "name": "inspect_journey", "arguments": "{}"},
                ],
                "usage": {"total_tokens": 12},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        result = DeepSeekResponsesClient(
            api_key="test-key", client=transport, base_url="https://example.test/responses"
        ).complete([{"role": "user", "content": "hello"}], tools=[])
    assert result.text == "ok"
    assert result.tool_calls[0]["function"]["name"] == "inspect_journey"
    assert requests[0]["model"] == "deepseek-v4-flash"
    assert requests[0]["input"][0]["role"] == "user"


def test_v10_responses_client_excludes_reasoning_from_schema_text() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-reasoning-json",
                "output": [
                    {"type": "reasoning", "id": "rs-1", "text": "private reasoning"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"intent":"PREFERENCE_CHANGE"}'}],
                    },
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        result = DeepSeekResponsesClient(
            api_key="test-key", client=transport, base_url="https://example.test/responses"
        ).complete([{"role": "user", "content": "classify"}], response_schema={"type": "object"})
    assert result.text == '{"intent":"PREFERENCE_CHANGE"}'
    assert result.continuation_items[0]["type"] == "reasoning"


def test_v10_forced_tool_turn_disables_reasoning_and_keeps_continuation_items() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp-tool-1",
                    "output": [
                        {"type": "reasoning", "id": "rs-1", "summary": []},
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "inspect_journey",
                            "arguments": "{}",
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"id": "resp-tool-2", "output": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = DeepSeekResponsesClient(
            api_key="test-key", client=transport, base_url="https://example.test/responses"
        )
        first = client.complete(
            [{"role": "user", "content": "inspect"}],
            tools=[{"type": "function", "name": "inspect_journey", "parameters": {"type": "object"}}],
            tool_choice="required",
            reasoning_effort="",
        )
        client.complete(
            [
                {"role": "user", "content": "inspect"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": list(first.tool_calls),
                    "_response_items": list(first.continuation_items),
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
            ],
            tools=[{"type": "function", "name": "inspect_journey", "parameters": {"type": "object"}}],
        )
    assert requests[0]["reasoning"] == {"effort": "none"}
    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["input"][1]["type"] == "reasoning"
    assert requests[1]["input"][2]["type"] == "function_call"


def test_v10_ics_parser_keeps_field_provenance() -> None:
    extracted = parse_reservation_calendar(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:CNX-42\nSUMMARY:Rail\nLOCATION:Tianjin West\nDTSTART:20261009T100000Z\nDTEND:20261009T120000Z\nEND:VEVENT\nEND:VCALENDAR"
    )
    assert extracted["confirmation_ref"] == "CNX-42"
    assert extracted["field_provenance"]["scheduled_departure_at"]["source"] == "ICS"


def test_v10_execution_clock_is_freezeable_and_timezone_aware() -> None:
    frozen = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(frozen):
        assert execution_clock.now("Asia/Shanghai").hour == 18
    assert execution_clock.now().tzinfo is not None
