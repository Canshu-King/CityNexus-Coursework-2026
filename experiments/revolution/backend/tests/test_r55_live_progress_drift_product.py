from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
import app.traveler_principal as principal_module
from app.core.config import settings
from app.db.models import Activity, Commitment, Event, GuardianRun, Journey, JourneyLeg, JourneyMember, JourneyTripContext, MobilityFact, Place
from app.db.session import SessionLocal
from app.execution.clock import execution_clock
from app.planning.authoritative_v12 import compose_authoritative_multiday
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers
from test_v12_authoritative_trip_execution import _journey_with_inputs, _seal_v12_test_plan


client = TestClient(main_module.app)


def _count(session, model, journey_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(model.journey_id == journey_id)) or 0)


def test_r55_one_authenticated_late_completion_flows_to_protected_commitment(monkeypatch):
    isolated = replace(
        settings,
        traveler_auth_secret="REDACTED_FOR_PUBLIC_RELEASE",
        lab_estimate_authority=False,
    )
    monkeypatch.setattr(main_module, "settings", isolated)
    monkeypatch.setattr(principal_module, "settings", isolated)

    journey_id, place_ids, anchor_commitment_id = _journey_with_inputs()
    replay_path = Path(__file__).resolve().parents[2] / "data" / "replay" / "tianjin_v135_journey_snapshot.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    provider_mobility = replay["mobility"]
    replay_candidates = replay["candidates"][:2]
    plan_place_ids = place_ids[: len(replay_candidates)]
    local_zone = timezone(timedelta(hours=8))
    trip_start = (datetime.now(local_zone) + timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
    trip_end = trip_start + timedelta(hours=12)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        context = session.get(JourneyTripContext, journey_id)
        anchor = session.get(Commitment, anchor_commitment_id)
        assert journey and context and anchor
        journey.start_at, journey.end_at = trip_start.isoformat(), trip_end.isoformat()
        arrival_node = next(item for item in provider_mobility["nodes"] if item.get("key") == "anchor:arrival")
        departure_node = next(item for item in provider_mobility["nodes"] if item.get("key") == "anchor:departure")
        context.arrival_anchor_json = json.dumps({"at": trip_start.isoformat(), "location": "Tianjin replay anchor", "latitude": arrival_node["latitude"], "longitude": arrival_node["longitude"]})
        context.departure_anchor_json = json.dumps({"at": trip_end.isoformat(), "location": "Tianjin replay departure", "latitude": departure_node["latitude"], "longitude": departure_node["longitude"]})
        anchor.policy_json = json.dumps({"semantic_node_key": "arrival-anchor", "scheduled_start_at": trip_start.isoformat(), "source": "USER_CONFIRMED_EXTERNAL_RESERVATION"})
        for place_id, candidate in zip(plan_place_ids, replay_candidates):
            place = session.get(Place, place_id)
            assert place
            place.external_id = candidate["external_id"]
            place.name = candidate["name"]
            place.latitude = candidate["latitude"]
            place.longitude = candidate["longitude"]
            place.category = candidate["category"]
        session.commit()

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey
        composed = compose_authoritative_multiday(
            session, journey, place_ids=plan_place_ids, candidate_count=1,
            reservation_id=anchor_commitment_id,
            discovery_evidence={"require_fully_grounded": True, "provider_mobility": provider_mobility},
        )
        plan_version = composed.plans[0]
        plan_rows = session.scalars(select(Activity).where(
            Activity.plan_version_id == plan_version.id,
        ).order_by(Activity.sequence_no)).all()
        plan = {
            "id": plan_version.id,
            "activities": [{"id": row.id, "title": row.title, "place_id": row.place_id,
                            "start_at": row.start_at, "end_at": row.end_at,
                            "sequence_no": row.sequence_no} for row in plan_rows],
        }
    _seal_v12_test_plan(plan)
    activities = plan["activities"]
    assert len(activities) >= 3
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
    started_journey = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started_journey.status_code == 200, started_journey.text

    first, next_activity = activities[0], activities[1]
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey and journey.current_activity_id == first["id"]
        traveler_id = str(journey.traveler_id)
        member_at = datetime.now(timezone.utc).isoformat()
        session.add(JourneyMember(
            id=str(uuid4()), journey_id=journey_id, traveler_id=traveler_id,
            role="OWNER", invited_by=traveler_id, expires_at=None,
            joined_at=member_at, removed_at=None, preferences_json="{}",
            created_at=member_at, updated_at=member_at,
        ))
        target_activity = session.get(Activity, next_activity["id"])
        assert target_activity
        session.add(Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=target_activity.id,
            kind="BOOKING",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=0,
            monetary_value_status="KNOWN",
            currency="CNY",
            external_ref="R55-EXPERIMENT-C4",
            policy_json=json.dumps({"scheduled_start_at": target_activity.start_at, "safety_buffer_min": 8, "source": "R55_EXPERIMENT_PROTECTED_COMMITMENT"}),
            created_at=trip_start.isoformat(),
            updated_at=trip_start.isoformat(),
        ))
        legs = session.scalars(select(JourneyLeg).where(
            JourneyLeg.plan_version_id == plan["id"],
            JourneyLeg.from_activity_id == first["id"],
            JourneyLeg.to_activity_id == next_activity["id"],
        )).all()
        assert len(legs) == 1
        leg = legs[0]
        assert leg.provider_snapshot_id and leg.provenance in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
        frozen_fact = session.get(MobilityFact, leg.provider_snapshot_id)
        assert frozen_fact is not None
        session.commit()

    headers = {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}
    operator_started = client.post(f"/api/v1/journeys/{journey_id}/operator/start", headers=headers, json={})
    assert operator_started.status_code == 200, operator_started.text

    # Run the true R42/R48 state graph. Only the execution clock is frozen;
    # completion timestamps and truth records are created by authenticated API handlers.
    milestones = [
        ("depart", datetime.fromisoformat(first["start_at"].replace("Z", "+00:00")) - timedelta(minutes=30)),
        ("arrive", datetime.fromisoformat(first["start_at"].replace("Z", "+00:00")) - timedelta(minutes=5)),
        ("begin", datetime.fromisoformat(first["start_at"].replace("Z", "+00:00"))),
    ]
    for action, stamp in milestones:
        with execution_clock.freeze(stamp):
            response = client.post(
                f"/api/v1/journeys/{journey_id}/activities/{first['id']}/{action}",
                headers=headers,
                json={"truth_kind": "OPERATOR_CONFIRMED_EXECUTION", "operator_confirmed": True},
            )
        assert response.status_code == 200, response.text

    planned_end = datetime.fromisoformat(first["end_at"].replace("Z", "+00:00"))
    actual_done = planned_end + timedelta(minutes=28)
    with SessionLocal() as session:
        before_events = _count(session, Event, journey_id)
        before_guardians = _count(session, GuardianRun, journey_id)
    with execution_clock.freeze(actual_done):
        completed = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{first['id']}/complete",
            headers=headers,
            json={"truth_kind": "OPERATOR_CONFIRMED_EXECUTION", "operator_confirmed": True},
        )
    assert completed.status_code == 200, completed.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        finished = session.get(Activity, first["id"])
        assert journey and finished
        truth = json.loads(finished.metadata_json)
        assert finished.status == "COMPLETED"
        assert datetime.fromisoformat(truth["completed_at_actual"]) == actual_done.astimezone(timezone.utc)
        assert truth["execution_truth"]["kind"] == "OPERATOR_CONFIRMED_EXECUTION"
        assert truth["execution_truth"]["transition"] == "COMPLETED"
        assert [item["transition"] for item in truth["execution_truth_history"][-4:]] == [
            "EN_ROUTE", "ARRIVED", "IN_PROGRESS", "COMPLETED",
        ]
        assert journey.current_activity_id == next_activity["id"]
        assert session.get(Activity, next_activity["id"]).status == "ACTIVE"
        drift_events = session.scalars(select(Event).where(
            Event.journey_id == journey_id,
            Event.event_type == "SCHEDULE_DELAY",
            Event.provenance == "SYSTEM_DERIVED",
        )).all()
        assert len(drift_events) == 1
        assert _count(session, Event, journey_id) == before_events + 1
        guardians = session.scalars(select(GuardianRun).where(
            GuardianRun.journey_id == journey_id,
        ).order_by(GuardianRun.created_at.desc(), GuardianRun.id.desc())).all()
        assert len(guardians) == before_guardians + 1
        event_payload = json.loads(drift_events[0].payload_json)
        assert datetime.fromisoformat(event_payload["source_milestone"]["actual_at"]) == actual_done.astimezone(timezone.utc)
        assert event_payload["derivation"] == "SYSTEM_DERIVED_EXECUTION_FACT"
        assert event_payload["source_execution_provenance"] == "OPERATOR_CONFIRMED_EXECUTION"
        assert event_payload["source_execution_milestone_id"] == first["id"]
        assert event_payload["source_execution_milestone"] == "COMPLETED"
        assert datetime.fromisoformat(event_payload["source_execution_at"]) == actual_done.astimezone(timezone.utc)
        assert event_payload["execution_drift"]["drift_min"] == 28
        assert event_payload["execution_drift"]["state"] == "COMMITMENT_AT_RISK"
        assert event_payload["execution_drift"]["next_risk"]["slack_after_min"] < 8

    operator = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=headers)
    assert operator.status_code == 200, operator.text
    projection = operator.json()
    assert projection["progress"]["state"] == "COMMITMENT_AT_RISK"
    assert projection["progress"]["basis"]["truth_kind"] == "OPERATOR_CONFIRMED_EXECUTION"
    assert any(item.get("risk_kind") == "EXECUTION_DRIFT_EXPOSURE" for item in projection["ahead"]["risks"])
    assert projection["progress"]["next_risk"]["slack_after_min"] < 8

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey
        result = {
            "capability": "CityNexus can now tell when the real trip is drifting away from the planned schedule — because the traveller actually left, arrived, started, or finished late — and automatically protect the rest of the Journey. It absorbs harmless lateness quietly, warns only when downstream commitments are threatened, and hands material drift to the same Operator for catch-up or replanning.",
            "outcome": ("PROGRESS_DRIFT_BRAIN_BLOCKED" if guardians[0].action_status == "OPERATOR_EXTERNAL_AGENT_BLOCKED" else "PROGRESS_DRIFT_TRIGGERED_RECOVERY"),
            "journey_id": journey_id,
            "active_plan_version_id": journey.active_plan_version_id,
            "completed_activity_id": first["id"],
            "next_activity_id": next_activity["id"],
            "protected_commitment_activity_id": next_activity["id"],
            "actual_milestone": truth["execution_truth"],
            "planned_end_at": finished.end_at,
            "completed_at_actual": truth["completed_at_actual"],
            "drift_min": projection["progress"]["drift_min"],
            "progress_state": projection["progress"]["state"],
            "slack_before_min": projection["progress"]["next_risk"]["slack_before_min"],
            "slack_after_min": projection["progress"]["next_risk"]["slack_after_min"],
            "route_evidence": projection["progress"]["next_risk"]["route"],
            "event_count_delta": _count(session, Event, journey_id) - before_events,
            "guardian_count_delta": _count(session, GuardianRun, journey_id) - before_guardians,
            "guardian_action_status": guardians[0].action_status,
            "current_activity_unchanged_by_recovery": journey.current_activity_id == next_activity["id"],
            "ahead_exposure": next((item for item in projection["ahead"]["risks"] if item.get("risk_kind") == "EXECUTION_DRIFT_EXPOSURE"), None),
            "lab_estimate_authority": bool(main_module.settings.lab_estimate_authority),
            "agent_estimate_authority": 0,
        }
        assert result["lab_estimate_authority"] is False
        output = Path(__file__).resolve().parents[1] / "output" / "r55-live-progress-drift-v1.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
