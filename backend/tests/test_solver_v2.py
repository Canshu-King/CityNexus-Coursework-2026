from __future__ import annotations

from app.planning.solver_v2 import (
    SolverV2Anchor,
    SolverV2Option,
    SolverV2Problem,
    SolverV2Slot,
    solve_v2,
)


def _problem(*, budget: int = 1_000, top_k: int = 4) -> SolverV2Problem:
    keep_one = SolverV2Option(
        option_key="keep-one",
        location_key="place-one",
        action="KEEP",
        duration_min=60,
        cost_minor=100,
        earliest_start_min=0,
        latest_end_min=180,
        original_start_min=60,
        original_cost_minor=100,
        preference_milli=200,
    )
    replace_one = SolverV2Option(
        option_key="replace-one",
        location_key="place-one-indoor",
        action="REPLACE",
        duration_min=45,
        cost_minor=120,
        earliest_start_min=0,
        latest_end_min=180,
        original_start_min=60,
        original_cost_minor=100,
        preference_milli=900,
    )
    keep_two = SolverV2Option(
        option_key="keep-two",
        location_key="place-two",
        action="KEEP",
        duration_min=60,
        cost_minor=100,
        earliest_start_min=120,
        latest_end_min=285,
        original_start_min=180,
        original_cost_minor=100,
        preference_milli=100,
    )
    replace_two = SolverV2Option(
        option_key="replace-two",
        location_key="place-two-indoor",
        action="REPLACE",
        duration_min=45,
        cost_minor=140,
        earliest_start_min=120,
        latest_end_min=285,
        original_start_min=180,
        original_cost_minor=100,
        preference_milli=850,
    )
    locations = {
        "start",
        "place-one",
        "place-one-indoor",
        "place-two",
        "place-two-indoor",
        "protected-cruise",
    }
    travel = {
        (origin, destination): (0 if origin == destination else 5)
        for origin in locations
        for destination in locations
    }
    return SolverV2Problem(
        journey_id="journey-v2",
        operation_key="replan:v2",
        now_min=0,
        journey_end_min=480,
        budget_ceiling_minor=budget,
        base_cost_minor=0,
        current_location_key="start",
        slots=(
            SolverV2Slot(
                slot_key="activity-one",
                sequence_no=1,
                original_location_key="place-one",
                original_start_min=60,
                original_end_min=120,
                original_cost_minor=100,
                options=(keep_one, replace_one),
                must_change=False,
            ),
            SolverV2Slot(
                slot_key="activity-two",
                sequence_no=2,
                original_location_key="place-two",
                original_start_min=180,
                original_end_min=240,
                original_cost_minor=100,
                options=(keep_two, replace_two),
                must_change=False,
            ),
        ),
        protected_anchors=(
            SolverV2Anchor(
                key="cruise",
                location_key="protected-cruise",
                start_min=300,
                duration_min=60,
            ),
        ),
        frozen_mobility_snapshot_id="snapshot-v2",
        travel_minutes=travel,
        safety_buffer_min=15,
        top_k=top_k,
    )


def test_solver_v2_returns_deterministic_top_k_for_multiple_slots():
    problem = _problem()
    first = solve_v2(problem)
    second = solve_v2(problem)

    assert first
    assert [item.fingerprint for item in first] == [item.fingerprint for item in second]
    assert first[0].protected_anchors[0].start_min == 300
    assert first[0].protected_anchors[0].end_min == 360
    assert all(
        item.start_min is None or item.end_min is not None and item.end_min <= 285
        for item in first[0].assignments
    )


def test_solver_v2_can_change_multiple_mutable_slots_and_keeps_anchor_fixed():
    problem = _problem()
    slots = tuple(
        SolverV2Slot(
            slot_key=slot.slot_key,
            sequence_no=slot.sequence_no,
            original_location_key=slot.original_location_key,
            original_start_min=slot.original_start_min,
            original_end_min=slot.original_end_min,
            original_cost_minor=slot.original_cost_minor,
            options=slot.options,
            must_change=True,
            allow_drop=False,
        )
        for slot in problem.slots
    )
    plans = solve_v2(
        SolverV2Problem(
            **{
                **problem.__dict__,
                "slots": slots,
            }
        )
    )
    assert plans
    assert all(item.action == "REPLACE" for item in plans[0].assignments)
    assert plans[0].changed_activity_count == 2
    assert plans[0].protected_anchors[0].key == "cruise"


def test_solver_v2_enforces_opening_window_budget_and_frozen_travel():
    problem = _problem(budget=250)
    expensive = SolverV2Option(
        option_key="expensive",
        location_key="place-expensive",
        action="REPLACE",
        duration_min=30,
        cost_minor=900,
        earliest_start_min=0,
        latest_end_min=90,
        original_start_min=60,
        original_cost_minor=100,
    )
    slots = (
        SolverV2Slot(
            slot_key="activity-one",
            sequence_no=1,
            original_location_key="place-one",
            original_start_min=60,
            original_end_min=120,
            original_cost_minor=100,
            options=(expensive,),
            must_change=True,
            allow_drop=False,
        ),
    )
    restricted = SolverV2Problem(
        **{
            **problem.__dict__,
            "slots": slots,
        }
    )
    assert solve_v2(restricted) == ()


def test_solver_v2_risk_penalty_has_priority_over_unverified_preference():
    problem = _problem(top_k=1)
    low_risk = SolverV2Option(
        option_key="low-risk",
        location_key="place-low-risk",
        action="REPLACE",
        duration_min=45,
        cost_minor=100,
        earliest_start_min=0,
        latest_end_min=180,
        original_start_min=60,
        original_cost_minor=100,
        preference_milli=100,
        risk_penalty_milli=0,
    )
    high_risk = SolverV2Option(
        option_key="high-risk",
        location_key="place-high-risk",
        action="REPLACE",
        duration_min=45,
        cost_minor=100,
        earliest_start_min=0,
        latest_end_min=180,
        original_start_min=60,
        original_cost_minor=100,
        preference_milli=1000,
        risk_penalty_milli=900,
    )
    slot = SolverV2Slot(
        slot_key="activity-one",
        sequence_no=1,
        original_location_key="place-one",
        original_start_min=60,
        original_end_min=120,
        original_cost_minor=100,
        options=(low_risk, high_risk),
        must_change=True,
        allow_drop=False,
    )
    locations = set(problem.travel_minutes)
    locations.update({("start", "place-low-risk"), ("place-low-risk", "protected-cruise")})
    locations.update({("start", "place-high-risk"), ("place-high-risk", "protected-cruise")})
    travel = dict(problem.travel_minutes)
    for origin, destination in locations:
        travel.setdefault((origin, destination), 5)
    result = solve_v2(
        SolverV2Problem(**{**problem.__dict__, "slots": (slot,), "travel_minutes": travel})
    )
    assert result[0].assignments[0].option_key == "low-risk"
