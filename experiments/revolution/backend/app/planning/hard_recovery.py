"""Durable mid-trip HARD capability recovery.

A newly confirmed HARD capability on an ACTIVE journey invalidates the active
plan.  The consequence must be *durable state*, not a JSON field buried in one
response:

    new HARD capability invalidates active plan
      -> durable Event + recovery state on the Journey
      -> C4 commitments preserved
      -> governed ReplanProposal, or an explicit no-alternative state
      -> visible frontend recovery action

Two invariants are load-bearing:

1. **Acknowledgement is not resolution.**  ``Event.status = ACKNOWLEDGED``
   records that the traveller saw the warning.  It never marks an infeasible
   plan safe, never hides the journey truth, and never lets stale evidence look
   current.  Only a revalidated feasible replacement resolves recovery.
2. **No-alternative is a persisted fact.**  ``RECOVERY_REQUIRED_NO_ALTERNATIVE``
   is written only after an actual replan attempt produced zero feasible
   candidates.  It is never inferred from a missing boolean.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Event, Journey, PlanVersion

# Durable recovery is a user-initiated change to the active journey.
# Event.event_type is a closed vocabulary (schema.sql), so the specific
# recovery kind is carried in the payload rather than inventing a new type.
EVENT_TYPE = "USER_CHANGE"
# Distinguishes recovery events from any other user-initiated change event.
PROVIDER_KEY = "citynexus-capability-recovery"
RECOVERY_KIND = "CAPABILITY_HARD_RECOVERY"
ACTIVE = "RECOVERY_REQUIRED"
NO_ALTERNATIVE = "RECOVERY_REQUIRED_NO_ALTERNATIVE"
RESOLVED = "RESOLVED"
ACKNOWLEDGED = "ACKNOWLEDGED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any, default: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return default
        return parsed if isinstance(parsed, dict) else default
    return default


def _open_events(session: Session, journey: Journey) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(
                Event.journey_id == journey.id,
                Event.event_type == EVENT_TYPE,
                Event.provider_key == PROVIDER_KEY,
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
            .order_by(Event.detected_at.desc())
        ).all()
    )


def active_recovery_event(session: Session, journey: Journey) -> Event | None:
    rows = _open_events(session, journey)
    return rows[0] if rows else None


def open_recovery_event(
    session: Session,
    journey: Journey,
    *,
    capability_code: str,
    capability_params: dict[str, Any] | None = None,
    failures: list[str] | None = None,
) -> Event:
    """Create (or refresh) the durable recovery Event for this Journey.

    Refreshing keeps the event open but clears any prior acknowledgement: the
    underlying situation has changed, so a previous acknowledgement no longer
    describes what the traveller has seen.
    """

    existing = active_recovery_event(session, journey)
    payload = {
        "capability_code": capability_code,
        "capability_params": dict(capability_params or {}),
        "active_plan_version_id": journey.active_plan_version_id,
        "feasibility_failures": list(failures or []),
        "requires_user_decision": True,
        "silent_plan_mutation": False,
        # Reset any previous replan outcome: this is a fresh invalidation.
        "replan_attempted": False,
        "no_alternative": False,
        "acknowledged_at": None,
    }
    now = _now()
    if existing is not None:
        existing.payload_json = _json(payload)
        existing.detected_at = now
        existing.status = "DETECTED"
        existing.resolved_at = None
        return existing
    event = Event(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=PROVIDER_KEY,
        provenance="USER_REPORTED",
        event_type=EVENT_TYPE,
        severity="MAJOR",
        status="DETECTED",
        affected_activity_id=None,
        occurred_at=now,
        detected_at=now,
        resolved_at=None,
        payload_json=_json(payload),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event


def acknowledge_recovery(session: Session, journey: Journey) -> Event | None:
    """Record that the traveller saw the warning.  Does NOT resolve it."""

    event = active_recovery_event(session, journey)
    if event is None:
        return None
    payload = _load(event.payload_json, {})
    payload["acknowledged_at"] = _now()
    event.payload_json = _json(payload)
    if event.status == "DETECTED":
        event.status = "ACKNOWLEDGED"
    return event


def record_replan_outcome(
    session: Session,
    journey: Journey,
    *,
    feasible_candidate_ids: list[str],
    failures: list[str] | None = None,
) -> Event | None:
    """Persist the outcome of an actual recovery/replan attempt.

    Zero feasibility-checked candidates is what makes the no-alternative state
    real.  A missing boolean never does.
    """

    event = active_recovery_event(session, journey)
    if event is None:
        return None
    payload = _load(event.payload_json, {})
    payload["replan_attempted"] = True
    payload["attempted_at"] = _now()
    payload["feasible_candidate_ids"] = list(feasible_candidate_ids)
    payload["no_alternative"] = not feasible_candidate_ids
    if failures:
        payload["replan_failures"] = list(failures)
    event.payload_json = _json(payload)
    return event


def resolve_recovery_events(session: Session, journey: Journey) -> int:
    """Mark every open recovery Event resolved (a feasible plan is active)."""

    rows = _open_events(session, journey)
    now = _now()
    for row in rows:
        row.status = "RESOLVED"
        row.resolved_at = now
        payload = _load(row.payload_json, {})
        payload["resolved_at"] = now
        row.payload_json = _json(payload)
    return len(rows)


def recovery_state(
    session: Session,
    journey: Journey,
    *,
    is_feasible: bool | None = None,
    failures: list[str] | None = None,
) -> dict[str, Any] | None:
    """Project the durable recovery facts for the phone UI.

    The projected state is derived from persisted facts, never from a caller's
    ``is_feasible`` argument alone:

    * a recorded replan attempt with zero feasible candidates -> NO_ALTERNATIVE
    * an acknowledgement does not change the state, only the event status
    * an open Event remains RECOVERY_REQUIRED until a transactional workflow
      explicitly resolves that Event after revalidation

    ``is_feasible`` is retained only for failure-detail projection compatibility;
    it is not allowed to resolve durable recovery from a read path.
    """

    event = active_recovery_event(session, journey)
    if event is None:
        return None
    payload = _load(event.payload_json, {})
    if payload.get("replan_attempted") and payload.get("no_alternative"):
        state = NO_ALTERNATIVE
    else:
        state = ACTIVE
    # Only candidates recorded by an actual replan attempt are advertised as
    # alternatives.  Arbitrary CANDIDATE rows are not proof of feasibility.
    candidate_ids = [
        str(item)
        for item in (
            payload.get("feasible_candidate_ids")
            if payload.get("replan_attempted")
            and isinstance(payload.get("feasible_candidate_ids"), list)
            else []
        )
    ]
    return {
        "state": state,
        "kind": RECOVERY_KIND,
        "event_id": event.id,
        "event_status": event.status,
        "acknowledged": event.status == ACKNOWLEDGED,
        "acknowledged_at": payload.get("acknowledged_at"),
        "capability_code": payload.get("capability_code"),
        "active_plan_version_id": journey.active_plan_version_id,
        "failures": list(failures or payload.get("feasibility_failures") or []),
        "replan_attempted": bool(payload.get("replan_attempted")),
        "no_alternative": bool(payload.get("no_alternative")),
        "candidate_plan_count": len(candidate_ids),
        "candidate_plan_ids": candidate_ids,
        "requires_user_decision": True,
        "silent_plan_mutation": False,
        "actions": ["REPLAN", "ACKNOWLEDGE_WARNING"],
        "detected_at": event.detected_at,
    }


def recovery_flags(session: Session, journey: Journey) -> dict[str, Any]:
    """Compact booleans for `/experience` clients."""

    event = active_recovery_event(session, journey)
    if event is None:
        return {"needs_review": False, "recovery_required": False}
    payload = _load(event.payload_json, {})
    return {
        "needs_review": True,
        "recovery_required": True,
        "acknowledged": event.status == ACKNOWLEDGED,
        "recovery_event_id": event.id,
        "recovery_capability_code": payload.get("capability_code"),
        "recovery_state": (
            NO_ALTERNATIVE
            if payload.get("replan_attempted") and payload.get("no_alternative")
            else ACTIVE
        ),
    }
