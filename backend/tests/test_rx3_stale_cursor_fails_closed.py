"""Recovery gate RX-3 — a present-but-unknown execution cursor must fail closed.

``_execution_truth`` in ``backend/app/operations/v9.py`` is the single NOW/NEXT
projection shared by Today, Live and the Edge companion.  Before CX-REC-001 it
treated two different situations as one:

  * ``current_activity_id is None``      -> legitimate pre-execution state, the
    first pending activity may be projected as Now;
  * ``current_activity_id`` set but not matching any activity (e.g. it still
    points at an activity of a superseded plan version) -> corrupt/stale state,
    which must NOT be turned into a fabricated Now.

The old code fell into the same branch for both, so a stale cursor silently
resumed projection from the first pending activity and every client above it
kept that invention as truth.  This is the same defect class the mission calls
out for ``activities[0]`` laundering, one layer down.

The gate is deliberately at the projection function itself: it is the shared
source, and a unit gate here means no caller has to re-derive the rule.
"""

from __future__ import annotations

from app.db.models import Activity, JourneyLeg
from app.operations.v9 import _execution_truth

_PLAN = "plan-v1"


def _activity(index: int, *, status: str = "PENDING") -> Activity:
    return Activity(
        id=f"act-{index}",
        plan_version_id=_PLAN,
        sequence_no=index,
        activity_type="SIGHTSEE",
        title=f"Activity {index}",
        start_at=f"2026-10-10T0{9 + index}:00:00+08:00",
        end_at=f"2026-10-10T0{10 + index}:00:00+08:00",
        travel_from_prev_min=10,
        estimated_cost_minor=0,
        currency="CNY",
        status=status,
        locked=False,
        metadata_json="{}",
        created_at="2026-10-10T00:00:00+08:00",
        updated_at="2026-10-10T00:00:00+08:00",
    )


def _leg(index: int) -> JourneyLeg:
    return JourneyLeg(
        id=f"leg-{index}",
        journey_id="j1",
        plan_version_id=_PLAN,
        sequence_no=index,
        from_key=f"place-{index}",
        to_key=f"place-{index + 1}",
        mode="walk",
        depart_at="2026-10-10T09:00:00+08:00",
        arrive_at="2026-10-10T09:20:00+08:00",
        duration_min=15 + index,
        distance_m=900,
        cost_status="UNKNOWN",
        walking_min=15,
        transfers=0,
        reliability_score=0.0,
    )


def _activities() -> list[Activity]:
    return [_activity(0), _activity(1), _activity(2)]


def _legs() -> list[JourneyLeg]:
    return [_leg(0), _leg(1)]


def test_absent_cursor_may_project_the_first_pending_activity() -> None:
    """Documented allowance: pre-execution has no cursor yet."""
    projection = _execution_truth(_activities(), _legs(), None)
    assert projection["now_activity_id"] == "act-0"
    assert projection["next_activity_id"] == "act-1"
    assert projection["next_mode"] == "walk"
    assert projection["next_eta_min"] == 15
    assert projection["cursor_status"] == "ABSENT_FALLBACK"


def test_matching_cursor_projects_that_activity_and_its_successor() -> None:
    projection = _execution_truth(_activities(), _legs(), "act-1")
    assert projection["now_activity_id"] == "act-1"
    assert projection["next_activity_id"] == "act-2"
    assert projection["next_eta_min"] == 16
    assert projection["cursor_status"] == "VALID"


def test_final_activity_has_no_fabricated_next() -> None:
    projection = _execution_truth(_activities(), _legs(), "act-2")
    assert projection["now_activity_id"] == "act-2"
    assert projection["next_activity_id"] is None


def test_stale_cursor_fails_closed_instead_of_resuming_from_the_first_activity() -> None:
    """The hole: a cursor pointing at another plan version's activity.

    Under the pre-CX-REC-001 code this projected ``act-0`` as Now, which is the
    fabricated-truth behaviour every downstream surface would then display.
    """
    projection = _execution_truth(_activities(), _legs(), "act-from-superseded-plan")
    assert projection["now_activity_id"] is None, (
        "a present-but-unknown cursor was resolved into a fabricated Now: "
        f"{projection['now_activity_id']}"
    )
    assert projection["next_activity_id"] is None
    assert projection["next_mode"] == "NONE", "a stale cursor still produced a Next leg"
    assert projection["next_eta_min"] == 0
    assert projection["cursor_status"] == "STALE"


def test_completed_prefix_does_not_hide_a_stale_cursor() -> None:
    """With everything before the cursor finished, the old fallback would have
    picked the only pending activity and looked perfectly plausible."""
    activities = [
        _activity(0, status="COMPLETED"),
        _activity(1, status="COMPLETED"),
        _activity(2),
    ]
    projection = _execution_truth(activities, _legs(), "act-from-superseded-plan")
    assert projection["now_activity_id"] is None
    assert projection["cursor_status"] == "STALE"
