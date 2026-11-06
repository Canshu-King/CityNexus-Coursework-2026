"""Pure semantic contract for one exact on-demand mobility edge."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

ROUTE_TRUTH_POLICY_VERSION = "r41-route-truth-v1"
ROUTE_PROVIDER_POLICY_VERSION = "r41-provider-policy-v1"

QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
UNAVAILABLE = "UNAVAILABLE"
FAILED = "FAILED"

OUTCOME_ROUTE_READY = "ROUTE_READY"
OUTCOME_PROVIDER_DEGRADED = "ROUTE_PROVIDER_DEGRADED"
OUTCOME_ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
OUTCOME_PROVIDER_FAILURE = "ROUTE_PROVIDER_FAILURE"
OUTCOME_INPUT_INVALID = "ROUTE_INPUT_INVALID"

TERMINAL_STATUSES = frozenset({COMPLETED, UNAVAILABLE, FAILED})


@dataclass(frozen=True)
class RouteRequest:
    journey_id: str
    origin_key: str
    target_key: str
    target_activity_id: str | None
    requested_mode: str
    provider_policy_version: str = ROUTE_PROVIDER_POLICY_VERSION
    route_truth_policy_version: str = ROUTE_TRUTH_POLICY_VERSION
    origin_evidence: dict[str, Any] | None = None

    def semantic_payload(self) -> dict[str, Any]:
        payload = {
            "journey_id": str(self.journey_id),
            "origin_key": str(self.origin_key),
            "target_key": str(self.target_key),
            "target_activity_id": (
                str(self.target_activity_id) if self.target_activity_id else None
            ),
            "requested_mode": str(self.requested_mode).upper(),
            "provider_policy_version": str(self.provider_policy_version),
            "route_truth_policy_version": str(self.route_truth_policy_version),
        }
        if self.origin_evidence is not None:
            payload["origin_evidence"] = dict(self.origin_evidence)
        return payload


def route_request_fingerprint(request: RouteRequest) -> str:
    """Stable identity for one semantic route request.

    Timestamps, operation ids, labels, and UI copy are intentionally excluded.
    """

    encoded = json.dumps(
        request.semantic_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_capture(
    *,
    explicit_fact: dict[str, Any] | None,
    provider_failed: bool,
    provider_degraded: bool = False,
) -> tuple[str, str]:
    """Map provider/capture truth to durable operation status and outcome."""

    if explicit_fact is not None:
        return (
            COMPLETED,
            OUTCOME_PROVIDER_DEGRADED if provider_degraded else OUTCOME_ROUTE_READY,
        )
    if provider_failed:
        return FAILED, OUTCOME_PROVIDER_FAILURE
    return UNAVAILABLE, OUTCOME_ROUTE_UNAVAILABLE


def fact_is_eligible_for_live_readiness(fact: dict[str, Any] | None) -> bool:
    """Only explicit non-synthetic route truth may drive a live leave-by."""

    if not fact:
        return False
    provenance = str(
        fact.get("provenance")
        or fact.get("route_provenance")
        or fact.get("fact_quality")
        or ""
    ).upper()
    provider = str(fact.get("provider") or fact.get("provider_key") or "").lower()
    duration = fact.get("duration_min")
    if duration is None:
        duration = fact.get("minutes")
    try:
        duration_value = int(duration)
    except (TypeError, ValueError):
        return False
    if duration_value < 0:
        return False
    if provider.startswith(("mock", "fixture")):
        return False
    if provenance in {
        "",
        "SYNTHETIC_FIXTURE",
        "SYNTHETIC_DEFAULT",
        "CITYNEXUS_ESTIMATE",
        "ESTIMATE",
        "TEST_PROVIDER_EMULATOR",
    }:
        return False
    return True
