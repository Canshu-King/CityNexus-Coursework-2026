from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Activity,
    Commitment,
    Journey,
    JourneyMember,
    LocationObservation,
    MemberPlaceInvitation,
    MemberPlaceInvitationProgress,
    MemberPlaceInvitationRecipient,
    MemberPlacePromotion,
    SharedStopCoPlanningGrant,
    MemberSideQuest,
    MemberSideQuestReturnReport,
    Place,
    JourneyMission,
    JourneyRoleCapability,
    PlanVersion,
    ReplanProposal,
    Traveler,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import (
    LOCATION_FRESHNESS_SECONDS,
    planned_separation_for_member,
    rendezvous_commitment_risk,
)

INVITATION_TTL_MINUTES = 15
INVITATION_FEASIBILITY_TTL_SECONDS = 120
OWNER_QUEST_STATES = {"READY", "EN_ROUTE", "ARRIVED"}
OPEN_QUEST_STATES = {"DESTINATION_UNRESOLVED", "READY", "EN_ROUTE", "ARRIVED"}
PROTECTED_COMMITMENT_LEVELS = {"C3_Guaranteed", "C4_Anchored"}
PROGRESS_CONSENT_VERSION = "R72_MICRO_GROUP_PROGRESS_V1"
AT_PLACE_SIGNAL = "AT_INVITED_PLACE_SELF_REPORTED"
HEADING_BACK_SIGNAL = "HEADING_BACK_SELF_REPORTED"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _feasibility_key() -> bytes:
    secret = settings.traveler_auth_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise HTTPException(503, "TRAVELER_PRINCIPAL_VERIFIER_NOT_CONFIGURED")
    return hmac.new(
        secret.encode("utf-8"),
        b"citynexus:member-place-invitation-feasibility:v1",
        hashlib.sha256,
    ).digest()


