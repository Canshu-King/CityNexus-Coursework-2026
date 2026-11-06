"""V13 authoritative integrity regressions.

These tests exercise the live API contracts rather than reaching around the
server to manufacture a green result.  They intentionally keep the V12
fixture small while checking the invariants that must survive every later
surface: execution, Trip Studio, commitments, and frozen provider evidence.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.agent.autorun import ensure_enrollment, run_once, stop_autorun
from app.agent.signal_pipeline import refresh_demo_signals
from app.agent.guardian import scan_guardian
from app.db.models import (
    Activity,
    Commitment,
    Event,
    GuardianRun,
    Journey,
    JourneyLeg,
    JourneyTripContext,
    MobilityFact,
    PlanVersion,
    ProviderObservation,
    AccommodationOfferSnapshot,
)
from app.db.session import SessionLocal
from app.main import app
from test_v12_authoritative_trip_execution import _journey_with_inputs
from _qoder_explicit_demo_mobility import seed_authoritative_recovery_mobility


def _started_journey() -> tuple[TestClient, str, dict, list[str]]:
    client = TestClient(app)
    journey_id, place_ids, commitment_id = _journey_with_inputs()
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1, "reservation_id": commitment_id},
    )
    assert composed.status_code == 201, composed.text
    plan = composed.json()["top_k"][0]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200, started.text
    return client, journey_id, plan, place_ids


def _complete_current(client: TestClient, journey_id: str) -> dict:
    actions = {"ACTIVE": "depart", "EN_ROUTE": "arrive", "ARRIVED": "begin", "IN_PROGRESS": "complete"}
    completed = None
    for _ in range(4):
        twin = client.get(f"/api/v1/journeys/{journey_id}/twin").json()
        execution = twin["context"]["execution"]
        activity_id = execution["now_activity_id"]
        action = actions[execution["now_state"]]
        completed = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{activity_id}/{action}"
        )
        assert completed.status_code == 200, completed.text
        if action == "complete":
            return completed.json()
    raise AssertionError("current activity did not complete")


def _feasible_late_reorder(plan: dict) -> list[str]:
    order = [item["id"] for item in plan["activities"]]
    tea_index = next(index for index, item in enumerate(plan["activities"]) if item["title"] == "Tea House")
    riverside_index = next(index for index, item in enumerate(plan["activities"]) if item["title"] == "Riverside Eye")
    order[tea_index], order[riverside_index] = order[riverside_index], order[tea_index]
    return order


def _set_anchor_window(journey_id: str, start_at: str, end_at: str) -> None:
    session = SessionLocal()
    journey = session.get(Journey, journey_id)
    context = session.get(JourneyTripContext, journey_id)
    assert journey is not None and context is not None
    journey.start_at = start_at
    journey.end_at = end_at
    arrival = json.loads(context.arrival_anchor_json)
    departure = json.loads(context.departure_anchor_json)
    arrival["at"] = start_at
    departure["at"] = end_at
    context.arrival_anchor_json = json.dumps(arrival)
    context.departure_anchor_json = json.dumps(departure)
    session.commit()
    session.close()


@pytest.mark.parametrize(
    ("start_at", "end_at", "expected_days", "expected_nights"),
    [
        ("2026-10-01T08:00:00+00:00", "2026-10-01T22:00:00+00:00", 1, 0),
        ("2026-10-01T08:00:00+00:00", "2026-10-03T22:00:00+00:00", 3, 2),
        ("2026-10-01T08:00:00+00:00", "2026-10-04T22:00:00+00:00", 4, 3),
        ("2026-10-01T08:00:00+00:00", "2026-10-05T22:00:00+00:00", 5, 4),
    ],
)
def test_v13_authoritative_compose_derives_day_and_night_shape(
    start_at: str,
    end_at: str,
    expected_days: int,
    expected_nights: int,
) -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    _set_anchor_window(journey_id, start_at, end_at)
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert response.status_code == 201, response.text
    if expected_nights == 0:
        assert response.json()["stay_offer_id"] is None
    else:
        assert response.json()["stay_offer_id"] is not None
    session = SessionLocal()
    plan = session.get(PlanVersion, response.json()["top_k"][0]["id"])
    assert plan is not None
    explanation = json.loads(plan.explanation_json)
    assert explanation["days"] == expected_days
    assert explanation["nights"] == expected_nights
    offers = session.scalars(
        select(AccommodationOfferSnapshot).where(
            AccommodationOfferSnapshot.journey_id == journey_id
        )
    ).all()
    if expected_nights:
        assert offers
        assert all(
            offer.total_amount_minor == offer.nightly_amount_minor * expected_nights
            for offer in offers
        )
        assert session.scalar(
            select(Activity).where(
                Activity.plan_version_id == plan.id,
                Activity.activity_type == "stay",
            ).order_by(Activity.sequence_no)
        ) is not None
        assert sum(
            1
            for item in session.scalars(
                select(Activity).where(Activity.plan_version_id == plan.id)
            ).all()
            if item.activity_type == "stay"
        ) == expected_nights
    else:
        assert not offers
    activity_count = session.scalar(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no).limit(1)
    )
    assert activity_count is not None
    session.close()


def test_v13_authoritative_compose_rejects_invalid_anchor_window() -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    session = SessionLocal()
    context = session.get(JourneyTripContext, journey_id)
    assert context is not None
    arrival = json.loads(context.arrival_anchor_json)
    departure = json.loads(context.departure_anchor_json)
    arrival["at"] = "2026-10-02T08:00:00+00:00"
    departure["at"] = "2026-10-01T22:00:00+00:00"
    context.arrival_anchor_json = json.dumps(arrival)
    context.departure_anchor_json = json.dumps(departure)
    session.commit()
    session.close()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert response.status_code == 422
    assert "V13_INVALID_ANCHOR_WINDOW" in response.text


def test_v13_mixed_provider_matrix_is_not_reported_as_full_capture() -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    first = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert first.status_code == 201, first.text

    with SessionLocal() as session:
        fact = session.scalar(select(MobilityFact).where(MobilityFact.journey_id == journey_id))
        assert fact is not None
        fact.provenance = "CAPTURED_REPLAY"
        fact.provider_key = "valhalla-replay"
        session.commit()

    second = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert second.status_code == 201, second.text
    evidence = second.json()["top_k"][0]["mobility_evidence"]
    assert evidence["provider_evidence"]["selected_path"] == "MIXED_PROVIDER_FACTS"
    assert evidence["provider_evidence"]["provenance_components"] == [
        "CAPTURED_REPLAY",
        "SYNTHETIC_FIXTURE",
    ]


def test_v13_mid_trip_c4_is_fixed_and_bound_to_selected_activity_via_inbox_api() -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={
            "artifact_type": "STRUCTURED_JSON",
            "payload": {
                "reservation": {
                    "reservation_type": "FERRY",
                    "provider_key": "user-confirmed-cruise",
                    "confirmation_ref": "CRUISE-V13-MIDTRIP",
                    "origin": {"name": "Haihe Wharf"},
                    "destination": {
                        "name": "Haihe Cruise",
                        "latitude": 39.1530,
                        "longitude": 117.1908,
                    },
                    "scheduled_departure_at": "2026-10-03T17:00:00+00:00",
                    "scheduled_arrival_at": "2026-10-03T17:30:00+00:00",
                    "price_amount_minor": 2500,
                    "currency": "CNY",
                    "price_status": "KNOWN",
                    "semantic_node_key": "v12-test-place-2",
                    "anchor_role": "MID_TRIP",
                }
            },
        },
    )
    assert imported.status_code == 201, imported.text
    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    commitment_id = confirmed.json()["commitment"]["id"]
    assert confirmed.json()["commitment"]["level"] == "C4_Anchored"
    assert confirmed.json()["commitment"]["policy"]["anchor_role"] == "MID_TRIP"
    assert confirmed.json()["commitment"]["policy"]["semantic_key"] == "v12-test-place-2"

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={
            "place_ids": place_ids,
            "candidate_count": 1,
            "reservation_id": confirmed.json()["reservation"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()["top_k"][0]
    tea = next(item for item in plan["activities"] if item["place_id"] == "v12-test-place-2")
    assert tea["locked"] is True
    assert tea["start_at"].startswith("2026-10-03T17:30:00")

    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
    session = SessionLocal()
    commitment = session.get(Commitment, commitment_id)
    assert commitment is not None and commitment.activity_id == tea["id"]
    session.close()


@pytest.mark.parametrize(
    ("semantic_key", "scheduled_start_at", "expected_code"),
    [
        ("v12-test-place-2", "2026-10-03T06:00:00+00:00", "V13_COMMITMENT_ANCHOR"),
        ("v12-test-place-not-selected", "2026-10-03T17:30:00+00:00", "V13_C4_ANCHOR_NOT_IN_INPUT"),
    ],
)
def test_v13_impossible_mid_trip_c4_is_a_deterministic_compose_conflict(
    semantic_key: str,
    scheduled_start_at: str,
    expected_code: str,
) -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    session = SessionLocal()
    session.add(
        Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=None,
            kind="BOOKING",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=3000,
            monetary_value_status="KNOWN",
            currency="CNY",
            external_ref="V13-CONFLICT",
            policy_json=json.dumps(
                {"semantic_key": semantic_key, "scheduled_start_at": scheduled_start_at}
            ),
            created_at="2026-10-01T08:00:00+00:00",
            updated_at="2026-10-01T08:00:00+00:00",
        )
    )
    session.commit()
    session.close()

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert response.status_code == 422, response.text
    assert expected_code in response.text


def test_v13_execution_rejects_illegal_transitions_without_mutation() -> None:
    client, journey_id, plan, _ = _started_journey()
    first_id = plan["activities"][0]["id"]

    for action in ("arrive", "begin", "complete"):
        rejected = client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/{action}")
        assert rejected.status_code == 409, rejected.text
        assert "INVALID_ACTIVITY_TRANSITION" in rejected.text

    session = SessionLocal()
    first = session.get(Activity, first_id)
    assert first is not None and first.status == "ACTIVE"
    session.close()

    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/depart").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/depart").status_code == 409
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/begin").status_code == 409
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/arrive").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/arrive").status_code == 409
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/begin").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/begin").status_code == 409
    assert client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/complete").status_code == 200


def test_v13_protected_current_activity_cannot_be_skipped() -> None:
    client, journey_id, plan, _ = _started_journey()
    first_id = plan["activities"][0]["id"]
    rejected = client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/skip")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["message"] == "PROTECTED_COMMITMENT_CANNOT_BE_SKIPPED"
    twin = client.get(f"/api/v1/journeys/{journey_id}/twin").json()
    assert twin["context"]["execution"]["now_activity_id"] == first_id


def test_v13_trip_studio_replays_frozen_evidence_and_preserves_progress() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    _complete_current(client, journey_id)
    active_plan = client.get(f"/api/v1/plans/{original_plan['id']}").json()
    draft_order = _feasible_late_reorder(active_plan)

    first_preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert first_preview.status_code == 200, first_preview.text
    first_payload = first_preview.json()
    assert first_payload["can_accept"] is True
    assert first_payload["deterministic_gate"]["authoritative"] is True
    evidence_id = first_payload["evidence_snapshot_id"]

    session = SessionLocal()
    for fact in session.scalars(select(MobilityFact).where(MobilityFact.journey_id == journey_id)):
        fact.duration_min = 9999
        fact.cost_amount_minor = 999999
        fact.provider_key = "mutated-after-capture"
        fact.provenance = "MUTATED_AFTER_CAPTURE"
    session.commit()
    session.close()

    replay = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["can_accept"] is True
    assert replay_payload["preview_id"] == first_payload["preview_id"]
    assert replay_payload["evidence_snapshot_id"] == evidence_id
    assert replay_payload["authoritative_gate"]["frozen_facts"]
    assert all(item["duration_min"] != 9999 for item in replay_payload["authoritative_gate"]["frozen_facts"])

    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": draft_order, "preview_id": replay_payload["preview_id"]},
    )
    assert accepted.status_code == 201, accepted.text
    new_plan_id = accepted.json()["plan"]["id"]
    new_plan = client.get(f"/api/v1/plans/{new_plan_id}").json()
    assert new_plan["status"] == "ACTIVE"

    twin = client.get(f"/api/v1/journeys/{journey_id}/twin").json()
    current_id = twin["context"]["execution"]["now_activity_id"]
    assert current_id is not None

    session = SessionLocal()
    journey = session.get(Journey, journey_id)
    assert journey is not None and journey.current_activity_id == current_id
    rows = session.scalars(select(Activity).where(Activity.plan_version_id == new_plan_id)).all()
    assert any(
        json.loads(row.metadata_json).get("trip_studio", {}).get("execution_status_before") == "COMPLETED"
        and row.status == "COMPLETED"
        for row in rows
    )
    legs = session.scalars(select(JourneyLeg).where(JourneyLeg.plan_version_id == new_plan_id)).all()
    assert legs and all(leg.duration_min != 9999 for leg in legs)
    session.close()


def test_v13_trip_studio_rejects_stale_preview_without_creating_plan() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    draft_order = _feasible_late_reorder(original_plan)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True
    session = SessionLocal()
    before = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())
    session.close()

    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": draft_order, "preview_id": str(uuid4())},
    )
    assert rejected.status_code == 409
    assert "TRIP_STUDIO_PREVIEW_STALE" in rejected.text

    session = SessionLocal()
    after = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())
    session.close()
    assert after == before


def test_v13_trip_studio_repeated_noop_accept_is_idempotent() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    order = [item["id"] for item in original_plan["activities"]]
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True
    assert preview.json()["impact"]["changed"] is False
    with SessionLocal() as session:
        before = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": order, "preview_id": preview.json()["preview_id"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["no_op"] is True
    assert accepted.json()["plan"]["id"] == original_plan["id"]
    with SessionLocal() as session:
        after = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())
    assert after == before


def test_v13_repeated_non_noop_accept_cannot_create_a_second_plan() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    draft_order = _feasible_late_reorder(original_plan)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True
    request = {"activity_ids": draft_order, "preview_id": preview.json()["preview_id"]}
    first = client.post(f"/api/v1/journeys/{journey_id}/trip-studio/accept", json=request)
    assert first.status_code == 201, first.text
    with SessionLocal() as session:
        after_first = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())

    repeated = client.post(f"/api/v1/journeys/{journey_id}/trip-studio/accept", json=request)
    assert repeated.status_code in {409, 422}
    assert "TRIP_STUDIO" in repeated.text
    with SessionLocal() as session:
        after_repeated = len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all())
    assert after_repeated == after_first


def test_v13_concurrent_accepts_have_one_authoritative_winner() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    draft_order = _feasible_late_reorder(original_plan)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True
    request = {"activity_ids": draft_order, "preview_id": preview.json()["preview_id"]}

    def submit_once() -> int:
        with TestClient(app) as local:
            return local.post(
                f"/api/v1/journeys/{journey_id}/trip-studio/accept",
                json=request,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _item: submit_once(), range(2)))
    assert statuses.count(201) == 1
    assert sum(status in {409, 422} for status in statuses) == 1
    with SessionLocal() as session:
        plans = session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all()
    assert len(plans) == 2


def test_v13_trip_studio_rejects_mutated_or_expired_frozen_evidence_on_accept() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    draft_order = _feasible_late_reorder(original_plan)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True

    session = SessionLocal()
    plan = session.get(PlanVersion, original_plan["id"])
    assert plan is not None
    evidence = json.loads(plan.mobility_evidence_json)
    evidence["facts"][0]["duration_min"] = 9999
    evidence["expires_at"] = "2020-01-01T00:00:00+00:00"
    plan.mobility_evidence_json = json.dumps(evidence)
    session.commit()
    session.close()

    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": draft_order, "preview_id": preview.json()["preview_id"]},
    )
    assert rejected.status_code == 422
    assert "TRIP_STUDIO_DETERMINISTIC_GATE_FAILED" in rejected.text
    assert "FROZEN_EVIDENCE_INTEGRITY_MISMATCH" in rejected.text or "STALE_FROZEN_EVIDENCE" in rejected.text


def test_v13_trip_studio_rejects_a_missing_frozen_matrix_pair() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    draft_order = _feasible_late_reorder(original_plan)

    session = SessionLocal()
    plan = session.get(PlanVersion, original_plan["id"])
    assert plan is not None
    evidence = json.loads(plan.mobility_evidence_json)
    place_by_activity_id = {
        item["id"]: item["place_id"] for item in original_plan["activities"]
    }
    draft_place_order = [place_by_activity_id[item] for item in draft_order]
    route_pair = next(
        (from_key, to_key)
        for from_key, to_key in zip(draft_place_order, draft_place_order[1:])
        if any(
            item.get("origin_key") == from_key and item.get("destination_key") == to_key
            for item in evidence["facts"]
        )
    )
    evidence["facts"] = [
        item
        for item in evidence["facts"]
        if (item.get("origin_key"), item.get("destination_key")) != route_pair
    ]
    evidence["selected_facts"] = [
        item
        for item in evidence["selected_facts"]
        if (item.get("origin_key"), item.get("destination_key")) != route_pair
    ]
    integrity_payload = dict(evidence)
    integrity_payload.pop("integrity_hash", None)
    evidence["integrity_hash"] = hashlib.sha256(
        json.dumps(
            integrity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    plan.mobility_evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    session.commit()
    session.close()

    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["can_accept"] is False
    failures = payload["deterministic_gate"]["failures"]
    assert "FROZEN_ROUTE_EVIDENCE_MISSING" in failures
    assert any(str(item).startswith("MOBILITY_DATA_UNAVAILABLE:") for item in failures)

    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": draft_order, "preview_id": payload["preview_id"]},
    )
    assert rejected.status_code == 422
    assert "TRIP_STUDIO_DETERMINISTIC_GATE_FAILED" in rejected.text


def test_v13_authoritative_predictive_recovery_executes_through_gateway_and_offline_truth(monkeypatch) -> None:
    """Provider replay -> Event -> Guardian -> gate -> accepted Plan -> projections."""

    stop_autorun()
    client = TestClient(app)
    journey_id, place_ids, commitment_id = _journey_with_inputs()
    # V5 (T2 same-env): the predictive recovery hard-gates the same v12-style
    # authoritative journey, so this scenario declares its route legs explicitly
    # as deterministic fixture facts (not laundered synthetic default).  A pair
    # outside the table stays synthetic/unavailable, so B1 is not neutralised.
    seed_authoritative_recovery_mobility(monkeypatch, journey_id)
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1, "reservation_id": commitment_id},
    )
    assert composed.status_code == 201, composed.text
    base_plan = composed.json()["top_k"][0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{base_plan['id']}/select"
    ).status_code == 200

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        traveler_id = journey.traveler_id
    policy = client.put(
        f"/api/v1/travelers/{traveler_id}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 5000,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 120,
            "allowed_activity_types": ["visit", "meal", "transport"],
            "protected_commitment_levels": ["C4_Anchored"],
            "allow_auto_commitment_management": False,
            "allow_auto_when_provider_degraded": False,
            "allow_auto_on_critical_event": False,
        },
    )
    assert policy.status_code == 200, policy.text
    mission = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "Protect the confirmed arrival and recover reversible activities autonomously.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission.status_code == 200, mission.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        ensure_enrollment(session, journey, interval_seconds=1)
        session.commit()
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={
            "state": "RISK_FORECAST",
            "payload": {
                "risk_score": 88,
                "forecast_horizon_min": 45,
                "expected_route_delay_min": 18,
            },
        },
    )
    assert fixture.status_code == 200, fixture.text

    results = run_once(enrolled_only=True)
    assert len(results) == 1
    result = results[0]
    guardian = result["guardian"]
    assert result["signals"]["created_event_ids"]
    assert result["signals"]["provider_provenance"]["weather"] == "REPLAY"
    assert guardian["trigger_kind"] == "PREDICTIVE"
    assert guardian["decision"] == "PREEMPTIVE_RECOVERY"
    assert guardian["action_status"] == "EXECUTED"
    assert guardian["active_plan_before_id"] == base_plan["id"]
    assert guardian["active_plan_after_id"] != base_plan["id"]
    assert guardian["evidence"]["policy"]["decision"] == "ALLOW_AUTO"
    assert guardian["evidence"]["policy"]["reason_codes"]
    assert guardian["evidence"]["mutation"]["executed"] is True
    assert guardian["evidence"]["solver"]["kind"] == "DETERMINISTIC_HEURISTIC"

    after_plan = client.get(f"/api/v1/plans/{guardian['active_plan_after_id']}")
    assert after_plan.status_code == 200, after_plan.text
    assert after_plan.json()["status"] == "ACTIVE"
    with SessionLocal() as session:
        event = session.get(Event, result["signals"]["created_event_ids"][0])
        assert event is not None and event.affected_activity_id is not None
        assert event.status == "ACKNOWLEDGED"
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert journey.active_plan_version_id == guardian["active_plan_after_id"]
        protected = session.get(Commitment, commitment_id)
        assert protected is not None and protected.status == "ACTIVE"

    twin = client.get(f"/api/v1/journeys/{journey_id}/twin")
    assert twin.status_code == 200
    assert twin.json()["active_plan_version_id"] == guardian["active_plan_after_id"]
    offline = client.get(f"/api/v1/journeys/{journey_id}/offline-snapshot")
    assert offline.status_code == 200
    assert "execution" in offline.json()["snapshot"]


def test_v13_guardian_reuses_a_signal_only_with_its_matching_mobility_pair() -> None:
    """High-frequency sensing must not mix a signal with another PlanVersion."""

    stop_autorun()
    _, journey_id, _, _ = _started_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        first = refresh_demo_signals(
            session,
            journey,
            captured_at="2026-10-01T08:00:00+00:00",
        )
        session.commit()
        first_ids = first.observation_ids
        first_count = len(
            session.scalars(
                select(ProviderObservation).where(ProviderObservation.journey_id == journey_id)
            ).all()
        )
        session.expire_all()
        journey = session.get(Journey, journey_id)
        assert journey is not None
        second = refresh_demo_signals(
            session,
            journey,
            captured_at="2026-10-01T08:01:00+00:00",
        )
        session.commit()
        second_count = len(
            session.scalars(
                select(ProviderObservation).where(ProviderObservation.journey_id == journey_id)
            ).all()
        )
    assert second.observation_ids == first_ids
    assert second_count == first_count


def test_v13_guardian_model_failure_is_persisted_as_retryable(monkeypatch) -> None:
    """A committed recovery attempt must not remain permanently in PLANNING."""

    stop_autorun()
    client, journey_id, _, _ = _started_journey()
    mission = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "Recover reversible activities while protecting commitments.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission.status_code == 200, mission.text
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={"state": "DISRUPTED", "payload": {"scenario": "transient-model-failure"}},
    )
    assert fixture.status_code == 200, fixture.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        refresh_demo_signals(
            session,
            journey,
            captured_at="2026-10-01T08:00:00+00:00",
        )
        session.commit()

        def transient_failure(*args, **kwargs):
            raise RuntimeError("transient model boundary failure")

        monkeypatch.setattr("app.agent.guardian._model_recovery", transient_failure)
        with pytest.raises(RuntimeError, match="transient model boundary failure"):
            scan_guardian(session, journey)

        rows = session.scalars(
            select(GuardianRun).where(GuardianRun.journey_id == journey_id)
        ).all()
        retryable = [row for row in rows if row.status == "FAILED_RETRYABLE"]
        assert len(retryable) == 1
        assert retryable[0].action_status == "RETRYABLE_FAILURE"
        assert ":failed:" in retryable[0].evidence_fingerprint


def test_v13_execution_rejects_terminal_wrong_activity_and_old_plan_mutation() -> None:
    client, journey_id, original_plan, _ = _started_journey()
    first_id = original_plan["activities"][0]["id"]
    assert _complete_current(client, journey_id)

    terminal_complete = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{first_id}/complete"
    )
    terminal_skip = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{first_id}/skip"
    )
    assert terminal_complete.status_code == 409
    assert terminal_skip.status_code == 409
    assert "not the current activity" in terminal_complete.text

    third_id = original_plan["activities"][2]["id"]
    wrong_activity = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{third_id}/depart"
    )
    assert wrong_activity.status_code == 409
    session = SessionLocal()
    third = session.get(Activity, third_id)
    first = session.get(Activity, first_id)
    assert third is not None and third.status == "NOT_STARTED"
    assert first is not None and first.status == "COMPLETED"
    session.close()

    current_plan = client.get(f"/api/v1/plans/{original_plan['id']}").json()
    draft_order = _feasible_late_reorder(current_plan)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": draft_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"] is True
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": draft_order, "preview_id": preview.json()["preview_id"]},
    )
    assert accepted.status_code == 201, accepted.text
    stale_plan_mutation = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{first_id}/depart"
    )
    assert stale_plan_mutation.status_code == 409
    assert "active plan" in stale_plan_mutation.text
