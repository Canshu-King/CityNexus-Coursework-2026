from __future__ import annotations

from uuid import UUID, uuid5

from app.integrations.commerce_ports import (
    BookingProviderOutcome,
    BookingSlot,
    PaymentProviderOutcome,
)

_NAMESPACE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _uuid(label: str) -> str:
    return str(uuid5(_NAMESPACE, label))


def _booking_ref(merchant_reference: str, state: str) -> str:
    return f"mock-booking:{_uuid(merchant_reference)}:{state.lower()}"


def _payment_ref(merchant_reference: str, operation: str, state: str) -> str:
    return f"mock-payment:{_uuid(merchant_reference + ':' + operation)}:{state.lower()}"


def _tail_state(external_ref: str) -> str:
    return external_ref.rsplit(":", 1)[-1].upper()


class MockBookingProvider:
    provider_key = "mock-booking"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def availability(self, service_id: str, start_at: str, end_at: str) -> list[BookingSlot]:
        self.calls.append(
            ("availability", {"service_id": service_id, "start_at": start_at, "end_at": end_at})
        )
        slot = BookingSlot(
            start_at="2026-10-10T15:30:00+08:00",
            end_at="2026-10-10T16:30:00+08:00",
            provider_key=self.provider_key,
            provider_slot_ref="slot-river-cruise-1530",
        )
        from datetime import datetime

        requested_start = datetime.fromisoformat(start_at)
        requested_end = datetime.fromisoformat(end_at)
        if requested_start > datetime.fromisoformat(
            slot.start_at
        ) or requested_end < datetime.fromisoformat(slot.end_at):
            return []
        return [slot]

    def create_booking(
        self,
        *,
        merchant_reference: str,
        activity_id: str,
        service_id: str | None = None,
        provider_slot_ref: str | None = None,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> BookingProviderOutcome:
        self.calls.append(
            (
                "create",
                {
                    "merchant_reference": merchant_reference,
                    "activity_id": activity_id,
                    "service_id": service_id,
                    "provider_slot_ref": provider_slot_ref,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "force_failure": force_failure,
                },
            )
        )
        status = "FAILED" if force_failure else "CONFIRMED"
        return BookingProviderOutcome(
            provider_key=self.provider_key,
            status=status,
            external_ref=_booking_ref(merchant_reference, status),
            amount_minor=amount_minor,
            currency=currency,
            cancellation_policy={
                "cancellable": True,
                "cancellable_before_payment": True,
                "loss_minor": 0,
            },
            provider_code="FORCED_FAILURE" if force_failure else "OK",
        )

    def cancel_booking(
        self, external_ref: str, *, force_failure: bool = False
    ) -> BookingProviderOutcome:
        self.calls.append(
            ("cancel", {"external_ref": external_ref, "force_failure": force_failure})
        )
        prior = _tail_state(external_ref)
        if force_failure:
            state = prior
            code = "FORCED_CANCEL_FAILURE"
        else:
            state = "CANCELLED"
            code = "OK"
        base = external_ref.rsplit(":", 1)[0]
        return BookingProviderOutcome(
            provider_key=self.provider_key,
            status=state,
            external_ref=f"{base}:{state.lower()}",
            amount_minor=0,
            currency="CNY",
            cancellation_policy={
                "cancellable": True,
                "cancellable_before_payment": True,
                "loss_minor": 0,
            },
            provider_code=code,
        )

    def get_booking(self, external_ref: str) -> BookingProviderOutcome:
        self.calls.append(("status", {"external_ref": external_ref}))
        return BookingProviderOutcome(
            provider_key=self.provider_key,
            status=_tail_state(external_ref),
            external_ref=external_ref,
            amount_minor=0,
            currency="CNY",
            cancellation_policy={
                "cancellable": True,
                "cancellable_before_payment": True,
                "loss_minor": 0,
            },
            provider_code="OK",
        )


class MockPaymentProvider:
    provider_key = "mock-payment"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _new(
        self,
        operation: str,
        merchant_reference: str,
        amount_minor: int,
        currency: str,
        success_status: str,
        force_failure: bool,
    ) -> PaymentProviderOutcome:
        self.calls.append(
            (
                operation,
                {
                    "merchant_reference": merchant_reference,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "force_failure": force_failure,
                },
            )
        )
        status = "FAILED" if force_failure else success_status
        return PaymentProviderOutcome(
            provider_key=self.provider_key,
            status=status,
            external_ref=_payment_ref(merchant_reference, operation, status),
            provider_code="FORCED_FAILURE" if force_failure else "OK",
        )

    def authorize(
        self,
        *,
        merchant_reference: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome:
        return self._new(
            "authorize", merchant_reference, amount_minor, currency, "AUTHORIZED", force_failure
        )

    def capture(
        self,
        *,
        merchant_reference: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome:
        return self._new(
            "capture", merchant_reference, amount_minor, currency, "SUCCEEDED", force_failure
        )

    def refund(
        self,
        *,
        external_ref: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome:
        self.calls.append(
            (
                "refund",
                {
                    "external_ref": external_ref,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "force_failure": force_failure,
                },
            )
        )
        if force_failure:
            return PaymentProviderOutcome(
                self.provider_key, _tail_state(external_ref), external_ref, "FORCED_REFUND_FAILURE"
            )
        base = external_ref.rsplit(":", 1)[0]
        return PaymentProviderOutcome(self.provider_key, "REFUNDED", f"{base}:refunded", "OK")

    def void(self, *, external_ref: str, force_failure: bool = False) -> PaymentProviderOutcome:
        self.calls.append(("void", {"external_ref": external_ref, "force_failure": force_failure}))
        if force_failure:
            return PaymentProviderOutcome(
                self.provider_key, _tail_state(external_ref), external_ref, "FORCED_VOID_FAILURE"
            )
        base = external_ref.rsplit(":", 1)[0]
        return PaymentProviderOutcome(self.provider_key, "VOIDED", f"{base}:voided", "OK")

    def get_status(self, external_ref: str) -> PaymentProviderOutcome:
        self.calls.append(("status", {"external_ref": external_ref}))
        return PaymentProviderOutcome(
            self.provider_key, _tail_state(external_ref), external_ref, "OK"
        )
