"""Small capability-aware client for the DeepSeek Responses API.

The rest of CityNexus consumes a normalized result instead of depending on a
provider-specific response envelope.  The client deliberately never logs the
authorization header or request payloads that may contain user itinerary data.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class DeepSeekResponsesError(RuntimeError):
    """The configured Responses endpoint could not produce a usable result."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        model: str | None = None,
        response_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        self.model = model
        self.response_id = response_id


@dataclass(frozen=True)
class ResponsesResult:
    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    web_observations: tuple[dict[str, Any], ...] = ()
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    output_types: tuple[str, ...] = ()
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    continuation_items: tuple[dict[str, Any], ...] = ()


def _text_content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "input_text", "text": value}]
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                kind = str(item.get("type") or "input_text")
                if kind in {"text", "input_text", "output_text"}:
                    result.append({"type": "input_text", "text": str(item.get("text") or "")})
                elif kind in {"image_url", "input_image"}:
                    source = item.get("image_url") or item.get("image") or item.get("source")
                    if isinstance(source, dict):
                        source = source.get("url") or source.get("data")
                    if source:
                        result.append({"type": "input_image", "image_url": str(source)})
        return result
    return [{"type": "input_text", "text": json.dumps(value, ensure_ascii=False)}]


def _input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            response_items = message.get("_response_items") or []
            if response_items:
                items.extend(item for item in response_items if isinstance(item, dict))
                continue
            if message.get("content"):
                items.append({"role": "assistant", "content": _text_content(message["content"])})
            for call in tool_calls:
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        items.append({"role": role, "content": _text_content(message.get("content"))})
    return items


def _response_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in tools or []:
        if item.get("type") != "function":
            normalized.append(item)
            continue
        function = item.get("function") or item
        normalized.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object"},
            }
        )
    return normalized


def _output_parts(envelope: dict[str, Any]) -> ResponsesResult:
    # Compatibility for existing provider fakes and a short-lived endpoint
    # alias. The production request is still always sent to Responses.
    output = envelope.get("output")
    if not output and envelope.get("choices"):
        message = (envelope.get("choices") or [{}])[0].get("message") or {}
        output = [{"type": "message", "content": [{"type": "output_text", "text": message.get("content") or ""}]}]
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            output.append(
                {
                    "type": "function_call",
                    "call_id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "{}",
                }
            )
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    continuation_items: list[dict[str, Any]] = []
    output_types: list[str] = []

    def add_observation(
        item: dict[str, Any], *, content: Any = None, annotation: dict[str, Any] | None = None
    ) -> None:
        source = annotation or item
        url = source.get("url") or source.get("source_url")
        if not url:
            return
        observations.append(
            {
                "type": item.get("type"),
                "id": source.get("id") or item.get("id"),
                "url": url,
                "title": source.get("title") or item.get("title"),
                "domain": source.get("domain") or item.get("domain"),
                "content": source.get("content")
                or source.get("snippet")
                or content
                or item.get("content")
                or item.get("text"),
            }
        )

    for item in output or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind:
            output_types.append(kind)
        if kind == "function_call":
            continuation_items.append(item)
            calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
            continue
        if kind == "reasoning":
            # DeepSeek requires this item on the next stateless tool turn. It
            # stays in process memory and is never persisted or shown to users.
            continuation_items.append(item)
        if kind in {"web_search_call", "web_search_result", "web_search"}:
            add_observation(item)
            action = item.get("action")
            if isinstance(action, dict):
                for source in action.get("sources") or action.get("results") or []:
                    if isinstance(source, dict):
                        add_observation(item, annotation=source)
            for source in item.get("sources") or item.get("results") or []:
                if isinstance(source, dict):
                    add_observation(item, annotation=source)
        # Reasoning is continuation state, not assistant-facing text. DeepSeek
        # commonly includes a long reasoning item before the schema-constrained
        # message; mixing both makes downstream JSON parsing fail even though
        # the final message is valid JSON.
        if kind != "reasoning":
            for content in item.get("content") or []:
                if isinstance(content, dict):
                    if content.get("text"):
                        text_parts.append(str(content["text"]))
                    for annotation in content.get("annotations") or []:
                        if isinstance(annotation, dict):
                            add_observation(item, content=content.get("text"), annotation=annotation)
        if item.get("text") and kind not in {"function_call", "reasoning"}:
            text_parts.append(str(item["text"]))
        if kind == "message" and isinstance(item.get("annotations"), list):
            for annotation in item["annotations"]:
                if isinstance(annotation, dict):
                    add_observation(item, annotation=annotation)
    unique_observations: list[dict[str, Any]] = []
    seen_observation_keys: set[tuple[str, str]] = set()
    for observation in observations:
        key = (str(observation.get("url") or ""), str(observation.get("title") or ""))
        if not key[0] or key in seen_observation_keys:
            continue
        seen_observation_keys.add(key)
        unique_observations.append(observation)
    return ResponsesResult(
        text="\n".join(text_parts).strip(),
        tool_calls=tuple(calls),
        web_observations=tuple(unique_observations),
        response_id=str(envelope.get("id")) if envelope.get("id") else None,
        usage=envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {},
        output_types=tuple(output_types),
        raw_metadata={
            "status": envelope.get("status"),
            "model": envelope.get("model"),
            "created_at": envelope.get("created_at"),
        },
        continuation_items=tuple(continuation_items),
    )


class DeepSeekResponsesClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com/responses",
        reasoning_effort: str = "high",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        self.api_key = api_key
        self.model = model.removeprefix("deepseek/")
        self.base_url = base_url
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self._client = client

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str = "citynexus_response",
        enable_web_search: bool = False,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ResponsesResult:
        body: dict[str, Any] = {
            "model": self.model,
            "input": _input_items(messages),
            "temperature": 0,
        }
        effort = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        if effort is not None:
            body["reasoning"] = {"effort": effort or "none"}
        normalized_tools = _response_tools(tools)
        if enable_web_search:
            normalized_tools.append({"type": "web_search"})
        if normalized_tools:
            body["tools"] = normalized_tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": response_schema_name,
                    "schema": response_schema,
                }
            }
        elif json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        owns = self._client is None
        started = time.perf_counter()
        try:
            try:
                response = client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                envelope = response.json()
                if not isinstance(envelope, dict):
                    raise TypeError("DeepSeek Responses envelope is not an object")
            except httpx.HTTPStatusError as exc:
                provider_code = None
                try:
                    payload = exc.response.json()
                    error = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(error, dict) and error.get("code"):
                        provider_code = str(error["code"])
                except (TypeError, ValueError):
                    pass
                status_code = exc.response.status_code
                suffix = f" status={status_code}"
                if provider_code:
                    suffix += f" code={provider_code}"
                raise DeepSeekResponsesError(
                    "DeepSeek Responses request failed" + suffix,
                    status_code=status_code,
                    provider_code=provider_code,
                    model=self.model,
                    response_id=exc.response.headers.get("x-request-id"),
                ) from exc
            except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DeepSeekResponsesError(
                    "DeepSeek Responses request failed",
                    model=self.model,
                ) from exc
            result = _output_parts(envelope)
            return ResponsesResult(
                text=result.text,
                tool_calls=result.tool_calls,
                web_observations=result.web_observations,
                response_id=result.response_id,
                usage=result.usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
                output_types=result.output_types,
                raw_metadata=result.raw_metadata,
                continuation_items=result.continuation_items,
            )
        finally:
            if owns:
                client.close()
