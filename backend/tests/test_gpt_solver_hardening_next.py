from __future__ import annotations

from uuid import uuid4

from test_solver_runtime_gate import (
    _generate,
    _prepare_disruption,
    _proposals,
    client,
)

import app.main as main_module
from app.db.models import Activity
from app.db.session import SessionLocal
from app.planning.solver_contract import SolverCandidate, solver_problem_fingerprint
from app.planning.solver_runtime import SolverAttempt
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


def _empty_attempt(problem, *, terminal_status: str = "INFEASIBLE") -> SolverAttempt:
    return SolverAttempt(
        candidates=(),
        engine="DETERMINISTIC_HEURISTIC",
        terminal_status=terminal_status,
        fallback_reason=f"ORTOOLS_{terminal_status}",
        attempted_solves=1,
        problem_fingerprint=solver_problem_fingerprint(problem),
        mobility_snapshot_id=problem.frozen_mobility_snapshot_id,
    )


def _valid_design_attempt(problem) -> SolverAttempt:
    option = next(
        item for item in problem.replacement_options if item.option_key == "place-design-library"
    )
    inbound = problem.travel(problem.current_location_key, option.location_key)
    outbound = problem.travel(option.location_key, problem.next_anchor.location_key)
    start = max(problem.now_min + inbound, option.earliest_start_min, option.original_start_min)
    end = start + option.duration_min
    candidate = SolverCandidate(
        option_key=option.option_key,
        location_key=option.location_key,
        start_min=start,
        end_min=end,
        cost_minor=option.cost_minor,
        inbound_travel_min=inbound,
        outbound_travel_min=outbound,
        schedule_shift_min=abs(start - option.original_start_min),
        preference_milli=option.preference_milli,
        dropped=False,
        fingerprint="valid-design-candidate",
        generator_kind="ORTOOLS_CP_SAT",
        solver_status="OPTIMAL",
        generator_rank=0,
    )
    return SolverAttempt(
        candidates=(candidate,),
        engine="ORTOOLS_CP_SAT",
        terminal_status="OPTIMAL",
        fallback_reason=None,
        attempted_solves=1,
        problem_fingerprint=solver_problem_fingerprint(problem),
        mobility_snapshot_id=problem.frozen_mobility_snapshot_id,
    )


def _malicious_design_attempt(problem) -> SolverAttempt:
    option = next(
        item for item in problem.replacement_options if item.option_key == "place-design-library"
    )
    candidate = SolverCandidate(
        option_key=option.option_key,
        location_key=option.location_key,
        start_min=390,  # 15:30, deliberately collides with the protected River Cruise C4.
        end_min=450,
        cost_minor=option.cost_minor,
        inbound_travel_min=20,
        outbound_travel_min=20,
        schedule_shift_min=130,
        preference_milli=option.preference_milli,
        dropped=False,
        fingerprint="malicious-design-candidate",
        generator_kind="ORTOOLS_CP_SAT",
        solver_status="FEASIBLE",
        generator_rank=0,
    )
    return SolverAttempt(
        candidates=(candidate,),
        engine="ORTOOLS_CP_SAT",
        terminal_status="FEASIBLE",
        fallback_reason=None,
        attempted_solves=1,
        problem_fingerprint=solver_problem_fingerprint(problem),
        mobility_snapshot_id=problem.frozen_mobility_snapshot_id,
    )


def test_srh01_solver_problem_carries_verified_traveler_preference_signal(monkeypatch):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")
    journey_id, _base, event = _prepare_disruption()
    captured = {}

    def inspect(problem, *, candidate_count, **_kwargs):
        captured["problem"] = problem
        return _empty_attempt(problem)

    monkeypatch.setattr(main_module, "generate_solver_candidates", inspect)
    _generate(journey_id, event["id"], count=3)
    problem = captured["problem"]
    preference = {item.option_key: item.preference_milli for item in problem.replacement_options}

    # Demo traveler has verified interest weights. Culture (Design Library) and
    # waterfront (City Park) must not both reach CP-SAT as the same zero signal.
    assert preference["place-design-library"] > 0
    assert preference["place-city-park"] > 0
    assert preference["place-design-library"] != preference["place-city-park"]


def test_srh02_solver_v1_does_not_optimize_activity_after_its_next_c4_anchor(monkeypatch):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")
    journey_id, base, _tech_event = _prepare_disruption()
    skyline = next(
        item for item in base["activities"] if item["place_id"] == "place-skyline-dinner"
    )
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": skyline["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "future activity after protected anchor"},
        },
    )
    assert event.status_code == 201, event.text
    calls = 0

    def should_not_run(problem, *, candidate_count, **_kwargs):
        nonlocal calls
        calls += 1
        return _empty_attempt(problem)

    monkeypatch.setattr(main_module, "generate_solver_candidates", should_not_run)
    _generate(journey_id, event.json()["id"], count=3)
    assert calls == 0


def test_srh03_solver_v1_fails_closed_when_intervening_mutable_activities_are_not_modeled(
    monkeypatch,
):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    captured = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"srh-capture-{uuid4()}"},
    )
    assert captured.status_code == 201, captured.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    with SessionLocal() as session:
        current = session.get(
            Activity, session.get(main_module.Journey, journey_id).current_activity_id
        )
        assert current.place_id == "place-old-town"

    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T09:10:00+08:00",
            "payload": {"reason": "future disruption while earlier activities remain"},
        },
    )
    assert event.status_code == 201, event.text
    calls = 0

    def should_not_run(problem, *, candidate_count, **_kwargs):
        nonlocal calls
        calls += 1
        return _empty_attempt(problem)

    monkeypatch.setattr(main_module, "generate_solver_candidates", should_not_run)
    _generate(journey_id, event.json()["id"], count=3)
    assert calls == 0


def test_srh04_post_gate_rejected_solver_assignment_can_be_rescued_by_same_heuristic_assignment(
    monkeypatch,
):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")
    journey_id, _base, event = _prepare_disruption()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda problem, **_kwargs: _malicious_design_attempt(problem),
    )

    _generate(journey_id, event["id"], count=3)
    proposals = _proposals(journey_id, event["id"])
    assert proposals

    # The solver's Design Library schedule is malicious and must fail the gate,
    # but the existing deterministic Design Library schedule is known-good and
    # should still be eligible as a fallback for the same semantic assignment.
    design = [item for item in proposals if "place-design-library" in item["diff"].get("added", [])]
    assert design
    assert all(
        item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
        for item in design
    )


def test_srh05_heuristic_complement_does_not_claim_post_gate_failure_when_solver_succeeded(
    monkeypatch,
):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "ortools")
    journey_id, _base, event = _prepare_disruption()
    monkeypatch.setattr(
        main_module,
        "generate_solver_candidates",
        lambda problem, **_kwargs: _valid_design_attempt(problem),
    )

    _generate(journey_id, event["id"], count=3)
    proposals = _proposals(journey_id, event["id"])
    solver = [
        item
        for item in proposals
        if item["diff"]["verified_evidence"]["generator"]["kind"] == "ORTOOLS_CP_SAT"
    ]
    heuristic = [
        item
        for item in proposals
        if item["diff"]["verified_evidence"]["generator"]["kind"] == "DETERMINISTIC_HEURISTIC"
    ]
    assert solver and heuristic
    assert all(
        item["diff"]["verified_evidence"]["generator"].get("fallback_reason")
        != "ORTOOLS_POST_GATE_FALLBACK"
        for item in heuristic
    )
