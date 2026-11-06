"""Overnight Steward projection for multi-day Journey continuity.

Read-only product state. Day-close writes (Twin learning, briefing notification)
must be performed by an explicit server-owned overnight operation.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import (
    Activity,
    Commitment,
    Journey,
    JourneyLeg,
    JourneyMember,
    JourneyOutcomeEvent,
    OvernightStewardCycle,
    TransportReservation,
    TravelerPreferenceEvidence,
)
from app.execution.clock import execution_clock


_EXECUTING = {"EN_ROUTE", "ARRIVED", "IN_PROGRESS"}
_TERMINAL = {"COMPLETED", "SKIPPED", "CANCELLED"}


def _aware(value: str | None, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def overnight_steward_state(
    *,
    journey_status: str,
    timezone_name: str,
    activities: Iterable[dict[str, Any]],
    now: datetime,
    day_closed: bool = False,
    learning_in_progress: bool = False,
    tomorrow_ready: bool = False,
    tomorrow_needs_attention: bool = False,
    provider_degraded: bool = False,
    brain_blocked: bool = False,
) -> dict[str, Any]:
    """Project whether the same Journey Operator should steward overnight."""

    if str(journey_status).upper() == "COMPLETED":
        return {
            "state": "INACTIVE",
            "reason": "JOURNEY_COMPLETE",
            "local_date": now.astimezone(ZoneInfo(timezone_name)).date().isoformat(),
            "tomorrow_activity_ids": [],
        }

    stamp = now.astimezone(timezone.utc)
    local_now = stamp.astimezone(ZoneInfo(timezone_name))
    rows = [dict(item) for item in activities]

    executing = [
        item for item in rows
        if str(item.get("status") or "").upper() in _EXECUTING
    ]
    if executing:
        return {
            "state": "INACTIVE",
            "reason": "EXECUTION_STILL_ACTIVE",
            "local_date": local_now.date().isoformat(),
            "tomorrow_activity_ids": [],
        }

    tomorrow_rows: list[tuple[datetime, dict[str, Any]]] = []
    for item in rows:
        if str(item.get("status") or "").upper() in _TERMINAL:
            continue
        start = _aware(item.get("start_at"), timezone_name)
        if start is None:
            continue
        local_start = start.astimezone(ZoneInfo(timezone_name))
        if local_start.date() > local_now.date():
            tomorrow_rows.append((start, item))
    tomorrow_rows.sort(key=lambda pair: pair[0])
    tomorrow_ids = [str(item.get("id")) for _start, item in tomorrow_rows[:3]]

    if not tomorrow_rows:
        return {
            "state": "INACTIVE",
            "reason": "NO_FUTURE_LOCAL_DAY",
            "local_date": local_now.date().isoformat(),
            "tomorrow_activity_ids": [],
        }

    if provider_degraded:
        state = "OVERNIGHT_PROVIDER_DEGRADED"
    elif brain_blocked:
        state = "OVERNIGHT_BRAIN_BLOCKED"
    elif learning_in_progress:
        state = "LEARNING_FROM_TODAY"
    elif tomorrow_needs_attention:
        state = "MORNING_NEEDS_ATTENTION"
    elif tomorrow_ready:
        state = "MORNING_READY"
    elif not day_closed:
        state = "DAY_CLOSING"
    else:
        state = "WATCHING_TOMORROW"

    return {
        "state": state,
        "reason": "FUTURE_LOCAL_DAY_AVAILABLE",
        "local_date": local_now.date().isoformat(),
        "next_local_date": tomorrow_rows[0][0].astimezone(ZoneInfo(timezone_name)).date().isoformat(),
        "tomorrow_activity_ids": tomorrow_ids,
    }


def overnight_cycle_key(*, journey_id: str, local_date: str, plan_version_id: str | None) -> str:
    """Stable day-boundary identity; repeated pulses must converge."""
    return f"overnight:{journey_id}:{local_date}:{plan_version_id or 'no-plan'}"


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _local_date(value: str | None, timezone_name: str) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(ZoneInfo(timezone_name)).date()


def _snapshot_payload(session, traveler_id: str) -> dict[str, Any]:
    from app.execution.traveler_twin import refresh_snapshot, snapshot_payload

    return snapshot_payload(refresh_snapshot(session, traveler_id))


def _eligible_day_event_ids(session, journey_id: str, traveler_id: str, day: date, timezone_name: str) -> set[str]:
    from app.execution.traveler_twin import _event_signal

    events = session.scalars(
        select(JourneyOutcomeEvent)
        .where(JourneyOutcomeEvent.journey_id == journey_id)
        .order_by(JourneyOutcomeEvent.occurred_at_actual, JourneyOutcomeEvent.id)
    ).all()
    eligible: set[str] = set()
    for event in events:
        if str(event.actor_id or "") != str(traveler_id):
            continue
        occurred = _local_date(event.occurred_at_actual, timezone_name)
        if occurred != day:
            continue
        if _event_signal(session, event) is not None:
            eligible.add(str(event.id))
    return eligible


def _new_evidence_for_events(session, traveler_id: str, journey_id: str, event_ids: set[str]) -> list[dict[str, Any]]:
    if not event_ids:
        return []
    rows = session.scalars(
        select(TravelerPreferenceEvidence).where(
            TravelerPreferenceEvidence.traveler_id == traveler_id,
            TravelerPreferenceEvidence.journey_id == journey_id,
        )
    ).all()
    result = []
    for row in rows:
        related = set(str(item) for item in _load(row.related_ids_json, []))
        matched = sorted(related.intersection(event_ids))
        if matched:
            result.append({
                "evidence_id": row.id,
                "dimension": row.preference_dimension,
                "direction": row.direction,
                "source": row.source,
                "outcome_ids": matched,
            })
    return result


def _briefing(session, journey: Journey, cycle_day: date, next_day_rows: list[Activity], now: datetime) -> dict[str, Any]:
    from app.agent.journey_ahead import journey_ahead_projection
    from app.agent.journey_intent import active_journey_intents
    from app.execution.readiness import departure_readiness_projection

    first = next_day_rows[0] if next_day_rows else None
    predecessor = None
    route = {"state": "ROUTE_NEEDED", "exact": False, "duration_min": None, "provenance": None, "must_leave_by": None}
    if first is not None:
        all_rows = list(session.scalars(
            select(Activity).where(Activity.plan_version_id == journey.active_plan_version_id).order_by(Activity.sequence_no)
        ).all()) if journey.active_plan_version_id else []
        predecessor = max((item for item in all_rows if item.sequence_no < first.sequence_no and item.status not in _TERMINAL), key=lambda item: item.sequence_no, default=None)
        if predecessor is not None:
            leg = session.scalar(select(JourneyLeg).where(
                JourneyLeg.plan_version_id == journey.active_plan_version_id,
                JourneyLeg.from_activity_id == predecessor.id,
                JourneyLeg.to_activity_id == first.id,
            ).order_by(JourneyLeg.sequence_no))
            if leg is not None:
                grounded = str(leg.provenance or "").upper() in {
                    "LIVE_PROVIDER_CAPTURE", "LIVE_PROVIDER", "PROVIDER_VERIFIED", "CAPTURED_REPLAY"
                }
                route = {
                    "state": "AVAILABLE" if grounded else "ROUTE_NEEDED",
                    "exact": bool(grounded),
                    "duration_min": int(leg.duration_min) if grounded else None,
                    "provider": leg.provider_key,
                    "provenance": leg.provenance,
                    "captured_at": leg.captured_at,
                    "origin_activity_id": predecessor.id,
                    "destination_activity_id": first.id,
                    "must_leave_by": None,
                }
                if grounded and leg.duration_min is not None:
                    start_at = datetime.fromisoformat(first.start_at.replace("Z", "+00:00"))
                    if start_at.tzinfo is None:
                        start_at = start_at.replace(tzinfo=ZoneInfo(journey.timezone))
                    route["must_leave_by"] = (start_at - timedelta(minutes=int(leg.duration_min) + 10)).isoformat()
    activity_ids = {item.id for item in next_day_rows}
    commitments = session.scalars(select(Commitment).where(
        Commitment.journey_id == journey.id,
        Commitment.status == "ACTIVE",
    )).all()
    reservations = session.scalars(select(TransportReservation).where(
        TransportReservation.journey_id == journey.id,
        TransportReservation.status.not_in(("CANCELLED", "REFUNDED")),
    )).all()
    readiness = departure_readiness_projection(session, journey, now=now)
    ahead = journey_ahead_projection(session, journey, now=now)
    active_intents = active_journey_intents(session, journey, now=now)
    return {
        "state": "MORNING_NEEDS_ATTENTION" if first is not None and not route["exact"] else "MORNING_READY",
        "local_date": cycle_day.isoformat(),
        "next_local_date": (cycle_day + timedelta(days=1)).isoformat(),
        "first_activities": [
            {"id": row.id, "title": row.title, "status": row.status, "start_at": row.start_at, "end_at": row.end_at}
            for row in next_day_rows[:3]
        ],
        "first_activity": {"id": first.id, "title": first.title, "status": first.status, "start_at": first.start_at} if first else None,
        "route": route,
        "readiness": {
            "state": readiness.get("state"),
            "target_activity_id": readiness.get("target_activity_id"),
            "venue_readiness": (readiness.get("next_move") or {}).get("venue_readiness"),
            "must_leave_by": route.get("must_leave_by") if route.get("exact") else None,
            "route_status": route["state"],
        },
        "commitments": [
            {"id": row.id, "activity_id": row.activity_id, "kind": row.kind, "level": row.level, "status": row.status}
            for row in commitments if row.activity_id in activity_ids
        ],
        "reservations": [
            {"id": row.id, "type": row.reservation_type, "scheduled_departure_at": row.scheduled_departure_at, "scheduled_arrival_at": row.scheduled_arrival_at, "realtime_status": row.realtime_status}
            for row in reservations
        ],
        "future_risk": ahead.get("top_risk"),
        "active_intents": active_intents,
        "captured_at": now.isoformat(),
        "read_only": True,
    }


def process_overnight_steward(session, journey: Journey, *, now: datetime | None = None) -> dict[str, Any]:
    """Close one local day and prepare the next, without touching execution truth."""
    if str(journey.status).upper() == "COMPLETED" or not journey.active_plan_version_id:
        return {"state": "INACTIVE", "reason": "JOURNEY_COMPLETE" if journey.status == "COMPLETED" else "NO_ACTIVE_PLAN"}
    stamp = (now or execution_clock.now(journey.timezone)).astimezone(timezone.utc)
    zone = ZoneInfo(journey.timezone)
    local_now = stamp.astimezone(zone)
    # The same closed-day identity is used late at night and the following morning.
    if local_now.hour >= 20:
        close_day = local_now.date()
    elif local_now.hour < 12:
        close_day = local_now.date() - timedelta(days=1)
    else:
        return {"state": "INACTIVE", "reason": "OUTSIDE_OVERNIGHT_WINDOW"}

    rows = list(session.scalars(
        select(Activity).where(Activity.plan_version_id == journey.active_plan_version_id).order_by(Activity.sequence_no)
    ).all())
    if any(str(row.status).upper() in _EXECUTING for row in rows):
        return {"state": "MORNING_NEEDS_ATTENTION", "reason": "EXECUTION_STILL_ACTIVE", "local_date": close_day.isoformat()}
    next_date = close_day + timedelta(days=1)
    next_rows = [
        row for row in rows
        if row.status not in _TERMINAL and _local_date(row.start_at, journey.timezone) == next_date
    ]
    if not next_rows:
        return {"state": "INACTIVE", "reason": "NO_FUTURE_LOCAL_DAY", "local_date": close_day.isoformat()}
    key = overnight_cycle_key(journey_id=journey.id, local_date=close_day.isoformat(), plan_version_id=journey.active_plan_version_id)
    cycle = session.scalar(select(OvernightStewardCycle).where(OvernightStewardCycle.cycle_key == key))
    if cycle is None:
        cycle = OvernightStewardCycle(
            id=str(uuid4()), cycle_key=key, journey_id=journey.id,
            local_date=close_day.isoformat(), next_local_date=next_date.isoformat(),
            plan_version_id=journey.active_plan_version_id, state="DAY_CLOSING",
            created_at=stamp.isoformat(), updated_at=stamp.isoformat(),
        )
        session.add(cycle)
        session.flush()

    summaries = _load(cycle.learning_summary_by_traveler_json, {})
    if cycle.closed_at is None:
        members = session.scalars(select(JourneyMember).where(
            JourneyMember.journey_id == journey.id,
            JourneyMember.removed_at.is_(None),
        )).all()
        traveler_ids = sorted({str(journey.traveler_id), *(str(row.traveler_id) for row in members)})
        consumed: set[str] = set()
        from app.execution.traveler_twin import learn_from_journey
        for traveler_id in traveler_ids:
            before = _snapshot_payload(session, traveler_id)
            eligible_ids = _eligible_day_event_ids(session, journey.id, traveler_id, close_day, journey.timezone)
            after_snapshot = learn_from_journey(session, journey, traveler_id=traveler_id, event_ids=eligible_ids)
            after = __import__("app.execution.traveler_twin", fromlist=["snapshot_payload"]).snapshot_payload(after_snapshot)
            learned = _new_evidence_for_events(session, traveler_id, journey.id, eligible_ids)
            learned_ids = {event_id for item in learned for event_id in item["outcome_ids"]}
            consumed.update(learned_ids)
            before_dirs = {str(item.get("direction")) for item in before.get("learned_soft_preferences", [])}
            after_dirs = {str(item.get("direction")) for item in after.get("learned_soft_preferences", [])}
            summaries[traveler_id] = {
                "twin_before": before,
                "new_consumed_outcome_ids": sorted(learned_ids),
                "twin_after": after,
                "changed_dimensions": sorted(after_dirs - before_dirs),
                "learning_count": len(learned),
                "summary": (
                    "I learned a soft preference from a choice you made today."
                    if learned else "No new preference was learned from today’s eligible outcomes."
                ),
            }
        cycle.consumed_outcome_ids_json = json.dumps(sorted(consumed))
        cycle.learning_summary_by_traveler_json = json.dumps(summaries, ensure_ascii=False)
        cycle.closed_at = stamp.isoformat()

    briefing = _briefing(session, journey, close_day, next_rows[:3], stamp)
    morning = local_now.date() >= next_date and local_now.hour >= 5
    if morning:
        cycle.state = briefing["state"]
        if cycle.morning_ready_at is None:
            cycle.morning_ready_at = stamp.isoformat()
            from app.operations.v9 import notify
            dedupe = f"overnight-morning:{journey.id}:{next_date.isoformat()}"
            exists = session.scalar(select(__import__("app.db.models", fromlist=["NotificationEvent"]).NotificationEvent).where(
                __import__("app.db.models", fromlist=["NotificationEvent"]).NotificationEvent.dedupe_key == dedupe
            ))
            if exists is None:
                notify(
                    session, journey.id, dedupe_key=dedupe,
                    kind="OVERNIGHT_MORNING_BRIEFING",
                    priority="HIGH" if briefing["state"] == "MORNING_NEEDS_ATTENTION" else "NORMAL",
                    payload={"cycle_key": key, "briefing": briefing},
                )
    else:
        cycle.state = "WATCHING_TOMORROW"
    cycle.briefing_json = json.dumps(briefing, ensure_ascii=False, default=str)
    cycle.updated_at = stamp.isoformat()
    session.flush()
    return {"state": cycle.state, "cycle_key": key, "local_date": cycle.local_date, "briefing": briefing, "learning_summary_by_traveler": summaries}


def overnight_steward_projection(session, journey: Journey, *, viewer_traveler_id: str | None = None) -> dict[str, Any]:
    if str(journey.status).upper() == "COMPLETED":
        return {"state": "INACTIVE", "reason": "JOURNEY_COMPLETE", "local_date": None, "learning_summary": None, "briefing": None, "read_only": True}
    local_today = execution_clock.now(journey.timezone).date().isoformat()
    cycle = session.scalar(select(OvernightStewardCycle).where(
        OvernightStewardCycle.journey_id == journey.id,
        OvernightStewardCycle.plan_version_id == journey.active_plan_version_id,
    ).order_by(OvernightStewardCycle.local_date.desc(), OvernightStewardCycle.created_at.desc()))
    if cycle is None:
        return {"state": "INACTIVE", "reason": "NO_OVERNIGHT_CYCLE", "local_date": local_today, "learning_summary": None, "briefing": None, "read_only": True}
    summaries = _load(cycle.learning_summary_by_traveler_json, {})
    traveler_id = str(viewer_traveler_id or journey.traveler_id)
    return {
        "state": cycle.state,
        "reason": "FUTURE_LOCAL_DAY_AVAILABLE",
        "local_date": cycle.local_date,
        "next_local_date": cycle.next_local_date,
        "cycle_key": cycle.cycle_key,
        "consumed_outcome_ids": _load(cycle.consumed_outcome_ids_json, []),
        "learning_summary": summaries.get(traveler_id),
        "briefing": _load(cycle.briefing_json, {}),
        "read_only": True,
    }
