"""Focused V12 product tests written against the live CityNexus contracts."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Commitment, Event, Journey, JourneyTripContext, MobilityFact, Place, Traveler
from app.db.session import SessionLocal
from app.main import app


def _journey_with_inputs() -> tuple[str, list[str], str]:
    client = TestClient(app)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    session = SessionLocal()
    traveler = session.scalar(select(Traveler))
    journey_id = str(uuid4())
    start = "2026-10-01T08:00:00+00:00"
    end = "2026-10-03T22:00:00+00:00"
    journey = Journey(
        id=journey_id,
        traveler_id=traveler.id,
        title="V12 authoritative journey",
        city_code="CN-TIANJIN",
        timezone="Asia/Shanghai",
        start_at=start,
        end_at=end,
        budget_currency="CNY",
        budget_amount_minor=100000,
        planning_mode="balanced",
        status="PLANNED",
        created_at=start,
        updated_at=start,
    )
    session.add(journey)
    session.flush()
    session.add(
        JourneyTripContext(
            journey_id=journey_id,
            arrival_anchor_json=json.dumps(
                {"at": start, "location": "Tianjin West Station", "latitude": 39.14, "longitude": 117.18}
            ),
            departure_anchor_json=json.dumps(
                {"at": end, "location": "Tianjin Binhai Airport", "latitude": 39.12, "longitude": 117.35}
            ),
            party_size=1,
            occupancy_json="{}",
            stay_budget_amount_minor=6000,
            stay_budget_currency="CNY",
            created_at=start,
            updated_at=start,
        )
    )
    commitment_id = str(uuid4())
    session.add(
        Commitment(
            id=commitment_id,
            journey_id=journey_id,
            activity_id=None,
            kind="BOOKING",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=12000,
            monetary_value_status="KNOWN",
            currency="CNY",
            external_ref="TRAIN-V12-C4",
            policy_json=json.dumps(
                {
                    "semantic_node_key": "arrival-anchor",
                    "scheduled_start_at": start,
                    "source": "USER_CONFIRMED_EXTERNAL_RESERVATION",
                }
            ),
            created_at=start,
            updated_at=start,
        )
    )
    place_ids: list[str] = []
    for index, (name, latitude, longitude, category, cost) in enumerate(
        [
            ("Italian Gallery", 39.1315, 117.2050, "arts_centre", 800),
            ("Design Library", 39.1226, 117.1964, "library", 0),
            ("Tea House", 39.1530, 117.1908, "cafe", 300),
            ("Five Avenues", 39.1160, 117.1910, "museum", 500),
            ("Water Park", 39.0840, 117.1710, "park", 200),
            ("Riverside Eye", 39.1570, 117.1880, "viewpoint", 1200),
        ]
    ):
        place_id = f"v12-test-place-{index}"
        place_ids.append(place_id)
        session.add(
            Place(
                id=place_id,
                provider_key="v12-test",
                external_id=place_id,
                city_code="CN-TIANJIN",
                name=name,
                category=category,
                latitude=latitude,
                longitude=longitude,
                opening_hours_json=json.dumps({"status": "NORMALIZED", "open": "09:00", "close": "21:00"}),
                tags_json=json.dumps(["real_city", "captured_replay"]),
                metadata_json=json.dumps(
                    {
                        "cost_status": "KNOWN",
                        "cost_minor": cost,
                        "duration_min": 75,
                        "cost_evidence": {"status": "KNOWN", "amount_minor": cost, "currency": "CNY", "source": "captured_replay"},
                    }
                ),
                updated_at=start,
            )
        )
    session.commit()
    session.close()
    return journey_id, place_ids, commitment_id


def test_v12_authoritative_compose_same_evidence_execute_studio_and_edge() -> None:
    client = TestClient(app)
    journey_id, place_ids, commitment_id = _journey_with_inputs()

    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 3, "reservation_id": commitment_id},
    )
    assert composed.status_code == 201, composed.text
    payload = composed.json()
    assert payload["complete_plan_count"] == 3
    plan = payload["top_k"][0]
    assert len(plan["activities"]) == 12
    assert plan["mobility_evidence"]["mode_matrix_count"] >= 100

    # Mutating the provider row after composition must not change the
    # candidate's original frozen selection world.
    session = SessionLocal()
    fact = session.scalar(select(MobilityFact).where(MobilityFact.journey_id == journey_id))
    fact.duration_min = 9999
    session.commit()
    session.close()
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200

    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200
    first_id = plan["activities"][0]["id"]
    for action in ("depart", "arrive", "begin", "complete"):
        transitioned = client.post(f"/api/v1/journeys/{journey_id}/activities/{first_id}/{action}")
        assert transitioned.status_code == 200, transitioned.text

    preview_order = [item["id"] for item in plan["activities"]]
    # The V13 authoritative gate evaluates the frozen mobility fact against
    # the existing fixed time slots.  The first two ordinary activities are
    # intentionally too close to the protected check-in; use the later pair
    # whose existing slack makes the reorder genuinely feasible.
    tea_index = next(index for index, item in enumerate(plan["activities"]) if item["title"] == "Tea House")
    riverside_index = next(index for index, item in enumerate(plan["activities"]) if item["title"] == "Riverside Eye")
    preview_order[tea_index], preview_order[riverside_index] = preview_order[riverside_index], preview_order[tea_index]
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/preview",
        json={"activity_ids": preview_order},
    )
    assert preview.status_code == 200 and preview.json()["can_accept"]
    assert preview.json()["deterministic_gate"]["authoritative"] is True
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/trip-studio/accept",
        json={"activity_ids": preview_order, "preview_id": preview.json()["preview_id"]},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["plan"]["source"] == "MANUAL"

    offline = client.get(f"/api/v1/journeys/{journey_id}/offline-snapshot")
    assert offline.status_code == 200
    assert "execution" in offline.json()["snapshot"]
    assert offline.json()["snapshot"]["execution"]["next_state"] in {"NOT_STARTED", "ACTIVE"}

    device = client.post(f"/api/v1/journeys/{journey_id}/edge/devices", json={"display_name": "V12 PIC"})
    assert device.status_code == 201
    edge = client.get(
        f"/api/v1/edge/devices/{device.json()['device_id']}/journey-snapshot",
        headers={"X-Edge-Token": device.json()["device_token"]},
    )
    assert edge.status_code == 200
    assert edge.json()["now_state"] == offline.json()["snapshot"]["execution"]["now_state"]


def test_v12_studio_protects_locked_order() -> None:
    client = TestClient(app)
    journey_id, place_ids, _ = _journey_with_inputs()
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert composed.status_code == 201
    plan = composed.json()["top_k"][0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    order = [item["id"] for item in plan["activities"]]
    order[0], order[1] = order[1], order[0]
    preview = client.post(f"/api/v1/journeys/{journey_id}/trip-studio/preview", json={"activity_ids": order})
    assert preview.status_code == 200
    assert preview.json()["can_accept"] is False
    assert "PROTECTED_ACTIVITY_REORDER" in preview.json()["deterministic_gate"]["failures"]


def test_v12_autonomous_recovery_uses_preauthorized_envelope(monkeypatch) -> None:
    client = TestClient(app)
    journey_id, place_ids, commitment_id = _journey_with_inputs()
    # V4 #1 (T2 hard truth gate): the grounded-recovery path re-uses the frozen
    # baseline snapshot and hard-gates the selected route legs it evaluates.
    # This scenario declares those legs as EXPLICIT DETERMINISTIC FIXTURE facts
    # (test fixture values, NOT live/Xiamen mobility).  A pair outside the table
    # stays unseeded/synthetic, so the mock blanket default is never laundered
    # into a hard fact (B1 intact).  Shared with V13's authoritative journey.
    seed_authoritative_recovery_mobility(monkeypatch, journey_id)
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1, "reservation_id": commitment_id},
    )
    assert composed.status_code == 201
    base_plan = composed.json()["top_k"][0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{base_plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    # Advance the protected arrival/check-in prefix so the disruption targets
    # the first ordinary, reversible Activity.
    actions = {"ACTIVE": "depart", "EN_ROUTE": "arrive", "ARRIVED": "begin", "IN_PROGRESS": "complete"}
    complete_count = 0
    for _ in range(12):
        execution = client.get(f"/api/v1/journeys/{journey_id}/twin").json()["context"]["execution"]
        action = actions.get(execution["now_state"])
        assert action is not None
        assert client.post(
            f"/api/v1/journeys/{journey_id}/activities/{execution['now_activity_id']}/{action}"
        ).status_code == 200
        if action == "complete":
            complete_count += 1
            if complete_count == 2:
                break

    session = SessionLocal()
    journey = session.get(Journey, journey_id)
    now = datetime.now(timezone.utc).isoformat()
    event = Event(
        id=str(uuid4()),
        journey_id=journey_id,
        provider_key="v12-test",
        provenance="USER_REPORTED",
        event_type="TRAFFIC_DELAY",
        severity="MAJOR",
        status="DETECTED",
        affected_activity_id=journey.current_activity_id,
        occurred_at=now,
        detected_at=now,
        payload_json=json.dumps({"extra_minutes": 15, "source": "v12_test"}),
        created_at=now,
    )
    session.add(event)
    session.commit()
    session.close()

    policy = client.put(
        "/api/v1/travelers/11111111-1111-4111-8111-111111111111/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 0,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 30,
            "allowed_activity_types": ["activity"],
            "allowed_booking_types": [],
            "protected_commitment_levels": ["C3_Guaranteed", "C4_Anchored"],
            "allow_auto_commitment_management": False,
            "allow_auto_when_provider_degraded": False,
            "allow_auto_on_critical_event": False,
        },
    )
    assert policy.status_code == 200
    requested = client.post(
        "/api/v1/agent/tools/v2/request_replan",
        json={
            "request_id": "v12-test-request-replan",
            "agent_id": "v12-test-agent",
            "journey_id": journey_id,
            "trigger_event_id": event.id,
            "candidate_count": 1,
        },
    )
    assert requested.status_code == 200, requested.text
    proposal_id = requested.json()["proposal"]["id"]
    accepted = client.post(
        "/api/v1/agent/tools/v2/accept_replan",
        json={
            "request_id": "v12-test-accept-replan",
            "agent_id": "v12-test-agent",
            "journey_id": journey_id,
            "proposal_id": proposal_id,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["decision"] == "ALLOW_AUTO"
    assert accepted.json()["executed"] is True
    assert accepted.json()["result"]["journey"]["active_plan_version_id"] != base_plan["id"]


# V4 #5: V12 uses v12-scoped node keys (v12:{journey_id}:* and v12-test-place-*),
# so the shared place-* demo table was irrelevant to it.  The autonomous-recovery
# scenario now declares its OWN explicit deterministic fixture legs in-test
# (seed_explicit_mobility); no module-wide autouse inheritance.
import pytest as _pytest  # noqa: E402,F401
from _qoder_explicit_demo_mobility import (  # noqa: E402
    seed_authoritative_recovery_mobility,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)
