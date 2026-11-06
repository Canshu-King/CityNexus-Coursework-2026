from __future__ import annotations

from typing import Any

SIMULATED_STATE_TRANSITION = "SIMULATED_STATE_TRANSITION"
OPERATOR_CONFIRMED_EXECUTION = "OPERATOR_CONFIRMED_EXECUTION"
DEVICE_LOCATION_VERIFIED_EXECUTION = "DEVICE_LOCATION_VERIFIED_EXECUTION"

EXECUTION_TRUTH_KINDS = {
    SIMULATED_STATE_TRANSITION,
    OPERATOR_CONFIRMED_EXECUTION,
    DEVICE_LOCATION_VERIFIED_EXECUTION,
}


def append_execution_truth(
    metadata: dict[str, Any],
    *,
    kind: str,
    transition: str,
    captured_at: str,
    actor_type: str,
    actor_id: str | None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one durable statement about *how* an execution state became true.

    Activity.status answers the state-machine question (EN_ROUTE, ARRIVED, ...).
    This record answers the real-world evidence question.  Keeping them
    separate prevents a fast HTTP demo from being reported as physical travel.
    """

    if kind not in EXECUTION_TRUTH_KINDS:
        raise ValueError(f"unsupported execution truth kind: {kind}")
    record = {
        "kind": kind,
        "transition": transition,
        "captured_at": captured_at,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "evidence": dict(evidence or {}),
    }
    history = metadata.get("execution_truth_history")
    if not isinstance(history, list):
        history = []
    history = [item for item in history if isinstance(item, dict)][-19:]
    history.append(record)
    metadata["execution_truth"] = record
    metadata["execution_truth_history"] = history
    return record


def current_execution_truth(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    record = metadata.get("execution_truth")
    if not isinstance(record, dict):
        return None
    if record.get("kind") not in EXECUTION_TRUTH_KINDS:
        return None
    return dict(record)
