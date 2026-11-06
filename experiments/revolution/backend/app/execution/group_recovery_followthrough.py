"""Member-bound follow-through for an applied shared recovery.

The accepted recovery audit binds the incident to one active PlanVersion.  Member
arrival is read from the existing authenticated Activity execution-truth history;
this module does not create a parallel arrival endpoint or infer physical presence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Activity,
    AuditLog,
    Event,
    IdempotencyKey,
    Journey,
    JourneyMember,
    JourneyRoleCapability,
    Place,
    Traveler,
)
from app.execution.group_presence import rendezvous_target


REJOINED_ACTION = "journey.group_recovery.rejoined"
AUTHENTICATED_ARRIVAL_KINDS = {
    "OPERATOR_CONFIRMED_EXECUTION",
    "DEVICE_LOCATION_VERIFIED_EXECUTION",
}


def _json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _member_in_force(member: JourneyMember, now: datetime) -> bool:
    if member.removed_at:
        return False
    if not member.expires_at:
        return True
    try:
        expires = datetime.fromisoformat(member.expires_at)
    except ValueError:
        return False
    return expires.tzinfo is not None and expires.astimezone(timezone.utc) > now


def build_followthrough_snapshot(
    session: Session,
    journey: Journey,
    *,
    incident_key: str,
    proposal_id: str,
    plan_version_id: str,
    recovery_epoch_id: str,
    authority_receipt_id: str | None,
    trigger_event_id: str | None,
    applied_at: str,
) -> dict[str, Any]:
    """Bind follow-through to the exact post-apply plan and in-force member set."""
    now = datetime.now(timezone.utc)
    members = [
        member
        for member in session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey.id)
        ).all()
        if _member_in_force(member, now)
    ]
    target_pair = rendezvous_target(session, journey)
    reason = None
    target_activity = target_place = None
    if target_pair is None:
        reason = "RENDEZVOUS_ACTIVITY_UNAVAILABLE"
    else:
        target_activity, target_place = target_pair
    if len(members) < 2:
        reason = "SHARED_MEMBER_SET_UNAVAILABLE"
    return {
        "state": "RECOVERY_APPLIED_REJOIN_PENDING" if reason is None else "REJOIN_CONFIRMATION_REQUIRED",
        "reason": reason,
        "journey_id": str(journey.id),
        "incident_key": str(incident_key),
        "applied_proposal_id": str(proposal_id),
        "applied_plan_version_id": str(plan_version_id),
        "recovery_epoch_id": str(recovery_epoch_id),
        "authority_receipt_id": str(authority_receipt_id or "") or None,
        "trigger_event_id": str(trigger_event_id or "") or None,
        "required_member_ids": sorted(str(member.id) for member in members),
        "member_roles_at_apply": {str(member.id): str(member.role) for member in members},
        "rendezvous_activity_id": str(target_activity.id) if target_activity else None,
        "rendezvous_place_id": str(target_place.id) if target_place else None,
        "rendezvous_title": str(target_activity.title) if target_activity else None,
        "applied_at": str(applied_at),
        "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
        "physical_presence_verified": False,
    }


def active_group_recovery_spend(
    session: Session, journey: Journey
) -> tuple[AuditLog, dict[str, Any], dict[str, Any]] | None:
    """Return the latest applied recovery that still owns the active plan."""
    for accepted in session.scalars(
        select(AuditLog)
        .where(AuditLog.journey_id == journey.id, AuditLog.action == "replan.accepted")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).all():
        after = _json(accepted.after_json)
        spend = after.get("group_recovery_spend")
        if not (
            isinstance(spend, dict)
            and spend.get("kind") == "COLLECTIVE_GROUP_RECOVERY_APPLY"
            and spend.get("result") == "APPLIED"
            and str(spend.get("after_plan_version_id") or "")
            == str(journey.active_plan_version_id or "")
        ):
            continue
        followthrough = spend.get("followthrough")
        if isinstance(followthrough, dict):
            return accepted, spend, followthrough
    return None


def _activity_execution_history(activity: Activity | None) -> list[dict[str, Any]]:
    if activity is None:
        return []
    metadata = _json(activity.metadata_json)
    history = metadata.get("execution_truth_history")
    if not isinstance(history, list):
        history = []
    current = metadata.get("execution_truth")
    records = [item for item in history if isinstance(item, dict)]
    if isinstance(current, dict) and current not in records:
        records.append(current)
    return records


def _member_arrival_records(
    activity: Activity | None,
    members: dict[str, JourneyMember],
    required_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Read movement hints from Activity history.

    ARRIVED history is retained as a compatibility fallback, but R62 durable
    member-bound receipts are the authoritative reunion progress source. A
    shared Activity metadata JSON document can be concurrently rewritten by
    multiple travellers and must not be the sole record that two people
    arrived.
    """
    arrivals: dict[str, dict[str, Any]] = {}
    moving: set[str] = set()
    for item in _activity_execution_history(activity):
        evidence = item.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        member_id = str(evidence.get("member_id") or "")
        member = members.get(member_id)
        if (
            member_id not in required_ids
            or member is None
            or str(item.get("actor_type") or "") != "AUTHENTICATED_TRAVELER"
            or str(item.get("actor_id") or "") != str(member.traveler_id)
            or str(evidence.get("member_id") or "") != str(member.id)
            or item.get("kind") not in AUTHENTICATED_ARRIVAL_KINDS
        ):
            continue
        transition = str(item.get("transition") or "").upper()
        if transition == "EN_ROUTE":
            moving.add(member_id)
        elif transition == "ARRIVED":
            arrivals[member_id] = item
            moving.discard(member_id)
    return arrivals, moving


