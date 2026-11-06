"""ORD-0001: the Next leg is bound to a trustworthy Now, never to an index.

``_execution_truth`` used to pick the leg with ``legs[current_index]``.  Two legal
states push ``current_index`` to ``len(activities)`` - a stale cursor, and a
journey whose activities are all terminal - and in any plan that carries more legs
than activities (arrival/departure transfers do exactly that) the guard
``0 <= current_index < len(legs)`` is satisfied by an index that is past the end of
the *activities*.  The result was a projection with no Now, no Next, and a live
NextLeg: mode, ETA and leg id for a step the traveller is not standing at.

Mission 002 A says a stale projection must be empty in all three slots.  This file
pins the leg to the same evidence the rest of the projection needs - a resolved
Now - and keeps the two shapes that are allowed to show a leg:

  * a valid cursor with a successor (the leg is how you get there);
  * a valid cursor on the final activity (the leg is the transfer out of where the
    traveller actually is; there is no Next activity, and this is the behaviour the
    order prescribes, so it is pinned rather than left implicit).
"""

from __future__ import annotations

import pytest

from app.db.models import Activity, JourneyLeg
from app.operations.v9 import _execution_truth

_PLAN = "plan-v1"
# One leg more than activities: 3 stops, 4 transfers (arrival + 3 hops), which is
# the shape that used to leak.  Legs are indexable one past the last activity.
_ACTIVITY_COUNT = 3
_LEG_COUNT = 4


def _activity(index: int, *, status: str = "PENDING") -> Activity:
    return Activity(
        id=f"act-{index}",
        plan_version_id=_PLAN,
        sequence_no=index,
        activity_type="SIGHTSEE",
        title=f"Activity {index}",
        start_at=f"2026-10-10T{9 + index:02d}:00:00+08:00",
        end_at=f"2026-10-10T{10 + index:02d}:00:00+08:00",
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
        mode="AUTO" if index >= _ACTIVITY_COUNT else "walk",
        depart_at="2026-10-10T09:00:00+08:00",
        arrive_at="2026-10-10T09:20:00+08:00",
        duration_min=20 + index,
        distance_m=900,
        cost_status="UNKNOWN",
        walking_min=15,
        transfers=0,
        reliability_score=0.0,
    )


def _activities(*, all_terminal: bool = False) -> list[Activity]:
    status = "COMPLETED" if all_terminal else "PENDING"
    return [_activity(index, status=status) for index in range(_ACTIVITY_COUNT)]


def _legs() -> list[JourneyLeg]:
    return [_leg(index) for index in range(_LEG_COUNT)]


@pytest.mark.parametrize(
    "cursor,expected_status",
    [("act-from-superseded-plan", "STALE"), (None, "ABSENT_FALLBACK")],
)
def test_no_trustworthy_now_projects_no_next_leg(cursor: str, expected_status: str) -> None:
    """(a) legs outnumber activities and there is no Now: every leg slot stays empty.

    ``None`` here is the finished-journey shape - the absent cursor falls back to the
    first pending activity, finds none, and lands past the end just like a stale
    cursor does.  Both used to hand out ``leg-3`` with mode AUTO and a real ETA.
    """
    activities = _activities(all_terminal=cursor is None)
    projection = _execution_truth(activities, _legs(), cursor)

    assert projection["cursor_status"] == expected_status
    assert projection["now_activity_id"] is None, (
        "the premise of this case is that nothing is a trustworthy Now: "
        f"{projection['now_activity_id']}"
    )
    assert projection["next_activity_id"] is None
    assert len(_legs()) > len(activities), (
        "the fixture stopped being the leaking shape; this case measures nothing"
    )
    assert projection["next_leg_id"] is None, (
        "a NextLeg was projected without a Now: "
        f"{projection['next_leg_id']} / {projection['next_mode']} / "
        f"{projection['next_eta_min']}"
    )
    assert projection["next_mode"] == "NONE"
    assert projection["next_eta_min"] == 0


def test_a_valid_cursor_with_a_successor_carries_its_next_state() -> None:
    """(b) VALID cursor plus a non-empty Next activity means Next has a state.

    The Edge and v10 surfaces read ``next_state`` to decide what to render; a
    projection that names a next activity without saying what state it is in gives
    them nothing to be correct with.
    """
    projection = _execution_truth(_activities(), _legs(), "act-1")

    assert projection["cursor_status"] == "VALID"
    assert projection["next_activity_id"] == "act-2"
    assert projection["next_state"], "a projected Next activity must carry its state"
    assert projection["next_state"] == "PENDING"
    assert projection["next_leg_id"] == "leg-1"
    assert projection["next_mode"] == "walk"
    assert projection["next_eta_min"] == 21


