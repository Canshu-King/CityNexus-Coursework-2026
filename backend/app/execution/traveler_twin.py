from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities import canonicalize_capabilities, capability_constraints, summarize_capabilities
from app.db.models import (
    Activity,
    Journey,
    JourneyMission,
    JourneyOutcomeEvent,
    Place,
    Preference,
    TravelerPreferenceEvidence,
    TravelerCapability,
    TravelerTwinSnapshot,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _place_tags(session: Session, activity: Activity | None) -> set[str]:
    if activity is None or not activity.place_id:
        return set()
    place = session.get(Place, activity.place_id)
    return {str(item).strip().lower() for item in _load(place.tags_json if place else "[]", [])}


def _explicit_items(session: Session, traveler_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(Preference)
        .where(Preference.traveler_id == str(traveler_id))
        .order_by(Preference.category, Preference.preference_key)
    ).all()
    return [
        {
            "id": row.id,
            "category": row.category,
            "dimension": row.preference_key,
            "direction": row.preference_key,
            "value": _load(row.value_json, None),
            "weight": float(row.weight),
            "source": "EXPLICIT",
            "explanation": "Explicit traveler preference.",
        }
        for row in rows
    ]


def _event_signal(session: Session, event: JourneyOutcomeEvent) -> tuple[str, str] | None:
    activity = session.get(Activity, event.activity_id) if event.activity_id else None
    tags = _place_tags(session, activity)
    payload = _load(event.payload_json, {})
    explicit = str(payload.get("preference_dimension") or "").strip().lower()
    if explicit in {"indoor", "outdoor", "low_walking", "cultural", "food"}:
        direction = str(payload.get("direction") or explicit).strip().lower()
        return explicit, direction
    if event.event_type in {"ACTIVITY_COMPLETED", "USER_ACCEPTED"}:
        if "indoor" in tags:
            return "indoor", "indoor"
        if "outdoor" in tags or "scenic" in tags or "waterfront" in tags:
            return "indoor", "outdoor"
    if event.event_type in {"ACTIVITY_SKIPPED", "USER_REJECTED", "MANUAL_OVERRIDE"}:
        if "outdoor" in tags or "scenic" in tags or "waterfront" in tags:
            return "indoor", "indoor"
        if "indoor" in tags:
            return "indoor", "outdoor"
    return None


def _active_evidence(session: Session, traveler_id: str) -> list[TravelerPreferenceEvidence]:
    now = datetime.now(timezone.utc)
    rows = list(
        session.scalars(
            select(TravelerPreferenceEvidence)
            .where(
                TravelerPreferenceEvidence.traveler_id == str(traveler_id),
                TravelerPreferenceEvidence.status == "ACTIVE",
            )
            .order_by(TravelerPreferenceEvidence.created_at, TravelerPreferenceEvidence.id)
        ).all()
    )
    active: list[TravelerPreferenceEvidence] = []
    for row in rows:
        if row.expires_at and datetime.fromisoformat(row.expires_at) <= now:
            row.status = "EXPIRED"
            continue
        active.append(row)
    return active


def active_capability_rows(
    session: Session, traveler_id: str, journey_id: str | None = None
) -> list[TravelerCapability]:
    """Return active explicit capability facts for a traveler and journey."""

    rows = list(
        session.scalars(
            select(TravelerCapability)
            .where(
                TravelerCapability.traveler_id == str(traveler_id),
                TravelerCapability.active.is_(True),
                (
                    (TravelerCapability.scope == "TRAVELER")
                    | (
                        (TravelerCapability.scope == "JOURNEY")
                        & (TravelerCapability.journey_id == str(journey_id))
                    )
                    if journey_id is not None
                    else (TravelerCapability.scope == "TRAVELER")
                ),
            )
            .order_by(TravelerCapability.scope, TravelerCapability.code, TravelerCapability.id)
        ).all()
    )
    return rows


def capability_row_payload(row: TravelerCapability) -> dict[str, Any]:
    return {
        "id": row.id,
        "traveler_id": row.traveler_id,
        "journey_id": row.journey_id,
        "code": row.code,
        "class": row.capability_class,
        "severity": row.severity,
        "params": _load(row.params_json, {}),
        "scope": row.scope,
        "source": row.source,
        "provenance": row.provenance,
        "explicit": bool(row.explicit),
        "confirmed": bool(row.confirmed),
        "active": bool(row.active),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "confirmed_at": row.confirmed_at,
    }


def capability_payload(session: Session, traveler_id: str, journey_id: str | None = None) -> list[dict[str, Any]]:
    return [capability_row_payload(row) for row in active_capability_rows(session, traveler_id, journey_id)]


def capability_constraint_items(
    session: Session, traveler_id: str, journey_id: str | None = None
) -> list[dict[str, Any]]:
    return capability_constraints(capability_payload(session, traveler_id, journey_id))


def refresh_snapshot(session: Session, traveler_id: str) -> TravelerTwinSnapshot:
    explicit = _explicit_items(session, traveler_id)
    evidence = _active_evidence(session, traveler_id)
    grouped: dict[tuple[str, str], list[TravelerPreferenceEvidence]] = defaultdict(list)
    for item in evidence:
        grouped[(item.preference_dimension, item.direction)].append(item)
    learned: list[dict[str, Any]] = []
    for (dimension, direction), items in sorted(grouped.items()):
        count = sum(max(1, int(item.evidence_count)) for item in items)
        strength = min(1.0, round(0.18 * count + 0.04 * max(0, count - 1), 3))
        confidence = min(0.95, round(0.25 + 0.15 * max(0, count - 1), 3))
        learned.append(
            {
                "dimension": dimension,
                "direction": direction,
                "strength": strength,
                "confidence": confidence,
                "evidence_count": count,
                "source": "LEARNED_SOFT",
                "explanation": items[-1].explanation,
                "evidence_ids": [item.id for item in items],
                "journey_ids": sorted({item.journey_id for item in items if item.journey_id}),
                "last_evidence_at": items[-1].created_at,
                "expires_at": items[-1].expires_at,
            }
        )
    explicit_keys = {(item["category"], item["dimension"]) for item in explicit}
    conflicts: list[dict[str, Any]] = []
    by_dimension: dict[str, set[str]] = defaultdict(set)
    for item in learned:
        by_dimension[item["dimension"]].add(item["direction"])
    for dimension, directions in sorted(by_dimension.items()):
        if len(directions) > 1:
            conflicts.append(
                {
                    "dimension": dimension,
                    "directions": sorted(directions),
                    "resolution": "recent evidence and explicit mission win",
                }
            )
        if ("interest", dimension) in explicit_keys or ("mobility", dimension) in explicit_keys:
            conflicts.append(
                {
                    "dimension": dimension,
                    "directions": sorted(directions),
                    "resolution": "EXPLICIT overrides LEARNED_SOFT",
                }
            )
    conflict_by_dimension = {item["dimension"]: item for item in conflicts}
    for item in learned:
        item["conflict"] = conflict_by_dimension.get(item["dimension"])
    now = _now()
    explicit_capabilities = capability_payload(session, traveler_id)
    snapshot = session.get(TravelerTwinSnapshot, str(traveler_id))
    if snapshot is None:
        snapshot = TravelerTwinSnapshot(traveler_id=str(traveler_id), updated_at=now)
        session.add(snapshot)
    snapshot.explicit_preferences_json = _json(explicit)
    snapshot.learned_soft_preferences_json = _json(learned)
    snapshot.conflicts_json = _json(conflicts)
    snapshot.explicit_capabilities_json = _json(explicit_capabilities)
    snapshot.confidence = max((float(item["confidence"]) for item in learned), default=0.0)
    snapshot.evidence_count = sum(int(item["evidence_count"]) for item in learned)
    snapshot.provenance_json = _json(
        {
            "source": "journey_outcome_events",
            "evidence_ids": [item.id for item in evidence],
            "updated_at": now,
        }
    )
    snapshot.updated_at = now
    session.flush()
    return snapshot


def snapshot_payload(snapshot: TravelerTwinSnapshot) -> dict[str, Any]:
    explicit_capabilities = _load(getattr(snapshot, "explicit_capabilities_json", "[]"), [])
    return {
        "traveler_id": snapshot.traveler_id,
        "explicit_preferences": _load(snapshot.explicit_preferences_json, []),
        "explicit_capabilities": explicit_capabilities,
        "capability_summary": summarize_capabilities(explicit_capabilities),
        "learned_soft_preferences": _load(snapshot.learned_soft_preferences_json, []),
        "confidence": float(snapshot.confidence),
        "evidence_count": int(snapshot.evidence_count),
        "conflicts": _load(snapshot.conflicts_json, []),
        "provenance": _load(snapshot.provenance_json, {}),
        "updated_at": snapshot.updated_at,
    }


def learn_from_journey(session: Session, journey: Journey) -> TravelerTwinSnapshot:
    """Accumulate bounded evidence from actual outcome events for one Journey."""

    events = session.scalars(
        select(JourneyOutcomeEvent)
        .where(JourneyOutcomeEvent.journey_id == journey.id)
        .order_by(JourneyOutcomeEvent.occurred_at_actual, JourneyOutcomeEvent.id)
    ).all()
    grouped: dict[tuple[str, str], list[JourneyOutcomeEvent]] = defaultdict(list)
    for event in events:
        signal = _event_signal(session, event)
        if signal is not None:
            grouped[signal].append(event)
    now = _now()
    for (dimension, direction), rows in grouped.items():
        existing = session.scalar(
            select(TravelerPreferenceEvidence)
            .where(
                TravelerPreferenceEvidence.traveler_id == journey.traveler_id,
                TravelerPreferenceEvidence.journey_id == journey.id,
                TravelerPreferenceEvidence.preference_dimension == dimension,
                TravelerPreferenceEvidence.direction == direction,
                TravelerPreferenceEvidence.status == "ACTIVE",
            )
            .order_by(TravelerPreferenceEvidence.created_at.desc())
        )
        explanation = (
            f"Journey outcome evidence recorded {len(rows)} actual decision(s): "
            f"the traveler accepted/completed evidence consistent with {direction}."
        )
        related = [item.id for item in rows]
        if existing is None:
            session.add(
                TravelerPreferenceEvidence(
                    id=str(uuid4()),
                    traveler_id=journey.traveler_id,
                    journey_id=journey.id,
                    evidence_type="BEHAVIORAL_SOFT",
                    preference_dimension=dimension,
                    direction=direction,
                    strength=min(1.0, 0.18 * len(rows)),
                    confidence=min(0.95, 0.25 + 0.15 * max(0, len(rows) - 1)),
                    source="OUTCOME_TELEMETRY",
                    evidence_count=len(rows),
                    explanation=explanation,
                    related_ids_json=_json(related),
                    created_at=now,
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=180)).isoformat(),
                    status="ACTIVE",
                )
            )
        else:
            existing.evidence_count = max(existing.evidence_count, len(rows))
            existing.related_ids_json = _json(
                sorted(set(_load(existing.related_ids_json, []) + related))
            )
            existing.explanation = explanation
            existing.created_at = now
            existing.expires_at = (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()
    session.flush()
    return refresh_snapshot(session, journey.traveler_id)


def clear_learned(session: Session, traveler_id: str) -> TravelerTwinSnapshot:
    for item in _active_evidence(session, traveler_id):
        item.status = "CLEARED"
    session.flush()
    return refresh_snapshot(session, traveler_id)


def planning_preference_items(
    session: Session, traveler_id: str, journey_id: str | None = None
) -> list[Any]:
    """Return explicit and learned soft items in the shape used by the planner."""

    rows = list(
        session.scalars(
            select(Preference)
            .where(Preference.traveler_id == str(traveler_id))
            .order_by(Preference.category, Preference.preference_key)
        ).all()
    )
    explicit_dimensions = {row.preference_key for row in rows}
    items: list[Any] = rows[:]
    for capability in capability_payload(session, traveler_id, journey_id):
        items.append(
            SimpleNamespace(
                category="capability",
                preference_key=str(capability.get("code") or ""),
                value_json=_json(capability.get("params") or {}),
                weight=1.0 if capability.get("severity") == "HARD" else 0.75,
                source=str(capability.get("source") or "USER_CONFIRMED"),
            )
        )
    if journey_id:
        mission = session.scalar(
            select(JourneyMission)
            .where(
                JourneyMission.journey_id == str(journey_id),
                JourneyMission.status.in_(("ACTIVE", "RUNNING", "CREATED")),
            )
            .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
        )
        mission_preferences = _load(mission.preferences_json if mission else "{}", {})
        for key, value in mission_preferences.items():
            if key in {"protected_activity_ids"}:
                continue
            explicit_dimensions.add(str(key))
            if key == "indoor":
                explicit_dimensions.add("outdoor")
            elif key == "outdoor":
                explicit_dimensions.add("indoor")
            items.append(
                SimpleNamespace(
                    category="mission",
                    preference_key=str(key),
                    value_json=_json(value),
                    weight=1.0,
                    source="EXPLICIT_MISSION",
                )
            )
    snapshot = refresh_snapshot(session, traveler_id)
    for item in _load(snapshot.learned_soft_preferences_json, []):
        if item.get("dimension") in explicit_dimensions:
            continue
        items.append(
            SimpleNamespace(
                category="interest",
                preference_key=str(item.get("direction") or item.get("dimension")),
                value_json="true",
                weight=float(item.get("strength", 0.0)),
                source="LEARNED_SOFT",
            )
        )
    return items