def _durable_member_arrival_receipts(
    session: Session,
    *,
    journey: Journey,
    snapshot: dict[str, Any],
    members: dict[str, JourneyMember],
    required_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Return verified per-member R62 receipts for this exact applied recovery.

    These rows are written in the same transaction as the authenticated
    execution truth. They are member-scoped and do not share one mutable JSON
    document, so they survive concurrent member progress without last-writer
    metadata loss.
    """
    incident_key = str(snapshot.get("incident_key") or "")
    plan_id = str(snapshot.get("applied_plan_version_id") or "")
    epoch_id = str(snapshot.get("recovery_epoch_id") or "")
    rendezvous_activity_id = str(snapshot.get("rendezvous_activity_id") or "")
    receipts: dict[str, dict[str, Any]] = {}
    for row in session.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.operation == "group_recovery.execution_arrival"
        )
    ).all():
        try:
            item = json.loads(row.response_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        member_id = str(item.get("journey_member_id") or "")
        member = members.get(member_id)
        if (
            member_id not in required_ids
            or member is None
            or str(item.get("journey_id") or "") != str(journey.id)
            or str(item.get("incident_key") or "") != incident_key
            or str(item.get("applied_plan_version_id") or "") != plan_id
            or str(item.get("recovery_epoch_id") or "") != epoch_id
            or str(item.get("rendezvous_activity_id") or "") != rendezvous_activity_id
            or str(item.get("traveler_id") or "") != str(member.traveler_id)
            or str(item.get("role_at_confirmation") or "") != str(
                (snapshot.get("member_roles_at_apply") or {}).get(member_id) or ""
            )
            or str(item.get("execution_truth_kind") or "")
            not in AUTHENTICATED_ARRIVAL_KINDS
        ):
            continue
        receipts[member_id] = {
            **item,
            "kind": item.get("execution_truth_kind"),
            "captured_at": item.get("confirmed_at"),
            "source": "DURABLE_GROUP_RECOVERY_EXECUTION_ARRIVAL",
        }
    return receipts


def followthrough_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None,
) -> dict[str, Any] | None:
    found = active_group_recovery_spend(session, journey)
    if found is None:
        return None
    _accepted, _spend, snapshot = found
    incident_key = str(snapshot.get("incident_key") or "")
    plan_id = str(snapshot.get("applied_plan_version_id") or "")
    epoch_id = str(snapshot.get("recovery_epoch_id") or "")
    if not incident_key or not plan_id or plan_id != str(journey.active_plan_version_id or ""):
        return None

    all_members = session.scalars(
        select(JourneyMember).where(JourneyMember.journey_id == journey.id)
    ).all()
    now = datetime.now(timezone.utc)
    member_by_id = {str(member.id): member for member in all_members}
    original_ids = {str(item) for item in snapshot.get("required_member_ids", []) if item}
    active_original = {
        member_id
        for member_id in original_ids
        if member_id in member_by_id and _member_in_force(member_by_id[member_id], now)
    }
    current_ids = {
        str(member.id) for member in all_members if _member_in_force(member, now)
    }
    added_ids = sorted(current_ids - original_ids)
    changed_role_ids = sorted(
        member_id
        for member_id in active_original
        if str(member_by_id[member_id].role)
        != str((snapshot.get("member_roles_at_apply") or {}).get(member_id) or "")
    )

    activity = session.get(Activity, str(snapshot.get("rendezvous_activity_id") or ""))
    place = session.get(Place, str(snapshot.get("rendezvous_place_id") or ""))
    history_arrivals, moving = _member_arrival_records(
        activity, member_by_id, original_ids
    )
    durable_arrivals = _durable_member_arrival_receipts(
        session,
        journey=journey,
        snapshot=snapshot,
        members=member_by_id,
        required_ids=original_ids,
    )
    # Durable member receipts win. Activity history remains a compatibility
    # fallback for an in-flight/pre-R62 row but is never the only modern truth.
    arrivals = {**history_arrivals, **durable_arrivals}
    closure = None
    for row in session.scalars(
        select(AuditLog).where(
            AuditLog.journey_id == journey.id,
            AuditLog.action == REJOINED_ACTION,
        )
    ).all():
        item = _json(row.after_json)
        if (
            item.get("incident_key") == incident_key
            and str(item.get("applied_plan_version_id") or "") == plan_id
            and str(item.get("recovery_epoch_id") or "") == epoch_id
        ):
            closure = item
            break

    members_projection = []
    for member_id in sorted(original_ids):
        member = member_by_id.get(member_id)
        if member is None:
            continue
        traveler = session.get(Traveler, member.traveler_id)
        in_force = member_id in active_original
        confirmed = member_id in arrivals
        status = (
            "REJOINED" if confirmed else "MOVING" if member_id in moving
            else "WAITING" if in_force else "NO_LONGER_REQUIRED"
        )
        arrival = arrivals.get(member_id, {})
        confirmation_kind = str(arrival.get("kind") or "") or None
        members_projection.append(
            {
                "member_id": member_id,
                "display_name": traveler.display_name if traveler else "Traveller",
                "role": member.role,
                "is_viewer": member_id == str(viewer_member_id or ""),
                "state": status,
                "confirmed_at": arrival.get("captured_at"),
                "confirmation_kind": confirmation_kind,
                "physical_presence_verified": (
                    confirmation_kind == "DEVICE_LOCATION_VERIFIED_EXECUTION"
                ),
            }
        )

    membership_changed = bool(added_ids or changed_role_ids)
    all_arrived = bool(active_original) and active_original.issubset(arrivals)
    if closure is not None:
        state = "RECOVERY_CLOSED"
        headline = "Everyone has confirmed arrival at the shared Journey stop. CityNexus is continuing the Journey."
    elif membership_changed or not activity or not place:
        state = "REJOIN_CONFIRMATION_REQUIRED"
        headline = "The recovery needs a fresh group check before CityNexus can close it."
    elif all_arrived:
        # Only the execution mutation path writes the terminal audit. GET never closes.
        state = "REJOIN_IN_PROGRESS"
        headline = "Everyone reported arrival. CityNexus is finalizing the existing execution record."
    elif arrivals or moving:
        state = "REJOIN_IN_PROGRESS"
        headline = f"{len(active_original.intersection(arrivals))} traveller(s) have arrived; waiting for the rest."
    else:
        state = str(snapshot.get("state") or "RECOVERY_APPLIED_REJOIN_PENDING")
        headline = (
            "The shared recovery is active. Continue to the rendezvous in the updated Journey."
            if state == "RECOVERY_APPLIED_REJOIN_PENDING"
            else "CityNexus cannot identify a safe shared rendezvous yet."
        )

    verified_presence_ids = {
        member_id
        for member_id in active_original
        if str(arrivals.get(member_id, {}).get("kind") or "")
        == "DEVICE_LOCATION_VERIFIED_EXECUTION"
    }
    all_required_physical_presence_verified = bool(active_original) and (
        active_original.issubset(verified_presence_ids)
    )

    viewer_id = str(viewer_member_id or "")
    viewer = member_by_id.get(viewer_id)
    viewer_can_report_execution = bool(
        viewer is not None
        and session.scalar(
            select(JourneyRoleCapability).where(
                JourneyRoleCapability.role == viewer.role,
                JourneyRoleCapability.capability == "JOURNEY_EXECUTION_UPDATE",
            )
        )
    )
    viewer_arrived = viewer_id in arrivals
    can_confirm = bool(
        state in {"RECOVERY_APPLIED_REJOIN_PENDING", "REJOIN_IN_PROGRESS"}
        and viewer is not None
        and viewer_can_report_execution
        and viewer_id in active_original
        and not viewer_arrived
        and activity is not None
        and activity.plan_version_id == plan_id
        and activity.place_id == str(snapshot.get("rendezvous_place_id") or "")
        and journey.current_activity_id == activity.id
        and str(activity.status).upper() in {"EN_ROUTE", "ARRIVED", "IN_PROGRESS"}
    )
    viewer_status = next(
        (item["state"] for item in members_projection if item["member_id"] == viewer_id),
        "NOT_IN_REQUIRED_SET",
    )
    next_action = (
        "CONTINUE_WITH_GROUP" if state == "RECOVERY_CLOSED"
        else "WAIT_FOR_GROUP" if viewer_arrived
        else "ARRIVE_AT_RENDEZVOUS" if can_confirm
        else "FOLLOW_CURRENT_JOURNEY"
    )
    return {
        "state": state,
        "headline": headline,
        "incident_key": incident_key,
        "applied_plan_version_id": plan_id,
        "applied_proposal_id": snapshot.get("applied_proposal_id"),
        "recovery_epoch_id": epoch_id,
        "authority_receipt_id": snapshot.get("authority_receipt_id"),
        "rendezvous": (
            {"activity_id": activity.id, "title": activity.title, "place_id": place.id, "place_name": place.name}
            if activity is not None and place is not None and activity.plan_version_id == plan_id
            else None
        ),
        "viewer": {
            "member_id": viewer_id or None,
            "status": viewer_status,
            "next_action": next_action,
        },
        "members": members_projection,
        "required_count": len(active_original),
        "confirmed_count": len(active_original.intersection(arrivals)),
        "viewer_has_confirmed": viewer_arrived,
        "viewer_can_confirm": can_confirm,
        "membership_reconfirmation_required": membership_changed,
        "new_member_ids": added_ids,
        "changed_role_member_ids": changed_role_ids,
        "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
        "arrival_progress_authority": (
            "DURABLE_MEMBER_EXECUTION_RECEIPTS"
            if durable_arrivals
            else "ACTIVITY_EXECUTION_HISTORY_FALLBACK"
        ),
        # Group-level verification means every currently required
        # traveller has device/location-verified arrival. A single verified
        # member must never upgrade other self-reported arrivals.
        "physical_presence_verified": all_required_physical_presence_verified,
        "physical_presence_verified_member_count": len(verified_presence_ids),
        "plan_unchanged_by_closure": bool(closure and closure.get("plan_unchanged_by_closure") is True),
        "closed_at": closure.get("closed_at") if closure else None,
    }


def rejoin_idempotency_key(
    *, journey_id: str, incident_key: str, plan_version_id: str, member_id: str
) -> str:
    digest = hashlib.sha256(
        "|".join((journey_id, incident_key, plan_version_id, member_id)).encode("utf-8")
    ).hexdigest()
    return f"group-rejoin:{digest}"


def register_execution_arrival(
    session: Session,
    journey: Journey,
    *,
    activity: Activity,
    truth_record: dict[str, Any],
) -> bool:
    """Bind an existing ARRIVED execution truth to this recovery, once per member.

    Returns False when the record is not an eligible member-bound arrival or is a
    replay.  It never creates an arrival; the caller must first pass the ordinary
    execution state transition and authenticated attestation checks.
    """
    found = active_group_recovery_spend(session, journey)
    if found is None:
        return False
    _accepted, _spend, snapshot = found
    if (
        str(snapshot.get("state") or "") != "RECOVERY_APPLIED_REJOIN_PENDING"
        or str(snapshot.get("rendezvous_activity_id") or "") != str(activity.id)
        or str(snapshot.get("applied_plan_version_id") or "") != str(activity.plan_version_id)
        or str(activity.plan_version_id) != str(journey.active_plan_version_id)
        or str(truth_record.get("transition") or "").upper() != "ARRIVED"
        or truth_record.get("kind") not in AUTHENTICATED_ARRIVAL_KINDS
        or str(truth_record.get("actor_type") or "") != "AUTHENTICATED_TRAVELER"
    ):
        return False
    evidence = truth_record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    member_id = str(evidence.get("member_id") or "")
    member = session.get(JourneyMember, member_id)
    if (
        member is None
        or member.journey_id != journey.id
        or str(member.traveler_id) != str(truth_record.get("actor_id") or "")
        or member_id not in {str(value) for value in snapshot.get("required_member_ids", [])}
        or not _member_in_force(member, datetime.now(timezone.utc))
        or str(member.role) != str((snapshot.get("member_roles_at_apply") or {}).get(member_id) or "")
    ):
        return False
    key = rejoin_idempotency_key(
        journey_id=journey.id,
        incident_key=str(snapshot.get("incident_key") or ""),
        plan_version_id=str(snapshot.get("applied_plan_version_id") or ""),
        member_id=member_id,
    )
    if session.get(IdempotencyKey, key) is not None:
        return False
    confirmed_at = str(truth_record.get("captured_at") or datetime.now(timezone.utc).isoformat())
    receipt = {
        "journey_id": journey.id,
        "incident_key": snapshot.get("incident_key"),
        "applied_proposal_id": snapshot.get("applied_proposal_id"),
        "applied_plan_version_id": snapshot.get("applied_plan_version_id"),
        "recovery_epoch_id": snapshot.get("recovery_epoch_id"),
        "rendezvous_activity_id": activity.id,
        "rendezvous_place_id": activity.place_id,
        "journey_member_id": member_id,
        "traveler_id": member.traveler_id,
        "role_at_confirmation": member.role,
        "confirmed_at": confirmed_at,
        "execution_truth_kind": truth_record.get("kind"),
        "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
        "physical_presence_verified": truth_record.get("kind") == "DEVICE_LOCATION_VERIFIED_EXECUTION",
    }
    session.add(
        IdempotencyKey(
            key=key,
            operation="group_recovery.execution_arrival",
            request_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            response_status=200,
            response_json=json.dumps(receipt, sort_keys=True),
            created_at=confirmed_at,
            expires_at="9999-12-31T23:59:59+00:00",
        )
    )
    record_audit(
        session,
        action="journey.group_recovery.member_arrival_bound",
        entity_type="JourneyMember",
        entity_id=member_id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=member.traveler_id,
        after=receipt,
    )
    session.flush()
    return True


def close_recovery_if_rejoined(session: Session, journey: Journey) -> dict[str, Any] | None:
    """Persist exactly one terminal audit after all currently required members arrive."""
    found = active_group_recovery_spend(session, journey)
    if found is None:
        return None
    _accepted, _spend, snapshot = found
    projection = followthrough_projection(session, journey, viewer_member_id=None)
    if (
        projection is None
        or projection["state"] == "RECOVERY_CLOSED"
        or projection["membership_reconfirmation_required"]
        or not projection["required_count"]
        or projection["confirmed_count"] != projection["required_count"]
        or journey.active_plan_version_id != projection["applied_plan_version_id"]
    ):
        return projection
    close_key = "group-recovery-close:" + hashlib.sha256(
        "|".join(
            (
                journey.id,
                str(snapshot.get("incident_key") or ""),
                str(snapshot.get("applied_plan_version_id") or ""),
                str(snapshot.get("recovery_epoch_id") or ""),
            )
        ).encode("utf-8")
    ).hexdigest()
    if session.get(IdempotencyKey, close_key) is None:
        closed_at = datetime.now(timezone.utc).isoformat()
        closure = {
            "kind": "GROUP_RECOVERY_REJOINED",
            "incident_key": snapshot.get("incident_key"),
            "applied_proposal_id": snapshot.get("applied_proposal_id"),
            "applied_plan_version_id": snapshot.get("applied_plan_version_id"),
            "recovery_epoch_id": snapshot.get("recovery_epoch_id"),
            "required_member_ids": sorted(
                item["member_id"] for item in projection["members"]
                if item["state"] != "NO_LONGER_REQUIRED"
            ),
            "rendezvous_activity_id": snapshot.get("rendezvous_activity_id"),
            "rendezvous_place_id": snapshot.get("rendezvous_place_id"),
            "closed_at": closed_at,
            "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
            "physical_presence_verified": projection["physical_presence_verified"],
            "physical_presence_verified_member_count": projection.get(
                "physical_presence_verified_member_count", 0
            ),
            "plan_unchanged_by_closure": journey.active_plan_version_id
            == str(snapshot.get("applied_plan_version_id") or ""),
        }
        session.add(
            IdempotencyKey(
                key=close_key,
                operation="group_recovery.close",
                request_hash=hashlib.sha256(close_key.encode("utf-8")).hexdigest(),
                response_status=200,
                response_json=json.dumps(closure, sort_keys=True),
                created_at=closed_at,
                expires_at="9999-12-31T23:59:59+00:00",
            )
        )
        record_audit(
            session,
            action=REJOINED_ACTION,
            entity_type="GroupRecoveryFollowThrough",
            entity_id=str(snapshot.get("incident_key") or ""),
            journey_id=journey.id,
            actor_type="SYSTEM",
            actor_id="group-recovery-followthrough",
            after=closure,
        )
        trigger_id = str(snapshot.get("trigger_event_id") or "")
        trigger = session.get(Event, trigger_id) if trigger_id else None
        if trigger is not None and trigger.journey_id == journey.id:
            payload = _json(trigger.payload_json)
            payload["group_recovery_closed_at"] = closed_at
            payload["group_recovery_incident_key"] = snapshot.get("incident_key")
            payload["group_recovery_resolution_reason"] = "MEMBERS_REJOINED"
            trigger.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if str(trigger.status).upper() not in {"RESOLVED", "IGNORED"}:
                trigger.status = "RESOLVED"
                trigger.resolved_at = closed_at
        session.flush()
    return followthrough_projection(session, journey, viewer_member_id=None)
