"""Historical duplicate confirmation identity detection / quarantine.

The unique index ``(journey_id, confirmation_ref)`` on
``transport_reservations`` is the real serialization point for concurrent C4
confirmation.  Before it can be enforced, any pre-existing duplicate identity
must be detected, reported and quarantined rather than silently collapsing two
different bookings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Commitment, Event, Journey, TransportReservation
from app.planning.anchors import anchor_claim_identity

QUARANTINE_EVENT = "RESERVATION_IDENTITY_DUPLICATE"
QUARANTINE_MARKER = "QUARANTINED_DUPLICATE_IDENTITY"


@dataclass(frozen=True)
class DuplicateReport:
    journey_id: str
    confirmation_ref: str
    reservation_ids: tuple[str, ...]
    winner_id: str
    quarantined_ids: tuple[str, ...]
    repaired: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "journey_id": self.journey_id,
            "confirmation_ref": self.confirmation_ref,
            "reservation_ids": list(self.reservation_ids),
            "winner_id": self.winner_id,
            "quarantined_ids": list(self.quarantined_ids),
            "repaired": self.repaired,
        }


def detect_duplicate_confirmation_identities(
    session: Session, journey_id: str | None = None
) -> list[DuplicateReport]:
    """Report every historical duplicate ``(journey_id, confirmation_ref)``."""

    query = (
        select(
            TransportReservation.journey_id,
            TransportReservation.confirmation_ref,
            func.count(TransportReservation.id),
        )
        .where(
            TransportReservation.confirmation_ref.is_not(None),
            TransportReservation.status != QUARANTINE_MARKER,
        )
        .group_by(TransportReservation.journey_id, TransportReservation.confirmation_ref)
        .having(func.count(TransportReservation.id) > 1)
    )
    if journey_id is not None:
        query = query.where(TransportReservation.journey_id == journey_id)
    reports: list[DuplicateReport] = []
    for row_journey, reference, _count in session.execute(query).all():
        rows = session.scalars(
            select(TransportReservation)
            .where(
                TransportReservation.journey_id == row_journey,
                TransportReservation.confirmation_ref == reference,
                TransportReservation.status != QUARANTINE_MARKER,
            )
            .order_by(TransportReservation.captured_at, TransportReservation.id)
        ).all()
        winner, *losers = rows
        reports.append(
            DuplicateReport(
                journey_id=str(row_journey),
                confirmation_ref=str(reference),
                reservation_ids=tuple(item.id for item in rows),
                winner_id=winner.id,
                quarantined_ids=tuple(item.id for item in losers),
                repaired=False,
            )
        )
    return reports


def quarantine_duplicate_confirmation_identities(
    session: Session, journey_id: str | None = None
) -> list[DuplicateReport]:
    """Quarantine losers so the unique identity can be enforced.

    The earliest reservation remains authoritative; later duplicates are marked
    ``QUARANTINED_DUPLICATE_IDENTITY``, their draft pointer is released, and a
    durable Event records the repair.  Nothing is deleted.
    """

    reports = detect_duplicate_confirmation_identities(session, journey_id)
    repaired: list[DuplicateReport] = []
    now = datetime.now(timezone.utc).isoformat()
    for report in reports:
        losers = [
            session.get(TransportReservation, item_id)
            for item_id in report.quarantined_ids
        ]
        quarantined: list[str] = []
        for row in losers:
            if row is None:
                continue
            row.status = QUARANTINE_MARKER
            row.draft_id = None
            quarantined.append(row.id)
        session.add(
            Event(
                id=str(uuid4()),
                journey_id=report.journey_id,
                provider_key="citynexus-reservation",
                provenance="SYSTEM_DERIVED",
                event_type="USER_CHANGE",
                severity="MAJOR",
                status="RESOLVED",
                affected_activity_id=None,
                occurred_at=now,
                detected_at=now,
                resolved_at=now,
                payload_json=_json(
                    {
                        "kind": QUARANTINE_EVENT,
                        "confirmation_ref": report.confirmation_ref,
                        "winner_id": report.winner_id,
                        "quarantined_ids": quarantined,
                        "policy": QUARANTINE_MARKER,
                    }
                ),
                created_at=now,
            )
        )
        repaired.append(
            DuplicateReport(
                journey_id=report.journey_id,
                confirmation_ref=report.confirmation_ref,
                reservation_ids=report.reservation_ids,
                winner_id=report.winner_id,
                quarantined_ids=tuple(quarantined),
                repaired=True,
            )
        )
    return repaired


ANCHOR_RETIRE_EVENT = "C4_ANCHOR_SUPERSEDED_BY_NEWER_CONFIRMATION"
ANCHOR_RETIRE_MARKER = "SUPERSEDED_BY_NEWER_CONFIRMATION"


def active_anchor_commitments(session: Session, journey_id: str) -> list[Commitment]:
    """Every ACTIVE C4 commitment of one journey, anchor or not.

    Deliberately not filtered by ``anchor_role`` here: the caller needs to see the rows
    ``anchor_claim_identity`` can return ``None`` for, otherwise "no other anchor claims
    this node" and "this journey has no C4 rows at all" read the same.
    """

    return list(
        session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey_id,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
            )
        ).all()
    )


def retire_superseded_active_anchor(
    session: Session,
    journey: Journey,
    *,
    identity: str,
    replaced_by_external_ref: str,
    now: str,
) -> list[Commitment]:
    """Move every older ACTIVE anchor of one node identity out of ACTIVE before a new claim.

    A traveller confirms a re-issued ticket for the same arrival by confirming a *different*
    ``confirmation_ref`` - the guard at ``operations/v9.py`` keys on the provider reference,
    so it never sees these two as the same booking.  Leaving both rows ACTIVE made the
    planner answer its own duplicate: ``resolve_plan_anchors`` emits one anchor per row, and
    two anchors for one node overlap with themselves, which surfaced as
    ``C4_ANCHOR_OVERLAP:key|key`` on a journey that had nothing wrong with it.

    The newer confirmation wins because it is the traveller's later statement about the same
    node, and because the door that would let them retire an anchor themselves -
    ``POST /api/v1/bookings/<id>/cancel`` - answers 422 ``only the supplied mock-booking
    provider is enabled in WP04`` for an imported reservation, so refusing here would strand
    the journey with no way out.  Rows are never deleted: the retired commitment keeps its
    ``external_ref`` and its Booking, and carries the marker plus a durable Event, the same
    shape ``quarantine_duplicate_confirmation_identities`` uses.
    """

    if not identity:
        return []
    retired: list[Commitment] = []
    for row in active_anchor_commitments(session, journey.id):
        try:
            policy = json.loads(row.policy_json or "{}")
        except json.JSONDecodeError:
            policy = {}
        if not isinstance(policy, dict):
            policy = {}
        if anchor_claim_identity(policy, journey_id=journey.id) != identity:
            continue
        policy["retire_reason"] = ANCHOR_RETIRE_MARKER
        policy["superseded_by_confirmation_ref"] = replaced_by_external_ref
        policy["retired_at"] = now
        row.status = "CANCELLED"
        row.policy_json = _json(policy)
        row.updated_at = now
        retired.append(row)
    if not retired:
        return []
    session.flush()
    session.add(
        Event(
            id=str(uuid4()),
            journey_id=journey.id,
            provider_key="citynexus-reservation",
            provenance="SYSTEM_DERIVED",
            event_type="USER_CHANGE",
            severity="MAJOR",
            status="RESOLVED",
            affected_activity_id=None,
            occurred_at=now,
            detected_at=now,
            resolved_at=now,
            payload_json=_json(
                {
                    "kind": ANCHOR_RETIRE_EVENT,
                    "identity": identity,
                    "retired_commitment_ids": [row.id for row in retired],
                    "replaced_by_confirmation_ref": replaced_by_external_ref,
                    "policy": ANCHOR_RETIRE_MARKER,
                }
            ),
            created_at=now,
        )
    )
    session.flush()
    return retired


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
