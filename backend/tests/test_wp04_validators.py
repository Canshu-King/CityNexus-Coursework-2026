from __future__ import annotations

import pytest

from app.commerce.validators import (
    BookingTargetContext,
    CommerceValidationError,
    PaymentTargetContext,
    assert_no_sensitive_payment_fields,
    stable_semantic_key,
    validate_booking_target,
    validate_idempotency_key,
    validate_payment_target,
    validate_transaction_target_ids,
)


def target(**overrides):
    values = {
        "journey_id": "j1",
        "journey_status": "PLANNED",
        "selected_plan_id": "p1",
        "selected_plan_status": "SELECTED",
        "activity_id": "a1",
        "activity_plan_id": "p1",
        "activity_status": "PLANNED",
        "activity_place_id": "place-river-cruise",
        "activity_service_id": None,
    }
    values.update(overrides)
    return BookingTargetContext(**values)


def test_booking_target_must_be_selected_plan_activity():
    validate_booking_target(target())
    with pytest.raises(CommerceValidationError):
        validate_booking_target(target(activity_plan_id="stale-plan"))


def test_booking_rejects_draft_journey_and_terminal_activity():
    with pytest.raises(CommerceValidationError):
        validate_booking_target(target(journey_status="DRAFT"))
    with pytest.raises(CommerceValidationError):
        validate_booking_target(target(activity_status="COMPLETED"))


def test_stable_semantic_key_prefers_cross_plan_place_identity():
    assert stable_semantic_key(target()) == "place-river-cruise"
    assert (
        stable_semantic_key(target(activity_place_id=None, activity_service_id="service-1"))
        == "service-1"
    )


def test_transaction_requires_booking_xor_order():
    validate_transaction_target_ids(booking_id="b1", order_id=None)
    validate_transaction_target_ids(booking_id=None, order_id="o1")
    with pytest.raises(CommerceValidationError):
        validate_transaction_target_ids(booking_id=None, order_id=None)
    with pytest.raises(CommerceValidationError):
        validate_transaction_target_ids(booking_id="b1", order_id="o1")


def test_payment_amount_currency_and_ownership_are_authoritative():
    context = PaymentTargetContext("j1", "j1", 12000, "CNY", "CONFIRMED")
    validate_payment_target(context, requested_amount_minor=12000, requested_currency="cny")
    with pytest.raises(CommerceValidationError):
        validate_payment_target(context, requested_amount_minor=11999, requested_currency="CNY")
    with pytest.raises(CommerceValidationError):
        validate_payment_target(
            PaymentTargetContext("j1", "j2", 12000, "CNY", "CONFIRMED"),
            requested_amount_minor=12000,
            requested_currency="CNY",
        )


def test_sensitive_card_credentials_are_rejected_recursively():
    assert_no_sensitive_payment_fields({"payment_method_token": "tok_demo"})
    with pytest.raises(CommerceValidationError):
        assert_no_sensitive_payment_fields({"nested": {"cvv": "123"}})
    with pytest.raises(CommerceValidationError):
        assert_no_sensitive_payment_fields({"card_number": "4111111111111111"})


def test_idempotency_key_has_a_small_bounded_contract():
    assert validate_idempotency_key("demo-key-123") == "demo-key-123"
    with pytest.raises(CommerceValidationError):
        validate_idempotency_key("short")
