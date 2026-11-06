"""V4 #5 sentinel: prove the per-module mobility helper cannot neutralise B1.

For every module subset the helper ships, assert that:
  * the table is genuinely minimal (a handful of legs, no self-pair, no
    accidental complete-graph / universal-value table);
  * a declared leg becomes an EXPLICIT_FROZEN_FIXTURE fact; and
  * a leg outside the subset stays SYNTHETIC_DEFAULT — the blanket default that
    hard feasibility must refuse (T2 / B1).

This is the per-module form of the sentinel GPT asked for (LEGACY_TEST_FIXTURE_
GATE / v4 #5): narrowing the shared table must not have quietly turned
"missing mobility" into "known mobility" for any module.
"""

from __future__ import annotations

import pytest

from _qoder_explicit_demo_mobility import (
    GOLDEN_CONTRACT_MOBILITY,
    GUARDIAN_MODEL_MOBILITY,
    POST_GOAL_MOBILITY,
    SOLVER_RUNTIME_MOBILITY,
    SOLVER_V2_MOBILITY,
    VERTICAL_SLICE_MOBILITY,
    WP06_FOLLOWUP_MOBILITY,
    WP06_POSTGATE_MOBILITY,
    seed_explicit_mobility,
)
from app.integrations.mock.providers import MockMobilityProvider
from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, SYNTHETIC_DEFAULT

MODULE_TABLES: dict[str, dict[tuple[str, str], int]] = {
    "golden_contract": GOLDEN_CONTRACT_MOBILITY,
    "guardian_model": GUARDIAN_MODEL_MOBILITY,
    "post_goal": POST_GOAL_MOBILITY,
    "solver_runtime": SOLVER_RUNTIME_MOBILITY,
    "solver_v2": SOLVER_V2_MOBILITY,
    "vertical_slice": VERTICAL_SLICE_MOBILITY,
    "wp06_followup": WP06_FOLLOWUP_MOBILITY,
    "wp06_postgate": WP06_POSTGATE_MOBILITY,
}

# Deliberately outside every module subset: must stay the synthetic default.
UNDECLARED_PAIR = ("place-river-cruise", "place-old-town")


@pytest.mark.parametrize("name,table", list(MODULE_TABLES.items()))
def test_module_subset_is_minimal_and_undeclared_stays_synthetic(name, table, monkeypatch):
    assert table, f"{name} must declare a non-empty subset"
    assert len(table) <= 3, f"{name} subset is broader than per-module minimal"
    for origin, destination in table:
        assert origin != destination, f"{name} declares a self-pair (complete-graph tell)"
    assert UNDECLARED_PAIR not in table, f"{name} unexpectedly declares the sentinel leg"

    seed_explicit_mobility(monkeypatch, table)
    provider = MockMobilityProvider()

    listed_origin, listed_destination = next(iter(table))
    assert provider.fact_quality(listed_origin, listed_destination) == EXPLICIT_FROZEN_FIXTURE
    assert provider.fact_quality(*UNDECLARED_PAIR) == SYNTHETIC_DEFAULT
