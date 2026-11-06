from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS
from app.db.models import Activity, AuditLog, Commitment, Journey, PaymentTransaction, ProviderCall
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def selected_fixture() -> tuple[str, dict, dict, dict, list[dict]]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    )
    assert response.status_code == 201
    plans = response.json()
    plan = next(
        item
        for item in plans
        if any(a["place_id"] == "place-river-cruise" for a in item["activities"])
    )
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    food = next(a for a in plan["activities"] if a["place_id"] == "place-food-hall")
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    return journey_id, plan, cruise, food, plans


def count_rows(model: type) -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_wp04_golden_booking_capture_replay_and_canonical_policy():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    slots = client.get(
        "/api/v1/city-services/service-river-cruise-booking/booking-slots",
        params={
            "start_at": "2026-10-10T15:00:00+08:00",
            "end_at": "2026-10-10T17:00:00+08:00",
        },
    )
    assert slots.status_code == 200
    assert slots.json()[0]["provider_slot_ref"] == "slot-river-cruise-1530"
    assert slots.json()[0]["provider_key"] == "mock-booking"

    booking_response = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert booking_response.status_code == 201
    booking = booking_response.json()
    assert booking["status"] == "CONFIRMED" and booking["commitment_id"]
    commitment_before = client.get(f"/api/v1/commitments/{booking['commitment_id']}").json()
    assert commitment_before["level"] == SOURCE_COMMITMENT_STRENGTHS[3]
    assert commitment_before["policy"]["semantic_key"] == "place-river-cruise"
    assert commitment_before["policy"]["scheduled_start_at"] == "2026-10-10T15:30:00+08:00"

    payload = {
        "booking_id": booking["id"],
        "transaction_type": "CAPTURE",
        "amount": booking["amount"],
        "provider_key": "mock-payment",
    }
    key = "wp04-golden-payment-0001"
    paid = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert paid.status_code == 201 and paid.json()["status"] == "SUCCEEDED"
    tx_id = paid.json()["id"]
    provider_calls_after_payment = count_rows(ProviderCall)
    replay = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code in {200, 201}
    assert replay.json()["id"] == tx_id
    assert count_rows(ProviderCall) == provider_calls_after_payment
    assert count_rows(PaymentTransaction) == 1

    commitment_after = client.get(f"/api/v1/commitments/{booking['commitment_id']}").json()
    assert commitment_after["level"] == SOURCE_COMMITMENT_STRENGTHS[4]
    assert commitment_after["policy"]["strength_rank"] == 4
    assert commitment_after["policy"]["semantic_key"] == "place-river-cruise"
    assert commitment_after["policy"]["scheduled_start_at"] == "2026-10-10T15:30:00+08:00"
    assert commitment_after["policy"]["source_refs"]["payment_transaction_id"] == tx_id


def test_wp04_booking_failure_and_unselected_activity_fail_before_provider_call():
    journey_id, _plan, cruise, _food, plans = selected_fixture()
    unselected_cruise = next(
        a for a in plans[1]["activities"] if a["place_id"] == "place-river-cruise"
    )
    before = count_rows(ProviderCall)
    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/bookings",
        json={"activity_id": unselected_cruise["id"]},
    )
    assert rejected.status_code == 422
    assert count_rows(ProviderCall) == before

    failed = client.post(
        f"/api/v1/journeys/{journey_id}/bookings",
        json={"activity_id": cruise["id"], "failure_token": "FORCE_BOOKING_FAIL"},
    )
    assert failed.status_code == 201
    body = failed.json()
    assert body["status"] == "FAILED" and body["commitment_id"] is None
    assert count_rows(Commitment) == 0


def test_wp04_payment_failure_replay_conflict_and_no_strengthening():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    commitment_id = booking["commitment_id"]
    before = client.get(f"/api/v1/commitments/{commitment_id}").json()
    payload = {
        "booking_id": booking["id"],
        "transaction_type": "CAPTURE",
        "amount": booking["amount"],
    }
    key = "wp04-failure-payment-0001"
    failed = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json=payload,
        headers={"Idempotency-Key": key, "X-Mock-Failure": "FORCE_PAYMENT_FAIL"},
    )
    assert failed.status_code == 201 and failed.json()["status"] == "FAILED"
    calls = count_rows(ProviderCall)
    replay = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code in {200, 201} and replay.json()["status"] == "FAILED"
    assert count_rows(ProviderCall) == calls
    after = client.get(f"/api/v1/commitments/{commitment_id}").json()
    assert after["level"] == before["level"]
    assert after["policy"]["strength_rank"] == before["policy"]["strength_rank"]

    conflict = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={**payload, "amount": {"amount_minor": 1, "currency": "CNY"}},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert count_rows(ProviderCall) == calls


def test_wp04_order_server_pricing_and_order_payment_target():
    journey_id, _plan, _cruise, food, _plans = selected_fixture()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/orders",
        json={
            "activity_id": food["id"],
            "items": [
                {"sku": "local-lunch-box", "quantity": 1, "unit_amount_minor": 1},
                {"sku": "jasmine-tea", "quantity": 2},
            ],
        },
    )
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "CONFIRMED"
    assert order["amount"] == {"amount_minor": 8200, "currency": "CNY"}
    assert order["items"][0]["unit_amount_minor"] == 5800
    paid = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "order_id": order["id"],
            "transaction_type": "CAPTURE",
            "amount": order["amount"],
        },
        headers={"Idempotency-Key": "wp04-order-payment-0001"},
    )
    assert paid.status_code == 201 and paid.json()["status"] == "SUCCEEDED"


