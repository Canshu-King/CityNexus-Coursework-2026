"""Deterministic R32 RecoveryEpoch contract tests.

These tests use only the local SQLAlchemy model and never call a provider or
external model.  The database unique index, rather than a process mutex, is
the concurrency arbiter for one OPEN epoch per Journey.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.main as main_module
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    AgentRun,
    Base,
    Event,
    Journey,
    JourneyMission,
    PlanVersion,
    RecoveryEpoch,
    ReplanProposal,
    Traveler,
)
from app.agent.approval import create_or_reuse_approval
from app.agent.gateway import AgentToolGateway
from app.db.session import _reconcile_agent_pending_action_status_check
from app.schemas import AgentToolCall
from app.recovery_epoch import (
    OPEN,
    RECOVERY_EPOCH_SUPERSEDED,
    RecoveryEpochSuperseded,
    commit_recovery_epoch,
    ensure_recovery_epoch,
    recovery_reality_basis,
    recovery_reality_fingerprint,
    semantic_plan_identity,
    validate_recovery_epoch_binding,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        traveler = Traveler(
            id=str(uuid4()), display_name="R32", email=None, locale="zh-CN",
            timezone="Asia/Shanghai", created_at=_now(), updated_at=_now(),
        )
        journey = Journey(
            id=str(uuid4()), traveler_id=traveler.id, title="R32", city_code="KIX",
            timezone="Asia/Tokyo", start_at="2026-10-06T09:00:00+09:00",
            end_at="2026-10-07T18:00:00+09:00", budget_currency="JPY",
            budget_amount_minor=10000, planning_mode="balanced", status="ACTIVE",
            active_plan_version_id=None, current_activity_id=None,
            created_at=_now(), updated_at=_now(), deleted_at=None,
        )
        value.add_all([traveler, journey])
        value.commit()
        yield value, journey


def _event(journey: Journey, *, event_id: str, status: str = "DETECTED", payload=None) -> Event:
    return Event(
        id=event_id, journey_id=journey.id, provider_key="fixture",
        provenance="FIXTURE", event_type="CLOSURE", severity="MAJOR", status=status,
        affected_activity_id=None, occurred_at="2026-10-06T09:00:00+09:00",
        detected_at="2026-10-06T09:01:00+09:00", resolved_at=None,
        payload_json=json.dumps(payload or {}, sort_keys=True), created_at=_now(),
    )


def test_open_epoch_is_idempotent_for_same_reality(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    assert first.id == second.id
    assert first.generation == second.generation == 1
    assert second.status == OPEN


@pytest.mark.parametrize("second_revision", [1, 2])
def test_provider_replay_dedupe_preserves_material_scope_change(session, second_revision):
    from app.agent.signal_pipeline import _upsert_fixture_event

    db, journey = session
    common = {
        "event_type": "CLOSURE", "severity": "MAJOR", "activity": None,
        "occurred_at": "2026-10-06T09:00:00+09:00",
    }
    first, created = _upsert_fixture_event(
        db, journey, scope_key="place-a",
        payload={"disruption_class": "PLACE_CLOSURE", "fixture_revision": 1}, **common,
    )
    assert created
    r1 = ensure_recovery_epoch(db, journey)
    db.commit()
    repeat, created = _upsert_fixture_event(
        db, journey, scope_key="place-a",
        payload={"disruption_class": "PLACE_CLOSURE", "fixture_revision": second_revision}, **common,
    )
    assert not created and repeat.id == first.id
    assert ensure_recovery_epoch(db, journey).id == r1.id
    second, created = _upsert_fixture_event(
        db, journey, scope_key="place-b",
        payload={"disruption_class": "PLACE_CLOSURE", "fixture_revision": second_revision}, **common,
    )
    assert created and second.id != first.id
    r2 = ensure_recovery_epoch(db, journey)
    assert r2.id != r1.id
    assert r1.status == "SUPERSEDED"


def test_relevant_event_change_supersedes_and_opens_next_generation(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.commit()
    db.add(_event(journey, event_id=str(uuid4()), payload={"incident_fingerprint": "A"}))
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    assert second.id != first.id
    assert second.generation == 2
    assert second.status == OPEN
    assert first.status == "SUPERSEDED"
    assert first.superseded_by_recovery_epoch_id == second.id


def test_irrelevant_resolved_event_does_not_change_epoch(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.add(_event(journey, event_id=str(uuid4()), status="RESOLVED"))
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    assert second.id == first.id
    assert recovery_reality_fingerprint(recovery_reality_basis(db, journey)) == first.recovery_reality_fingerprint


def test_bound_agent_cannot_forge_or_switch_epoch(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.add(_event(journey, event_id=str(uuid4()), payload={"incident_fingerprint": "B"}))
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    with pytest.raises(RecoveryEpochSuperseded) as error:
        validate_recovery_epoch_binding(db, journey.id, first.id)
    assert error.value.facts["code"] == RECOVERY_EPOCH_SUPERSEDED
    assert error.value.facts["current_recovery_epoch_id"] == second.id


def test_semantic_plan_identity_excludes_plan_and_proposal_ids(session):
    db, journey = session
    p1 = PlanVersion(
        id=str(uuid4()), journey_id=journey.id, version_no=1, planning_mode="balanced",
        status="CANDIDATE", lifecycle="ACTIVE", trigger_contract_json="{}", source="R32",
        parent_plan_version_id=None, total_cost_minor=100, currency="JPY",
        estimated_duration_min=60, utility_score=1.0, score_breakdown_json="{}",
        explanation_json="{}", mobility_snapshot_id=None, mobility_evidence_json=None,
        created_at=_now(),
    )
    p2 = PlanVersion(
        id=str(uuid4()), journey_id=journey.id, version_no=2, planning_mode="balanced",
        status="CANDIDATE", lifecycle="ACTIVE", trigger_contract_json="{}", source="OTHER",
        parent_plan_version_id=None, total_cost_minor=100, currency="JPY",
        estimated_duration_min=60, utility_score=0.2, score_breakdown_json="{}",
        explanation_json="different model text", mobility_snapshot_id=None,
        mobility_evidence_json=None, created_at=_now(),
    )
    db.add_all([p1, p2])
    db.flush()
    for plan in (p1, p2):
        db.add(Activity(
            id=str(uuid4()), plan_version_id=plan.id, sequence_no=1,
            activity_type="PLACE", title="Same future", place_id=None, city_service_id=None,
            start_at="2026-10-06T10:00:00+09:00", end_at="2026-10-06T11:00:00+09:00",
            travel_from_prev_min=0, estimated_cost_minor=100, currency="JPY",
            status="PLANNED", locked=False, metadata_json="{}", created_at=_now(), updated_at=_now(),
        ))
    db.commit()
    assert semantic_plan_identity(db, p1.id) == semantic_plan_identity(db, p2.id)


def test_duplicate_event_row_with_same_incident_does_not_rotate_epoch(session):
    db, journey = session
    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "same-incident"},
        )
    )
    db.commit()
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "same-incident"},
        )
    )
    db.commit()
    second = ensure_recovery_epoch(db, journey)

    assert second.id == first.id
    assert second.generation == first.generation == 1


def test_acknowledging_same_incident_does_not_rotate_epoch(session):
    db, journey = session
    event = _event(
        journey,
        event_id=str(uuid4()),
        payload={"incident_fingerprint": "ack-same-incident"},
    )
    db.add(event)
    db.commit()
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    event.status = "ACKNOWLEDGED"
    db.commit()
    second = ensure_recovery_epoch(db, journey)

    assert second.id == first.id
    assert second.recovery_reality_fingerprint == first.recovery_reality_fingerprint


def test_material_change_inside_same_incident_rotates_epoch(session):
    db, journey = session
    event = _event(
        journey,
        event_id=str(uuid4()),
        payload={"incident_fingerprint": "severity-incident"},
    )
    event.severity = "MINOR"
    db.add(event)
    db.commit()
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    event.severity = "CRITICAL"
    db.commit()
    second = ensure_recovery_epoch(db, journey)

    assert second.id != first.id
    assert second.generation == 2


def test_binding_validation_refreshes_reality_without_prior_explicit_ensure(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "late-shock"},
        )
    )
    db.commit()

    with pytest.raises(RecoveryEpochSuperseded) as error:
        validate_recovery_epoch_binding(db, journey.id, first.id)

    assert error.value.facts["code"] == RECOVERY_EPOCH_SUPERSEDED
    assert error.value.facts["stale_recovery_epoch_id"] == first.id
    assert error.value.facts["current_recovery_epoch_id"] != first.id
    current = db.get(RecoveryEpoch, error.value.facts["current_recovery_epoch_id"])
    assert current is not None
    assert current.generation == 2
    assert current.status == OPEN


def test_stale_validation_after_committed_success_does_not_mint_r3(session):
    db, journey = session

    # R1
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    # Material Shock B opens R2 and supersedes R1.
    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "shock-b-before-commit"},
        )
    )
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    db.commit()
    assert first.status == "SUPERSEDED"
    assert second.generation == 2

    # B legally commits R2.
    commit_recovery_epoch(db, second, reason="REPLAN_ACCEPTED")
    db.commit()
    assert second.status == "COMMITTED"

    before = db.scalars(
        select(RecoveryEpoch)
        .where(RecoveryEpoch.journey_id == journey.id)
        .order_by(RecoveryEpoch.generation)
    ).all()
    assert [row.generation for row in before] == [1, 2]

    # A/R1 checks its stale binding after B has already committed.  The check
    # must reject A without manufacturing an otherwise causeless R3.
    with pytest.raises(RecoveryEpochSuperseded) as error:
        validate_recovery_epoch_binding(db, journey.id, first.id)

    assert error.value.facts["stale_recovery_epoch_id"] == first.id
    assert error.value.facts["current_recovery_epoch_id"] == second.id
    after = db.scalars(
        select(RecoveryEpoch)
        .where(RecoveryEpoch.journey_id == journey.id)
        .order_by(RecoveryEpoch.generation)
    ).all()
    assert [row.generation for row in after] == [1, 2]
    assert not any(row.status == OPEN for row in after)


def test_committed_run_validation_is_inert_until_fresh_work_starts(session):
    db, journey = session
    epoch = ensure_recovery_epoch(db, journey)
    db.commit()
    commit_recovery_epoch(db, epoch, reason="REPLAN_ACCEPTED")
    db.commit()

    with pytest.raises(RecoveryEpochSuperseded) as error:
        validate_recovery_epoch_binding(db, journey.id, epoch.id)

    assert error.value.facts["current_recovery_epoch_id"] == epoch.id
    rows = db.scalars(
        select(RecoveryEpoch).where(RecoveryEpoch.journey_id == journey.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].status == "COMMITTED"


def test_database_enforces_one_open_epoch_per_journey(session):
    db, journey = session
    first = ensure_recovery_epoch(db, journey)
    db.commit()

    duplicate = RecoveryEpoch(
        id=str(uuid4()),
        journey_id=journey.id,
        generation=first.generation + 1,
        active_plan_version_id_at_open=journey.active_plan_version_id,
        material_world_epoch_at_open=None,
        recovery_reality_fingerprint="manual-duplicate-open",
        triggering_event_ids_json="[]",
        status=OPEN,
        opened_at=_now(),
        closed_at=None,
        close_reason=None,
        superseded_by_recovery_epoch_id=None,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_old_epoch_cannot_mint_new_approval_after_unseen_shock(session):
    db, journey = session
    old = ensure_recovery_epoch(db, journey)
    db.commit()
    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "approval-shock"},
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as error:
        create_or_reuse_approval(
            db,
            traveler_id=journey.traveler_id,
            journey_id=journey.id,
            action="ACCEPT_REPLAN",
            target_type="ReplanProposal",
            target_id="proposal-stale",
            request_fingerprint="fingerprint-stale",
            actor_id="agent-r32",
            required_decision="REQUIRE_CONFIRMATION",
            recovery_epoch_id=old.id,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == RECOVERY_EPOCH_SUPERSEDED


def test_gateway_uses_exact_bound_run_not_latest_mission_run(session):
    db, journey = session
    mission = JourneyMission(
        id=str(uuid4()),
        journey_id=journey.id,
        traveler_id=journey.traveler_id,
        trigger_event_id=None,
        user_text="Recover autonomously.",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
        status="RUNNING",
        created_at=_now(),
        started_at=_now(),
    )
    db.add(mission)
    first = ensure_recovery_epoch(db, journey)
    run_a = AgentRun(
        id=str(uuid4()),
        mission_id=mission.id,
        journey_id=journey.id,
        provider_key="external-a",
        model_key="a",
        status="RUNNING",
        created_at=_now(),
        started_at="2026-10-06T00:00:00+00:00",
        recovery_epoch_id=first.id,
    )
    db.add(run_a)
    db.commit()

    db.add(
        _event(
            journey,
            event_id=str(uuid4()),
            payload={"incident_fingerprint": "second-shock"},
        )
    )
    db.commit()
    second = ensure_recovery_epoch(db, journey)
    run_b = AgentRun(
        id=str(uuid4()),
        mission_id=mission.id,
        journey_id=journey.id,
        provider_key="external-b",
        model_key="b",
        status="RUNNING",
        created_at=_now(),
        started_at="2026-10-06T00:01:00+00:00",
        recovery_epoch_id=second.id,
    )
    db.add(run_b)
    db.commit()

    request = AgentToolCall(
        request_id="exact-run-binding",
        journey_id=journey.id,
        mission_id=mission.id,
    )
    with pytest.raises(HTTPException) as stale:
        AgentToolGateway(
            db, bound_run_id=run_a.id
        )._bound_recovery_epoch_id(request, journey)
    assert stale.value.detail["code"] == RECOVERY_EPOCH_SUPERSEDED

    exact = AgentToolGateway(
        db, bound_run_id=run_b.id
    )._bound_recovery_epoch_id(request, journey)
    assert exact == second.id


def test_legacy_pending_action_check_is_upgraded_for_superseded():
    engine = create_engine("sqlite://", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE agent_pending_actions (
                id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                journey_id TEXT NOT NULL,
                originating_step_id TEXT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN (
                        'PENDING','RESUMING','RESUMED','FAILED','EXPIRED'
                    )),
                result_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                resumed_at TEXT,
                updated_at TEXT NOT NULL,
                authority_receipt_id TEXT,
                recovery_epoch_id TEXT
            )
            """
        )
        _reconcile_agent_pending_action_status_check(connection)
        ddl = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='agent_pending_actions'"
        ).scalar()
        assert ddl is not None
        assert "SUPERSEDED" in ddl
        connection.exec_driver_sql(
            """
            INSERT INTO agent_pending_actions (
                id, approval_id, run_id, mission_id, journey_id,
                tool_name, status, created_at, updated_at
            ) VALUES (
                'pending-1', 'approval-1', 'run-1', 'mission-1',
                'journey-1', 'accept_replan', 'SUPERSEDED',
                '2026-10-06T00:00:00+00:00',
                '2026-10-06T00:00:00+00:00'
            )
            """
        )
        status = connection.exec_driver_sql(
            "SELECT status FROM agent_pending_actions WHERE id='pending-1'"
        ).scalar()
        assert status == "SUPERSEDED"


