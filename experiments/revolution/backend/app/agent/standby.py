"""Deterministic trigger authority for the R20 standby portfolio."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class StandbyTriggerMatch:
    compatible: bool
    code: str
    details: dict[str, Any]


def _load(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
        return value
    except (TypeError, json.JSONDecodeError):
        return fallback


def _expired(evidence: dict[str, Any]) -> bool:
    value = evidence.get("expires_at")
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return True


def standby_trigger_match(standby: Any, event: Any | None, current_plan: Any | None) -> StandbyTriggerMatch:
    """Match a standby contract to current event and reality, fail closed."""
    contract = _load(getattr(standby, "trigger_contract_json", None), {})
    evidence = _load(getattr(standby, "mobility_evidence_json", None), {})
    if getattr(standby, "lifecycle", None) != "STANDBY":
        return StandbyTriggerMatch(False, "NOT_STANDBY", {})
    if event is None:
        return StandbyTriggerMatch(False, "NO_EVENT", {})
    if current_plan is None or getattr(standby, "parent_plan_version_id", None) != getattr(current_plan, "id", None):
        return StandbyTriggerMatch(False, "PARENT_STALE", {"parent_plan_version_id": getattr(standby, "parent_plan_version_id", None)})
    if _expired(evidence):
        return StandbyTriggerMatch(False, "EVIDENCE_EXPIRED", {"expires_at": evidence.get("expires_at")})
    if str(getattr(event, "event_type", "")) not in set(contract.get("allowed_event_types") or []):
        return StandbyTriggerMatch(False, "TYPE_MISMATCH", {"event_type": getattr(event, "event_type", None)})
    if str(getattr(event, "provenance", "")) not in set(contract.get("allowed_provenance") or []):
        return StandbyTriggerMatch(False, "PROVENANCE_MISMATCH", {"provenance": getattr(event, "provenance", None)})
    severity_order = {"INFO": 0, "MINOR": 1, "MAJOR": 2, "CRITICAL": 3}
    required = contract.get("minimum_severity")
    if required and severity_order.get(str(getattr(event, "severity", "INFO")), -1) < severity_order.get(str(required), 99):
        return StandbyTriggerMatch(False, "THRESHOLD_NOT_MET", {"required": required, "actual": getattr(event, "severity", None)})
    event_activity = str(getattr(event, "affected_activity_id", None) or "")
    event_place = str((getattr(event, "payload_json", "") or ""))
    try:
        event_payload = json.loads(event_place or "{}")
    except json.JSONDecodeError:
        event_payload = {}
    event_place = str(event_payload.get("place_id") or "")
    activity_scope = set(str(item) for item in (contract.get("affected_activity_ids") or []))
    place_scope = set(str(item) for item in (contract.get("affected_place_ids") or []))
    if activity_scope and event_activity not in activity_scope and event_place not in place_scope:
        return StandbyTriggerMatch(False, "SCOPE_MISMATCH", {"activity_id": event_activity, "place_id": event_place})
    if place_scope and not activity_scope and event_place and event_place not in place_scope:
        return StandbyTriggerMatch(False, "SCOPE_MISMATCH", {"place_id": event_place})
    return StandbyTriggerMatch(True, "MATCH", {
        "scenario_type": contract.get("scenario_type"),
        "event_type": getattr(event, "event_type", None),
        "provenance": getattr(event, "provenance", None),
    })


def contract_for_simulation(*, scenario_type: str, dropped_activity_ids: list[str], dropped_place_ids: list[str], simulation_id: str, parent_plan_id: str) -> dict[str, Any]:
    if scenario_type == "WEATHER_SHIFT":
        allowed_events = ["WEATHER"]
        provenance = ["PROVIDER_VERIFIED", "SYSTEM_DERIVED"]
    elif scenario_type == "PLACE_CLOSURE":
        allowed_events = ["CLOSURE", "SERVICE_UNAVAILABLE"]
        provenance = ["PROVIDER_VERIFIED", "USER_REPORTED"]
    else:
        allowed_events = ["TRAFFIC_DELAY", "SCHEDULE_DELAY"]
        # A traveler-observed delay is a valid signal for a mobility branch;
        # the Gateway still requires event type, scope, severity, freshness,
        # and current-parent equality before it can mutate reality.
        provenance = ["PROVIDER_VERIFIED", "SYSTEM_DERIVED", "USER_REPORTED"]
    return {
        "scenario_type": scenario_type,
        "allowed_event_types": allowed_events,
        "affected_activity_ids": sorted(set(dropped_activity_ids)),
        "affected_place_ids": sorted(set(dropped_place_ids)),
        "allowed_provenance": provenance,
        "minimum_severity": "MINOR",
        "parent_plan_version_id": parent_plan_id,
        "counterfactual_simulation_id": simulation_id,
    }
