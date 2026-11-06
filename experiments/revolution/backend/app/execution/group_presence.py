"""Member-scoped, privacy-preserving group presence and rendezvous targeting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    Commitment,
    Journey,
    JourneyIntent,
    JourneyMember,
    JourneyRendezvousMemberSignal,
    JourneyRendezvousSignalAcknowledgement,
    LocationObservation,
    Place,
    RouteAcquisitionOperation,
    Traveler,
)
from app.execution.clock import execution_clock
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE

LOCATION_FRESHNESS_SECONDS = 300
MAX_USEFUL_ACCURACY_M = 100.0
PROTECTED_LEVELS = {"C3_GUARANTEED", "C4_ANCHORED"}
LIVE_ROUTE_PROVENANCE = {"LIVE_PROVIDER_CAPTURE", "LIVE_CAPTURE"}
REPLAY_ROUTE_PROVENANCE = {"CAPTURED_REPLAY", "SEALED_REAL_REPLAY"}


def _stamp(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _member_in_force(member: JourneyMember, now: datetime) -> bool:
    if member.removed_at:
        return False
    expiry = _stamp(member.expires_at)
    return member.expires_at is None or (expiry is not None and expiry > now)


def _distance_m(a: LocationObservation, b: LocationObservation) -> float:
    earth_radius = 6_371_000.0
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return earth_radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def rendezvous_target(session: Session, journey: Journey) -> tuple[Activity, Place] | None:
    """Use the next exact active-plan stop, preferring the next C3/C4 anchor."""
    if not journey.active_plan_version_id:
        return None
    activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == journey.active_plan_version_id)
        .order_by(Activity.sequence_no, Activity.id)
    ).all()
    current = next((row for row in activities if row.id == journey.current_activity_id), None)
    current_state = str(current.status or "").upper() if current else ""
    eligible = [row for row in activities if str(row.status or "").upper() not in {"COMPLETED", "SKIPPED"}]
    if current is not None and current_state in {"ARRIVED", "IN_PROGRESS", "COMPLETED", "SKIPPED"}:
        eligible = [row for row in eligible if row.sequence_no > current.sequence_no]
    # If already en route (or the next activity has not started), that exact stop is the
    # rendezvous. Otherwise use the next uncompleted stop. Never synthesize a midpoint.
    eligible = [row for row in eligible if row.place_id]
    if not eligible:
        return None
    activity_ids = [row.id for row in eligible]
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.activity_id.in_(activity_ids),
            Commitment.status == "ACTIVE",
        )
    ).all()
    protected_ids = {
        str(row.activity_id)
        for row in commitments
        if str(row.level or "").upper() in PROTECTED_LEVELS
    }
    target = next((row for row in eligible if row.id in protected_ids), eligible[0])
    place = session.get(Place, str(target.place_id))
    return (target, place) if place is not None else None


def public_destination_coordinates(place: Place | None) -> tuple[float, float] | None:
    """Return only validated canonical WGS84 coordinates for a public plan Place."""
    if place is None or str(place.coordinate_system or "").upper() != "WGS84":
        return None
    try:
        latitude = float(place.latitude)
        longitude = float(place.longitude)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return latitude, longitude


def _rendezvous_coordination_signals(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str,
    members: list[JourneyMember],
    target_activity: Activity | None,
    target_place: Place | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Project only current, audience-bound self-reports; never route or GPS data."""
    if target_activity is None or target_place is None:
        return []
    current_member_ids = sorted(str(member.id) for member in members)
    if viewer_member_id not in current_member_ids:
        return []
    member_by_id = {str(member.id): member for member in members}
    rows = session.scalars(
        select(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == journey.id,
            JourneyRendezvousMemberSignal.status == "ACTIVE",
        ).order_by(JourneyRendezvousMemberSignal.reported_at, JourneyRendezvousMemberSignal.id)
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        expiry = _stamp(row.expires_at)
        try:
            audience = json.loads(row.audience_member_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not (
            expiry is not None
            and expiry > now
            and str(row.active_plan_version_id) == str(journey.active_plan_version_id or "")
            and str(row.current_activity_id or "") == str(journey.current_activity_id or "")
            and str(row.rendezvous_activity_id) == str(target_activity.id)
            and str(row.public_place_id) == str(target_place.id)
            and isinstance(audience, list)
            and all(isinstance(item, str) for item in audience)
            and sorted(set(audience)) == current_member_ids
            and viewer_member_id in audience
            and str(row.journey_member_id) in member_by_id
            and row.signal_kind in {"HEADING_TO_RENDEZVOUS", "NEEDS_COORDINATION"}
        ):
            continue
        owner = member_by_id[str(row.journey_member_id)]
        traveler = session.get(Traveler, str(owner.traveler_id))
        acknowledgement = session.scalar(
            select(JourneyRendezvousSignalAcknowledgement).where(
                JourneyRendezvousSignalAcknowledgement.signal_id == row.id,
            )
        )
        acknowledgement_projection = None
        if acknowledgement is not None:
            responder = session.get(Traveler, str(acknowledgement.responder_traveler_id))
            acknowledgement_projection = {
                "display_name": responder.display_name if responder else "Traveller",
                "acknowledged_at": str(acknowledgement.acknowledged_at),
                "evidence_level": "SIGNED_ACKNOWLEDGEMENT_NOT_PHYSICAL_PRESENCE",
            }
        result.append({
            "signal_id": str(row.id),
            "signal_kind": str(row.signal_kind),
            "member_id": str(owner.id),
            "display_name": traveler.display_name if traveler else "Traveller",
            "rendezvous_activity_id": str(target_activity.id),
            "rendezvous_title": str(target_activity.title),
            "public_place_id": str(target_place.id),
            "public_place_name": str(target_place.name),
            "reported_at": str(row.reported_at),
            "expires_at": str(row.expires_at),
            "freshness": "CURRENT",
            "evidence_level": "SELF_REPORTED_NOT_VERIFIED",
            "viewer_can_withdraw": str(owner.id) == viewer_member_id,
            "viewer_can_acknowledge": bool(
                row.signal_kind == "NEEDS_COORDINATION"
                and str(owner.id) != viewer_member_id
                and acknowledgement is None
            ),
            "viewer_is_acknowledger": bool(
                acknowledgement is not None
                and str(acknowledgement.responder_member_id) == viewer_member_id
            ),
            "acknowledgement": acknowledgement_projection,
        })
    return result


def _active_planned_separations(
    session: Session,
    journey: Journey,
    members: list[JourneyMember],
    *,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Resolve current member-owned split intents against the active shared plan."""
    from app.agent.journey_intent import intent_is_active

    member_by_id = {str(member.id): member for member in members}
    rows = session.scalars(
        select(JourneyIntent).where(
            JourneyIntent.journey_id == journey.id,
            JourneyIntent.intent_kind == "PLANNED_SEPARATION",
            JourneyIntent.status == "ACTIVE",
        ).order_by(JourneyIntent.created_at.desc(), JourneyIntent.id.desc())
    ).all()
    resolved: dict[str, dict[str, Any]] = {}
    for intent in rows:
        member_id = str(intent.created_by_member_id or "")
        member = member_by_id.get(member_id)
        if member is None or str(intent.created_by_traveler_id) != str(member.traveler_id):
            continue
        if not intent_is_active(
            status=intent.status,
            starts_at=intent.scope_start_at or "",
            ends_at=intent.scope_end_at,
            now=now,
        ):
            continue
        try:
            target_ids = json.loads(intent.target_activity_ids_json or "[]")
            envelope = json.loads(intent.semantic_envelope_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        metadata = envelope.get("r63_planned_separation") if isinstance(envelope, dict) else None
        if not isinstance(metadata, dict) or not target_ids:
            continue
        if str(metadata.get("active_plan_version_id") or "") != str(journey.active_plan_version_id or ""):
            continue
        target = session.get(Activity, str(target_ids[0]))
        if (
            target is None
            or str(target.plan_version_id) != str(journey.active_plan_version_id or "")
            or str(target.status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
        ):
            # A plan change or terminal rendezvous invalidates suppression. The
            # old intent stays durable history; it is never treated as current.
            continue
        place = session.get(Place, str(target.place_id or "")) if target.place_id else None
        if place is None:
            continue
        resolved.setdefault(member_id, {
            "intent": intent,
            "target": target,
            "place": place,
            "metadata": metadata,
        })
    return resolved


def planned_separation_for_member(
    session: Session,
    journey: Journey,
    member: JourneyMember,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return only this in-force member's current, active-plan-bound split target."""
    reference = (now or execution_clock.now()).astimezone(timezone.utc)
    if str(member.journey_id) != str(journey.id) or not _member_in_force(member, reference):
        return None
    return _active_planned_separations(
        session, journey, [member], now=reference
    ).get(str(member.id))


def _planned_return_state(
    *,
    row: LocationObservation | None,
    eta: int | None,
    eta_state: str,
    target: Activity,
    commitment_risk: dict[str, Any] | None,
    now: datetime,
) -> str:
    if eta is None or row is None:
        return "RETURN_UNKNOWN"
    authority = _observation_authority(row)
    if authority != "TRUSTED_MEMBER_LOCATION":
        return "ADVISORY_RETURN_RISK" if (
            isinstance(commitment_risk, dict)
            and commitment_risk.get("state") == "COMMITMENT_AT_RISK"
        ) else "ADVISORY_ROUTE_ONLY" if eta is not None else "RETURN_UNKNOWN"
    if eta_state != "LIVE_PROVIDER_CAPTURE":
        return "RETURN_UNKNOWN"
    if isinstance(commitment_risk, dict):
        state = str(commitment_risk.get("state") or "")
        if state == "COMMITMENT_AT_RISK":
            return "RETURN_RISK"
        if state in {"WITHIN_BUFFER", "LOW_BUFFER"}:
            return "ON_TRACK" if state == "WITHIN_BUFFER" else "LOW_BUFFER"
    scheduled = _stamp(target.start_at)
    if scheduled is None:
        return "RETURN_UNKNOWN"
    slack_min = int((scheduled - (now + timedelta(minutes=eta))).total_seconds() // 60) - 5
    return "RETURN_RISK" if slack_min < 0 else "ON_TRACK"


def _member_observation(
    session: Session, journey: Journey, member: JourneyMember
) -> LocationObservation | None:
    rows = session.scalars(
        select(LocationObservation)
        .where(
            LocationObservation.journey_id == journey.id,
            LocationObservation.traveler_id == member.traveler_id,
            LocationObservation.journey_member_id == member.id,
            LocationObservation.source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        )
    ).all()
    return max(
        rows,
        key=lambda row: (_stamp(row.captured_at) or datetime.min.replace(tzinfo=timezone.utc), row.id),
        default=None,
    )


def _location_state(row: LocationObservation | None, now: datetime) -> tuple[str, int | None]:
    if row is None:
        return "MISSING", None
    captured = _stamp(row.captured_at)
    if captured is None:
        return "UNKNOWN", None
    age = int((now - captured).total_seconds())
    if age < -30:
        return "UNKNOWN", age
    if age > LOCATION_FRESHNESS_SECONDS:
        return "STALE", age
    if float(row.accuracy_m) > MAX_USEFUL_ACCURACY_M:
        return "IMPRECISE", age
    return "RECENT", max(0, age)


def _observation_authority(row: LocationObservation | None) -> str:
    if row is None:
        return "NO_BOUND_LOCATION"
    source = str(row.source or "").upper()
    if source in {"DEVICE_ATTESTED", "LOCATION_ATTESTED"}:
        return "TRUSTED_MEMBER_LOCATION"
    if source == "FOREGROUND_WEB":
        return "ADVISORY_FOREGROUND_WEB"
    return "UNKNOWN_BOUND_LOCATION"


def _group_authority(rows: dict[str, LocationObservation | None]) -> str:
    authorities = {
        _observation_authority(row)
        for row in rows.values()
        if row is not None
    }
    if not authorities:
        return "NO_MEMBER_LOCATION"
    if authorities == {"TRUSTED_MEMBER_LOCATION"}:
        return "TRUSTED_MEMBER_LOCATION"
    if authorities == {"ADVISORY_FOREGROUND_WEB"}:
        return "ADVISORY_FOREGROUND_WEB_NOT_DEVICE_ATTESTED"
    return "MIXED_MEMBER_LOCATION_AUTHORITY"


def _route_eta(
    session: Session,
    journey: Journey,
    row: LocationObservation | None,
    target_activity: Activity | None,
) -> tuple[int | None, str | None, str]:
    if row is None or target_activity is None:
        return None, None, "ROUTE_NOT_CAPTURED"
    routes = session.scalars(
        select(RouteAcquisitionOperation)
        .where(
            RouteAcquisitionOperation.journey_id == journey.id,
            RouteAcquisitionOperation.origin_key == f"location-observation:{row.id}",
            RouteAcquisitionOperation.target_key == str(target_activity.place_id or ""),
            RouteAcquisitionOperation.target_activity_id == target_activity.id,
            RouteAcquisitionOperation.status == "COMPLETED",
        )
        .order_by(RouteAcquisitionOperation.finished_at.desc(), RouteAcquisitionOperation.id.desc())
    ).all()
    route = None
    for candidate in routes:
        try:
            request = json.loads(candidate.request_json or "{}")
            evidence = request.get("origin_evidence") if isinstance(request, dict) else None
        except (TypeError, json.JSONDecodeError):
            evidence = None
        if isinstance(evidence, dict) and evidence.get("private_scope") == "R80_PRIVATE_BUDDY_HANDOFF":
            continue
        route = candidate
        break
    if route is None:
        return None, None, "ROUTE_NOT_CAPTURED"
    try:
        result = json.loads(route.result_json or "{}")
        provenance = str(result.get("provenance") or "UNKNOWN").upper()
        provider = str(result.get("provider") or "").casefold()
        minutes = result.get("duration_min")
        if minutes is None:
            return None, provenance, "ROUTE_DURATION_MISSING"
        if provider.startswith(("mock", "fixture")):
            return None, provenance, "NON_PRODUCT_PROVIDER"
        if provenance in LIVE_ROUTE_PROVENANCE:
            return max(0, int(minutes)), provenance, "LIVE_PROVIDER_CAPTURE"
        if provenance in REPLAY_ROUTE_PROVENANCE:
            return max(0, int(minutes)), provenance, "CAPTURED_REPLAY"
        return None, provenance, "PROVENANCE_NOT_AUTHORIZED"
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "UNKNOWN", "ROUTE_RECEIPT_INVALID"


def group_recovery_incident_key(
    *,
    journey_id: str,
    active_plan_version_id: str,
    affected_member_id: str,
    rendezvous_activity_id: str,
) -> str:
    """Stable incident identity; evidence row IDs and repeated taps do not fan out work."""
    identity = "|".join((
        str(journey_id),
        str(active_plan_version_id),
        str(affected_member_id),
        str(rendezvous_activity_id),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def group_recovery_projection(group_presence: dict[str, Any]) -> dict[str, Any]:
    """Build a privacy-minimized recovery choice from the member-specific read model."""
    members = [item for item in group_presence.get("members", []) if isinstance(item, dict)]
    base = {
        "state": "INSUFFICIENT_MEMBER_TRUTH",
        "headline": "There is not enough current route evidence to assess a shared recovery.",
        "affected_member": None,
        "shared_commitment": None,
        "options": [],
        "decision_authority": "COLLECTIVE_IF_PLAN_CHANGES",
        "can_request_planning": False,
    }
    if group_presence.get("state") == "MEMBER_IDENTITY_UNAVAILABLE":
        return {**base, "state": "INACTIVE", "headline": "A signed Journey member is required."}
    if len(members) <= 1:
        return {**base, "state": "INACTIVE", "headline": "This Journey has no shared group recovery."}
    if group_presence.get("state") == "TOGETHER":
        return {**base, "state": "GROUP_TOGETHER", "headline": "Recent member locations show the group together."}

    at_risk = [
        member for member in members
        if isinstance(member.get("shared_commitment_risk"), dict)
        and member["shared_commitment_risk"].get("state") == "COMMITMENT_AT_RISK"
        and member.get("route_eta_minutes") is not None
        and member.get("authority") in {
            "ADVISORY_FOREGROUND_WEB",
            "TRUSTED_MEMBER_LOCATION",
        }
        and (
            not member.get("planned_separation")
            or (
                member.get("authority") == "TRUSTED_MEMBER_LOCATION"
                and member.get("route_eta_state") == "LIVE_PROVIDER_CAPTURE"
            )
        )
    ]
    if not at_risk:
        has_route = any(member.get("route_eta_minutes") is not None for member in members)
        if has_route:
            return {**base, "state": "NO_SHARED_RISK", "headline": "Current route evidence does not show a missed protected group commitment."}
        return base

    at_risk.sort(key=lambda member: (
        int((member.get("shared_commitment_risk") or {}).get("slack_after_buffer_min") or 0),
        str(member.get("member_id") or ""),
    ))
    member = at_risk[0]
    risk = member["shared_commitment_risk"]
    advisory = member.get("authority") == "ADVISORY_FOREGROUND_WEB"
    state = "ADVISORY_MEMBER_LATE" if advisory else "VERIFIED_MEMBER_RISK"
    arrival = member.get("route_eta_minutes")
    commitment = risk.get("next_commitment") if isinstance(risk.get("next_commitment"), dict) else None
    name = str(member.get("display_name") or "A traveller")
    title = str((commitment or {}).get("title") or "the next shared commitment")
    return {
        **base,
        "state": state,
        "headline": (
            f"{name} may not reach {title} within the protected time buffer."
            if advisory
            else f"Trusted member-location evidence shows {name} may miss {title}."
        ),
        "affected_member": {
            "member_id": member.get("member_id"),
            "display_name": name,
            "authority": member.get("authority"),
            "eta_minutes": arrival,
            "route_provenance": member.get("route_eta_provenance"),
            "route_evidence_state": member.get("route_eta_state"),
        },
        "shared_commitment": {
            "activity_id": (commitment or {}).get("activity_id"),
            "title": title,
            "slack_after_buffer_min": risk.get("slack_after_buffer_min"),
        },
        "options": [
            {"kind": "KEEP_PLAN_MEMBER_CATCH_UP", "mutates_plan": False},
            {"kind": "ADAPT_REMAINING_JOURNEY", "mutates_plan": True},
        ],
        "can_request_planning": True,
    }


def rendezvous_commitment_risk(
    session: Session,
    journey: Journey,
    target_activity: Activity,
    *,
    route_duration_min: int,
    authority: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project whether this member-specific route threatens the shared protected stop."""

    base = {
        "state": "NO_PROTECTED_RENDEZVOUS_COMMITMENT",
        "authority": authority,
        "next_commitment": None,
        "predicted_arrival_at": None,
        "slack_after_buffer_min": None,
    }
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.activity_id == target_activity.id,
            Commitment.status == "ACTIVE",
        )
    ).all()
    commitment = next(
        (
            row
            for row in commitments
            if str(row.level or "").upper() in PROTECTED_LEVELS
        ),
        None,
    )
    if commitment is None:
        return base
    try:
        policy = json.loads(commitment.policy_json or "{}")
    except (TypeError, json.JSONDecodeError):
        policy = {}
    scheduled = _stamp(
        str(policy.get("scheduled_start_at") or target_activity.start_at)
    )
    reference = (now or execution_clock.now()).astimezone(timezone.utc)
    if scheduled is None:
        return {
            **base,
            "state": "COMMITMENT_TIME_UNKNOWN",
            "next_commitment": {
                "commitment_id": commitment.id,
                "activity_id": target_activity.id,
                "title": target_activity.title,
            },
        }
    buffer_min = 5
    for key in ("safety_buffer_min", "required_buffer_min", "resilience_buffer_min"):
        if policy.get(key) is None:
            continue
        try:
            buffer_min = max(0, int(policy[key]))
            break
        except (TypeError, ValueError):
            continue
    duration = max(0, int(route_duration_min))
    predicted = reference + timedelta(minutes=duration)
    slack = int((scheduled - predicted).total_seconds() // 60) - buffer_min
    state = (
        "COMMITMENT_AT_RISK"
        if slack < 0
        else "LOW_BUFFER"
        if slack < max(10, buffer_min * 2)
        else "WITHIN_BUFFER"
    )
    return {
        "state": state,
        "authority": authority,
        "next_commitment": {
            "commitment_id": commitment.id,
            "activity_id": target_activity.id,
            "title": target_activity.title,
            "scheduled_at": scheduled.isoformat(),
            "safety_buffer_min": buffer_min,
        },
        "predicted_arrival_at": predicted.isoformat(),
        "slack_after_buffer_min": slack,
        "route_duration_min": duration,
        "basis": "DIRECT_MEMBER_ROUTE_TO_EXISTING_SHARED_COMMITMENT",
    }


def _group_state(
    *,
    members: list[JourneyMember],
    location_states: dict[str, str],
    coarse_group: dict[str, str],
) -> str:
    """Return one stable group-level fact so every consumer tells the same story."""

    if len(members) <= 1:
        return "SINGLE_TRAVELLER"
    if any(coarse_group.get(member.id) == "SPLIT_FROM_GROUP" for member in members):
        return "GROUP_SPLIT_ADVISORY"
    if all(
        location_states.get(member.id) == "RECENT"
        and coarse_group.get(member.id) == "WITH_GROUP"
        for member in members
    ):
        return "TOGETHER"
    if any(location_states.get(member.id) == "MISSING" for member in members):
        return "MEMBER_LOCATION_MISSING"
    if any(
        location_states.get(member.id) in {"STALE", "IMPRECISE", "UNKNOWN"}
        for member in members
    ):
        return "GROUP_LOCATION_STALE"
    return "GROUP_LOCATION_UNKNOWN"


def group_presence_projection(
    session: Session, journey: Journey, *, viewer_member_id: str | None
) -> dict[str, Any]:
    """Return coarse member states plus the shared public Place destination.

    The canonical active-plan Place may have WGS84 coordinates. Member
    observation origins, routes, and identifiers are never included.
    """
    if not viewer_member_id:
        return {
            "state": "MEMBER_IDENTITY_UNAVAILABLE",
            "authority": "SIGNED_TRAVELER_PRINCIPAL_REQUIRED",
            "active_plan_version_id": None,
            "current_activity_id": None,
            "members": [],
            "coordination_signals": [],
            "coordination_audience_sha256": None,
            "rendezvous": None,
            "rendezvous_state": "RENDEZVOUS_TARGET_UNAVAILABLE",
            "viewer_route": None,
            "can_guide": False,
        }
    now = execution_clock.now().astimezone(timezone.utc)
    members = [
        member for member in session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey.id)
        ).all() if _member_in_force(member, now)
    ]
    viewer = next((member for member in members if member.id == str(viewer_member_id)), None)
    if viewer is None:
        return {
            "state": "MEMBER_IDENTITY_UNAVAILABLE",
            "authority": "SIGNED_TRAVELER_PRINCIPAL_REQUIRED",
            "active_plan_version_id": None,
            "current_activity_id": None,
            "members": [],
            "coordination_signals": [],
            "coordination_audience_sha256": None,
            "rendezvous": None,
            "rendezvous_state": "RENDEZVOUS_TARGET_UNAVAILABLE",
            "viewer_route": None,
            "can_guide": False,
        }
    target_pair = rendezvous_target(session, journey)
    target_activity, target_place = target_pair if target_pair else (None, None)
    planned_separations = _active_planned_separations(
        session, journey, members, now=now
    )
    rows = {member.id: _member_observation(session, journey, member) for member in members}
    location_states = {member_id: _location_state(row, now)[0] for member_id, row in rows.items()}
    coarse_group = {member.id: "UNKNOWN" for member in members}
    unplanned = [member for member in members if member.id not in planned_separations]
    precise = [member for member in unplanned if location_states[member.id] == "RECENT"]
    for member in precise:
        comparisons = []
        row = rows[member.id]
        for other in precise:
            if member.id == other.id:
                continue
            other_row = rows[other.id]
            threshold = max(250.0, 3.0 * (float(row.accuracy_m) + float(other_row.accuracy_m)))
            comparisons.append(_distance_m(row, other_row) <= threshold)
        coarse_group[member.id] = "WITH_GROUP" if all(comparisons) else "SPLIT_FROM_GROUP"
    # Planned travellers are excluded from the remaining group's cohesion test.
    # Otherwise the existing pairwise projection marks both people in a two-person
    # group as unexpectedly split when only one is intentionally away.
    for member_id in planned_separations:
        row = rows.get(member_id)
        if row is None or location_states.get(member_id) != "RECENT":
            continue
        comparisons = []
        for other in precise:
            other_row = rows[other.id]
            threshold = max(250.0, 3.0 * (float(row.accuracy_m) + float(other_row.accuracy_m)))
            comparisons.append(_distance_m(row, other_row) <= threshold)
        if comparisons:
            coarse_group[member_id] = "WITH_GROUP" if all(comparisons) else "SPLIT_FROM_GROUP"

    projected = []
    planned_members: list[dict[str, Any]] = []
    for member in members:
        traveler = session.get(Traveler, member.traveler_id)
        row = rows[member.id]
        state, age = _location_state(row, now)
        planned = planned_separations.get(str(member.id))
        route_target = planned["target"] if planned else target_activity
        eta, eta_provenance, eta_state = _route_eta(
            session, journey, row, route_target
        )
        route_authority = (
            "TRUSTED_MEMBER_LOCATION_ROUTE"
            if _observation_authority(row) == "TRUSTED_MEMBER_LOCATION"
            else "ADVISORY_FOREGROUND_LOCATION"
        )
        commitment_risk = (
            rendezvous_commitment_risk(
                session,
                journey,
                route_target,
                route_duration_min=eta,
                authority=route_authority,
                now=now,
            )
            if eta is not None and route_target is not None
            else None
        )
        return_state = None
        planned_rendezvous = None
        if planned:
            # A saved replay is useful context, but it is not a current route
            # for waking Guardian on an intentional split.
            if (
                _observation_authority(row) == "TRUSTED_MEMBER_LOCATION"
                and eta_state != "LIVE_PROVIDER_CAPTURE"
            ):
                commitment_risk = None
            return_state = _planned_return_state(
                row=row,
                eta=eta,
                eta_state=eta_state,
                target=route_target,
                commitment_risk=commitment_risk,
                now=now,
            )
            place = planned["place"]
            planned_rendezvous = {
                "activity_id": str(route_target.id),
                "title": str(route_target.title),
                "place_name": str(place.name),
                "scheduled_at": route_target.start_at,
            }
            planned_members.append({
                "member_id": str(member.id),
                "display_name": traveler.display_name if traveler else "Traveller",
                "is_viewer": member.id == viewer.id,
                "planned_separation": True,
                "return_state": return_state,
                "route_provenance": eta_provenance,
                "rendezvous": planned_rendezvous,
            })
        projected.append({
            "member_id": member.id,
            "display_name": traveler.display_name if traveler else "Traveller",
            "is_viewer": member.id == viewer.id,
            "location_state": state,
            "group_state": coarse_group[member.id],
            "age_seconds": age,
            "authority": _observation_authority(row),
            "route_eta_minutes": eta,
            "route_eta_provenance": eta_provenance,
            "route_eta_state": eta_state,
            "shared_commitment_risk": commitment_risk,
            "planned_separation": bool(planned),
            "return_state": return_state,
            "planned_rendezvous": planned_rendezvous,
        })
    unexpected_split = any(
        coarse_group.get(member.id) == "SPLIT_FROM_GROUP"
        and member.id not in planned_separations
        for member in members
    )
    if planned_separations and not unexpected_split:
        returns = [item["return_state"] for item in planned_members]
        if "RETURN_RISK" in returns:
            state = "PLANNED_SPLIT_RETURN_RISK"
        elif returns and all(value in {"ON_TRACK", "LOW_BUFFER"} for value in returns):
            state = "PLANNED_SPLIT_ON_TRACK"
        else:
            state = "PLANNED_SPLIT_RETURN_UNKNOWN"
    else:
        state = _group_state(
            members=members,
            location_states=location_states,
            coarse_group=coarse_group,
        )
    rendezvous_ready = target_activity is not None and target_place is not None
    # Public shared-plan geometry is not another member's device location.
    # Only disclose a valid canonical WGS84 destination point; never guess
    # missing geometry or misinterpret a source coordinate system.
    public_destination_coords = public_destination_coordinates(target_place)
    viewer_row = rows.get(viewer.id)
    viewer_planned = planned_separations.get(str(viewer.id))
    viewer_eta, viewer_eta_provenance, viewer_eta_state = _route_eta(
        session,
        journey,
        viewer_row,
        viewer_planned["target"] if viewer_planned else target_activity,
    )
    viewer_planned_member = next((item for item in planned_members if item["is_viewer"]), None)
    planned_rendezvous = (
        viewer_planned_member.get("rendezvous")
        if viewer_planned_member
        else planned_members[0].get("rendezvous") if planned_members else None
    )
    planned_state = state if state.startswith("PLANNED_SPLIT_") else None
    viewer_return_state = (
        viewer_planned_member.get("return_state") if viewer_planned_member else None
    )
    viewer_next_action = (
        "RETURN_SOON" if viewer_return_state in {"RETURN_RISK", "LOW_BUFFER", "ADVISORY_RETURN_RISK"}
        else "ROUTE_TO_RENDEZVOUS" if viewer_return_state == "RETURN_UNKNOWN"
        else "CONTINUE_PERSONAL_EXCURSION" if viewer_planned_member
        else None
    )
    return {
        "state": state,
        "authority": _group_authority(rows),
        "active_plan_version_id": str(journey.active_plan_version_id or "") or None,
        "current_activity_id": str(journey.current_activity_id or "") or None,
        "members": projected,
        # The in-force, signed-member audience is server-owned. A browser on
        # local-network HTTP may not expose crypto.subtle; status sharing must
        # not require a client-side hash implementation or location consent.
        "coordination_audience_sha256": hashlib.sha256(
            json.dumps(
                sorted(str(member.id) for member in members),
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "coordination_signals": _rendezvous_coordination_signals(
            session,
            journey,
            viewer_member_id=str(viewer.id),
            members=members,
            target_activity=target_activity,
            target_place=target_place,
            now=now,
        ),
        "rendezvous": (
            {"activity_id": target_activity.id, "title": target_activity.title,
             "place_id": str(target_place.id), "place_name": target_place.name,
             "latitude": public_destination_coords[0] if public_destination_coords else None,
             "longitude": public_destination_coords[1] if public_destination_coords else None,
             "coordinate_system": "WGS84" if public_destination_coords else None,
             "basis": "ACTIVE_PLAN_EXACT_PLACE"}
            if rendezvous_ready else None
        ),
        "rendezvous_state": (
            "RENDEZVOUS_ROUTE_READY"
            if viewer_eta is not None
            else "RENDEZVOUS_TARGET_READY"
            if rendezvous_ready
            else "RENDEZVOUS_TARGET_UNAVAILABLE"
        ),
        "viewer_route": (
            {
                "eta_minutes": viewer_eta,
                "provenance": viewer_eta_provenance,
                "evidence_state": viewer_eta_state,
            }
            if viewer_eta is not None
            else {
                "eta_minutes": None,
                "provenance": viewer_eta_provenance,
                "evidence_state": viewer_eta_state,
            }
            if viewer_eta_state != "ROUTE_NOT_CAPTURED"
            else None
        ),
        "planned_separation": (
            {
                "state": planned_state,
                "active_plan_version_id": str(journey.active_plan_version_id),
                "rendezvous": planned_rendezvous,
                "members": planned_members,
                "viewer_next_action": viewer_next_action,
                "shared_plan_changed": False,
            }
            if planned_members
            else None
        ),
        "can_guide": bool(
            len(members) > 1
            and
            str(journey.status).upper() == "ACTIVE"
            and rendezvous_ready
        ),
    }
