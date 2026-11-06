"""Durable server-owned recovery reality epochs (R32).

The epoch is deliberately independent from the R31 E/W/S authority values.  It
binds a model run and every authority-bearing continuation to the Journey
reality that was current when the run started.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    CollectiveAuthorityReceipt,
    Event,
    Journey,
    PlanVersion,
    RecoveryEpoch,
    ReplanProposal,
)

OPEN = "OPEN"
SUPERSEDED = "SUPERSEDED"
COMMITTED = "COMMITTED"
ABANDONED = "ABANDONED"
RECOVERY_EPOCH_SUPERSEDED = "RECOVERY_EPOCH_SUPERSEDED"
_ACTIONABLE_EVENT_STATUSES = {"DETECTED", "ACKNOWLEDGED", "ACTIVE", "OPEN"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_fingerprint(payload_json: str | None) -> str | None:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in ("incident_fingerprint", "incidentFingerprint", "fingerprint"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def recovery_reality_basis(session: Session, journey: Journey) -> dict[str, Any]:
    """Return decision-material Journey facts with storage identity kept separate.

    Recovery reality is semantic, not row identity.  Two provider rows that
    describe the same canonical incident must not create a new generation just
    because their Event UUIDs differ.  Likewise DETECTED -> ACKNOWLEDGED is a
    workflow transition while the same incident remains actionable, not a new
    physical world.

    Source Event ids are retained only for lineage/debugging and are explicitly
    excluded from the recovery-reality fingerprint below.
    """

    rows = session.scalars(
        select(Event)
        .where(Event.journey_id == journey.id)
        .order_by(Event.id.asc())
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for event in rows:
        if str(event.status or "").upper() not in _ACTIONABLE_EVENT_STATUSES:
            continue
        incident = _payload_fingerprint(event.payload_json)
        semantic = {
            "provider_key": str(event.provider_key or ""),
            "provenance": str(event.provenance or ""),
            "event_type": str(event.event_type or ""),
            "severity": str(event.severity or ""),
            "affected_activity_id": str(event.affected_activity_id or ""),
            "incident_fingerprint": incident,
            # When a provider supplied no semantic incident fingerprint, the
            # server-owned occurrence time is the conservative fallback that
            # distinguishes genuinely separate incidents without using UUIDs.
            "fallback_occurred_at": None if incident else str(event.occurred_at or ""),
        }
        identity_material = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_identity = (
            "semantic:"
            + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
        )
        item = grouped.setdefault(
            event_identity,
            {
                "event_identity": event_identity,
                **semantic,
                "source_event_ids": [],
            },
        )
        item["source_event_ids"].append(str(event.id))

    events = sorted(grouped.values(), key=lambda item: str(item["event_identity"]))
    for item in events:
        item["source_event_ids"] = sorted(set(item["source_event_ids"]))
    return {
        "schema": "citynexus.recovery-reality.v2",
        "journey_id": str(journey.id),
        "active_plan_version_id": str(journey.active_plan_version_id or ""),
        "current_activity_id": str(journey.current_activity_id or ""),
        "events": events,
    }


def recovery_reality_fingerprint(basis: Mapping[str, Any]) -> str:
    material = dict(basis)
    # Event UUIDs are provenance, never recovery reality.
    material["events"] = [
        {
            key: value
            for key, value in dict(item).items()
            if key != "source_event_ids"
        }
        for item in material.get("events", [])
        if isinstance(item, Mapping)
    ]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def current_open_recovery_epoch(session: Session, journey_id: str) -> RecoveryEpoch | None:
    return session.scalars(
        select(RecoveryEpoch)
        .where(RecoveryEpoch.journey_id == str(journey_id), RecoveryEpoch.status == OPEN)
        .order_by(RecoveryEpoch.generation.desc())
        .limit(1)
    ).first()


def latest_recovery_epoch(
    session: Session, journey_id: str
) -> RecoveryEpoch | None:
    """Return the newest durable generation without opening a new one."""

    return session.scalars(
        select(RecoveryEpoch)
        .where(RecoveryEpoch.journey_id == str(journey_id))
        .order_by(RecoveryEpoch.generation.desc(), RecoveryEpoch.id.desc())
        .limit(1)
    ).first()


class RecoveryEpochSuperseded(RuntimeError):
    code = RECOVERY_EPOCH_SUPERSEDED

    def __init__(self, *, bound: RecoveryEpoch | None, current: RecoveryEpoch | None, reason: str = ""):
        self.bound = bound
        self.current = current
        self.facts = {
            "code": self.code,
            "bound_recovery_epoch_id": getattr(bound, "id", None),
            "stale_recovery_epoch_id": getattr(bound, "id", None),
            "current_recovery_epoch_id": getattr(current, "id", None),
            "bound_generation": getattr(bound, "generation", None),
            "stale_generation": getattr(bound, "generation", None),
            "current_generation": getattr(current, "generation", None),
            "bound_recovery_reality_fingerprint": getattr(bound, "recovery_reality_fingerprint", None),
            "stale_recovery_reality_fingerprint": getattr(bound, "recovery_reality_fingerprint", None),
            "current_recovery_reality_fingerprint": getattr(current, "recovery_reality_fingerprint", None),
            "reason": reason or "bound recovery epoch is no longer current",
        }
        super().__init__(self.facts["reason"])


def _retire_superseded_epoch_dependents(
    session: Session, epoch: RecoveryEpoch, *, decided_at: str
) -> None:
    """Remove stale authority/candidates from the current executable surface.

    History remains queryable.  Only still-PRESENTED candidates and unspent
    receipts are retired; no old decision is rewritten as if it never existed.
    """

    proposals = session.scalars(
        select(ReplanProposal).where(
            ReplanProposal.recovery_epoch_id == epoch.id,
            ReplanProposal.status == "PRESENTED",
        )
    ).all()
    for proposal in proposals:
        proposal.status = "INVALIDATED"
        proposal.decided_at = decided_at
        plan = session.get(PlanVersion, proposal.proposed_plan_version_id)
        if plan is not None and plan.status == "CANDIDATE":
            plan.status = "REJECTED"

    receipts = session.scalars(
        select(CollectiveAuthorityReceipt).where(
            CollectiveAuthorityReceipt.recovery_epoch_id == epoch.id,
            CollectiveAuthorityReceipt.invalidated_at.is_(None),
        )
    ).all()
    for receipt in receipts:
        receipt.invalidated_at = decided_at
        receipt.invalidation_code = RECOVERY_EPOCH_SUPERSEDED


def ensure_recovery_epoch(session: Session, journey: Journey) -> RecoveryEpoch:
    basis = recovery_reality_basis(session, journey)
    fingerprint = recovery_reality_fingerprint(basis)
    current = current_open_recovery_epoch(session, journey.id)
    if current is not None and (
        current.recovery_reality_fingerprint == fingerprint
        and current.active_plan_version_id_at_open == journey.active_plan_version_id
    ):
        return current

    now = _now()
    if current is not None:
        current.status = SUPERSEDED
        current.closed_at = now
        current.close_reason = "RECOVERY_REALITY_CHANGED"
        _retire_superseded_epoch_dependents(
            session, current, decided_at=now
        )
        # Flush the retirement inside the caller transaction before opening
        # the next generation.  Do not point superseded_by at a row that does
        # not exist yet.
        session.flush()

    max_generation = session.scalar(
        select(func.max(RecoveryEpoch.generation)).where(
            RecoveryEpoch.journey_id == journey.id
        )
    ) or 0
    epoch = RecoveryEpoch(
        id=str(uuid4()),
        journey_id=journey.id,
        generation=int(max_generation) + 1,
        active_plan_version_id_at_open=journey.active_plan_version_id,
        # This is the server-owned material-world basis captured with the
        # incident.  It intentionally shares the semantic recovery fingerprint
        # so provider row churn cannot invalidate a blocked resume.
        material_world_epoch_at_open=fingerprint,
        recovery_reality_fingerprint=fingerprint,
        triggering_event_ids_json=json.dumps(
            sorted(
                {
                    str(event_id)
                    for item in basis["events"]
                    for event_id in item.get("source_event_ids", [])
                }
            ),
            separators=(",", ":"),
        ),
        status=OPEN,
        opened_at=now,
    )
    try:
        # A savepoint contains only the competing OPEN-row insert.  A losing
        # concurrent opener must not roll back unrelated caller work or the
        # durable retirement of stale authority.
        with session.begin_nested():
            session.add(epoch)
            session.flush()
    except IntegrityError:
        session.expire_all()
        winner = current_open_recovery_epoch(session, journey.id)
        if winner is not None:
            return winner
        raise

    if current is not None:
        current.superseded_by_recovery_epoch_id = epoch.id
        session.flush()
    return epoch


def validate_recovery_epoch_binding(
    session: Session,
    journey_id: str,
    bound_recovery_epoch_id: str | None,
    *,
    expected_status: str = OPEN,
) -> RecoveryEpoch:
    bound = (
        session.get(RecoveryEpoch, str(bound_recovery_epoch_id))
        if bound_recovery_epoch_id
        else None
    )

    # A run already bound to a closed/superseded generation is stale by
    # definition.  Reject it without calling ensure_recovery_epoch(): stale
    # work must never mint a fresh generation merely by checking whether it is
    # still authorized.
    if bound is None or bound.status != expected_status:
        current = (
            current_open_recovery_epoch(session, journey_id)
            or latest_recovery_epoch(session, journey_id)
        )
        raise RecoveryEpochSuperseded(
            bound=bound,
            current=current,
            reason=(
                "bound recovery epoch is no longer spendable; stale validation "
                "does not create a successor generation"
            ),
        )

    # When the bound generation is still OPEN, storage status alone is not
    # enough: a provider/Event transaction may already have changed reality
    # before any caller explicitly refreshed the epoch.  Recompute only in
    # this case so current work sees new world facts while stale work stays
    # causally inert.
    journey = session.get(Journey, str(journey_id))
    current = (
        ensure_recovery_epoch(session, journey)
        if journey is not None
        else current_open_recovery_epoch(session, journey_id)
    )
    if current is None or bound.id != current.id:
        raise RecoveryEpochSuperseded(bound=bound, current=current)
    return current


def close_recovery_epoch(
    session: Session, epoch: RecoveryEpoch, *, status: str, reason: str
) -> RecoveryEpoch:
    epoch.status = status
    epoch.closed_at = _now()
    epoch.close_reason = reason
    session.flush()
    return epoch


def supersede_recovery_epoch(session: Session, journey: Journey, reason: str) -> RecoveryEpoch | None:
    """Close the current open epoch as superseded without retargeting callers."""

    current = current_open_recovery_epoch(session, journey.id)
    if current is None:
        return None
    return close_recovery_epoch(session, current, status=SUPERSEDED, reason=reason)


def commit_recovery_epoch(session: Session, epoch: RecoveryEpoch, reason: str = "COMMITTED") -> RecoveryEpoch:
    return close_recovery_epoch(session, epoch, status=COMMITTED, reason=reason)


def semantic_plan_identity(session: Session, plan_version_id: str) -> str:
    """Canonical executable plan identity, excluding proposal/run metadata."""

    plan = session.get(PlanVersion, str(plan_version_id))
    if plan is None:
        return ""
    rows = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no.asc())
    ).all()
    activities = [
        {
            "sequence_no": int(row.sequence_no),
            "activity_type": str(row.activity_type or ""),
            "place_id": str(row.place_id or ""),
            "city_service_id": str(row.city_service_id or ""),
            "start_at": str(row.start_at or ""),
            "end_at": str(row.end_at or ""),
            "travel_from_prev_min": int(row.travel_from_prev_min or 0),
            "estimated_cost_minor": int(row.estimated_cost_minor or 0),
            "currency": str(row.currency or ""),
            "locked": bool(row.locked),
        }
        for row in rows
    ]
    material = {"schema": "citynexus.executable-plan.v1", "activities": activities}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
