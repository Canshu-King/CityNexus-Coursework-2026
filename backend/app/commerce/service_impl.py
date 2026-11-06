"""SQLAlchemy adapter for the supplied WP04 commerce reference modules."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commerce.commitment_policy import (
    CommitmentFacts,
    derive_strength_rank,
    source_strength_code,
)
from app.commerce.idempotency import IdempotencyConflict, canonical_request_signature
from app.commerce.order_pricing import OrderLineRequest, price_order
from app.commerce.source_types_generated import SOURCE_COMMITMENT_SOURCE_TYPES
from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS
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
from app.core.audit import record_audit
from app.replanning.commitment_remap import (
    RemapConflict as CommitmentRemapConflict,
    resolve_commitment_target,
)
from app.db.models import (
    Activity,
    Booking,
    CityService,
    Commitment,
    IdempotencyKey,
    Journey,
    Order,
    PaymentTransaction,
    PlanVersion,
    ProviderCall,
)
from app.integrations.commerce_ports import BookingProvider, PaymentProvider


class CommerceConflict(ValueError):
    """A valid commerce request conflicts with current persisted state."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_iso(value: str) -> str:
    """Normalize persisted fixture timestamps to the contract's +08:00 form."""

    return datetime.fromisoformat(value).isoformat()


def load_catalog() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "data" / "mock" / "wp04_commerce_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _provider_call(
    session: Session,
    *,
    provider_key: str,
    operation: str,
    request: dict[str, Any],
    response: dict[str, Any],
    ok: bool,
) -> None:
    assert_no_sensitive_payment_fields(request)
    assert_no_sensitive_payment_fields(response)
    session.add(
        ProviderCall(
            id=str(uuid4()),
            provider_key=provider_key,
            operation=operation,
            request_json=dump(request),
            response_json=dump(response),
            status="SUCCESS" if ok else "FAILURE",
            latency_ms=0,
            created_at=now_iso(),
        )
    )


def selected_activity(
    session: Session, journey_id: str, activity_id: str
) -> tuple[Journey, PlanVersion, Activity, BookingTargetContext]:
    journey = session.get(Journey, journey_id)
    if journey is None:
        raise CommerceValidationError("Journey not found")
    if not journey.active_plan_version_id:
        raise CommerceValidationError("Journey has no selected plan")
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    if plan is None or plan.journey_id != journey.id:
        raise CommerceValidationError("selected PlanVersion is missing/inconsistent")
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise CommerceValidationError("Activity not found")
    context = BookingTargetContext(
        journey_id=journey.id,
        journey_status=journey.status,
        selected_plan_id=journey.active_plan_version_id,
        selected_plan_status=plan.status,
        activity_id=activity.id,
        activity_plan_id=activity.plan_version_id,
        activity_status=activity.status,
        activity_place_id=activity.place_id,
        activity_service_id=activity.city_service_id,
    )
    validate_booking_target(context)
    return journey, plan, activity, context


def _assert_feasible(
    session: Session,
    journey: Journey,
    plan: PlanVersion,
    callback: Callable[[Session, Journey, PlanVersion], Any] | None,
) -> None:
    if callback is None:
        return
    result = callback(session, journey, plan)
    if result is not None and not getattr(result, "passed", True):
        failures = ", ".join(getattr(result, "failures", ()))
        raise CommerceValidationError(
            "selected plan is no longer feasible" + (f": {failures}" if failures else "")
        )


def _validate_source_codes(source_strength_codes: Sequence[str]) -> None:
    if tuple(source_strength_codes) != tuple(SOURCE_COMMITMENT_STRENGTHS):
        raise CommerceValidationError("source commitment strengths do not match frozen v0.4.2 YAML")


def _validate_source_type(value: str) -> str:
    if value not in SOURCE_COMMITMENT_SOURCE_TYPES:
        raise CommerceValidationError("source commitment type is not in frozen v0.4.2 YAML")
    return value


