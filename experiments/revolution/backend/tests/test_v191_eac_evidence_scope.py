"""V19.1 EAC-aware capability contract tests.

Tests-only.  Nothing here modifies production code.

Two groups, following the round's convention:

  test_v191_eac_guard_*   must pass today; a failure means a regression
  test_v191_eac_defect_*  documents a known defect; a failure means it was FIXED

Covers the EAC-Aware Update brief:
  * the two mandated false-HARD sentences
  * denial / hearsay / hypothetical / facility-subject false HARD
  * HARD accessibility immutability
  * duplicate merge, cross-scope merge, numeric-merge direction
  * provider-evidence scope (toilets/ramp/elevator must not verify step-free)

Pure functions only, so the suite runs without a database or a network.
"""

from __future__ import annotations

import pytest

from app.capabilities import (
    CAPABILITY_CODES,
    canonicalize_capabilities,
    extract_capabilities_from_text,
)

HARD_CODES = {"STEP_FREE_REQUIRED", "AVOID_STAIRS"}


def codes(text: str) -> list[str]:
    items = extract_capabilities_from_text(text, scope="JOURNEY")
    out = []
    for item in items:
        out.append(item.code if hasattr(item, "code") else str(item.get("code")))
    return sorted(out)


def canon(items):
    out = canonicalize_capabilities(items)
    return [
        item if isinstance(item, dict) else item.as_dict()  # type: ignore[union-attr]
        for item in out
    ]


def is_hard(text: str) -> bool:
    return bool(set(codes(text)) & HARD_CODES)


# --------------------------------------------------------------------------
# guards: these must hold today
# --------------------------------------------------------------------------
def test_v191_eac_guard_unknown_code_is_dropped():
    assert canon([{"code": "NOT_A_REAL_CODE", "severity": "HARD"}]) == []


def test_v191_eac_guard_control_hard_need_is_detected():
    # The controls must keep working; a fix must not simply disable detection.
    assert "STEP_FREE_REQUIRED" in codes("我必须全程无障碍")
    assert "STEP_FREE_REQUIRED" in codes("我需要轮椅，必须全程无障碍")
    assert "STEP_FREE_REQUIRED" in codes("wheelchair, must be step-free")


def test_v191_eac_guard_plain_denial_is_clean():
    assert codes("我不需要轮椅") == []


def test_v191_eac_guard_hard_code_cannot_be_downgraded():
    """Guard for the D1 delta target.  EAC_REFERENCE fixes this; FINAL_V19 does not."""
    rows = canon([{"code": "STEP_FREE_REQUIRED", "severity": "SOFT"}])
    assert rows, "the item must survive canonicalization"
    assert rows[0]["severity"] == "HARD", (
        "a HARD accessibility code must not be downgraded by the caller"
    )


def test_v191_eac_guard_duplicate_codes_are_merged():
    """Guard for the D3 delta target.  EAC_REFERENCE fixes this; FINAL_V19 does not."""
    rows = canon([
        {"code": "STEP_FREE_REQUIRED", "severity": "HARD", "params": {"a": 1}},
        {"code": "STEP_FREE_REQUIRED", "severity": "HARD", "params": {"b": 2}},
    ])
    assert len(rows) == 1, f"expected one merged row, got {len(rows)}"


# --------------------------------------------------------------------------
# defects: the mandated false-HARD class (brief item 1)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "I don't need a wheelchair, I can walk",
    "朋友说我可能需要轮椅，但我不这么认为",
])
def test_v191_eac_defect_mandated_false_hard(text):
    """The two sentences the brief names.  Both implementations still fail these."""
    assert not is_hard(text), (
        f"false HARD accessibility need manufactured from {text!r}: {codes(text)}"
    )


@pytest.mark.parametrize("text", [
    "I don't use a wheelchair",
    "I am not a wheelchair user",
    "no wheelchair needed, I can walk fine",
    "我不用轮椅",
    "我没有轮椅需求",
    "no stairs problem for me, I'm fine",
])
def test_v191_eac_defect_denial_does_not_create_need(text):
    assert not is_hard(text), f"denial created a need: {codes(text)}"


@pytest.mark.parametrize("text", [
    "my friend needs a wheelchair, not me",
    "my mother uses a wheelchair",
    "my colleague said I might need a wheelchair",
    "我朋友需要轮椅",
    "我妈妈坐轮椅",
    "同事说我也许需要轮椅",
])
def test_v191_eac_defect_third_party_does_not_create_need(text):
    assert not is_hard(text), f"third-party need became the traveller's: {codes(text)}"


