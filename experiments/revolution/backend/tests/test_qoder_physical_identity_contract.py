"""T1 — physical venue identity contract.

The predecessor predicate was `same id OR rounded coordinates equal OR token
intersection`.  Each disjunct can produce a false positive:

* ``str(None) == str(None)`` makes two id-less rows identical;
* two businesses in one building share a geocoder centroid;
* ``overpass:node/123`` and ``other:way/123`` share the bare number ``123``.

Identity must be proven by structure.  Coordinates are supporting evidence and
may only corroborate a matching name and compatible category.

Contract: docs/PHYSICAL_IDENTITY_CONTRACT.md
"""

from __future__ import annotations

import json

from app.replanning.mock_replan_engine import (
    AMBIGUOUS,
    DIFFERENT,
    SAME,
    compare_physical_venue,
    is_same_physical_venue,
    physical_venue_identity,
    rank_closure_replacements,
    should_exclude_replacement_candidate,
)

# Coordinates of the real Xiamen wrapper/provider pair this contract came from.
XIAMEN_LAT = 24.4581979
XIAMEN_LON = 118.0689473


class _Place:
    """Minimal Place stand-in carrying only the identity-bearing fields."""

    def __init__(
        self,
        place_id,
        *,
        provider_key="overpass",
        external_id=None,
        lat=None,
        lon=None,
        name=None,
        category=None,
        tags=(),
        duration_min=60,
        cost_minor=0,
    ):
        self.id = place_id
        self.provider_key = provider_key
        self.external_id = external_id
        self.latitude = lat
        self.longitude = lon
        self.name = name
        self.category = category
        self.tags_json = json.dumps(list(tags))
        self.metadata_json = json.dumps({"duration_min": duration_min, "cost_minor": cost_minor})


def _wrapper():
    """The domain wrapper row that names the provider row it wraps."""

    return _Place(
        "e1ced246-e238-5e48-a816-1b82e8b2953c",
        provider_key="citynexus-v9-domain",
        external_id="CN-XIAMEN:real-place-07e7689756fa27109a8bff96",
        lat=XIAMEN_LAT,
        lon=XIAMEN_LON,
        name="OSM restaurant",
        category="restaurant",
        tags=("indoor",),
    )


def _wrapped_provider_row():
    """The provider row the wrapper explicitly references."""

    return _Place(
        "real-place-07e7689756fa27109a8bff96",
        provider_key="overpass",
        external_id="overpass:node/1588295483",
        lat=XIAMEN_LAT,
        lon=XIAMEN_LON,
        name="OSM restaurant",
        category="restaurant",
        tags=("indoor",),
    )


def _different_venue():
    return _Place(
        "real-place-1564f860645d87ceaae7949a",
        provider_key="overpass",
        external_id="overpass:node/999999",
        lat=24.5,
        lon=118.12,
        name="必胜客",
        category="restaurant",
        tags=("indoor",),
    )


# --------------------------------------------------------------------------
# Required regressions 1-9 (docs/TEST_MATRIX.md, docs/PHYSICAL_IDENTITY_CONTRACT.md)
# --------------------------------------------------------------------------


def test_regression_01_two_null_ids_are_not_the_same_venue() -> None:
    """`str(None) == str(None)` must never establish identity.

    Two id-less rows with no corroborating evidence at all are not the same
    venue.  (A shared coordinate *plus* an identical name is a different case
    and is covered separately below.)
    """

    left = _Place(None, external_id=None, name="A")
    right = _Place(None, external_id=None, name="B")
    assert compare_physical_venue(left, right) == DIFFERENT
    assert is_same_physical_venue(left, right) is False


def test_null_ids_with_shared_coordinate_alone_are_still_different() -> None:
    """Null ids never become identity through coordinates on their own."""

    left = _Place(None, external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name=None)
    right = _Place(None, external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name=None)
    assert compare_physical_venue(left, right) == DIFFERENT


def test_regression_01b_empty_string_ids_are_not_the_same_venue() -> None:
    left = _Place("", external_id=None, name="A")
    right = _Place("   ", external_id=None, name="A")
    assert compare_physical_venue(left, right) == DIFFERENT


def test_regression_02_identical_non_empty_row_id_is_same() -> None:
    left = _Place("place-1", external_id=None, name="A")
    right = _Place("place-1", external_id=None, name="A")
    assert compare_physical_venue(left, right) == SAME


def test_regression_03_explicit_wrapper_reference_is_same() -> None:
    """The wrapper names the provider row; that relation is structural."""

    assert compare_physical_venue(_wrapper(), _wrapped_provider_row()) == SAME
    # Order must not matter.
    assert compare_physical_venue(_wrapped_provider_row(), _wrapper()) == SAME


def test_regression_04_same_coordinates_different_names_are_not_same() -> None:
    left = _Place("a", external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name="咖啡店")
    right = _Place("b", external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name="理发店")
    assert compare_physical_venue(left, right) == DIFFERENT


