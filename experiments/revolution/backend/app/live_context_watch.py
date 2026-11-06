"""Server-owned live venue context watch for active Journey activities."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import Activity, Event, Journey, Place, WebObservation
from app.fact_acquisition import acquire_place_facts
from app.replanning.proposal_lifecycle import invalidate_pending_replans_for_event

WATCH_STATUSES = {
    "OPEN_COMPATIBLE",
    "CLOSED_CONFLICT",
    "OUTSIDE_VERIFIED_HOURS",
    "UNKNOWN",
    "PROVIDER_DEGRADED",
}
_TERMINAL = {"COMPLETED", "SKIPPED", "CANCELLED"}
_DAY_NAMES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_VALID_OPENING = {"NORMALIZED_VALID", "OFFICIAL_WEB_VERIFIED", "VERIFIED_PROVIDER"}


def _minutes(value: Any) -> int | None:
    text = str(value or "").strip()
    if text == "24:00":
        return 24 * 60
    try:
        hour, minute = text.split(":", 1)
        result = int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 23 * 60 + 59 else None


def _intervals(opening: dict[str, Any]) -> list[dict[str, Any]]:
    raw = opening.get("intervals")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _activity_interval(activity: Any, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.fromisoformat(str(activity.start_at))
    end = datetime.fromisoformat(str(activity.end_at))
    start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
    end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
    return start, end


def observation_is_fresh(captured_at: str, previous_captured_at: str | None) -> bool:
    """Require a newly captured observation to be newer than stored evidence."""

    if not previous_captured_at:
        return True
    try:
        candidate = datetime.fromisoformat(captured_at)
        previous = datetime.fromisoformat(previous_captured_at)
    except (TypeError, ValueError):
        return False
    if candidate.tzinfo is None or previous.tzinfo is None:
        return False
    return candidate.astimezone(timezone.utc) > previous.astimezone(timezone.utc)


def _days_match(days: Any, weekday: str) -> bool:
    if not isinstance(days, list) or not days:
        return True
    return "*" in days or weekday in {str(item) for item in days}


def classify_opening_hours(*, activity: Any, place: Any, opening: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    """Compare a qualified opening-hours fact with one exact activity interval."""

    status = str(opening.get("status") or "UNKNOWN").upper()
    start, end = _activity_interval(activity, timezone_name)
    planned_start = start.isoformat()
    planned_end = end.isoformat()
    base = {
        "activity_id": str(activity.id),
        "place_id": str(place.id),
        "provider_entity": str(getattr(place, "external_id", "") or ""),
        "planned_local_interval": {"start": planned_start, "end": planned_end},
        "opening_hours": opening,
    }
    if status == "CLOSED":
        return {**base, "status": "CLOSED_CONFLICT", "reason_code": "EXPLICIT_CLOSED_STATE"}
    if status not in _VALID_OPENING:
        return {**base, "status": "UNKNOWN", "reason_code": "OPENING_HOURS_UNQUALIFIED"}

    matching: list[dict[str, Any]] = []
    # An overnight opening interval belongs to the day on which it opens.
    # Therefore an activity shortly after midnight must consider both its own
    # calendar day and the previous day (e.g. Fri 00:30 inside Thu 22:00-02:00).
    candidate_open_days = (start - timedelta(days=1), start)
    for item in _intervals(opening):
        open_min = _minutes(item.get("open"))
        close_min = _minutes(item.get("close"))
        if open_min is None or close_min is None:
            continue
        for opening_day in candidate_open_days:
            weekday = _DAY_NAMES[opening_day.weekday()]
            if not _days_match(item.get("days"), weekday):
                continue
            day_floor = opening_day.replace(hour=0, minute=0, second=0, microsecond=0)
            open_at = day_floor + timedelta(minutes=open_min)
            normalized_close_min = close_min
            if normalized_close_min <= open_min and normalized_close_min != 24 * 60:
                normalized_close_min += 24 * 60
            close_at = day_floor + timedelta(minutes=normalized_close_min)
            evidence = {
                "open": item.get("open"),
                "close": item.get("close"),
                "days": item.get("days"),
                "opening_day": opening_day.date().isoformat(),
            }
            if evidence not in matching:
                matching.append(evidence)
            if start >= open_at and end <= close_at:
                return {
                    **base,
                    "status": "OPEN_COMPATIBLE",
                    "reason_code": "PLANNED_INTERVAL_WITHIN_VERIFIED_HOURS",
                    "verified_opening_interval": evidence,
                }
    return {
        **base,
        "status": "OUTSIDE_VERIFIED_HOURS",
        "reason_code": "PLANNED_INTERVAL_OUTSIDE_VERIFIED_HOURS",
        "verified_opening_intervals": matching,
    }


def _place_receipt(
    receipt: dict[str, Any],
    place_id: str,
    provider_entity: str | None = None,
) -> dict[str, Any] | None:
    """Return only evidence bound to the exact planned Place/provider entity."""

    for item in receipt.get("places") or []:
        if not isinstance(item, dict) or str(item.get("place_id")) != str(place_id):
            continue
        if provider_entity is not None and str(item.get("provider_entity") or "") != str(
            provider_entity
        ):
            continue
        return item
    return None


def _persist_observation(
    session: Session,
    journey: Journey,
    place: Place,
    row: dict[str, Any],
    captured_at: str,
) -> WebObservation:
    binding = row.get("source_binding") or {}
    opening = row.get("opening_hours") or {"status": "UNKNOWN"}
    source = str(binding.get("source_url") or row.get("source") or "")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "journey_id": journey.id,
                "place_id": place.id,
                "provider_entity": place.external_id,
                "captured_at": captured_at,
                "opening_hours": opening,
                "source": source,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    existing = session.scalar(
        select(WebObservation).where(
            WebObservation.journey_id == journey.id,
            WebObservation.evidence_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return existing
    parsed = urlparse(source)
    observation = WebObservation(
        id=str(uuid4()),
        journey_id=journey.id,
        query=f"opening hours for exact provider entity {place.external_id}",
        url=source or "https://overpass-api.de/api/interpreter",
        domain=parsed.hostname or "overpass-api.de",
        title=str(getattr(place, "name", "") or place.id),
        source_class=str(binding.get("source_class") or "PROVIDER_TAGS"),
        provider_key="truth-mesh",
        model_key="server",
        captured_at=captured_at,
        evidence_fingerprint=fingerprint,
        content_excerpt=json.dumps(
            {
                "provider_entity": place.external_id,
                "identity_evidence": row.get("identity_evidence"),
                "opening_hours": opening,
                "cost_evidence": row.get("cost_evidence"),
                "source_binding": binding,
            },
            ensure_ascii=False,
            sort_keys=True,
        )[:4000],
        authority_status="NON_AUTHORITATIVE",
        created_at=captured_at,
    )
    session.add(observation)
    session.flush()
    return observation


def _context_incident_fingerprint(
    *, journey: Journey, activity: Activity, place: Place, result: dict[str, Any]
) -> str:
    """Stable identity for one plan-vs-venue conflict across repeated fresh polls."""

    material = {
        "journey_id": journey.id,
        "activity_id": activity.id,
        "place_id": place.id,
        "provider_entity": place.external_id,
        "planned_local_interval": result.get("planned_local_interval"),
        "status": result.get("status"),
        "reason_code": result.get("reason_code"),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _open_live_context_events(
    session: Session, *, journey_id: str, activity_id: str
) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(
                Event.journey_id == str(journey_id),
                Event.provider_key == "truth-mesh",
                Event.provenance == "SYSTEM_DERIVED",
                Event.event_type == "CLOSURE",
                Event.affected_activity_id == str(activity_id),
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
            .order_by(Event.detected_at, Event.id)
        ).all()
    )


def _resolve_live_context_event(
    session: Session,
    event: Event,
    *,
    resolved_at: str,
    reason: str,
    observation_id: str,
) -> None:
    try:
        payload = json.loads(event.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload["context_resolution"] = {
        "reason": reason,
        "resolved_at": resolved_at,
        "observation_id": observation_id,
    }
    event.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    event.status = "RESOLVED"
    event.resolved_at = resolved_at
    invalidate_pending_replans_for_event(
        session,
        event_id=event.id,
        journey_id=event.journey_id,
        decided_at=resolved_at,
        reason="trigger_event_resolved",
    )
    record_audit(
        session,
        action="live_context_watch.conflict.resolved",
        entity_type="Event",
        entity_id=event.id,
        journey_id=event.journey_id,
        after=payload["context_resolution"],
    )


def run_live_context_watch(
    session: Session,
    journey: Journey,
    *,
    activity_ids: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Refresh exact active-plan venues and classify reality without starting an Agent."""

    captured_at = datetime.now(timezone.utc).isoformat()
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
        if journey.active_plan_version_id
        else []
    )
    allowed_activity_ids = (
        {str(item) for item in activity_ids} if activity_ids is not None else None
    )
    active = [
        item
        for item in activities
        if item.status not in _TERMINAL
        and item.place_id
        and (allowed_activity_ids is None or str(item.id) in allowed_activity_ids)
    ]
    place_ids = list(dict.fromkeys(str(item.place_id) for item in active))
    previous_captures: dict[str, str | None] = {}
    for place_id in place_ids:
        place = session.get(Place, place_id)
        metadata = json.loads(place.metadata_json or "{}") if place is not None else {}
        fact_acquisition = metadata.get("fact_acquisition") if isinstance(metadata, dict) else {}
        previous_captures[place_id] = (
            str(fact_acquisition.get("retrieved_at"))
            if isinstance(fact_acquisition, dict) and fact_acquisition.get("retrieved_at")
            else None
        )
    receipt = acquire_place_facts(session, journey, place_ids=place_ids)
    observations: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unknown_or_degraded: list[dict[str, Any]] = []
    observation_ids: list[str] = []
    event_ids: list[str] = []
    created_event_ids: list[str] = []
    resolved_event_ids: list[str] = []
    degraded = bool(receipt.get("degraded_providers"))
    for activity in active:
        place = session.get(Place, str(activity.place_id))
        row = (
            _place_receipt(
                receipt,
                str(activity.place_id),
                str(place.external_id or ""),
            )
            if place is not None
            else None
        )
        if place is None or row is None:
            result = {
                "activity_id": activity.id,
                "place_id": activity.place_id,
                "status": "UNKNOWN",
                "reason_code": "EXACT_PLACE_EVIDENCE_MISSING",
            }
            classifications.append(result)
            unknown_or_degraded.append(result)
            continue
        observation = _persist_observation(session, journey, place, row, captured_at)
        observation_ids.append(observation.id)
        observation_payload = {
            "id": observation.id,
            "place_id": place.id,
            "provider_entity": place.external_id,
            "provider_key": observation.provider_key,
            "source_class": observation.source_class,
            "authority_status": observation.authority_status,
            "captured_at": observation.captured_at,
        }
        observations.append(observation_payload)
        if not observation_is_fresh(captured_at, previous_captures.get(str(place.id))):
            result = {
                "activity_id": activity.id,
                "place_id": place.id,
                "status": "UNKNOWN",
                "reason_code": "STALE_OBSERVATION",
                "observation_id": observation.id,
            }
        elif degraded:
            result = {
                "activity_id": activity.id,
                "place_id": place.id,
                "status": "PROVIDER_DEGRADED",
                "reason_code": "FACT_PROVIDER_DEGRADED",
                "observation_id": observation.id,
            }
        else:
            opening = row.get("opening_hours") or {"status": "UNKNOWN"}
            result = classify_opening_hours(
                activity=activity,
                place=place,
                opening=opening,
                timezone_name=journey.timezone,
            )
            result["observation_id"] = observation.id
        classifications.append(result)
        if result["status"] in {"CLOSED_CONFLICT", "OUTSIDE_VERIFIED_HOURS"}:
            incident_fingerprint = _context_incident_fingerprint(
                journey=journey,
                activity=activity,
                place=place,
                result=result,
            )
            open_events = _open_live_context_events(
                session,
                journey_id=journey.id,
                activity_id=activity.id,
            )
            event = None
            for candidate in open_events:
                try:
                    candidate_payload = json.loads(candidate.payload_json or "{}")
                except json.JSONDecodeError:
                    candidate_payload = {}
                if candidate_payload.get("incident_fingerprint") == incident_fingerprint:
                    event = candidate
                    continue
                _resolve_live_context_event(
                    session,
                    candidate,
                    resolved_at=captured_at,
                    reason="SUPERSEDED_BY_FRESH_CONTEXT",
                    observation_id=observation.id,
                )
                resolved_event_ids.append(candidate.id)

            payload = {
                "context_watch_status": result["status"],
                "reason_code": result["reason_code"],
                "incident_fingerprint": incident_fingerprint,
                "affected_activity_id": activity.id,
                "affected_place_id": place.id,
                "provider_entity": place.external_id,
                "observation_ids": [observation.id],
                "source_class": observation.source_class,
                "provider_key": observation.provider_key,
                "captured_at": captured_at,
                "planned_local_interval": result["planned_local_interval"],
                "verified_opening": result.get("opening_hours") or result.get("verified_opening_intervals"),
            }
            if event is None:
                event = Event(
                    id=str(uuid4()),
                    journey_id=journey.id,
                    provider_key="truth-mesh",
                    provenance="SYSTEM_DERIVED",
                    event_type="CLOSURE",
                    severity="MAJOR",
                    status="DETECTED",
                    affected_activity_id=activity.id,
                    occurred_at=result["planned_local_interval"]["start"],
                    detected_at=captured_at,
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at=captured_at,
                )
                session.add(event)
                session.flush()
                created_event_ids.append(event.id)
                audit_action = "live_context_watch.conflict.detected"
            else:
                event.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                audit_action = "live_context_watch.conflict.refreshed"

            record_audit(
                session,
                action=audit_action,
                entity_type="Event",
                entity_id=event.id,
                journey_id=journey.id,
                after=payload,
            )
            result["event_id"] = event.id
            result["incident_fingerprint"] = incident_fingerprint
            conflicts.append(result)
            event_ids.append(event.id)
        elif result["status"] == "OPEN_COMPATIBLE":
            for event in _open_live_context_events(
                session,
                journey_id=journey.id,
                activity_id=activity.id,
            ):
                _resolve_live_context_event(
                    session,
                    event,
                    resolved_at=captured_at,
                    reason="LIVE_CONTEXT_CLEARED",
                    observation_id=observation.id,
                )
                resolved_event_ids.append(event.id)
        elif result["status"] in {"UNKNOWN", "PROVIDER_DEGRADED"}:
            unknown_or_degraded.append(result)
    if conflicts:
        outcome = "MATERIAL_CONFLICT"
    elif unknown_or_degraded:
        outcome = "UNKNOWN" if not degraded else "PROVIDER_DEGRADED"
    else:
        outcome = "NO_MATERIAL_CHANGE"
    session.flush()
    return {
        "status": outcome,
        "journey_id": journey.id,
        "captured_at": captured_at,
        "checked_activities": [{"activity_id": item.id, "place_id": item.place_id} for item in active],
        "observations": observations,
        "classifications": classifications,
        "material_conflicts": conflicts,
        "unknown_or_degraded": unknown_or_degraded,
        "recovery_event_ids": list(dict.fromkeys(event_ids)),
        "created_recovery_event_ids": list(dict.fromkeys(created_event_ids)),
        "resolved_recovery_event_ids": list(dict.fromkeys(resolved_event_ids)),
        "agent_started": False,
        "mutation_count": 0,
        "acquisition": receipt,
    }
