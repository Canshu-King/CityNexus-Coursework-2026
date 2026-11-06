"""Two-leg private buddy routing and protected-commitment feasibility.

The only origin that can reach a provider is the caller's fresh, handoff-bound
private observation.  The second leg starts at the already agreed public
handoff Place and returns to the current original shared rendezvous.  Both
operations are stored as caller-private route operations and are released only
after the full handoff context is revalidated.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.concurrency_guards import journey_write_lock
from app.db.models import (
    Activity,
    Journey,
    JourneyMember,
    JourneyRendezvousBuddyHandoff,
    LocationObservation,
    Place,
    RouteAcquisitionOperation,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import (
    LIVE_ROUTE_PROVENANCE,
    LOCATION_FRESHNESS_SECONDS,
    REPLAY_ROUTE_PROVENANCE,
    public_destination_coordinates,
    rendezvous_commitment_risk,
)
from app.execution.private_buddy_route import (
    PRIVATE_SCOPE,
    _accepted_handoff_context,
    _load_result,
    private_route_scope,
    _provider_receipt_persisted,
    _record_unconfigured_route,
    _route_request,
    _stamp,
    _latest_private_operation,
    validate_private_location_binding,
)
from app.execution.route_session_contract import RouteRequest
from app.route_session_operations import submit_operation
from app.traveler_principal import TravelerPrincipal


def _now() -> datetime:
    return execution_clock.now().astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _public_place_snapshot(place: Place | None) -> dict[str, Any] | None:
    coords = public_destination_coordinates(place)
    if place is None or coords is None:
        return None
    return {
        "place_id": str(place.id),
        "name": str(place.name),
        "latitude": coords[0],
        "longitude": coords[1],
        "coordinate_system": "WGS84",
        "provider_key": str(place.provider_key or "unknown"),
        "external_id": str(place.external_id or ""),
    }


def _context_member_row(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
) -> JourneyRendezvousBuddyHandoff:
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "R81_SIGNED_MEMBER_MISMATCH")
    row = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == str(handoff_id),
        JourneyRendezvousBuddyHandoff.journey_id == str(journey.id),
    ))
    if row is None:
        raise HTTPException(404, "R81_HANDOFF_NOT_FOUND")
    if str(viewer.id) not in {str(row.helper_member_id), str(row.requester_member_id)}:
        raise HTTPException(403, "R81_HANDOFF_PAIR_MEMBER_REQUIRED")
    return row


def _stale_result(handoff_id: str, reason: str = "R81_ROUTE_CONTEXT_STALE") -> dict[str, Any]:
    empty_leg = {
        "route_state": "UNKNOWN",
        "status": "STALE",
        "reason": reason,
        "duration_min": None,
        "provider": None,
        "provenance": "UNKNOWN",
        "captured_at": None,
    }
    return {
        "handoff_id": str(handoff_id),
        "route_state": "UNKNOWN",
        "status": "STALE",
        "reason": reason,
        "duration_min": None,
        "provider": None,
        "provenance": "UNKNOWN",
        "outbound_leg": dict(empty_leg),
        "return_leg": dict(empty_leg),
        "shared_commitment_risk": {
            "state": "UNKNOWN",
            "authority": "UNKNOWN",
            "next_commitment": None,
            "slack_after_buffer_min": None,
            "recommended_action": "REFRESH_THE_CURRENT_HANDOFF_AND_SHARED_STOP",
        },
        "shared_plan_changed": False,
        "physical_arrival_verified": False,
        "guardian_triggered": False,
    }


def _context(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
) -> tuple[JourneyRendezvousBuddyHandoff, dict[str, Any], Activity, Place, Place]:
    handoff, handoff_snapshot, activity, original = _accepted_handoff_context(
        session,
        journey=journey,
        viewer=viewer,
        principal=principal,
        handoff_id=handoff_id,
    )
    handoff_place = session.get(Place, str(handoff.handoff_place_id))
    handoff_coords = public_destination_coordinates(handoff_place)
    original_coords = public_destination_coordinates(original)
    try:
        captured_snapshot_coords = (
            float(handoff_snapshot["latitude"]),
            float(handoff_snapshot["longitude"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        captured_snapshot_coords = None
    if (
        handoff_place is None
        or handoff_coords is None
        or original_coords is None
        or captured_snapshot_coords != handoff_coords
        or str(handoff_snapshot.get("coordinate_system") or "").upper() != "WGS84"
        or str(activity.place_id or "") != str(original.id)
    ):
        raise HTTPException(409, "R81_PUBLIC_ROUTE_TARGET_STALE")
    return handoff, handoff_snapshot, activity, handoff_place, original


def _return_request(
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    handoff_snapshot: dict[str, Any],
    activity: Activity,
    handoff_place: Place,
    original_place: Place,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    observation: LocationObservation,
    outbound_operation_id: str,
) -> RouteRequest:
    handoff_public = _public_place_snapshot(handoff_place)
    original_public = _public_place_snapshot(original_place)
    if handoff_public is None or original_public is None:
        raise HTTPException(409, "R81_PUBLIC_ROUTE_COORDINATES_UNAVAILABLE")
    return RouteRequest(
        journey_id=str(journey.id),
        origin_key=str(handoff_place.id),
        target_key=str(original_place.id),
        target_activity_id=str(activity.id),
        requested_mode="WALK",
        origin_evidence={
            "private_scope": PRIVATE_SCOPE,
            "route_leg": "RETURN_TO_SHARED_STOP",
            "journey_member_id": str(viewer.id),
            "traveler_id": str(principal.traveler_id),
            "handoff_id": str(handoff.id),
            "active_plan_version_id": str(handoff.active_plan_version_id),
            "current_activity_id": str(handoff.current_activity_id or ""),
            "audience_sha256": str(handoff.audience_sha256),
            "private_location_binding_id": str(observation.id),
            "bound_outbound_operation_id": str(outbound_operation_id),
            "handoff_place_snapshot_sha256": _sha(handoff_snapshot),
            "shared_stop_snapshot_sha256": _sha(original_public),
            "source": "AGREED_PUBLIC_HANDOFF_PLACE",
            "external_provider_disclosure_confirmed": True,
        },
    )


def _return_operation_matches(
    row: RouteAcquisitionOperation,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_snapshot: dict[str, Any],
    original_place: Place,
    outbound_operation_id: str,
    observation_id: str,
) -> bool:
    try:
        request = json.loads(row.request_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    evidence = request.get("origin_evidence") if isinstance(request, dict) else None
    shared_snapshot = _public_place_snapshot(original_place)
    if not isinstance(evidence, dict) or shared_snapshot is None:
        return False
    return bool(
        evidence.get("private_scope") == PRIVATE_SCOPE
        and evidence.get("route_leg") == "RETURN_TO_SHARED_STOP"
        and str(evidence.get("journey_member_id") or "") == str(viewer.id)
        and str(evidence.get("traveler_id") or "") == str(principal.traveler_id)
        and str(evidence.get("handoff_id") or "") == str(handoff.id)
        and str(evidence.get("active_plan_version_id") or "") == str(handoff.active_plan_version_id)
        and str(evidence.get("current_activity_id") or "") == str(handoff.current_activity_id or "")
        and str(evidence.get("audience_sha256") or "") == str(handoff.audience_sha256)
        and str(evidence.get("private_location_binding_id") or "") == str(observation_id)
        and str(evidence.get("bound_outbound_operation_id") or "") == str(outbound_operation_id)
        and str(evidence.get("handoff_place_snapshot_sha256") or "") == _sha(handoff_snapshot)
        and str(evidence.get("shared_stop_snapshot_sha256") or "") == _sha(shared_snapshot)
        and bool(evidence.get("external_provider_disclosure_confirmed"))
        and str(row.origin_key) == str(handoff.handoff_place_id)
        and str(row.target_key) == str(handoff.original_public_place_id)
        and str(row.target_activity_id or "") == str(handoff.original_rendezvous_activity_id)
    )


def _find_return_operation(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_snapshot: dict[str, Any],
    original_place: Place,
    outbound_operation_id: str,
    observation_id: str,
) -> RouteAcquisitionOperation | None:
    rows = session.scalars(select(RouteAcquisitionOperation).where(
        RouteAcquisitionOperation.journey_id == str(journey.id),
        RouteAcquisitionOperation.origin_key == str(handoff.handoff_place_id),
        RouteAcquisitionOperation.target_key == str(handoff.original_public_place_id),
        RouteAcquisitionOperation.target_activity_id == str(handoff.original_rendezvous_activity_id),
    ).order_by(RouteAcquisitionOperation.queued_at.desc(), RouteAcquisitionOperation.id.desc())).all()
    return next((row for row in rows if _return_operation_matches(
        row,
        handoff=handoff,
        viewer=viewer,
        principal=principal,
        handoff_snapshot=handoff_snapshot,
        original_place=original_place,
        outbound_operation_id=outbound_operation_id,
        observation_id=observation_id,
    )), None)


def _leg_projection(
    session: Session,
    *,
    row: RouteAcquisitionOperation,
    observation_age_seconds: int | None,
) -> dict[str, Any]:
    result, error = _load_result(row)
    provider = str(result.get("provider") or "")
    provenance = str(result.get("provenance") or result.get("route_provenance") or "UNKNOWN").upper()
    duration_raw = result.get("duration_min")
    try:
        duration = int(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError, OverflowError):
        duration = None
    captured = _stamp(result.get("captured_at") or row.finished_at)
    age = max(0, int((_now() - captured).total_seconds())) if captured else None
    fresh = age is not None and age <= LOCATION_FRESHNESS_SECONDS
    route_state = "UNKNOWN"
    reason = str(error.get("reason") or error.get("classification") or row.outcome or "ROUTE_UNAVAILABLE")
    if provenance == "TEST_PROVIDER_EMULATOR":
        if (
            row.status == "COMPLETED"
            and duration is not None
            and duration >= 0
            and not provider.casefold().startswith(("mock", "fixture", "synthetic"))
        ):
            route_state = "TEST_PROVIDER_EMULATOR"
            reason = "CONTROLLED_TEST_PROVIDER_NOT_LIVE_EVIDENCE"
        else:
            reason = "TEST_PROVIDER_RESULT_INCOMPLETE_OR_UNTRUSTED"
    elif (
        row.status == "COMPLETED"
        and duration is not None
        and duration >= 0
        and fresh
        and not provider.casefold().startswith(("mock", "fixture", "synthetic", "test"))
    ):
        if provenance in LIVE_ROUTE_PROVENANCE and _provider_receipt_persisted(
            session,
            operation=row,
            provider=provider,
            provenance=provenance,
            duration_min=duration,
        ):
            route_state = "LIVE"
            reason = ""
        elif provenance in REPLAY_ROUTE_PROVENANCE:
            route_state = "REPLAY"
            reason = "CAPTURED_HISTORICAL_ROUTE_NOT_CURRENT_TRAFFIC"
    elif age is not None and age > LOCATION_FRESHNESS_SECONDS:
        reason = "ROUTE_EVIDENCE_STALE"
    return {
        "route_state": route_state,
        "status": str(row.status),
        "reason": reason,
        "duration_min": duration if route_state in {"LIVE", "REPLAY", "TEST_PROVIDER_EMULATOR"} else None,
        "provider": provider or None,
        "provenance": provenance,
        "captured_at": str(result.get("captured_at") or row.finished_at or "") or None,
        "route_age_seconds": age,
        "origin_age_seconds": observation_age_seconds,
        "operation_id": str(row.id),
    }


def _missing_leg(reason: str = "ROUTE_LEG_NOT_REQUESTED") -> dict[str, Any]:
    return {
        "route_state": "UNKNOWN",
        "status": "NOT_REQUESTED",
        "reason": reason,
        "duration_min": None,
        "provider": None,
        "provenance": "UNKNOWN",
        "captured_at": None,
        "route_age_seconds": None,
        "origin_age_seconds": None,
        "operation_id": None,
    }


def _unknown_risk(authority: str = "UNKNOWN", *, next_commitment: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "state": "UNKNOWN",
        "authority": authority,
        "next_commitment": next_commitment,
        "slack_after_buffer_min": None,
        "recommended_action": "USE_DESTINATION_ONLY_WALKING_DIRECTIONS_OR_REQUEST_AUTHORIZED_GROUP_RECOVERY",
    }


def _shared_risk(
    session: Session,
    journey: Journey,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    activity: Activity,
    outbound: dict[str, Any],
    return_leg: dict[str, Any],
    same_place: bool,
) -> dict[str, Any]:
    first_state = str(outbound.get("route_state") or "UNKNOWN")
    second_state = str(return_leg.get("route_state") or "UNKNOWN")
    first_duration = outbound.get("duration_min")
    second_duration = 0 if same_place else return_leg.get("duration_min")
    authority = str(outbound.get("authority") or "ADVISORY_FOREGROUND_LOCATION")
    for leg_name, leg, leg_state in (
        ("TO_HANDOFF", outbound, first_state),
        ("RETURN_TO_SHARED_STOP", return_leg, second_state),
    ):
        duration = leg.get("duration_min")
        if leg_state != "LIVE" or duration is None:
            continue
        lower_bound = rendezvous_commitment_risk(
            session,
            journey,
            activity,
            route_duration_min=int(duration),
            authority=authority,
        )
        if lower_bound.get("state") == "COMMITMENT_AT_RISK":
            return {
                **lower_bound,
                "basis": "ONE_VERIFIED_ROUTE_LEG_ALONE_EXCEEDS_PROTECTED_COMMITMENT",
                "legs_used": [leg_name],
                "recommended_action": "RETURN_TO_SHARED_STOP_OR_REQUEST_AUTHORIZED_GROUP_RECOVERY",
            }
    test_pair = first_state == second_state == "TEST_PROVIDER_EMULATOR"
    live_pair = first_state == "LIVE" and (same_place or second_state == "LIVE")
    if not live_pair and not test_pair:
        return _unknown_risk(authority)
    if first_duration is None or second_duration is None:
        return _unknown_risk(authority)
    total = int(first_duration) + int(second_duration)
    risk = rendezvous_commitment_risk(
        session,
        journey,
        activity,
        route_duration_min=total,
        authority="TEST_PROVIDER_EMULATOR" if test_pair else authority,
    )
    risk.update({
        "route_duration_min": total,
        "outbound_duration_min": int(first_duration),
        "return_duration_min": int(second_duration),
        "legs_used": ["TO_HANDOFF"] + ([] if same_place else ["RETURN_TO_SHARED_STOP"]),
        "basis": "TWO_LEG_ROUTE_TO_HANDOFF_AND_CURRENT_SHARED_STOP",
        "evidence_scope": "TEST_ONLY" if test_pair else "CURRENT_PROVIDER_CAPTURE",
    })
    if risk.get("state") == "COMMITMENT_AT_RISK":
        # Travel alone already exhausts the protected window. Any time spent
        # helping at the public handoff can only make that worse.
        risk["recommended_action"] = "RETURN_TO_SHARED_STOP_OR_REQUEST_AUTHORIZED_GROUP_RECOVERY"
    elif risk.get("state") in {"LOW_BUFFER", "WITHIN_BUFFER"}:
        # Both legs give a lower bound on travel time, NOT a guaranteed
        # homecoming: the human help/handoff duration is unknown. Never show
        # a green C3/C4 slack merely by assuming the meeting takes 0 minutes.
        travel_only_slack = risk.get("slack_after_buffer_min")
        risk.update({
            "state": "UNKNOWN",
            "basis": "TWO_LEG_TRAVEL_ONLY_HANDOFF_DWELL_UNKNOWN",
            "slack_after_buffer_min": None,
            "travel_only_slack_after_buffer_min": travel_only_slack,
            "predicted_arrival_at": None,
            "handoff_dwell_time_known": False,
            "recommended_action": "CHECK_HANDOFF_DURATION_BEFORE_DEPARTURE",
        })
    elif risk.get("state") == "NO_PROTECTED_RENDEZVOUS_COMMITMENT":
        risk["recommended_action"] = "CONTINUE_WITH_THE_INDEPENDENTLY_AGREED_HANDOFF"
    return risk


def _route_result(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    handoff_snapshot: dict[str, Any],
    activity: Activity,
    handoff_place: Place,
    original_place: Place,
    outbound_row: RouteAcquisitionOperation | None,
    return_row: RouteAcquisitionOperation | None,
    observation_age_seconds: int,
) -> dict[str, Any]:
    same_place = str(handoff_place.id) == str(original_place.id)
    outbound = (
        _leg_projection(session, row=outbound_row, observation_age_seconds=observation_age_seconds)
        if outbound_row is not None
        else _missing_leg()
    )
    if same_place:
        return_leg = {
            "route_state": "SAME_PUBLIC_DESTINATION",
            "status": "NOT_REQUIRED",
            "reason": "HANDOFF_IS_THE_CURRENT_SHARED_STOP",
            "duration_min": 0,
            "provider": None,
            "provenance": "SAME_WGS84_PLACE",
            "captured_at": None,
            "route_age_seconds": None,
            "origin_age_seconds": None,
            "operation_id": None,
        }
    else:
        return_leg = (
            _leg_projection(session, row=return_row, observation_age_seconds=None)
            if return_row is not None
            else _missing_leg()
        )
    first_state = str(outbound.get("route_state") or "UNKNOWN")
    second_state = str(return_leg.get("route_state") or "UNKNOWN")
    required_states = [first_state] + ([] if same_place else [second_state])
    if all(state == "LIVE" for state in required_states):
        state = "LIVE"
    elif all(state == "REPLAY" for state in required_states):
        state = "REPLAY"
    elif all(state == "TEST_PROVIDER_EMULATOR" for state in required_states):
        state = "TEST_PROVIDER_EMULATOR"
    else:
        state = "UNKNOWN"
    durations = [outbound.get("duration_min")] + ([] if same_place else [return_leg.get("duration_min")])
    total = sum(int(value) for value in durations) if state in {"LIVE", "REPLAY", "TEST_PROVIDER_EMULATOR"} and all(value is not None for value in durations) else None
    rows = [outbound_row] + ([] if same_place or return_row is None else [return_row])
    if state in {"LIVE", "REPLAY", "TEST_PROVIDER_EMULATOR"}:
        status = "COMPLETED"
        reason = ""
    elif any(str(leg.get("status")) == "STALE" for leg in (outbound, return_leg)):
        status = "STALE"
        reason = "ROUTE_EVIDENCE_STALE"
    elif any(row is not None and row.status in {"FAILED", "UNAVAILABLE"} for row in rows):
        status = "UNAVAILABLE"
        reason = next((str(leg.get("reason") or "") for leg in (outbound, return_leg) if leg.get("reason")), "ROUTE_UNAVAILABLE")
    elif outbound_row is None:
        status = "NOT_REQUESTED"
        reason = "NO_PRIVATE_ROUTE_REQUEST"
    else:
        status = "PARTIAL"
        reason = next((str(leg.get("reason") or "") for leg in (outbound, return_leg) if leg.get("reason")), "TWO_LEG_ROUTE_EVIDENCE_INCOMPLETE")
    authority = str(private_route_scope(outbound_row.request_json).get("authority") or "ADVISORY_FOREGROUND_LOCATION") if outbound_row is not None and private_route_scope(outbound_row.request_json) else "UNKNOWN"
    required_providers = [outbound.get("provider")] + ([] if same_place else [return_leg.get("provider")])
    provider = (
        str(required_providers[0])
        if required_providers and required_providers[0]
        and all(str(value or "") == str(required_providers[0]) for value in required_providers)
        else None
    )
    outbound["authority"] = authority
    risk = _shared_risk(
        session,
        journey,
        handoff=handoff,
        activity=activity,
        outbound=outbound,
        return_leg=return_leg,
        same_place=same_place,
    )
    return {
        "handoff_id": str(handoff.id),
        "route_state": state,
        "status": status,
        "reason": reason,
        "duration_min": total,
        "provider": provider if state in {"LIVE", "REPLAY"} else None,
        "provenance": "TWO_LEG_ROUTE_EVIDENCE" if state in {"LIVE", "REPLAY"} else state,
        "outbound_leg": outbound,
        "return_leg": return_leg,
        "target_place_name": str(handoff_snapshot.get("name") or handoff_place.name),
        "target_source_label": str(handoff_snapshot.get("source_label") or "Public catalog record"),
        "target_evidence_grade": str(handoff_snapshot.get("evidence_grade") or "UNKNOWN"),
        "original_shared_stop_name": str(original_place.name),
        "shared_commitment_risk": risk,
        "external_walking_directions": {
            "available": True,
            "destination_only": True,
            "provider_label": "Google Maps · walking directions",
            "sends_private_origin": False,
            "server_eta": False,
        },
        "shared_plan_changed": False,
        "physical_arrival_verified": False,
        "guardian_triggered": False,
    }


def _validated_outbound_operation(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_snapshot: dict[str, Any],
    observation_id: str,
) -> tuple[RouteAcquisitionOperation | None, LocationObservation, str, int]:
    observation, source, age = validate_private_location_binding(
        session,
        journey=journey,
        handoff=handoff,
        member=viewer,
        principal=principal,
        observation_id=observation_id,
        place_snapshot=handoff_snapshot,
    )
    outbound_row, evidence = _latest_private_operation(
        session,
        journey=journey,
        handoff=handoff,
        member=viewer,
        principal=principal,
        place_snapshot=handoff_snapshot,
    )
    if outbound_row is not None and evidence is not None:
        if (
            str(evidence.get("route_leg") or "TO_HANDOFF") != "TO_HANDOFF"
            or str(evidence.get("location_observation_id") or "") != str(observation_id)
            or str(evidence.get("private_location_binding_id") or observation_id) != str(observation_id)
        ):
            outbound_row = None
    return outbound_row, observation, source, age


def _make_operation(session: Session, journey: Journey, request: RouteRequest) -> RouteAcquisitionOperation:
    if settings.live_valhalla_url:
        row, _reused = submit_operation(session, journey, request)
        return row
    return _record_unconfigured_route(session, journey, request)


def acquire_private_buddy_safe_return(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
    observation_id: str,
    external_provider_disclosure_confirmed: bool,
) -> dict[str, Any]:
    """Capture both public route legs after distinct caller/provider consent.

    Provider calls run without the journey lock. Persisted route evidence is
    retained if the accepted handoff changes, but no stale ETA is released.
    """
    if not external_provider_disclosure_confirmed:
        raise HTTPException(400, "R81_EXTERNAL_PROVIDER_DISCLOSURE_CONFIRMATION_REQUIRED")
    handoff, snapshot, activity, handoff_place, original_place = _context(
        session,
        journey=journey,
        viewer=viewer,
        principal=principal,
        handoff_id=handoff_id,
    )
    outbound_request, _source, _age = _validate_and_build_outbound(
        session,
        journey=journey,
        handoff=handoff,
        snapshot=snapshot,
        activity=activity,
        member=viewer,
        principal=principal,
        observation_id=observation_id,
    )
    outbound_row = _make_operation(session, journey, outbound_request)
    if str(handoff_place.id) == str(original_place.id):
        return_row = None
    else:
        # The first provider call may have taken long enough for the pair
        # agreement, member set, plan cursor, or capture consent to change.
        # Revalidate in a fresh transaction before releasing the second leg.
        with journey_write_lock(str(journey.id)):
            session.expire_all()
            fresh_journey = session.get(Journey, str(journey.id))
            fresh_viewer = session.get(JourneyMember, str(viewer.id))
            if fresh_journey is None or fresh_viewer is None:
                return _stale_result(str(handoff.id), "R81_ROUTE_CONTEXT_CHANGED_AFTER_OUTBOUND")
            try:
                fresh_handoff, fresh_snapshot, fresh_activity, fresh_handoff_place, fresh_original = _context(
                    session,
                    journey=fresh_journey,
                    viewer=fresh_viewer,
                    principal=principal,
                    handoff_id=handoff_id,
                )
                fresh_outbound, fresh_observation, _fresh_source, _fresh_age = _validated_outbound_operation(
                    session,
                    journey=fresh_journey,
                    handoff=fresh_handoff,
                    viewer=fresh_viewer,
                    principal=principal,
                    handoff_snapshot=fresh_snapshot,
                    observation_id=observation_id,
                )
            except HTTPException:
                return _stale_result(str(handoff.id), "R81_ROUTE_CONTEXT_CHANGED_AFTER_OUTBOUND")
            if (
                fresh_outbound is None
                or str(fresh_outbound.id) != str(outbound_row.id)
                or str(fresh_observation.id) != str(observation_id)
                or str(fresh_handoff_place.id) == str(fresh_original.id)
            ):
                return _stale_result(str(handoff.id), "R81_ROUTE_CONTEXT_CHANGED_AFTER_OUTBOUND")
            return_request = _return_request(
                journey=fresh_journey,
                handoff=fresh_handoff,
                handoff_snapshot=fresh_snapshot,
                activity=fresh_activity,
                handoff_place=fresh_handoff_place,
                original_place=fresh_original,
                viewer=fresh_viewer,
                principal=principal,
                observation=fresh_observation,
                outbound_operation_id=str(fresh_outbound.id),
            )
        return_row = _make_operation(session, fresh_journey, return_request)

    with journey_write_lock(str(journey.id)):
        session.expire_all()
        fresh_journey = session.get(Journey, str(journey.id))
        fresh_viewer = session.get(JourneyMember, str(viewer.id))
        if fresh_journey is None or fresh_viewer is None:
            return _stale_result(str(handoff_id), "R81_ROUTE_CONTEXT_CHANGED_DURING_PROVIDER_WORK")
        try:
            fresh_handoff, fresh_snapshot, fresh_activity, fresh_handoff_place, fresh_original = _context(
                session,
                journey=fresh_journey,
                viewer=fresh_viewer,
                principal=principal,
                handoff_id=handoff_id,
            )
            fresh_outbound, fresh_observation, _fresh_source, fresh_age = _validated_outbound_operation(
                session,
                journey=fresh_journey,
                handoff=fresh_handoff,
                viewer=fresh_viewer,
                principal=principal,
                handoff_snapshot=fresh_snapshot,
                observation_id=observation_id,
            )
        except HTTPException:
            return _stale_result(str(handoff_id), "R81_ROUTE_CONTEXT_CHANGED_DURING_PROVIDER_WORK")
        fresh_evidence = private_route_scope(fresh_outbound.request_json) if fresh_outbound is not None else None
        if (
            fresh_outbound is None
            or str(fresh_outbound.id) != str(outbound_row.id)
            or fresh_evidence is None
            or str(fresh_evidence.get("location_observation_id") or "") != str(observation_id)
            or str(fresh_evidence.get("captured_at") or "") != str(fresh_observation.captured_at)
            or str(fresh_evidence.get("private_location_binding_id") or observation_id) != str(observation_id)
        ):
            # The operation remains durable, but its result cannot be tied back
            # to the currently authorized capture/context.
            return _stale_result(str(handoff_id), "R81_ROUTE_CONTEXT_CHANGED_DURING_PROVIDER_WORK")
        fresh_return = None
        if str(fresh_handoff_place.id) != str(fresh_original.id):
            fresh_return = _find_return_operation(
                session,
                journey=fresh_journey,
                handoff=fresh_handoff,
                viewer=fresh_viewer,
                principal=principal,
                handoff_snapshot=fresh_snapshot,
                original_place=fresh_original,
                outbound_operation_id=str(fresh_outbound.id),
                observation_id=observation_id,
            )
            if fresh_return is None or return_row is None or str(fresh_return.id) != str(return_row.id):
                return _stale_result(str(handoff_id), "R81_RETURN_ROUTE_CONTEXT_CHANGED")
        return _route_result(
            session,
            journey=fresh_journey,
            handoff=fresh_handoff,
            handoff_snapshot=fresh_snapshot,
            activity=fresh_activity,
            handoff_place=fresh_handoff_place,
            original_place=fresh_original,
            outbound_row=fresh_outbound,
            return_row=fresh_return,
            observation_age_seconds=fresh_age,
        )


def _validate_and_build_outbound(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    snapshot: dict[str, Any],
    activity: Activity,
    member: JourneyMember,
    principal: TravelerPrincipal,
    observation_id: str,
) -> tuple[RouteRequest, str, int]:
    observation, source, age = validate_private_location_binding(
        session,
        journey=journey,
        handoff=handoff,
        member=member,
        principal=principal,
        observation_id=observation_id,
        place_snapshot=snapshot,
    )
    # The existing R80 request builder owns the exact outbound WGS84 target
    # and carries only the observation key, never raw coordinates, in evidence.
    from app.execution.private_buddy_route import _route_request

    request = _route_request(
        journey=journey,
        handoff=handoff,
        place_snapshot=snapshot,
        member=member,
        principal=principal,
        observation=observation,
        source=source,
    )
    return request, source, age


def _has_stored_private_outbound_for_caller(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
) -> bool:
    """Distinguish an expired/invalid capture from a handoff with no request."""

    rows = session.scalars(select(RouteAcquisitionOperation).where(
        RouteAcquisitionOperation.journey_id == str(journey.id),
        RouteAcquisitionOperation.target_key == str(handoff.handoff_place_id),
        RouteAcquisitionOperation.target_activity_id == str(handoff.original_rendezvous_activity_id),
    )).all()
    for route in rows:
        evidence = private_route_scope(route.request_json)
        if evidence is None:
            continue
        observation_id = str(evidence.get("location_observation_id") or "")
        if (
            str(evidence.get("route_leg") or "") == "TO_HANDOFF"
            and str(evidence.get("handoff_id") or "") == str(handoff.id)
            and str(evidence.get("journey_member_id") or "") == str(viewer.id)
            and str(evidence.get("traveler_id") or "") == str(principal.traveler_id)
            and str(evidence.get("active_plan_version_id") or "") == str(handoff.active_plan_version_id)
            and str(evidence.get("audience_sha256") or "") == str(handoff.audience_sha256)
            and observation_id
            and str(evidence.get("private_location_binding_id") or "") == observation_id
        ):
            return True
    return False


def current_private_buddy_safe_return(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
) -> dict[str, Any]:
    """Read the current two-leg projection for only the signed pair member."""
    row = _context_member_row(
        session,
        journey=journey,
        viewer=viewer,
        principal=principal,
        handoff_id=handoff_id,
    )
    try:
        handoff, snapshot, activity, handoff_place, original_place = _context(
            session,
            journey=journey,
            viewer=viewer,
            principal=principal,
            handoff_id=handoff_id,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return _stale_result(str(row.id), "R81_HANDOFF_OR_SHARED_STOP_STALE")
    outbound_row, evidence = _latest_private_operation(
        session,
        journey=journey,
        handoff=handoff,
        member=viewer,
        principal=principal,
        place_snapshot=snapshot,
    )
    if outbound_row is None or evidence is None:
        if _has_stored_private_outbound_for_caller(
            session,
            journey=journey,
            handoff=handoff,
            viewer=viewer,
            principal=principal,
        ):
            return _stale_result(str(handoff.id), "R81_PRIVATE_LOCATION_BINDING_STALE")
        return _route_result(
            session,
            journey=journey,
            handoff=handoff,
            handoff_snapshot=snapshot,
            activity=activity,
            handoff_place=handoff_place,
            original_place=original_place,
            outbound_row=None,
            return_row=None,
            observation_age_seconds=0,
        )
    if str(evidence.get("route_leg") or "TO_HANDOFF") != "TO_HANDOFF":
        return _stale_result(str(handoff.id), "R81_OUTBOUND_ROUTE_SCOPE_INVALID")
    observation_id = str(evidence.get("location_observation_id") or "")
    try:
        validated, _source, age = validate_private_location_binding(
            session,
            journey=journey,
            handoff=handoff,
            member=viewer,
            principal=principal,
            observation_id=observation_id,
            place_snapshot=snapshot,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return _stale_result(str(handoff.id), "R81_PRIVATE_LOCATION_BINDING_STALE")
    if str(evidence.get("private_location_binding_id") or observation_id) != str(validated.id):
        return _stale_result(str(handoff.id), "R81_PRIVATE_LOCATION_BINDING_MISMATCH")
    same_place = str(handoff_place.id) == str(original_place.id)
    return_row = None if same_place else _find_return_operation(
        session,
        journey=journey,
        handoff=handoff,
        viewer=viewer,
        principal=principal,
        handoff_snapshot=snapshot,
        original_place=original_place,
        outbound_operation_id=str(outbound_row.id),
        observation_id=observation_id,
    )
    return _route_result(
        session,
        journey=journey,
        handoff=handoff,
        handoff_snapshot=snapshot,
        activity=activity,
        handoff_place=handoff_place,
        original_place=original_place,
        outbound_row=outbound_row,
        return_row=return_row,
        observation_age_seconds=age,
    )
