from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db.models import Activity, Commitment
from app.db.session import SessionLocal
from app.main import app
from test_v12_authoritative_trip_execution import _journey_with_inputs


def _import_confirm(
    client: TestClient,
    journey_id: str,
    *,
    role: str,
    ref: str,
    semantic_node_key: str | None = None,
):
    reservation = {
        "reservation_type": "RAIL" if role != "MID_TRIP" else "FERRY",
        "provider_key": "v1312-audit",
        "confirmation_ref": ref,
        "origin": {
            "name": "Tianjin West Station",
            "latitude": 39.1366,
            "longitude": 117.1696,
        },
        "destination": {
            "name": "Tianjin Binhai Airport",
            "latitude": 39.1244,
            "longitude": 117.3462,
        },
        "scheduled_departure_at": (
            "2026-10-03T20:00:00+00:00"
            if role == "DEPARTURE"
            else "2026-10-01T07:00:00+00:00"
        ),
        "scheduled_arrival_at": (
            "2026-10-03T20:35:00+00:00"
            if role == "DEPARTURE"
            else "2026-10-01T08:00:00+00:00"
        ),
        "price_amount_minor": 5500,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": role,
    }
    if semantic_node_key is not None:
        reservation["semantic_node_key"] = semantic_node_key
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": reservation}},
    )
    assert imported.status_code == 201, imported.text
    return client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )


@pytest.mark.parametrize(
    ("role", "expected_suffix"),
    [("ARRIVAL", "arrival-anchor"), ("DEPARTURE", "departure-anchor")],
)
def test_structural_reservation_survives_v9_identity_then_v12_compose_and_select(
    role: str, expected_suffix: str
) -> None:
    client = TestClient(app)
    journey_id, place_ids, _legacy_commitment_id = _journey_with_inputs()

    confirmed = _import_confirm(
        client,
        journey_id,
        role=role,
        ref=f"V1312-{role}",
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    commitment = payload["commitment"]
    assert commitment["policy"]["anchor_role"] == role
    assert commitment["policy"]["semantic_node_key"] == expected_suffix
    assert commitment["policy"]["semantic_key"]

    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={
            "place_ids": place_ids,
            "candidate_count": 1,
            "reservation_id": payload["reservation"]["id"],
        },
    )
    assert composed.status_code == 201, composed.text
    plan = composed.json()["top_k"][0]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text

    with SessionLocal() as session:
        row = session.get(Commitment, commitment["id"])
        assert row is not None and row.activity_id is not None
        activity = session.get(Activity, row.activity_id)
        assert activity is not None
        assert activity.plan_version_id == plan["id"]
        assert activity.place_id == f"v12:{journey_id}:{expected_suffix}"
        policy = json.loads(row.policy_json)
        assert policy["semantic_key"] == activity.place_id
        assert policy["semantic_node_key"] == expected_suffix


def test_structural_role_owns_its_semantic_node_even_if_input_supplies_another_one() -> None:
    client = TestClient(app)
    journey_id, place_ids, _legacy_commitment_id = _journey_with_inputs()
    confirmed = _import_confirm(
        client,
        journey_id,
        role="DEPARTURE",
        ref="V1312-DEPARTURE-CONFLICTING-KEY",
        semantic_node_key=place_ids[0],
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["commitment"]["policy"]["semantic_node_key"] == "departure-anchor"

    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={
            "place_ids": place_ids,
            "candidate_count": 1,
            "reservation_id": payload["reservation"]["id"],
        },
    )
    assert composed.status_code == 201, composed.text


def test_mid_trip_reservation_without_semantic_target_cannot_be_confirmed() -> None:
    client = TestClient(app)
    journey_id, _place_ids, _legacy_commitment_id = _journey_with_inputs()
    confirmed = _import_confirm(
        client,
        journey_id,
        role="MID_TRIP",
        ref="V1312-MID-NO-TARGET",
    )
    assert confirmed.status_code == 409, confirmed.text
    # The rule under test is that a MID_TRIP booking without a resolvable stop
    # cannot be confirmed.  The message is traveller-facing wording now, so
    # assert the blocking fact instead of one internal sentence.
    assert "still needs a few details" in confirmed.text
    assert "which stop in your trip this booking belongs to" in confirmed.text


def test_unknown_anchor_role_cannot_be_confirmed() -> None:
    client = TestClient(app)
    journey_id, _place_ids, _legacy_commitment_id = _journey_with_inputs()
    confirmed = _import_confirm(
        client,
        journey_id,
        role="BANANA",
        ref="V1312-BAD-ROLE",
    )
    assert confirmed.status_code == 409, confirmed.text
    assert "still needs a few details" in confirmed.text
    assert "which part of the trip this booking belongs to" in confirmed.text
