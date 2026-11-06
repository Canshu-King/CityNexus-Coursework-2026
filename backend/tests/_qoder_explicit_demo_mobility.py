"""Test-side helper: seed scenario-declared EXPLICIT mobility legs (per module).

V2-B1 (closed in v3) made the real replan path refuse the mock fixture's blanket
``default_minutes=20`` as a hard-feasibility fact.  Several pre-existing replan
tests were written against the OLD behaviour where the offline demo silently used
that default, so once B1 is closed they fail with ``MOBILITY_DATA_UNAVAILABLE``.

The v2 helper generated a complete graph at one invented value and autoused it
module-wide (rejected by V3 review).  The v3 helper used a single ~25-pair table
shared by every module via autouse — still broader than needed: a module gained
route legs unrelated to its own scenarios (V4 #5).

V4 #5 narrows this to a per-module minimal subset: each module seeds only the
exact legs its replan scenarios actually traverse (discovered empirically — see
the module->legs mapping in TEST_RESULTS).  These are *explicit deterministic
fixture facts* the scenario declares, each carrying the same real duration as
the historical offline fixture.  They are NOT live/Xiamen measurements.  Any
pair outside a module's subset stays unseeded and resolves to the mock blanket
default, which the fail-closed classifier marks SYNTHETIC_DEFAULT and hard
feasibility rejects — so B1 stays intact (the sentinel in
test_qoder_mobility_replan_endpoint proves this).
"""

from __future__ import annotations

from app.integrations.mock import providers as _providers

# Shared leg durations.  Each value is the historical offline fixture duration
# for that adjacency (deterministic, scenario-declared — not a live measurement).
_FOOD_HALL__RIVER_CRUISE = 20
_TECH_ART__CITY_PARK = 8
_CITY_PARK__RIVERSIDE_MUSEUM = 17
_DESIGN_LIBRARY__RIVERSIDE_MUSEUM = 15
_OLD_TOWN__CITY_PARK = 22
_RIVER_CRUISE__CITY_PARK = 16


def _leg(a: str, b: str, minutes: int) -> dict[tuple[str, str], int]:
    return {(f"place-{a}", f"place-{b}"): minutes}


# --- per-module minimal subsets (only the legs that module's tests traverse) ---

GOLDEN_CONTRACT_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
    **_leg("tech-art", "city-park", _TECH_ART__CITY_PARK),
}

GUARDIAN_MODEL_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("city-park", "riverside-museum", _CITY_PARK__RIVERSIDE_MUSEUM),
    **_leg("design-library", "riverside-museum", _DESIGN_LIBRARY__RIVERSIDE_MUSEUM),
}

POST_GOAL_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
    **_leg("old-town", "city-park", _OLD_TOWN__CITY_PARK),
}

SOLVER_RUNTIME_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("tech-art", "city-park", _TECH_ART__CITY_PARK),
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
}

SOLVER_V2_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("river-cruise", "city-park", _RIVER_CRUISE__CITY_PARK),
    **_leg("tech-art", "city-park", _TECH_ART__CITY_PARK),
}

VERTICAL_SLICE_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
    **_leg("tech-art", "city-park", _TECH_ART__CITY_PARK),
}

WP06_FOLLOWUP_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
    **_leg("old-town", "city-park", _OLD_TOWN__CITY_PARK),
}

WP06_POSTGATE_MOBILITY: dict[tuple[str, str], int] = {
    **_leg("food-hall", "river-cruise", _FOOD_HALL__RIVER_CRUISE),
}


def seed_explicit_mobility(
    monkeypatch, pairs: dict[tuple[str, str], int]
) -> None:
    """Register ONLY the given exact pairs as explicit fixture facts.

    Any pair not passed in stays absent from the matrix, so on a replan capture it
    resolves to the blanket default and is (correctly) refused for hard truth.
    """

    minutes_input = {f"{a}|{b}": int(m) for (a, b), m in pairs.items()}
    original_init = _providers.MockMobilityProvider.__init__

    def patched_init(self, place_lookup=None, *, strict=False):
        original_init(self, place_lookup, strict=strict)
        minutes = self._fixture["minutes"]
        for key, value in minutes_input.items():
            minutes.setdefault(key, value)

    monkeypatch.setattr(_providers.MockMobilityProvider, "__init__", patched_init)


def seed_module_mobility(monkeypatch, table: dict[tuple[str, str], int]) -> None:
    """Autouse hook: seed exactly one module's declared subset.

    A module never gains legs outside its own table, so narrowing the shared
    v3 table into per-module subsets keeps every scenario honest about which
    mobility facts it actually relies on.
    """

    seed_explicit_mobility(monkeypatch, table)


def authoritative_recovery_mobility(journey_id: str) -> dict[tuple[str, str], int]:
    """Explicit deterministic FIXTURE legs for the authoritative V12/V13 journey.

    Both tests compose the same `v12:{journey_id}:*` / `v12-test-place-*` world
    and their (auto)nomous / predictive recovery hard-gates the selected route.
    These are scenario fixture values (the itinerary legs mirror the offline demo
    fixture's per-adjacency durations), NOT live/Xiamen measurements.  A pair
    outside this table stays unseeded/synthetic, so B1 is never neutralised.
    """

    a = lambda k: f"v12:{journey_id}:{k}"  # noqa: E731
    p = lambda i: f"v12-test-place-{i}"  # noqa: E731
    return {
        (a("arrival-anchor"), a("check-in")): 15,
        (a("check-in"), p(0)): 8,
        (a("check-in"), p(3)): 24,
        (p(0), p(3)): 17,
        (p(3), a("stay-night-1")): 21,
        (a("stay-night-1"), p(1)): 12,
        (p(1), p(4)): 19,
        (p(4), a("stay-night-2")): 23,
        (a("stay-night-2"), p(2)): 14,
        (p(2), p(5)): 9,
        (p(5), a("check-out")): 11,
        (a("check-out"), a("departure-anchor")): 42,
    }


def seed_authoritative_recovery_mobility(monkeypatch, journey_id: str) -> None:
    seed_explicit_mobility(monkeypatch, authoritative_recovery_mobility(journey_id))
