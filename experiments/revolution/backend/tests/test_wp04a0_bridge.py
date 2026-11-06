from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import AuditLog, JourneyConstraint
from app.db.session import SessionLocal
from app.main import app
from app.replanning.feasibility import FeasibilityActivity, build_context, evaluate_feasibility
from app.replanning.reference_replanner import Activity, Candidate, Commitment, validate_candidate
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _metrics() -> dict[str, float]:
    return {
        "commitment_preservation": 1.0,
        "stability": 1.0,
        "time_reliability": 1.0,
        "preference_fit": 1.0,
        "mobility_fit": 1.0,
        "budget_fit": 1.0,
        "robustness": 1.0,
        "opportunity_value": 1.0,
    }


def test_duplicate_semantic_locations_match_locks_by_start_independently():
    candidate = Candidate(
        "duplicate-place",
        (
            Activity("place-park", 60, 100),
            Activity("place-park", 180, 220),
        ),
        _metrics(),
    )
    for start in (60, 180):
        result = validate_candidate(
            candidate,
            journey_start_min=0,
            journey_end_min=600,
            budget_max_minor=10000,
            locked_commitments=[Commitment("place-park", "LOCKED", start)],
        )
        assert result.passed

    missing = validate_candidate(
        candidate,
        journey_start_min=0,
        journey_end_min=600,
        budget_max_minor=10000,
        locked_commitments=[Commitment("place-park", "LOCKED", 120)],
    )
    assert "LOCKED_COMMITMENT_LOST:place-park" in missing.failures


def test_feasibility_rejects_selected_activity_currency_mismatch_without_budget_rule():
    journey = {
        "id": "journey-currency",
        "city_code": "CNX-DEMO",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T19:00:00+08:00",
        "budget_amount_minor": 50000,
        "budget_currency": "CNY",
    }
    activity = FeasibilityActivity(
        key="place-old-town",
        place_id="place-old-town",
        start_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        end_at=datetime.fromisoformat("2026-10-10T10:00:00+08:00"),
        cost_minor=0,
        currency="USD",
    )
    place = {
        "id": "place-old-town",
        "city_code": "CNX-DEMO",
        "opening_hours_json": '{"open":"08:00","close":"20:00"}',
        "metadata_json": '{"available":true}',
    }
    result = evaluate_feasibility(
        build_context(journey, [], {"place-old-town": place}, {}),
        [activity],
    )
    assert not result.passed
    assert "ACTIVITY_CURRENCY_MISMATCH:place-old-town" in result.failures


def test_planned_journey_currency_change_is_rejected_without_hard_budget_constraint():
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    journey_id = reset.json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plans[0]['id']}/select").status_code
        == 200
    )
    # Remove the seeded HARD BUDGET_MAX so this regression exercises the
    # shared currency invariant itself rather than the pre-existing budget
    # constraint path.
    with SessionLocal() as session:
        session.query(JourneyConstraint).filter(
            JourneyConstraint.journey_id == journey_id,
            JourneyConstraint.rule_code == "BUDGET_MAX",
        ).delete(synchronize_session=False)
        session.commit()
    before = client.get(f"/api/v1/journeys/{journey_id}").json()
    response = client.patch(
        f"/api/v1/journeys/{journey_id}",
        json={"budget": {"amount_minor": 50000, "currency": "USD"}},
    )
    assert response.status_code == 422
    assert client.get(f"/api/v1/journeys/{journey_id}").json() == before


def test_selecting_current_plan_is_idempotent_and_does_not_add_audit():
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    journey_id = reset.json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    plan_id = plans[0]["id"]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select").status_code == 200
    with SessionLocal() as session:
        before = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "plan.selected", AuditLog.entity_id == plan_id)
        )
    second = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select")
    assert second.status_code == 200
    with SessionLocal() as session:
        after = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "plan.selected", AuditLog.entity_id == plan_id)
        )
    assert after == before == 1
