"""Continuous Journey Day Operator projection.

This module is deliberately read-only. It turns existing execution truth,
presence, readiness and next-move facts into one traveller-facing day flow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db.models import Activity, Journey, JourneyLeg, PlanVersion
from app.execution.presence import presence_projection
from app.execution.readiness import departure_readiness_projection


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


def continuous_journey_state(
    *,
    journey_status: str,
    activity_status: str | None,
    presence_state: str | None,
    readiness_state: str | None,
    must_leave_by: str | None,
    activity_end_at: str | None,
    next_activity_id: str | None,
    operator_needs_attention: bool = False,
    now: datetime,
) -> dict[str, Any]:
    """Project the next human-facing milestone without creating execution truth."""

    if str(journey_status).upper() == "COMPLETED":
        return {
            "state": "DAY_COMPLETE",
            "recommended_action": "NONE",
            "can_confirm": False,
        }

    if operator_needs_attention:
        return {
            "state": "NEEDS_ATTENTION",
            "recommended_action": "REVIEW_OPERATOR",
            "can_confirm": False,
        }

    status = str(activity_status or "").upper()
    presence = str(presence_state or "").upper()
    readiness = str(readiness_state or "").upper()

    if status == "EN_ROUTE":
        if presence in {"NEAR_TARGET", "VERIFIED_AT_TARGET"}:
            return {
                "state": "ARRIVAL_READY",
                "recommended_action": "CONFIRM_ARRIVAL",
                "can_confirm": True,
            }
        return {
            "state": "ON_THE_WAY",
            "recommended_action": "CONTINUE_ROUTE",
            "can_confirm": True,
        }

    if status == "ARRIVED":
        return {
            "state": "READY_TO_BEGIN",
            "recommended_action": "CONFIRM_BEGIN",
            "can_confirm": True,
        }

    if status == "IN_PROGRESS":
        end_at = _aware(activity_end_at)
        if end_at is not None and now.astimezone(timezone.utc) >= end_at:
            return {
                "state": "READY_TO_COMPLETE",
                "recommended_action": "CONFIRM_COMPLETE",
                "can_confirm": True,
            }
        return {
            "state": "IN_ACTIVITY",
            "recommended_action": "CONTINUE_ACTIVITY",
            "can_confirm": True,
        }

    if status == "COMPLETED":
        if next_activity_id:
            return {
                "state": "BETWEEN_STOPS",
                "recommended_action": "PREPARE_NEXT_STOP",
                "can_confirm": False,
            }
        return {
            "state": "DAY_COMPLETE",
            "recommended_action": "NONE",
            "can_confirm": False,
        }

    leave_at = _aware(must_leave_by)
    if readiness == "BLOCKED":
        return {
            "state": "NEEDS_ATTENTION",
            "recommended_action": "CHECK_BLOCKER",
            "can_confirm": False,
        }
    if leave_at is not None and now.astimezone(timezone.utc) >= leave_at:
        return {
            "state": "LEAVE_NOW",
            "recommended_action": "CONFIRM_DEPARTURE",
            "can_confirm": True,
        }
    if readiness in {"READY", "READY_WITH_UNCERTAINTY", "ACTION_NEEDED", "NOT_DUE"}:
        return {
            "state": "PREPARING_TO_LEAVE",
            "recommended_action": "WAIT_OR_CHECK_ROUTE",
            "can_confirm": True,
        }

    return {
        "state": "NEXT_STOP_PREPARING",
        "recommended_action": "CHECK_NEXT_STEP",
        "can_confirm": False,
    }


def milestone_notification_key(
    *, journey_id: str, activity_id: str, state: str
) -> str:
    """Stable, human-milestone dedupe key."""
    return f"journey-flow:{journey_id}:{activity_id}:{str(state).upper()}"


def _activity_payload(row: Activity | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "title": row.title,
        "status": row.status,
        "place_id": row.place_id,
        "start_at": row.start_at,
        "end_at": row.end_at,
    }


def continuous_flow_projection(
    session: Any,
    journey: Journey,
    *,
    viewer_member_id: str | None = None,
    operator_needs_attention: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the one read-only day flow used by the Operator surface.

    This function deliberately never writes execution state or acquires a route.
    It prefers the exact frozen plan edge (and its JourneyLeg) and keeps a
    missing edge explicit so a caller can offer the existing route check action.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
    ) if journey.active_plan_version_id else []
    terminal = {"COMPLETED", "SKIPPED", "CANCELLED"}
    current = next((row for row in rows if str(row.id) == str(journey.current_activity_id)), None)
    if current is None and journey.status != "COMPLETED":
        current = next((row for row in rows if row.status not in terminal), None)
    next_row = None
    previous_completed = None
    if current is not None:
        next_row = next((row for row in rows if row.sequence_no > current.sequence_no and row.status not in terminal), None)
        previous_completed = next(
            (row for row in reversed(rows) if row.sequence_no < current.sequence_no and row.status == "COMPLETED"),
            None,
        )

    readiness = departure_readiness_projection(session, journey, now=stamp)
    presence = presence_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
        target_activity_id=str(current.id) if current is not None else None,
        now=stamp,
    )

    # For an automatically advanced cursor, the actionable edge is the exact
    # previous completed Place -> current Place transition.  Never substitute a
    # hotel/launch origin for that edge.
    edge_origin = previous_completed if current is not None and current.status in {"ACTIVE", "NOT_STARTED"} else current
    edge_destination = current if current is not None and edge_origin is not current else next_row
    route: dict[str, Any] = {
        "state": "UNKNOWN",
        "exact": False,
        "reason_code": "NO_TRAVEL_EDGE",
        "origin_activity_id": str(edge_origin.id) if edge_origin else None,
        "destination_activity_id": str(edge_destination.id) if edge_destination else None,
        "origin_place_id": edge_origin.place_id if edge_origin else None,
        "destination_place_id": edge_destination.place_id if edge_destination else None,
        "duration_min": None,
        "provider": None,
        "provenance": None,
        "captured_at": None,
    }
    if edge_origin is not None and edge_destination is not None and journey.active_plan_version_id:
        leg = session.scalar(
            select(JourneyLeg).where(
                JourneyLeg.plan_version_id == journey.active_plan_version_id,
                JourneyLeg.from_activity_id == edge_origin.id,
                JourneyLeg.to_activity_id == edge_destination.id,
            ).order_by(JourneyLeg.sequence_no)
        )
        if leg is not None:
            route.update({
                "state": "AVAILABLE",
                "exact": True,
                "reason_code": "EXACT_FROZEN_PLAN_EDGE",
                "duration_min": int(leg.duration_min),
                "provider": leg.provider_key,
                "provenance": leg.provenance,
                "captured_at": leg.captured_at,
            })
        else:
            plan = session.get(PlanVersion, journey.active_plan_version_id)
            try:
                evidence = __import__("json").loads(plan.mobility_evidence_json or "{}") if plan else {}
            except (TypeError, ValueError):
                evidence = {}
            facts = evidence.get("facts") if isinstance(evidence, dict) else []
            for fact in facts or []:
                if str(fact.get("origin_key")) == str(edge_origin.place_id) and str(fact.get("destination_key")) == str(edge_destination.place_id):
                    route.update({
                        "state": "AVAILABLE",
                        "exact": True,
                        "reason_code": "EXACT_FROZEN_PLAN_EDGE",
                        "duration_min": fact.get("minutes"),
                        "provider": fact.get("provider"),
                        "provenance": fact.get("route_provenance") or fact.get("fact_quality"),
                        "captured_at": fact.get("captured_at"),
                    })
                    break

    must_leave_by = readiness.get("must_leave_by")
    if route["exact"] and edge_destination is not None:
        start = _aware(edge_destination.start_at)
        if start is not None and route.get("duration_min") is not None:
            from datetime import timedelta
            must_leave_by = (start - timedelta(minutes=int(route["duration_min"]) + 10)).isoformat()
    route["must_leave_by"] = must_leave_by
    readiness = {**readiness, "must_leave_by": must_leave_by, "route": route}

    state = continuous_journey_state(
        journey_status=journey.status,
        activity_status=current.status if current else None,
        presence_state=presence.get("state"),
        readiness_state=readiness.get("state"),
        must_leave_by=must_leave_by,
        activity_end_at=current.end_at if current else None,
        next_activity_id=str(next_row.id) if next_row else None,
        operator_needs_attention=operator_needs_attention,
        now=stamp,
    )
    current_id = str(current.id) if current else str(journey.id)
    return {
        **state,
        "current": _activity_payload(current),
        "next": _activity_payload(next_row),
        "previous_completed": _activity_payload(previous_completed),
        "presence": presence,
        "readiness": readiness,
        "route": route,
        "notification_key": milestone_notification_key(
            journey_id=str(journey.id), activity_id=current_id, state=state["state"]
        ),
        "captured_at": stamp.isoformat(),
        "read_only": True,
    }
