"""Read-only departure readiness projection for the next meaningful Journey move."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.watch_policy import next_move_projection
from app.db.models import Commitment, Event, Journey, TransportReservation
from app.execution.clock import execution_clock
from app.execution.departure_activation import (
    departure_activation_projection,
    departure_cue_fingerprint,
)

_MATERIAL_EVENT_TYPES = {"WEATHER", "CLOSURE", "TRAFFIC_DELAY", "SCHEDULE_DELAY"}
_BLOCKING_VENUE_STATES = {"CLOSED_CONFLICT", "OUTSIDE_VERIFIED_HOURS"}
_UNCERTAIN_VENUE_STATES = {"UNKNOWN", "PROVIDER_DEGRADED"}


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


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _open_material_events(session: Session, journey_id: str, activity_id: str | None) -> list[Event]:
    rows = list(
        session.scalars(
            select(Event)
            .where(
                Event.journey_id == journey_id,
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
                Event.event_type.in_(tuple(_MATERIAL_EVENT_TYPES)),
            )
            .order_by(Event.detected_at, Event.id)
        ).all()
    )
    relevant: list[Event] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("predictive_only"):
            continue
        if activity_id and row.affected_activity_id and str(row.affected_activity_id) != str(activity_id):
            continue
        relevant.append(row)
    return relevant


def _target_commitments(
    session: Session, journey_id: str, activity_id: str | None
) -> list[Commitment]:
    rows = list(
        session.scalars(
            select(Commitment)
            .where(
                Commitment.journey_id == journey_id,
                Commitment.status == "ACTIVE",
                Commitment.level.in_(("C3_Guaranteed", "C4_Anchored")),
            )
            .order_by(Commitment.created_at, Commitment.id)
        ).all()
    )
    if not activity_id:
        return []
    # Commitment.activity_id is the durable plan-local binding.  Do not fall
    # back to an unrelated C3/C4 row elsewhere in the Journey: that would make
    # any protected fact appear to block the current next move.
    return [row for row in rows if str(row.activity_id or "") == str(activity_id)]


def _next_reservation(
    session: Session,
    journey_id: str,
    *,
    now: datetime,
    activity_id: str | None = None,
    target_start_at: datetime | None = None,
) -> TransportReservation | None:
    rows = list(
        session.scalars(
            select(TransportReservation)
            .where(
                TransportReservation.journey_id == journey_id,
                TransportReservation.status.in_(("CONFIRMED", "ACTIVE", "CANCELLED")),
            )
            .order_by(TransportReservation.scheduled_departure_at, TransportReservation.id)
        ).all()
    )
    if not activity_id:
        return None
    commitments = _target_commitments(session, journey_id, activity_id)
    commitment_ids = {str(row.id) for row in commitments}
    future: list[tuple[datetime, TransportReservation]] = []
    for row in rows:
        departure = _aware(row.scheduled_departure_at)
        if departure is not None and departure >= now - timedelta(minutes=30):
            if target_start_at is not None and departure > target_start_at + timedelta(hours=2):
                continue
            # A confirmed reservation normally points at its C4 commitment.
            # If an older imported row has no commitment link, only accept it
            # when its departure is in the current target's bounded window;
            # never let a remote Journey reservation affect this move.
            if row.commitment_id and str(row.commitment_id) not in commitment_ids:
                continue
            if not row.commitment_id and target_start_at is None:
                continue
            future.append((departure, row))
    return min(future, key=lambda item: item[0])[1] if future else None


def departure_readiness_projection(
    session: Session,
    journey: Journey,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fuse current evidence into one authority-neutral readiness answer."""

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    next_move = next_move_projection(session, journey, now=stamp)
    target = next_move.get("next_activity") or next_move.get("current_activity")
    target_id = str((target or {}).get("id") or "") or None
    starts_at = _aware((target or {}).get("start_at"))
    minutes_until_start = (
        round((starts_at - stamp).total_seconds() / 60.0, 1) if starts_at is not None else None
    )

    venue = next_move.get("venue_readiness") or {}
    venue_state = str(venue.get("state") or "UNKNOWN")
    route = next_move.get("route_evidence") or {}
    route_state = str(route.get("state") or "UNKNOWN")
    route_degraded = bool(route.get("degraded"))
    launch_origin = next_move.get("launch_origin") or route.get("launch_origin")
    launch_edge = next_move.get("launch_edge") or route.get("launch_edge")
    launch_edge_state = str((launch_edge or {}).get("state") or "")
    presence = next_move.get("presence") or {}
    presence_state = str(presence.get("state") or "LOCATION_UNAVAILABLE")

    commitments = _target_commitments(session, journey.id, target_id)
    reservation = _next_reservation(
        session,
        journey.id,
        now=stamp,
        activity_id=target_id,
        target_start_at=starts_at,
    )
    events = _open_material_events(session, journey.id, target_id)

    blocking: list[str] = []
    warnings: list[str] = []

    if venue_state in _BLOCKING_VENUE_STATES:
        blocking.append("VENUE_NOT_COMPATIBLE")
    elif venue_state in _UNCERTAIN_VENUE_STATES:
        warnings.append("VENUE_NOT_VERIFIED")

    if route_degraded or route_state.upper() in {"UNKNOWN", "UNAVAILABLE", "DEGRADED", "FROZEN"} or launch_edge_state == "LAUNCH_ROUTE_UNAVAILABLE":
        warnings.append("ROUTE_EVIDENCE_UNCERTAIN")

    critical_events = [row for row in events if str(row.severity).upper() == "CRITICAL"]
    if critical_events:
        blocking.append("CRITICAL_MATERIAL_EVENT")
    elif events:
        warnings.append("OPEN_MATERIAL_EVENT")

    reservation_state = "NONE"
    realtime_state = None
    if reservation is not None:
        reservation_state = str(reservation.status or "UNKNOWN").upper()
        realtime_state = str(reservation.realtime_status or "UNKNOWN_REALTIME").upper()
        if reservation_state == "CANCELLED" or realtime_state == "CANCELLED":
            blocking.append("TRANSPORT_CANCELLED")
        elif realtime_state in {"UNKNOWN_REALTIME", "STALE_REALTIME"}:
            warnings.append("TRANSPORT_REALTIME_UNCERTAIN")

    commitment_state = "PROTECTED" if commitments else "NONE"

    if presence_state in {"LOCATION_STALE", "LOCATION_IMPRECISE"} and (
        minutes_until_start is not None and minutes_until_start <= 60
    ):
        warnings.append("PRESENCE_NOT_FRESH_ENOUGH")
    elif presence_state == "LOCATION_UNAVAILABLE":
        warnings.append("LOCATION_UNAVAILABLE")

    must_leave_by = None
    safety_buffer_minutes = None
    if "edge_match" not in route:
        # Compatibility for callers supplying the older compact route shape;
        # persisted projections use the explicit NO_TRAVEL_EDGE reason.
        must_leave_reason = "ROUTE_EVIDENCE_DOES_NOT_EXPOSE_TRAVEL_MINUTES"
    elif not route.get("edge_match"):
        must_leave_reason = str(route.get("reason_code") or "NO_TRAVEL_EDGE")
    elif route_degraded:
        must_leave_reason = "ROUTE_EVIDENCE_DEGRADED"
    elif route_state.upper() == "FROZEN":
        must_leave_reason = "ROUTE_EVIDENCE_FROZEN"
    else:
        must_leave_reason = "ROUTE_EVIDENCE_UNKNOWN"
    # Only use an explicit duration from the exact current -> next edge.
    route_minutes = route.get("minutes") if route.get("edge_match") else None
    try:
        travel_minutes = int(route_minutes) if route_minutes is not None else None
    except (TypeError, ValueError):
        travel_minutes = None
    if (
        starts_at is not None
        and travel_minutes is not None
        and travel_minutes >= 0
        and not route_degraded
        and route_state.upper() not in {"DEGRADED", "FROZEN"}
    ):
        safety_buffer_minutes = 10
        must_leave_by = (starts_at - timedelta(minutes=travel_minutes + safety_buffer_minutes)).isoformat()
        must_leave_reason = None

    if not target:
        state = "COMPLETE" if journey.status == "COMPLETED" else "UNKNOWN"
        action = "NONE"
    elif blocking:
        state = "BLOCKED"
        action = "DO_NOT_DEPART"
    elif warnings:
        close = minutes_until_start is not None and minutes_until_start <= 60
        state = "ACTION_NEEDED" if close and any(
            item in {
                "VENUE_NOT_VERIFIED",
                "ROUTE_EVIDENCE_UNCERTAIN",
                "PRESENCE_NOT_FRESH_ENOUGH",
                "OPEN_MATERIAL_EVENT",
            }
            for item in warnings
        ) else "READY_WITH_UNCERTAINTY"
        action = "CHECK_BEFORE_LEAVING" if state == "ACTION_NEEDED" else "LEAVE_ON_SCHEDULE"
    elif minutes_until_start is not None and minutes_until_start > 120:
        state = "NOT_DUE"
        action = "WAIT"
    else:
        state = "READY"
        action = "LEAVE_ON_SCHEDULE"

    return {
        "state": state,
        "target_activity_id": target_id,
        "target_title": (target or {}).get("title"),
        "starts_at": (target or {}).get("start_at"),
        "minutes_until_start": minutes_until_start,
        "must_leave_by": must_leave_by,
        "must_leave_by_absent_reason": must_leave_reason,
        "safety_buffer_minutes": safety_buffer_minutes,
        "launch_origin": launch_origin,
        "launch_edge": launch_edge,
        "blocking_reasons": sorted(set(blocking)),
        "warning_reasons": sorted(set(warnings)),
        "checks": {
            "venue": venue_state,
            "route": route_state,
            "reservation": reservation_state,
            "reservation_realtime": realtime_state,
            "commitment": commitment_state,
            "presence": presence_state,
        },
        "recommended_action": action,
        "authority": "READ_ONLY_PROJECTION",
        "next_move": next_move,
        "evidence": {
            "material_event_ids": [row.id for row in events],
            "commitment_ids": [row.id for row in commitments],
            "reservation_id": reservation.id if reservation is not None else None,
        },
    }


