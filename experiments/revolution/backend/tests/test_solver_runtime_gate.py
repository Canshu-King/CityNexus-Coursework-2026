from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_solver_reference_contract import _problem

import app.main as main_module
import app.planning.solver_runtime as solver_runtime_module
from app.db.models import Activity, Commitment, Journey, Place, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from app.planning.ortools_replan_solver import (
    ORToolsRun,
    ORToolsUnavailable,
    solve_replacement_candidates,
)
from app.planning.solver_contract import SolverCandidate
from app.planning.solver_runtime import SolverAttempt

client = TestClient(main_module.app)


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# These gate tests exercise solver/window/budget behaviour on top of a real
# replan, so give the demo graph *explicit* mobility rather than a fabricated
# constant (see tests/_qoder_explicit_demo_mobility.py).
from _qoder_explicit_demo_mobility import (  # noqa: E402
    SOLVER_RUNTIME_MOBILITY as _MODULE_MOBILITY,
    seed_module_mobility as _seed,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@pytest.fixture(autouse=True)
def _explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)


def _prepare_disruption() -> tuple[str, dict[str, object], dict[str, object]]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )

    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings",
        json={"activity_id": cruise["id"]},
    ).json()
    captured = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"ort-capture-{uuid4()}"},
    )
    assert captured.status_code == 201, captured.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        completed = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{activity['id']}/complete"
        )
        assert completed.status_code == 200, completed.text

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "solver runtime gate"},
        },
    )
    assert event.status_code == 201, event.text
    return journey_id, plan, event.json()


def _generate(journey_id: str, event_id: str, *, count: int = 3) -> dict[str, object]:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": count},
        headers={"Idempotency-Key": f"ort-replan-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _proposals(journey_id: str, event_id: str) -> list[dict[str, object]]:
    return [
        item
        for item in client.get(f"/api/v1/journeys/{journey_id}/replans").json()
        if item["trigger_event_id"] == event_id
    ]


def _enable_ortools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")


def test_ort01_completed_prefix_is_unchanged_after_solver_materialization(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, base, event = _prepare_disruption()
    chosen = _generate(journey_id, event["id"], count=3)

    with SessionLocal() as session:
        old = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == base["id"])
            .order_by(Activity.sequence_no)
        ).all()
        new = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == chosen["proposed_plan_version_id"])
            .order_by(Activity.sequence_no)
        ).all()
        terminal = {item.sequence_no: item for item in old if item.status == "COMPLETED"}
        copied = {item.sequence_no: item for item in new}

    assert terminal
    for sequence, before in terminal.items():
        after = copied[sequence]
        assert (
            after.place_id,
            after.city_service_id,
            after.start_at,
            after.end_at,
            after.status,
        ) == (
            before.place_id,
            before.city_service_id,
            before.start_at,
            before.end_at,
            before.status,
        )


def test_ort02_c4_semantic_key_and_time_are_unchanged(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    chosen = _generate(journey_id, event["id"])
    with SessionLocal() as session:
        commitment = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == journey_id,
                Commitment.level == "C4_Anchored",
                Commitment.status == "ACTIVE",
            )
        )
        policy = json.loads(commitment.policy_json)
        anchor = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == chosen["proposed_plan_version_id"],
                Activity.place_id == policy["semantic_key"],
            )
        )
    assert anchor is not None
    assert datetime.fromisoformat(anchor.start_at) == datetime.fromisoformat(
        policy["scheduled_start_at"]
    )


def test_ort03_solver_reuses_one_operation_snapshot_and_never_recaptures(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    original = main_module.capture_runtime_mobility_snapshot
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", counted)
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    snapshot_ids = {
        item["diff"]["verified_evidence"]["mobility"]["snapshot"]["snapshot_id"]
        for item in proposals
    }
    assert calls == 1
    assert len(snapshot_ids) == 1


def _malicious_attempt(
    candidate_count: int,
    *,
    start_min: int = 390,
    end_min: int = 450,
) -> SolverAttempt:
    candidate = SolverCandidate(
        option_key="place-design-library",
        location_key="place-design-library",
        start_min=start_min,
        end_min=end_min,
        cost_minor=2000,
        inbound_travel_min=20,
        outbound_travel_min=20,
        schedule_shift_min=130,
        preference_milli=0,
        dropped=False,
        fingerprint="malicious-overlap",
        generator_kind="ORTOOLS_CP_SAT",
        solver_status="FEASIBLE",
        generator_rank=0,
    )
    return SolverAttempt(
        candidates=(candidate,)[:candidate_count],
        engine="ORTOOLS_CP_SAT",
        terminal_status="FEASIBLE",
        fallback_reason=None,
        attempted_solves=1,
        problem_fingerprint="malicious-problem",
        mobility_snapshot_id="malicious-snapshot",
    )


def test_ort04_malicious_solver_assignment_is_rejected_by_authoritative_gate(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda _problem, *, candidate_count, **_kwargs: _malicious_attempt(candidate_count),
        raising=False,
    )
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert proposals
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in proposals
    )


