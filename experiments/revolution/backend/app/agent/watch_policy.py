"""Deterministic adaptive scheduling policy for Guardian Autopilot."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Activity, AutopilotEnrollment, Event, Journey
from app.execution.clock import execution_clock
from app.execution.presence import presence_projection

_TERMINAL_ACTIVITY = {"COMPLETED", "SKIPPED", "CANCELLED"}
_MATERIAL_EVENT_TYPES = {"WEATHER", "CLOSURE", "TRAFFIC_DELAY", "SCHEDULE_DELAY"}


def _as_aware(value: str | None, *, timezone_name: str = "UTC") -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def _is_predictive(event: Event) -> bool:
    try:
        return bool(json.loads(event.payload_json or "{}").get("predictive_only"))
    except (TypeError, json.JSONDecodeError):
        return False


def _next_activity(
    session: Session,
    journey: Journey,
    *,
    now: datetime,
) -> tuple[Activity | None, float | None]:
    if not journey.active_plan_version_id:
        return None, None
    rows = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
    )
    candidates: list[tuple[float, Activity]] = []
    for row in rows:
        if row.status in _TERMINAL_ACTIVITY:
            continue
        start = _as_aware(row.start_at, timezone_name=journey.timezone)
        end = _as_aware(row.end_at, timezone_name=journey.timezone)
        if start is None:
            continue
        if end is not None and end <= now:
            continue
        horizon = max(0.0, (start - now).total_seconds())
        candidates.append((horizon, row))
    if not candidates:
        return None, None
    horizon, row = min(candidates, key=lambda item: (item[0], item[1].sequence_no))
    return row, horizon


def _open_material_events(session: Session, journey_id: str) -> list[Event]:
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
    return [row for row in rows if not _is_predictive(row)]



def journey_phase_projection(
    session: Session,
    journey: Journey,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive control-plane Journey phase and bounded monitoring scope.

    Clock inference is allowed to change watch urgency/scope only. It is never
    physical execution truth and never grants mutation authority.
    """

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    if not journey.active_plan_version_id:
        return {
            "journey_phase": "UNKNOWN",
            "phase_reason_code": "NO_ACTIVE_PLAN",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": None,
            "monitored_activity_ids": [],
        }

    rows = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
    )
    active = [row for row in rows if row.status not in _TERMINAL_ACTIVITY]
    explicit_transit = [row for row in active if row.status == "EN_ROUTE"]
    explicit_activity = [row for row in active if row.status in {"ARRIVED", "IN_PROGRESS"}]

    def bounded_from(anchor: Activity | None, include_anchor: bool = True) -> list[str]:
        if anchor is None:
            return [str(row.id) for row in active[:2]]
        try:
            index = active.index(anchor)
        except ValueError:
            return [str(anchor.id)]
        selected = active[index : index + 2] if include_anchor else active[index + 1 : index + 3]
        return [str(row.id) for row in selected]

    if len(explicit_transit) + len(explicit_activity) > 1:
        return {
            "journey_phase": "UNKNOWN",
            "phase_reason_code": "CONTRADICTORY_EXECUTION_TRUTH",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": None,
            "monitored_activity_ids": [str(row.id) for row in active[:2]],
        }
    if explicit_transit:
        row = explicit_transit[0]
        return {
            "journey_phase": "IN_TRANSIT",
            "phase_reason_code": "EXPLICIT_EN_ROUTE_EXECUTION_TRUTH",
            "phase_authority": "EXECUTION_TRUTH",
            "current_activity_id": str(row.id),
            "monitored_activity_ids": bounded_from(row),
        }
    if explicit_activity:
        row = explicit_activity[0]
        return {
            "journey_phase": "IN_ACTIVITY",
            "phase_reason_code": "EXPLICIT_ACTIVE_EXECUTION_TRUTH",
            "phase_authority": "EXECUTION_TRUTH",
            "current_activity_id": str(row.id),
            "monitored_activity_ids": bounded_from(row),
        }
    if not active:
        return {
            "journey_phase": "JOURNEY_COMPLETE",
            "phase_reason_code": "NO_NONTERMINAL_ACTIVITIES",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": None,
            "monitored_activity_ids": [],
        }

    timed: list[tuple[Activity, datetime | None, datetime | None]] = [
        (
            row,
            _as_aware(row.start_at, timezone_name=journey.timezone),
            _as_aware(row.end_at, timezone_name=journey.timezone),
        )
        for row in active
    ]
    current = next(
        (
            row
            for row, start, end in timed
            if start is not None and end is not None and start <= stamp < end
        ),
        None,
    )
    if current is not None:
        return {
            "journey_phase": "IN_ACTIVITY",
            "phase_reason_code": "CLOCK_WITHIN_PLANNED_ACTIVITY_WINDOW",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": str(current.id),
            "monitored_activity_ids": bounded_from(current),
        }

    future = [(row, start) for row, start, _end in timed if start is not None and start > stamp]
    next_row, next_start = min(future, key=lambda item: item[1]) if future else (None, None)
    if next_row is None or next_start is None:
        return {
            "journey_phase": "JOURNEY_COMPLETE",
            "phase_reason_code": "PLAN_WINDOW_ELAPSED",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": None,
            "monitored_activity_ids": [],
        }

    local_now = stamp.astimezone(ZoneInfo(journey.timezone))
    local_next = next_start.astimezone(ZoneInfo(journey.timezone))
    horizon = (next_start - stamp).total_seconds()
    if local_next.date() > local_now.date() and (local_now.hour >= 20 or local_now.hour < 6):
        phase, reason = "OVERNIGHT", "NEXT_ACTIVITY_ON_FUTURE_LOCAL_DAY"
    elif horizon <= 2 * 3600:
        phase, reason = "APPROACHING_NEXT", "NEXT_ACTIVITY_WITHIN_2H"
    else:
        prior_ended = any(end is not None and end <= stamp for _row, _start, end in timed)
        phase = "BETWEEN_ACTIVITIES" if prior_ended else "PRE_TRIP"
        reason = "CLOCK_BETWEEN_PLANNED_ACTIVITIES" if prior_ended else "BEFORE_FIRST_ACTIVITY"

    return {
        "journey_phase": phase,
        "phase_reason_code": reason,
        "phase_authority": "CONTROL_PLANE_ONLY",
        "current_activity_id": None,
        "monitored_activity_ids": bounded_from(next_row),
    }


