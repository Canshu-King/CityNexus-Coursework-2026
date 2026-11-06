from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BookingSlot:
    start_at: str
    end_at: str
    provider_key: str
    provider_slot_ref: str


@dataclass(frozen=True)
class BookingProviderOutcome:
    provider_key: str
    status: str
    external_ref: str | None
    amount_minor: int
    currency: str
    cancellation_policy: dict
    provider_code: str | None = None


@dataclass(frozen=True)
class PaymentProviderOutcome:
    provider_key: str
    status: str
    external_ref: str | None
    provider_code: str | None = None


class BookingProvider(Protocol):
    provider_key: str

    def availability(self, service_id: str, start_at: str, end_at: str) -> list[BookingSlot]: ...

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
    ) -> BookingProviderOutcome: ...

    def cancel_booking(
        self, external_ref: str, *, force_failure: bool = False
    ) -> BookingProviderOutcome: ...

    def get_booking(self, external_ref: str) -> BookingProviderOutcome: ...


class PaymentProvider(Protocol):
    provider_key: str

    def authorize(
        self,
        *,
        merchant_reference: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome: ...

    def capture(
        self,
        *,
        merchant_reference: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome: ...

    def refund(
        self,
        *,
        external_ref: str,
        amount_minor: int,
        currency: str,
        force_failure: bool = False,
    ) -> PaymentProviderOutcome: ...

    def void(self, *, external_ref: str, force_failure: bool = False) -> PaymentProviderOutcome: ...

    def get_status(self, external_ref: str) -> PaymentProviderOutcome: ...
