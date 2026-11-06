"""Normalize Hermes runtime events into a safe CityNexus-facing contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


NORMALIZED_EVENT_TYPES = frozenset(
    {
        "semantic.interpreted",
        "mission.planned",
        "run.started",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "gateway.checked",
        "guardian.checked",
        "proposal.created",
        "approval.required",
        "run.completed",
        "run.failed",
        "run.stopped",
    }
)

TOOL_LABELS = {
    "inspect_journey": "Inspect current Journey",
    "inspect_twin": "Inspect JourneyTwin",
    "list_replans": "Inspect proposals and protected commitments",
    "search_city_services": "Search candidate places and services",
    "request_replan": "Request deterministic replan",
    "accept_replan": "Accept replan (CityNexus approval required)",
    "replace_activity": "Replace activity (CityNexus approval required)",
    "reschedule_activity": "Reschedule activity (CityNexus approval required)",
    "create_booking": "Create booking (CityNexus approval required)",
    "cancel_refundable_booking": "Cancel refundable booking (CityNexus approval required)",
    "request_commitment_change": "Request commitment change (CityNexus approval required)",
}

_IGNORED_EVENT_NAMES = frozenset(
    {
        "message.delta",
        "reasoning.available",
        "reasoning.delta",
        "content.delta",
        "heartbeat",
    }
)


def _string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _event_name(raw: Mapping[str, Any]) -> str:
    value = raw.get("event_type") or raw.get("event") or raw.get("type") or raw.get("name")
    return str(value or "run.started").strip().lower().replace("_", ".")


def _tool_name(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("tool_name") or raw.get("tool") or raw.get("name")
    if not value:
        return None
    name = str(value)
    if "__" in name:
        name = name.rsplit("__", 1)[-1]
    return name


def _safe_summary(value: Any) -> dict[str, Any] | str | None:
    """Keep result summaries useful while excluding free-form model output."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in (
            "decision",
            "status",
            "approval_status",
            "proposal_id",
            "mission_id",
            "executed",
            "error",
            "tool",
        ):
            if key in value and isinstance(value[key], (str, int, float, bool)):
                result[key] = value[key]
        if not result:
            result["keys"] = sorted(str(key) for key in value.keys())[:20]
        return result
    if isinstance(value, (str, int, float, bool)):
        # Hermes can put model prose/reasoning in a lifecycle result. Keep a
        # bounded shape without copying free-form output into the audit/UI
        # contract; authoritative proposal facts come from CityNexus tools.
        if isinstance(value, str):
            return {"kind": "text", "length": len(value)}
        return {"value": value}
    if isinstance(value, list):
        return {"items": len(value)}
    return None


def _normalize_type(name: str, raw: Mapping[str, Any], tool: str | None) -> str:
    if name in NORMALIZED_EVENT_TYPES:
        return name
    if name in {"run", "started", "run_started", "run.started"}:
        return "run.started"
    if name in {"completed", "succeeded", "run_completed", "run.completed"}:
        return "run.completed"
    if name in {"failed", "error", "run_failed", "run.failed"}:
        return "run.failed"
    if name in {"stopped", "cancelled", "canceled", "run_stopped", "run.stopped"}:
        return "run.stopped"
    if "approval" in name:
        return "approval.required"
    if "gateway" in name or "policy" in name:
        return "gateway.checked"
    if "guardian" in name:
        return "guardian.checked"
    if "proposal" in name and "created" in name:
        return "proposal.created"
    if tool:
        if "failed" in name or raw.get("error") is True:
            return "tool.failed"
        if "completed" in name or "finished" in name or raw.get("status") in {"completed", "succeeded"}:
            return "tool.completed"
        return "tool.started"
    return "run.started"


def normalize_hermes_event(
    raw: Mapping[str, Any], *, run_id: str | None = None
) -> dict[str, Any] | None:
    """Map one Hermes event to the stable CityNexus runtime event contract."""

    original = _event_name(raw)
    if original in _IGNORED_EVENT_NAMES:
        return None
    tool = _tool_name(raw)
    event_type = _normalize_type(original, raw, tool)
    timestamp = raw.get("timestamp") or raw.get("created_at") or raw.get("started_at")
    duration = raw.get("duration_ms")
    if duration is None and raw.get("duration") is not None:
        try:
            duration = round(float(raw["duration"]) * 1000)
        except (TypeError, ValueError):
            duration = None
    status = str(raw.get("status") or "started").lower()
    if event_type.endswith("completed"):
        status = "completed"
    elif event_type.endswith("failed"):
        status = "failed"
    elif event_type == "approval.required":
        status = "approval_required"
    elif event_type.endswith("stopped"):
        status = "stopped"
    elif status not in {"started", "completed", "failed", "approval_required", "stopped"}:
        status = "started"
    result = raw.get("result")
    if result is None:
        result = raw.get("output")
    if result is None and "error" in raw:
        result = {"error": raw.get("error")}
    decision = raw.get("policy_decision") or raw.get("decision")
    if decision is None and isinstance(result, Mapping):
        decision = result.get("decision")
    approval_status = raw.get("approval_status")
    if approval_status is None and isinstance(result, Mapping):
        approval_status = result.get("approval_status") or result.get("status")
    source = str(raw.get("source") or "HERMES_NATIVE").upper()
    if source not in {"HERMES_NATIVE", "CITYNEXUS"}:
        source = "HERMES_NATIVE"
    normalized: dict[str, Any] = {
        "event_type": event_type,
        "run_id": _string(run_id or raw.get("run_id")),
        "event_id": _string(raw.get("event_id") or raw.get("id")),
        "sequence": raw.get("sequence") if isinstance(raw.get("sequence"), int) else None,
        "tool_name": tool,
        "friendly_label": TOOL_LABELS.get(tool or "", "Hermes runtime") if tool else "Hermes runtime",
        "status": status,
        "timestamp": timestamp,
        "duration_ms": duration,
        "result_summary": _safe_summary(result),
        "policy_decision": _string(decision),
        "raw_event_type": original,
        "source": source,
    }
    # Preserve an approval id or proposal id when Hermes returns one, but never
    # copy raw arguments or model text into the public contract.
    for key in ("approval_id", "proposal_id", "mission_id"):
        value = raw.get(key)
        if value is None and isinstance(result, Mapping):
            value = result.get(key)
        if value is not None:
            normalized[key] = str(value)
    if approval_status is not None and isinstance(approval_status, (str, int, float, bool)):
        normalized["approval_status"] = str(approval_status)
    return normalized
