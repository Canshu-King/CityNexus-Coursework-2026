"""Scoped temporary Journey-intent semantics for live Operator steering."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class IntentScope:
    kind: str
    starts_at: str
    ends_at: str | None
    local_date: str | None


def resolve_intent_scope(
    *,
    scope_kind: str,
    now: datetime,
    timezone_name: str,
    journey_end_at: str | None = None,
) -> IntentScope:
    """Resolve model/user temporal meaning into a server-owned bounded scope."""
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    kind = str(scope_kind or "").upper()

    if kind == "REST_OF_TODAY":
        end_local = datetime.combine(
            local_now.date() + timedelta(days=1),
            time.min,
            tzinfo=zone,
        )
        return IntentScope(
            kind=kind,
            starts_at=now.astimezone(timezone.utc).isoformat(),
            ends_at=end_local.astimezone(timezone.utc).isoformat(),
            local_date=local_now.date().isoformat(),
        )

    if kind == "NEXT_MORNING":
        next_day = local_now.date() + timedelta(days=1)
        start_local = datetime.combine(next_day, time(hour=5), tzinfo=zone)
        end_local = datetime.combine(next_day, time(hour=12), tzinfo=zone)
        return IntentScope(
            kind=kind,
            starts_at=start_local.astimezone(timezone.utc).isoformat(),
            ends_at=end_local.astimezone(timezone.utc).isoformat(),
            local_date=next_day.isoformat(),
        )

    if kind == "NEXT_STOP":
        # This is a semantic scope, not a guessed clock duration. The server
        # should end it when the next targeted activity becomes terminal.
        return IntentScope(
            kind=kind,
            starts_at=now.astimezone(timezone.utc).isoformat(),
            ends_at=None,
            local_date=local_now.date().isoformat(),
        )

    if kind == "REMAINING_JOURNEY":
        return IntentScope(
            kind=kind,
            starts_at=now.astimezone(timezone.utc).isoformat(),
            ends_at=journey_end_at,
            local_date=None,
        )

    raise ValueError("UNRESOLVED_INTENT_SCOPE")


def intent_is_active(
    *,
    status: str,
    starts_at: str,
    ends_at: str | None,
    now: datetime,
) -> bool:
    if str(status).upper() != "ACTIVE":
        return False
    start = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
    if start.tzinfo is None:
        return False
    stamp = now.astimezone(timezone.utc)
    if stamp < start.astimezone(timezone.utc):
        return False
    if ends_at:
        end = datetime.fromisoformat(str(ends_at).replace("Z", "+00:00"))
        if end.tzinfo is None or stamp >= end.astimezone(timezone.utc):
            return False
    return True


def canonical_intent_dimension(
    *,
    intent_kind: str,
    constraints: dict[str, Any] | None = None,
) -> str:
    """Dimension used for supersession, never as a global preference key."""
    kind = str(intent_kind or "GENERAL_GOAL").upper()
    constraint_keys = sorted(str(key).lower() for key in (constraints or {}).keys())
    if kind == "PACE":
        return "journey-intent:pace"
    if kind == "WALKING_LIMIT":
        return "journey-intent:walking"
    if kind == "FOOD_PREFERENCE":
        return "journey-intent:food"
    if kind in {"INDOOR_OUTDOOR", "INTEREST"}:
        return "journey-intent:interest"
    if kind == "TIME_CONSTRAINT":
        return "journey-intent:time"
    if kind == "BUDGET_TIGHTENING":
        return "journey-intent:budget"
    if kind == "REST_BREAK":
        return "journey-intent:rest"
    if kind == "ACCESSIBILITY_NEED":
        return "capability"
    return "journey-intent:" + ("+".join(constraint_keys) if constraint_keys else "general")


def resolve_intent_targets(
    *,
    scope_kind: str,
    activities: list[dict[str, Any]],
    now: datetime,
    timezone_name: str,
    current_activity_id: str | None,
) -> list[str]:
    """Resolve semantic horizon to server-owned non-executing activities."""
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    ordered = sorted(activities, key=lambda item: int(item.get("sequence_no") or 0))
    eligible = [
        item for item in ordered
        if str(item.get("status") or "").upper() not in {"COMPLETED", "SKIPPED", "CANCELLED"}
        and str(item.get("id") or "") != str(current_activity_id or "")
        and str(item.get("status") or "").upper() not in {"EN_ROUTE", "ARRIVED", "IN_PROGRESS"}
    ]
    kind = str(scope_kind or "").upper()
    if kind == "NEXT_STOP":
        return [str(eligible[0]["id"])] if eligible else []
    if kind == "REST_OF_TODAY":
        return [
            str(item["id"]) for item in eligible
            if _local_activity_date(item.get("start_at"), zone) == local_now.date()
        ]
    if kind == "NEXT_MORNING":
        next_day = local_now.date() + timedelta(days=1)
        return [
            str(item["id"]) for item in eligible
            if _local_activity_date(item.get("start_at"), zone) == next_day
            and _local_activity_hour(item.get("start_at"), zone) >= 5
            and _local_activity_hour(item.get("start_at"), zone) < 12
        ]
    if kind == "REMAINING_JOURNEY":
        return [str(item["id"]) for item in eligible]
    return []


def _local_activity_date(value: Any, zone: ZoneInfo) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(zone).date()


def _local_activity_hour(value: Any, zone: ZoneInfo) -> int:
    if not value:
        return -1
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return -1
    if parsed.tzinfo is None:
        return -1
    return parsed.astimezone(zone).hour


def intent_scopes_overlap(
    *,
    left_kind: str,
    left_start: str | None,
    left_end: str | None,
    left_targets: list[str],
    right_kind: str,
    right_start: str | None,
    right_end: str | None,
    right_targets: list[str],
) -> bool:
    if left_kind in {"NEXT_STOP", "UNTIL_RENDEZVOUS"} or right_kind in {"NEXT_STOP", "UNTIL_RENDEZVOUS"}:
        return bool(set(left_targets).intersection(right_targets))

    def parse(value: str | None, *, fallback: datetime) -> datetime:
        if not value:
            return fallback
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)

    floor = datetime.min.replace(tzinfo=timezone.utc)
    ceiling = datetime.max.replace(tzinfo=timezone.utc)
    l_start, l_end = parse(left_start, fallback=floor), parse(left_end, fallback=ceiling)
    r_start, r_end = parse(right_start, fallback=floor), parse(right_end, fallback=ceiling)
    return l_start < r_end and r_start < l_end


def journey_intent_payload(intent: Any) -> dict[str, Any]:
    import json

    def load(name: str, fallback: Any) -> Any:
        try:
            value = json.loads(getattr(intent, name, "") or "")
            return value
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    envelope = load("semantic_envelope_json", {})
    planned_separation = (
        envelope.get("r63_planned_separation")
        if isinstance(envelope, dict) and isinstance(envelope.get("r63_planned_separation"), dict)
        else None
    )

    return {
        "id": str(intent.id),
        "journey_id": str(intent.journey_id),
        "created_by_traveler_id": str(intent.created_by_traveler_id),
        "created_by_member_id": intent.created_by_member_id,
        "source_text": intent.source_text,
        "intent_kind": intent.intent_kind,
        "scope_kind": intent.scope_kind,
        "scope_start_at": intent.scope_start_at,
        "scope_end_at": intent.scope_end_at,
        "local_date": intent.local_date,
        "target_activity_ids": load("target_activity_ids_json", []),
        "canonical_dimensions": load("canonical_dimensions_json", []),
        "constraints": load("constraints_json", []),
        "capabilities": load("capabilities_json", []),
        "preference_hints": load("preference_hints_json", {}),
        "planned_separation": planned_separation,
        "source": intent.source,
        "compiler": {
            "provider_key": intent.compiler_provider_key,
            "model_key": intent.compiler_model_key,
            "confidence": intent.compiler_confidence,
        },
        "status": intent.status,
        "evaluation_state": intent.evaluation_state,
        "created_at": intent.created_at,
        "superseded_at": intent.superseded_at,
        "expired_at": intent.expired_at,
        "cancelled_at": intent.cancelled_at,
    }


def active_journey_intents(
    session: Any,
    journey: Any,
    *,
    now: datetime,
    traveler_id: str | None = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import Activity, JourneyIntent

    rows = session.scalars(
        select(JourneyIntent)
        .where(JourneyIntent.journey_id == journey.id, JourneyIntent.status == "ACTIVE")
        .order_by(JourneyIntent.created_at, JourneyIntent.id)
    ).all()
    active: list[dict[str, Any]] = []
    for row in rows:
        if traveler_id is not None and str(row.created_by_traveler_id) != str(traveler_id):
            continue
        if not intent_is_active(
            status=row.status,
            starts_at=row.scope_start_at or "",
            ends_at=row.scope_end_at,
            now=now,
        ):
            continue
        targets = _safe_json_list(row.target_activity_ids_json)
        if row.scope_kind in {"NEXT_STOP", "UNTIL_RENDEZVOUS"} and targets:
            target = session.get(Activity, str(targets[0]))
            if target is None or str(target.status).upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}:
                continue
            if row.intent_kind == "PLANNED_SEPARATION":
                metadata = _safe_json_object(row.semantic_envelope_json).get("r63_planned_separation")
                if (
                    not isinstance(metadata, dict)
                    or str(metadata.get("active_plan_version_id") or "") != str(journey.active_plan_version_id or "")
                    or str(target.plan_version_id) != str(journey.active_plan_version_id or "")
                ):
                    continue
        active.append(journey_intent_payload(row))
    return active


def pending_journey_intent_clarifications(
    session: Any,
    journey: Any,
    *,
    traveler_id: str,
) -> list[dict[str, Any]]:
    """Return unresolved clarifications only to the traveller who created them."""
    from sqlalchemy import select

    from app.db.models import JourneyIntent

    rows = session.scalars(
        select(JourneyIntent)
        .where(
            JourneyIntent.journey_id == journey.id,
            JourneyIntent.created_by_traveler_id == str(traveler_id),
            JourneyIntent.status == "NEEDS_CLARIFICATION",
        )
        .order_by(JourneyIntent.created_at, JourneyIntent.id)
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = journey_intent_payload(row)
        envelope = _safe_json_object(row.semantic_envelope_json)
        item["clarification"] = (
            envelope.get("r52_clarification")
            if isinstance(envelope.get("r52_clarification"), dict)
            else {}
        )
        result.append(item)
    return result


def _safe_json_list(value: str | None) -> list[Any]:
    import json

    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_json_object(value: str | None) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def expire_journey_intents(session: Any, journey: Any, *, now: datetime) -> int:
    from sqlalchemy import select

    from app.db.models import JourneyIntent

    count = 0
    rows = session.scalars(
        select(JourneyIntent).where(
            JourneyIntent.journey_id == journey.id,
            JourneyIntent.status == "ACTIVE",
        )
    ).all()
    stamp = now.astimezone(timezone.utc)
    for row in rows:
        end = row.scope_end_at
        expired = False
        if end:
            try:
                parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
                expired = parsed.tzinfo is not None and stamp >= parsed.astimezone(timezone.utc)
            except ValueError:
                expired = True
        if row.scope_kind in {"NEXT_STOP", "UNTIL_RENDEZVOUS"} and row.target_activity_ids_json:
            target_ids = _safe_json_list(row.target_activity_ids_json)
            if target_ids:
                from app.db.models import Activity

                target = session.get(Activity, str(target_ids[0]))
                expired = expired or target is None or str(target.status).upper() in {
                    "COMPLETED", "SKIPPED", "CANCELLED"
                }
                if row.intent_kind == "PLANNED_SEPARATION":
                    metadata = _safe_json_object(row.semantic_envelope_json).get("r63_planned_separation")
                    expired = expired or (
                        not isinstance(metadata, dict)
                        or str(metadata.get("active_plan_version_id") or "") != str(journey.active_plan_version_id or "")
                        or (target is not None and str(target.plan_version_id) != str(journey.active_plan_version_id or ""))
                    )
        if str(journey.status).upper() == "COMPLETED":
            expired = True
        if expired:
            row.status = "EXPIRED"
            row.expired_at = stamp.isoformat()
            row.updated_at = stamp.isoformat()
            count += 1
    if count:
        session.flush()
    return count


def should_require_clarification(
    *,
    confidence: float,
    scope_resolved: bool,
    constraint_count: int,
    intent_kind: str | None,
) -> bool:
    """Fail closed on vague steering rather than launching an unsafe replan."""
    if confidence < 0.55:
        return True
    if not scope_resolved:
        return True
    if not intent_kind:
        return True
    if str(intent_kind).upper() == "GENERAL_GOAL" and constraint_count == 0:
        return True
    return False