def _time_band(minutes_until_start: float | None) -> str:
    if minutes_until_start is None or minutes_until_start > 120:
        return "QUIET"
    if minutes_until_start > 60:
        return "WINDOW_120_TO_60"
    if minutes_until_start > 30:
        return "WINDOW_60_TO_30"
    if minutes_until_start > 15:
        return "WINDOW_30_TO_15"
    return "WINDOW_15_OR_LESS"


def readiness_fingerprint(projection: dict[str, Any]) -> str:
    """Stable identity for one meaningful readiness answer."""

    material = {
        "journey_target": projection.get("target_activity_id"),
        "state": projection.get("state"),
        "blocking": sorted(projection.get("blocking_reasons") or []),
        "warnings": sorted(projection.get("warning_reasons") or []),
        "reservation": (projection.get("evidence") or {}).get("reservation_id"),
        "commitments": sorted((projection.get("evidence") or {}).get("commitment_ids") or []),
        "band": _time_band(projection.get("minutes_until_start")),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def briefing_priority(projection: dict[str, Any]) -> str | None:
    """Return a notification priority only when the readiness window is useful."""

    minutes = projection.get("minutes_until_start")
    if minutes is None or minutes > 60:
        return None
    state = str(projection.get("state") or "UNKNOWN")
    if minutes <= 15 and state == "BLOCKED":
        return "CRITICAL"
    if minutes <= 15 and state == "ACTION_NEEDED":
        return "HIGH"
    if minutes <= 30 and state in {"ACTION_NEEDED", "READY_WITH_UNCERTAINTY"}:
        return "HIGH"
    return "NORMAL"


def refresh_departure_readiness(
    session: Session,
    journey: Journey,
    *,
    now: datetime | None = None,
    emit_briefing: bool = True,
    refresh_offline: bool = True,
) -> dict[str, Any]:
    """Refresh the read model and, when due, record one deduped briefing."""

    from app.operations.v9 import notify

    projection = departure_readiness_projection(session, journey, now=now)
    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    next_move = projection.get("next_move") or {}
    current_move = next_move.get("current_activity") or {}
    target = next_move.get("next_activity") or current_move or {}
    activity_status = current_move.get("status") if current_move.get("status") in {"EN_ROUTE", "ARRIVED", "IN_PROGRESS"} else target.get("status")
    activation = departure_activation_projection(
        projection,
        activity_status=activity_status,
        now=stamp,
    )
    captured_at = stamp.isoformat()
    notification: dict[str, Any] = {
        "eligible": False,
        "created_or_reused": False,
        "id": None,
        "dedupe_key": None,
        "priority": None,
    }
    priority = briefing_priority(projection)
    if emit_briefing and priority is not None:
        fingerprint = readiness_fingerprint(projection)
        dedupe_key = f"departure-readiness:{journey.id}:{fingerprint}"
        row = notify(
            session,
            journey.id,
            dedupe_key=dedupe_key,
            kind="DEPARTURE_READINESS",
            priority=priority,
            payload={
                "target_activity_id": projection.get("target_activity_id"),
                "state": projection.get("state"),
                "recommended_action": projection.get("recommended_action"),
                "must_leave_by": projection.get("must_leave_by"),
                "summary": projection.get("recommended_action"),
            },
        )
        notification = {
            "eligible": True,
            "created_or_reused": True,
            "id": row.id,
            "dedupe_key": dedupe_key,
            "priority": priority,
            "occurrence_count": row.occurrence_count,
        }
    departure_cue: dict[str, Any] = {
        "eligible": False,
        "created_or_reused": False,
        "id": None,
        "dedupe_key": None,
        "priority": None,
    }
    cue_states = {"LEAVE_SOON", "DUE_NOW", "OVERDUE", "BLOCKED"}
    if (
        emit_briefing
        and activation.get("state") in cue_states
        and activation.get("activity_id")
    ):
        cue_state = str(activation["state"])
        blocker_codes = list(projection.get("blocking_reasons") or [])
        cue_fingerprint = departure_cue_fingerprint(
            journey_id=str(journey.id),
            activity_id=str(activation["activity_id"]),
            activation_state=cue_state,
            readiness_state=str(projection.get("state") or "UNKNOWN"),
            blocker_codes=blocker_codes,
        )
        dedupe_key = f"departure-activation:{journey.id}:{cue_fingerprint}"
        cue_priority = {
            "BLOCKED": "CRITICAL",
            "OVERDUE": "HIGH",
            "DUE_NOW": "HIGH",
            "LEAVE_SOON": "NORMAL",
        }[cue_state]
        cue_copy = {
            "BLOCKED": "Hold before leaving — check the issue first.",
            "OVERDUE": "You’re past the planned leave time. Confirm when you’re on the way.",
            "DUE_NOW": "It’s time to leave for your next stop.",
            "LEAVE_SOON": "You’ll need to leave soon for your next stop.",
        }[cue_state]
        row = notify(
            session,
            journey.id,
            dedupe_key=dedupe_key,
            kind="DEPARTURE_ACTIVATION",
            priority=cue_priority,
            payload={
                "activity_id": activation.get("activity_id"),
                "state": cue_state,
                "readiness_state": projection.get("state"),
                "must_leave_by": projection.get("must_leave_by"),
                "summary": cue_copy,
            },
        )
        departure_cue = {
            "eligible": True,
            "created_or_reused": True,
            "id": row.id,
            "dedupe_key": dedupe_key,
            "priority": cue_priority,
            "occurrence_count": row.occurrence_count,
            "state": cue_state,
        }
    projection = {
        **projection,
        "departure_activation": {**activation, "captured_at": captured_at, "cue": departure_cue},
        "captured_at": captured_at,
        "notification": notification,
    }
    if refresh_offline:
        from app.operations.v9 import refresh_offline_snapshot

        refresh_offline_snapshot(session, journey)
    return projection
