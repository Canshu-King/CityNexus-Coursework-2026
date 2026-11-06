"""Server-owned durable authority receipts for R30 collective convergence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    CollectiveAuthorityReceipt,
    Journey,
    PlanVersion,
    ReplanProposal,
)
from app.recovery_epoch import RECOVERY_EPOCH_SUPERSEDED

AUTHENTICATED_MEMBER_CONSENT = "AUTHENTICATED_MEMBER_CONSENT"
UNANIMOUS_DOMINANCE = "UNANIMOUS_DOMINANCE"
_MODES = {AUTHENTICATED_MEMBER_CONSENT, UNANIMOUS_DOMINANCE}
COLLECTIVE_SEMANTIC_EPOCH_SCHEMA = "citynexus.collective-semantic-decision-epoch.v1"
AUTHENTICATED_COLLECTIVE_DECISION_EPOCH_SCHEMA = "citynexus.collective-decision-epoch.v1"
COLLECTIVE_WORLD_EPOCH_SCHEMA = "citynexus.collective-material-world-epoch.v1"
COLLECTIVE_SURFACE_SCHEMA = "citynexus.collective-candidate-surface.v1"


def collective_authority_provenance(
    authority_mode: str | None,
) -> dict[str, Any]:
    """Describe the builder that actually produced each authority fact."""

    if authority_mode == AUTHENTICATED_MEMBER_CONSENT:
        decision = {
            "source": "AUTHENTICATED_MEMBER_COLLECTIVE_DECISION_EPOCH",
            "schema": AUTHENTICATED_COLLECTIVE_DECISION_EPOCH_SCHEMA,
        }
    elif authority_mode == UNANIMOUS_DOMINANCE:
        decision = {
            "source": "ALGORITHMIC_UNANIMOUS_SEMANTIC_EPOCH",
            "schema": COLLECTIVE_SEMANTIC_EPOCH_SCHEMA,
        }
    else:
        decision = {"source": None, "schema": None}
    return {
        "decision_epoch": decision,
        "material_world_epoch": {
            "source": "SERVER_COLLECTIVE_MATERIAL_WORLD_EPOCH",
            "schema": COLLECTIVE_WORLD_EPOCH_SCHEMA,
        },
        "candidate_surface_fingerprint": {
            "source": "SERVER_CANDIDATE_FIT_SURFACE",
            "schema": COLLECTIVE_SURFACE_SCHEMA,
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(values: Iterable[Any]) -> str:
    return json.dumps(sorted({str(value) for value in values if value}), ensure_ascii=False)


def candidate_surface_fingerprint(candidate_fit_matrix: Iterable[Any] | None) -> str:
    """Fingerprint the server's current candidate surface without aliasing E/W."""

    surface = json.dumps(
        list(candidate_fit_matrix or []),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(surface.encode("utf-8")).hexdigest()


def server_semantic_decision_epoch(
    *,
    journey_id: str,
    active_plan_version_id: str | None,
    target_plan_version_id: str,
    classification: str,
    candidate_id: str,
    candidate_fit_matrix: Iterable[Any] | None,
    members: Iterable[Any] | None,
) -> str:
    """Build a distinct server-owned E for an algorithmic collective decision.

    The semantic epoch deliberately has its own schema and projection.  It
    describes what the server decided about the current member-fit surface and
    target, while the surface fingerprint remains the independent S value.
    """

    material = {
        "schema": COLLECTIVE_SEMANTIC_EPOCH_SCHEMA,
        "journey_id": str(journey_id),
        "active_plan_version_id": str(active_plan_version_id or ""),
        "target_plan_version_id": str(target_plan_version_id),
        "classification": str(classification),
        "candidate_id": str(candidate_id),
        "candidate_fit_matrix": list(candidate_fit_matrix or []),
        "members": list(members or []),
    }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def issue_collective_authority_receipt(
    session: Session,
    *,
    journey_id: str,
    proposal_id: str,
    plan_version_id: str,
    authority_mode: str,
    decision_epoch: str,
    material_world_epoch: str,
    candidate_surface_fingerprint: str,
    classification: str,
    required_member_ids: Iterable[Any] = (),
    response_audit_ids: Iterable[Any] = (),
    principal_actor_ids: Iterable[Any] = (),
    source_mission_id: str | None = None,
    source_agent_run_id: str | None = None,
    source_clarification_id: str | None = None,
    recovery_epoch_id: str | None = None,
) -> CollectiveAuthorityReceipt:
    """Issue a receipt only from server-computed, exact target facts."""

    if authority_mode not in _MODES:
        raise ValueError(f"unsupported collective authority mode: {authority_mode}")
    values = (decision_epoch, material_world_epoch, candidate_surface_fingerprint)
    if not all(str(value or "") for value in values):
        raise ValueError("collective authority receipt requires current epochs and surface fingerprint")
    journey = session.get(Journey, str(journey_id))
    proposal = session.get(ReplanProposal, str(proposal_id))
    plan = session.get(PlanVersion, str(plan_version_id))
    if journey is None or proposal is None or plan is None:
        raise ValueError("collective authority receipt target does not exist")
    if (
        proposal.journey_id != journey.id
        or proposal.proposed_plan_version_id != plan.id
        or plan.journey_id != journey.id
    ):
        raise ValueError("collective authority receipt target does not match")
    if proposal.status not in {"PRESENTED", "ACCEPTED"}:
        raise ValueError("collective authority receipt requires a current proposal")
    required_ids = {str(value) for value in required_member_ids if value}
    audit_ids = {str(value) for value in response_audit_ids if value}
    actor_ids = {str(value) for value in principal_actor_ids if value}
    if authority_mode == AUTHENTICATED_MEMBER_CONSENT:
        if not required_ids or not audit_ids or not actor_ids:
            raise ValueError("authenticated member consent requires response audits and principals")
        audits = session.scalars(
            select(AuditLog).where(AuditLog.id.in_(audit_ids))
        ).all()
        if len(audits) != len(audit_ids) or any(
            item.action != "agent.collective_decision.response"
            or item.entity_type != "JourneyMission"
            or item.journey_id != journey.id
            or item.actor_type != "AUTHENTICATED_TRAVELER"
            or (
                source_mission_id is not None
                and item.entity_id != str(source_mission_id)
            )
            for item in audits
        ):
            raise ValueError("collective authority receipt contains invalid response audits")

        decoded_audits = [
            (item, json.loads(item.after_json or "{}"))
            for item in audits
        ]
        if source_clarification_id is not None and any(
            str(after.get("clarification_id") or "")
            != str(source_clarification_id)
            for _item, after in decoded_audits
        ):
            raise ValueError(
                "collective authority receipt mixes response audits from another clarification"
            )
        if any(
            str(after.get("traveler_id") or "") != str(item.actor_id or "")
            for item, after in decoded_audits
        ):
            raise ValueError(
                "collective authority receipt response audit principal mismatch"
            )
        if any(
            str(after.get("proposal_id") or "") != str(proposal_id)
            for _item, after in decoded_audits
        ):
            raise ValueError(
                "collective authority receipt response audit selected another proposal"
            )
        if any(
            str(after.get("decision_epoch") or "") != str(decision_epoch)
            for _item, after in decoded_audits
        ):
            raise ValueError(
                "collective authority receipt response audit decision epoch mismatch"
            )

        audit_members = {
            str(after.get("member_id") or "")
            for _item, after in decoded_audits
        }
        audit_actors = {
            str(item.actor_id)
            for item, _after in decoded_audits
            if item.actor_id
        }
        if not required_ids.issubset(audit_members):
            raise ValueError("collective authority receipt lacks every required member response")
        if actor_ids != audit_actors:
            raise ValueError(
                "collective authority receipt principal set does not match response audits"
            )
    required_json = _json(required_ids)
    audits_json = _json(audit_ids)
    actors_json = _json(actor_ids)
    exact_existing = session.scalar(
        select(CollectiveAuthorityReceipt)
        .where(
            CollectiveAuthorityReceipt.journey_id == str(journey_id),
            CollectiveAuthorityReceipt.proposal_id == str(proposal_id),
            CollectiveAuthorityReceipt.plan_version_id == str(plan_version_id),
            CollectiveAuthorityReceipt.authority_mode == authority_mode,
            CollectiveAuthorityReceipt.decision_epoch == str(decision_epoch),
            CollectiveAuthorityReceipt.material_world_epoch
            == str(material_world_epoch),
            CollectiveAuthorityReceipt.candidate_surface_fingerprint
            == str(candidate_surface_fingerprint),
            CollectiveAuthorityReceipt.classification == str(classification),
            CollectiveAuthorityReceipt.source_mission_id
            == (str(source_mission_id) if source_mission_id else None),
            CollectiveAuthorityReceipt.source_agent_run_id
            == (str(source_agent_run_id) if source_agent_run_id else None),
            CollectiveAuthorityReceipt.recovery_epoch_id
            == (str(recovery_epoch_id) if recovery_epoch_id else None),
            CollectiveAuthorityReceipt.required_member_ids_json == required_json,
            CollectiveAuthorityReceipt.response_audit_ids_json == audits_json,
            CollectiveAuthorityReceipt.principal_actor_ids_json == actors_json,
            CollectiveAuthorityReceipt.invalidated_at.is_(None),
        )
        .order_by(
            CollectiveAuthorityReceipt.issued_at,
            CollectiveAuthorityReceipt.id,
        )
    )
    if exact_existing is not None:
        return exact_existing

    receipt = CollectiveAuthorityReceipt(
        id=str(uuid4()),
        journey_id=str(journey_id),
        proposal_id=str(proposal_id),
        plan_version_id=str(plan_version_id),
        authority_mode=authority_mode,
        decision_epoch=str(decision_epoch),
        material_world_epoch=str(material_world_epoch),
        candidate_surface_fingerprint=str(candidate_surface_fingerprint),
        source_mission_id=str(source_mission_id) if source_mission_id else None,
        source_agent_run_id=str(source_agent_run_id) if source_agent_run_id else None,
        recovery_epoch_id=str(recovery_epoch_id) if recovery_epoch_id else None,
        required_member_ids_json=required_json,
        response_audit_ids_json=audits_json,
        principal_actor_ids_json=actors_json,
        classification=str(classification),
        issued_at=_now(),
    )
    session.add(receipt)
    session.flush()
    return receipt


def receipt_validation(
    receipt: CollectiveAuthorityReceipt | None,
    *,
    journey_id: str,
    proposal_id: str,
    plan_version_id: str,
    decision_epoch: str,
    material_world_epoch: str,
    candidate_surface_fingerprint: str,
    authority_mode: str | None = None,
    recovery_epoch_id: str | None = None,
) -> dict[str, Any]:
    """Pure exact-match validation used immediately before every mutation."""

    if receipt is None:
        return {"valid": False, "code": "COLLECTIVE_AUTHORITY_RECEIPT_MISSING"}
    checks = (
        (receipt.journey_id, str(journey_id), "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_JOURNEY"),
        (receipt.proposal_id, str(proposal_id), "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_PROPOSAL"),
        (receipt.plan_version_id, str(plan_version_id), "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_PLAN"),
        (receipt.decision_epoch, str(decision_epoch), "COLLECTIVE_AUTHORITY_RECEIPT_STALE_DECISION_EPOCH"),
        (receipt.material_world_epoch, str(material_world_epoch), "COLLECTIVE_AUTHORITY_RECEIPT_STALE_WORLD_EPOCH"),
        (receipt.candidate_surface_fingerprint, str(candidate_surface_fingerprint), "COLLECTIVE_AUTHORITY_RECEIPT_STALE_SURFACE"),
    )
    if receipt.invalidated_at:
        return {"valid": False, "code": receipt.invalidation_code or "COLLECTIVE_AUTHORITY_RECEIPT_INVALIDATED"}
    if receipt.authority_mode not in _MODES:
        return {"valid": False, "code": "COLLECTIVE_AUTHORITY_RECEIPT_MODE_INVALID"}
    if authority_mode and receipt.authority_mode != authority_mode:
        return {"valid": False, "code": "COLLECTIVE_AUTHORITY_RECEIPT_MODE_MISMATCH"}
    for actual, expected, code in checks:
        if str(actual or "") != expected:
            return {"valid": False, "code": code}
    if recovery_epoch_id is not None and str(receipt.recovery_epoch_id or "") != str(recovery_epoch_id):
        return {"valid": False, "code": RECOVERY_EPOCH_SUPERSEDED}
    return {"valid": True, "code": None, "receipt_id": receipt.id, "authority_mode": receipt.authority_mode}


def authority_receipt_for_allowed_gate(
    session: Session,
    *,
    journey_id: str,
    proposal_id: str,
    plan_version_id: str,
    gate: dict[str, Any],
    resolved_decision: dict[str, Any] | None = None,
) -> CollectiveAuthorityReceipt | None:
    """Resolve one exact receipt for a server-approved mutation preflight.

    This function never decides whether a collective mutation is allowed. The
    caller supplies the current collective-mutation gate result. It turns that
    allowed server fact into one durable, idempotent authority receipt so
    approval suspension and final mutation can bind the same token.
    """

    if gate.get("allowed") is not True:
        raise ValueError("collective authority receipt requires an allowed gate")
    recovery_epoch_id = str(gate.get("recovery_epoch_id") or "") or None
    if not recovery_epoch_id and str(gate.get("classification") or "") != "RENEWED_COLLECTIVE_CONSENT":
        raise ValueError("COLLECTIVE_AUTHORITY_EPOCH_UNAVAILABLE")

    classification = str(gate.get("classification") or "")
    proposal = session.get(ReplanProposal, str(proposal_id))
    proposal_source_run_id = (
        str(proposal.source_agent_run_id)
        if proposal is not None
        and getattr(proposal, "source_agent_run_id", None)
        else None
    )
    if classification == UNANIMOUS_DOMINANCE:
        # The gate is the sole authority for current provenance.  In
        # particular, never manufacture E or W from the candidate surface S.
        decision_epoch = str(gate.get("current_decision_epoch") or "")
        material_world_epoch = str(
            gate.get("current_material_world_epoch") or ""
        )
        surface_fingerprint = str(
            gate.get("candidate_surface_fingerprint") or ""
        )
        if not decision_epoch or not material_world_epoch or not surface_fingerprint:
            raise ValueError("COLLECTIVE_AUTHORITY_EPOCH_UNAVAILABLE")
        receipt = issue_collective_authority_receipt(
            session,
            journey_id=journey_id,
            proposal_id=proposal_id,
            plan_version_id=plan_version_id,
            authority_mode=UNANIMOUS_DOMINANCE,
            decision_epoch=decision_epoch,
            material_world_epoch=material_world_epoch,
            candidate_surface_fingerprint=surface_fingerprint,
            classification=UNANIMOUS_DOMINANCE,
            source_agent_run_id=proposal_source_run_id,
            recovery_epoch_id=recovery_epoch_id,
        )
        check = receipt_validation(
            receipt,
            journey_id=journey_id,
            proposal_id=proposal_id,
            plan_version_id=plan_version_id,
            decision_epoch=decision_epoch,
            material_world_epoch=material_world_epoch,
            candidate_surface_fingerprint=surface_fingerprint,
            authority_mode=UNANIMOUS_DOMINANCE,
            recovery_epoch_id=recovery_epoch_id,
        )
        if not check.get("valid"):
            raise ValueError(
                str(check.get("code") or "COLLECTIVE_AUTHORITY_RECEIPT_INVALID")
            )
        return receipt

    if classification == "RENEWED_COLLECTIVE_CONSENT":
        receipt_id = str(
            gate.get("authority_receipt_id")
            or (resolved_decision or {}).get("authority_receipt_id")
            or ""
        )
        receipt = (
            session.get(CollectiveAuthorityReceipt, receipt_id)
            if receipt_id
            else None
        )
        if receipt is None:
            raise ValueError("COLLECTIVE_AUTHORITY_RECEIPT_MISSING")
        decision_epoch = str(
            gate.get("resolved_decision_epoch")
            or gate.get("current_decision_epoch")
            or ""
        )
        material_world_epoch = str(
            gate.get("resolved_material_world_epoch")
            or gate.get("current_material_world_epoch")
            or ""
        )
        surface_fingerprint = str(
            gate.get("candidate_surface_fingerprint") or ""
        )
        if not decision_epoch or not material_world_epoch:
            raise ValueError("COLLECTIVE_AUTHORITY_EPOCH_UNAVAILABLE")
        if not surface_fingerprint:
            raise ValueError("COLLECTIVE_AUTHORITY_SURFACE_UNAVAILABLE")
        check = receipt_validation(
            receipt,
            journey_id=journey_id,
            proposal_id=proposal_id,
            plan_version_id=plan_version_id,
            decision_epoch=decision_epoch,
            material_world_epoch=material_world_epoch,
            candidate_surface_fingerprint=surface_fingerprint,
            authority_mode=AUTHENTICATED_MEMBER_CONSENT,
            recovery_epoch_id=recovery_epoch_id,
        )
        if not check.get("valid"):
            raise ValueError(
                str(check.get("code") or "COLLECTIVE_AUTHORITY_RECEIPT_INVALID")
            )
        return receipt

    # Single-member and other authority modes are outside R30 collective
    # receipt semantics. They remain governed by existing product policy.
    return None


def latest_collective_authority_receipt(
    session: Session, *, journey_id: str, proposal_id: str, plan_version_id: str | None = None
) -> CollectiveAuthorityReceipt | None:
    stmt = select(CollectiveAuthorityReceipt).where(
        CollectiveAuthorityReceipt.journey_id == str(journey_id),
        CollectiveAuthorityReceipt.proposal_id == str(proposal_id),
    )
    if plan_version_id is not None:
        stmt = stmt.where(CollectiveAuthorityReceipt.plan_version_id == str(plan_version_id))
    return session.scalars(
        stmt.order_by(CollectiveAuthorityReceipt.issued_at.desc(), CollectiveAuthorityReceipt.id.desc())
    ).first()


def validate_collective_authority_receipt(
    session: Session,
    *,
    journey_id: str,
    proposal_id: str,
    plan_version_id: str,
    decision_epoch: str,
    material_world_epoch: str,
    candidate_surface_fingerprint: str,
    authority_mode: str | None = None,
) -> dict[str, Any]:
    """Load and validate the latest server receipt for a mutation target."""

    receipt = latest_collective_authority_receipt(
        session,
        journey_id=journey_id,
        proposal_id=proposal_id,
        plan_version_id=plan_version_id,
    )
    return receipt_validation(
        receipt,
        journey_id=journey_id,
        proposal_id=proposal_id,
        plan_version_id=plan_version_id,
        decision_epoch=decision_epoch,
        material_world_epoch=material_world_epoch,
        candidate_surface_fingerprint=candidate_surface_fingerprint,
        authority_mode=authority_mode,
    )


def invalidate_collective_authority_receipt(
    session: Session, receipt: CollectiveAuthorityReceipt, code: str
) -> None:
    receipt.invalidated_at = _now()
    receipt.invalidation_code = str(code)
