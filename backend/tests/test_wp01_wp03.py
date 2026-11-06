from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Activity,
    AuditLog,
    Base,
    CityService,
    Journey,
    JourneyConstraint,
    Place,
    PlanVersion,
)
from app.db.session import SessionLocal, ensure_schema, make_engine
from app.integrations.mock.providers import MockMobilityProvider
from app.main import app
from app.replanning.feasibility import (
    FeasibilityActivity,
    build_context,
    check_records,
    evaluate_feasibility,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
JOURNEY_ID = "22222222-2222-4222-8222-222222222222"
TRAVELER_ID = "11111111-1111-4111-8111-111111111111"
LOCK_START = "2026-10-10T15:30:00+08:00"
LOCKED_COMMITMENT = {
    "kind": "HARD",
    "category": "commitment",
    "rule_code": "LOCKED_COMMITMENT",
    "params": {"place_id": "place-river-cruise", "start_at": LOCK_START, "level": "LOCKED"},
    "weight": 1,
}


def reset() -> None:
    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200


def add_locked_commitment(*, start_at: str = LOCK_START, place_id: str = "place-river-cruise"):
    payload = {**LOCKED_COMMITMENT, "params": {"place_id": place_id, "start_at": start_at}}
    response = client.post(f"/api/v1/journeys/{JOURNEY_ID}/constraints", json=payload)
    assert response.status_code == 201
    return response.json()


def test_cnx_i_001_schema_from_empty_sqlite():
    empty_engine = make_engine("sqlite://")
    ensure_schema(empty_engine)
    LocalSession = sessionmaker(bind=empty_engine)
    with LocalSession() as session:
        tables = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        names = {row[0] for row in tables}
    expected_tables = {
        "travelers",
        "preferences",
        "journeys",
        "constraints",
        "plan_versions",
        "activities",
        "places",
        "city_services",
        "commitments",
        "bookings",
        "orders",
        "transactions",
        "events",
        "replan_proposals",
        "recommendations",
        "journey_twins",
        "provider_calls",
        "audit_logs",
        "idempotency_keys",
    }
    assert expected_tables <= names
    indexes = {
        row[0]
        for row in session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        )
    }
    assert {
        "idx_journeys_traveler",
        "idx_plan_versions_journey",
        "idx_activities_plan",
        "idx_commitments_journey",
        "idx_events_journey",
        "idx_replans_journey",
        "idx_audit_journey",
    } <= indexes
    journey_columns = {row[1] for row in session.execute(text("PRAGMA table_info(journeys)"))}
    assert "planning_mode" in journey_columns


def test_cnx_i_002_foreign_keys_enabled():
    empty_engine = make_engine("sqlite://")
    ensure_schema(empty_engine)
    LocalSession = sessionmaker(bind=empty_engine)
    with LocalSession() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO preferences (id, traveler_id, category, preference_key, value_json, weight, source, created_at, updated_at) VALUES ('x', 'missing', 'interest', 'art', 'true', 1, 'user', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
            )
            session.commit()


