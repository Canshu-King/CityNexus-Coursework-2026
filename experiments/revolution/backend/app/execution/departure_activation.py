"""Read-only departure activation timing and cue identity."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def departure_activation_projection(
    readiness: dict[str, Any],
    *,
    activity_status: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Project whether departure is due without asserting that movement occurred."""

    status = str(activity_status or "").upper()
    activity_id = readiness.get("target_activity_id")
    target_title = readiness.get("target_title")
    must_leave_by = readiness.get("must_leave_by")
    readiness_state = str(readiness.get("state") or "UNKNOWN")

    def result(**values: Any) -> dict[str, Any]:
        return {
            "activity_id": activity_id,
            "target_title": target_title,
            "must_leave_by": must_leave_by,
            "readiness_state": readiness_state,
            **values,
        }

    if status == "EN_ROUTE":
        return result(
            state="ALREADY_EN_ROUTE",
            minutes_to_leave=None,
            can_confirm_departure=False,
            recommended_action="CONTINUE_ROUTE",
            authority_required=None,
        )
    if status in {"ARRIVED", "IN_PROGRESS", "COMPLETED", "SKIPPED", "CANCELLED"}:
        return result(
            state="ARRIVED_OR_LATER" if status != "COMPLETED" else "COMPLETE",
            minutes_to_leave=None,
            can_confirm_departure=False,
            recommended_action="NONE",
            authority_required=None,
        )

    readiness_state = str(readiness.get("state") or "UNKNOWN").upper()
    if readiness_state == "BLOCKED":
        return result(
            state="BLOCKED",
            minutes_to_leave=None,
            can_confirm_departure=False,
            recommended_action="CHECK_BLOCKER",
            authority_required="OPERATOR_CONFIRMED_EXECUTION",
        )

    leave_at = _aware(readiness.get("must_leave_by"))
    if leave_at is None:
        return result(
            state="NO_ROUTE",
            minutes_to_leave=None,
            can_confirm_departure=False,
            recommended_action="CHECK_ROUTE",
            authority_required="OPERATOR_CONFIRMED_EXECUTION",
        )

    stamp = now.astimezone(timezone.utc)
    minutes = (leave_at - stamp).total_seconds() / 60.0
    if minutes > 30:
        state = "NOT_DUE"
        action = "WAIT"
    elif minutes > 10:
        state = "LEAVE_SOON"
        action = "CONFIRM_WHEN_LEAVING"
    elif minutes >= -5:
        state = "DUE_NOW"
        action = "CONFIRM_DEPARTURE"
    else:
        state = "OVERDUE"
        action = "CONFIRM_DEPARTURE"

    return result(
        state=state,
        minutes_to_leave=round(minutes, 1),
        can_confirm_departure=True,
        recommended_action=action,
        authority_required="OPERATOR_CONFIRMED_EXECUTION",
    )


def departure_cue_fingerprint(
    *,
    journey_id: str,
    activity_id: str,
    activation_state: str,
    readiness_state: str,
    blocker_codes: list[str] | tuple[str, ...] = (),
) -> str:
    """Stable notification identity for one meaningful activation band."""

    payload = {
        "journey_id": str(journey_id),
        "activity_id": str(activity_id),
        "activation_state": str(activation_state).upper(),
        "readiness_state": str(readiness_state).upper(),
        "blocker_codes": sorted({str(item) for item in blocker_codes}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
