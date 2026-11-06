"""Pair-private public-place handoffs after an authenticated R78 help request."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Journey,
    JourneyMember,
    JourneyRendezvousBuddyExecution,
    JourneyRendezvousBuddyHandoff,
    JourneyRendezvousMemberSignal,
    JourneyRendezvousSignalAcknowledgement,
    Place,
    Traveler,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import public_destination_coordinates
from app.execution.rendezvous_coordination import (
    _claim_journey_revision,
    _lock_journey_for_withdraw,
    _current_context,
    _parse_audience,
    _row_is_current,
)
from app.traveler_principal import TravelerPrincipal

HANDOFF_TTL = timedelta(minutes=20)
HANDOFF_ACTIVE = {"OFFERED", "ACCEPTED"}


def _now() -> datetime:
    return execution_clock.now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _unavailable_status(row: JourneyRendezvousBuddyHandoff, now: datetime) -> str:
    try:
        expiry = datetime.fromisoformat(row.expires_at)
    except (TypeError, ValueError):
        return "STALE"
    if expiry.tzinfo is not None and expiry.astimezone(timezone.utc) <= now:
        return "EXPIRED"
    return "STALE"


def _retire_unavailable(
    session: Session,
    row: JourneyRendezvousBuddyHandoff,
    *,
    journey_id: str,
    actor_id: str,
    now: datetime,
) -> str:
    status = _unavailable_status(row, now)
    closed_at = _iso(now)
    row.status = status
    row.closed_at = closed_at
    row.updated_at = closed_at
    record_audit(
        session,
        action=f"journey.rendezvous_buddy_handoff.{status.casefold()}",
        entity_type="JourneyRendezvousBuddyHandoff",
        entity_id=row.id,
        journey_id=journey_id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=actor_id,
        after={"status": status, "shared_plan_changed": False},
    )
    return status


def _member_in_force(member: JourneyMember, now: datetime) -> bool:
    if member.removed_at:
        return False
    if member.expires_at is None:
        return True
    try:
        expires = datetime.fromisoformat(member.expires_at)
    except (TypeError, ValueError):
        return False
    return expires.tzinfo is not None and expires.astimezone(timezone.utc) > now


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audience_json(members: list[JourneyMember]) -> str:
    return json.dumps(sorted(str(member.id) for member in members), separators=(",", ":"))


def _distance_m(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> int:
    radius = 6_371_000.0
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    d_lat = lat_b - lat_a
    d_lon = math.radians(longitude_b - longitude_a)
    value = math.sin(d_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(d_lon / 2) ** 2
    return int(round(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))))


def _candidate(place: Place, *, original: Place, distance_m: int) -> dict[str, object]:
    provider = str(place.provider_key or "unknown")
    normalized = provider.casefold()
    metadata: dict[str, object]
    try:
        decoded = json.loads(place.metadata_json or "{}")
        metadata = decoded if isinstance(decoded, dict) else {}
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    fixture_source = (
        any(token in normalized for token in ("mock", "fixture", "synthetic", "test"))
        or str(metadata.get("authority_envelope") or "").upper() == "SEALED_DEMO_FIXTURE"
    )
    is_original = str(place.id) == str(original.id)
    coords = public_destination_coordinates(place)
    if coords is None:
        raise HTTPException(409, "NO_GROUNDED_PUBLIC_HANDOFF")
    source_kind = "CURRENT_SHARED_PLAN_STOP" if is_original else "PUBLIC_PLACE_CATALOG_RECORD"
    evidence = "DEMO_FIXTURE_NOT_LIVE_VERIFIED" if fixture_source else "CATALOG_RECORD_NOT_ROUTE_VERIFIED"
    source_label = (
        f"Current shared-plan stop · {provider} · demo fixture, not live-verified"
        if is_original and fixture_source
        else f"Current shared-plan stop · {provider}"
        if is_original
        else f"Demo catalog fixture · {provider} · not live-verified"
        if fixture_source
        else f"Public catalog record · {provider} · not route-verified"
    )
    return {
        "place_id": str(place.id),
        "name": str(place.name),
        "category": str(place.category or "public place"),
        "latitude": coords[0],
        "longitude": coords[1],
        "coordinate_system": "WGS84",
        "source_kind": source_kind,
        "source_provider_key": provider,
        "source_external_id": str(place.external_id),
        "source_label": source_label,
        "evidence_grade": evidence,
        "is_original_shared_stop": is_original,
        "distance_from_original_m": distance_m,
        "distance_basis": "STRAIGHT_LINE_NOT_ETA",
        "opening_hours_state": "UNKNOWN",
        "accessibility_state": "UNKNOWN",
        "route_feasibility_state": "UNKNOWN",
        "mutates_shared_plan": False,
        "protected_commitment_warning": True,
    }


def _is_public_place(place: Place) -> bool:
    """Reject a public handoff if *any* catalog source explicitly denies access.

    Never let a later metadata dictionary overwrite an OSM private access tag.
    OSM adapters can store their access facts at the root or under "tags".
    Absence of a restriction remains UNKNOWN, not proof of legal entry;
    handoff feasibility is still displayed as UNKNOWN.
    """
    restricted = {"private", "no", "restricted", "staff", "customers"}
    for raw in (place.tags_json, place.metadata_json):
        try:
            decoded = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue
        for facts in (decoded, decoded.get("tags"), decoded.get("osm_tags")):
            if not isinstance(facts, dict):
                continue
            if facts.get("is_public") is False or facts.get("public") is False:
                return False
            if any(
                str(facts.get(key) or "").strip().casefold() in restricted
                for key in ("access", "visibility")
            ):
                return False
    return True


def _candidate_places(
    session: Session,
    journey: Journey,
    *,
    original: Place,
) -> list[dict[str, object]]:
    origin = public_destination_coordinates(original)
    if origin is None or not _is_public_place(original):
        raise HTTPException(409, "NO_GROUNDED_PUBLIC_HANDOFF")
    original_item = _candidate(original, original=original, distance_m=0)
    rows = session.scalars(
        select(Place)
        .where(Place.city_code == journey.city_code, Place.coordinate_system == "WGS84")
        .order_by(Place.name, Place.id)
    ).all()
    alternatives: list[tuple[int, Place]] = []
    for row in rows:
        if str(row.id) == str(original.id) or not _is_public_place(row):
            continue
        coords = public_destination_coordinates(row)
        if coords is None:
            continue
        distance = _distance_m(origin[0], origin[1], coords[0], coords[1])
        if distance <= 1500:
            alternatives.append((distance, row))
    alternatives.sort(key=lambda item: (item[0], str(item[1].name).casefold(), str(item[1].id)))
    # One exact original stop plus no more than two nearby catalog choices.
    return [original_item] + [
        _candidate(place, original=original, distance_m=distance)
        for distance, place in alternatives[:2]
    ]


def _load_help_context(
    session: Session,
    journey: Journey,
    *,
    signal_id: str,
    viewer: JourneyMember,
    require_helper: bool = False,
    require_requester: bool = False,
) -> tuple[JourneyRendezvousMemberSignal, JourneyRendezvousSignalAcknowledgement, list[JourneyMember], object, Place, datetime]:
    signal = session.scalar(select(JourneyRendezvousMemberSignal).where(
        JourneyRendezvousMemberSignal.id == signal_id,
        JourneyRendezvousMemberSignal.journey_id == journey.id,
    ))
    if signal is None:
        raise HTTPException(404, "RENDEZVOUS_HELP_REQUEST_NOT_FOUND")
    if signal.signal_kind != "NEEDS_COORDINATION":
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUEST_REQUIRED")
    acknowledgement = session.scalar(select(JourneyRendezvousSignalAcknowledgement).where(
        JourneyRendezvousSignalAcknowledgement.signal_id == signal.id,
        JourneyRendezvousSignalAcknowledgement.journey_id == journey.id,
    ))
    if acknowledgement is None:
        raise HTTPException(409, "RENDEZVOUS_HELP_ACKNOWLEDGEMENT_REQUIRED")
    if str(acknowledgement.requester_member_id) != str(signal.journey_member_id):
        raise HTTPException(409, "RENDEZVOUS_HELP_ACKNOWLEDGEMENT_STALE")
    if require_helper and str(acknowledgement.responder_member_id) != str(viewer.id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_HELPER_MUST_BE_ACKNOWLEDGER")
    if require_requester and str(signal.journey_member_id) != str(viewer.id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_REQUESTER_REQUIRED")

    now = _now()
    members, activity, place, context_now = _current_context(
        session,
        journey,
        viewer,
        expected_plan_version_id=str(signal.active_plan_version_id),
        expected_current_activity_id=str(signal.current_activity_id or ""),
        expected_rendezvous_activity_id=str(signal.rendezvous_activity_id),
        expected_public_place_id=str(signal.public_place_id),
    )
    if signal.status != "ACTIVE" or not _row_is_current(
        signal,
        journey=journey,
        members=members,
        activity_id=str(activity.id),
        place_id=str(place.id),
        now=context_now,
    ):
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUEST_STALE")
    member_ids = {str(member.id) for member in members}
    if (
        str(signal.journey_member_id) not in member_ids
        or str(acknowledgement.responder_member_id) not in member_ids
    ):
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_MEMBER_SET_STALE")
    audience = _parse_audience(signal.audience_member_ids_json)
    current_audience = sorted(member_ids)
    if audience != current_audience:
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_AUDIENCE_CHANGED")
    if now < context_now:
        now = context_now
    return signal, acknowledgement, members, activity, place, now


def list_handoff_candidates(
    session: Session,
    *,
    journey: Journey,
    helper: JourneyMember,
    principal: TravelerPrincipal,
    signal_id: str,
) -> dict[str, object]:
    if str(helper.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_SIGNED_ACTOR_MISMATCH")
    signal, acknowledgement, _members, activity, original, _now_value = _load_help_context(
        session,
        journey,
        signal_id=signal_id,
        viewer=helper,
        require_helper=True,
    )
    items = _candidate_places(session, journey, original=original)
    return {
        "help_signal_id": str(signal.id),
        "acknowledgement_id": str(acknowledgement.id),
        "active_plan_version_id": str(journey.active_plan_version_id),
        "current_activity_id": str(journey.current_activity_id) if journey.current_activity_id else None,
        "rendezvous_activity_id": str(activity.id),
        "original_public_place_id": str(original.id),
        "audience_sha256": _sha256(_audience_json(_members)),
        "items": items,
        "maximum_straight_line_distance_m": 1500,
        "route_eta_state": "UNKNOWN",
        "shared_plan_changed": False,
    }


def _name(session: Session, traveler_id: str) -> str:
    traveler = session.get(Traveler, str(traveler_id))
    return str(traveler.display_name) if traveler else "Traveller"


def _payload(
    row: JourneyRendezvousBuddyHandoff,
    session: Session,
    *,
    viewer_member_id: str,
) -> dict[str, object]:
    try:
        place = json.loads(row.handoff_place_snapshot_json)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_PLACE_SNAPSHOT_INVALID")
    return {
        "handoff_id": str(row.id),
        "help_signal_id": str(row.signal_id),
        "status": str(row.status),
        "viewer_role": "HELPER" if str(row.helper_member_id) == viewer_member_id else "REQUESTER",
        "helper_display_name": _name(session, str(row.helper_traveler_id)),
        "requester_display_name": _name(session, str(row.requester_traveler_id)),
        "handoff_place": place,
        "original_shared_stop": {
            "activity_id": str(row.original_rendezvous_activity_id),
            "place_id": str(row.original_public_place_id),
            "place_name": str(session.get(Place, str(row.original_public_place_id)).name)
            if session.get(Place, str(row.original_public_place_id)) else "Original shared stop",
        },
        "offered_at": str(row.offered_at),
        "expires_at": str(row.expires_at),
        "responded_at": str(row.responded_at) if row.responded_at else None,
        "evidence_level": (
            "MUTUAL_PUBLIC_INTENTION_NOT_PHYSICAL_PRESENCE"
            if row.status == "ACCEPTED"
            else "ONE_SIDED_PUBLIC_HANDOFF_OFFER_NOT_ACCEPTED"
        ),
        "shared_plan_changed": False,
        "route_eta_state": "UNKNOWN",
        "accessibility_state": "UNKNOWN",
        "opening_hours_state": "UNKNOWN",
    }


def _current_projection(
    session: Session,
    journey: Journey,
    *,
    row: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
) -> dict[str, object] | None:
    if row.status not in HANDOFF_ACTIVE or str(viewer.id) not in {
        str(row.helper_member_id), str(row.requester_member_id)
    }:
        return None
    try:
        expires = datetime.fromisoformat(row.expires_at)
    except (TypeError, ValueError):
        return None
    now = _now()
    if expires.tzinfo is None or expires.astimezone(timezone.utc) <= now:
        return None
    try:
        signal, acknowledgement, members, activity, original, context_now = _load_help_context(
            session,
            journey,
            signal_id=str(row.signal_id),
            viewer=viewer,
        )
    except HTTPException:
        return None
    if (
        str(acknowledgement.id) != str(row.acknowledgement_id)
        or str(signal.journey_member_id) != str(row.requester_member_id)
        or str(acknowledgement.responder_member_id) != str(row.helper_member_id)
        or str(signal.traveler_id) != str(row.requester_traveler_id)
        or str(acknowledgement.responder_traveler_id) != str(row.helper_traveler_id)
        or str(journey.active_plan_version_id) != str(row.active_plan_version_id)
        or str(journey.current_activity_id or "") != str(row.current_activity_id or "")
        or str(activity.id) != str(row.original_rendezvous_activity_id)
        or str(original.id) != str(row.original_public_place_id)
        or _audience_json(members) != row.audience_member_ids_json
        or _sha256(_audience_json(members)) != row.audience_sha256
        or context_now >= expires.astimezone(timezone.utc)
    ):
        return None
    try:
        stored = json.loads(row.handoff_place_snapshot_json)
    except (TypeError, json.JSONDecodeError):
        return None
    current_candidate = next((
        candidate for candidate in _candidate_places(session, journey, original=original)
        if str(candidate["place_id"]) == str(row.handoff_place_id)
    ), None)
    if current_candidate is None or current_candidate != stored:
        return None
    return _payload(row, session, viewer_member_id=str(viewer.id))


def list_current_handoffs(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
) -> dict[str, object]:
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_SIGNED_ACTOR_MISMATCH")
    rows = session.scalars(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
        JourneyRendezvousBuddyHandoff.status.in_(tuple(HANDOFF_ACTIVE)),
        or_(
            JourneyRendezvousBuddyHandoff.helper_member_id == viewer.id,
            JourneyRendezvousBuddyHandoff.requester_member_id == viewer.id,
        ),
    ).order_by(JourneyRendezvousBuddyHandoff.offered_at, JourneyRendezvousBuddyHandoff.id)).all()
    items = []
    for row in rows:
        projected = _current_projection(session, journey, row=row, viewer=viewer)
        from app.execution.rendezvous_buddy_execution import (
            execution_for_current_viewer,
            execution_window_available_for_helper,
        )

        execution = execution_for_current_viewer(
            session, journey=journey, handoff=row, viewer=viewer
        )
        if projected is not None:
            projected["execution"] = execution
            projected["execution_window_offer_available"] = (
                execution is None
                and execution_window_available_for_helper(session, handoff=row, viewer=viewer)
            )
            items.append(projected)
        elif execution is not None:
            # A mutually consented finite execution lease survives the original
            # 20-minute R79 offer and R78 self-report expiry. Its projection is
            # still restricted to the two signed participants and current plan.
            projected = _payload(row, session, viewer_member_id=str(viewer.id))
            projected["execution"] = execution
            projected["execution_window_offer_available"] = False
            items.append(projected)
    return {"items": items}


def _offer_context(
    session: Session,
    *,
    journey: Journey,
    helper: JourneyMember,
    principal: TravelerPrincipal,
    help_signal_id: str,
    expected_plan_version_id: str,
    expected_current_activity_id: str | None,
    expected_rendezvous_activity_id: str,
    expected_original_public_place_id: str,
    expected_audience_sha256: str,
) -> tuple[JourneyRendezvousMemberSignal, JourneyRendezvousSignalAcknowledgement, list[JourneyMember], object, Place, datetime]:
    if str(helper.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_SIGNED_ACTOR_MISMATCH")
    journey = _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=expected_plan_version_id,
        current_activity_id=expected_current_activity_id,
    )
    signal, acknowledgement, members, activity, original, now = _load_help_context(
        session,
        journey,
        signal_id=help_signal_id,
        viewer=helper,
        require_helper=True,
    )
    if (
        str(journey.active_plan_version_id) != expected_plan_version_id
        or str(journey.current_activity_id or "") != str(expected_current_activity_id or "")
        or str(activity.id) != expected_rendezvous_activity_id
        or str(original.id) != expected_original_public_place_id
    ):
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONTEXT_CHANGED")
    actual_audience_sha = _sha256(_audience_json(members))
    if actual_audience_sha != expected_audience_sha256:
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_AUDIENCE_CHANGED")
    return signal, acknowledgement, members, activity, original, now


def offer_handoff(
    session: Session,
    *,
    journey: Journey,
    helper: JourneyMember,
    principal: TravelerPrincipal,
    help_signal_id: str,
    expected_plan_version_id: str,
    expected_current_activity_id: str | None,
    expected_rendezvous_activity_id: str,
    expected_original_public_place_id: str,
    expected_audience_sha256: str,
    handoff_place_id: str,
    execution_window_minutes: int | None,
    idempotency_key: str,
) -> dict[str, object]:
    key = str(idempotency_key or "").strip()
    if len(key) < 8 or len(key) > 128:
        raise HTTPException(400, "RENDEZVOUS_HANDOFF_IDEMPOTENCY_KEY_REQUIRED")
    if execution_window_minutes is not None and int(execution_window_minutes) not in {30, 60, 90}:
        raise HTTPException(422, "BUDDY_EXECUTION_WINDOW_UNSUPPORTED")
    signal, acknowledgement, members, activity, original, now = _offer_context(
        session,
        journey=journey,
        helper=helper,
        principal=principal,
        help_signal_id=help_signal_id,
        expected_plan_version_id=expected_plan_version_id,
        expected_current_activity_id=expected_current_activity_id,
        expected_rendezvous_activity_id=expected_rendezvous_activity_id,
        expected_original_public_place_id=expected_original_public_place_id,
        expected_audience_sha256=expected_audience_sha256,
    )
    candidate = next((item for item in _candidate_places(session, journey, original=original)
                      if str(item["place_id"]) == str(handoff_place_id)), None)
    if candidate is None:
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_PLACE_NOT_SERVER_SUGGESTED")
    audience_json = _audience_json(members)
    request_facts = {
        "signal_id": str(signal.id),
        "acknowledgement_id": str(acknowledgement.id),
        "plan_version_id": expected_plan_version_id,
        "current_activity_id": expected_current_activity_id,
        "rendezvous_activity_id": str(activity.id),
        "original_public_place_id": str(original.id),
        "handoff_place_id": str(handoff_place_id),
        "execution_window_minutes": int(execution_window_minutes) if execution_window_minutes is not None else None,
        "audience_sha256": expected_audience_sha256,
    }
    request_hash = _sha256(json.dumps(request_facts, sort_keys=True, separators=(",", ":")))
    prior = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
        JourneyRendezvousBuddyHandoff.helper_member_id == helper.id,
        JourneyRendezvousBuddyHandoff.idempotency_key == key,
    ))
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, "RENDEZVOUS_HANDOFF_IDEMPOTENCY_KEY_REUSED")
        projected = _current_projection(session, journey, row=prior, viewer=helper)
        if projected is not None:
            session.commit()
            return projected
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_IDEMPOTENCY_KEY_RETIRED")

    existing = session.scalars(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.signal_id == signal.id,
        JourneyRendezvousBuddyHandoff.status.in_(tuple(HANDOFF_ACTIVE)),
    )).all()
    for row in existing:
        status = _unavailable_status(row, now)
        if status == "STALE" and _current_projection(session, journey, row=row, viewer=helper) is not None:
            raise HTTPException(409, "RENDEZVOUS_HANDOFF_ALREADY_CURRENT")
        _retire_unavailable(
            session,
            row,
            journey_id=str(journey.id),
            actor_id=str(principal.traveler_id),
            now=now,
        )
    session.flush()
    row = JourneyRendezvousBuddyHandoff(
        id=str(uuid4()),
        journey_id=str(journey.id),
        signal_id=str(signal.id),
        acknowledgement_id=str(acknowledgement.id),
        requester_member_id=str(signal.journey_member_id),
        requester_traveler_id=str(signal.traveler_id),
        helper_member_id=str(helper.id),
        helper_traveler_id=str(principal.traveler_id),
        active_plan_version_id=str(journey.active_plan_version_id),
        current_activity_id=str(journey.current_activity_id) if journey.current_activity_id else None,
        original_rendezvous_activity_id=str(activity.id),
        original_public_place_id=str(original.id),
        handoff_place_id=str(handoff_place_id),
        handoff_place_snapshot_json=json.dumps(candidate, sort_keys=True, separators=(",", ":")),
        audience_member_ids_json=audience_json,
        audience_sha256=expected_audience_sha256,
        status="OFFERED",
        idempotency_key=key,
        request_hash=request_hash,
        offered_at=_iso(now),
        expires_at=_iso(now + HANDOFF_TTL),
        responded_at=None,
        closed_at=None,
        created_at=_iso(now),
        updated_at=_iso(now),
    )
    session.add(row)
    if execution_window_minutes is not None:
        from app.execution.rendezvous_buddy_execution import create_execution_window_offer_record

        create_execution_window_offer_record(
            session,
            handoff=row,
            window_minutes=int(execution_window_minutes),
            idempotency_key="initial-window-" + str(row.id),
            proposed_at=now,
        )
    record_audit(
        session,
        action="journey.rendezvous_buddy_handoff.offered",
        entity_type="JourneyRendezvousBuddyHandoff",
        entity_id=row.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={
            "signal_id": str(signal.id),
            "acknowledgement_id": str(acknowledgement.id),
            "requester_member_id": str(signal.journey_member_id),
            "helper_member_id": str(helper.id),
            "handoff_place_id": str(handoff_place_id),
            "source_kind": candidate["source_kind"],
            "evidence_grade": candidate["evidence_grade"],
            "expires_at": row.expires_at,
            "shared_plan_changed": False,
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        winner = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
            JourneyRendezvousBuddyHandoff.journey_id == str(journey.id),
            JourneyRendezvousBuddyHandoff.helper_member_id == str(helper.id),
            JourneyRendezvousBuddyHandoff.idempotency_key == key,
        ))
        if winner is not None and winner.request_hash == request_hash:
            projected = _current_projection(session, journey, row=winner, viewer=helper)
            if projected is not None:
                return projected
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONCURRENT_UPDATE_RETRY") from exc
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_WRITE_BUSY_RETRY") from exc
    return _payload(row, session, viewer_member_id=str(helper.id))


def _replay_retired_handoff(
    session: Session,
    *,
    journey: Journey,
    row: JourneyRendezvousBuddyHandoff,
    expected_status: str,
    member: JourneyMember,
    principal: TravelerPrincipal,
) -> dict[str, object]:
    """An old-tab retry cannot claim to close a newer pair appointment.

    Lock the same Journey row as offer/respond/close before reading any
    successor. A real retry stays idempotent while no newer agreement exists.
    """
    _lock_journey_for_withdraw(session, str(journey.id))
    current_member = session.get(JourneyMember, str(member.id))
    if (
        current_member is None
        or str(current_member.journey_id) != str(journey.id)
        or str(current_member.traveler_id) != str(principal.traveler_id)
        or not _member_in_force(current_member, _now())
    ):
        session.rollback()
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_MEMBER_NOT_IN_FORCE")
    current = session.get(JourneyRendezvousBuddyHandoff, str(row.id))
    if current is None or current.status != expected_status:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_RETIRED_REPLAY_STALE")
    newer = session.scalar(select(JourneyRendezvousBuddyHandoff.id).where(
        JourneyRendezvousBuddyHandoff.signal_id == current.signal_id,
        JourneyRendezvousBuddyHandoff.id != current.id,
        JourneyRendezvousBuddyHandoff.status.in_(tuple(HANDOFF_ACTIVE)),
    ))
    if newer is not None:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_RETIRED_REPLAY_STALE")
    session.commit()
    return {"handoff_id": str(current.id), "status": expected_status, "shared_plan_changed": False}


def respond_to_handoff(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
    decision: str,
) -> dict[str, object]:
    if decision not in {"ACCEPT", "DECLINE"}:
        raise HTTPException(422, "RENDEZVOUS_HANDOFF_DECISION_UNSUPPORTED")
    row = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == handoff_id,
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
    ))
    if row is None or str(row.requester_member_id) != str(viewer.id):
        raise HTTPException(404, "RENDEZVOUS_HANDOFF_NOT_FOUND")
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_SIGNED_ACTOR_MISMATCH")
    target_status = "ACCEPTED" if decision == "ACCEPT" else "DECLINED"
    if row.status == target_status:
        projected = _current_projection(session, journey, row=row, viewer=viewer)
        if projected is not None:
            return projected
        if row.status == "DECLINED":
            return _replay_retired_handoff(
                session, journey=journey, row=row, expected_status="DECLINED",
                member=viewer, principal=principal,
            )
    if row.status != "OFFERED":
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_OFFER_NOT_CURRENT")
    journey = _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=str(row.active_plan_version_id),
        current_activity_id=str(row.current_activity_id or "") or None,
    )
    row = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
    assert row is not None
    if _current_projection(session, journey, row=row, viewer=viewer) is None:
        status = _retire_unavailable(
            session,
            row,
            journey_id=str(journey.id),
            actor_id=str(principal.traveler_id),
            now=_now(),
        )
        try:
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONCURRENT_UPDATE_RETRY") from exc
        raise HTTPException(409, f"RENDEZVOUS_HANDOFF_{status}")
    now_dt = _now()
    now = _iso(now_dt)
    row.status = target_status
    row.responded_at = now
    row.closed_at = now if target_status == "DECLINED" else None
    row.updated_at = now
    if decision == "ACCEPT":
        from app.execution.rendezvous_buddy_execution import activate_execution_on_handoff_accept

        activate_execution_on_handoff_accept(
            session,
            handoff=row,
            requester=viewer,
            principal=principal,
            accepted_at=now_dt,
        )
    else:
        execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
            JourneyRendezvousBuddyExecution.handoff_id == row.id,
        ))
        if execution is not None and execution.status == "WINDOW_OFFERED":
            execution.status = "CANCELLED"
            execution.closed_at = now
            execution.outcome_code = "HANDOFF_DECLINED_BEFORE_EXECUTION"
            execution.updated_at = now
    record_audit(
        session,
        action="journey.rendezvous_buddy_handoff." + decision.casefold(),
        entity_type="JourneyRendezvousBuddyHandoff",
        entity_id=row.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={"decision": decision, "shared_plan_changed": False},
    )
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONCURRENT_UPDATE_RETRY") from exc
    return _payload(row, session, viewer_member_id=str(viewer.id))


def close_handoff(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
    action: str,
) -> dict[str, object]:
    row = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == handoff_id,
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
    ))
    if row is None or str(viewer.id) not in {str(row.helper_member_id), str(row.requester_member_id)}:
        raise HTTPException(404, "RENDEZVOUS_HANDOFF_NOT_FOUND")
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_SIGNED_ACTOR_MISMATCH")
    if action == "CANCEL" and str(viewer.id) != str(row.helper_member_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_HELPER_REQUIRED")
    if action == "REVOKE" and str(viewer.id) != str(row.requester_member_id):
        raise HTTPException(403, "RENDEZVOUS_HANDOFF_REQUESTER_REQUIRED")
    target_status = "CANCELLED" if action == "CANCEL" else "REVOKED"
    if row.status == target_status:
        return _replay_retired_handoff(
            session, journey=journey, row=row, expected_status=target_status,
            member=viewer, principal=principal,
        )
    if row.status not in HANDOFF_ACTIVE:
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_NOT_CURRENT")
    journey = _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=str(row.active_plan_version_id),
        current_activity_id=str(row.current_activity_id or "") or None,
    )
    row = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
    assert row is not None
    if _current_projection(session, journey, row=row, viewer=viewer) is None:
        now_dt = _now()
        stale_status = _retire_unavailable(
            session,
            row,
            journey_id=str(journey.id),
            actor_id=str(principal.traveler_id),
            now=now_dt,
        )
        try:
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONCURRENT_UPDATE_RETRY") from exc
        raise HTTPException(409, f"RENDEZVOUS_HANDOFF_{stale_status}")
    now = _iso(_now())
    row.status = target_status
    row.closed_at = now
    row.updated_at = now
    execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.handoff_id == row.id,
    ))
    if execution is not None and execution.status not in {
        "MUTUALLY_ACKNOWLEDGED", "REPLAN_REQUIRED", "CANCELLED", "EXPIRED", "STALE",
    }:
        execution.status = "CANCELLED"
        execution.closed_at = now
        execution.outcome_code = "HANDOFF_CLOSED_BY_PARTICIPANT"
        execution.updated_at = now
    record_audit(
        session,
        action="journey.rendezvous_buddy_handoff." + action.casefold(),
        entity_type="JourneyRendezvousBuddyHandoff",
        entity_id=row.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={"status": target_status, "shared_plan_changed": False},
    )
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_HANDOFF_CONCURRENT_UPDATE_RETRY") from exc
    return {"handoff_id": str(row.id), "status": target_status, "shared_plan_changed": False}