def test_orm_metadata_matches_repaired_sql_relationships_and_uniques():
    journeys = Base.metadata.tables["journeys"]
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in journeys.c.active_plan_version_id.foreign_keys
    } == {("plan_versions.id", "SET NULL")}
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in journeys.c.current_activity_id.foreign_keys
    } == {("activities.id", "SET NULL")}
    plan_versions = Base.metadata.tables["plan_versions"]
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in plan_versions.c.parent_plan_version_id.foreign_keys
    } == {("plan_versions.id", "SET NULL")}
    for table_name, columns in {
        "preferences": {"traveler_id", "category", "preference_key"},
        "places": {"provider_key", "external_id"},
        "city_services": {"provider_key", "external_id"},
        "plan_versions": {"journey_id", "version_no"},
        "activities": {"plan_version_id", "sequence_no"},
    }.items():
        unique_columns = {
            tuple(constraint.columns.keys())
            for constraint in Base.metadata.tables[table_name].constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert tuple(columns) in unique_columns or any(
            set(item) == columns for item in unique_columns
        )


def test_cnx_i_003_create_and_get_traveler():
    reset()
    payload = {"display_name": "Taylor", "timezone": "Asia/Shanghai"}
    created = client.post("/api/v1/travelers", json=payload)
    assert created.status_code == 201
    assert (
        client.get(f"/api/v1/travelers/{created.json()['id']}").json()["display_name"] == "Taylor"
    )


def test_cnx_i_004_replace_preferences():
    reset()
    response = client.put(
        f"/api/v1/travelers/{TRAVELER_ID}/preferences",
        json=[{"category": "interest", "key": "art", "value": True, "weight": 0.8}],
    )
    assert response.status_code == 200
    assert client.get(f"/api/v1/travelers/{TRAVELER_ID}/preferences").json()[0]["key"] == "art"


def test_cnx_i_004_duplicate_preferences_are_rejected_without_mutation():
    reset()
    baseline = [{"category": "interest", "key": "art", "value": True, "weight": 0.8}]
    assert (
        client.put(f"/api/v1/travelers/{TRAVELER_ID}/preferences", json=baseline).status_code == 200
    )
    existing = client.get(f"/api/v1/travelers/{TRAVELER_ID}/preferences").json()
    response = client.put(
        f"/api/v1/travelers/{TRAVELER_ID}/preferences",
        json=baseline + [{"category": "interest", "key": "art", "value": False, "weight": 0.2}],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/travelers/{TRAVELER_ID}/preferences").json() == existing


def test_cnx_i_005_valid_journey_is_draft():
    reset()
    payload = {
        "traveler_id": TRAVELER_ID,
        "title": "Test",
        "city_code": "CNX-DEMO",
        "timezone": "Asia/Shanghai",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T19:00:00+08:00",
        "budget": {"amount_minor": 50000, "currency": "CNY"},
        "planning_mode": "budget",
    }
    response = client.post("/api/v1/journeys", json=payload)
    assert (
        response.status_code == 201
        and response.json()["status"] == "DRAFT"
        and response.json()["planning_mode"] == "budget"
    )
    generated = client.post(
        f"/api/v1/journeys/{response.json()['id']}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201
    assert generated.json()[0]["planning_mode"] == "budget"


def test_cnx_i_006_end_before_start_is_422():
    reset()
    payload = {
        "traveler_id": TRAVELER_ID,
        "title": "Invalid",
        "city_code": "CNX-DEMO",
        "timezone": "Asia/Shanghai",
        "start_at": "2026-10-10T19:00:00+08:00",
        "end_at": "2026-10-10T09:00:00+08:00",
        "budget": {"amount_minor": 50000, "currency": "CNY"},
    }
    assert client.post("/api/v1/journeys", json=payload).status_code == 422


def test_cnx_i_007_hard_budget_constraint_changes_planner_output():
    reset()
    baseline = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert baseline.status_code == 201 and len(baseline.json()) == 3
    response = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={
            "kind": "HARD",
            "category": "budget",
            "rule_code": "BUDGET_MAX",
            "params": {"amount_minor": 33000},
            "weight": 1,
        },
    )
    assert response.status_code == 201 and response.json()["kind"] == "HARD"
    constrained = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert constrained.status_code == 201
    assert len(constrained.json()) < len(baseline.json())
    assert all(plan["total_cost"]["amount_minor"] <= 33000 for plan in constrained.json())


def test_candidate_count_one_skips_infeasible_raw_template_and_returns_next_feasible():
    reset()
    response = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={
            "kind": "HARD",
            "category": "budget",
            "rule_code": "BUDGET_MAX",
            "params": {"amount_minor": 33000},
            "weight": 1,
        },
    )
    assert response.status_code == 201
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201
    plans = generated.json()
    assert len(plans) == 1
    assert plans[0]["total_cost"]["amount_minor"] <= 33000


def test_cnx_i_008_places_are_deterministic():
    reset()
    first = client.get("/api/v1/places?city_code=CNX-DEMO").json()
    second = client.get("/api/v1/places?city_code=CNX-DEMO").json()
    assert first == second and len(first) == 8


def test_journey_end_by_rejects_late_candidates():
    reset()
    response = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={
            "kind": "HARD",
            "category": "time",
            "rule_code": "JOURNEY_END_BY",
            "params": {"time": "2026-10-10T16:00:00+08:00"},
            "weight": 1,
        },
    )
    assert response.status_code == 201
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert generated.status_code == 422
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json() == []


