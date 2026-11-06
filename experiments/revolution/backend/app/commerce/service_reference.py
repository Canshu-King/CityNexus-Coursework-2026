from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID, uuid5

from app.commerce.commitment_policy import CommitmentFacts, derive_strength_rank
from app.commerce.idempotency import MemoryIdempotencyRegistry
from app.integrations.commerce_ports import BookingProvider, PaymentProvider

_NAMESPACE = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def _id(label: str) -> str:
    return str(uuid5(_NAMESPACE, label))


@dataclass(frozen=True)
class SelectedActivity:
    id: str
    journey_id: str
    semantic_key: str
    start_at: str
    end_at: str
    amount_minor: int
    currency: str
    user_locked: bool
    fixed_schedule: bool
    easily_replaceable: bool = False


@dataclass(frozen=True)
class BookingRecord:
    id: str
    journey_id: str
    activity_id: str
    status: str
    provider_key: str
    external_ref: str | None
    amount_minor: int
    currency: str
    commitment_id: str | None


@dataclass(frozen=True)
class CommitmentRecord:
    id: str
    journey_id: str
    activity_id: str
    status: str
    strength_rank: int
    semantic_key: str
    scheduled_start_at: str
    scheduled_end_at: str
    booking_external_ref: str | None = None
    payment_external_ref: str | None = None


@dataclass(frozen=True)
class PaymentTransactionRecord:
    id: str
    journey_id: str
    booking_id: str | None
    status: str
    transaction_type: str
    amount_minor: int
    currency: str
    provider_key: str
    external_ref: str | None


class CommerceRepository(Protocol):
    def get_selected_activity(self, journey_id: str, activity_id: str) -> SelectedActivity: ...
    def save_booking(self, record: BookingRecord) -> None: ...
    def get_booking(self, booking_id: str) -> BookingRecord: ...
    def save_commitment(self, record: CommitmentRecord) -> None: ...
    def get_commitment(self, commitment_id: str) -> CommitmentRecord: ...
    def save_transaction(self, record: PaymentTransactionRecord) -> None: ...
    def get_transaction(self, transaction_id: str) -> PaymentTransactionRecord: ...


class MemoryCommerceRepository:
    def __init__(self, activities: list[SelectedActivity]) -> None:
        self.activities = {(a.journey_id, a.id): a for a in activities}
        self.bookings: dict[str, BookingRecord] = {}
        self.commitments: dict[str, CommitmentRecord] = {}
        self.transactions: dict[str, PaymentTransactionRecord] = {}

    def get_selected_activity(self, journey_id: str, activity_id: str) -> SelectedActivity:
        return self.activities[(journey_id, activity_id)]

    def save_booking(self, record: BookingRecord) -> None:
        self.bookings[record.id] = record

    def get_booking(self, booking_id: str) -> BookingRecord:
        return self.bookings[booking_id]

    def save_commitment(self, record: CommitmentRecord) -> None:
        self.commitments[record.id] = record

    def get_commitment(self, commitment_id: str) -> CommitmentRecord:
        return self.commitments[commitment_id]

    def save_transaction(self, record: PaymentTransactionRecord) -> None:
        self.transactions[record.id] = record

    def get_transaction(self, transaction_id: str) -> PaymentTransactionRecord:
        return self.transactions[transaction_id]


