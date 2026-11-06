from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CommerceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BookingTargetContext:
    journey_id: str
    journey_status: str
    selected_plan_id: str | None
    selected_plan_status: str | None
    activity_id: str
    activity_plan_id: str
    activity_status: str
    activity_place_id: str | None = None
    activity_service_id: str | None = None


@dataclass(frozen=True)
class PaymentTargetContext:
    journey_id: str
    target_journey_id: str
    authoritative_amount_minor: int
    authoritative_currency: str
    target_status: str


def validate_booking_target(context: BookingTargetContext) -> None:
    if context.journey_status != "PLANNED":
        raise CommerceValidationError("booking requires a PLANNED Journey")
    if not context.selected_plan_id or context.selected_plan_status != "SELECTED":
        raise CommerceValidationError("booking requires the selected PlanVersion")
    if context.activity_plan_id != context.selected_plan_id:
        raise CommerceValidationError("activity does not belong to the selected PlanVersion")
    if context.activity_status in {"COMPLETED", "SKIPPED", "CANCELLED"}:
        raise CommerceValidationError("terminal activity cannot be booked")


def stable_semantic_key(context: BookingTargetContext) -> str:
    if context.activity_place_id:
        return context.activity_place_id
    if context.activity_service_id:
        return context.activity_service_id
    raise CommerceValidationError(
        "bookable activity requires a stable place_id or city_service_id semantic identity"
    )


def validate_transaction_target_ids(*, booking_id: str | None, order_id: str | None) -> None:
    if (booking_id is None) == (order_id is None):
        raise CommerceValidationError("exactly one of booking_id or order_id is required")


def validate_payment_target(
    context: PaymentTargetContext,
    *,
    requested_amount_minor: int,
    requested_currency: str,
) -> None:
    if context.target_journey_id != context.journey_id:
        raise CommerceValidationError("payment target does not belong to Journey")
    if requested_amount_minor != context.authoritative_amount_minor:
        raise CommerceValidationError("payment amount must match authoritative target total")
    if requested_currency.upper() != context.authoritative_currency.upper():
        raise CommerceValidationError("payment currency must match authoritative target currency")
    if context.target_status not in {"CONFIRMED", "PENDING"}:
        raise CommerceValidationError("payment target is not payable in its current state")


SENSITIVE_KEY_FRAGMENTS = (
    "card_number",
    "cardnumber",
    "full_pan",
    "pan_number",
    "security_code",
    "cvv",
    "cvc",
    "track_data",
)


def find_sensitive_payment_fields(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_").replace(" ", "_")
            if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS):
                hits.append(".".join((*path, str(key))))
            hits.extend(find_sensitive_payment_fields(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(find_sensitive_payment_fields(child, (*path, str(index))))
    return hits


def assert_no_sensitive_payment_fields(value: Any) -> None:
    hits = find_sensitive_payment_fields(value)
    if hits:
        raise CommerceValidationError(
            "sensitive card credentials are not accepted/persisted: " + ", ".join(hits)
        )


def validate_idempotency_key(value: str) -> str:
    key = value.strip()
    if not 8 <= len(key) <= 128:
        raise CommerceValidationError("Idempotency-Key must be 8..128 characters")
    return key
