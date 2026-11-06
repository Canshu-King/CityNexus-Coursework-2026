from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class JourneyTwinProjection:
    journey_id: str
    active_plan_version_id: str | None
    current_activity_id: str | None
    latest_event_id: str | None
    projection_status: str
    snapshot_at: str
    upcoming_commitments: tuple[dict[str, Any], ...]
    context: dict[str, Any]

    @classmethod
    def from_authoritative_facts(
        cls,
        *,
        journey_id: str,
        plan_id: str | None,
        current_activity_id: str | None,
        latest_event_id: str | None,
        commitments: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> JourneyTwinProjection:
        return cls(
            journey_id=journey_id,
            active_plan_version_id=plan_id,
            current_activity_id=current_activity_id,
            latest_event_id=latest_event_id,
            projection_status="READY",
            snapshot_at=datetime.now(timezone.utc).isoformat(),
            upcoming_commitments=tuple(commitments),
            context=dict(context or {}),
        )