def test_regression_05_same_building_different_shops_are_not_same() -> None:
    """One building centroid routinely holds several distinct venues."""

    shop_a = _Place(
        "shop-a", external_id=None, lat=24.4798, lon=118.0894, name="三楼川菜", category="restaurant"
    )
    shop_b = _Place(
        "shop-b", external_id=None, lat=24.4798, lon=118.0894, name="一楼便利店", category="shop"
    )
    assert compare_physical_venue(shop_a, shop_b) == DIFFERENT


def test_regression_06_osm_node_and_way_with_same_number_are_not_same() -> None:
    """`node/123` and `way/123` are different OSM objects."""

    node = _Place("x", external_id="overpass:node/12345678", lat=1.0, lon=1.0, name="X")
    way = _Place("y", external_id="overpass:way/12345678", lat=9.0, lon=9.0, name="Y")
    assert compare_physical_venue(node, way) == DIFFERENT


def test_regression_07_same_number_across_providers_is_not_same() -> None:
    overpass = _Place("x", external_id="overpass:node/12345678", lat=1.0, lon=1.0, name="X")
    other = _Place("y", external_id="otherprovider:node/12345678", lat=9.0, lon=9.0, name="Y")
    assert compare_physical_venue(overpass, other) == DIFFERENT


def test_regression_08_provider_scoped_identity_survives_coordinate_drift() -> None:
    """Same provider tuple with small drift is still the same venue."""

    left = _Place("a", external_id="overpass:node/1588295483", lat=XIAMEN_LAT, lon=XIAMEN_LON, name="OSM restaurant")
    right = _Place(
        "b",
        external_id="overpass:node/1588295483",
        lat=XIAMEN_LAT + 0.0004,
        lon=XIAMEN_LON + 0.0004,
        name="OSM restaurant",
    )
    assert compare_physical_venue(left, right) == SAME


def test_regression_09_cross_reference_with_coordinate_drift_is_same() -> None:
    drifted = _wrapped_provider_row()
    drifted.latitude = XIAMEN_LAT + 0.0009
    drifted.longitude = XIAMEN_LON + 0.0009
    assert compare_physical_venue(_wrapper(), drifted) == SAME


def test_coordinate_only_evidence_is_never_enough() -> None:
    """Regression 4 of the contract: coordinates alone are not identity."""

    left = _Place("a", external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name=None)
    right = _Place("b", external_id=None, lat=XIAMEN_LAT, lon=XIAMEN_LON, name=None)
    assert compare_physical_venue(left, right) == DIFFERENT


def test_same_coordinate_same_name_incompatible_category_is_ambiguous() -> None:
    """Ambiguity must be expressible so callers can fail safe."""

    left = _Place("a", external_id=None, lat=1.0, lon=1.0, name="中心", category="restaurant")
    right = _Place("b", external_id=None, lat=1.0, lon=1.0, name="中心", category="museum")
    assert compare_physical_venue(left, right) == AMBIGUOUS
    # V2-T1: identity truth is SAME-only; AMBIGUOUS is NOT "same".
    assert is_same_physical_venue(left, right) is False
    # ...but AMBIGUOUS still fails safe for replacement exclusion.
    assert should_exclude_replacement_candidate(left, right) is True


# --------------------------------------------------------------------------
# V2-T1 hardening regressions (docs/T1_IDENTITY_HARDENING.md, HARD_BLOCKERS B2)
# --------------------------------------------------------------------------


def _cross_provider_same_venue():
    """Overpass node and a different provider's place id for one restaurant."""

    overpass = _Place(
        "p1", external_id="overpass:node/555000111", lat=XIAMEN_LAT, lon=XIAMEN_LON,
        name="沙坡尾咖啡馆", category="cafe",
    )
    other = _Place(
        "p2", external_id="foursquare:place/ABC123XYZ", lat=XIAMEN_LAT, lon=XIAMEN_LON,
        name="沙坡尾咖啡馆", category="cafe",
    )
    return overpass, other


def test_v2_01_cross_provider_same_venue_is_ambiguous_not_different() -> None:
    """Different provider ids are not proof of different physical venues."""

    overpass, other = _cross_provider_same_venue()
    verdict = compare_physical_venue(overpass, other)
    assert verdict == AMBIGUOUS
    # Fail-safe: excluded from replacement family, yet not claimed as same.
    assert is_same_physical_venue(overpass, other) is False
    assert should_exclude_replacement_candidate(overpass, other) is True


def test_v2_02_same_provider_different_objects_clearly_different_is_different() -> None:
    """Same namespace, different objects, no corroborating signature -> DIFFERENT."""

    a = _Place("a", external_id="overpass:node/111", lat=1.0, lon=1.0, name="甲馆")
    b = _Place("b", external_id="overpass:way/222", lat=9.0, lon=9.0, name="乙馆")
    assert compare_physical_venue(a, b) == DIFFERENT
    assert should_exclude_replacement_candidate(a, b) is False