def test_wp04_payment_on_flexible_order_does_not_derive_c4_or_duplicate_commitment():
    journey_id, _plan, _cruise, food, _plans = selected_fixture()
    order = client.post(
        f"/api/v1/journeys/{journey_id}/orders",
        json={"activity_id": food["id"], "items": [{"sku": "jasmine-tea", "quantity": 1}]},
    ).json()
    commitment_id = order["commitment_id"]
    before = client.get(f"/api/v1/commitments/{commitment_id}").json()
    assert before["level"] == SOURCE_COMMITMENT_STRENGTHS[2]
    paid = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "order_id": order["id"],
            "transaction_type": "CAPTURE",
            "amount": order["amount"],
        },
        headers={"Idempotency-Key": "wp04-flexible-order-0001"},
    )
    assert paid.status_code == 201 and paid.json()["status"] == "SUCCEEDED"
    after = client.get(f"/api/v1/commitments/{commitment_id}").json()
    assert after["level"] == SOURCE_COMMITMENT_STRENGTHS[3]
    assert after["level"] != SOURCE_COMMITMENT_STRENGTHS[4]
    assert after["policy"]["fixed_schedule"] is False
    assert "anchored multi-factor commitment" not in after["policy"]["strength_reasons"]
    assert count_rows(Commitment) == 1


def _create_second_selected_journey(journey_id: str) -> tuple[str, dict]:
    source = client.get(f"/api/v1/journeys/{journey_id}").json()
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": source["traveler_id"],
            "title": "Second WP04 journey",
            "city_code": source["city_code"],
            "timezone": source["timezone"],
            "start_at": source["start_at"],
            "end_at": source["end_at"],
            "budget": source["budget"],
        },
    )
    assert created.status_code == 201
    other_id = created.json()["id"]
    plans = client.post(f"/api/v1/journeys/{other_id}/plans/generate", json={"candidate_count": 1})
    assert plans.status_code == 201
    plan = plans.json()[0]
    assert client.post(f"/api/v1/journeys/{other_id}/plans/{plan['id']}/select").status_code == 200
    activity = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    return other_id, activity


def test_wp04_cross_journey_targets_and_draft_precondition_fail_before_provider():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    other_id, other_cruise = _create_second_selected_journey(journey_id)
    before_calls = count_rows(ProviderCall)
    cross_booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": other_cruise["id"]}
    )
    assert cross_booking.status_code == 422
    assert count_rows(ProviderCall) == before_calls

    other_booking = client.post(
        f"/api/v1/journeys/{other_id}/bookings", json={"activity_id": other_cruise["id"]}
    )
    assert other_booking.status_code == 201
    before_payment_calls = count_rows(ProviderCall)
    cross_payment = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": other_booking.json()["id"],
            "transaction_type": "CAPTURE",
            "amount": other_booking.json()["amount"],
        },
        headers={"Idempotency-Key": "wp04-cross-journey-0001"},
    )
    assert cross_payment.status_code == 422
    assert count_rows(ProviderCall) == before_payment_calls

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.status = "DRAFT"
        session.commit()
    before_draft_calls = count_rows(ProviderCall)
    draft_booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert draft_booking.status_code == 422
    assert count_rows(ProviderCall) == before_draft_calls


def test_wp04_commerce_write_rechecks_selected_plan_feasibility_before_provider():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    with SessionLocal() as session:
        activity = session.get(Activity, cruise["id"])
        assert activity is not None
        activity.currency = "USD"
        session.commit()
    before_calls = count_rows(ProviderCall)
    response = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert response.status_code == 422
    assert "ACTIVITY_CURRENCY_MISMATCH" in response.json()["error"]["message"]
    assert count_rows(ProviderCall) == before_calls


def test_wp04_sensitive_input_and_short_idempotency_leave_no_provider_or_audit_secret():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    before_calls = count_rows(ProviderCall)
    sensitive = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
            "card_number": "4111111111111111",
            "cvv": "123",
        },
        headers={"Idempotency-Key": "wp04-secret-input-0001"},
    )
    assert sensitive.status_code == 422
    short_key = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "short"},
    )
    assert short_key.status_code == 422
    assert count_rows(ProviderCall) == before_calls
    with SessionLocal() as session:
        rows = session.scalars(select(AuditLog)).all()
        serialized = " ".join(
            f"{row.before_json or ''} {row.after_json or ''}" for row in rows
        ).lower()
        assert "4111111111111111" not in serialized
        assert '"cvv"' not in serialized


def test_wp04_unpaid_cancel_and_sensitive_payment_input_are_safe():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    cancelled = client.post(f"/api/v1/bookings/{booking['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    commitment = client.get(f"/api/v1/commitments/{booking['commitment_id']}").json()
    assert commitment["status"] == "CANCELLED"

    sensitive = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
            "card_number": "4111111111111111",
        },
        headers={"Idempotency-Key": "wp04-sensitive-0001"},
    )
    assert sensitive.status_code == 422


def test_wp04_transaction_xor_ownership_and_amount_checks_are_before_provider():
    journey_id, _plan, cruise, _food, _plans = selected_fixture()
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    before = count_rows(ProviderCall)
    neither = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={"transaction_type": "CAPTURE", "amount": booking["amount"]},
        headers={"Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
    )
    assert neither.status_code == 422
    both = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "order_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "wp04-xor-both"},
    )
    assert both.status_code == 422
    mismatch = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": {"amount_minor": 1, "currency": "USD"},
        },
        headers={"Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
    )
    assert mismatch.status_code == 422
    assert count_rows(ProviderCall) == before
