"""V19 post-fix capability contract tests.

These tests target the capability domain Codex landed *during* the V19 run
(``backend/app/capabilities.py``, first seen 2026-09-15 10:53).  They are
written to the contract the module documents about itself, not to the
implementation, so they fail wherever the contract is unmet.

Naming convention matches the rest of the V19 suite:

* ``test_v19_guard_*``  - behaviour that is already correct; must not regress.
* ``test_v19_char_*``   - measured defect, locked on purpose so a fix is
  observable as a test flipping from fail to pass.

Run from ``repo_copy_current/backend``:

    python -m pytest tests/test_v19_postfix_capability_contract.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.capabilities import (
    accessibility_state,
    canonicalize_capabilities,
    capability_constraints,
    extract_capabilities_from_text,
    summarize_capabilities,
)
from app.integrations.mock.providers import MockMobilityProvider
from app.replanning.feasibility import (
    FeasibilityActivity,
    FeasibilityContext,
    evaluate_feasibility,
)

START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

PLACE = {
    "id": "p1",
    "city_code": "TJN",
    "status": "available",
    "opening_hours_json": {"start": "08:00", "end": "22:00"},
    "metadata_json": {},
}


def _codes(text: str, scope: str = "JOURNEY") -> list[str]:
    return [item["code"] for item in extract_capabilities_from_text(text, scope=scope)]


def _severity(text: str, code: str) -> str | None:
    for item in extract_capabilities_from_text(text):
        if item["code"] == code:
            return item["severity"]
    return None


def _gate(constraints):
    context = FeasibilityContext(
        journey_id="journey-1",
        city_code="TJN",
        journey_start=START,
        journey_end=START + timedelta(hours=10),
        budget_amount_minor=200_000,
        budget_currency="CNY",
        constraints=tuple(constraints),
        places={"p1": PLACE},
        services={},
        mobility_provider=MockMobilityProvider(),
        plan_currency="CNY",
    )
    activities = [
        FeasibilityActivity(
            key="a1",
            start_at=START + timedelta(hours=1),
            end_at=START + timedelta(hours=2),
            cost_minor=5_000,
            place_id="p1",
        )
    ]
    return evaluate_feasibility(context, activities)


# --------------------------------------------------------------------------- #
# Guards: already correct, must not regress.
# --------------------------------------------------------------------------- #


def test_v19_guard_unknown_accessibility_is_not_verified():
    assert accessibility_state(None) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({}) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({"name": "Wheelchair ramp entrance"}) == "ACCESSIBILITY_UNVERIFIED"


def test_v19_guard_accessibility_evidence_is_normalised():
    assert accessibility_state({"step_free": True}) == "ACCESSIBILITY_VERIFIED"
    assert accessibility_state({"step_free": False}) == "ACCESSIBILITY_CONFLICT"
    assert accessibility_state({"stairs": True}) == "ACCESSIBILITY_CONFLICT"


def test_v19_guard_soft_preference_never_reaches_the_gate():
    caps = extract_capabilities_from_text("最好少走点楼梯，能坐电梯就坐电梯")
    assert capability_constraints(caps) == []


def test_v19_guard_unknown_capability_code_is_dropped():
    assert canonicalize_capabilities([{"code": "NOT_A_REAL_CODE", "severity": "HARD"}]) == ()


def test_v19_guard_clean_hard_sentence_is_understood():
    caps = extract_capabilities_from_text("我用轮椅，必须全程 step-free，一步楼梯都不能走")
    assert {c["code"] for c in caps} == {"STEP_FREE_REQUIRED", "AVOID_STAIRS"}
    assert all(c["severity"] == "HARD" for c in caps)
    assert all(c["class"] == "HARD_ACCESSIBILITY_NEED" for c in caps)


def test_v19_guard_hard_code_class_is_corrected_when_mislabeled():
    canon = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "severity": "HARD", "class": "SOFT_PREFERENCE"}]
    )
    assert canon[0]["class"] == "HARD_ACCESSIBILITY_NEED"


# --------------------------------------------------------------------------- #
# D1 - the hard-downgrade guard in canonicalize_capabilities is a tautology.
#      It only preserves HARD when the caller already said HARD, so a model
#      (or an API client) can still erase a hard accessibility need.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("severity", ["STRONG", "SOFT"])
def test_v19_char_d1a_model_cannot_downgrade_hard_accessibility(severity):
    canon = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "severity": severity, "class": "HARD_ACCESSIBILITY_NEED"}]
    )
    assert canon, "the capability must survive canonicalization"
    assert canon[0]["severity"] == "HARD", (
        f"a model labelling STEP_FREE_REQUIRED as {severity} downgraded a hard "
        "accessibility need; the module documents that this may not happen"
    )
    assert capability_constraints(canon), "a hard accessibility need must reach the gate"


@pytest.mark.parametrize("severity", ["STRONG", "SOFT"])
def test_v19_char_d1b_model_cannot_downgrade_avoid_stairs(severity):
    canon = canonicalize_capabilities([{"code": "AVOID_STAIRS", "severity": severity}])
    assert canon[0]["severity"] == "HARD"
    assert capability_constraints(canon)


def test_v19_char_d1c_omitted_severity_must_not_downgrade_a_hard_code():
    canon = canonicalize_capabilities([{"code": "STEP_FREE_REQUIRED"}])
    assert canon, "a hard accessibility code must not be dropped for a missing severity"
    assert canon[0]["severity"] == "HARD"


def test_v19_char_d1d_model_SOFT_plus_silent_backstop_loses_the_need():
    """The whole exploit: model says SOFT, the local extractor says nothing."""
    text = "我以前不用轮椅，但现在必须全程 step-free"
    local = extract_capabilities_from_text(text)
    merged = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "severity": "SOFT", "params": {}}, *local],
        source="SEMANTIC",
        provenance="USER_STATED",
    )
    assert capability_constraints(merged), (
        "a wheelchair traveller who states a hard step-free need ends up with no "
        "hard gate at all"
    )


# --------------------------------------------------------------------------- #
# D2 - the negation guard is text-global: any negated/historical marker
#      anywhere in the sentence erases the whole clause.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "我以前不用轮椅，但现在必须全程 step-free",
        "我没有过敏，但我用轮椅，必须全程无障碍",
        "过去我坐轮椅，现在必须全程无障碍",
        "wheelchair 必须 step-free，没有例外",
        "I used to walk fine but now I use a wheelchair and need every leg step-free",
    ],
)
def test_v19_char_d2a_live_hard_need_survives_a_negated_clause_elsewhere(text):
    assert "STEP_FREE_REQUIRED" in _codes(text), (
        "a live hard accessibility need was erased by a negation or past-tense "
        f"marker elsewhere in the sentence: {text!r}"
    )


def test_v19_char_d2b_the_word_for_no_exceptions_erases_the_need():
    """'没有例外' (no exceptions) contains 没有, which trips the global guard."""
    text = "wheelchair 必须 step-free，没有例外"
    assert "STEP_FREE_REQUIRED" in _codes(text), "the need must survive '没有例外'"


# --------------------------------------------------------------------------- #
# D3 - no de-duplication: one code can arrive twice with conflicting severity.
# --------------------------------------------------------------------------- #


def test_v19_char_d3a_same_code_is_not_reported_as_both_hard_and_soft():
    merged = canonicalize_capabilities(
        [
            {"code": "STEP_FREE_REQUIRED", "severity": "SOFT", "params": {}},
            *extract_capabilities_from_text("必须全程无障碍，一步楼梯都不能走"),
        ]
    )
    summary = summarize_capabilities(merged)
    both = {c["code"] for c in summary["hard"]} & {c["code"] for c in summary["soft"]}
    assert not both, f"the same capability is reported as both HARD and SOFT: {sorted(both)}"


def test_v19_char_d3b_canonicalize_deduplicates_by_code():
    merged = canonicalize_capabilities(
        [
            {"code": "STEP_FREE_REQUIRED", "severity": "SOFT"},
            {"code": "STEP_FREE_REQUIRED", "severity": "HARD"},
        ]
    )
    assert len(merged) == 1


# --------------------------------------------------------------------------- #
# D4/D5 - hard care and numeric limits are not extracted at all, so nothing
#         reaches the gate and the plan silently ignores the need.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,code",
    [
        ("老人中午 12 点到 14 点必须回酒店休息", "MIDDAY_REST_WINDOW"),
        ("我最多走 500 米，超过就走不动了", "MAX_WALKING_PER_LEG_M"),
        ("走路不能超过 500m，这是硬性要求", "MAX_WALKING_PER_LEG_M"),
        ("宝宝下午 1 点到 3 点必须睡午觉，不能安排活动", "INFANT_NAP_WINDOW"),
        ("我需要每 60 分钟休息一次", "REST_EVERY_MIN"),
    ],
)
def test_v19_char_d4a_hard_care_and_limit_statements_are_extracted(text, code):
    assert code in _codes(text), f"a stated hard need produced no capability at all: {text!r}"


def test_v19_char_d4b_numeric_walking_limit_keeps_its_value():
    caps = extract_capabilities_from_text("走路不能超过 500m，这是硬性要求")
    walking = [c for c in caps if c["code"] == "MAX_WALKING_PER_LEG_M"]
    assert walking, "the numeric walking limit was not extracted"
    assert walking[0]["params"].get("max_m") == 500


# --------------------------------------------------------------------------- #
# D6 - the capability layer projects rule codes the feasibility gate does not
#      implement, so a hard need makes the Journey unplannable instead of
#      respected.
# --------------------------------------------------------------------------- #


def test_v19_char_d6a_capability_projection_is_interpretable_by_the_gate():
    caps = extract_capabilities_from_text("我用轮椅，必须全程 step-free，一步楼梯都不能走")
    constraints = capability_constraints(caps)
    assert constraints, "a hard accessibility need must project a gate constraint"
    result = _gate(constraints)
    unsupported = [f for f in result.failures if f.startswith("UNSUPPORTED_HARD_CONSTRAINT")]
    assert not unsupported, (
        "the capability layer emits rule codes the gate cannot interpret, so every "
        f"wheelchair traveller is blocked: {unsupported}"
    )


def test_v19_char_d6b_hard_accessibility_need_blocks_with_evidence_request():
    """A hard step-free need must never be satisfied by *absence* of evidence.

    This test originally asserted ``result.passed``.  That expectation is wrong
    and directly contradicts ``test_v19_traveler_capability.
    test_feasibility_blocks_unknown_and_conflicting_step_free_evidence``, which
    requires the same situation to be rejected.  Treating "no provider
    accessibility data" as a pass would let a wheelchair traveller be routed to
    a place that may have no step-free access at all, so the gate reports the
    missing evidence instead of silently accepting the plan.

    What *is* required is that the refusal is a truthful, specific evidence
    request rather than an unsupported-rule crash.
    """

    caps = extract_capabilities_from_text("我用轮椅，必须全程 step-free，一步楼梯都不能走")
    result = _gate(capability_constraints(caps))
    unsupported = [f for f in result.failures if f.startswith("UNSUPPORTED_HARD_CONSTRAINT")]
    assert not unsupported, f"the gate cannot interpret this need: {unsupported}"
    assert not result.passed, "unknown accessibility evidence must not pass as accessible"
    assert any(f.startswith("NEEDS_LIVE_EVIDENCE") for f in result.failures), (
        "the traveller must be told which evidence is missing, got "
        f"{list(result.failures)}"
    )


def test_v19_char_d6c_gate_consults_accessibility_evidence():
    """The gate imports accessibility_state() but never calls it."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "replanning" / "feasibility.py"
    text = source.read_text(encoding="utf-8")
    assert "accessibility_state(" in text.replace(
        "from app.capabilities import accessibility_state", ""
    ), "the gate never consults provider accessibility evidence"


# --------------------------------------------------------------------------- #
# D7 - the module defines TRAVELER scope but the deterministic path cannot
#      reach it, so 'always' and 'this trip only' are indistinguishable.
# --------------------------------------------------------------------------- #


def test_v19_char_d7a_language_can_select_traveler_scope():
    caps = extract_capabilities_from_text("我用轮椅，这是永久情况，不只是这次旅行")
    assert [c["scope"] for c in caps] == ["TRAVELER"]