def test_closed_place_makes_all_candidates_infeasible():
    reset()
    with SessionLocal() as session:
        cruise = session.get(Place, "place-river-cruise")
        assert cruise is not None
        cruise.opening_hours_json = '{"open":"09:00","close":"15:00"}'
        session.commit()
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert generated.status_code == 422
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json() == []


def test_insufficient_mobility_gap_makes_candidate_infeasible():
    class SlowMockMobility:
        provider_key = "mock-mobility"

        def estimate_minutes(self, _origin: str, _destination: str) -> int:
            return 90

    start = "2026-10-10T09:00:00+08:00"
    context = build_context(
        {
            "id": JOURNEY_ID,
            "city_code": "CNX-DEMO",
            "start_at": start,
            "end_at": "2026-10-10T19:00:00+08:00",
            "budget_amount_minor": 50000,
            "budget_currency": "CNY",
        },
        [],
        {
            "place-a": {
                "id": "place-a",
                "city_code": "CNX-DEMO",
                "opening_hours_json": '{"open":"08:00","close":"22:00"}',
            },
            "place-b": {
                "id": "place-b",
                "city_code": "CNX-DEMO",
                "opening_hours_json": '{"open":"08:00","close":"22:00"}',
            },
        },
        {},
        mobility_provider=SlowMockMobility(),
    )
    result = evaluate_feasibility(
        context,
        [
            FeasibilityActivity(
                key="a",
                place_id="place-a",
                start_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
                end_at=datetime.fromisoformat("2026-10-10T10:00:00+08:00"),
                cost_minor=0,
            ),
            FeasibilityActivity(
                key="b",
                place_id="place-b",
                start_at=datetime.fromisoformat("2026-10-10T10:30:00+08:00"),
                end_at=datetime.fromisoformat("2026-10-10T11:00:00+08:00"),
                cost_minor=0,
            ),
        ],
    )
    assert not result.passed
    assert any(failure.startswith("INSUFFICIENT_TRAVEL_GAP") for failure in result.failures)


def test_cnx_i_009_generate_at_least_two_feasible_candidates():
    reset()
    response = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate",
        json={"planning_mode": "balanced", "candidate_count": 3},
    )
    assert response.status_code == 201
    plans = response.json()
    assert len(plans) >= 2
    assert all(
        plan["status"] == "CANDIDATE"
        and plan["activities"]
        and plan["score_breakdown"]["total"] >= 0
        for plan in plans
    )


def test_cnx_i_009_generated_candidates_pass_independent_authoritative_gate():
    reset()
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert generated.status_code == 201
    with SessionLocal() as session:
        journey = session.get(Journey, JOURNEY_ID)
        assert journey is not None
        constraints = session.scalars(
            select(JourneyConstraint).where(JourneyConstraint.journey_id == JOURNEY_ID)
        ).all()
        places = {
            item.id: item
            for item in session.scalars(
                select(Place).where(Place.city_code == journey.city_code)
            ).all()
        }
        services = {
            item.id: item
            for item in session.scalars(
                select(CityService).where(CityService.city_code == journey.city_code)
            ).all()
        }
        plans = session.scalars(
            select(PlanVersion).where(PlanVersion.journey_id == JOURNEY_ID)
        ).all()
        for plan in plans:
            activities = session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == plan.id)
                .order_by(Activity.sequence_no)
            ).all()
            assert check_records(
                journey,
                constraints,
                places,
                services,
                activities,
                mobility_provider=MockMobilityProvider(),
            ).passed


