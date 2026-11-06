"""Traveller-facing collective state for the Journey Operator."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    CollectiveAuthorityReceipt,
    Journey,
    JourneyMember,
    JourneyMission,
    JourneyRoleCapability,
    PlanVersion,
    RecoveryEpoch,
    ReplanProposal,
    Traveler,
)


def classify_collective_operator_state(
    *,
    member_count: int,
    status: str | None,
    response_count: int = 0,
    authority_receipt_present: bool = False,
    stale: bool = False,
) -> str:
    if member_count <= 1:
        return "SINGLE_TRAVELLER"
    if stale or str(status or "").upper() == "STALE":
        return "COLLECTIVE_STALE"
    if authority_receipt_present:
        return "COLLECTIVE_AUTHORIZED"
    state = str(status or "").upper()
    if state == "WAITING_FOR_MEMBERS":
        return "WAITING_FOR_MEMBERS"
    if state == "MEDIATION_REQUIRED":
        return "MEDIATING"
    if state == "RESOLVED":
        return "COLLECTIVE_READY"
    if response_count and response_count < member_count:
        return "WAITING_FOR_MEMBERS"
    return "COLLECTIVE_NEEDS_ATTENTION"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _decision_round_is_current(
    session: Session,
    journey: Journey,
    mission: JourneyMission,
    clarification: dict[str, Any],
) -> bool:
    """Read-only freshness check for the response buttons shown to members."""
    trigger_id = str(clarification.get("trigger_event_id") or "")
    expected_epoch = str(clarification.get("decision_epoch") or "")
    base_plan_id = str(clarification.get("base_plan_version_id") or "")
    options = [
        item for item in clarification.get("candidate_options") or []
        if isinstance(item, dict) and item.get("proposal_id")
    ]
    # Preserve the legacy read projection for historical rounds that predate
    # decision-epoch binding. New rooms always carry all three fields.
    if not trigger_id or not expected_epoch or not base_plan_id or not options:
        return True
    if base_plan_id != str(journey.active_plan_version_id or ""):
        return False

    from app.collective_authority import candidate_surface_fingerprint
    from app.collective_mediation import (
        collective_decision_epoch,
        collective_material_world_epoch,
    )
    from app.main import (
        active_journey_members_with_capabilities,
        collective_material_world_basis,
        collective_proposal_receipt,
    )

    proposals = session.scalars(
        select(ReplanProposal).where(
            ReplanProposal.journey_id == journey.id,
            ReplanProposal.trigger_event_id == trigger_id,
            ReplanProposal.status == "PRESENTED",
        ).order_by(ReplanProposal.created_at, ReplanProposal.id)
    ).all()
    receipt = collective_proposal_receipt(session, journey, proposals)
    stored_surface = clarification.get("candidate_surface_fingerprint")
    if stored_surface is not None and stored_surface != candidate_surface_fingerprint(
        receipt.get("candidate_fit_matrix") or []
    ):
        return False

    option_ids = {str(item.get("proposal_id") or "") for item in options}
    current_options = [
        item
        for item in (receipt.get("proposal_collective_decisions") or {}).values()
        if str(item.get("proposal_id") or "") in option_ids
        and item.get("status") == "GROUNDED_FEASIBLE"
    ]
    if {str(item.get("proposal_id") or "") for item in current_options} != option_ids:
        return False
    for option in current_options:
        plan = session.get(PlanVersion, str(option.get("plan_id") or ""))
        if plan is None:
            return False
        option["material_world_epoch"] = collective_material_world_epoch(
            collective_material_world_basis(
                session,
                journey,
                plan,
                fit_subject_place_ids=list(option.get("fit_subject_place_ids") or []),
            )
        )
    required_ids = [
        str(item) for item in clarification.get("required_member_ids") or []
    ]
    members = active_journey_members_with_capabilities(session, journey).get(
        "members", []
    )
    current_epoch = collective_decision_epoch(
        journey_id=journey.id,
        mission_id=mission.id,
        trigger_event_id=trigger_id,
        base_plan_version_id=journey.active_plan_version_id,
        options=current_options,
        required_member_ids=required_ids,
        decision_relevant_member_ids=(
            (receipt.get("collective_mediation") or {}).get(
                "decision_relevant_member_ids"
            )
            if (receipt.get("collective_mediation") or {}).get(
                "decision_relevant_member_ids"
            ) is not None
            else clarification.get(
                "decision_relevant_member_ids", required_ids
            )
        ),
        members=members,
    )
    return current_epoch == expected_epoch


def collective_operator_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    members = list(
        session.scalars(
            select(JourneyMember)
            .where(
                JourneyMember.journey_id == journey.id,
                JourneyMember.removed_at.is_(None),
            )
            .order_by(JourneyMember.created_at, JourneyMember.id)
        ).all()
    )
    if len(members) <= 1:
        return {
            "state": "SINGLE_TRAVELLER",
            "required_count": 1,
            "response_count": 0,
            "viewer_has_responded": False,
            "members": [],
            "options": [],
            "mission_id": None,
            "authority_receipt_id": None,
        }

    # Select the latest durable collective round, including an answered source
    # mission whose mediation child is still running. This keeps all viewers
    # attached to the same Journey decision instead of the latest child mission.
    mission = None
    clarification: dict[str, Any] = {}
    for candidate in session.scalars(
        select(JourneyMission)
        .where(JourneyMission.journey_id == journey.id, JourneyMission.is_standing.is_(False))
        .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
    ).all():
        candidate_clarification = _json(candidate.clarification_json, {})
        if isinstance(candidate_clarification, dict) and candidate_clarification.get("kind") == "COLLECTIVE_TRADEOFF":
            mission, clarification = candidate, candidate_clarification
            break
    if mission is None:
        return {
            "state": "COLLECTIVE_NEEDS_ATTENTION",
            "required_count": len(members),
            "response_count": 0,
            "viewer_has_responded": False,
            "members": [],
            "options": [],
            "mission_id": None,
            "authority_receipt_id": None,
        }

    required_ids = [str(item) for item in clarification.get("required_member_ids") or []]
    responses = clarification.get("responses") or {}
    required_set = set(required_ids)
    active_members = {
        item.id: item for item in members
        if _member_is_in_force(item, now=now)
    }
    member_rows = [active_members[item] for item in required_ids if item in active_members]
    membership_stale = len(member_rows) != len(required_ids)
    capability_rows = session.scalars(select(JourneyRoleCapability)).all()
    member_update_roles = {row.role for row in capability_rows if row.capability == "JOURNEY_UPDATE"}
    if any(item.role not in member_update_roles for item in member_rows):
        membership_stale = True
    member_views = []
    for item in member_rows:
        traveler = session.get(Traveler, item.traveler_id)
        member_views.append(
            {
                "display_name": traveler.display_name if traveler is not None else "Traveller",
                "role": item.role,
                "member_id": item.id if viewer_member_id == item.id else None,
                "responded": item.id in responses,
                "is_viewer": bool(viewer_member_id and viewer_member_id == item.id),
                "needs_response": item.id not in responses,
            }
        )

    authority_receipt_id = clarification.get("authority_receipt_id")
    receipt = (
        session.get(CollectiveAuthorityReceipt, str(authority_receipt_id))
        if authority_receipt_id
        else None
    )
    response_count = len(set(required_ids).intersection(responses))
    decision_is_current = _decision_round_is_current(
        session, journey, mission, clarification
    )
    receipt_is_current = bool(
        receipt is not None
        and receipt.invalidated_at is None
        and receipt.decision_epoch == clarification.get("decision_epoch")
        and receipt.material_world_epoch == clarification.get("material_world_epoch")
        and receipt.candidate_surface_fingerprint == clarification.get("candidate_surface_fingerprint")
        and set(_json(receipt.required_member_ids_json, [])) == required_set
    )
    # Read-side RecoveryEpoch freshness must match the write boundary. Do not
    # call ensure_recovery_epoch() from this GET projection because that would
    # mutate history. Recompute the semantic reality fingerprint read-only and
    # refuse to advertise spendable authority when the bound generation is no
    # longer the current world.
    from app.recovery_epoch import recovery_reality_basis, recovery_reality_fingerprint

    selected_for_epoch_id = str(clarification.get("selected_proposal_id") or "")
    selected_for_epoch = (
        session.get(ReplanProposal, selected_for_epoch_id)
        if selected_for_epoch_id
        else None
    )
    bound_epoch = (
        session.get(RecoveryEpoch, str(selected_for_epoch.recovery_epoch_id))
        if selected_for_epoch is not None and selected_for_epoch.recovery_epoch_id
        else None
    )
    current_reality_fingerprint = recovery_reality_fingerprint(
        recovery_reality_basis(session, journey)
    )
    recovery_epoch_current = bool(
        bound_epoch is not None
        and bound_epoch.status == "OPEN"
        and str(bound_epoch.active_plan_version_id_at_open or "")
        == str(journey.active_plan_version_id or "")
        and str(bound_epoch.recovery_reality_fingerprint or "")
        == current_reality_fingerprint
        and (
            receipt is None
            or not receipt.recovery_epoch_id
            or str(receipt.recovery_epoch_id) == str(bound_epoch.id)
        )
    )
    state = classify_collective_operator_state(
        member_count=len(required_ids) or len(member_rows),
        status=str(clarification.get("status") or ""),
        response_count=response_count,
        authority_receipt_present=receipt_is_current,
        stale=(
            membership_stale
            or not decision_is_current
            or not recovery_epoch_current
            or str(clarification.get("status") or "").upper() == "STALE"
        ),
    )
    options = [
        {
            "proposal_id": item.get("proposal_id"),
            "label": item.get("label") or item.get("summary") or "Recovery option",
            "schedule_delta_min": item.get("schedule_delta_min", item.get("end_time_delta_min")),
            "cost_delta_minor": item.get("cost_delta_minor"),
            "fit_subject_place_ids": item.get("fit_subject_place_ids") or [],
        }
        for item in clarification.get("candidate_options") or []
        if isinstance(item, dict)
    ]
    selected_proposal_id = str(clarification.get("selected_proposal_id") or "") or None
    selected_option = next(
        (
            item
            for item in clarification.get("candidate_options") or []
            if isinstance(item, dict)
            and str(item.get("proposal_id") or "") == str(selected_proposal_id or "")
        ),
        None,
    )
    selected_plan_id = str(selected_option.get("plan_id") or "") if selected_option else ""
    selected_option_label = (
        str(
            selected_option.get("label")
            or selected_option.get("summary")
            or "Recovery option"
        )
        if selected_option
        else None
    )
    selected_proposal = session.get(ReplanProposal, str(selected_proposal_id)) if selected_proposal_id else None
    selected_plan = session.get(PlanVersion, selected_plan_id) if selected_plan_id else None
    selected_proposal_current = bool(
        selected_proposal is not None
        and selected_plan is not None
        and selected_proposal.journey_id == journey.id
        and selected_proposal.status == "PRESENTED"
        and selected_proposal.base_plan_version_id == journey.active_plan_version_id
        and selected_proposal.proposed_plan_version_id == selected_plan.id
        and selected_plan.status == "CANDIDATE"
    )
    viewer_member = active_members.get(str(viewer_member_id or ""))
    mission_intent = _json(mission.resolved_intent_json, {})
    group_recovery_context = (
        mission_intent.get("group_recovery")
        if isinstance(mission_intent, dict)
        else None
    )
    viewer_can_apply = bool(
        state == "COLLECTIVE_AUTHORIZED"
        and decision_is_current
        and receipt_is_current
        and recovery_epoch_current
        and selected_proposal_current
        and viewer_member is not None
        and viewer_member.role in member_update_roles
    )
    return {
        "state": state,
        "mission_id": mission.id,
        "clarification_id": clarification.get("clarification_id"),
        "decision_epoch": clarification.get("decision_epoch"),
        "required_count": len(required_ids),
        "response_count": response_count,
        "viewer_member_id": viewer_member_id,
        "viewer_has_responded": bool(viewer_member_id and viewer_member_id in responses),
        "viewer_can_respond": bool(
            viewer_member_id
            and viewer_member_id in required_set
            and viewer_member_id in active_members
            and active_members[viewer_member_id].role in member_update_roles
            and str(clarification.get("status") or "").upper() == "WAITING_FOR_MEMBERS"
            and decision_is_current
            and viewer_member_id not in responses
        ),
        "members": member_views,
        "options": options,
        "authority_receipt_id": receipt.id if receipt is not None else None,
        "selected_proposal_id": selected_proposal_id,
        "selected_plan_version_id": selected_plan_id or None,
        "selected_option_label": selected_option_label,
        "reconsent_required": bool(
            isinstance(group_recovery_context, dict)
            and group_recovery_context.get("reconsent_for_clarification_id")
        ),
        "proposal_ids": sorted(
            {
                str(item.get("proposal_id"))
                for item in clarification.get("candidate_options") or []
                if isinstance(item, dict) and item.get("proposal_id")
            }
        ),
        "viewer_can_apply": viewer_can_apply,
        "apply_block_reason": None if viewer_can_apply else (
            "AUTHORIZED_MEMBER_REQUIRED"
            if state == "COLLECTIVE_AUTHORIZED"
            else "CURRENT_COLLECTIVE_AUTHORITY_REQUIRED"
        ),
        "waiting_for_member_count": max(0, len(required_ids) - response_count),
        "mediation_mission_id": _latest_mediation_child(session, journey.id, mission.id),
        "status": clarification.get("status"),
        "mission_status": mission.status,
        "decision_epoch_current": not membership_stale and decision_is_current,
        "recovery_epoch_current": recovery_epoch_current,
        "timeline": collective_operator_timeline(session, journey.id, member_rows),
    }


def _member_is_in_force(member: JourneyMember, *, now: datetime) -> bool:
    if member.removed_at:
        return False
    if not member.expires_at:
        return True
    try:
        expires = datetime.fromisoformat(member.expires_at)
    except ValueError:
        return False
    return expires.tzinfo is not None and expires.astimezone(timezone.utc) > now


def _latest_mediation_child(session: Session, journey_id: str, source_mission_id: str) -> str | None:
    rows = session.scalars(
        select(JourneyMission).where(
            JourneyMission.journey_id == journey_id,
            JourneyMission.parent_mission_id == source_mission_id,
        ).order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
    ).all()
    for row in rows:
        intent = _json(row.resolved_intent_json, {})
        mediation = intent.get("collective_mediation") if isinstance(intent, dict) else None
        if isinstance(mediation, dict) and mediation.get("kind") == "DISAGREEMENT_MEDIATION":
            return row.id
    return None


def collective_operator_timeline(
    session: Session, journey_id: str, members: list[JourneyMember]
) -> list[dict[str, Any]]:
    names = {
        member.id: (
            session.get(Traveler, member.traveler_id).display_name
            if session.get(Traveler, member.traveler_id) is not None
            else "A traveller"
        )
        for member in members
    }
    labels = {
        "agent.collective_decision.requested": "CityNexus found recovery options for the group",
        "agent.collective_decision.mediation_started": "The group choices conflict; looking for a compromise",
        "agent.collective_decision.authority_issued": "All required travellers agreed",
        "operator.recovered": "Shared plan updated; watching again",
    }
    rows = session.scalars(
        select(AuditLog).where(AuditLog.journey_id == journey_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    items: list[dict[str, Any]] = []
    response_seen: set[tuple[str, str]] = set()
    for row in rows:
        after = _json(row.after_json, {})
        if not isinstance(after, dict):
            continue
        label = labels.get(row.action)
        if row.action == "agent.collective_decision.response":
            member_id = str(after.get("member_id") or "")
            clarification_id = str(after.get("clarification_id") or "")
            key = (member_id, clarification_id)
            if key in response_seen:
                continue
            response_seen.add(key)
            label = f"{names.get(member_id, 'A traveller')} responded"
        if label:
            items.append({"at": row.created_at, "kind": row.action, "label": label})
    return items[-12:]
