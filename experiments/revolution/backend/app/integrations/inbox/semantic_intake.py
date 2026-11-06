"""V13.2 semantic intake: model meaning in, deterministic identity out."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from rapidfuzz.fuzz import ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    Commitment,
    ExternalReservationDraft,
    Journey,
    JourneyTripContext,
    Place,
    PlanVersion,
    TransportReservation,
)

ALLOWED_ANCHOR_ROLES = {"ARRIVAL", "DEPARTURE", "MID_TRIP", "UNKNOWN"}
STRUCTURAL_ROLES = {"ARRIVAL", "DEPARTURE"}


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _unknown(value: Any) -> bool:
    return value in (None, "", "UNKNOWN", "unknown")


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _compact(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _role_node(role: str) -> str:
    return "arrival-anchor" if role == "ARRIVAL" else "departure-anchor"


def journey_interpreter_context(session: Session, journey: Journey) -> dict[str, Any]:
    """Return bounded Journey facts suitable for a semantic model prompt."""

    context = session.get(JourneyTripContext, journey.id)
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
        )
    ).all()
    activities = []
    if journey.active_plan_version_id:
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.start_at)
        ).all()
    return {
        "city_code": journey.city_code,
        "start_at": journey.start_at,
        "end_at": journey.end_at,
        "anchors": {
            "arrival": _load(context.arrival_anchor_json, {}) if context else {},
            "departure": _load(context.departure_anchor_json, {}) if context else {},
        },
        "protected_commitments": [
            {
                "kind": item.kind,
                "level": item.level,
                "external_ref": item.external_ref,
                "policy": _load(item.policy_json, {}),
            }
            for item in commitments[:12]
        ],
        "candidate_activities": [
            {
                "title": item.title,
                "semantic_key": item.place_id or item.city_service_id,
                "start_at": item.start_at,
                "end_at": item.end_at,
            }
            for item in activities[:30]
        ],
    }


def _candidate_rows(session: Session, journey: Journey) -> list[dict[str, Any]]:
    places = {
        item.id: item
        for item in session.scalars(
            select(Place).where(Place.city_code == journey.city_code)
        ).all()
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    activities = []
    if journey.active_plan_version_id:
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.start_at)
        ).all()
    for item in activities:
        semantic_key = item.place_id or item.city_service_id or item.id
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        place = places.get(item.place_id) if item.place_id else None
        rows.append(
            {
                "semantic_key": semantic_key,
                "activity_id": item.id,
                "label": item.title or (place.name if place else semantic_key),
                "start_at": item.start_at,
                "end_at": item.end_at,
            }
        )
    for item in places.values():
        if item.id in seen:
            continue
        rows.append(
            {
                "semantic_key": item.id,
                "activity_id": None,
                "label": item.name,
                "start_at": None,
                "end_at": None,
            }
        )
    return rows


def resolve_semantic_target(
    session: Session,
    journey: Journey,
    extracted: dict[str, Any],
) -> dict[str, Any] | None:
    role = str(extracted.get("anchor_role") or "UNKNOWN").upper()
    target = extracted.get("semantic_target")
    target = target if isinstance(target, dict) else {}
    if role in STRUCTURAL_ROLES:
        return {
            "status": "RESOLVED",
            "kind": "STRUCTURAL_ANCHOR",
            "semantic_node_key": _role_node(role),
            "label": "Arrival anchor" if role == "ARRIVAL" else "Departure anchor",
            "confidence": _confidence(target.get("confidence") or extracted.get("anchor_role_confidence")),
            "alternatives": [],
        }
    if role != "MID_TRIP":
        return None

    query = str(target.get("query") or "").strip()
    candidate_key = target.get("candidate_key")
    rows = _candidate_rows(session, journey)
    if candidate_key:
        exact = next(
            (
                row
                for row in rows
                if str(row["semantic_key"]) == str(candidate_key)
                or str(row.get("activity_id") or "") == str(candidate_key)
            ),
            None,
        )
        if exact is not None:
            return {
                "status": "RESOLVED",
                "kind": "ACTIVITY_OR_PLACE",
                "semantic_node_key": exact["semantic_key"],
                "label": exact["label"],
                "confidence": max(0.95, _confidence(target.get("confidence"))),
                "alternatives": [],
            }

    if not query:
        return {
            "status": "NEEDS_BINDING",
            "kind": "ACTIVITY_OR_PLACE",
            "semantic_node_key": None,
            "label": None,
            "confidence": 0.0,
            "alternatives": [],
        }

    needle = _compact(query)
    scored = []
    for row in rows:
        label = _compact(row["label"])
        score = ratio(needle, label) / 100.0
        if needle in label or label in needle:
            score = max(score, 0.92)
        if score >= 0.68:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1]["label"]))
    if not scored:
        return {
            "status": "NEEDS_BINDING",
            "kind": "ACTIVITY_OR_PLACE",
            "semantic_node_key": None,
            "label": None,
            "confidence": 0.0,
            "alternatives": [],
        }
    top_score, top = scored[0]
    alternatives = [
        {"label": row["label"], "confidence": round(score, 3)}
        for score, row in scored[:3]
    ]
    if len(scored) > 1 and scored[1][0] >= 0.68 and top_score - scored[1][0] < 0.08:
        return {
            "status": "AMBIGUOUS",
            "kind": "ACTIVITY_OR_PLACE",
            "semantic_node_key": None,
            "label": None,
            "confidence": round(top_score, 3),
            "alternatives": alternatives,
        }
    return {
        "status": "RESOLVED",
        "kind": "ACTIVITY_OR_PLACE",
        "semantic_node_key": top["semantic_key"],
        "label": top["label"],
        "confidence": round(min(1.0, max(top_score, _confidence(target.get("confidence")))), 3),
        "alternatives": alternatives[1:],
    }


def _conflicts(session: Session, journey: Journey, extracted: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    confirmation_ref = extracted.get("confirmation_ref")
    if confirmation_ref:
        duplicate = session.scalar(
            select(TransportReservation).where(
                TransportReservation.journey_id == journey.id,
                TransportReservation.confirmation_ref == str(confirmation_ref),
            )
        )
        if duplicate is not None:
            conflicts.append("DUPLICATE_CONFIRMATION_REF")
    role = str(extracted.get("anchor_role") or "UNKNOWN").upper()
    if role in STRUCTURAL_ROLES:
        commitments = session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
            )
        ).all()
        for item in commitments:
            policy = _load(item.policy_json, {})
            if str(policy.get("anchor_role") or "").upper() == role:
                conflicts.append("EXISTING_" + role + "_ANCHOR")
                break
    return conflicts


def prepare_semantic_intake(
    session: Session,
    journey: Journey,
    extracted: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Normalize model semantics, resolve server identity, and classify intake."""

    normalized = dict(extracted)
    semantic_contract = any(
        key in extracted
        for key in (
            "anchor_role",
            "anchor_role_confidence",
            "semantic_target",
            "candidate_journey_match",
        )
    )
    if semantic_contract:
        role = str(extracted.get("anchor_role") or "UNKNOWN").upper()
        normalized["anchor_role"] = role if role in ALLOWED_ANCHOR_ROLES else "UNKNOWN"
        normalized["anchor_role_confidence"] = _confidence(
            extracted.get("anchor_role_confidence")
        )
        normalized["semantic_target"] = (
            extracted.get("semantic_target")
            if isinstance(extracted.get("semantic_target"), dict)
            else {}
        )
        normalized["payment_status"] = str(extracted.get("payment_status") or "UNKNOWN")
        normalized["refundability"] = str(extracted.get("refundability") or "UNKNOWN")
        normalized["suggested_commitment_level"] = str(
            extracted.get("suggested_commitment_level") or "C4_ANCHORED"
        )
        normalized["reasoning_summary"] = str(extracted.get("reasoning_summary") or "")[:240]
        normalized["uncertain_fields"] = list(extracted.get("uncertain_fields") or [])
        match = extracted.get("candidate_journey_match")
        match = match if isinstance(match, dict) else {}
        explicit_match = str(match.get("match") or "").upper()
        # V13.2 model-shaped fixtures omitted the new enum while still
        # providing a high-confidence explanation. Treat that legacy shape as
        # a positive match; explicit UNKNOWN remains review-only.
        # A booking the traveller entered by hand belongs to this Journey by
        # construction: there is no model judgement to wait for, so the match is
        # established by the user rather than reported as UNKNOWN forever.
        user_entered = (
            str(extracted.get("artifact_source") or "").upper() == "USER_ENTERED"
        )
        match_state = explicit_match or (
            "MATCH" if "confidence" in match else "MATCH" if user_entered else "UNKNOWN"
        )
        if match_state not in {"MATCH", "MISMATCH", "UNKNOWN"}:
            match_state = "UNKNOWN"
        match_confidence = _confidence(match.get("confidence"))
        if user_entered and match_confidence == 0.0:
            match_confidence = 1.0
        normalized["candidate_journey_match"] = {
            "match": match_state,
            "confidence": match_confidence,
            "reason": str(match.get("reason") or "")[:240]
            or ("Traveller entered this booking for this Journey." if user_entered else ""),
        }
        normalized["_semantic_contract"] = "V13.3_PREP"

    role = str(normalized.get("anchor_role") or "").upper()
    target = dict(normalized.get("semantic_target") or {})
    if role == "MID_TRIP" and normalized.get("semantic_node_key") and not target.get("candidate_key"):
        target["candidate_key"] = normalized["semantic_node_key"]
        normalized["semantic_target"] = target
    resolved = resolve_semantic_target(session, journey, normalized) if semantic_contract else None
    if resolved and resolved.get("status") == "RESOLVED":
        normalized["semantic_node_key"] = resolved["semantic_node_key"]
        target = dict(normalized.get("semantic_target") or {})
        target.update(
            {
                "resolved_key": resolved["semantic_node_key"],
                "resolved_label": resolved["label"],
                "resolution_confidence": resolved["confidence"],
            }
        )
        normalized["semantic_target"] = target

    unresolved = [
        field
        for field in (
            "reservation_type",
            "origin",
            "destination",
            "scheduled_departure_at",
            "scheduled_arrival_at",
            "confirmation_ref",
        )
        if _unknown(normalized.get(field))
    ]
    reasons: list[str] = []
    conflicts = _conflicts(session, journey, normalized) if semantic_contract else []
    if semantic_contract:
        if role not in {"ARRIVAL", "DEPARTURE", "MID_TRIP"}:
            reasons.append("ANCHOR_ROLE_REVIEW_REQUIRED")
        if role == "MID_TRIP" and (resolved is None or resolved.get("status") != "RESOLVED"):
            reasons.append("SEMANTIC_TARGET_REVIEW_REQUIRED")
        if normalized["anchor_role_confidence"] < 0.95:
            reasons.append("ANCHOR_ROLE_CONFIDENCE_LOW")
        journey_match = normalized["candidate_journey_match"]
        if journey_match["match"] == "MISMATCH" and journey_match["confidence"] >= 0.8:
            reasons.append("JOURNEY_MISMATCH")
        elif journey_match["match"] == "UNKNOWN":
            reasons.append("JOURNEY_MATCH_UNKNOWN")
        elif journey_match["confidence"] < 0.75:
            reasons.append("JOURNEY_MATCH_CONFIDENCE_LOW")
        if normalized.get("uncertain_fields"):
            reasons.extend(str(item) for item in normalized["uncertain_fields"])
    if conflicts:
        reasons.extend(conflicts)

    if not semantic_contract:
        decision = "NEEDS_REVIEW"
        reasons.append("SEMANTIC_INTERPRETATION_REQUIRED")
    elif "DUPLICATE_CONFIRMATION_REF" in conflicts:
        decision = "REQUIRES_CONFIRMATION"
    elif any(item.startswith("EXISTING_") for item in conflicts):
        decision = "REQUIRES_CONFIRMATION"
    elif "JOURNEY_MISMATCH" in reasons:
        decision = "REQUIRES_CONFIRMATION"
    elif unresolved or reasons:
        decision = "NEEDS_REVIEW"
    elif role == "MID_TRIP" or normalized.get("payment_status") == "PAID":
        decision = "APPLY_WITH_NOTICE"
    else:
        decision = "AUTO_APPLY"
    normalized["smart_intake"] = {
        "decision": decision,
        "reasons": list(dict.fromkeys(reasons)),
        "conflicts": conflicts,
        "resolved_target": resolved,
        "role_confidence": normalized.get("anchor_role_confidence"),
    }
    return normalized, unresolved