def _sign_feasibility(payload: dict[str, Any]) -> str:
    body = _b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(hmac.new(_feasibility_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _verify_feasibility(token: str) -> dict[str, Any]:
    try:
        body, signature = str(token).split(".", 1)
        expected = _b64url(hmac.new(_feasibility_key(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode((body + padding).encode("ascii")).decode("utf-8"))
    except HTTPException:
        raise
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_TOKEN_INVALID") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_TOKEN_INVALID")
    try:
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_TOKEN_INVALID") from exc
    if expires_at <= int(_now().timestamp()):
        raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_PREVIEW_EXPIRED")
    return payload


def _observation_fingerprint(observation: LocationObservation) -> str:
    material = {
        "id": str(observation.id),
        "journey_id": str(observation.journey_id),
        "traveler_id": str(observation.traveler_id or ""),
        "member_id": str(observation.journey_member_id or ""),
        "latitude": float(observation.latitude),
        "longitude": float(observation.longitude),
        "accuracy_m": float(observation.accuracy_m),
        "captured_at": str(observation.captured_at),
        "source": str(observation.source or ""),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _route_token_projection(route: dict[str, Any] | None, *, now: datetime) -> dict[str, Any] | None:
    if route is None:
        return None
    captured = _stamp(str(route.get("captured_at") or ""))
    return {
        "duration_min": int(route["duration_min"]),
        "provider": str(route.get("provider") or "unknown"),
        "provenance": str(route.get("provenance") or "unknown"),
        "captured_at": captured.isoformat() if captured else None,
        "age_seconds": max(0, int((now - captured).total_seconds())) if captured else None,
        "mode": str(route.get("mode") or "WALK"),
    }


def _route_capture_is_fresh(route: dict[str, Any] | None, *, now: datetime) -> bool:
    if route is None:
        return False
    captured = _stamp(str(route.get("captured_at") or ""))
    if captured is None:
        return False
    age_seconds = (now - captured).total_seconds()
    return -30 <= age_seconds <= LOCATION_FRESHNESS_SECONDS


def _feasibility_binding(
    *,
    journey: Journey,
    recipient: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient,
) -> dict[str, str]:
    owner_context_hash = hashlib.sha256(
        json.dumps(
            {
                "owner_split_intent_id": str(invitation.owner_split_intent_id),
                "owner_side_quest_id": str(invitation.owner_side_quest_id),
                "active_plan_version_id": str(invitation.active_plan_version_id),
                "place_id": str(invitation.place_id),
                "rendezvous_activity_id": str(invitation.rendezvous_activity_id),
                "rendezvous_place_id": str(invitation.rendezvous_place_id),
                "disclosure_snapshot_json": str(invitation.disclosure_snapshot_json or "{}"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "journey_id": str(journey.id),
        "invitation_id": str(invitation.id),
        "response_id": str(response.id),
        "recipient_member_id": str(recipient.id),
        "recipient_traveler_id": str(traveler_id),
        "owner_context_hash": owner_context_hash,
        "active_plan_version_id": str(invitation.active_plan_version_id),
        "place_id": str(invitation.place_id),
        "place_provider_key": str(invitation.place_provider_key),
        "place_external_id": str(invitation.place_external_id),
        "rendezvous_activity_id": str(invitation.rendezvous_activity_id),
        "rendezvous_place_id": str(invitation.rendezvous_place_id),
    }


def _feasibility_result(
    *,
    session: Session,
    journey: Journey,
    recipient: JourneyMember,
    invitation: MemberPlaceInvitation,
    target: Activity,
    observation: LocationObservation | None,
    outbound: dict[str, Any] | None,
    returning: dict[str, Any] | None,
    dwell_minutes: int,
    now: datetime,
    blocked_reason: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    deadline = _stamp(target.start_at)
    state = "UNKNOWN"
    reason = "QUALIFYING_ROUTE_EVIDENCE_UNAVAILABLE"
    duration = None
    slack = None
    commitment_risk: dict[str, Any] | None = None
    if blocked_reason:
        state = "BLOCKED"
        reason = blocked_reason
    elif observation is None:
        reason = "CURRENT_LOCATION_UNAVAILABLE"
    elif outbound is None or returning is None:
        reason = "QUALIFYING_LIVE_ROUTE_EVIDENCE_UNAVAILABLE"
    elif deadline is None:
        reason = "RENDEZVOUS_TIME_UNKNOWN"
    else:
        duration = int(outbound["duration_min"]) + int(dwell_minutes) + int(returning["duration_min"])
        side_departure = now + timedelta(minutes=int(outbound["duration_min"]) + int(dwell_minutes))
        commitment_risk = rendezvous_commitment_risk(
            session,
            journey,
            target,
            route_duration_min=int(returning["duration_min"]),
            authority="ADVISORY_RECIPIENT_PLACE_INVITATION_PREVIEW",
            now=side_departure,
        )
        next_commitment = commitment_risk.get("next_commitment") or {}
        if next_commitment.get("scheduled_at"):
            deadline = _stamp(str(next_commitment["scheduled_at"])) or deadline
        arrival = now + timedelta(minutes=duration)
        slack = int((deadline - arrival).total_seconds() // 60)
        risk_state = str(commitment_risk.get("state") or "")
        if risk_state == "COMMITMENT_AT_RISK":
            state = "BLOCKED"
            reason = "PROTECTED_RENDEZVOUS_BUFFER_WOULD_BE_BREACHED"
        elif risk_state in {"LOW_BUFFER", "COMMITMENT_TIME_UNKNOWN"}:
            state = "AT_RISK" if risk_state == "LOW_BUFFER" else "UNKNOWN"
            reason = "PROTECTED_RENDEZVOUS_BUFFER_IS_TIGHT" if state == "AT_RISK" else "RENDEZVOUS_TIME_UNKNOWN"
        elif slack < 0:
            state = "AT_RISK"
            reason = "ROUTE_EXCEEDS_RENDEZVOUS_DEADLINE"
        else:
            state = "CAN_MAKE_IT"
            reason = "GROUNDED_ROUTE_FITS_RENDEZVOUS"

    evidence_rows = [route for route in (outbound, returning) if route]
    evidence_times = [str(route.get("captured_at") or "") for route in evidence_rows]
    evidence_dt = [_stamp(value) for value in evidence_times]
    valid_evidence_dt = [value for value in evidence_dt if value is not None]
    evidence_at = min(valid_evidence_dt).isoformat() if valid_evidence_dt else None
    evidence_age = max(
        (
            max(0, int((now - captured).total_seconds()))
            for route in evidence_rows
            if (captured := _stamp(str(route.get("captured_at") or ""))) is not None
        ),
        default=None,
    )
    provider_names = sorted({str(route.get("provider") or "unknown") for route in evidence_rows})
    provenance = sorted({str(route.get("provenance") or "unknown") for route in evidence_rows})
    return {
        "state": state,
        "reason_code": reason,
        "observed_at": _iso(now),
        "evidence": {
            "captured_at": evidence_at,
            "age_seconds": evidence_age,
            "freshness_seconds": LOCATION_FRESHNESS_SECONDS,
            "source_type": provenance[0] if len(provenance) == 1 else ("MIXED" if provenance else None),
            "provider": provider_names[0] if len(provider_names) == 1 else ("MIXED" if provider_names else None),
            "location_source": str(observation.source) if observation is not None else None,
            "location_captured_at": _stamp(str(observation.captured_at or "")).isoformat()
            if observation is not None and _stamp(str(observation.captured_at or ""))
            else None,
            "location_accuracy_m": float(observation.accuracy_m) if observation is not None else None,
        },
        "rendezvous_deadline": deadline.isoformat() if deadline else None,
        "travel_duration_min": duration,
        "travel_duration_interval_min": None,
        "slack_min": slack,
        "outbound_duration_min": int(outbound["duration_min"]) if outbound else None,
        "return_duration_min": int(returning["duration_min"]) if returning else None,
        "dwell_minutes": int(dwell_minutes),
        "plan_unchanged": True,
        "physical_arrival_proof": False,
    }, commitment_risk.get("state") if commitment_risk else None


def preview_recipient_invitation_feasibility(
    session: Session,
    *,
    journey: Journey,
    recipient: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient,
    location_observation_id: str | None,
    dwell_minutes: int,
    requested_mode: str = "WALK",
    now: datetime | None = None,
) -> dict[str, Any]:
    if dwell_minutes not in {10, 20, 30}:
        raise HTTPException(422, "PLACE_INVITATION_DWELL_MINUTES_UNSUPPORTED")
    if requested_mode not in {"WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"}:
        raise HTTPException(422, "PLACE_INVITATION_ROUTE_MODE_UNSUPPORTED")
    current = _now(now)
    if response.state != "INVITED":
        raise HTTPException(409, "PLACE_INVITATION_ALREADY_RESPONDED")
    context = validate_invitation_for_acceptance(
        session,
        journey=journey,
        recipient=recipient,
        traveler_id=traveler_id,
        invitation=invitation,
        response=response,
        now=current,
    )
    conflict = recipient_conflict_reason(
        session,
        journey=journey,
        recipient=recipient,
        invitation=invitation,
        same_invitation_split_id=None,
        now=current,
    )
    observation: LocationObservation | None = None
    outbound = returning = None
    observation_error: str | None = None
    if location_observation_id:
        from app.execution.member_side_quest import (
            _capture_private_route_leg,
            _fresh_member_observation,
        )

        try:
            observation = _fresh_member_observation(
                session,
                journey=journey,
                member=recipient,
                traveler_id=traveler_id,
                observation_id=str(location_observation_id),
            )
        except HTTPException as exc:
            if exc.status_code == 404 or exc.status_code == 403:
                raise
            observation_error = str(exc.detail)
        if observation is not None and conflict is None:
            outbound = _capture_private_route_leg(
                session,
                journey=journey,
                origin_key=f"location-observation:{observation.id}",
                target_key=str(context["place"].id),
                requested_mode=requested_mode,
            )
            returning = _capture_private_route_leg(
                session,
                journey=journey,
                origin_key=str(context["place"].id),
                target_key=str(context["rendezvous"].id),
                requested_mode=requested_mode,
            )
            if not _route_capture_is_fresh(outbound, now=current):
                outbound = None
            if not _route_capture_is_fresh(returning, now=current):
                returning = None
    # Route acquisition can cross an external-provider call. Recheck the live
    # invitation and recipient conflict state before issuing a usable preview.
    context = validate_invitation_for_acceptance(
        session,
        journey=journey,
        recipient=recipient,
        traveler_id=traveler_id,
        invitation=invitation,
        response=response,
        now=current,
    )
    conflict = recipient_conflict_reason(
        session,
        journey=journey,
        recipient=recipient,
        invitation=invitation,
        same_invitation_split_id=None,
        now=current,
    )
    result, _risk_state = _feasibility_result(
        session=session,
        journey=journey,
        recipient=recipient,
        invitation=invitation,
        target=context["target"],
        observation=observation,
        outbound=outbound,
        returning=returning,
        dwell_minutes=dwell_minutes,
        now=current,
        blocked_reason=conflict,
    )
    if observation_error:
        result["reason_code"] = observation_error
    token_payload = {
        "v": 1,
        "iat": int(current.timestamp()),
        "exp": int((current + timedelta(seconds=INVITATION_FEASIBILITY_TTL_SECONDS)).timestamp()),
        "binding": _feasibility_binding(
            journey=journey,
            recipient=recipient,
            traveler_id=traveler_id,
            invitation=invitation,
            response=response,
        ),
        "observation_id": str(observation.id) if observation is not None else None,
        "observation_fingerprint": _observation_fingerprint(observation) if observation is not None else None,
        "dwell_minutes": dwell_minutes,
        "requested_mode": requested_mode,
        "state": result["state"],
        "outbound": _route_token_projection(outbound, now=current),
        "returning": _route_token_projection(returning, now=current),
    }
    result["feasibility_token"] = _sign_feasibility(token_payload)
    result["expires_in_seconds"] = INVITATION_FEASIBILITY_TTL_SECONDS
    return result


def recheck_recipient_invitation_feasibility(
    session: Session,
    *,
    journey: Journey,
    recipient: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient,
    token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    payload = _verify_feasibility(token)
    expected_binding = _feasibility_binding(
        journey=journey,
        recipient=recipient,
        traveler_id=traveler_id,
        invitation=invitation,
        response=response,
    )
    if payload.get("binding") != expected_binding:
        raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_BINDING_STALE")
    observation = None
    observation_id = payload.get("observation_id")
    if observation_id:
        observation = session.get(LocationObservation, str(observation_id))
        if (
            observation is None
            or str(observation.journey_id) != str(journey.id)
            or str(observation.traveler_id or "") != str(traveler_id)
            or str(observation.journey_member_id or "") != str(recipient.id)
            or _observation_fingerprint(observation) != str(payload.get("observation_fingerprint") or "")
        ):
            raise HTTPException(409, "PLACE_INVITATION_FEASIBILITY_EVIDENCE_CHANGED")
        from app.execution.member_side_quest import _fresh_member_observation

        try:
            observation = _fresh_member_observation(
                session,
                journey=journey,
                member=recipient,
                traveler_id=traveler_id,
                observation_id=str(observation.id),
            )
        except HTTPException:
            observation = None
    context = validate_invitation_for_acceptance(
        session,
        journey=journey,
        recipient=recipient,
        traveler_id=traveler_id,
        invitation=invitation,
        response=response,
        now=current,
    )
    conflict = recipient_conflict_reason(
        session,
        journey=journey,
        recipient=recipient,
        invitation=invitation,
        same_invitation_split_id=None,
        now=current,
    )
    outbound = payload.get("outbound") if isinstance(payload.get("outbound"), dict) else None
    returning = payload.get("returning") if isinstance(payload.get("returning"), dict) else None
    # Provider evidence is separately time-bounded; a fresh observation cannot
    # make an old route true again.
    for route in (outbound, returning):
        captured = _stamp(str(route.get("captured_at") or "")) if route else None
        age = (current - captured).total_seconds() if captured else LOCATION_FRESHNESS_SECONDS + 1
        if captured is None or age < -30 or age > LOCATION_FRESHNESS_SECONDS:
            outbound = returning = None
            break
    result, _risk_state = _feasibility_result(
        session=session,
        journey=journey,
        recipient=recipient,
        invitation=invitation,
        target=context["target"],
        observation=observation,
        outbound=outbound,
        returning=returning,
        dwell_minutes=int(payload.get("dwell_minutes") or 20),
        now=current,
        blocked_reason=conflict,
    )
    return result


def _now(value: datetime | None = None) -> datetime:
    result = value or execution_clock.now()
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat()


def _stamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _member_in_force(member: JourneyMember | None, now: datetime) -> bool:
    if member is None or member.removed_at:
        return False
    if member.expires_at is None:
        return True
    expiry = _stamp(member.expires_at)
    return expiry is not None and expiry > now


def _current_members(session: Session, journey_id: str, now: datetime) -> list[JourneyMember]:
    rows = session.scalars(
        select(JourneyMember).where(JourneyMember.journey_id == journey_id)
    ).all()
    return [row for row in rows if _member_in_force(row, now)]


def _place_payload(place: Place) -> dict[str, Any]:
    provider = str(place.provider_key or "unknown")
    source = "OpenStreetMap" if provider.casefold() in {"osm", "openstreetmap"} else provider
    return {
        "place_id": str(place.id),
        "name": str(place.name),
        "category": str(place.category or "Place"),
        "source": source,
        "latitude": float(place.latitude),
        "longitude": float(place.longitude),
        "coordinate_system": str(place.coordinate_system or ""),
    }


def _disclosure_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "place": _place_payload(context["place"]),
        "rendezvous": {
            "activity_id": str(context["target"].id),
            "title": str(context["target"].title),
            "place_name": str(context["rendezvous"].name),
            "scheduled_at": context["target"].start_at,
        },
    }


def _snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _require_shareable_place(place: Place | None, journey: Journey) -> Place:
    if place is None or str(place.city_code) != str(journey.city_code):
        raise HTTPException(409, "PLACE_INVITATION_TARGET_STALE")
    if str(place.coordinate_system or "").upper() != "WGS84":
        raise HTTPException(409, "PLACE_INVITATION_TARGET_COORDINATES_UNAVAILABLE")
    try:
        latitude = float(place.latitude)
        longitude = float(place.longitude)
    except (TypeError, ValueError):
        raise HTTPException(409, "PLACE_INVITATION_TARGET_COORDINATES_UNAVAILABLE")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise HTTPException(409, "PLACE_INVITATION_TARGET_COORDINATES_UNAVAILABLE")
    return place


def resolve_owner_place_context(
    session: Session,
    *,
    journey: Journey,
    owner: JourneyMember,
    traveler_id: str,
    side_quest_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    quest = session.get(MemberSideQuest, str(side_quest_id))
    if (
        quest is None
        or str(quest.journey_id) != str(journey.id)
        or str(quest.member_id) != str(owner.id)
        or str(quest.traveler_id) != str(traveler_id)
    ):
        raise HTTPException(404, "PLACE_INVITATION_SIDE_QUEST_NOT_FOUND")
    if str(journey.status or "").upper() != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "PLACE_INVITATION_JOURNEY_NOT_ACTIVE")
    split = planned_separation_for_member(session, journey, owner, now=current)
    if (
        split is None
        or str(split["intent"].id) != str(quest.planned_separation_intent_id)
        or str(quest.active_plan_version_id) != str(journey.active_plan_version_id)
        or str(quest.rendezvous_activity_id) != str(split["target"].id)
        or str(quest.rendezvous_place_id) != str(split["place"].id)
    ):
        raise HTTPException(409, "PLACE_INVITATION_OWNER_SPLIT_STALE")
    if (
        str(quest.destination_authority) != "EXACT_KNOWN_PLACE"
        or str(quest.state) not in OWNER_QUEST_STATES
        or not quest.side_place_id
    ):
        raise HTTPException(409, "PLACE_INVITATION_REQUIRES_CURRENT_EXACT_STOP")
    place = _require_shareable_place(session.get(Place, str(quest.side_place_id)), journey)
    if str(place.id) != str(quest.side_place_id):
        raise HTTPException(409, "PLACE_INVITATION_TARGET_STALE")
    target = split["target"]
    rendezvous = split["place"]
    if (
        str(target.plan_version_id) != str(journey.active_plan_version_id)
        or str(target.status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
    ):
        raise HTTPException(409, "PLACE_INVITATION_RENDEZVOUS_STALE")
    return {"quest": quest, "split": split, "place": place, "target": target, "rendezvous": rendezvous}


def invitation_preview(
    session: Session,
    *,
    journey: Journey,
    owner: JourneyMember,
    traveler_id: str,
    side_quest_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    context = resolve_owner_place_context(
        session,
        journey=journey,
        owner=owner,
        traveler_id=traveler_id,
        side_quest_id=side_quest_id,
        now=current,
    )
    companions = [row for row in _current_members(session, journey.id, current) if str(row.id) != str(owner.id)]
    audience_snapshot = _audience_snapshot(
        journey=journey,
        owner=owner,
        context=context,
        companions=companions,
    )
    disclosure_snapshot = _disclosure_snapshot(context)
    return {
        "side_quest_id": str(context["quest"].id),
        "place": disclosure_snapshot["place"],
        "rendezvous": disclosure_snapshot["rendezvous"],
        "recipient_count": len(companions),
        "audience_snapshot": audience_snapshot,
        "expires_in_minutes": INVITATION_TTL_MINUTES,
        "disclosure": (
            "This shares the selected public Place name and exact public pin with current Journey companions. "
            "It does not share your device location, original search coordinates, private note, route, or browsing history."
        ),
    }


def _audience_snapshot(
    *,
    journey: Journey,
    owner: JourneyMember,
    context: dict[str, Any],
    companions: list[JourneyMember],
) -> str:
    material = {
        "journey_id": str(journey.id),
        "owner_member_id": str(owner.id),
        "owner_split_intent_id": str(context["split"]["intent"].id),
        "owner_side_quest_id": str(context["quest"].id),
        "active_plan_version_id": str(journey.active_plan_version_id),
        "place_id": str(context["place"].id),
        "place_provider_key": str(context["place"].provider_key),
        "place_external_id": str(context["place"].external_id),
        "rendezvous_activity_id": str(context["target"].id),
        "rendezvous_place_id": str(context["rendezvous"].id),
        "disclosure_snapshot": _disclosure_snapshot(context),
        "recipient_member_ids": sorted(str(row.id) for row in companions),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def create_invitation(
    session: Session,
    *,
    journey: Journey,
    owner: JourneyMember,
    traveler_id: str,
    side_quest_id: str,
    expected_audience_snapshot: str,
    now: datetime | None = None,
) -> MemberPlaceInvitation:
    current = _now(now)
    context = resolve_owner_place_context(
        session,
        journey=journey,
        owner=owner,
        traveler_id=traveler_id,
        side_quest_id=side_quest_id,
        now=current,
    )
    companions = [row for row in _current_members(session, journey.id, current) if str(row.id) != str(owner.id)]
    if not companions:
        raise HTTPException(409, "PLACE_INVITATION_NO_COMPANIONS")
    current_audience_snapshot = _audience_snapshot(
        journey=journey,
        owner=owner,
        context=context,
        companions=companions,
    )
    if current_audience_snapshot != str(expected_audience_snapshot):
        raise HTTPException(409, "PLACE_INVITATION_AUDIENCE_CHANGED")
    existing = session.scalar(
        select(MemberPlaceInvitation).where(
            MemberPlaceInvitation.journey_id == journey.id,
            MemberPlaceInvitation.owner_side_quest_id == context["quest"].id,
            MemberPlaceInvitation.state == "OPEN",
        )
    )
    if existing is not None:
        if (_stamp(existing.expires_at) or datetime.min.replace(tzinfo=timezone.utc)) > current:
            same_binding = (
                str(existing.owner_member_id) == str(owner.id)
                and str(existing.owner_split_intent_id) == str(context["split"]["intent"].id)
                and str(existing.active_plan_version_id) == str(journey.active_plan_version_id)
                and str(existing.place_id) == str(context["place"].id)
                and str(existing.place_provider_key) == str(context["place"].provider_key)
                and str(existing.place_external_id) == str(context["place"].external_id)
                and str(existing.rendezvous_activity_id) == str(context["target"].id)
                and str(existing.rendezvous_place_id) == str(context["rendezvous"].id)
                and str(existing.disclosure_snapshot_json or "{}")
                == _snapshot_json(_disclosure_snapshot(context))
            )
            if same_binding:
                existing_recipient_ids = set(session.scalars(
                    select(MemberPlaceInvitationRecipient.recipient_member_id).where(
                        MemberPlaceInvitationRecipient.invitation_id == existing.id
                    )
                ).all())
                current_recipient_ids = {str(member.id) for member in companions}
                if existing_recipient_ids.issubset(current_recipient_ids):
                    stamp = _iso(current)
                    for member in companions:
                        if str(member.id) in existing_recipient_ids:
                            continue
                        session.add(MemberPlaceInvitationRecipient(
                            id=str(uuid4()),
                            invitation_id=existing.id,
                            recipient_member_id=str(member.id),
                            recipient_traveler_id=str(member.traveler_id),
                            state="INVITED",
                            invited_at=stamp,
                            updated_at=stamp,
                        ))
                    session.commit()
                    session.refresh(existing)
                    return existing
                # A removed/expired addressed member makes the old invitation
                # stale. A fresh invitation can still be issued after the owner
                # reviews and confirms the new audience snapshot.
                existing.state = "STALE"
                existing.updated_at = _iso(current)
                session.flush()
            existing.state = "STALE"
            existing.updated_at = _iso(current)
        else:
            existing.state = "EXPIRED"
            existing.updated_at = _iso(current)
        session.flush()

    stamp = _iso(current)
    invitation = MemberPlaceInvitation(
        id=str(uuid4()),
        journey_id=str(journey.id),
        owner_member_id=str(owner.id),
        owner_traveler_id=str(traveler_id),
        owner_split_intent_id=str(context["split"]["intent"].id),
        owner_side_quest_id=str(context["quest"].id),
        active_plan_version_id=str(journey.active_plan_version_id),
        place_id=str(context["place"].id),
        place_provider_key=str(context["place"].provider_key),
        place_external_id=str(context["place"].external_id),
        rendezvous_activity_id=str(context["target"].id),
        rendezvous_place_id=str(context["rendezvous"].id),
        disclosure_snapshot_json=_snapshot_json(_disclosure_snapshot(context)),
        state="OPEN",
        created_at=stamp,
        expires_at=_iso(current + timedelta(minutes=INVITATION_TTL_MINUTES)),
        updated_at=stamp,
    )
    session.add(invitation)
    session.flush()
    for member in companions:
        session.add(
            MemberPlaceInvitationRecipient(
                id=str(uuid4()),
                invitation_id=invitation.id,
                recipient_member_id=str(member.id),
                recipient_traveler_id=str(member.traveler_id),
                state="INVITED",
                invited_at=stamp,
                updated_at=stamp,
            )
        )
    session.commit()
    session.refresh(invitation)
    return invitation


def _invitation_context_is_current(
    session: Session,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    *,
    now: datetime,
) -> bool:
    if (
        str(invitation.journey_id) != str(journey.id)
        or str(journey.status or "").upper() != "ACTIVE"
        or str(invitation.active_plan_version_id) != str(journey.active_plan_version_id or "")
    ):
        return False
    owner = session.get(JourneyMember, str(invitation.owner_member_id))
    if (
        not _member_in_force(owner, now)
        or owner is None
        or str(owner.traveler_id) != str(invitation.owner_traveler_id)
    ):
        return False
    try:
        context = resolve_owner_place_context(
            session,
            journey=journey,
            owner=owner,
            traveler_id=str(invitation.owner_traveler_id),
            side_quest_id=str(invitation.owner_side_quest_id),
            now=now,
        )
    except HTTPException:
        return False
    place = context["place"]
    addressed_recipients = session.scalars(
        select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation.id
        )
    ).all()
    if not addressed_recipients:
        return False
    for response in addressed_recipients:
        addressed_member = session.get(JourneyMember, str(response.recipient_member_id))
        if (
            not _member_in_force(addressed_member, now)
            or addressed_member is None
            or str(addressed_member.traveler_id) != str(response.recipient_traveler_id)
        ):
            return False
    try:
        saved_snapshot = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        str(context["split"]["intent"].id) == str(invitation.owner_split_intent_id)
        and str(context["target"].id) == str(invitation.rendezvous_activity_id)
        and str(context["rendezvous"].id) == str(invitation.rendezvous_place_id)
        and str(place.id) == str(invitation.place_id)
        and str(place.provider_key) == str(invitation.place_provider_key)
        and str(place.external_id) == str(invitation.place_external_id)
        and saved_snapshot == _disclosure_snapshot(context)
    )


def get_recipient_invitation(
    session: Session,
    *,
    journey: Journey,
    recipient: JourneyMember,
    traveler_id: str,
    invitation_id: str,
) -> tuple[MemberPlaceInvitation, MemberPlaceInvitationRecipient]:
    invitation = session.scalar(
        select(MemberPlaceInvitation).where(
            MemberPlaceInvitation.id == str(invitation_id),
            MemberPlaceInvitation.journey_id == str(journey.id),
        ).with_for_update()
    )
    if invitation is None:
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    response = session.scalar(
        select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation.id,
            MemberPlaceInvitationRecipient.recipient_member_id == str(recipient.id),
            MemberPlaceInvitationRecipient.recipient_traveler_id == str(traveler_id),
        ).with_for_update()
    )
    if response is None:
        # A guessed invitation ID must not reveal whether the Place exists.
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    return invitation, response


def validate_invitation_for_acceptance(
    session: Session,
    *,
    journey: Journey,
    recipient: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    if not _member_in_force(recipient, current) or str(recipient.traveler_id) != str(traveler_id):
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    continuing_accepted_response = response.state == "ACCEPTANCE_NEEDS_COMPLETION"
    if not continuing_accepted_response:
        # Withdrawal, expiry, or a changed source context can stop a new accept,
        # but cannot revoke a response that was durably accepted already.
        if invitation.state != "OPEN":
            raise HTTPException(409, f"PLACE_INVITATION_{invitation.state}")
        if (_stamp(invitation.expires_at) or datetime.min.replace(tzinfo=timezone.utc)) <= current:
            invitation.state = "EXPIRED"
            invitation.updated_at = _iso(current)
            session.commit()
            raise HTTPException(409, "PLACE_INVITATION_EXPIRED")
        if not _invitation_context_is_current(session, journey, invitation, now=current):
            invitation.state = "STALE"
            invitation.updated_at = _iso(current)
            session.commit()
            raise HTTPException(409, "PLACE_INVITATION_STALE")
    elif str(journey.active_plan_version_id or "") != str(invitation.active_plan_version_id):
        raise HTTPException(409, "PLACE_INVITATION_RENDEZVOUS_STALE")
    place = _require_shareable_place(session.get(Place, str(invitation.place_id)), journey)
    target = session.get(Activity, str(invitation.rendezvous_activity_id))
    rendezvous = session.get(Place, str(invitation.rendezvous_place_id))
    if (
        target is None
        or rendezvous is None
        or str(target.plan_version_id) != str(invitation.active_plan_version_id)
        or str(target.place_id or "") != str(invitation.rendezvous_place_id)
        or str(target.status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
    ):
        raise HTTPException(409, "PLACE_INVITATION_RENDEZVOUS_STALE")
    return {"place": place, "target": target, "rendezvous": rendezvous}


def recipient_conflict_reason(
    session: Session,
    *,
    journey: Journey,
    recipient: JourneyMember,
    invitation: MemberPlaceInvitation,
    same_invitation_split_id: str | None,
    now: datetime | None = None,
) -> str | None:
    """Return a recoverable block without overwriting the recipient's current trip."""
    current = _now(now)
    split = planned_separation_for_member(session, journey, recipient, now=current)
    if split is not None and str(split["intent"].id) != str(same_invitation_split_id or ""):
        return "INVITATION_RECIPIENT_HAS_ACTIVE_SPLIT"
    open_quest = session.scalar(
        select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.member_id == recipient.id,
            MemberSideQuest.state.in_(tuple(OPEN_QUEST_STATES)),
        ).order_by(MemberSideQuest.created_at.desc(), MemberSideQuest.id.desc())
    )
    if open_quest is not None:
        belongs_to_invitation = (
            str(open_quest.planned_separation_intent_id) == str(same_invitation_split_id or "")
            and str(open_quest.side_place_id or "") == str(invitation.place_id)
        )
        if not belongs_to_invitation:
            return "INVITATION_RECIPIENT_HAS_ACTIVE_SIDE_QUEST"

    rendezvous = session.get(Activity, str(invitation.rendezvous_activity_id))
    if rendezvous is None:
        return "PLACE_INVITATION_RENDEZVOUS_STALE"
    rendezvous_at = _stamp(rendezvous.start_at)
    for commitment in session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level.in_(tuple(PROTECTED_COMMITMENT_LEVELS)),
        )
    ).all():
        if str(commitment.activity_id or "") == str(rendezvous.id):
            continue
        activity = session.get(Activity, str(commitment.activity_id or ""))
        if (
            activity is None
            or str(activity.plan_version_id) != str(journey.active_plan_version_id or "")
            or str(activity.status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
        ):
            continue
        at = _stamp(activity.start_at)
        if at is not None and at > current and (rendezvous_at is None or at <= rendezvous_at):
            return "INVITATION_RECIPIENT_PROTECTED_COMMITMENT"
    return None


def _authorized_invitation_participant(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    invitation_id: str,
    now: datetime,
) -> tuple[MemberPlaceInvitation, MemberPlaceInvitationRecipient | None, str]:
    invitation = session.scalar(
        select(MemberPlaceInvitation).where(
            MemberPlaceInvitation.id == str(invitation_id),
            MemberPlaceInvitation.journey_id == str(journey.id),
        )
    )
    if invitation is None or not _member_in_force(member, now):
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    if (
        str(invitation.owner_member_id) == str(member.id)
        and str(invitation.owner_traveler_id) == str(traveler_id)
        and str(member.traveler_id) == str(traveler_id)
    ):
        return invitation, None, "INVITER"
    response = session.scalar(
        select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation.id,
            MemberPlaceInvitationRecipient.recipient_member_id == str(member.id),
            MemberPlaceInvitationRecipient.recipient_traveler_id == str(traveler_id),
        )
    )
    if response is None or response.state != "ACCEPTED_OWN_STOP_READY":
        # Pending, declined, and guessed IDs all get the same non-disclosing result.
        raise HTTPException(404, "PLACE_INVITATION_NOT_FOUND")
    return invitation, response, "JOINED"


def _validated_invitation_progress_binding(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient | None,
    role: str,
    signal_type: str,
    now: datetime,
) -> tuple[MemberSideQuest, str]:
    if (
        str(journey.status or "").upper() != "ACTIVE"
        or not journey.active_plan_version_id
        or str(journey.active_plan_version_id) != str(invitation.active_plan_version_id)
    ):
        raise HTTPException(409, "PLACE_INVITATION_PROGRESS_PLAN_CHANGED")

    if role == "INVITER":
        expected_quest_id = str(invitation.owner_side_quest_id)
        expected_split_id = str(invitation.owner_split_intent_id)
    else:
        if response is None or not response.accepted_side_quest_id or not response.accepted_split_intent_id:
            raise HTTPException(409, "PLACE_INVITATION_PROGRESS_ACCEPTED_STOP_UNAVAILABLE")
        expected_quest_id = str(response.accepted_side_quest_id)
        expected_split_id = str(response.accepted_split_intent_id)

    from app.execution.member_side_quest import _member_side_quest_for_action

    allowed_states = (
        frozenset({"ARRIVED"})
        if signal_type == AT_PLACE_SIGNAL
        else frozenset({"COMPLETED", "CANCELLED"})
    )
    quest, split, place = _member_side_quest_for_action(
        session,
        journey=journey,
        member=member,
        traveler_id=str(traveler_id),
        quest_id=expected_quest_id,
        allowed_states=allowed_states,
    )
    if (
        str(quest.planned_separation_intent_id) != expected_split_id
        or str(split["intent"].id) != expected_split_id
        or str(quest.active_plan_version_id) != str(invitation.active_plan_version_id)
        or str(quest.side_place_id or "") != str(invitation.place_id)
        or str(place.id) != str(invitation.place_id)
        or str(quest.rendezvous_activity_id) != str(invitation.rendezvous_activity_id)
        or str(quest.rendezvous_place_id) != str(invitation.rendezvous_place_id)
    ):
        raise HTTPException(409, "PLACE_INVITATION_PROGRESS_BINDING_CHANGED")

    # Once the recipient ACCEPTED, their R63/R64 trip is independently owned.
    # The inviter may finish or end their own split first. Requiring the
    # inviter's still-active quest here wrongly strands a joined traveller
    # despite that traveller's own binding having just passed validation.
    # Do not weaken the JOINED membership, exact invitation, recipient quest,
    # PlanVersion, or own split checks above.

    if signal_type == HEADING_BACK_SIGNAL:
        receipt = session.scalar(
            select(MemberSideQuestReturnReport).where(
                MemberSideQuestReturnReport.journey_id == journey.id,
                MemberSideQuestReturnReport.member_id == member.id,
                MemberSideQuestReturnReport.traveler_id == str(traveler_id),
                MemberSideQuestReturnReport.side_quest_id == quest.id,
                MemberSideQuestReturnReport.planned_separation_intent_id == expected_split_id,
                MemberSideQuestReturnReport.active_plan_version_id == invitation.active_plan_version_id,
                MemberSideQuestReturnReport.status == "RETURN_STARTED_REPORTED",
            )
        )
        if receipt is None:
            raise HTTPException(409, "PLACE_INVITATION_PROGRESS_HEAD_BACK_REPORT_REQUIRED")
    return quest, expected_split_id


def update_member_place_invitation_progress(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    invitation_id: str,
    action: str,
    share_audience_token: str | None = None,
    now: datetime | None = None,
) -> MemberPlaceInvitationProgress | None:
    current = _now(now)
    invitation, response, role = _authorized_invitation_participant(
        session,
        journey=journey,
        member=member,
        traveler_id=str(traveler_id),
        invitation_id=str(invitation_id),
        now=current,
    )
    actions = {
        "SHARE_ARRIVAL": (AT_PLACE_SIGNAL, True),
        "WITHDRAW_ARRIVAL": (AT_PLACE_SIGNAL, False),
        "SHARE_HEADING_BACK": (HEADING_BACK_SIGNAL, True),
        "WITHDRAW_HEADING_BACK": (HEADING_BACK_SIGNAL, False),
    }
    selected = actions.get(str(action))
    if selected is None:
        raise HTTPException(422, "PLACE_INVITATION_PROGRESS_ACTION_INVALID")
    signal_type, sharing = selected
    if sharing:
        recipients = session.scalars(
            select(MemberPlaceInvitationRecipient).where(
                MemberPlaceInvitationRecipient.invitation_id == invitation.id
            )
        ).all()
        participants = _invitation_progress_participants(
            session,
            invitation=invitation,
            recipients=recipients,
            now=current,
        )
        expected_audience_token = _progress_audience_token(invitation, participants)
        if (
            not share_audience_token
            or not hmac.compare_digest(str(share_audience_token), expected_audience_token)
        ):
            raise HTTPException(409, "PLACE_INVITATION_PROGRESS_AUDIENCE_CHANGED")
    existing = session.scalar(
        select(MemberPlaceInvitationProgress).where(
            MemberPlaceInvitationProgress.invitation_id == invitation.id,
            MemberPlaceInvitationProgress.member_id == member.id,
            MemberPlaceInvitationProgress.signal_type == signal_type,
        )
    )

    if not sharing:
        if existing is None or existing.status == "WITHDRAWN":
            return existing
        stamp = _iso(current)
        changed = session.execute(
            update(MemberPlaceInvitationProgress)
            .where(
                MemberPlaceInvitationProgress.id == existing.id,
                MemberPlaceInvitationProgress.status == "ACTIVE",
            )
            .values(status="WITHDRAWN", withdrawn_at=stamp, updated_at=stamp)
        )
        session.commit()
        return session.get(MemberPlaceInvitationProgress, existing.id) if changed.rowcount else session.scalar(
            select(MemberPlaceInvitationProgress).where(
                MemberPlaceInvitationProgress.invitation_id == invitation.id,
                MemberPlaceInvitationProgress.member_id == member.id,
                MemberPlaceInvitationProgress.signal_type == signal_type,
            )
        )

    quest, split_id = _validated_invitation_progress_binding(
        session,
        journey=journey,
        member=member,
        traveler_id=str(traveler_id),
        invitation=invitation,
        response=response,
        role=role,
        signal_type=signal_type,
        now=current,
    )
    if existing is not None and existing.status == "ACTIVE":
        return existing

    stamp = _iso(current)
    values = {
        "journey_id": str(journey.id),
        "invitation_id": str(invitation.id),
        "member_id": str(member.id),
        "traveler_id": str(traveler_id),
        "signal_type": signal_type,
        "active_plan_version_id": str(invitation.active_plan_version_id),
        "planned_separation_intent_id": split_id,
        "side_quest_id": str(quest.id),
        "place_id": str(invitation.place_id),
        "consent_version": PROGRESS_CONSENT_VERSION,
        "status": "ACTIVE",
        "reported_at": stamp,
        "withdrawn_at": None,
        "updated_at": stamp,
    }
    if existing is not None:
        changed = session.execute(
            update(MemberPlaceInvitationProgress)
            .where(
                MemberPlaceInvitationProgress.id == existing.id,
                MemberPlaceInvitationProgress.status == "WITHDRAWN",
            )
            .values(**{key: value for key, value in values.items() if key != "created_at"})
        )
        if changed.rowcount == 1:
            session.commit()
            return session.get(MemberPlaceInvitationProgress, existing.id)
        session.rollback()
    else:
        session.add(MemberPlaceInvitationProgress(id=str(uuid4()), created_at=stamp, **values))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()

    winner = session.scalar(
        select(MemberPlaceInvitationProgress).where(
            MemberPlaceInvitationProgress.invitation_id == invitation.id,
            MemberPlaceInvitationProgress.member_id == member.id,
            MemberPlaceInvitationProgress.signal_type == signal_type,
        )
    )
    if winner is None:
        raise HTTPException(409, "PLACE_INVITATION_PROGRESS_WRITE_CONFLICT")
    return winner


def _progress_can_be_shared(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    invitation: MemberPlaceInvitation,
    response: MemberPlaceInvitationRecipient | None,
    role: str,
    signal_type: str,
    now: datetime,
) -> bool:
    try:
        _validated_invitation_progress_binding(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            invitation=invitation,
            response=response,
            role=role,
            signal_type=signal_type,
            now=now,
        )
    except HTTPException:
        return False
    return True


def _invitation_progress_participants(
    session: Session,
    *,
    invitation: MemberPlaceInvitation,
    recipients: list[MemberPlaceInvitationRecipient],
    now: datetime,
) -> list[tuple[JourneyMember, Traveler | None, MemberPlaceInvitationRecipient | None, str]]:
    participants: list[tuple[JourneyMember, Traveler | None, MemberPlaceInvitationRecipient | None, str]] = []
    owner = session.get(JourneyMember, str(invitation.owner_member_id))
    owner_traveler = session.get(Traveler, str(invitation.owner_traveler_id))
    if (
        owner is not None
        and _member_in_force(owner, now)
        and str(owner.traveler_id) == str(invitation.owner_traveler_id)
    ):
        participants.append((owner, owner_traveler, None, "INVITER"))
    for response in recipients:
        if response.state != "ACCEPTED_OWN_STOP_READY":
            continue
        member = session.get(JourneyMember, str(response.recipient_member_id))
        traveler = session.get(Traveler, str(response.recipient_traveler_id))
        if (
            member is not None
            and _member_in_force(member, now)
            and str(member.traveler_id) == str(response.recipient_traveler_id)
        ):
            participants.append((member, traveler, response, "JOINED"))
    return participants


def _progress_audience_token(
    invitation: MemberPlaceInvitation,
    participants: list[tuple[JourneyMember, Traveler | None, MemberPlaceInvitationRecipient | None, str]],
) -> str:
    audience = [
        {
            "member_id": str(member.id),
            "traveler_id": str(member.traveler_id),
            "membership": role,
            "response_id": str(response.id) if response is not None else None,
            "accepted_at": (
                str(response.responded_at or response.updated_at)
                if response is not None
                else None
            ),
        }
        for member, _traveler, response, role in participants
    ]
    audience.sort(key=lambda row: (row["member_id"], row["membership"]))
    payload = {
        "version": 1,
        "invitation_id": str(invitation.id),
        "active_plan_version_id": str(invitation.active_plan_version_id),
        "members": audience,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _invitation_progress_board(
    session: Session,
    *,
    journey: Journey,
    invitation: MemberPlaceInvitation,
    recipients: list[MemberPlaceInvitationRecipient],
    viewer: JourneyMember,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    participants = _invitation_progress_participants(
        session,
        invitation=invitation,
        recipients=recipients,
        now=now,
    )
    audience_token = _progress_audience_token(invitation, participants)

    active_signals = session.scalars(
        select(MemberPlaceInvitationProgress).where(
            MemberPlaceInvitationProgress.invitation_id == invitation.id,
            MemberPlaceInvitationProgress.status == "ACTIVE",
        )
    ).all()
    # An accepted member's visibility begins when their own stop became
    # ready. Earlier opt-in reports were shared with the *then-joined* cohort;
    # a later join cannot retroactively widen that audience.
    viewer_joined_at: datetime | None = None
    if str(viewer.id) != str(invitation.owner_member_id):
        viewer_response = next(
            (
                row for row in recipients
                if str(row.recipient_member_id) == str(viewer.id)
                and str(row.recipient_traveler_id) == str(viewer.traveler_id)
                and row.state == "ACCEPTED_OWN_STOP_READY"
            ),
            None,
        )
        if viewer_response is not None:
            viewer_joined_at = _stamp(viewer_response.updated_at)

    signal_by_participant: dict[tuple[str, str], MemberPlaceInvitationProgress] = {}
    for signal in active_signals:
        signal_by_participant[(str(signal.member_id), str(signal.signal_type))] = signal

    views: list[dict[str, Any]] = []
    own_context: tuple[MemberPlaceInvitationRecipient | None, str] | None = None
    for participant, traveler, response, role in participants:
        expected_quest_id = (
            str(invitation.owner_side_quest_id)
            if role == "INVITER"
            else str(response.accepted_side_quest_id or "")
        )
        progress: dict[str, dict[str, Any] | None] = {}
        for signal_type, field in ((AT_PLACE_SIGNAL, "at_invited_place"), (HEADING_BACK_SIGNAL, "heading_back")):
            signal = signal_by_participant.get((str(participant.id), signal_type))
            if (
                signal is None
                or str(signal.traveler_id) != str(participant.traveler_id)
                or str(signal.side_quest_id) != expected_quest_id
                or str(signal.place_id) != str(invitation.place_id)
                or str(signal.active_plan_version_id) != str(invitation.active_plan_version_id)
                or (
                    str(viewer.id) != str(invitation.owner_member_id)
                    and str(participant.id) != str(viewer.id)
                    and (
                        viewer_joined_at is None
                        or _stamp(signal.reported_at) is None
                        or _stamp(signal.reported_at) < viewer_joined_at
                    )
                )
            ):
                progress[field] = None
            else:
                progress[field] = {
                    "reported_at": signal.reported_at,
                    "claim": signal_type,
                    "physical_presence_verified": False,
                }
        views.append({
            "member_id": str(participant.id),
            "display_name": str(traveler.display_name if traveler else "Traveller"),
            "membership": role,
            **progress,
        })
        if str(participant.id) == str(viewer.id):
            own_context = (response, role)

    own_view = next((row for row in views if str(row["member_id"]) == str(viewer.id)), None)
    own_progress = {
        "can_share_arrival": False,
        "arrival_shared": bool(own_view and own_view["at_invited_place"]),
        "can_share_heading_back": False,
        "heading_back_shared": bool(own_view and own_view["heading_back"]),
        "share_audience_token": audience_token if own_context is not None else None,
    }
    if own_context is not None:
        response, role = own_context
        own_progress["can_share_arrival"] = _progress_can_be_shared(
            session,
            journey=journey,
            member=viewer,
            traveler_id=str(viewer.traveler_id),
            invitation=invitation,
            response=response,
            role=role,
            signal_type=AT_PLACE_SIGNAL,
            now=now,
        )
        own_progress["can_share_heading_back"] = _progress_can_be_shared(
            session,
            journey=journey,
            member=viewer,
            traveler_id=str(viewer.traveler_id),
            invitation=invitation,
            response=response,
            role=role,
            signal_type=HEADING_BACK_SIGNAL,
            now=now,
        )
    return views, own_progress


def invitation_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not viewer_member_id:
        return None
    current = _now(now)
    viewer = session.get(JourneyMember, str(viewer_member_id))
    if not _member_in_force(viewer, current):
        return None
    # A signed collaborator does not automatically carry JOURNEY_UPDATE.
    viewer_capability = session.scalar(
        select(JourneyRoleCapability.role).where(
            JourneyRoleCapability.role == viewer.role,
            JourneyRoleCapability.capability == "JOURNEY_UPDATE",
        )
    )
    output: dict[str, list[dict[str, Any]]] = {
        "sent": [], "incoming": [], "joined": [], "promotions": [], "co_planning_grants": []
    }
    can_manage_members = session.scalar(
        select(JourneyRoleCapability.role).where(
            JourneyRoleCapability.role == viewer.role,
            JourneyRoleCapability.capability == "MEMBER_MANAGE",
        )
    ) is not None

    def _co_planning_options(invitation: MemberPlaceInvitation) -> list[dict[str, Any]]:
        if not can_manage_members or not journey.active_plan_version_id:
            return []
        from app.shared_stop_coplanning import grant_status_for_projection, member_is_in_force

        rows = session.scalars(
            select(JourneyMember)
            .where(JourneyMember.journey_id == str(journey.id))
            .order_by(JourneyMember.created_at, JourneyMember.id)
        ).all()
        options: list[dict[str, Any]] = []
        for candidate in rows:
            if (
                str(candidate.id) == str(viewer.id)
                or not member_is_in_force(candidate, now=current)
                or session.scalar(
                    select(JourneyRoleCapability.role).where(
                        JourneyRoleCapability.role == candidate.role,
                        JourneyRoleCapability.capability == "JOURNEY_UPDATE",
                    )
                ) is not None
            ):
                continue
            grants = session.scalars(
                select(SharedStopCoPlanningGrant)
                .where(
                    SharedStopCoPlanningGrant.journey_id == str(journey.id),
                    SharedStopCoPlanningGrant.invitation_id == str(invitation.id),
                    SharedStopCoPlanningGrant.base_plan_version_id == str(journey.active_plan_version_id),
                    SharedStopCoPlanningGrant.grantee_member_id == str(candidate.id),
                )
                .order_by(
                    SharedStopCoPlanningGrant.updated_at.desc(),
                    SharedStopCoPlanningGrant.id.desc(),
                )
            ).all()
            latest = grants[0] if grants else None
            state = "NOT_OFFERED"
            grant_id = None
            expires_at = None
            if latest is not None:
                state = grant_status_for_projection(latest, now=current)
                if latest.promotion_id:
                    state = "BOUND"
                grant_id = str(latest.id)
                expires_at = str(latest.expires_at)
            traveler = session.get(Traveler, str(candidate.traveler_id))
            options.append(
                {
                    "member_id": str(candidate.id),
                    "display_name": str(traveler.display_name if traveler else "Traveller"),
                    "role": str(candidate.role),
                    "grant_id": grant_id,
                    "state": state,
                    "expires_at": expires_at,
                }
            )
        return options
    parent_query = select(MemberPlaceInvitation).where(
        MemberPlaceInvitation.journey_id == journey.id
    )
    if str(viewer.id):
        parent_query = parent_query.outerjoin(
            MemberPlaceInvitationRecipient,
            MemberPlaceInvitationRecipient.invitation_id == MemberPlaceInvitation.id,
        ).where(
            (MemberPlaceInvitation.owner_member_id == str(viewer.id))
            | (MemberPlaceInvitationRecipient.recipient_member_id == str(viewer.id))
        ).distinct()
    parents = session.scalars(
        parent_query.order_by(MemberPlaceInvitation.created_at.desc(), MemberPlaceInvitation.id.desc()).limit(30)
    ).all()

    for invitation in parents:
        recipients = session.scalars(
            select(MemberPlaceInvitationRecipient)
            .where(MemberPlaceInvitationRecipient.invitation_id == invitation.id)
            .order_by(MemberPlaceInvitationRecipient.invited_at, MemberPlaceInvitationRecipient.id)
        ).all()
        owner = session.get(JourneyMember, str(invitation.owner_member_id))
        owner_traveler = session.get(Traveler, str(invitation.owner_traveler_id))
        owner_name = str(owner_traveler.display_name if owner_traveler else "Traveller")
        place = session.get(Place, str(invitation.place_id))
        rendezvous_place = session.get(Place, str(invitation.rendezvous_place_id))
        rendezvous_activity = session.get(Activity, str(invitation.rendezvous_activity_id))
        if place is None or rendezvous_place is None or rendezvous_activity is None:
            continue
        parent_state = str(invitation.state)
        if parent_state == "OPEN":
            expiry = _stamp(invitation.expires_at)
            if expiry is None or expiry <= current:
                parent_state = "EXPIRED"
            elif not _invitation_context_is_current(session, journey, invitation, now=current):
                parent_state = "STALE"

        try:
            disclosure_snapshot = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
            place_view = disclosure_snapshot["place"]
            rendezvous_view = disclosure_snapshot["rendezvous"]
            if not isinstance(place_view, dict) or not isinstance(rendezvous_view, dict):
                continue
        except (TypeError, KeyError, json.JSONDecodeError):
            continue
        from app.execution.member_place_group_share import current_group_share

        group_share = current_group_share(session, journey, invitation, now=current)
        viewer_is_source = str(invitation.owner_member_id) == str(viewer.id)
        viewer_is_accepted_recipient = any(
            str(row.recipient_member_id) == str(viewer.id)
            and str(row.recipient_traveler_id) == str(viewer.traveler_id)
            and str(row.state) == "ACCEPTED_OWN_STOP_READY"
            for row in recipients
        )
        viewer_can_manage_group_share = (
            viewer_is_source
            and group_share.get("status") != "SHARED_BY_ORIGINAL_INVITATION"
            and group_share.get("status") != "GROUP_SHARE_REQUIRES_ACCEPTED_INVITEE"
        )
        viewer_can_propose_group_share = (
            group_share.get("shared")
            and (viewer_is_source or viewer_is_accepted_recipient)
        )
        if str(invitation.owner_member_id) == str(viewer.id):
            progress_board, own_progress = _invitation_progress_board(
                session,
                journey=journey,
                invitation=invitation,
                recipients=recipients,
                viewer=viewer,
                now=current,
            )
            recipient_views: list[dict[str, Any]] = []
            for response in recipients:
                member = session.get(JourneyMember, str(response.recipient_member_id))
                traveler = session.get(Traveler, str(response.recipient_traveler_id))
                response_state = str(response.state)
                if response_state == "INVITED":
                    if not _member_in_force(member, current):
                        response_state = "STALE"
                    elif parent_state != "OPEN":
                        response_state = parent_state
                recipient_views.append({
                    "member_id": str(response.recipient_member_id),
                    "display_name": str(traveler.display_name if traveler else "Traveller"),
                    "state": response_state,
                })
            output["sent"].append({
                "invitation_id": str(invitation.id),
                "side_quest_id": str(invitation.owner_side_quest_id),
                "state": parent_state,
                "created_at": invitation.created_at,
                "expires_at": invitation.expires_at,
                "place": place_view,
                "rendezvous": rendezvous_view,
                "recipients": recipient_views,
                "participants": progress_board,
                "own_progress": own_progress,
                "viewer_can_propose": viewer_capability is not None and viewer_can_propose_group_share,
                "viewer_has_journey_update": viewer_capability is not None,
                "group_share_status": str(group_share.get("status") or "NOT_SHARED_TO_GROUP"),
                "group_share_receipt_id": (
                    str(group_share["receipt"].id) if group_share.get("receipt") is not None else None
                ),
                "viewer_can_manage_group_share": viewer_can_manage_group_share,
                "co_planning_options": _co_planning_options(invitation) if group_share.get("shared") else [],
            })
            continue

        response = next((row for row in recipients if str(row.recipient_member_id) == str(viewer.id)), None)
        if response is None:
            continue
        response_state = str(response.state)
        if response_state == "INVITED":
            if parent_state != "OPEN":
                response_state = parent_state
            elif str(owner.journey_id if owner else "") != str(journey.id) or not _member_in_force(owner, current):
                response_state = "STALE"
        if response_state == "DECLINED":
            continue
        if response_state == "ACCEPTED_OWN_STOP_READY":
            progress_board, own_progress = _invitation_progress_board(
                session,
                journey=journey,
                invitation=invitation,
                recipients=recipients,
                viewer=viewer,
                now=current,
            )
            output["joined"].append({
                "invitation_id": str(invitation.id),
                "state": parent_state,
                "inviter_display_name": owner_name,
                "created_at": invitation.created_at,
                "place": place_view,
                "rendezvous": rendezvous_view,
                "participants": progress_board,
                "own_progress": own_progress,
                "viewer_can_propose": viewer_capability is not None and viewer_can_propose_group_share,
                "viewer_has_journey_update": viewer_capability is not None,
                "group_share_status": str(group_share.get("status") or "NOT_SHARED_TO_GROUP"),
                "group_share_receipt_id": (
                    str(group_share["receipt"].id) if group_share.get("receipt") is not None else None
                ),
                "viewer_can_manage_group_share": False,
                "co_planning_options": _co_planning_options(invitation) if group_share.get("shared") else [],
            })
            continue
        output["incoming"].append({
            "invitation_id": str(invitation.id),
            "state": response_state,
            "inviter_display_name": owner_name,
            "created_at": invitation.created_at,
            "expires_at": invitation.expires_at,
            "place": place_view,
            "rendezvous": rendezvous_view,
            "can_respond": (
                response_state == "INVITED" and parent_state == "OPEN"
            ) or response_state == "ACCEPTANCE_NEEDS_COMPLETION",
        })

    # A scoped R73 offer is visible to its named grantee even when they were
    # never part of the temporary R70/R72 invitation audience. Managers see
    # only grants they issued (the roster options are projected on their sent
    # invitation above).
    grant_query = select(SharedStopCoPlanningGrant).where(
        SharedStopCoPlanningGrant.journey_id == str(journey.id),
        (SharedStopCoPlanningGrant.grantee_member_id == str(viewer.id))
        | (SharedStopCoPlanningGrant.grantor_member_id == str(viewer.id)),
    )
    for grant in session.scalars(
        grant_query.order_by(
            SharedStopCoPlanningGrant.updated_at.desc(),
            SharedStopCoPlanningGrant.id.desc(),
        ).limit(30)
    ).all():
        invitation = session.get(MemberPlaceInvitation, str(grant.invitation_id))
        if invitation is None:
            continue
        from app.execution.member_place_group_share import current_group_share

        if not current_group_share(session, journey, invitation, now=current).get("shared"):
            continue
        try:
            frozen = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        place_view = frozen.get("place") if isinstance(frozen, dict) else None
        rendezvous_view = frozen.get("rendezvous") if isinstance(frozen, dict) else None
        if not isinstance(place_view, dict) or not isinstance(rendezvous_view, dict):
            continue
        from app.shared_stop_coplanning import grant_status_for_projection, find_accepted_grant

        grantee = session.get(JourneyMember, str(grant.grantee_member_id))
        grant_state = grant_status_for_projection(grant, now=current)
        if grant_state == "ACCEPTED" and (
            grantee is None
            or find_accepted_grant(
                session,
                journey_id=str(journey.id),
                invitation_id=str(grant.invitation_id),
                base_plan_version_id=str(grant.base_plan_version_id),
                grantee=grantee,
                promotion_id=str(grant.promotion_id) if grant.promotion_id else None,
                decision_epoch=str(grant.decision_epoch) if grant.decision_epoch else None,
                require_unbound=grant.promotion_id is None,
                now=current,
            ) is None
        ):
            grant_state = "STALE"
        elif grant.promotion_id and grant_state == "ACCEPTED":
            grant_state = "BOUND"
        grantor = session.get(JourneyMember, str(grant.grantor_member_id))
        grantor_traveler = session.get(Traveler, str(grant.grantor_traveler_id))
        grantee_traveler = session.get(Traveler, str(grant.grantee_traveler_id))
        output["co_planning_grants"].append(
            {
                "grant_id": str(grant.id),
                "invitation_id": str(invitation.id),
                "base_plan_version_id": str(grant.base_plan_version_id),
                "state": grant_state,
                "purpose": str(grant.purpose),
                "expires_at": str(grant.expires_at),
                "place": place_view,
                "rendezvous": rendezvous_view,
                "grantor_member_id": str(grant.grantor_member_id),
                "grantor_name": str(grantor_traveler.display_name if grantor_traveler else "Traveller"),
                "grantee_member_id": str(grant.grantee_member_id),
                "grantee_name": str(grantee_traveler.display_name if grantee_traveler else "Traveller"),
                "viewer_can_respond": (
                    str(viewer.id) == str(grant.grantee_member_id)
                    and grant_state == "OFFERED"
                    and grant.promotion_id is None
                ),
                "viewer_can_revoke": (
                    str(viewer.id) == str(grant.grantor_member_id)
                    and grant_state in {"OFFERED", "ACCEPTED", "BOUND"}
                ),
            }
        )

    # R73's shared-stop proposal is a Journey-level collective decision, so it
    # is projected to every current Journey member, including members outside
    # the temporary R70/R72 invitation group.
    promotions = session.scalars(
        select(MemberPlacePromotion)
        .where(MemberPlacePromotion.journey_id == journey.id)
        .order_by(MemberPlacePromotion.updated_at.desc(), MemberPlacePromotion.id.desc())
        .limit(20)
    ).all()
    for promotion in promotions:
        invitation = session.get(MemberPlaceInvitation, str(promotion.invitation_id))
        if invitation is None:
            continue
        from app.execution.member_place_group_share import current_group_share

        if not current_group_share(session, journey, invitation, now=current).get("shared"):
            continue
        try:
            snapshot = json.loads(str(invitation.disclosure_snapshot_json or "{}"))
            place_view = snapshot.get("place")
            rendezvous_view = snapshot.get("rendezvous")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(place_view, dict) or not isinstance(rendezvous_view, dict):
            continue
        mission = (
            session.get(JourneyMission, str(promotion.mission_id))
            if promotion.mission_id
            else None
        )
        clarification = (
            json.loads(str(mission.clarification_json or "{}"))
            if mission is not None
            else {}
        )
        responses = clarification.get("responses")
        responses = responses if isinstance(responses, dict) else {}
        required_ids = [str(item) for item in clarification.get("required_member_ids") or []]
        viewer_response = responses.get(str(viewer.id))
        proposal = (
            session.get(ReplanProposal, str(promotion.proposal_id))
            if promotion.proposal_id
            else None
        )
        active_plan = session.get(PlanVersion, str(journey.active_plan_version_id)) if journey.active_plan_version_id else None
        state = str(promotion.state)
        if (
            state == "AUTHORIZED"
            and proposal is not None
            and active_plan is not None
            and str(active_plan.id) == str(proposal.proposed_plan_version_id)
        ):
            state = "APPLIED"
        response_views = []
        for member_id in required_ids:
            member = session.get(JourneyMember, member_id)
            traveler = session.get(Traveler, str(member.traveler_id)) if member else None
            response = responses.get(member_id)
            response_views.append({
                "member_id": member_id,
                "display_name": str(traveler.display_name if traveler else "Traveller"),
                "decision": (
                    "APPROVE" if isinstance(response, dict) and response.get("proposal_id")
                    else "DECLINE" if isinstance(response, dict) and response.get("decision") == "DECLINE"
                    else "WAITING"
                ),
            })
        try:
            state_detail = json.loads(str(promotion.state_detail_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            state_detail = {}
        # Project only bounded, server-derived decision facts, not raw routes,
        # coordinates or private journey/Place search context.
        tradeoff = None
        if proposal is not None:
            try:
                proposal_diff = json.loads(str(proposal.diff_json or "{}"))
            except (TypeError, json.JSONDecodeError):
                proposal_diff = {}
            if not isinstance(proposal_diff, dict):
                proposal_diff = {}
            verified = proposal_diff.get("verified_evidence")
            mobility = verified.get("mobility") if isinstance(verified, dict) else None
            mobility = mobility if isinstance(mobility, dict) else {}
            snapshot = mobility.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            qualities = sorted({
                str(item.get("fact_quality") or "UNKNOWN")
                for item in snapshot.get("facts") or []
                if isinstance(item, dict)
            })
            def _safe_int(value: Any) -> int | None:
                if value is None or isinstance(value, bool):
                    return None
                try:
                    return int(value)
                except (TypeError, ValueError, OverflowError):
                    return None
            tradeoff = {
                "added_count": len(proposal_diff.get("added") or []),
                "removed_count": len(proposal_diff.get("removed") or []),
                "moved_count": len(proposal_diff.get("moved") or []),
                "cost_delta_minor": _safe_int(proposal_diff.get("cost_delta_minor")),
                "travel_delta_min": _safe_int(proposal_diff.get("travel_delta_min")),
                "end_time_delta_min": _safe_int(proposal_diff.get("end_time_delta_min")),
                "currency": str(journey.budget_currency or ""),
                "protected_commitments_preserved": bool(proposal.locked_commitments_preserved),
                "hard_constraints_passed": bool(proposal.hard_constraints_passed),
                "route_provider": str(mobility.get("provider") or snapshot.get("source_provider") or "UNKNOWN"),
                "route_fact_qualities": qualities,
            }
        viewer_scoped_grant = None
        if viewer_capability is None and state == "WAITING_FOR_MEMBERS":
            from app.shared_stop_coplanning import find_accepted_grant

            viewer_scoped_grant = find_accepted_grant(
                session,
                journey_id=str(journey.id),
                invitation_id=str(promotion.invitation_id),
                base_plan_version_id=str(promotion.active_plan_version_id),
                grantee=viewer,
                promotion_id=str(promotion.id),
                decision_epoch=str(promotion.decision_epoch or ""),
                now=current,
            )
        output["promotions"].append({
            "promotion_id": str(promotion.id),
            "invitation_id": str(invitation.id),
            "state": state,
            "state_detail": state_detail,
            "place": place_view,
            "rendezvous": rendezvous_view,
            "dwell_minutes": int(promotion.dwell_minutes),
            "tradeoff": tradeoff,
            "base_plan_version_id": str(promotion.active_plan_version_id),
            "proposal_id": str(promotion.proposal_id) if promotion.proposal_id else None,
            "mission_id": str(promotion.mission_id) if promotion.mission_id else None,
            "clarification_id": clarification.get("clarification_id"),
            "responses": response_views,
            "viewer_decision": (
                "APPROVE" if isinstance(viewer_response, dict) and viewer_response.get("proposal_id")
                else "DECLINE" if isinstance(viewer_response, dict) and viewer_response.get("decision") == "DECLINE"
                else None
            ),
            "viewer_can_decide": (
                state == "WAITING_FOR_MEMBERS"
                and str(viewer.id) in set(required_ids)
                and not isinstance(viewer_response, dict)
                and (viewer_capability is not None or viewer_scoped_grant is not None)
            ),
            "viewer_scoped_authority_expires_at": (
                str(viewer_scoped_grant.expires_at) if viewer_scoped_grant is not None else None
            ),
            "viewer_can_apply": state == "AUTHORIZED" and viewer_capability is not None,
            "applied_plan_version_id": str(promotion.applied_plan_version_id) if promotion.applied_plan_version_id else None,
        })
    return output
