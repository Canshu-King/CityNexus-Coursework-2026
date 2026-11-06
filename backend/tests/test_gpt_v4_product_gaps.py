from importlib.util import find_spec
from types import SimpleNamespace

import pytest


def test_v4_01_mission_budget_understands_chinese_numerals(monkeypatch):
    from app.agent import mission

    monkeypatch.setattr(mission, "_resolve_protected_activities", lambda *_args, **_kwargs: ())
    spec = mission.compile_mission(
        None,
        object(),
        "海河那边有点堵，最多多花五十元，你自己处理。",
        requested_autonomy="AUTONOMOUS",
    )
    assert spec.max_cost_delta_minor == 5000


def test_v4_02_protection_parser_respects_user_negation():
    from app.agent import mission

    activities = [
        SimpleNamespace(
            id="act-cruise",
            place_id="place-river-cruise",
            city_service_id=None,
            title="River Cruise",
        ),
        SimpleNamespace(
            id="act-dinner",
            place_id="place-skyline-dinner",
            city_service_id=None,
            title="Dinner",
        ),
    ]
    places = [
        SimpleNamespace(id="place-river-cruise", name="Haihe River Cruise"),
        SimpleNamespace(id="place-skyline-dinner", name="Skyline Dinner"),
    ]

    class _Rows:
        def all(self):
            return places

    class _Session:
        def scalars(self, _statement):
            return _Rows()

    original = mission._active_activities
    try:
        mission._active_activities = lambda *_args, **_kwargs: activities
        protected = mission._resolve_protected_activities(
            _Session(),
            object(),
            "游船可以取消，晚餐别动。",
        )
    finally:
        mission._active_activities = original

    assert "act-cruise" not in protected
    assert "act-dinner" in protected


@pytest.mark.skipif(
    find_spec("ortools") is None,
    reason="OR-Tools is required for the V4 CP-SAT gate",
)
def test_v4_03_solver_v2_is_real_cp_sat_not_enumeration():
    from app.planning.solver_v2 import (
        SolverV2Option,
        SolverV2Problem,
        SolverV2Slot,
        solve_v2,
    )

    slot = SolverV2Slot(
        slot_key="slot-1",
        sequence_no=1,
        original_location_key="A",
        original_start_min=30,
        original_end_min=60,
        original_cost_minor=0,
        options=(
            SolverV2Option(
                option_key="keep-A",
                location_key="A",
                action="KEEP",
                duration_min=30,
                cost_minor=0,
                earliest_start_min=0,
                latest_end_min=120,
                original_start_min=30,
                original_cost_minor=0,
            ),
            SolverV2Option(
                option_key="replace-B",
                location_key="B",
                action="REPLACE",
                duration_min=30,
                cost_minor=0,
                earliest_start_min=0,
                latest_end_min=120,
                original_start_min=30,
                original_cost_minor=0,
            ),
        ),
    )
    problem = SolverV2Problem(
        journey_id="j",
        operation_key="op",
        now_min=0,
        journey_end_min=180,
        budget_ceiling_minor=1000,
        base_cost_minor=0,
        current_location_key="A",
        slots=(slot,),
        protected_anchors=(),
        frozen_mobility_snapshot_id="snap",
        travel_minutes={("A", "B"): 5, ("B", "A"): 5},
        top_k=2,
    )
    plans = solve_v2(problem)
    assert plans
    assert plans[0].generator_kind in {"ORTOOLS_CP_SAT_V2", "ORTOOLS_CP_SAT"}
