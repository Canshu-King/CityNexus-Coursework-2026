"""Snapshot K — stay timeline truth regressions.

The stay augmentation is a pinned temporal-constraint calendar
(docs/STAY_TIMELINE_TRUTH.md).  These tests assert the invariants directly on
the calendar, so they fail if anyone reintroduces incremental cursor patching.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.stay_mobility import (
    GraphNode,
    StayItineraryConflict,
    _reschedule_with_carried,
)

TZ = "+08:00"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def node(key, role, start, end, *, locked=False, lat=24.50, lon=118.10) -> GraphNode:
    return GraphNode(
        key=key,
        title=key,
        role=role,
        start_at=start,
        end_at=end,
        latitude=lat,
        longitude=lon,
        place_key=key,
        locked=locked,
    )


def overlaps(a: GraphNode, b: GraphNode) -> bool:
    return not (dt(a.end_at) <= dt(b.start_at) or dt(a.start_at) >= dt(b.end_at))


def _multiday_fixture(days: int = 3, flexible_count: int = 8):
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-0{5 + days}T20:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(),
             (arrival + timedelta(minutes=20)).isoformat(), locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T14:00:00+08:00",
             "2026-11-06T14:20:00+08:00", locked=True),
    ]
    for index in range(days - 1):
        day = 6 + index
        anchors.append(
            node(f"night{index + 1}", "STAY",
                 f"2026-11-0{day}T22:30:00+08:00",
                 f"2026-11-0{day + 1}T06:30:00+08:00", locked=True)
        )
    anchors += [
        node("checkout", "CHECK_OUT", f"2026-11-0{5 + days}T12:00:00+08:00",
             f"2026-11-0{5 + days}T12:20:00+08:00", locked=True),
        node("departure", "DEPARTURE",
             (departure - timedelta(minutes=10)).isoformat(),
             departure.isoformat(), locked=True),
    ]
    starts = [
        "2026-11-06T11:00:00+08:00", "2026-11-06T15:30:00+08:00",
        "2026-11-06T17:00:00+08:00", "2026-11-07T10:00:00+08:00",
        "2026-11-07T13:00:00+08:00", "2026-11-07T16:00:00+08:00",
        "2026-11-08T09:00:00+08:00", "2026-11-08T13:00:00+08:00",
    ][:flexible_count]
    carried = [
        node(f"p{i + 1}", "ACTIVITY", s,
             (dt(s) + timedelta(hours=1)).isoformat(),
             lat=24.49 + i * 0.005, lon=118.12 + i * 0.005)
        for i, s in enumerate(starts)
    ]
    return arrival, departure, anchors, carried


def test_check_in_precedes_first_stay_on_multiday_itinerary():
    arrival, departure, anchors, carried = _multiday_fixture(days=3, flexible_count=8)
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    check_in = next(n for n in out if str(n.role).upper() == "CHECK_IN")
    first_stay = min(
        (n for n in out if str(n.role).upper() == "STAY"),
        key=lambda n: dt(n.start_at),
    )
    assert dt(check_in.end_at) <= dt(first_stay.start_at), (
        f"CHECK_IN occurs after first STAY: {check_in.start_at} vs {first_stay.start_at}"
    )


def test_check_out_follows_last_stay_and_precedes_departure():
    arrival, departure, anchors, carried = _multiday_fixture(days=3, flexible_count=8)
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    check_out = next(n for n in out if str(n.role).upper() == "CHECK_OUT")
    last_stay = max(
        (n for n in out if str(n.role).upper() == "STAY"),
        key=lambda n: dt(n.end_at),
    )
    dep = next(n for n in out if str(n.role).upper() == "DEPARTURE")
    assert dt(check_out.start_at) >= dt(last_stay.end_at)
    assert dt(check_out.end_at) <= dt(dep.start_at)


def test_flexible_activity_never_overlaps_locked_truth():
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T20:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(),
             (arrival + timedelta(minutes=20)).isoformat(), locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T16:00:00+08:00",
             "2026-11-06T16:20:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T19:50:00+08:00",
             departure.isoformat(), locked=True),
    ]
    carried = [
        node("flex", "ACTIVITY", "2026-11-06T11:00:00+08:00",
             "2026-11-06T13:00:00+08:00"),
        node("locked", "ACTIVITY", "2026-11-06T12:00:00+08:00",
             "2026-11-06T13:00:00+08:00", locked=True),
    ]
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    flex = next(n for n in out if n.key == "flex")
    locked = next(n for n in out if n.key == "locked")
    assert not overlaps(flex, locked), (
        f"flexible overlaps locked truth: {flex.start_at}-{flex.end_at} "
        f"vs {locked.start_at}-{locked.end_at}"
    )


def test_locked_activity_timestamp_is_byte_identical():
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T20:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(),
             (arrival + timedelta(minutes=20)).isoformat(), locked=True),
        node("departure", "DEPARTURE", "2026-11-06T19:50:00+08:00",
             departure.isoformat(), locked=True),
    ]
    locked = node("locked", "ACTIVITY", "2026-11-06T12:00:00+08:00",
                  "2026-11-06T13:00:00+08:00", locked=True)
    carried = [locked,
               node("flex", "ACTIVITY", "2026-11-06T11:00:00+08:00",
                    "2026-11-06T12:00:00+08:00")]
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    kept = next(n for n in out if n.key == "locked")
    assert kept.start_at == locked.start_at
    assert kept.end_at == locked.end_at


def test_luggage_drop_survives_and_precedes_check_in():
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-08T20:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(),
             (arrival + timedelta(minutes=20)).isoformat(), locked=True),
        node("luggage", "LUGGAGE_DROP", "2026-11-06T11:00:00+08:00",
             "2026-11-06T11:20:00+08:00", locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T14:00:00+08:00",
             "2026-11-06T14:20:00+08:00", locked=True),
        node("night1", "STAY", "2026-11-06T22:30:00+08:00",
             "2026-11-07T06:30:00+08:00", locked=True),
        node("checkout", "CHECK_OUT", "2026-11-08T12:00:00+08:00",
             "2026-11-08T12:20:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-08T19:50:00+08:00",
             departure.isoformat(), locked=True),
    ]
    carried = [node("p1", "ACTIVITY", "2026-11-06T15:00:00+08:00",
                    "2026-11-06T16:00:00+08:00")]
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    roles = [str(n.role).upper() for n in out]
    assert "LUGGAGE_DROP" in roles
    luggage = next(n for n in out if str(n.role).upper() == "LUGGAGE_DROP")
    check_in = next(n for n in out if str(n.role).upper() == "CHECK_IN")
    assert dt(luggage.end_at) <= dt(check_in.start_at)


def test_all_selected_pois_survive_a_tight_calendar():
    arrival, departure, anchors, carried = _multiday_fixture(days=3, flexible_count=8)
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    kept = {n.key for n in out if str(n.role).upper() == "ACTIVITY"}
    assert kept == {n.key for n in carried}, "a chosen POI was silently dropped"


def test_no_structural_operation_overlaps_another():
    arrival, departure, anchors, carried = _multiday_fixture(days=3, flexible_count=8)
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    structural = [
        n for n in out
        if str(n.role).upper() in {"ARRIVAL", "DEPARTURE", "CHECK_IN",
                                   "CHECK_OUT", "STAY", "LUGGAGE_DROP"}
    ]
    structural.sort(key=lambda n: dt(n.start_at))
    for previous, current in zip(structural, structural[1:]):
        assert dt(previous.end_at) <= dt(current.start_at), (
            f"structural overlap: {previous.key} -> {current.key}"
        )


def test_impossible_calendar_raises_explicit_conflict():
    """A flexible stop that cannot fit must raise, not be deleted."""

    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T12:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(),
             (arrival + timedelta(minutes=20)).isoformat(), locked=True),
        node("locked", "ACTIVITY", "2026-11-06T10:20:00+08:00",
             "2026-11-06T11:40:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T11:50:00+08:00",
             departure.isoformat(), locked=True),
    ]
    carried = [
        node("locked", "ACTIVITY", "2026-11-06T10:20:00+08:00",
             "2026-11-06T11:40:00+08:00", locked=True),
        node("big", "ACTIVITY", "2026-11-06T11:00:00+08:00",
             "2026-11-06T13:00:00+08:00"),
    ]
    with pytest.raises(StayItineraryConflict):
        _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fixture_travel(monkeypatch):
    """Deterministic short transition time for the local fixtures.

    These tests assert structural invariants (survival, ordering, locked truth,
    duration preservation) on co-located fixture coordinates.  Pinning the
    transition cost keeps them independent of the distance heuristic while the
    real travel reachability rule stays fully active in production.
    """

    import app.stay_mobility as _sm

    monkeypatch.setattr(_sm, "_travel_minutes", lambda a, b, journey=None: 5)