def test_collective_authority_surface_is_current_epoch_and_trigger_pure(
    session, monkeypatch
):
    db, journey = session

    base = PlanVersion(
        id=str(uuid4()),
        journey_id=journey.id,
        version_no=1,
        planning_mode="balanced",
        status="ACTIVE",
        lifecycle="ACTIVE",
        trigger_contract_json="{}",
        source="INITIAL",
        parent_plan_version_id=None,
        total_cost_minor=0,
        currency="JPY",
        estimated_duration_min=60,
        utility_score=0.0,
        score_breakdown_json="{}",
        explanation_json="{}",
        mobility_snapshot_id=None,
        mobility_evidence_json="{}",
        created_at=_now(),
    )
    db.add(base)
    db.flush()
    journey.active_plan_version_id = base.id

    event_a = _event(
        journey,
        event_id=str(uuid4()),
        payload={"incident_fingerprint": "surface-event-a"},
    )
    db.add(event_a)
    db.flush()
    r1 = ensure_recovery_epoch(db, journey)
    db.commit()

    event_b = _event(
        journey,
        event_id=str(uuid4()),
        payload={"incident_fingerprint": "surface-event-b"},
    )
    event_b.event_type = "WEATHER"
    db.add(event_b)
    db.commit()
    r2 = ensure_recovery_epoch(db, journey)
    db.commit()

    plans = {}
    for offset, name in enumerate(
        (
            "target",
            "current-same-trigger",
            "stale-r1",
            "legacy-unbound",
            "current-other-trigger",
        ),
        start=2,
    ):
        plan = PlanVersion(
            id=str(uuid4()),
            journey_id=journey.id,
            version_no=offset,
            planning_mode="balanced",
            status="CANDIDATE",
            lifecycle="ACTIVE",
            trigger_contract_json="{}",
            source="REPLAN",
            parent_plan_version_id=base.id,
            total_cost_minor=offset,
            currency="JPY",
            estimated_duration_min=60,
            utility_score=0.0,
            score_breakdown_json="{}",
            explanation_json=json.dumps({"name": name}),
            mobility_snapshot_id=None,
            mobility_evidence_json="{}",
            created_at=_now(),
        )
        plans[name] = plan
        db.add(plan)
    db.flush()

    def proposal(name, *, epoch_id, trigger_id):
        plan = plans[name]
        return ReplanProposal(
            id=str(uuid4()),
            journey_id=journey.id,
            trigger_event_id=trigger_id,
            base_plan_version_id=base.id,
            proposed_plan_version_id=plan.id,
            status="PRESENTED",
            hard_constraints_passed=True,
            locked_commitments_preserved=True,
            utility_score=0.0,
            score_breakdown_json="{}",
            diff_json="{}",
            explanation_json="{}",
            created_at=_now(),
            recovery_epoch_id=epoch_id,
            semantic_identity=f"semantic-{name}",
            canonical_proposal_id=None,
        )

    db.add_all(
        [
            proposal("target", epoch_id=r2.id, trigger_id=event_a.id),
            proposal(
                "current-same-trigger",
                epoch_id=r2.id,
                trigger_id=event_a.id,
            ),
            proposal("stale-r1", epoch_id=r1.id, trigger_id=event_a.id),
            proposal("legacy-unbound", epoch_id=None, trigger_id=event_a.id),
            proposal(
                "current-other-trigger",
                epoch_id=r2.id,
                trigger_id=event_b.id,
            ),
        ]
    )
    db.commit()

    monkeypatch.setattr(
        "app.collective_mediation.journey_has_unresolved_collective_mediation",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.collective_mediation.pending_resolved_mediation_selection",
        lambda *_args, **_kwargs: None,
    )
    captured_windows = []

    def fake_members(_session, _journey, candidate_windows=None):
        if candidate_windows is None:
            return {
                "member_count": 2,
                "members": [
                    {"member_id": "member-a"},
                    {"member_id": "member-b"},
                ],
            }
        captured_windows.extend(candidate_windows)
        return {
            "member_count": 2,
            "members": [
                {"member_id": "member-a"},
                {"member_id": "member-b"},
            ],
            "candidate_fit_matrix": [],
            "mediation": {
                "classification": "HUMAN_TRADEOFF_REQUIRED",
            },
        }

    monkeypatch.setattr(
        main_module,
        "active_journey_members_with_capabilities",
        fake_members,
    )
    monkeypatch.setattr(
        main_module,
        "plan_collective_fit_subject",
        lambda *_args, **_kwargs: {
            "fit_subject_place_ids": [],
            "fit_subject_basis": "NO_OPTIONAL_PLAN_DELTA",
        },
    )
    monkeypatch.setattr(
        main_module,
        "plan_schema",
        lambda *_args, **_kwargs: SimpleNamespace(
            model_dump=lambda **_kw: {"selectable": True}
        ),
    )

    gate = main_module.collective_mutation_gate(
        db,
        journey,
        plans["target"],
        hard_feasible_plan_ids={
            plans["target"].id,
            plans["current-same-trigger"].id,
        },
    )

    assert {row["window_id"] for row in captured_windows} == {
        plans["target"].id,
        plans["current-same-trigger"].id,
    }
    assert gate["candidate_surface_scope"] == "RECOVERY_EPOCH_TRIGGER"
    assert gate["candidate_surface_recovery_epoch_id"] == r2.id
    assert gate["candidate_surface_trigger_event_id"] == event_a.id
    assert plans["stale-r1"].id not in {
        row["window_id"] for row in captured_windows
    }
    assert plans["legacy-unbound"].id not in {
        row["window_id"] for row in captured_windows
    }
    assert plans["current-other-trigger"].id not in {
        row["window_id"] for row in captured_windows
    }