def _booking_policy(
    *,
    context: BookingTargetContext,
    activity: Activity,
    external_ref: str | None,
    cancellation_policy: dict[str, Any],
    source_type: str,
) -> tuple[dict[str, Any], int]:
    decision = derive_strength_rank(
        CommitmentFacts(
            provider_confirmed=True,
            payment_succeeded=False,
            fixed_schedule=bool(activity.locked),
            user_locked=bool(activity.locked),
            cancellation_loss_minor=int(cancellation_policy.get("loss_minor", 0)),
            easily_replaceable=not bool(activity.locked),
        )
    )
    policy = {
        "semantic_key": stable_semantic_key(context),
        "scheduled_start_at": _canonical_iso(activity.start_at),
        "scheduled_end_at": _canonical_iso(activity.end_at),
        "fixed_schedule": bool(activity.locked),
        "user_locked": bool(activity.locked),
        "easily_replaceable": not bool(activity.locked),
        "strength_rank": decision.rank,
        "strength_reasons": list(decision.reasons),
        "source_type": _validate_source_type(source_type),
        "source_refs": {"booking_external_ref": external_ref},
    }
    return policy, decision.rank


def _remap_commitment_to_active_plan(session: Session, commitment: Commitment) -> None:
    """Keep a commitment addressable before its payment is allowed to promote it.

    ``Commitment.activity_id`` is PlanVersion-local.  A capture that arrives after
    a concurrent replan would otherwise promote a pointer into a superseded plan
    to C4 truth, which is exactly the "protected commitment" the traveller believes
    they hold.  Either the now-active plan contains the same commitment target — by
    structural identity, not by name — or the capture fails closed.
    """

    journey = session.get(Journey, commitment.journey_id)
    if journey is None or not journey.active_plan_version_id or not commitment.activity_id:
        return
    anchored = session.get(Activity, commitment.activity_id)
    if anchored is not None and anchored.plan_version_id == journey.active_plan_version_id:
        return
    policy = json.loads(commitment.policy_json or "{}")
    candidates = session.scalars(
        select(Activity).where(Activity.plan_version_id == journey.active_plan_version_id)
    ).all()
    try:
        target = resolve_commitment_target(
            policy=policy,
            anchored_activity=anchored,
            candidates=list(candidates),
            subject="captured booking",
        )
    except CommitmentRemapConflict as exc:
        raise CommerceConflict(str(exc)) from exc
    commitment.activity_id = target.id


