"""ORD-0543 · the template leg may widen, the hours evidence may not.

Commander ruling for this order was option 1: widen the day-sequence leg (`_catalogue_templates`)
rather than register another source. The failure this closes is measured: with the registered
offline source on the line a city holds places (`places_in_catalog` 5) but only one of them states
opening hours, so the leg returned no day sequence at all and every door behind a schedule said
"no feasible candidates" - the same sentence a genuinely empty city says.

Widening is allowed. Inventing is not, so these arms pin four separate things:

  * a place that never states hours can now be drawn, inside a band named on the parameters;
  * a place that *does* state hours is still bounded by its own open/close, and is never displaced
    behind an unsourced stop;
  * the leg still refuses to make a day out of a single stop;
  * widening reads hours, it never writes them - the store's `opening_hours_json` is untouched.

The count of sequences resting on unsourced minutes is a report, not a decoration, so it is checked
in both directions (zero when everything is stated, non-zero when something is not).
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from app.services import _catalogue_templates, _templates_resting_on_unsourced_hours

BAND_START = 9 * 60
BAND_END = 18 * 60


def _place(place_id: str, *, hours: dict | None, duration: int = 60) -> SimpleNamespace:
    """A Place-shaped object: the leg only reads `id`, `name`, `opening_hours_json`, `metadata_json`."""

    return SimpleNamespace(
        id=place_id,
        name=f"地点 {place_id}",
        category="museum",
        opening_hours_json=json.dumps(hours) if hours is not None else None,
        metadata_json=json.dumps({"duration_min": duration}),
    )


def _hours(opened: str, closed: str) -> dict:
    return {"open": opened, "close": closed, "status": "STATED"}


def _hm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


NO_TRAVEL = lambda origin, destination: 0  # noqa: E731 - the leg's travel input is pinned for these arms


def test_a_city_without_stated_hours_now_composes_a_day_sequence() -> None:
    places = {
        pid: _place(pid, hours=None) for pid in ("p1", "p2", "p3")
    }
    templates = _catalogue_templates(places, NO_TRAVEL, day_start_minutes=BAND_START,
                                     day_end_minutes=BAND_END)
    assert templates, "the widened leg still returned nothing for a city with no stated hours"
    sequence = templates[0]
    assert len(sequence) >= 2, sequence
    for place_id, _title, start_text, end_text in sequence:
        assert _hm(start_text) >= BAND_START, sequence
        assert _hm(end_text) <= BAND_END, sequence
        assert _hm(end_text) > _hm(start_text), sequence


def test_a_stated_window_still_bounds_its_own_stop() -> None:
    """The unsourced stops may not push a real window around, and may not be honoured by its hours."""

    late = _place("late", hours=_hours("14:00", "15:30"), duration=45)
    places = {"late": late, "u1": _place("u1", hours=None), "u2": _place("u2", hours=None)}
    templates = _catalogue_templates(places, NO_TRAVEL, day_start_minutes=BAND_START,
                                     day_end_minutes=BAND_END)
    assert templates, templates
    stops = {row[0]: row for row in templates[0]}
    assert "late" in stops, templates[0]
    assert _hm(stops["late"][2]) >= _hm("14:00"), stops["late"]
    assert _hm(stops["late"][3]) <= _hm("15:30"), stops["late"]
    # stated hours come first in the queue: an unsourced stop never takes the leading slot
    assert templates[0][0][0] == "late", templates[0]


def test_one_stop_is_still_not_a_day() -> None:
    places = {"only": _place("only", hours=None)}
    assert _catalogue_templates(places, NO_TRAVEL) == []
    # and an empty catalogue is still an empty answer, widened leg or not
    assert _catalogue_templates({}, NO_TRAVEL) == []


def test_widening_reads_hours_and_never_writes_them() -> None:
    places = {pid: _place(pid, hours=None) for pid in ("a", "b", "c")}
    before = {pid: place.opening_hours_json for pid, place in places.items()}
    assert set(before.values()) == {None}, before
    _catalogue_templates(places, NO_TRAVEL)
    assert {pid: place.opening_hours_json for pid, place in places.items()} == before


def test_a_band_that_closes_before_it_opens_is_not_used() -> None:
    """The fallback band is only usable as a band; an inverted one schedules nothing.

    This pins the guard rather than leaving it as an untested branch: with `day_end_minutes` behind
    `day_start_minutes` there is no interval to place a stop in, so the leg must behave exactly as
    it did before the widening for those places - and a city of only unstated places gets no day,
    which is an honest zero, not a stop squeezed into a window that does not exist.
    """

    places = {pid: _place(pid, hours=None) for pid in ("x", "y", "z")}
    assert _catalogue_templates(places, NO_TRAVEL, day_start_minutes=12 * 60,
                                day_end_minutes=9 * 60) == []
    # positive control: the very same places do compose a day when the band is a band, which is what
    # makes the line above a refusal of the inverted interval rather than an always-empty leg.
    assert _catalogue_templates(places, NO_TRAVEL, day_start_minutes=9 * 60,
                                day_end_minutes=18 * 60), "the same places must compose a day in a sane band"


def test_the_unsourced_count_is_measured_in_both_directions() -> None:
    stated = {"s1": _place("s1", hours=_hours("09:00", "12:00")),
              "s2": _place("s2", hours=_hours("12:30", "17:00"))}
    templates = _catalogue_templates(stated, NO_TRAVEL)
    assert templates, templates
    assert _templates_resting_on_unsourced_hours(templates, stated) == 0

    mixed = {**{pid: _place(pid, hours=None) for pid in ("u1", "u2")}, "s1": stated["s1"]}
    mixed_templates = _catalogue_templates(mixed, NO_TRAVEL)
    assert _templates_resting_on_unsourced_hours(mixed_templates, mixed) > 0, mixed_templates

    none_stated = {pid: _place(pid, hours=None) for pid in ("n1", "n2", "n3")}
    none_templates = _catalogue_templates(none_stated, NO_TRAVEL)
    assert _templates_resting_on_unsourced_hours(none_templates, none_stated) == len(none_templates)