def test_v2_03_arbitrary_uppercase_external_id_is_not_a_wrapper() -> None:
    """Only documented CN-<CODE> ids may act as a CityNexus wrapper reference."""

    arbitrary = _Place("row-x", external_id="FOO-SOMETHING:real-place-abc", name="Nope")
    identity = physical_venue_identity(arbitrary)
    assert identity is not None
    assert identity.wrapper_reference is None
    # It must not falsely lock onto a provider row sharing the tail token.
    provider_row = _Place("real-place-abc", external_id="overpass:node/777", name="Else")
    assert compare_physical_venue(arbitrary, provider_row) == DIFFERENT


def test_v2_04_cross_provider_alias_excluded_from_replacement_family() -> None:
    """A closed venue re-described under another provider must not be re-offered."""

    closed, alias = _cross_provider_same_venue()
    choices = rank_closure_replacements(
        affected_place=closed,
        candidate_places=[alias, _different_venue()],
        occupied_place_ids={closed.id},
        max_duration_min=90,
    )
    offered = [choice.place_id for choice in choices]
    assert alias.id not in offered, "a closed venue reappeared under a cross-provider alias"
    assert offered == [_different_venue().id]


# --------------------------------------------------------------------------
# V3-4 near-coordinate identity (docs/T1_NEAR_IDENTITY_GATE.md)
# --------------------------------------------------------------------------


def _two_providers(name, category, lat_a, lon_a, lat_b, lon_b):
    a = _Place("a", provider_key="overpass", external_id="overpass:node/90001",
               lat=lat_a, lon=lon_a, name=name, category=category)
    b = _Place("b", provider_key="foursquare", external_id="foursquare:place/XYZ7",
               lat=lat_b, lon=lon_b, name=name, category=category)
    return a, b


def test_v3_04_cross_provider_near_drift_is_ambiguous_not_different() -> None:
    """Same venue a provider geocodes a few to a few tens of metres apart."""

    # ~10 m drift in latitude (~0.00009 deg).
    a, b = _two_providers("沙坡尾小馆", "restaurant", XIAMEN_LAT, XIAMEN_LON, XIAMEN_LAT + 0.00009, XIAMEN_LON)
    assert compare_physical_venue(a, b) == AMBIGUOUS
    assert is_same_physical_venue(a, b) is False
    assert should_exclude_replacement_candidate(a, b) is True


def test_v3_04_cross_provider_far_apart_is_different() -> None:
    """Same name/category but ~2 km apart are clearly different venues."""

    a, b = _two_providers("连锁咖啡", "cafe", XIAMEN_LAT, XIAMEN_LON, XIAMEN_LAT + 0.018, XIAMEN_LON)
    assert compare_physical_venue(a, b) == DIFFERENT


def test_v3_04_same_chain_different_branch_beyond_threshold_is_different() -> None:
    """A shared chain name must not merge branches separated by ~500 m."""

    a, b = _two_providers("全家便利店", "convenience", XIAMEN_LAT, XIAMEN_LON, XIAMEN_LAT + 0.0045, XIAMEN_LON)
    assert compare_physical_venue(a, b) == DIFFERENT


def test_identity_extraction_reads_provider_tuple_not_bare_number() -> None:
    identity = physical_venue_identity(_wrapped_provider_row())
    assert identity is not None
    assert identity.provider_scoped == ("overpass", "node", "1588295483")
    wrapper_identity = physical_venue_identity(_wrapper())
    assert wrapper_identity is not None
    assert wrapper_identity.wrapper_reference == "real-place-07e7689756fa27109a8bff96"
    assert wrapper_identity.provider_scoped is None


def test_regression_10_closed_venue_is_excluded_from_its_own_replacement_family() -> None:
    """The closed wrapper and the provider row it wraps are both excluded."""

    choices = rank_closure_replacements(
        affected_place=_wrapper(),
        candidate_places=[_wrapped_provider_row(), _different_venue()],
        occupied_place_ids={_wrapper().id},
        max_duration_min=90,
    )
    offered = [choice.place_id for choice in choices]
    assert _wrapped_provider_row().id not in offered, (
        "the closed venue reappeared as its own replacement"
    )
    assert offered == [_different_venue().id]


def test_occupied_venue_is_not_reused_under_another_id() -> None:
    already_used = _wrapped_provider_row()
    choices = rank_closure_replacements(
        affected_place=_different_venue(),
        candidate_places=[already_used, _wrapper()],
        occupied_place_ids={already_used.id},
        occupied_places=[already_used],
        max_duration_min=90,
    )
    assert all(
        choice.place_id not in {already_used.id, _wrapper().id} for choice in choices
    ), "a venue already used in the plan was offered again under a different id"