def create_booking(
    session: Session,
    *,
    journey_id: str,
    activity_id: str,
    provider: BookingProvider,
    source_strength_codes: Sequence[str],
    booking_source_type_code: str,
    assert_selected_plan_feasible: Callable[[Session, Journey, PlanVersion], Any] | None = None,
    force_failure: bool = False,
) -> Booking:
    _validate_source_codes(source_strength_codes)
    journey, plan, activity, context = selected_activity(session, journey_id, activity_id)
    _assert_feasible(session, journey, plan, assert_selected_plan_feasible)
    service_id = _service_for_activity(session, activity, load_catalog(), order=False)
    active_booking = session.scalar(
        select(Booking.id).where(
            Booking.journey_id == journey.id,
            Booking.activity_id == activity.id,
            Booking.provider_key == provider.provider_key,
            Booking.status.not_in(("FAILED", "CANCELLED")),
        )
    )
    if active_booking is not None:
        raise CommerceConflict("ACTIVE_BOOKING_EXISTS")
    slots = provider.availability(service_id, activity.start_at, activity.end_at)
    slot = next(
        (
            candidate
            for candidate in slots
            if _canonical_iso(candidate.start_at) == _canonical_iso(activity.start_at)
            and _canonical_iso(candidate.end_at) == _canonical_iso(activity.end_at)
        ),
        None,
    )
    if slot is None:
        raise CommerceValidationError("selected activity schedule does not match a provider slot")
    booking_id = str(uuid4())
    request = {
        "merchant_reference": booking_id,
        "activity_id": activity.id,
        "service_id": service_id,
        "provider_slot_ref": slot.provider_slot_ref,
        "amount_minor": activity.estimated_cost_minor,
        "currency": activity.currency.upper(),
    }
    outcome = provider.create_booking(**request, force_failure=force_failure)
    if outcome.status not in {"CONFIRMED", "FAILED", "PENDING", "DRAFT"}:
        raise CommerceConflict("booking provider returned an unsupported status")
    response = {
        "status": outcome.status,
        "external_ref": outcome.external_ref,
        "amount_minor": outcome.amount_minor,
        "currency": outcome.currency,
        "cancellation_policy": outcome.cancellation_policy,
        "provider_code": outcome.provider_code,
    }
    _provider_call(
        session,
        provider_key=outcome.provider_key,
        operation="booking.create",
        request=request,
        response=response,
        ok=outcome.status == "CONFIRMED",
    )
    now = now_iso()
    commitment_id: str | None = None
    if outcome.status == "CONFIRMED":
        policy, rank = _booking_policy(
            context=context,
            activity=activity,
            external_ref=outcome.external_ref,
            cancellation_policy=outcome.cancellation_policy,
            source_type=booking_source_type_code,
        )
        commitment_id = str(uuid4())
        session.add(
            Commitment(
                id=commitment_id,
                journey_id=journey.id,
                activity_id=activity.id,
                kind="BOOKING",
                level=source_strength_code(rank, source_strength_codes),
                status="ACTIVE",
                monetary_value_minor=outcome.amount_minor,
                currency=outcome.currency.upper(),
                external_ref=outcome.external_ref,
                policy_json=dump(policy),
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
    booking = Booking(
        id=booking_id,
        journey_id=journey.id,
        activity_id=activity.id,
        commitment_id=commitment_id,
        provider_key=outcome.provider_key,
        external_ref=outcome.external_ref,
        status=outcome.status,
        amount_minor=outcome.amount_minor,
        currency=outcome.currency.upper(),
        cancellation_policy_json=dump(outcome.cancellation_policy),
        created_at=now,
        updated_at=now,
    )
    session.add(booking)
    record_audit(
        session,
        action="booking.created",
        entity_type="Booking",
        entity_id=booking.id,
        journey_id=journey.id,
        after={"status": booking.status, "external_ref": booking.external_ref},
    )
    session.flush()
    return booking


def _service_for_activity(
    session: Session, activity: Activity, catalog: dict[str, Any], *, order: bool
) -> str:
    semantic = activity.place_id or activity.city_service_id
    if activity.city_service_id:
        service_id = activity.city_service_id
    elif semantic == "place-river-cruise":
        service_id = "service-river-cruise-booking"
    elif semantic == "place-food-hall":
        service_id = "service-food-preorder"
    else:
        raise CommerceValidationError("activity has no supported tradable commerce service")
    section = "order_services" if order else "booking_services"
    entry = catalog.get(section, {}).get(service_id)
    if not entry:
        raise CommerceValidationError("activity service is not available in the frozen catalog")
    if entry.get("place_id") and entry["place_id"] != semantic:
        raise CommerceValidationError("commerce service semantic identity does not match activity")
    service = session.get(CityService, service_id)
    if service is not None and service.city_code != "CNX-DEMO":
        raise CommerceValidationError("commerce service belongs to another city")
    return service_id


def service_id_for_activity(
    session: Session, activity: Activity, catalog: dict[str, Any], *, order: bool
) -> str:
    """Expose the frozen catalog mapping without duplicating it in route code."""

    return _service_for_activity(session, activity, catalog, order=order)


def create_order(
    session: Session,
    *,
    journey_id: str,
    activity_id: str,
    service_id: str,
    items: list[OrderLineRequest],
    catalog: dict[str, Any],
    source_strength_codes: Sequence[str],
    order_source_type_code: str,
    provider_key: str = "mock-booking",
    assert_selected_plan_feasible: Callable[[Session, Journey, PlanVersion], Any] | None = None,
) -> Order:
    _validate_source_codes(source_strength_codes)
    journey, plan, activity, context = selected_activity(session, journey_id, activity_id)
    _assert_feasible(session, journey, plan, assert_selected_plan_feasible)
    expected_service = _service_for_activity(session, activity, catalog, order=True)
    if service_id != expected_service:
        raise CommerceValidationError("order service does not match selected activity")
    priced = price_order(service_id=service_id, requests=items, catalog=catalog)
    now = now_iso()
    order_id = str(uuid4())
    commitment_id = str(uuid4())
    decision = derive_strength_rank(
        CommitmentFacts(
            provider_confirmed=True,
            fixed_schedule=bool(activity.locked),
            user_locked=bool(activity.locked),
            easily_replaceable=True,
        )
    )
    policy = {
        "semantic_key": stable_semantic_key(context),
        "scheduled_start_at": _canonical_iso(activity.start_at),
        "scheduled_end_at": _canonical_iso(activity.end_at),
        "fixed_schedule": bool(activity.locked),
        "user_locked": bool(activity.locked),
        "easily_replaceable": True,
        "strength_rank": decision.rank,
        "strength_reasons": list(decision.reasons),
        "source_type": _validate_source_type(order_source_type_code),
        "source_refs": {"order_id": order_id, "service_id": service_id},
    }
    session.add(
        Commitment(
            id=commitment_id,
            journey_id=journey.id,
            activity_id=activity.id,
            kind="ORDER",
            level=source_strength_code(decision.rank, source_strength_codes),
            status="ACTIVE",
            monetary_value_minor=priced.total_minor,
            currency=priced.currency,
            external_ref=f"mock-order:{order_id}",
            policy_json=dump(policy),
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    order = Order(
        id=order_id,
        journey_id=journey.id,
        activity_id=activity.id,
        commitment_id=commitment_id,
        provider_key=provider_key,
        external_ref=f"mock-order:{order_id}:confirmed",
        status="CONFIRMED",
        amount_minor=priced.total_minor,
        currency=priced.currency,
        items_json=dump(
            [
                {
                    "sku": line.sku,
                    "quantity": line.quantity,
                    "unit_amount_minor": line.unit_amount_minor,
                    "line_total_minor": line.line_total_minor,
                }
                for line in priced.lines
            ]
        ),
        created_at=now,
        updated_at=now,
    )
    session.add(order)
    record_audit(
        session,
        action="order.created",
        entity_type="Order",
        entity_id=order.id,
        journey_id=journey.id,
        after={
            "status": order.status,
            "amount_minor": order.amount_minor,
            "currency": order.currency,
        },
    )
    session.flush()
    return order


def _target_for_payment(
    session: Session, booking_id: str | None, order_id: str | None
) -> tuple[Booking | None, Order | None, Booking | Order]:
    validate_transaction_target_ids(booking_id=booking_id, order_id=order_id)
    booking = session.get(Booking, booking_id) if booking_id else None
    order = session.get(Order, order_id) if order_id else None
    target = booking if booking_id else order
    if target is None:
        raise CommerceValidationError("payment target not found")
    return booking, order, target


def create_transaction(
    session: Session,
    *,
    journey_id: str,
    booking_id: str | None,
    order_id: str | None,
    transaction_type: str,
    requested_amount_minor: int,
    requested_currency: str,
    provider: PaymentProvider,
    idempotency_key: str,
    source_strength_codes: Sequence[str],
    force_failure: bool = False,
) -> PaymentTransaction:
    _validate_source_codes(source_strength_codes)
    if transaction_type not in {"AUTHORIZE", "CAPTURE"}:
        raise CommerceValidationError(
            "WP04 supports AUTHORIZE and CAPTURE only; REFUND/VOID need an original transaction reference"
        )
    header_key = validate_idempotency_key(idempotency_key)
    validate_transaction_target_ids(booking_id=booking_id, order_id=order_id)
    currency = requested_currency.upper()
    semantic_payload = {
        "journey_id": journey_id,
        "provider_key": provider.provider_key,
        "booking_id": booking_id,
        "order_id": order_id,
        "transaction_type": transaction_type,
        "amount_minor": requested_amount_minor,
        "currency": currency,
    }
    assert_no_sensitive_payment_fields(semantic_payload)
    signature = canonical_request_signature(semantic_payload)
    scoped_key = f"transaction.create:{provider.provider_key}:{header_key}"
    existing = session.get(IdempotencyKey, scoped_key)
    if existing is not None:
        if existing.request_hash != signature:
            raise IdempotencyConflict("same idempotency key used for a different request")
        stored = json.loads(existing.response_json or "{}")
        transaction_id = stored.get("transaction_id")
        transaction = session.get(PaymentTransaction, transaction_id) if transaction_id else None
        if transaction is None:
            raise CommerceConflict("idempotency record points to a missing transaction")
        return transaction

    booking, order, target = _target_for_payment(session, booking_id, order_id)
    del booking, order
    if transaction_type == "CAPTURE":
        target_filter = (
            PaymentTransaction.booking_id == booking_id
            if booking_id
            else PaymentTransaction.order_id == order_id
        )
        prior_capture = session.scalar(
            select(PaymentTransaction.id).where(
                target_filter,
                PaymentTransaction.transaction_type == "CAPTURE",
                PaymentTransaction.status == "SUCCEEDED",
            )
        )
        if prior_capture is not None:
            raise CommerceConflict("TARGET_ALREADY_CAPTURED")
    validate_payment_target(
        PaymentTargetContext(
            journey_id=journey_id,
            target_journey_id=target.journey_id,
            authoritative_amount_minor=target.amount_minor,
            authoritative_currency=target.currency,
            target_status=target.status,
        ),
        requested_amount_minor=requested_amount_minor,
        requested_currency=currency,
    )
    transaction_id = str(uuid4())
    request = {
        "merchant_reference": transaction_id,
        "amount_minor": requested_amount_minor,
        "currency": currency,
    }
    if transaction_type == "AUTHORIZE":
        outcome = provider.authorize(**request, force_failure=force_failure)
        operation = "payment.authorize"
    else:
        outcome = provider.capture(**request, force_failure=force_failure)
        operation = "payment.capture"
    if outcome.status not in {"AUTHORIZED", "SUCCEEDED", "FAILED"}:
        raise CommerceConflict("payment provider returned an unsupported status")
    _provider_call(
        session,
        provider_key=provider.provider_key,
        operation=operation,
        request=request,
        response={
            "status": outcome.status,
            "external_ref": outcome.external_ref,
            "provider_code": outcome.provider_code,
        },
        ok=outcome.status in {"AUTHORIZED", "SUCCEEDED"},
    )
    now = now_iso()
    tx = PaymentTransaction(
        id=transaction_id,
        journey_id=journey_id,
        booking_id=booking_id,
        order_id=order_id,
        provider_key=provider.provider_key,
        idempotency_key=header_key,
        transaction_type=transaction_type,
        status=outcome.status,
        amount_minor=requested_amount_minor,
        currency=currency,
        external_ref=outcome.external_ref,
        provider_payload_json=dump({"provider_code": outcome.provider_code}),
        created_at=now,
        updated_at=now,
    )
    session.add(tx)
    commitment_id = target.commitment_id
    if transaction_type == "CAPTURE" and outcome.status == "SUCCEEDED" and commitment_id:
        commitment = session.get(Commitment, commitment_id)
        if commitment is None:
            raise CommerceConflict("target commitment is missing")
        # Resolve the pointer first: everything below promotes what this commitment
        # is attached to, so an attachment into a superseded plan must never reach
        # the strength update.
        _remap_commitment_to_active_plan(session, commitment)
        policy = json.loads(commitment.policy_json or "{}")
        decision = derive_strength_rank(
            CommitmentFacts(
                provider_confirmed=True,
                payment_succeeded=True,
                fixed_schedule=bool(policy.get("fixed_schedule")),
                user_locked=bool(policy.get("user_locked")),
                cancellation_loss_minor=max(int(commitment.monetary_value_minor), 0),
                easily_replaceable=bool(policy.get("easily_replaceable", True)),
            )
        )
        old_rank = int(policy.get("strength_rank", 0))
        new_rank = max(old_rank, decision.rank)
        source_refs = dict(policy.get("source_refs") or {})
        source_refs.update(
            {"payment_transaction_id": tx.id, "payment_external_ref": outcome.external_ref}
        )
        policy.update(
            {
                "strength_rank": new_rank,
                "strength_reasons": list(decision.reasons),
                "source_refs": source_refs,
                "payment_succeeded": True,
            }
        )
        commitment.level = source_strength_code(new_rank, source_strength_codes)
        commitment.external_ref = outcome.external_ref or commitment.external_ref
        commitment.policy_json = dump(policy)
        commitment.updated_at = now
    session.add(
        IdempotencyKey(
            key=scoped_key,
            operation="transaction.create",
            request_hash=signature,
            response_status=201,
            response_json=dump({"transaction_id": tx.id, "status": tx.status}),
            created_at=now,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
    )
    record_audit(
        session,
        action=(
            f"payment.{transaction_type.lower()}.succeeded"
            if tx.status in {"AUTHORIZED", "SUCCEEDED"}
            else f"payment.{transaction_type.lower()}.failed"
        ),
        entity_type="PaymentTransaction",
        entity_id=tx.id,
        journey_id=journey_id,
        after={"status": tx.status, "external_ref": tx.external_ref},
    )
    session.flush()
    return tx


def create_capture_transaction(
    session: Session,
    *,
    journey_id: str,
    booking_id: str | None,
    order_id: str | None,
    requested_amount_minor: int,
    requested_currency: str,
    provider: PaymentProvider,
    idempotency_key: str,
    source_strength_codes: Sequence[str],
    force_failure: bool = False,
) -> PaymentTransaction:
    return create_transaction(
        session,
        journey_id=journey_id,
        booking_id=booking_id,
        order_id=order_id,
        transaction_type="CAPTURE",
        requested_amount_minor=requested_amount_minor,
        requested_currency=requested_currency,
        provider=provider,
        idempotency_key=idempotency_key,
        source_strength_codes=source_strength_codes,
        force_failure=force_failure,
    )


def get_booking_slots(
    session: Session,
    *,
    service_id: str,
    start_at: str,
    end_at: str,
    provider: BookingProvider,
) -> list[Any]:
    if not load_catalog().get("booking_services", {}).get(service_id):
        raise CommerceValidationError("bookable service was not found")
    service = session.get(CityService, service_id)
    if service is not None and service.provider_key != provider.provider_key:
        raise CommerceValidationError("service provider is not available")
    slots = provider.availability(service_id, start_at, end_at)
    _provider_call(
        session,
        provider_key=provider.provider_key,
        operation="booking.availability",
        request={"service_id": service_id, "start_at": start_at, "end_at": end_at},
        response={
            "slots": [
                {
                    "start_at": slot.start_at,
                    "end_at": slot.end_at,
                    "provider_key": slot.provider_key,
                    "provider_slot_ref": slot.provider_slot_ref,
                }
                for slot in slots
            ]
        },
        ok=True,
    )
    session.flush()
    return slots


def cancel_booking(
    session: Session,
    *,
    booking_id: str,
    provider: BookingProvider,
    force_failure: bool = False,
) -> Booking:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise CommerceValidationError("Booking not found")
    if booking.status != "CONFIRMED":
        raise CommerceConflict("only a CONFIRMED booking can be cancelled")
    paid = session.scalar(
        select(PaymentTransaction.id).where(
            PaymentTransaction.booking_id == booking.id,
            PaymentTransaction.status == "SUCCEEDED",
        )
    )
    if paid:
        raise CommerceConflict("paid/anchored booking cannot be cancelled in WP04")
    policy = json.loads(booking.cancellation_policy_json or "{}")
    if policy.get("cancellable_before_payment") is False:
        raise CommerceConflict("booking cancellation is not permitted before payment")
    if not booking.external_ref:
        raise CommerceConflict("booking has no provider reference")
    outcome = provider.cancel_booking(booking.external_ref, force_failure=force_failure)
    _provider_call(
        session,
        provider_key=provider.provider_key,
        operation="booking.cancel",
        request={"external_ref": booking.external_ref},
        response={
            "status": outcome.status,
            "external_ref": outcome.external_ref,
            "provider_code": outcome.provider_code,
        },
        ok=outcome.status == "CANCELLED",
    )
    if outcome.status == "CANCELLED":
        booking.status = "CANCELLED"
        booking.external_ref = outcome.external_ref
        commitment = (
            session.get(Commitment, booking.commitment_id) if booking.commitment_id else None
        )
        if commitment is not None:
            commitment.status = "CANCELLED"
            commitment.updated_at = now_iso()
        record_audit(
            session,
            action="booking.cancelled",
            entity_type="Booking",
            entity_id=booking.id,
            journey_id=booking.journey_id,
            after={"status": booking.status, "external_ref": booking.external_ref},
        )
    session.flush()
    return booking