@pytest.mark.parametrize("text", [
    "if I get tired I might need a wheelchair",
    "如果累了可能需要轮椅",
    "maybe I should avoid stairs",
])
def test_v191_eac_defect_hypothetical_does_not_create_hard(text):
    assert not is_hard(text), f"hypothetical became HARD: {codes(text)}"


@pytest.mark.parametrize("text", [
    "the station has an accessible toilet",
    "车站有无障碍厕所",
])
def test_v191_eac_defect_facility_description_is_not_a_need(text):
    """Describing a venue's accessibility is not a statement about the traveller."""
    assert not is_hard(text), f"facility description became a need: {codes(text)}"


# --------------------------------------------------------------------------
# defects: clause-local negation / history (brief item 1, the erase direction)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "轮椅必须全程无障碍，没有例外",
    "我以前不用轮椅，但现在必须无障碍",
    "过去我坐轮椅，现在必须全程无障碍",
    "我没有过敏，但我用轮椅，必须全程无障碍",
    "不需要人扶，但我坐轮椅，必须全程无障碍",
    "我一直坐轮椅，不是这次才需要",
])
def test_v191_eac_defect_live_clause_must_survive(text):
    """EAC_REFERENCE fixes these; FINAL_V19 erases the live need."""
    assert "STEP_FREE_REQUIRED" in codes(text), (
        f"the live need was erased by a negation/history clause: {codes(text)}"
    )


def test_v191_eac_defect_scoped_negation_keeps_luggage_need():
    assert "LARGE_LUGGAGE" in codes("不是地铁，我需要出租车因为行李很多")


# --------------------------------------------------------------------------
# defects: merge semantics (brief item 3)
# --------------------------------------------------------------------------
def test_v191_eac_defect_merge_respects_scope():
    """TRAVELER + JOURNEY must stay two rows.  EAC_REFERENCE collapses them."""
    rows = canon([
        {"code": "AVOID_STAIRS", "severity": "HARD", "scope": "TRAVELER",
         "params": {"note": "traveler"}},
        {"code": "AVOID_STAIRS", "severity": "HARD", "scope": "JOURNEY",
         "params": {"note": "journey"}},
    ])
    scopes = sorted({row.get("scope") for row in rows})
    assert scopes == ["JOURNEY", "TRAVELER"], (
        f"cross-scope items must not collapse: got scopes {scopes}"
    )


def test_v191_eac_defect_merge_keeps_strictest_limit():
    """Merging two limits must not weaken the constraint."""
    rows = canon([
        {"code": "MAX_WALKING_PER_LEG_M", "severity": "HARD", "params": {"max_m": 400}},
        {"code": "MAX_WALKING_PER_LEG_M", "severity": "HARD", "params": {"max_m": 800}},
    ])
    assert len(rows) == 1
    assert rows[0]["params"].get("max_m") == 400, (
        "the stricter walking limit must win, got "
        f"{rows[0]['params'].get('max_m')}"
    )


# --------------------------------------------------------------------------
# defects: numeric values must be range-checked (brief item 1 / EAC new defect)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "走路不能超过 0 米",
    "走路不能超过 -5 米",
])
def test_v191_eac_defect_nonsense_walking_limit_is_rejected(text):
    items = extract_capabilities_from_text(text, scope="JOURNEY")
    rows = [
        item.as_dict() if hasattr(item, "as_dict") else dict(item)  # type: ignore
        for item in items
    ]
    walking = [r for r in rows if r.get("code") == "MAX_WALKING_PER_LEG_M"]
    if not walking:
        pytest.skip("no walking limit extracted at all (also acceptable)")
    for row in walking:
        value = (row.get("params") or {}).get("max_m")
        assert value is None or value >= 1, (
            f"a non-positive walking limit must not be persisted: {value}"
        )


# --------------------------------------------------------------------------
# defects: provider evidence scope (brief item 2)
# --------------------------------------------------------------------------
# NOTE: this must NOT be a module-level `pytest.importorskip(...)`.  A
# module-level importorskip raises Skipped during collection, which silently
# drops *every* test in the file -- including the two mandated-sentence tests
# above.  Bind the module to None instead and gate only the tests that need it.
try:
    from app.integrations import accessibility as _acc_pkg  # noqa: F401
    from app.integrations.accessibility import osm  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - EAC-only module
    osm = None  # type: ignore[assignment]

requires_osm = pytest.mark.skipif(
    osm is None,
    reason="provider-evidence adapter is EAC-only; skipped on the current tree",
)

STEP_FREE_VERIFIED = "ACCESSIBILITY_VERIFIED"


def step_free_satisfied(tags) -> bool:
    facts = osm.evidence_from_osm_tags(tags)
    overall = osm.overall_accessibility_state(facts)
    return overall == STEP_FREE_VERIFIED


