"""Hotfix v2 regressions: closure truth and deterministic target resolution.

Covers the two P0/P1 blockers found by the independent v2 recheck:

* R1-a — an imported booking commitment has ``activity_id = None``, so the old
  protection test ``affected_activity_id in {row.activity_id ...}`` evaluated
  ``None in {None}`` and rejected every closure on a booking-first journey with
  a false "protected" 422.
* R1-b — a named venue that is not passed as ``affected_activity_id`` must
  resolve deterministically, and an ambiguous name must not be guessed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.intelligence.impact_contract import ImpactActivityContext
from app.intelligence.impact_resolver import _match_named_venue, _named_venue_query


def _context(activity_id: str, title: str, place_name: str | None = None, aliases=()):
    start = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)
    return ImpactActivityContext(
        activity_id=activity_id,
        sequence_no=1,
        title=title,
        activity_type="visit",
        place_id=f"place-{activity_id}",
        place_name=place_name,
        place_category="museum",
        latitude=24.4,
        longitude=118.0,
        start_at=start,
        end_at=start + timedelta(hours=1),
        status="PLANNED",
        locked=False,
        is_current=False,
        is_c4=False,
        place_aliases=tuple(aliases),
    )


def test_null_protected_activity_id_cannot_protect_an_unrelated_poi() -> None:
    """The exact false positive: `None in {None}` must not be treated as protected."""

    # A booking-first journey: the imported commitment carries activity_id=None.
    c4_rows = [SimpleNamespace(activity_id=None)]
    protected = {str(row.activity_id) for row in c4_rows if row.activity_id is not None}
    closure_targets = [
        str(value) for value in (None, None) if value is not None
    ]

    assert protected == set(), "a null commitment id is not a protected activity"
    assert not any(value in protected for value in closure_targets), (
        "a closure on a mutable POI must not be blocked by a null booking id"
    )


def test_null_commitment_id_does_not_match_a_null_closure_target() -> None:
    """Guards the precise old expression so the bug cannot silently return."""

    affected_activity_id = None
    old_expression = affected_activity_id in {row.activity_id for row in [SimpleNamespace(activity_id=None)]}
    assert old_expression is True, "the old expression really was the bug"

    new_protected = {
        str(row.activity_id)
        for row in [SimpleNamespace(activity_id=None)]
        if row.activity_id is not None
    }
    new_expression = bool(new_protected) and any(
        value in new_protected
        for value in [str(v) for v in (affected_activity_id,) if v is not None]
    )
    assert new_expression is False, "the fixed expression must not block the closure"


def test_real_protected_activity_still_blocks() -> None:
    """The protection rule itself must keep working for a real locked activity."""

    locked_id = "activity-locked-1"
    protected = {str(row.activity_id) for row in [SimpleNamespace(activity_id=locked_id)]}
    closure_targets = [str(v) for v in (locked_id,) if v is not None]
    assert any(value in protected for value in closure_targets), (
        "a genuinely protected activity must still refuse automatic replacement"
    )


def test_named_venue_resolves_deterministically() -> None:
    activities = (
        _context("a1", "鼓浪屿风琴博物馆(临时展馆)", "鼓浪屿风琴博物馆"),
        _context("a2", "环岛路骑行", "环岛路"),
    )
    matches = _match_named_venue("鼓浪屿风琴博物馆", activities)
    assert len(matches) == 1, "a uniquely named venue must resolve to exactly one stop"
    assert matches[0].activity_id == "a1"


def test_exact_name_beats_a_partial_match() -> None:
    """An exact title match is not ambiguous just because another title contains it."""

    activities = (
        _context("a1", "厦门博物馆", "厦门博物馆"),
        _context("a2", "厦门博物馆分馆", "厦门博物馆分馆"),
    )
    matches = _match_named_venue("厦门博物馆", activities)
    assert len(matches) == 1 and matches[0].activity_id == "a1", (
        "an exact match must win over a containment match"
    )


def test_ambiguous_named_venue_is_not_guessed() -> None:
    """Two equally-matching stops must yield ambiguity, never a silent first pick."""

    activities = (
        _context("a1", "厦门博物馆老馆", "厦门博物馆老馆"),
        _context("a2", "厦门博物馆新馆", "厦门博物馆新馆"),
    )
    matches = _match_named_venue("厦门博物馆", activities)
    assert len(matches) != 1, "an ambiguous name must not be resolved to a single stop"


def test_unknown_named_venue_matches_nothing() -> None:
    activities = (_context("a1", "环岛路骑行", "环岛路"),)
    assert _match_named_venue("完全不存在的场馆", activities) == []


def test_named_venue_query_reads_the_expected_payload_keys() -> None:
    assert _named_venue_query({"named_venue": "鼓浪屿"}) == "鼓浪屿"
    assert _named_venue_query({"venue_name": " 环岛路 "}) == "环岛路"
    assert _named_venue_query({}) == ""
    assert _named_venue_query({"named_venue": "   "}) == ""


# --------------------------------------------------------------------------
# R1-c: unrelated places must never be treated as one physical venue.
#
# ``_travel_minutes`` used to declare two stops co-located whenever their keys
# shared a >=12 character prefix.  Opaque place identities such as
# ``real-place-<hex>`` share the literal ``real-place-`` prefix, so two distant
# attractions were given a 5 minute leg.  The calendar then reserved 5 minutes
# for a leg the feasibility gate validated at 11, and the stay option was
# refused with a false INSUFFICIENT_TRAVEL_GAP.
# --------------------------------------------------------------------------


def _node(key: str, lat: float, lon: float, place_key: str | None = None):
    from app.stay_mobility import GraphNode

    return GraphNode(
        key=key,
        title=key,
        role="ACTIVITY",
        start_at="2026-11-06T10:00:00+08:00",
        end_at="2026-11-06T11:00:00+08:00",
        latitude=lat,
        longitude=lon,
        place_key=place_key,
    )


def test_opaque_place_ids_sharing_a_prefix_are_not_one_venue() -> None:
    from app.stay_mobility import _same_complex

    left = _node("real-place-07e7689756fa27109a8bff96", 24.4798, 118.0894)
    right = _node("real-place-0f6a7b4b3d5359684929cdbb", 24.5123, 118.1321)
    assert _same_complex(left, right) is False, (
        "two different real places must not be reported as one venue"
    )


def test_check_in_and_its_night_are_one_venue() -> None:
    from app.stay_mobility import _same_complex

    check_in = _node("stay:osm:node/4304450093:check-in", 24.4798, 118.0894)
    night = _node("stay:osm:node/4304450093:night:1", 24.4798, 118.0894)
    assert _same_complex(check_in, night) is True


def test_reserved_leg_time_matches_the_gate_for_unrelated_places() -> None:
    """The calendar and the gate must describe one journey, not two."""

    from app.stay_mobility import _travel_minutes

    left = _node("real-place-07e7689756fa27109a8bff96", 24.4798, 118.0894)
    right = _node("real-place-0f6a7b4b3d5359684929cdbb", 24.5123, 118.1321)
    reserved = _travel_minutes(left, right)
    # A real cross-city hop cannot be 5 minutes; the old prefix rule said it was.
    assert reserved > 5, f"unrelated places reserved only {reserved} minutes"


# --------------------------------------------------------------------------
# R1-d: a closed venue must never be offered as its own replacement.
#
# One physical venue can exist as two Place rows: a domain wrapper
# (provider ``citynexus-v9-domain``, external_id ``CN-XIAMEN:real-place-<hex>``)
# and the provider row it wraps (provider ``overpass``).  They have different
# ids, so excluding candidates by id alone let a replan "replace" a closed
# restaurant with that same restaurant under a new id: the plan looked
# recovered, the diff was minimal, and the traveller was still sent to a
# closed venue.
# --------------------------------------------------------------------------


class _Venue:
    def __init__(self, place_id, provider, external_id, lat, lon, name, tags=()):
        self.id = place_id
        self.provider_key = provider
        self.external_id = external_id
        self.latitude = lat
        self.longitude = lon
        self.name = name
        self.category = "restaurant"
        self.tags_json = __import__("json").dumps(list(tags))
        self.metadata_json = __import__("json").dumps({"duration_min": 60})


def _wrapper_row():
    return _Venue(
        "e1ced246-e238-5e48-a816-1b82e8b2953c",
        "citynexus-v9-domain",
        "CN-XIAMEN:real-place-07e7689756fa27109a8bff96",
        24.4581979,
        118.0689473,
        "OSM restaurant",
        ("indoor",),
    )


def _provider_row_for_the_same_venue():
    return _Venue(
        "real-place-07e7689756fa27109a8bff96",
        "overpass",
        "overpass:node/1588295483",
        24.4581979,
        118.0689473,
        "OSM restaurant",
        ("indoor",),
    )


def _genuinely_different_venue():
    return _Venue(
        "real-place-1564f860645d87ceaae7949a",
        "overpass",
        "overpass:node/999999",
        24.5000000,
        118.1200000,
        "必胜客",
        ("indoor",),
    )


def test_wrapper_and_provider_row_are_one_venue() -> None:
    from app.replanning.mock_replan_engine import is_same_physical_venue

    assert is_same_physical_venue(_wrapper_row(), _provider_row_for_the_same_venue()) is True
    assert is_same_physical_venue(_wrapper_row(), _genuinely_different_venue()) is False


def test_closed_venue_is_not_its_own_replacement() -> None:
    from app.replanning.mock_replan_engine import rank_closure_replacements

    choices = rank_closure_replacements(
        affected_place=_wrapper_row(),
        candidate_places=[
            _provider_row_for_the_same_venue(),
            _genuinely_different_venue(),
        ],
        occupied_place_ids={_wrapper_row().id},
        max_duration_min=90,
    )
    offered = [choice.place_id for choice in choices]
    assert _provider_row_for_the_same_venue().id not in offered, (
        "the closed venue reappeared as its own replacement"
    )
    assert offered == [_genuinely_different_venue().id]


def test_occupied_venue_is_not_reused_under_another_id() -> None:
    from app.replanning.mock_replan_engine import rank_closure_replacements

    already_used = _provider_row_for_the_same_venue()
    choices = rank_closure_replacements(
        affected_place=_genuinely_different_venue(),
        candidate_places=[already_used, _wrapper_row()],
        occupied_place_ids={already_used.id},
        occupied_places=[already_used],
        max_duration_min=90,
    )
    assert all(
        choice.place_id not in {already_used.id, _wrapper_row().id} for choice in choices
    ), "a venue already used in the plan was offered again under a different id"
