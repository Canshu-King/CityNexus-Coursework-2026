"""Caller-private routing to a mutually accepted R79 public handoff Place."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.audit import record_audit
from app.concurrency_guards import journey_write_lock
from app.db.models import (
    Activity,
    Journey,
    JourneyMember,
    JourneyRendezvousBuddyHandoff,
    LocationObservation,
    Place,
    PrivateBuddyRouteLocationBinding,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import (
    LIVE_ROUTE_PROVENANCE,
    LOCATION_FRESHNESS_SECONDS,
    MAX_USEFUL_ACCURACY_M,
    REPLAY_ROUTE_PROVENANCE,
    rendezvous_commitment_risk,
)
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE
from app.execution.rendezvous_buddy_handoff import _current_projection
from app.execution.route_session_contract import (
    COMPLETED,
    OUTCOME_ROUTE_UNAVAILABLE,
    ROUTE_PROVIDER_POLICY_VERSION,
    ROUTE_TRUTH_POLICY_VERSION,
    UNAVAILABLE,
    RouteRequest,
    route_request_fingerprint,
)
from app.route_session_operations import (
    find_open_operation,
    find_reusable_completed_operation,
    submit_operation,
)
from app.traveler_principal import TravelerPrincipal

PRIVATE_SCOPE = "R80_PRIVATE_BUDDY_HANDOFF"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return execution_clock.now().astimezone(timezone.utc)


def private_route_scope(request_json: str | None) -> dict[str, Any] | None:
    try:
        request = json.loads(request_json or "{}")
        evidence = request.get("origin_evidence") if isinstance(request, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(evidence, dict) and evidence.get("private_scope") == PRIVATE_SCOPE:
        return evidence
    return None


def _accepted_handoff_context(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
) -> tuple[JourneyRendezvousBuddyHandoff, dict[str, Any], Activity, Place]:
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "R80_SIGNED_MEMBER_MISMATCH")
    row = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == str(handoff_id),
        JourneyRendezvousBuddyHandoff.journey_id == str(journey.id),
    ))
    if row is None:
        raise HTTPException(404, "R80_HANDOFF_NOT_FOUND")
    if str(viewer.id) not in {str(row.helper_member_id), str(row.requester_member_id)}:
        raise HTTPException(403, "R80_HANDOFF_PAIR_MEMBER_REQUIRED")
    if row.status != "ACCEPTED":
        raise HTTPException(409, "R80_HANDOFF_MUST_BE_MUTUALLY_ACCEPTED")
    accepted = _current_projection(session, journey, row=row, viewer=viewer)
    active_execution = _active_execution_projection(
        session, journey=journey, handoff=row, viewer=viewer
    )
    if accepted is None and active_execution is None:
        raise HTTPException(409, "R80_HANDOFF_CONTEXT_STALE")
    try:
        snapshot = json.loads(row.handoff_place_snapshot_json or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(409, "R80_HANDOFF_PLACE_SNAPSHOT_INVALID")
    if not isinstance(snapshot, dict) or str(snapshot.get("place_id") or "") != str(row.handoff_place_id):
        raise HTTPException(409, "R80_HANDOFF_PLACE_SNAPSHOT_INVALID")
    activity = session.get(Activity, str(row.original_rendezvous_activity_id))
    original_place = session.get(Place, str(row.original_public_place_id))
    if (
        activity is None
        or original_place is None
        or str(activity.plan_version_id) != str(row.active_plan_version_id)
        or str(journey.active_plan_version_id or "") != str(row.active_plan_version_id)
    ):
        raise HTTPException(409, "R80_HANDOFF_SHARED_STOP_STALE")
    return row, snapshot, activity, original_place


def _active_execution_projection(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
) -> dict[str, Any] | None:
    """Return a current pair lease even after its R79 offer or signal expires."""
    from app.execution.rendezvous_buddy_execution import execution_for_current_viewer

    projection = execution_for_current_viewer(
        session, journey=journey, handoff=handoff, viewer=viewer
    )
    if (
        projection is None
        or projection.get("status") != "ACTIVE"
        or not bool(projection.get("context_current"))
    ):
        return None
    expiry = _stamp(projection.get("expires_at"))
    if expiry is None or expiry <= _now():
        return None
    return projection


def _validate_observation(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    principal: TravelerPrincipal,
    observation_id: str,
    now: datetime | None = None,
) -> tuple[LocationObservation, str, int]:
    observation = session.get(LocationObservation, str(observation_id))
    if observation is None or str(observation.journey_id) != str(journey.id):
        raise HTTPException(404, "R80_LOCATION_OBSERVATION_NOT_FOUND")
    if (
        str(observation.traveler_id or "") != str(principal.traveler_id)
        or str(observation.journey_member_id or "") != str(member.id)
    ):
        raise HTTPException(403, "R80_LOCATION_OBSERVATION_MEMBER_MISMATCH")
    source = str(observation.source or "").upper()
    # A private handoff route never borrows a general foreground/device sample.
    # R81 requires the exact capture-time authorization receipt below; legacy
    # sources have no receipt and cannot be upgraded by a later request.
    if source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE:
        raise HTTPException(409, "R81_PRIVATE_LOCATION_BINDING_REQUIRED")
    captured = _stamp(observation.captured_at)
    if captured is None:
        raise HTTPException(409, "R80_LOCATION_CAPTURE_TIME_UNKNOWN")
    reference = _now() if now is None else now.astimezone(timezone.utc)
    age_seconds = (reference - captured).total_seconds()
    if age_seconds < -30 or age_seconds > LOCATION_FRESHNESS_SECONDS:
        raise HTTPException(409, "R80_LOCATION_OBSERVATION_STALE")
    if float(observation.accuracy_m) > MAX_USEFUL_ACCURACY_M:
        raise HTTPException(409, "R80_LOCATION_OBSERVATION_IMPRECISE")
    return observation, source, max(0, int(age_seconds))


def _destination_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_json(snapshot).encode("utf-8")).hexdigest()


def _binding_expiry(
    *,
    captured_at: datetime,
    handoff: JourneyRendezvousBuddyHandoff,
    execution_expires_at: datetime | None = None,
) -> datetime:
    consent_expires = execution_expires_at or _stamp(handoff.expires_at)
    if consent_expires is None:
        raise HTTPException(409, "R81_PRIVATE_LOCATION_HANDOFF_EXPIRY_UNKNOWN")
    return min(
        captured_at.astimezone(timezone.utc) + timedelta(seconds=LOCATION_FRESHNESS_SECONDS),
        consent_expires,
    )


def _private_location_binding_for_context(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    place_snapshot: dict[str, Any],
    observation_id: str,
    now: datetime | None = None,
) -> tuple[LocationObservation, str, int]:
    observation, source, age = _validate_observation(
        session,
        journey=journey,
        member=viewer,
        principal=principal,
        observation_id=observation_id,
        now=now,
    )
    binding = session.get(PrivateBuddyRouteLocationBinding, str(observation.id))
    captured = _stamp(observation.captured_at)
    accepted_at = _stamp(handoff.responded_at)
    expiry = _stamp(binding.expires_at) if binding is not None else None
    reference = _now() if now is None else now.astimezone(timezone.utc)
    active_execution = _active_execution_projection(
        session, journey=journey, handoff=handoff, viewer=viewer
    )
    execution_expiry = (
        _stamp(active_execution.get("expires_at")) if active_execution is not None else None
    )
    expected_expiry = _binding_expiry(
        captured_at=captured,
        handoff=handoff,
        execution_expires_at=execution_expiry,
    ) if captured else None
    active_member = viewer.removed_at is None
    if viewer.expires_at:
        member_expiry = _stamp(viewer.expires_at)
        active_member = active_member and member_expiry is not None and member_expiry > reference
    if not active_member:
        raise HTTPException(409, "R81_PRIVATE_LOCATION_MEMBER_INACTIVE")
    if (
        binding is None
        or handoff.status != "ACCEPTED"
        or accepted_at is None
        or str(binding.observation_id) != str(observation.id)
        or str(binding.journey_id) != str(journey.id)
        or str(binding.handoff_id) != str(handoff.id)
        or str(binding.journey_member_id) != str(viewer.id)
        or str(binding.traveler_id) != str(principal.traveler_id)
        or str(binding.active_plan_version_id) != str(handoff.active_plan_version_id)
        or str(binding.current_activity_id or "") != str(handoff.current_activity_id or "")
        or str(binding.destination_place_id) != str(handoff.handoff_place_id)
        or str(binding.destination_place_snapshot_sha256)
            != _destination_snapshot_sha256(place_snapshot)
        or str(binding.audience_sha256) != str(handoff.audience_sha256)
        or _stamp(binding.handoff_accepted_at) != accepted_at
        or _stamp(binding.captured_at) != captured
        or expiry is None
        or expected_expiry is None
        or expiry != expected_expiry
        or expiry <= reference
    ):
        raise HTTPException(409, "R81_PRIVATE_LOCATION_BINDING_CONTEXT_MISMATCH")
    return observation, source, age


def validate_private_location_binding(
    session: Session,
    *,
    journey: Journey,
    principal: TravelerPrincipal,
    observation_id: str,
    handoff: JourneyRendezvousBuddyHandoff | None = None,
    member: JourneyMember | None = None,
    place_snapshot: dict[str, Any] | None = None,
    handoff_id: str | None = None,
    viewer: JourneyMember | None = None,
    now: datetime | None = None,
) -> tuple[LocationObservation, str, int]:
    """Revalidate an observation against one exact, currently accepted handoff.

    Callers that add another private route surface should use this instead of
    passing an observation UUID through the older generic location validator.
    The explicit handoff_id re-checks signed principal, pair, active plan/cursor,
    destination snapshot, acceptance and expiry on every use.
    """

    signed_member = member or viewer
    exact_handoff_id = str(handoff_id or (handoff.id if handoff is not None else ""))
    if signed_member is None or not exact_handoff_id:
        raise HTTPException(422, "R81_PRIVATE_LOCATION_EXACT_HANDOFF_REQUIRED")
    current_handoff, snapshot, _activity, _original = _accepted_handoff_context(
        session,
        journey=journey,
        viewer=signed_member,
        principal=principal,
        handoff_id=exact_handoff_id,
    )
    if handoff is not None and str(handoff.id) != str(current_handoff.id):
        raise HTTPException(409, "R81_PRIVATE_LOCATION_HANDOFF_CONTEXT_MISMATCH")
    if (
        place_snapshot is not None
        and _destination_snapshot_sha256(place_snapshot) != _destination_snapshot_sha256(snapshot)
    ):
        raise HTTPException(409, "R81_PRIVATE_LOCATION_DESTINATION_CONTEXT_MISMATCH")
    return _private_location_binding_for_context(
        session,
        journey=journey,
        handoff=current_handoff,
        viewer=signed_member,
        principal=principal,
        place_snapshot=snapshot,
        observation_id=observation_id,
        now=now,
    )


def capture_private_buddy_route_location(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
    latitude: float,
    longitude: float,
    accuracy_m: float,
    captured_at: datetime,
) -> dict[str, Any]:
    handoff, snapshot, _activity, _original = _accepted_handoff_context(
        session, journey=journey, viewer=viewer, principal=principal, handoff_id=handoff_id
    )
    if captured_at.tzinfo is None:
        raise HTTPException(422, "R80_LOCATION_CAPTURE_TIMEZONE_REQUIRED")
    age = (_now() - captured_at.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > LOCATION_FRESHNESS_SECONDS:
        raise HTTPException(409, "R80_LOCATION_OBSERVATION_STALE")
    if accuracy_m > MAX_USEFUL_ACCURACY_M:
        raise HTTPException(409, "R80_LOCATION_OBSERVATION_IMPRECISE")
    captured = captured_at.astimezone(timezone.utc)
    active_execution = _active_execution_projection(
        session, journey=journey, handoff=handoff, viewer=viewer
    )
    execution_expiry = (
        _stamp(active_execution.get("expires_at")) if active_execution is not None else None
    )
    binding_expires = _binding_expiry(
        captured_at=captured,
        handoff=handoff,
        execution_expires_at=execution_expiry,
    )
    bound_at = _now()
    if binding_expires <= bound_at:
        raise HTTPException(409, "R81_PRIVATE_LOCATION_BINDING_ALREADY_EXPIRED")
    accepted_at = _stamp(handoff.responded_at)
    if accepted_at is None:
        raise HTTPException(409, "R81_PRIVATE_LOCATION_ACCEPTANCE_TIME_UNKNOWN")
    row = LocationObservation(
        id=str(uuid4()),
        journey_id=str(journey.id),
        traveler_id=str(principal.traveler_id),
        journey_member_id=str(viewer.id),
        latitude=float(latitude),
        longitude=float(longitude),
        accuracy_m=float(accuracy_m),
        captured_at=captured.isoformat(),
        source=PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
    )
    session.add(row)
    session.add(PrivateBuddyRouteLocationBinding(
        observation_id=str(row.id),
        journey_id=str(journey.id),
        handoff_id=str(handoff.id),
        journey_member_id=str(viewer.id),
        traveler_id=str(principal.traveler_id),
        active_plan_version_id=str(handoff.active_plan_version_id),
        current_activity_id=str(handoff.current_activity_id) if handoff.current_activity_id else None,
        destination_place_id=str(handoff.handoff_place_id),
        destination_place_snapshot_sha256=_destination_snapshot_sha256(snapshot),
        audience_sha256=str(handoff.audience_sha256),
        handoff_accepted_at=accepted_at.isoformat(),
        captured_at=captured.isoformat(),
        expires_at=binding_expires.isoformat(),
        bound_at=bound_at.isoformat(),
    ))
    record_audit(
        session,
        action="journey.private_location.capture_recorded",
        entity_type="PrivateLocationCapture",
        entity_id=None,
        journey_id=str(journey.id),
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=str(principal.traveler_id),
        after={
            "source": PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
            "visibility": "CALLER_PRIVATE",
            "external_provider_contacted": False,
        },
    )
    session.commit()
    return {
        "location_observation_id": str(row.id),
        "journey_id": str(journey.id),
        "source": PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        "principal_bound": True,
        "visibility": "CALLER_PRIVATE",
        "external_provider_contacted": False,
    }


def _route_request(
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    place_snapshot: dict[str, Any],
    member: JourneyMember,
    principal: TravelerPrincipal,
    observation: LocationObservation,
    source: str,
) -> RouteRequest:
    target_snapshot_hash = hashlib.sha256(_json(place_snapshot).encode("utf-8")).hexdigest()
    return RouteRequest(
        journey_id=str(journey.id),
        origin_key=f"location-observation:{observation.id}",
        target_key=str(handoff.handoff_place_id),
        target_activity_id=str(handoff.original_rendezvous_activity_id),
        requested_mode="WALK",
        provider_policy_version=ROUTE_PROVIDER_POLICY_VERSION,
        route_truth_policy_version=ROUTE_TRUTH_POLICY_VERSION,
        origin_evidence={
            "location_observation_id": str(observation.id),
            # The binding uses observation_id as its immutable primary key.
            "private_location_binding_id": str(observation.id),
            "route_leg": "TO_HANDOFF",
            "journey_member_id": str(member.id),
            "traveler_id": str(principal.traveler_id),
            "source": source,
            "captured_at": str(observation.captured_at),
            "accuracy_m": float(observation.accuracy_m),
            "authority": (
                "TRUSTED_MEMBER_LOCATION_ROUTE"
                if source in {"DEVICE_ATTESTED", "LOCATION_ATTESTED"}
                else "ADVISORY_FOREGROUND_LOCATION"
            ),
            "private_scope": PRIVATE_SCOPE,
            "handoff_id": str(handoff.id),
            "active_plan_version_id": str(handoff.active_plan_version_id),
            "current_activity_id": str(handoff.current_activity_id or ""),
            "audience_sha256": str(handoff.audience_sha256),
            "target_place_snapshot_sha256": target_snapshot_hash,
            "external_provider_disclosure_confirmed": True,
        },
    )


def _record_unconfigured_route(
    session: Session,
    journey: Journey,
    route_request: RouteRequest,
) -> RouteAcquisitionOperation:
    """Durably say UNKNOWN without consulting mock data or fabricating a provider call."""
    fingerprint = route_request_fingerprint(route_request)
    existing = session.scalar(select(RouteAcquisitionOperation).where(
        RouteAcquisitionOperation.journey_id == journey.id,
        RouteAcquisitionOperation.fingerprint == fingerprint,
    ).order_by(RouteAcquisitionOperation.queued_at.desc()).limit(1))
    if existing is not None:
        return existing
    stamp = _now().isoformat()
    row = RouteAcquisitionOperation(
        id=str(uuid4()),
        journey_id=str(journey.id),
        fingerprint=fingerprint,
        origin_key=route_request.origin_key,
        target_key=route_request.target_key,
        target_activity_id=route_request.target_activity_id,
        requested_mode="WALK",
        provider_policy_version=route_request.provider_policy_version,
        route_truth_policy_version=route_request.route_truth_policy_version,
        status=UNAVAILABLE,
        phase="COMPLETE",
        outcome=OUTCOME_ROUTE_UNAVAILABLE,
        request_json=_json(route_request.semantic_payload()),
        result_json=_json({
            "provider": None,
            "provenance": "NONE",
            "attempt_count": 0,
            "captured_at": stamp,
        }),
        error_json=_json({
            "classification": OUTCOME_ROUTE_UNAVAILABLE,
            "reason": "ROUTE_PROVIDER_NOT_CONFIGURED",
        }),
        queued_at=stamp,
        started_at=None,
        finished_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.commit()
    return row


def _load_result(row: RouteAcquisitionOperation) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result = json.loads(row.result_json or "{}")
        error = json.loads(row.error_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}, {}
    return (
        result if isinstance(result, dict) else {},
        error if isinstance(error, dict) else {},
    )


def _provider_receipt_persisted(
    session: Session,
    *,
    operation: RouteAcquisitionOperation,
    provider: str,
    provenance: str,
    duration_min: int,
) -> bool:
    """A LIVE badge needs the matching successful provider capture, not any call.

    ProviderCall is an internal persisted receipt, not a user-supplied claim;
    still bind it to this *exact private request*, operation, measured duration,
    provider and provenance. A mock or a failed/cross-request receipt cannot
    upgrade a fabricated operation result into LIVE.
    """
    if provider.casefold() != "valhalla-http" or provenance not in LIVE_ROUTE_PROVENANCE:
        return False
    receipts = session.scalars(select(ProviderCall).where(
        ProviderCall.operation == "ROUTE_ACQUISITION",
        ProviderCall.provider_key == "valhalla",
        ProviderCall.status == "SUCCESS",
        ProviderCall.request_json == operation.request_json,
    )).all()
    for receipt in receipts:
        try:
            response = json.loads(receipt.response_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(response, dict):
            continue
        if (
            str(response.get("operation_id") or "") == str(operation.id)
            and str(response.get("provider") or "").casefold() == provider.casefold()
            and str(response.get("provenance") or "").upper() == provenance
            and response.get("duration_min") == duration_min
        ):
            return True
    return False


def _commitment_risk(
    session: Session,
    journey: Journey,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    activity: Activity,
    duration_min: int,
    authority: str,
) -> dict[str, Any]:
    risk = rendezvous_commitment_risk(
        session,
        journey,
        activity,
        route_duration_min=duration_min,
        authority=authority,
    )
    if str(handoff.handoff_place_id) == str(handoff.original_public_place_id):
        return risk
    # The route stops at the private handoff. A positive buffer there cannot
    # establish that the traveller can also reach the original shared stop.
    if risk.get("state") in {"WITHIN_BUFFER", "LOW_BUFFER"}:
        return {
            "state": "UNKNOWN",
            "authority": authority,
            "next_commitment": risk.get("next_commitment"),
            "slack_after_buffer_min": None,
            "basis": "ONWARD_ROUTE_FROM_PRIVATE_HANDOFF_TO_SHARED_STOP_UNKNOWN",
        }
    if risk.get("state") == "COMMITMENT_AT_RISK":
        return {
            "state": "COMMITMENT_AT_RISK",
            "authority": authority,
            "next_commitment": risk.get("next_commitment"),
            "slack_after_buffer_min": risk.get("slack_after_buffer_min"),
            "basis": "ROUTE_TO_PRIVATE_HANDOFF_ALREADY_EXCEEDS_SHARED_COMMITMENT",
        }
    return risk


def _project_operation(
    session: Session,
    *,
    row: RouteAcquisitionOperation,
    handoff: JourneyRendezvousBuddyHandoff,
    place_snapshot: dict[str, Any],
    activity: Activity,
    journey: Journey,
    observation_age_seconds: int,
) -> dict[str, Any]:
    result, error = _load_result(row)
    provider = str(result.get("provider") or "")
    provenance = str(result.get("provenance") or result.get("route_provenance") or "").upper()
    duration = result.get("duration_min")
    try:
        duration_value = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None
    route_stamp = _stamp(result.get("captured_at") or row.finished_at)
    route_age = max(0, int((_now() - route_stamp).total_seconds())) if route_stamp else None
    route_state = "UNKNOWN"
    reason = str(error.get("reason") or error.get("classification") or row.outcome or "ROUTE_UNAVAILABLE")
    is_non_product_provider = provider.casefold().startswith(("mock", "fixture", "synthetic"))
    route_is_fresh = route_age is not None and route_age <= LOCATION_FRESHNESS_SECONDS
    if (
        row.status == COMPLETED
        and duration_value is not None
        and duration_value >= 0
        and not is_non_product_provider
        and route_is_fresh
    ):
        if provenance in LIVE_ROUTE_PROVENANCE and _provider_receipt_persisted(
            session,
            operation=row,
            provider=provider,
            provenance=provenance,
            duration_min=duration_value,
        ):
            route_state = "LIVE"
            reason = ""
        elif provenance in REPLAY_ROUTE_PROVENANCE:
            route_state = "REPLAY"
            reason = "CAPTURED_HISTORICAL_ROUTE_NOT_CURRENT_TRAFFIC"
    authority = str(private_route_scope(row.request_json).get("authority") or "ADVISORY_FOREGROUND_LOCATION") if private_route_scope(row.request_json) else "ADVISORY_FOREGROUND_LOCATION"
    risk = (
        _commitment_risk(
            session,
            journey,
            handoff=handoff,
            activity=activity,
            duration_min=duration_value,
            authority=authority,
        )
        if route_state in {"LIVE", "REPLAY"} and duration_value is not None
        else {"state": "UNKNOWN", "authority": authority, "next_commitment": None, "slack_after_buffer_min": None}
    )
    return {
        "handoff_id": str(handoff.id),
        "route_state": route_state,
        "status": str(row.status),
        "reason": reason,
        "duration_min": duration_value if route_state in {"LIVE", "REPLAY"} else None,
        "provider": provider or None,
        "provenance": provenance or "UNKNOWN",
        "captured_at": str(result.get("captured_at") or row.finished_at or "") or None,
        "route_age_seconds": route_age,
        "origin_age_seconds": observation_age_seconds,
        "authority": authority,
        "target_place_name": str(place_snapshot.get("name") or "Public handoff place"),
        "target_source_label": str(place_snapshot.get("source_label") or "Public catalog record"),
        "target_evidence_grade": str(place_snapshot.get("evidence_grade") or "UNKNOWN"),
        "original_shared_stop_name": str(
            session.get(Place, str(handoff.original_public_place_id)).name
            if session.get(Place, str(handoff.original_public_place_id)) else "Original shared stop"
        ),
        "shared_commitment_risk": risk,
        "shared_plan_changed": False,
        "physical_arrival_verified": False,
        "guardian_triggered": False,
    }


def _operation_matches_context(
    row: RouteAcquisitionOperation,
    *,
    session: Session,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    member: JourneyMember,
    principal: TravelerPrincipal,
    place_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    evidence = private_route_scope(row.request_json)
    if evidence is None:
        return None
    expected_place_hash = _destination_snapshot_sha256(place_snapshot)
    if not (
        str(evidence.get("handoff_id") or "") == str(handoff.id)
        and str(evidence.get("journey_member_id") or "") == str(member.id)
        and str(evidence.get("traveler_id") or "") == str(principal.traveler_id)
        and str(evidence.get("active_plan_version_id") or "") == str(handoff.active_plan_version_id)
        and str(evidence.get("current_activity_id") or "") == str(handoff.current_activity_id or "")
        and str(evidence.get("audience_sha256") or "") == str(handoff.audience_sha256)
        and str(evidence.get("target_place_snapshot_sha256") or "") == expected_place_hash
        and str(evidence.get("route_leg") or "") == "TO_HANDOFF"
        and str(evidence.get("private_location_binding_id") or "")
            == str(evidence.get("location_observation_id") or "")
        and str(row.target_key) == str(handoff.handoff_place_id)
        and str(row.target_activity_id or "") == str(handoff.original_rendezvous_activity_id)
    ):
        return None
    observation_id = str(evidence.get("location_observation_id") or "")
    try:
        observation, source, _age = _private_location_binding_for_context(
            session,
            journey=journey,
            handoff=handoff,
            viewer=member,
            principal=principal,
            place_snapshot=place_snapshot,
            observation_id=observation_id,
        )
    except HTTPException:
        return None
    if (
        source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE
        or str(observation.captured_at) != str(evidence.get("captured_at") or "")
        or abs(float(observation.accuracy_m) - float(evidence.get("accuracy_m") or -1)) > 0.001
        or not bool(evidence.get("external_provider_disclosure_confirmed"))
    ):
        return None
    return evidence


def _latest_private_operation(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    member: JourneyMember,
    principal: TravelerPrincipal,
    place_snapshot: dict[str, Any],
) -> tuple[RouteAcquisitionOperation | None, dict[str, Any] | None]:
    rows = session.scalars(select(RouteAcquisitionOperation).where(
        RouteAcquisitionOperation.journey_id == str(journey.id),
        RouteAcquisitionOperation.target_key == str(handoff.handoff_place_id),
        RouteAcquisitionOperation.target_activity_id == str(handoff.original_rendezvous_activity_id),
    ).order_by(RouteAcquisitionOperation.queued_at.desc(), RouteAcquisitionOperation.id.desc())).all()
    for row in rows:
        evidence = _operation_matches_context(
            row,
            session=session,
            journey=journey,
            handoff=handoff,
            member=member,
            principal=principal,
            place_snapshot=place_snapshot,
        )
        if evidence is not None:
            return row, evidence
    return None, None


def current_private_route(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
) -> dict[str, Any]:
    handoff, snapshot, activity, _original = _accepted_handoff_context(
        session, journey=journey, viewer=viewer, principal=principal, handoff_id=handoff_id
    )
    operation, evidence = _latest_private_operation(
        session,
        journey=journey,
        handoff=handoff,
        member=viewer,
        principal=principal,
        place_snapshot=snapshot,
    )
    if operation is None or evidence is None:
        return {
            "handoff_id": str(handoff.id),
            "route_state": "UNKNOWN",
            "status": "NOT_REQUESTED",
            "reason": "NO_PRIVATE_ROUTE_REQUEST",
            "duration_min": None,
            "provider": None,
            "provenance": "UNKNOWN",
            "captured_at": None,
            "route_age_seconds": None,
            "origin_age_seconds": None,
            "authority": "UNKNOWN",
            "target_place_name": str(snapshot.get("name") or "Public handoff place"),
            "target_source_label": str(snapshot.get("source_label") or "Public catalog record"),
            "target_evidence_grade": str(snapshot.get("evidence_grade") or "UNKNOWN"),
            "original_shared_stop_name": str(_original.name),
            "shared_commitment_risk": {"state": "UNKNOWN", "next_commitment": None},
            "shared_plan_changed": False,
            "physical_arrival_verified": False,
            "guardian_triggered": False,
        }
    try:
        observation, _source, age = _private_location_binding_for_context(
            session,
            journey=journey,
            handoff=handoff,
            viewer=viewer,
            principal=principal,
            place_snapshot=snapshot,
            observation_id=str(evidence.get("location_observation_id") or ""),
        )
    except HTTPException:
        return {
            "handoff_id": str(handoff.id),
            "route_state": "UNKNOWN",
            "status": "STALE",
            "reason": "PRIVATE_ROUTE_ORIGIN_STALE",
            "duration_min": None,
            "provider": None,
            "provenance": "UNKNOWN",
            "captured_at": None,
            "route_age_seconds": None,
            "origin_age_seconds": None,
            "authority": "UNKNOWN",
            "target_place_name": str(snapshot.get("name") or "Public handoff place"),
            "target_source_label": str(snapshot.get("source_label") or "Public catalog record"),
            "target_evidence_grade": str(snapshot.get("evidence_grade") or "UNKNOWN"),
            "original_shared_stop_name": str(_original.name),
            "shared_commitment_risk": {"state": "UNKNOWN", "next_commitment": None},
            "shared_plan_changed": False,
            "physical_arrival_verified": False,
            "guardian_triggered": False,
        }
    # Keep the row retained for audit, but a route tied to changed ownership or capture facts is
    # never an actionable current result.
    if (
        str(observation.captured_at) != str(evidence.get("captured_at") or "")
        or abs(float(observation.accuracy_m) - float(evidence.get("accuracy_m") or -1)) > 0.001
        or not bool(evidence.get("external_provider_disclosure_confirmed"))
    ):
        raise HTTPException(409, "R80_PRIVATE_ROUTE_EVIDENCE_CHANGED")
    return _project_operation(
        session,
        row=operation,
        handoff=handoff,
        place_snapshot=snapshot,
        activity=activity,
        journey=journey,
        observation_age_seconds=age,
    )


def acquire_private_buddy_route(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    handoff_id: str,
    observation_id: str,
    external_provider_disclosure_confirmed: bool,
) -> dict[str, Any]:
    if not external_provider_disclosure_confirmed:
        raise HTTPException(400, "R80_EXTERNAL_PROVIDER_DISCLOSURE_CONFIRMATION_REQUIRED")
    handoff, snapshot, activity, _original = _accepted_handoff_context(
        session, journey=journey, viewer=viewer, principal=principal, handoff_id=handoff_id
    )
    observation, source, _age = _private_location_binding_for_context(
        session,
        journey=journey,
        handoff=handoff,
        viewer=viewer,
        principal=principal,
        place_snapshot=snapshot,
        observation_id=observation_id,
    )
    place = session.get(Place, str(handoff.handoff_place_id))
    if place is None:
        raise HTTPException(409, "R80_HANDOFF_PLACE_UNAVAILABLE")
    try:
        stored_coordinates = (
            float(snapshot["latitude"]),
            float(snapshot["longitude"]),
        )
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "R80_HANDOFF_PLACE_COORDINATES_UNAVAILABLE")
    if (
        str(snapshot.get("coordinate_system") or "").upper() != "WGS84"
        or (float(place.latitude), float(place.longitude)) != stored_coordinates
    ):
        raise HTTPException(409, "R80_HANDOFF_PLACE_COORDINATES_CHANGED")
    route_request = _route_request(
        journey=journey,
        handoff=handoff,
        place_snapshot=snapshot,
        member=viewer,
        principal=principal,
        observation=observation,
        source=source,
    )
    route, reused = (
        submit_operation(session, journey, route_request)
        if settings.live_valhalla_url
        else (_record_unconfigured_route(session, journey, route_request), True)
    )
    del reused  # only the caller-private, sanitized route projection crosses this boundary
    # Provider work deliberately runs outside this process-local Journey lock so a
    # revoke can complete while it is pending. Recheck under the lock before an
    # actionable result leaves this request, closing the release/revoke race.
    with journey_write_lock(str(journey.id)):
        session.expire_all()
        fresh_journey = session.get(Journey, str(journey.id))
        fresh_viewer = session.get(JourneyMember, str(viewer.id))
        if fresh_journey is None or fresh_viewer is None:
            raise HTTPException(409, "R80_ROUTE_RESULT_CONTEXT_CHANGED")
        fresh_handoff, fresh_snapshot, fresh_activity, _fresh_original = _accepted_handoff_context(
            session,
            journey=fresh_journey,
            viewer=fresh_viewer,
            principal=principal,
            handoff_id=handoff_id,
        )
        evidence = _operation_matches_context(
            route,
            session=session,
            journey=fresh_journey,
            handoff=fresh_handoff,
            member=fresh_viewer,
            principal=principal,
            place_snapshot=fresh_snapshot,
        )
        if evidence is None or str(evidence.get("location_observation_id") or "") != str(observation_id):
            raise HTTPException(409, "R80_ROUTE_RESULT_CONTEXT_CHANGED")
        fresh_observation, _fresh_source, age = _private_location_binding_for_context(
            session,
            journey=fresh_journey,
            handoff=fresh_handoff,
            viewer=fresh_viewer,
            principal=principal,
            place_snapshot=fresh_snapshot,
            observation_id=observation_id,
        )
        if str(fresh_observation.captured_at) != str(evidence.get("captured_at") or ""):
            raise HTTPException(409, "R80_ROUTE_RESULT_ORIGIN_STALE")
        return _project_operation(
            session,
            row=route,
            handoff=fresh_handoff,
            place_snapshot=fresh_snapshot,
            activity=fresh_activity,
            journey=fresh_journey,
            observation_age_seconds=age,
        )


def read_private_route_operation(
    session: Session,
    *,
    journey: Journey,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    operation: RouteAcquisitionOperation,
) -> dict[str, Any]:
    evidence = private_route_scope(operation.request_json)
    if evidence is None:
        raise HTTPException(404, "R80_PRIVATE_ROUTE_NOT_FOUND")
    handoff_id = str(evidence.get("handoff_id") or "")
    handoff, snapshot, activity, _original = _accepted_handoff_context(
        session,
        journey=journey,
        viewer=viewer,
        principal=principal,
        handoff_id=handoff_id,
    )
    matched = _operation_matches_context(
        operation,
        session=session,
        journey=journey,
        handoff=handoff,
        member=viewer,
        principal=principal,
        place_snapshot=snapshot,
    )
    if matched is None:
        raise HTTPException(404, "R80_PRIVATE_ROUTE_NOT_FOUND")
    observation, _source, age = _private_location_binding_for_context(
        session,
        journey=journey,
        handoff=handoff,
        viewer=viewer,
        principal=principal,
        place_snapshot=snapshot,
        observation_id=str(matched.get("location_observation_id") or ""),
    )
    if str(observation.captured_at) != str(matched.get("captured_at") or ""):
        raise HTTPException(404, "R80_PRIVATE_ROUTE_NOT_FOUND")
    return _project_operation(
        session,
        row=operation,
        handoff=handoff,
        place_snapshot=snapshot,
        activity=activity,
        journey=journey,
        observation_age_seconds=age,
    )