class CommerceServiceReference:
    """Framework-free orchestration proof for Codex to port to SQLAlchemy.

    It intentionally omits HTTP, auditing and DB transaction mechanics. Those belong
    to the application adapter. It proves provider-authoritative outcome, semantic
    commitment strengthening and idempotent payment replay.
    """

    def __init__(
        self,
        repo: CommerceRepository,
        booking_provider: BookingProvider,
        payment_provider: PaymentProvider,
        idempotency: MemoryIdempotencyRegistry | None = None,
    ) -> None:
        self.repo = repo
        self.booking_provider = booking_provider
        self.payment_provider = payment_provider
        self.idempotency = idempotency or MemoryIdempotencyRegistry()

    def create_booking(
        self, *, journey_id: str, activity_id: str, force_failure: bool = False
    ) -> BookingRecord:
        activity = self.repo.get_selected_activity(journey_id, activity_id)
        booking_id = _id(f"booking:{journey_id}:{activity_id}")
        outcome = self.booking_provider.create_booking(
            merchant_reference=booking_id,
            activity_id=activity.id,
            amount_minor=activity.amount_minor,
            currency=activity.currency,
            force_failure=force_failure,
        )
        commitment_id: str | None = None
        if outcome.status == "CONFIRMED":
            commitment_id = _id(
                f"commitment:{journey_id}:{activity.semantic_key}:{activity.start_at}"
            )
            decision = derive_strength_rank(
                CommitmentFacts(
                    provider_confirmed=True,
                    payment_succeeded=False,
                    fixed_schedule=activity.fixed_schedule,
                    user_locked=activity.user_locked,
                    cancellation_loss_minor=int(outcome.cancellation_policy.get("loss_minor", 0)),
                    easily_replaceable=activity.easily_replaceable,
                )
            )
            self.repo.save_commitment(
                CommitmentRecord(
                    id=commitment_id,
                    journey_id=journey_id,
                    activity_id=activity.id,
                    status="ACTIVE",
                    strength_rank=decision.rank,
                    semantic_key=activity.semantic_key,
                    scheduled_start_at=activity.start_at,
                    scheduled_end_at=activity.end_at,
                    booking_external_ref=outcome.external_ref,
                )
            )
        record = BookingRecord(
            id=booking_id,
            journey_id=journey_id,
            activity_id=activity.id,
            status=outcome.status,
            provider_key=outcome.provider_key,
            external_ref=outcome.external_ref,
            amount_minor=activity.amount_minor,
            currency=activity.currency,
            commitment_id=commitment_id,
        )
        self.repo.save_booking(record)
        return record

    def capture_booking_payment(
        self,
        *,
        journey_id: str,
        booking_id: str,
        idempotency_key: str,
        force_failure: bool = False,
    ) -> PaymentTransactionRecord:
        booking = self.repo.get_booking(booking_id)
        if booking.journey_id != journey_id:
            raise ValueError("booking belongs to a different journey")
        if booking.status != "CONFIRMED":
            raise ValueError("booking is not confirmed")
        payload = {
            "journey_id": journey_id,
            "provider_key": self.payment_provider.provider_key,
            "booking_id": booking_id,
            "transaction_type": "CAPTURE",
            "amount_minor": booking.amount_minor,
            "currency": booking.currency,
        }
        scoped = f"transaction.create:{self.payment_provider.provider_key}:{idempotency_key}"
        replay = self.idempotency.resolve(scoped, payload)
        if replay is not None:
            return self.repo.get_transaction(replay["transaction_id"])

        transaction_id = _id(f"transaction:{journey_id}:{booking_id}:{idempotency_key}")
        outcome = self.payment_provider.capture(
            merchant_reference=transaction_id,
            amount_minor=booking.amount_minor,
            currency=booking.currency,
            force_failure=force_failure,
        )
        transaction = PaymentTransactionRecord(
            id=transaction_id,
            journey_id=journey_id,
            booking_id=booking_id,
            status=outcome.status,
            transaction_type="CAPTURE",
            amount_minor=booking.amount_minor,
            currency=booking.currency,
            provider_key=outcome.provider_key,
            external_ref=outcome.external_ref,
        )
        self.repo.save_transaction(transaction)

        if outcome.status == "SUCCEEDED" and booking.commitment_id:
            commitment = self.repo.get_commitment(booking.commitment_id)
            activity = self.repo.get_selected_activity(journey_id, booking.activity_id)
            decision = derive_strength_rank(
                CommitmentFacts(
                    provider_confirmed=True,
                    payment_succeeded=True,
                    fixed_schedule=activity.fixed_schedule,
                    user_locked=activity.user_locked,
                    cancellation_loss_minor=booking.amount_minor,
                    easily_replaceable=activity.easily_replaceable,
                )
            )
            self.repo.save_commitment(
                replace(
                    commitment,
                    strength_rank=max(commitment.strength_rank, decision.rank),
                    payment_external_ref=outcome.external_ref,
                )
            )

        self.idempotency.store(
            scoped,
            payload,
            {"transaction_id": transaction.id, "status": transaction.status},
        )
        return transaction