def test_the_final_activity_keeps_the_transfer_out_of_it_and_no_next_activity() -> None:
    """Pinned behaviour, not a new rule: Now is trustworthy, so its leg is real.

    The traveller is standing at the last stop and ``leg-2`` is the transfer away
    from it, while there is genuinely no next activity.  This is what the order's
    binding condition (a trustworthy Now) produces, so it is recorded here instead
    of being left to chance - if the product later decides a NextLeg requires a
    Next, this is the node that has to change on purpose.
    """
    projection = _execution_truth(_activities(), _legs(), "act-2")

    assert projection["cursor_status"] == "VALID"
    assert projection["now_activity_id"] == "act-2"
    assert projection["next_activity_id"] is None
    assert projection["next_state"] is None
    assert projection["next_leg_id"] == "leg-2"
    assert projection["next_mode"] == "walk"
    assert projection["next_eta_min"] == 22

# --------------------------------------------------------------------------
# ORD-0011 (1): the structural relationship between the three leg fields
# --------------------------------------------------------------------------
# The precise STALE tuple is already pinned by whole-dict equality above, so repeating
# those four assertions here would add no coverage.  What was unowned is the *shape*
# relationship, which has to hold in every state and in every plan shape:
#
#     next_leg_id is None   <=>   (next_mode, next_eta_min) == ("NONE", 0)
#
# A projection may say "no leg" in three legal shapes today - a plan with no legs at all,
# one leg per hop, and a plan with more legs than activities (the `ON DELETE SET NULL`
# state ORD-0006 had to construct) - and it has to say it under five cursor states and
# three completion patterns.  The defect owned here is the mixed encoding: a null leg id
# beside a mode or an ETA that still describes travel. `("NONE", 17)` or `(null, "AUTO", 0)`
# is what a client renders as a spinner for a trip nobody promised.
_SHAPE_PLANS = {
    "zero-leg plan": (3, 0),
    "one leg per hop": (3, 2),
    "more legs than activities": (9, 10),
}


def _sweep(shape: str):
    """Every (completion pattern x cursor state) combination for one plan shape."""
    activity_count, leg_count = _SHAPE_PLANS[shape]
    legs = [_leg(index) for index in range(leg_count)]
    patterns = {
        "all pending": lambda index: "PENDING",
        "all completed": lambda index: "COMPLETED",
        "prefix completed": lambda index: (
            "COMPLETED" if index < activity_count // 2 else "PENDING"
        ),
    }
    for pattern, status_of in patterns.items():
        activities = [
            _activity(index, status=status_of(index)) for index in range(activity_count)
        ]
        cursors: list[str | None] = [
            None,
            "act-from-superseded-plan",
            activities[0].id if activities else None,
            activities[-1].id if activities else None,
        ]
        for cursor in dict.fromkeys(cursors):
            yield pattern, cursor, _execution_truth(activities, legs, cursor)


@pytest.mark.parametrize("shape", sorted(_SHAPE_PLANS))
def test_the_three_leg_fields_are_absent_or_present_together(shape: str) -> None:
    combinations = list(_sweep(shape))
    assert len(combinations) >= 8, f"{shape}: only {len(combinations)} combinations swept"

    for pattern, cursor, projection in combinations:
        missing = projection["next_leg_id"] is None
        sentinel = (projection["next_mode"], projection["next_eta_min"]) == ("NONE", 0)
        assert missing == sentinel, (
            f"mixed absence encoding in shape {shape!r} / {pattern} / cursor={cursor!r}: "
            f"next_leg_id={projection['next_leg_id']!r} but "
            f"mode={projection['next_mode']!r} eta={projection['next_eta_min']!r}"
        )
        assert projection["cursor_status"] in {"VALID", "STALE", "ABSENT_FALLBACK"}
        if projection["now_activity_id"] is None:
            assert projection["next_leg_id"] is None, (
                f"a leg was projected from a position nobody is standing at "
                f"({shape} / {pattern} / cursor={cursor!r})"
            )


def test_a_projected_leg_is_always_one_of_this_plan_s_legs() -> None:
    """The same invariant read positively.

    A shown mode and ETA must name the leg they came from: a duration with no identity
    cannot be refreshed, re-checked or explained, and a client displaying it has no way to
    learn it was invented.
    """
    activities, legs = (
        [_activity(index) for index in range(9)],
        [_leg(index) for index in range(10)],
    )
    named = [item.id for item in legs]
    for cursor in [None, activities[0].id, activities[4].id, activities[-1].id]:
        projection = _execution_truth(activities, legs, cursor)
        if projection["next_mode"] != "NONE" or projection["next_eta_min"] != 0:
            assert projection["next_leg_id"], (
                "a leg was described without naming it: "
                f"{projection['next_mode']} / {projection['next_eta_min']} at cursor {cursor}"
            )
            assert projection["next_leg_id"] in named, (
                f"the projection named {projection['next_leg_id']}, which is not one of this "
                f"plan's {len(legs)} legs"
            )