def test_select_revalidates_after_stricter_hard_constraint_without_state_change():
    reset()
    plans = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 2}
    ).json()
    before_journey = client.get(f"/api/v1/journeys/{JOURNEY_ID}").json()
    assert before_journey["status"] == "DRAFT" and before_journey["active_plan_version_id"] is None
    assert (
        client.post(
            f"/api/v1/journeys/{JOURNEY_ID}/constraints",
            json={
                "kind": "HARD",
                "category": "budget",
                "rule_code": "BUDGET_MAX",
                "params": {"amount_minor": 1000, "currency": "CNY"},
                "weight": 1,
            },
        ).status_code
        == 201
    )
    response = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plans[0]['id']}/select")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    after_journey = client.get(f"/api/v1/journeys/{JOURNEY_ID}").json()
    assert after_journey["status"] == before_journey["status"]
    assert after_journey["active_plan_version_id"] == before_journey["active_plan_version_id"]
    statuses = {
        item["id"]: item["status"]
        for item in client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json()
    }
    assert statuses[plans[0]["id"]] == "CANDIDATE"


def _select_first_plan() -> tuple[dict, dict]:
    plans = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 2}
    ).json()
    selected = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plans[0]['id']}/select")
    assert selected.status_code == 200
    return plans[0], selected.json()


def test_planned_journey_budget_patch_rejects_without_mutation():
    reset()
    plan, before = _select_first_plan()
    before_plans = client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json()
    response = client.patch(
        f"/api/v1/journeys/{JOURNEY_ID}",
        json={"budget": {"amount_minor": 1000, "currency": "CNY"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}").json() == before
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json() == before_plans
    assert plan["id"] == before["active_plan_version_id"]


def test_planned_journey_end_patch_rejects_without_mutation():
    reset()
    _, before = _select_first_plan()
    response = client.patch(
        f"/api/v1/journeys/{JOURNEY_ID}",
        json={"end_at": "2026-10-10T16:00:00+08:00"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}").json() == before


@pytest.mark.parametrize(
    ("category", "rule_code", "params"),
    [
        ("budget", "BUDGET_MAX", {"amount_minor": 1000, "currency": "CNY"}),
        ("time", "JOURNEY_END_BY", {"time": "2026-10-10T16:00:00+08:00"}),
    ],
)
def test_planned_hard_constraint_rejects_without_mutation(category, rule_code, params):
    reset()
    _, before = _select_first_plan()
    before_constraints = client.get(f"/api/v1/journeys/{JOURNEY_ID}/constraints").json()
    response = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={
            "kind": "HARD",
            "category": category,
            "rule_code": rule_code,
            "params": params,
            "weight": 1,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}").json() == before
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}/constraints").json() == before_constraints


def test_planned_journey_preserving_patch_succeeds_with_selected_plan_intact():
    reset()
    plan, before = _select_first_plan()
    response = client.patch(
        f"/api/v1/journeys/{JOURNEY_ID}",
        json={
            "title": "Preserved Journey",
            "budget": {"amount_minor": 40000, "currency": "CNY"},
            "end_at": "2026-10-10T18:45:00+08:00",
        },
    )
    assert response.status_code == 200
    after = response.json()
    assert (
        after["status"] == "PLANNED"
        and after["active_plan_version_id"] == plan["id"]
        and after["title"] == "Preserved Journey"
        and after["budget"]["amount_minor"] == 40000
        and after["end_at"] == "2026-10-10T18:45:00+08:00"
    )
    assert before["status"] == "PLANNED"


def test_cnx_i_010_select_candidate_plans_journey():
    reset()
    plans = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 2}
    ).json()
    response = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plans[0]['id']}/select")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PLANNED" and body["active_plan_version_id"] == plans[0]["id"]
    listed = client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json()
    selected = next(item for item in listed if item["id"] == plans[0]["id"])
    assert selected["status"] == "SELECTED"
    assert all(item["status"] != "ACTIVE" for item in listed)
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "plan.selected", AuditLog.entity_id == plans[0]["id"]
                )
            )
            is not None
        )


