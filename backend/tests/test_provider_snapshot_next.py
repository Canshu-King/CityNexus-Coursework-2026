from __future__ import annotations

from app.replanning.feasibility import FeasibilityActivity, build_context, evaluate_feasibility
from app.replanning.provider_snapshots import MobilitySnapshot


class LiveRouter:
    provider_key = "valhalla-live"

    def estimate_minutes(self, _a, _b):
        return 10


def _journey():
    return {
        "id": "j",
        "city_code": "C",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T12:00:00+08:00",
        "budget_amount_minor": 1000,
        "budget_currency": "CNY",
    }


def _places():
    return {
        "a": {"id": "a", "city_code": "C", "opening_hours": {"open": "08:00", "close": "20:00"}},
        "b": {"id": "b", "city_code": "C", "opening_hours": {"open": "08:00", "close": "20:00"}},
    }


def _activities():
    from datetime import datetime

    return [
        FeasibilityActivity(
            "a",
            datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            datetime.fromisoformat("2026-10-10T09:30:00+08:00"),
            0,
            place_id="a",
            currency="CNY",
        ),
        FeasibilityActivity(
            "b",
            datetime.fromisoformat("2026-10-10T09:45:00+08:00"),
            datetime.fromisoformat("2026-10-10T10:15:00+08:00"),
            0,
            place_id="b",
            currency="CNY",
        ),
    ]


def test_live_router_is_not_called_inside_authoritative_gate():
    gate = evaluate_feasibility(
        build_context(
            _journey(),
            [],
            _places(),
            {},
            mobility_provider=LiveRouter(),
            plan_currency="CNY",
        ),
        _activities(),
    )
    assert "NON_DETERMINISTIC_MOBILITY_PROVIDER" in gate.failures


def test_frozen_real_provider_snapshot_is_accepted_by_gate():
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="valhalla",
        minutes={("a", "b"): 10},
        captured_at="2026-10-10T08:59:00+08:00",
    )
    gate = evaluate_feasibility(
        build_context(
            _journey(),
            [],
            _places(),
            {},
            mobility_provider=snapshot,
            plan_currency="CNY",
        ),
        _activities(),
    )
    assert gate.passed, gate.failures