@requires_osm
@pytest.mark.parametrize("tags", [
    {"toilets:wheelchair": "yes"},
    {"entrance:wheelchair": "yes"},
    {"ramp": "yes"},
    {"elevator": "yes"},
    {"kerb": "lowered"},
    {"tactile_paving": "yes"},
])
def test_v191_eac_defect_non_step_free_tag_must_not_verify(tags):
    """A facility-scoped tag must never establish a step-free route."""
    assert not step_free_satisfied(tags), (
        f"{tags} wrongly promoted to a verified step-free route"
    )


@requires_osm
@pytest.mark.parametrize("tags", [
    {"wheelchair": "yes", "step_count": 5},
    {"wheelchair": "yes", "steps": "yes"},
    {"wheelchair": "yes", "highway": "steps"},
])
def test_v191_eac_guard_conflicting_tags_are_conflict(tags):
    facts = osm.evidence_from_osm_tags(tags)
    assert osm.overall_accessibility_state(facts) == "ACCESSIBILITY_CONFLICT"


@requires_osm
def test_v191_eac_defect_verified_place_not_blocked_by_partial_tag():
    """An explicit place-level verification must survive an unrelated partial tag."""
    assert step_free_satisfied({"wheelchair": "yes"})
    assert step_free_satisfied(
        {"wheelchair": "yes", "ramp": "yes"}), (
        "an unrelated ramp tag must not downgrade a verified place"
    )


@requires_osm
@pytest.mark.parametrize("tags", [
    {"wheelchair": ["yes"]},
    {"toilets:wheelchair": ["yes"]},
])
def test_v191_eac_defect_sequence_tag_values_are_not_dropped(tags):
    facts = osm.evidence_from_osm_tags(tags)
    assert facts, f"sequence-valued tags were silently dropped: {tags}"
    assert not step_free_satisfied(tags), (
        "a sequence-valued tag must not be read as a verified route"
    )


@requires_osm
def test_v191_eac_defect_empty_tags_stay_unknown():
    facts = osm.evidence_from_osm_tags({})
    assert osm.overall_accessibility_state(facts) == "NEEDS_LIVE_EVIDENCE"


def test_v191_eac_guard_capability_vocabulary_is_stable():
    assert "STEP_FREE_REQUIRED" in CAPABILITY_CODES
    assert "AVOID_STAIRS" in CAPABILITY_CODES


# ==========================================================================
# CODEX_V19_1_IF_AVAILABLE section
#
# Codex V19.1 added a per-requirement evaluator in app.capabilities:
#     normalize_osm_tags(value)                  -> dict[str, str]
#     evaluate_accessibility_requirement(code, facts) -> state string
# and wired it into the gate (feasibility.py:286).  The tests below drive that
# API directly, so unlike the EAC `osm` block above they run on the live tree
# instead of being skipped.
#
# The `guard_` group passes on Codex V19.1 today and must keep passing.
# The `defect_` group fails on Codex V19.1 today; a failure means the defect is
# still live, a pass means it was fixed.
# ==========================================================================
try:
    from app.capabilities import (  # type: ignore[attr-defined]
        evaluate_accessibility_requirement as _eval_req,
        normalize_osm_tags as _norm_tags,
    )
    _HAS_PER_REQUIREMENT = True
except ImportError:  # pragma: no cover - older trees
    _HAS_PER_REQUIREMENT = False

requires_per_requirement = pytest.mark.skipif(
    not _HAS_PER_REQUIREMENT,
    reason="per-requirement evaluator not present on this tree",
)


def _step_free_state(tags) -> str:
    """Exactly the view the gate uses (feasibility.py:286)."""
    return _eval_req("STEP_FREE_REQUIRED", tags if isinstance(tags, dict) else {})


# ---- guards: provider scope must not leak across subject / code ----------
@requires_per_requirement
@pytest.mark.parametrize("tags", [
    {"toilets:wheelchair": "yes"},
    {"toilets:wheelchair": "no"},
    {"ramp": "yes"},
    {"ramp:wheelchair": "yes"},
    {"elevator": "yes"},
    {"highway:elevator": "yes"},
    {"kerb": "lowered"},
    {"tactile_paving": "yes"},
    {"entrance:wheelchair": "yes"},
    {"wheelchair": "yes"},
    {"wheelchair": "designated"},
    {"wheelchair": "limited"},
    {"wheelchair": "no"},
])
def test_v191_eac_guard_facility_tags_do_not_verify_step_free(tags):
    """A facility-scoped tag must never establish a step-free place or route."""
    assert _step_free_state(tags) != "ACCESSIBILITY_VERIFIED", (
        f"{tags} wrongly promoted to a verified step-free requirement"
    )