def test_locked_commitment_canonical_generation_and_persisted_selection_revalidation():
    reset()
    add_locked_commitment()
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert generated.status_code == 201
    plans = generated.json()
    assert len(plans) == 3
    cruise = next(
        activity
        for activity in plans[0]["activities"]
        if activity["place_id"] == "place-river-cruise"
    )
    assert cruise["start_at"] == LOCK_START
    selected = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plans[0]['id']}/select")
    assert selected.status_code == 200
    assert selected.json()["status"] == "PLANNED"
    assert selected.json()["active_plan_version_id"] == plans[0]["id"]


def test_locked_commitment_changed_start_blocks_generation():
    reset()
    add_locked_commitment(start_at="2026-10-10T16:00:00+08:00")
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 3}
    )
    assert generated.status_code == 422
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}/plans").json() == []


def test_locked_commitment_removed_cruise_blocks_persisted_selection():
    reset()
    add_locked_commitment()
    plan = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    with SessionLocal() as session:
        cruise = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == plan["id"],
                Activity.place_id == "place-river-cruise",
            )
        )
        assert cruise is not None
        session.delete(cruise)
        session.commit()
    selected = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plan['id']}/select")
    assert selected.status_code == 422
    assert selected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}").json()["status"] == "DRAFT"


def test_locked_commitment_different_place_at_same_time_blocks_persisted_selection():
    reset()
    add_locked_commitment()
    plan = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    with SessionLocal() as session:
        cruise = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == plan["id"],
                Activity.place_id == "place-river-cruise",
            )
        )
        assert cruise is not None
        cruise.place_id = "place-city-park"
        session.commit()
    selected = client.post(f"/api/v1/journeys/{JOURNEY_ID}/plans/{plan['id']}/select")
    assert selected.status_code == 422
    assert selected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/journeys/{JOURNEY_ID}").json()["status"] == "DRAFT"


def test_material_wp01_wp03_mutations_emit_audit_records():
    reset()
    assert (
        client.patch(
            f"/api/v1/journeys/{JOURNEY_ID}", json={"title": "Audited Journey"}
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/travelers/{TRAVELER_ID}/preferences",
            json=[{"category": "interest", "key": "art", "value": True, "weight": 0.8}],
        ).status_code
        == 200
    )
    # Payload changed by ORD-0219: the write layer now refuses a rule_code outside the canonical
    # vocabulary, and `NOOP` was one.  This door's subject is the audit trail, so it keeps using a
    # row the store accepts - `SLOW_PACE` is canonical and, being a preference, is never executed
    # as a hard gate.  That the old payload is now *refused* is pinned by
    # tests/test_qoder_ord0219_write_side_rule_guard.py, not silently here.
    constraint = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/constraints",
        json={"kind": "SOFT", "category": "test", "rule_code": "SLOW_PACE", "params": {}},
    )
    assert constraint.status_code == 201
    assert (
        client.delete(
            f"/api/v1/journeys/{JOURNEY_ID}/constraints/{constraint.json()['id']}"
        ).status_code
        == 204
    )
    with SessionLocal() as session:
        actions = {item.action for item in session.scalars(select(AuditLog)).all()}
    assert {
        "journey.updated",
        "preferences.replaced",
        "constraint.created",
        "constraint.deleted",
    } <= actions


def test_plan_generation_emits_audit_record():
    reset()
    generated = client.post(
        f"/api/v1/journeys/{JOURNEY_ID}/plans/generate", json={"candidate_count": 2}
    )
    assert generated.status_code == 201
    with SessionLocal() as session:
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "plans.generated", AuditLog.journey_id == JOURNEY_ID
            )
        )
        assert audit is not None