def test_ort05_fixed_seed_and_single_worker_are_reproducible():
    first = solve_replacement_candidates(_problem(), candidate_count=3).candidates
    second = solve_replacement_candidates(_problem(), candidate_count=3).candidates
    assert first == second


def test_ort06_opening_window_is_hard_in_solver_and_authoritative_gate(monkeypatch):
    problem = _problem(allow_drop=False)
    option = dataclasses.replace(
        problem.replacement_options[0], earliest_start_min=350, latest_end_min=370
    )
    run = solve_replacement_candidates(
        dataclasses.replace(problem, replacement_options=(option,)), candidate_count=1
    )
    assert run.candidates == ()

    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    with SessionLocal() as session:
        design = session.get(Place, "place-design-library")
        design.opening_hours_json = json.dumps({"open": "14:00", "close": "19:00"})
        session.commit()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda _problem, *, candidate_count, **_kwargs: _malicious_attempt(
            candidate_count, start_min=285, end_min=345
        ),
    )
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert proposals
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in proposals
    )


def test_ort07_drop_policy_never_drops_c4_and_obeys_allow_drop():
    no_routes = dataclasses.replace(_problem(), travel_minutes={})
    assert solve_replacement_candidates(no_routes, candidate_count=2).candidates == ()
    direct_only = dataclasses.replace(
        _problem(),
        replacement_options=(),
        travel_minutes={("place-tech-art", "place-river-cruise"): 20},
    )
    dropped = solve_replacement_candidates(direct_only, candidate_count=2).candidates
    assert len(dropped) == 1 and dropped[0].dropped
    assert dropped[0].option_key is None
    assert (
        solve_replacement_candidates(
            dataclasses.replace(direct_only, allow_drop=False), candidate_count=1
        ).candidates
        == ()
    )


def test_ort08_budget_is_hard_in_solver_and_authoritative_gate(monkeypatch):
    candidates = solve_replacement_candidates(_problem(budget=0), candidate_count=3).candidates
    assert all(item.cost_minor == 0 for item in candidates)
    assert all(item.option_key != "design-library" for item in candidates)

    _enable_ortools(monkeypatch)
    journey_id, base, event = _prepare_disruption()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        rows = session.scalars(select(Activity).where(Activity.plan_version_id == base["id"])).all()
        affected_id = event["affected_activity_id"]
        fixed_cost = sum(item.estimated_cost_minor for item in rows if item.id != affected_id)
        journey.budget_amount_minor = fixed_cost + 1000
        session.commit()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda _problem, *, candidate_count, **_kwargs: _malicious_attempt(
            candidate_count, start_min=285, end_min=345
        ),
    )
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert proposals
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in proposals
    )


