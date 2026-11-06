"""Resolve asserted future disruption targets from persisted Journey facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Activity, Booking, Commitment, Journey, PlanVersion


@dataclass(frozen=True)
class EventTarget:
    activity_id: str | None
    occurred_at: str | None
    resolution: str


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_event_target(
    session: Session,
    journey: Journey,
    *,
    event_type: str,
    semantic: dict[str, Any],
    now: datetime,
) -> EventTarget:
    """Use only persisted Activity/Booking/Commitment facts for future scope."""

    if str(semantic.get("effective_time") or "NOW").upper() != "FUTURE":
        return EventTarget(journey.current_activity_id, now.isoformat(), "CURRENT_POSITION")

    plan = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    semantic_entities = semantic.get("entities") if isinstance(semantic.get("entities"), dict) else {}
    subject = str(semantic_entities.get("subject_text") or "").casefold()
    if plan is None:
        if event_type == "SCHEDULE_DELAY":
            return EventTarget(None, journey.end_at, "AUTHORITATIVE_JOURNEY_END")
        return EventTarget(None, None, "UNRESOLVED_REVIEW_REQUIRED")
    activities = list(
        session.scalars(
            select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
        )
    )
    upcoming = [
        activity
        for activity in activities
        if (_parse_time(activity.start_at) or datetime.max.replace(tzinfo=timezone.utc)) >= now
    ]
    bookings = list(session.scalars(select(Booking).where(Booking.journey_id == journey.id)).all())
    commitments = list(session.scalars(select(Commitment).where(Commitment.journey_id == journey.id)).all())
    protected_activity_ids = {item.activity_id for item in commitments if item.activity_id}
    booked_activity_ids = {item.activity_id for item in bookings if item.activity_id}
    words = [word for word in re.split(r"[^\w\u4e00-\u9fff]+", subject) if len(word) > 1]

    def matches(activity: Activity) -> bool:
        haystack = f"{activity.title} {activity.activity_type}".casefold()
        return bool(words and any(word in haystack for word in words))

    candidates = [item for item in upcoming if matches(item)]
    if event_type == "SCHEDULE_DELAY":
        booked = [item for item in upcoming if item.id in booked_activity_ids]
        candidates = candidates or booked
    if not candidates and len(upcoming) == 1:
        candidates = upcoming
    if not candidates and len([item for item in upcoming if item.id in protected_activity_ids]) == 1:
        candidates = [item for item in upcoming if item.id in protected_activity_ids]
    if len(candidates) != 1:
        return EventTarget(None, None, "UNRESOLVED_REVIEW_REQUIRED")
    target = candidates[0]
    return EventTarget(target.id, target.start_at, "AUTHORITATIVE_ACTIVITY_START")