@requires_per_requirement
@pytest.mark.parametrize("facts", [
    {"subject": "ROUTE"},
    {"subject": "ROUTE", "wheelchair": "yes"},
])
def test_v191_eac_guard_route_without_route_evidence_stays_unknown(facts):
    """Route-level step-free with no real provider evidence must stay unknown."""
    assert _step_free_state(facts) in ("NEEDS_LIVE_EVIDENCE",
                                       "ACCESSIBILITY_UNVERIFIED")


@requires_per_requirement
@pytest.mark.parametrize("tags", [
    {"step_free": "yes"},
    {"step_free_verified": "yes"},
])
def test_v191_eac_guard_explicit_step_free_assertion_verifies(tags):
    assert _step_free_state(tags) == "ACCESSIBILITY_VERIFIED"


@requires_per_requirement
def test_v191_eac_guard_explicit_stairs_are_conflict():
    assert _step_free_state({"wheelchair": "yes", "stairs": "yes"}) == (
        "ACCESSIBILITY_CONFLICT"
    )


# ---- defects: residual provider-evidence scope problems on Codex V19.1 ----
@requires_per_requirement
@pytest.mark.parametrize("tags", [
    {"wheelchair": "yes", "step_count": 5},
    {"wheelchair": "yes", "steps": "yes"},
    {"wheelchair": "yes", "highway": "steps"},
])
def test_v191_eac_defect_step_evidence_contradiction_is_conflict(tags):
    """`step_count` / `steps` / `highway=steps` must count as stairs.

    Codex V19.1 only reads `stairs` / `has_stairs`, so these are reported as
    merely unverified instead of the contradiction they are.
    """
    assert _step_free_state(tags) == "ACCESSIBILITY_CONFLICT", (
        f"{tags} is a contradiction but was reported as "
        f"{_step_free_state(tags)}"
    )


@requires_per_requirement
@pytest.mark.parametrize("tags,key,expected", [
    ({"wheelchair": ["yes"]}, "wheelchair", "yes"),
    ({"ramp": ("yes",)}, "ramp", "yes"),
    ({"step_count": [5]}, "step_count", "5"),
    ({"toilets:wheelchair": ["yes"]}, "toilets:wheelchair", "yes"),
])
def test_v191_eac_defect_sequence_tag_values_are_not_dropped_by_normalizer(
        tags, key, expected):
    """A list/tuple used as a mapping value must not be stringified into junk.

    `normalize_osm_tags` does `str(v)` on mapping values, so `["yes"]` becomes
    the literal `"['yes']"` and is silently lost.
    """
    resolved = _norm_tags(tags)
    assert resolved.get(key) == expected, (
        f"sequence value dropped: {tags} normalized to {resolved}"
    )


@requires_per_requirement
@pytest.mark.parametrize("tags", [
    ["wheelchair=yes", "ramp=yes"],
    {"tags": {"wheelchair": "yes"}},
    {"tags": ["wheelchair=yes"]},
    {"osm_tags": {"wheelchair": "yes"}},
])
def test_v191_eac_guard_sequence_and_nested_forms_still_work(tags):
    """The sequence and nested forms are handled correctly; keep them working."""
    inner = tags
    if isinstance(tags, dict):
        inner = tags.get("osm_tags") or tags.get("tags") or tags
    assert _norm_tags(inner).get("wheelchair") == "yes", (
        f"{tags} was not resolved"
    )


@requires_per_requirement
def test_v191_eac_defect_top_level_step_free_verified_is_seen():
    """`step_free_verified` at the metadata top level must reach the evaluator.

    `_accessibility_metadata` whitelists `step_free` but not
    `step_free_verified`, so the same assertion is honoured when nested under
    `tags`/`osm_tags` and silently ignored when placed at the top level.
    """
    from app.replanning.feasibility import _accessibility_metadata

    class _Rec:
        def __init__(self, metadata):
            self.metadata_json = __import__("json").dumps(metadata)

    for metadata in ({"step_free_verified": "yes"},
                     {"step_free": "yes"}):
        projected = _accessibility_metadata(_Rec(metadata))
        state = _eval_req("STEP_FREE_REQUIRED", projected or {})
        assert state == "ACCESSIBILITY_VERIFIED", (
            f"top-level {metadata} projected to {projected} -> {state}"
        )


# ---- defects: the two residual speculation cases -------------------------
@pytest.mark.parametrize("text", [
    "说不定到时候我需要轮椅",
    "if I end up in a wheelchair I'll tell you",
])
def test_v191_eac_defect_speculation_does_not_create_hard(text):
    """Residual D2 cases after the Codex V19.1 negation/history fix."""
    assert not is_hard(text), f"speculation became HARD: {codes(text)}"