def next_move_projection(
    session: Session,
    journey: Journey,
    *,
    phase: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a read-only traveller projection for the next meaningful move."""

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    phase_value = phase or journey_phase_projection(session, journey, now=stamp)
    active_rows: list[Activity] = []
    if callable(getattr(session, "scalars", None)) and journey.active_plan_version_id:
        active_rows = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        )
        active_rows = [row for row in active_rows if row.status not in _TERMINAL_ACTIVITY]

    current_id = str(
        phase_value.get("current_activity_id")
        or getattr(journey, "current_activity_id", None)
        or ""
    )
    current = next((row for row in active_rows if str(row.id) == current_id), None)
    if current is not None and phase_value.get("journey_phase") in {"IN_TRANSIT", "IN_ACTIVITY"}:
        next_activity = next(
            (row for row in active_rows if row.sequence_no > current.sequence_no),
            None,
        )
    else:
        next_activity, _ = _next_activity(session, journey, now=stamp)
    target = next_activity or current
    presence = (
        presence_projection(
            session,
            journey,
            target_activity_id=str(target.id) if target is not None else None,
            now=stamp,
        )
        if callable(getattr(session, "scalar", None))
        else {
            "state": "LOCATION_UNAVAILABLE",
            "authority": "NONE",
            "arrival_transition_allowed": False,
            "reason_code": "PRESENCE_ROWS_UNAVAILABLE",
        }
    )

    venue_readiness: dict[str, Any] = {
        "state": "UNKNOWN",
        "activity_id": str(target.id) if target is not None else None,
        "source": None,
        "observed_at": None,
    }
    if callable(getattr(session, "scalar", None)):
        from app.db.models import GuardianPulse, PlanVersion

        latest_pulse = session.scalar(
            select(GuardianPulse)
            .where(GuardianPulse.journey_id == journey.id)
            .order_by(GuardianPulse.started_at.desc(), GuardianPulse.id.desc())
        )
        if latest_pulse is not None:
            try:
                pulse_result = json.loads(latest_pulse.result_json or "{}")
            except (TypeError, json.JSONDecodeError):
                pulse_result = {}
            classifications = (pulse_result.get("context_watch") or {}).get("classifications") or []
            for item in classifications:
                if target is not None and str(item.get("activity_id")) == str(target.id):
                    venue_readiness = {
                        "state": item.get("status") or "UNKNOWN",
                        "activity_id": item.get("activity_id"),
                        "source": item.get("opening_hours", {}).get("source")
                        if isinstance(item.get("opening_hours"), dict)
                        else None,
                        "observed_at": item.get("observation_id"),
                    }
                    break

        plan = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    else:
        plan = None
    mobility_raw: dict[str, Any] = {}
    if plan is not None:
        try:
            mobility_raw = json.loads(plan.mobility_evidence_json or "{}")
        except (TypeError, json.JSONDecodeError):
            mobility_raw = {}
    # Aggregate plan mobility is not enough to answer whether this exact move
    # is routable.  Only expose the frozen fact for the edge represented by the
    # current/next activity pair; a different edge must remain UNKNOWN.
    route_evidence = {
        "state": "UNKNOWN",
        "kind": "ACTIVITY_EDGE",
        "provider": None,
        "provenance": None,
        "captured_at": None,
        "expires_at": None,
        "minutes": None,
        "from_activity_id": None,
        "to_activity_id": None,
        "edge_match": False,
        "degraded": False,
        "reason_code": "NO_TRAVEL_EDGE",
    }
    launch_origin: dict[str, Any] | None = None
    launch_edge: dict[str, Any] | None = None
    edge_origin = current
    edge_destination = next_activity
    if edge_destination is not None and edge_origin is None:
        try:
            destination_index = active_rows.index(edge_destination)
        except ValueError:
            destination_index = 0
        edge_origin = active_rows[destination_index - 1] if destination_index > 0 else None
    if plan is not None and edge_origin is not None and edge_destination is not None:
        facts = mobility_raw.get("facts") if isinstance(mobility_raw, dict) else None
        facts_by_pair = {
            (str(item.get("origin_key")), str(item.get("destination_key"))): item
            for item in (facts or [])
            if isinstance(item, dict) and item.get("origin_key") and item.get("destination_key")
        }
        fact = facts_by_pair.get((str(edge_origin.place_id), str(edge_destination.place_id)))
        if fact is not None:
            degraded = bool(fact.get("degraded", mobility_raw.get("degraded", False)))
            route_evidence = {
                "state": "DEGRADED" if degraded else "AVAILABLE",
                "kind": "ACTIVITY_EDGE",
                "provider": fact.get("provider") or mobility_raw.get("source_provider"),
                "provenance": fact.get("route_provenance") or fact.get("fact_quality"),
                "captured_at": fact.get("captured_at") or mobility_raw.get("captured_at"),
                "expires_at": fact.get("expires_at") or mobility_raw.get("expires_at"),
                "minutes": fact.get("minutes"),
                "from_activity_id": str(edge_origin.id),
                "to_activity_id": str(edge_destination.id),
                "edge_match": True,
                "degraded": degraded,
                "reason_code": "ROUTE_EDGE_MATCHED",
            }

    # Before the first activity there may be no previous activity edge.  Use a
    # grounded launch origin only for that gap, and only accept an exact edge
    # for the target Place.  This remains a projection: no route fact is
    # captured and no execution state is changed here.
    if (
        not route_evidence.get("edge_match")
        and next_activity is not None
        and (
            phase_value.get("journey_phase") == "PRE_TRIP"
            or edge_origin is None
            or (current is not None and str(current.id) == str(next_activity.id))
        )
        and callable(getattr(session, "scalar", None))
    ):
        from app.execution.launch_origin import launch_context_projection

        launch = launch_context_projection(
            session,
            journey,
            target_activity=next_activity,
            previous_activity=None,
            now=stamp,
        )
        launch_origin = launch.get("origin")
        launch_edge = launch.get("edge")
        edge_state = str((launch_edge or {}).get("state") or "LAUNCH_EDGE_UNAVAILABLE")
        if edge_state in {"LAUNCH_ROUTE_READY", "LAUNCH_ROUTE_READY_FROZEN"}:
            edge = launch_edge or {}
            route_evidence = {
                "state": (
                    "FROZEN"
                    if edge_state == "LAUNCH_ROUTE_READY_FROZEN"
                    else ("DEGRADED" if edge.get("degraded") else "AVAILABLE")
                ),
                "kind": "LAUNCH_EDGE",
                "provider": edge.get("provider"),
                "provenance": edge.get("provenance"),
                "captured_at": edge.get("captured_at"),
                "expires_at": edge.get("expires_at"),
                "minutes": edge.get("duration_min"),
                "from_activity_id": None,
                "to_activity_id": str(next_activity.id),
                "origin_key": edge.get("origin_key"),
                "target_key": edge.get("target_key"),
                "edge_match": True,
                "degraded": bool(edge.get("degraded")),
                "reason_code": edge.get("reason_code") or "EXACT_LAUNCH_EDGE_MATCH",
                "launch_origin": launch_origin,
            }
        else:
            route_evidence = {
                **route_evidence,
                "kind": "LAUNCH_EDGE",
                "reason_code": (
                    "LAUNCH_ORIGIN_UNKNOWN"
                    if str((launch_origin or {}).get("state")) == "UNKNOWN_ORIGIN"
                    else "LAUNCH_EDGE_UNAVAILABLE"
                ),
                "launch_origin": launch_origin,
                "launch_edge": launch_edge,
            }

    presence_state = str(presence.get("state") or "LOCATION_UNAVAILABLE")
    if presence_state == "LOCATION_STALE":
        copy = "Your location is too old to judge where you are."
    elif presence_state == "LOCATION_IMPRECISE":
        copy = "Your location is too imprecise to judge where you are."
    elif presence_state == "NEAR_TARGET":
        copy = "You appear close to the next stop. Confirm arrival when you’re there."
    elif presence_state == "VERIFIED_AT_TARGET":
        copy = "Guardian verified a trusted location near the next stop. Confirm arrival to continue."
    elif presence_state == "AWAY_FROM_TARGET":
        copy = "You’re still far from your next stop. Guardian is watching the route and venue."
    elif presence_state == "TARGET_UNBOUND":
        copy = "The next stop has no exact place yet; Guardian is using the plan schedule."
    else:
        copy = "Location sharing is off; Guardian is using the plan schedule only."

    if route_evidence.get("kind") == "LAUNCH_EDGE":
        origin_name = str((launch_origin or {}).get("name") or "your starting point")
        if route_evidence.get("edge_match") and route_evidence.get("minutes") is not None:
            copy = f"Using {origin_name}, the next stop is about {route_evidence['minutes']} minutes away."
        elif str((launch_origin or {}).get("state")) == "UNKNOWN_ORIGIN":
            copy = "Your first route is not ready yet; Guardian cannot safely estimate when to leave."
        else:
            copy = "Your starting point is known, but the route to the next stop is not ready yet."

    def activity_payload(row: Activity | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": str(row.id),
            "title": getattr(row, "title", None),
            "status": getattr(row, "status", None),
            "place_id": getattr(row, "place_id", None),
            "start_at": getattr(row, "start_at", None),
            "end_at": getattr(row, "end_at", None),
        }

    return {
        "current_activity": activity_payload(current),
        "next_activity": activity_payload(next_activity),
        "presence": presence,
        "venue_readiness": venue_readiness,
        "route_evidence": route_evidence,
        "launch_origin": launch_origin,
        "launch_edge": launch_edge,
        "journey_phase": phase_value.get("journey_phase"),
        "phase_authority": phase_value.get("phase_authority"),
        "copy": copy,
        "authority": presence.get("authority") or "NONE",
    }

def adaptive_watch_policy(
    session: Session,
    journey: Journey,
    enrollment: AutopilotEnrollment,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return current server-owned watch urgency and effective cadence.

    interval_seconds remains the maximum requested interval. The adaptive
    policy may shorten it as a meaningful activity approaches or while a
    material incident remains unresolved.
    """

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    configured = max(0.05, float(enrollment.interval_seconds))
    # Keep the policy callable for older lightweight callers that provide only
    # the cadence collaborators (for example a stub session in an integration
    # boundary).  A phase cannot be inferred without the persisted activity
    # rows, so degrade its metadata explicitly without changing cadence.
    if callable(getattr(session, "scalars", None)):
        phase = journey_phase_projection(session, journey, now=stamp)
    else:
        phase = {
            "journey_phase": "UNKNOWN",
            "phase_reason_code": "ACTIVITY_ROWS_UNAVAILABLE",
            "phase_authority": "CONTROL_PLANE_ONLY",
            "current_activity_id": None,
            "monitored_activity_ids": [],
        }
    next_activity, horizon = _next_activity(session, journey, now=stamp)
    incidents = _open_material_events(session, journey.id)
    next_move = next_move_projection(session, journey, phase=phase, now=stamp)
    presence = next_move.get("presence") or {}

    if incidents:
        state = "INCIDENT_ACTIVE"
        policy_interval = 60.0
        reason = "UNRESOLVED_MATERIAL_INCIDENT"
    elif horizon is not None and horizon <= 2 * 3600:
        state = "APPROACHING"
        policy_interval = 5 * 60.0
        reason = "NEXT_ACTIVITY_WITHIN_2H"
    elif horizon is not None and horizon <= 8 * 3600:
        state = "TODAY"
        policy_interval = 15 * 60.0
        reason = "NEXT_ACTIVITY_WITHIN_8H"
    elif horizon is not None and horizon <= 24 * 3600:
        state = "DAY_AHEAD"
        policy_interval = 30 * 60.0
        reason = "NEXT_ACTIVITY_WITHIN_24H"
    else:
        state = "QUIET"
        policy_interval = configured
        reason = "NO_NEAR_TERM_ACTIVITY_OR_MATERIAL_INCIDENT"

    # Presence only tightens a due cadence. It never creates an incident or
    # grants execution authority. A fresh foreground location near/far from a
    # near-term stop is still advisory evidence.
    presence_state = str(presence.get("state") or "")
    if not incidents and horizon is not None and horizon <= 2 * 3600:
        if presence_state == "AWAY_FROM_TARGET":
            policy_interval = min(policy_interval, 60.0)
            reason = "FRESH_FOREGROUND_LOCATION_AWAY_FROM_NEAR_TERM_STOP"
        elif presence_state in {"NEAR_TARGET", "VERIFIED_AT_TARGET"}:
            policy_interval = min(policy_interval, 300.0)
            reason = "FRESH_FOREGROUND_LOCATION_NEAR_NEXT_STOP"

    effective = min(configured, policy_interval)
    previous = _as_aware(enrollment.last_pulse_at)
    next_due = stamp if previous is None else previous + timedelta(seconds=effective)

    event_newer_than_last_pulse = False
    if incidents:
        detected = [_as_aware(row.detected_at) for row in incidents]
        detected = [item for item in detected if item is not None]
        newest = max(detected, default=None)
        if newest is not None and (previous is None or newest > previous):
            event_newer_than_last_pulse = True
            next_due = stamp

    return {
        "watch_state": state,
        "reason_code": reason,
        "configured_interval_seconds": configured,
        "effective_interval_seconds": effective,
        "next_due_at": next_due.isoformat(),
        "next_activity_id": next_activity.id if next_activity is not None else None,
        "next_activity_start_at": next_activity.start_at if next_activity is not None else None,
        "next_activity_horizon_seconds": horizon,
        "open_material_event_ids": [row.id for row in incidents],
        "event_newer_than_last_pulse": event_newer_than_last_pulse,
        "presence": presence,
        "next_move": next_move,
        **phase,
    }


def watch_is_due(
    session: Session,
    journey: Journey,
    enrollment: AutopilotEnrollment,
    *,
    now: datetime | None = None,
) -> bool:
    if not enrollment.enabled:
        return False
    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    policy = adaptive_watch_policy(session, journey, enrollment, now=stamp)
    due = _as_aware(str(policy.get("next_due_at") or ""))
    return bool(due is None or due <= stamp)
