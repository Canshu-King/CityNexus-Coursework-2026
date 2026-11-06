"""Journey Inbox semantics for grounded reservation-change intake."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboxImpactDecision:
    state: str
    material: bool
    reason: str


def inbox_artifact_fingerprint(
    *,
    source_kind: str,
    normalized_text: str | None = None,
    artifact_sha256: str | None = None,
) -> str:
    """Stable identity for replay-safe intake; never use timestamps/run ids."""
    payload = {
        "source_kind": str(source_kind or "").upper(),
        "normalized_text": " ".join(str(normalized_text or "").split()).strip(),
        "artifact_sha256": str(artifact_sha256 or "").lower().strip(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reservation_change_fingerprint(
    *,
    journey_id: str,
    confirmation_ref: str | None,
    change_kind: str,
    reservation_type: str | None,
    old_value: Any,
    new_value: Any,
) -> str:
    payload = {
        "journey_id": str(journey_id),
        "confirmation_ref": str(confirmation_ref or "").strip().upper(),
        "change_kind": str(change_kind or "").upper(),
        "reservation_type": str(reservation_type or "").upper(),
        "old_value": old_value,
        "new_value": new_value,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_inbox_impact(
    *,
    exact_binding: bool,
    change_kind: str,
    affects_protected_commitment: bool,
    schedule_delta_min: int | None = None,
    cancellation: bool = False,
    current_execution_affected: bool = False,
) -> InboxImpactDecision:
    """Classify impact after truth has been grounded by server-owned binding."""
    if not exact_binding:
        return InboxImpactDecision(
            state="BINDING_REQUIRED",
            material=False,
            reason="RESERVATION_IDENTITY_NOT_EXACT",
        )

    kind = str(change_kind or "").upper()
    if kind == "UNKNOWN":
        return InboxImpactDecision(
            state="NEEDS_REVIEW",
            material=False,
            reason="CHANGE_KIND_UNKNOWN",
        )

    if cancellation:
        return InboxImpactDecision(
            state="RECOVERY_REQUIRED",
            material=True,
            reason=(
                "PROTECTED_COMMITMENT_CANCELLED"
                if affects_protected_commitment
                else "RESERVATION_CANCELLED"
            ),
        )

    delta = abs(int(schedule_delta_min or 0))
    if current_execution_affected:
        return InboxImpactDecision(
            state="RECOVERY_REQUIRED",
            material=True,
            reason="CURRENT_EXECUTION_CONFLICT",
        )

    if affects_protected_commitment and delta >= 15:
        return InboxImpactDecision(
            state="RECOVERY_REQUIRED",
            material=True,
            reason="PROTECTED_COMMITMENT_TIME_CHANGED",
        )

    if delta >= 30:
        return InboxImpactDecision(
            state="RECOVERY_REQUIRED",
            material=True,
            reason="MATERIAL_SCHEDULE_CHANGE",
        )

    return InboxImpactDecision(
        state="APPLIED_NO_MATERIAL_IMPACT",
        material=False,
        reason="TRUTH_UPDATED_WITHIN_TOLERANCE",
    )


def normalize_change_kind(value: str | None) -> str:
    kind = str(value or "").strip().upper()
    aliases = {
        "CONFIRMED": "NEW_CONFIRMATION",
        "CONFIRMATION": "NEW_CONFIRMATION",
        "TIME_CHANGE": "SCHEDULE_CHANGE",
        "RESCHEDULED": "SCHEDULE_CHANGE",
        "CANCELLED": "CANCELLATION",
        "CANCELED": "CANCELLATION",
        "VENUE_CHANGE": "LOCATION_CHANGE",
    }
    kind = aliases.get(kind, kind)
    return kind if kind in {
        "NEW_CONFIRMATION",
        "SCHEDULE_CHANGE",
        "CANCELLATION",
        "LOCATION_CHANGE",
        "STATUS_CHANGE",
    } else "UNKNOWN"


def inbox_provenance_for_source(source_kind: str) -> str:
    kind = str(source_kind or "").upper()
    return {
        "PASTED_TEXT": "USER_PASTED_TEXT",
        "IMAGE_UPLOAD": "USER_UPLOADED_IMAGE",
        "EMAIL_FORWARD_REPLAY": "CAPTURED_REPLAY",
        "LAB_MESSAGE_FIXTURE": "LAB_FIXTURE",
    }.get(kind, "USER_SUPPLIED_UNVERIFIED")
