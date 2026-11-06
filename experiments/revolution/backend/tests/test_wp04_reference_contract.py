from __future__ import annotations

import pytest

from app.commerce.commitment_policy import (
    CommitmentFacts,
    derive_strength_rank,
    source_strength_code,
)
from app.commerce.idempotency import IdempotencyConflict, MemoryIdempotencyRegistry
from app.integrations.mock.commerce_providers import MockBookingProvider, MockPaymentProvider


def test_exactly_five_source_strength_codes_are_required():
    codes = ["L0", "L1", "L2", "L3", "L4"]
    assert source_strength_code(4, codes) == "L4"
    with pytest.raises(ValueError):
        source_strength_code(4, ["L0", "L1", "L2", "L3"])


def test_payment_alone_does_not_anchor_commitment():
    decision = derive_strength_rank(
        CommitmentFacts(
            payment_succeeded=True,
            fixed_schedule=False,
            user_locked=False,
            provider_confirmed=False,
            easily_replaceable=True,
        )
    )
    assert decision.rank < 4


def test_canonical_multi_factor_commitment_reaches_rank_four():
    decision = derive_strength_rank(
        CommitmentFacts(
            provider_confirmed=True,
            payment_succeeded=True,
            fixed_schedule=True,
            user_locked=True,
            cancellation_loss_minor=12000,
            easily_replaceable=False,
        )
    )
    assert decision.rank == 4
    assert "anchored multi-factor commitment" in decision.reasons


def test_mock_booking_provider_is_deterministic_and_failure_is_explicit():
    provider = MockBookingProvider()
    ok = provider.create_booking(
        merchant_reference="booking-1",
        activity_id="river-cruise",
        amount_minor=12000,
        currency="CNY",
    )
    fail = provider.create_booking(
        merchant_reference="booking-fail",
        activity_id="river-cruise-fail",
        amount_minor=12000,
        currency="CNY",
        force_failure=True,
    )
    assert ok.status == "CONFIRMED"
    assert ok.external_ref
    assert fail.status == "FAILED"
    assert fail.provider_code == "FORCED_FAILURE"


def test_mock_payment_failure_never_claims_success():
    provider = MockPaymentProvider()
    fail = provider.capture(
        merchant_reference="payment-fail", amount_minor=12000, currency="CNY", force_failure=True
    )
    assert fail.status == "FAILED"
    assert fail.status != "SUCCEEDED"


def test_idempotency_same_key_same_request_replays_without_conflict():
    registry = MemoryIdempotencyRegistry()
    payload = {
        "journey_id": "j1",
        "booking_id": "b1",
        "transaction_type": "CAPTURE",
        "amount_minor": 12000,
        "currency": "CNY",
    }
    registry.store(
        "transaction.create:mock-payment:key-123456",
        payload,
        {"transaction_id": "t1", "status": "SUCCEEDED"},
    )
    assert registry.resolve("transaction.create:mock-payment:key-123456", payload) == {
        "transaction_id": "t1",
        "status": "SUCCEEDED",
    }


def test_idempotency_same_key_different_request_conflicts():
    registry = MemoryIdempotencyRegistry()
    key = "transaction.create:mock-payment:key-123456"
    first = {"booking_id": "b1", "amount_minor": 12000, "currency": "CNY"}
    second = {"booking_id": "b1", "amount_minor": 13000, "currency": "CNY"}
    registry.store(key, first, {"transaction_id": "t1"})
    with pytest.raises(IdempotencyConflict):
        registry.resolve(key, second)


from app.commerce.service_reference import (
    CommerceServiceReference,
    MemoryCommerceRepository,
    SelectedActivity,
)


def canonical_activity() -> SelectedActivity:
    return SelectedActivity(
        id="activity-river-cruise",
        journey_id="journey-1",
        semantic_key="place-river-cruise",
        start_at="2026-10-10T15:30:00+08:00",
        end_at="2026-10-10T16:30:00+08:00",
        amount_minor=12000,
        currency="CNY",
        user_locked=True,
        fixed_schedule=True,
        easily_replaceable=False,
    )


def test_reference_canonical_booking_payment_strengthens_same_commitment_to_rank_four():
    repo = MemoryCommerceRepository([canonical_activity()])
    booking_provider = MockBookingProvider()
    payment_provider = MockPaymentProvider()
    service = CommerceServiceReference(repo, booking_provider, payment_provider)
    booking = service.create_booking(journey_id="journey-1", activity_id="activity-river-cruise")
    assert booking.status == "CONFIRMED" and booking.commitment_id
    before = repo.get_commitment(booking.commitment_id)
    transaction = service.capture_booking_payment(
        journey_id="journey-1",
        booking_id=booking.id,
        idempotency_key="payment-key-123456",
    )
    after = repo.get_commitment(booking.commitment_id)
    assert transaction.status == "SUCCEEDED"
    assert after.id == before.id
    assert after.strength_rank == 4
    assert after.semantic_key == "place-river-cruise"
    assert after.scheduled_start_at == "2026-10-10T15:30:00+08:00"


def test_reference_payment_replay_does_not_call_provider_or_strengthen_twice():
    repo = MemoryCommerceRepository([canonical_activity()])
    booking_provider = MockBookingProvider()
    payment_provider = MockPaymentProvider()
    service = CommerceServiceReference(repo, booking_provider, payment_provider)
    booking = service.create_booking(journey_id="journey-1", activity_id="activity-river-cruise")
    first = service.capture_booking_payment(
        journey_id="journey-1", booking_id=booking.id, idempotency_key="payment-key-123456"
    )
    calls = len(payment_provider.calls)
    first_commitment = repo.get_commitment(booking.commitment_id)
    second = service.capture_booking_payment(
        journey_id="journey-1", booking_id=booking.id, idempotency_key="payment-key-123456"
    )
    assert second.id == first.id
    assert len(payment_provider.calls) == calls
    assert repo.get_commitment(booking.commitment_id) == first_commitment


def test_reference_failed_payment_does_not_strengthen_commitment_to_paid_or_anchored():
    repo = MemoryCommerceRepository([canonical_activity()])
    service = CommerceServiceReference(repo, MockBookingProvider(), MockPaymentProvider())
    booking = service.create_booking(journey_id="journey-1", activity_id="activity-river-cruise")
    before = repo.get_commitment(booking.commitment_id)
    transaction = service.capture_booking_payment(
        journey_id="journey-1",
        booking_id=booking.id,
        idempotency_key="payment-key-fail-123",
        force_failure=True,
    )
    after = repo.get_commitment(booking.commitment_id)
    assert transaction.status == "FAILED"
    assert after == before


def test_fixed_unpaid_can_outrank_paid_low_impact_commitment():
    fixed_unpaid = derive_strength_rank(
        CommitmentFacts(provider_confirmed=True, fixed_schedule=True, payment_succeeded=False)
    )
    paid_low_impact = derive_strength_rank(
        CommitmentFacts(
            provider_confirmed=True,
            payment_succeeded=True,
            fixed_schedule=False,
            user_locked=False,
            cancellation_loss_minor=0,
            easily_replaceable=True,
        )
    )
    assert fixed_unpaid.rank > paid_low_impact.rank


def test_canonical_booking_is_not_c4_until_payment_is_captured():
    repo = MemoryCommerceRepository([canonical_activity()])
    service = CommerceServiceReference(repo, MockBookingProvider(), MockPaymentProvider())
    booking = service.create_booking(journey_id="journey-1", activity_id="activity-river-cruise")
    commitment = repo.get_commitment(booking.commitment_id)
    assert commitment.strength_rank == 3