def test_ort09_unavailable_solver_falls_back_to_current_deterministic_path(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()

    def unavailable(*_args, **_kwargs):
        raise ORToolsUnavailable("simulated missing optional dependency")

    monkeypatch.setattr(solver_runtime_module, "solve_replacement_candidates", unavailable)
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert proposals
    generator = proposals[0]["diff"]["verified_evidence"]["generator"]
    assert generator["kind"] == "DETERMINISTIC_HEURISTIC"
    assert generator["fallback_reason"] == "ORTOOLS_UNAVAILABLE"


def test_ort10_offline_closure_golden_accepts_solver_proposal(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    chosen = _generate(journey_id, event["id"])
    accepted = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": f"ort-accept-{uuid4()}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["proposal"]["status"] == "ACCEPTED"


def test_ort11_solver_provenance_keeps_citynexus_as_feasibility_authority(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    _generate(journey_id, event["id"])
    solver_proposals = [
        item
        for item in _proposals(journey_id, event["id"])
        if item["diff"]["verified_evidence"]["generator"]["kind"] == "ORTOOLS_CP_SAT"
    ]
    assert solver_proposals
    evidence = solver_proposals[0]["diff"]["verified_evidence"]
    assert evidence["feasibility_authority"] == "CITYNEXUS_DETERMINISTIC_GATE"
    assert evidence["generator"]["solver_status"] in {"OPTIMAL", "FEASIBLE"}


def test_ort12_same_frozen_problem_has_stable_semantic_ordering():
    first = solve_replacement_candidates(_problem(), candidate_count=3).candidates
    second = solve_replacement_candidates(_problem(), candidate_count=3).candidates
    assert [(item.option_key, item.dropped) for item in first] == [
        (item.option_key, item.dropped) for item in second
    ]


def test_ort13_top_k_assignments_are_semantically_unique():
    candidates = solve_replacement_candidates(_problem(), candidate_count=5).candidates
    identities = [item.option_key if not item.dropped else "DROP" for item in candidates]
    assert len(identities) == len(set(identities))


def test_ort14_unknown_solver_status_cannot_create_solver_proposal(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()

    partial = solve_replacement_candidates(_problem(), candidate_count=1).candidates

    def unknown(*_args, **_kwargs):
        return ORToolsRun(partial, "UNKNOWN", 2)

    monkeypatch.setattr(solver_runtime_module, "solve_replacement_candidates", unknown)
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in proposals
    )


def test_ort15_solver_problem_is_pure_json_serializable_data():
    payload = _problem().to_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert "Session" not in encoded
    assert "httpx" not in encoded
    assert "provider" not in encoded.lower()


def test_ort16_accept_time_feasibility_revalidation_remains_authoritative(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    chosen = _generate(journey_id, event["id"])
    with SessionLocal() as session:
        anchor = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == chosen["proposed_plan_version_id"],
                Activity.place_id == "place-river-cruise",
            )
        )
        anchor.start_at = (
            datetime.fromisoformat(anchor.start_at) + timedelta(minutes=10)
        ).isoformat()
        anchor.end_at = (datetime.fromisoformat(anchor.end_at) + timedelta(minutes=10)).isoformat()
        session.commit()
    accepted = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": f"ort-stale-{uuid4()}"},
    )
    assert accepted.status_code in {409, 422}


def test_ort17_candidate_count_does_not_persist_hidden_candidates(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    _generate(journey_id, event["id"], count=1)
    with SessionLocal() as session:
        proposals = session.scalars(
            select(ReplanProposal).where(ReplanProposal.trigger_event_id == event["id"])
        ).all()
        plan_ids = {item.proposed_plan_version_id for item in proposals}
        candidates = session.scalars(
            select(PlanVersion).where(
                PlanVersion.journey_id == journey_id,
                PlanVersion.status == "CANDIDATE",
            )
        ).all()
    assert len(proposals) == 1
    assert {item.id for item in candidates} == plan_ids


def test_ort18_rejected_solver_rows_leave_no_orphan_candidate_plan(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda _problem, *, candidate_count, **_kwargs: _malicious_attempt(candidate_count),
        raising=False,
    )
    _generate(journey_id, event["id"], count=2)
    with SessionLocal() as session:
        proposals = session.scalars(
            select(ReplanProposal).where(ReplanProposal.trigger_event_id == event["id"])
        ).all()
        candidates = session.scalars(
            select(PlanVersion).where(
                PlanVersion.journey_id == journey_id,
                PlanVersion.status == "CANDIDATE",
            )
        ).all()
    assert {item.id for item in candidates} == {item.proposed_plan_version_id for item in proposals}


def test_ort19_provenance_has_stable_problem_fingerprint_and_exact_snapshot(monkeypatch):
    _enable_ortools(monkeypatch)
    journey_id, _base, event = _prepare_disruption()
    _generate(journey_id, event["id"])
    solver = next(
        item
        for item in _proposals(journey_id, event["id"])
        if item["diff"]["verified_evidence"]["generator"]["kind"] == "ORTOOLS_CP_SAT"
    )
    evidence = solver["diff"]["verified_evidence"]
    generator = evidence["generator"]
    snapshot_id = evidence["mobility"]["snapshot"]["snapshot_id"]
    assert len(generator["problem_fingerprint"]) == 64
    assert generator["mobility_snapshot_id"] == snapshot_id
    assert generator["assignment_fingerprint"]


def test_ort20_disabled_mode_preserves_deterministic_heuristic_path(monkeypatch):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "deterministic")
    journey_id, _base, event = _prepare_disruption()
    _generate(journey_id, event["id"])
    proposals = _proposals(journey_id, event["id"])
    assert proposals
    assert {item["diff"]["candidate_family"] for item in proposals} >= {
        "F0_SAME_SLOT_REPLACEMENT",
        "F3_OPPORTUNITY_WINDOW",
    }
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in proposals
    )