def journey_memory_payload(session: Session, journey: Journey) -> dict[str, Any]:
    drafts = {
        item.id: _load(item.extracted_json, {})
        for item in session.scalars(
            select(ExternalReservationDraft).where(
                ExternalReservationDraft.journey_id == journey.id
            )
        ).all()
    }
    reservations = session.scalars(
        select(TransportReservation)
        .where(TransportReservation.journey_id == journey.id)
        .order_by(TransportReservation.scheduled_departure_at)
    ).all()
    items: list[dict[str, Any]] = []
    reservation_commitment_ids: set[str] = set()
    for reservation in reservations:
        extracted = drafts.get(reservation.draft_id, {})
        role = str(extracted.get("anchor_role") or "UNKNOWN").upper()
        policy = {}
        if reservation.commitment_id:
            commitment = session.get(Commitment, reservation.commitment_id)
            policy = _load(commitment.policy_json, {}) if commitment else {}
            reservation_commitment_ids.add(reservation.commitment_id)
        location = (
            (extracted.get("origin") or {}).get("name")
            if role == "DEPARTURE" and isinstance(extracted.get("origin"), dict)
            else (extracted.get("destination") or {}).get("name")
            if isinstance(extracted.get("destination"), dict)
            else None
        )
        items.append(
            {
                "id": reservation.id,
                "kind": "RESERVATION",
                "role": role,
                "title": {
                    "ARRIVAL": "Arrive",
                    "DEPARTURE": "Depart",
                    "MID_TRIP": "Journey commitment",
                }.get(role, "Travel fact"),
                "label": location or reservation.confirmation_ref or reservation.reservation_type,
                "at": policy.get("scheduled_start_at")
                or reservation.scheduled_departure_at,
                "status": "PROTECTED",
                "protected": bool(reservation.commitment_id),
                "confidence": (extracted.get("anchor_role_confidence")),
                "reasoning_summary": extracted.get("reasoning_summary") or "",
                "semantic_node_key": policy.get("semantic_node_key"),
            }
        )
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
        )
    ).all()
    for commitment in commitments:
        if commitment.id in reservation_commitment_ids or not commitment.activity_id:
            continue
        activity = session.get(Activity, commitment.activity_id)
        if activity is None:
            continue
        policy = _load(commitment.policy_json, {})
        items.append(
            {
                "id": commitment.id,
                "kind": "COMMITMENT",
                "role": str(policy.get("anchor_role") or "MID_TRIP"),
                "title": activity.title,
                "label": activity.title,
                "at": policy.get("scheduled_start_at") or activity.start_at,
                "status": "PROTECTED",
                "protected": True,
                "confidence": None,
                "reasoning_summary": "Protected by a confirmed Journey commitment.",
                "semantic_node_key": policy.get("semantic_node_key"),
            }
        )
    items.sort(key=lambda item: str(item.get("at") or ""))
    return {"journey_id": journey.id, "title": "Journey Memory", "items": items}
