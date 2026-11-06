"""V19.1 capability contract tests — canonicalization, negation, dedupe, numeric, time, scope.

Targets the CURRENT V19 implementation. Written against the acceptance matrix in
the V19.1 task book, not against the implementation's internals, so a correct fix
flips a ``test_v191_defect_*`` green with no test edit.

Naming:
  ``test_v191_guard_*``   behaviour that is already correct; must not regress.
  ``test_v191_defect_*``  measured defect, locked on purpose so the fix is
                          observable as a fail -> pass flip.

Run from ``snapshot_a/backend``:
    python -m pytest tests/test_v191_capability_contract.py -q
"""

from __future__ import annotations

import pytest

from app.capabilities import (
    CAPABILITY_CODES,
    HARD_ACCESSIBILITY_CODES,
    accessibility_state,
    canonicalize_capabilities,
    capability_constraints,
    extract_capabilities_from_text,
    summarize_capabilities,
)

HARD_CODES = sorted(HARD_ACCESSIBILITY_CODES)


def codes(text: str, scope: str = "JOURNEY") -> list[str]:
    return [c["code"] for c in extract_capabilities_from_text(text, scope=scope)]


def canon(items):
    return [dict(x) for x in canonicalize_capabilities(items)]


def gate(items):
    return [g["rule_code"] for g in capability_constraints(items)]


# =========================================================================== #
# GUARDS — correct today, must not regress
# =========================================================================== #


def test_v191_guard_unknown_accessibility_is_never_verified():
    assert accessibility_state(None) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({}) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({"name": "Wheelchair ramp entrance"}) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({"evidence_status": "UNKNOWN"}) == "ACCESSIBILITY_UNVERIFIED"


def test_v191_guard_accessibility_evidence_normalisation():
    assert accessibility_state({"step_free": True}) == "ACCESSIBILITY_VERIFIED"
    assert accessibility_state({"step_free": False}) == "ACCESSIBILITY_CONFLICT"
    assert accessibility_state({"stairs": True}) == "ACCESSIBILITY_CONFLICT"


def test_v191_guard_unknown_capability_code_is_dropped():
    # canonicalize_capabilities returns a list, not a tuple.
    assert canon([{"code": "NOT_A_REAL_CODE", "severity": "HARD"}]) == []


def test_v191_guard_soft_preference_never_reaches_the_gate():
    assert capability_constraints(extract_capabilities_from_text("最好少走点楼梯，能坐电梯就坐电梯")) == []


def test_v191_guard_clean_hard_accessibility_sentence():
    caps = extract_capabilities_from_text("必须全程无障碍，一步楼梯都不能走")
    got = {c["code"] for c in caps}
    assert got == {"STEP_FREE_REQUIRED", "AVOID_STAIRS"}
    assert all(c["severity"] == "HARD" for c in caps)
    assert all(c["class"] == "HARD_ACCESSIBILITY_NEED" for c in caps)


def test_v191_guard_hard_code_class_is_corrected():
    out = canon([{"code": "STEP_FREE_REQUIRED", "severity": "HARD", "class": "SOFT_PREFERENCE"}])
    assert out[0]["class"] == "HARD_ACCESSIBILITY_NEED"


def test_v191_guard_denial_of_a_soft_preference_creates_nothing_harmful():
    # "no stroller" must not become a HARD need that blocks planning.
    assert all(
        c["severity"] != "HARD" for c in extract_capabilities_from_text("no stroller this time")
    )


# =========================================================================== #
# D1 — hard accessibility must not be downgradable by the model or an API client
# =========================================================================== #


@pytest.mark.parametrize("code", HARD_CODES)
@pytest.mark.parametrize("severity", ["HARD", "STRONG", "SOFT", None])
def test_v191_defect_d1_hard_accessibility_severity_is_not_downgradable(code, severity):
    payload: dict = {"code": code}
    if severity:
        payload["severity"] = severity
    out = canon([payload])
    assert out, f"{code} was dropped entirely for severity={severity!r}"
    assert out[0]["severity"] == "HARD", (
        f"{code} accepted severity={severity!r} -> {out[0]['severity']!r}; "
        "a hard accessibility need must be HARD regardless of the caller's label"
    )
    assert capability_constraints(out), f"{code} with severity={severity!r} never reaches the gate"


@pytest.mark.parametrize("code", HARD_CODES)
def test_v191_defect_d1_omitted_severity_defaults_to_hard(code):
    out = canon([{"code": code}])
    assert out and out[0]["severity"] == "HARD"


@pytest.mark.parametrize("code", HARD_CODES)
@pytest.mark.parametrize("severity", ["STRONG", "SOFT"])
def test_v191_defect_d1_mislabeled_class_cannot_rescue_the_downgrade(code, severity):
    out = canon([{"code": code, "severity": severity, "class": "SOFT_PREFERENCE"}])
    assert out[0]["severity"] == "HARD"


def test_v191_defect_d1_model_soft_plus_silent_extractor_loses_the_need():
    """End to end: model says SOFT, the deterministic extractor says nothing."""
    text = "我以前不用轮椅，但现在必须全程 step-free"
    local = extract_capabilities_from_text(text)
    merged = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "severity": "SOFT", "params": {}}, *local],
        source="SEMANTIC",
        provenance="USER_STATED",
    )
    assert capability_constraints(merged), "a stated hard step-free need produced no gate constraint"


# =========================================================================== #
# D2 — negation/history must be clause-local and entity-local
# =========================================================================== #


@pytest.mark.parametrize(
    "text,expected",
    [
        # a live hard need must survive a negated / historical clause elsewhere
        ("wheelchair 必须 step-free，没有例外", "STEP_FREE_REQUIRED"),
        ("我以前不用轮椅，但现在必须无障碍", "STEP_FREE_REQUIRED"),
        ("我没有过敏，但我用轮椅，必须全程无障碍", "STEP_FREE_REQUIRED"),
        ("过去我坐轮椅，现在必须全程无障碍", "STEP_FREE_REQUIRED"),
        ("不是地铁，我需要出租车因为行李很多", "LARGE_LUGGAGE"),
        ("I used to walk fine but now I use a wheelchair and need every leg step-free", "STEP_FREE_REQUIRED"),
    ],
)
def test_v191_defect_d2a_live_need_survives_negation_elsewhere(text, expected):
    assert expected in codes(text), (
        f"a live need was erased by a negation/history marker elsewhere in the sentence: {text!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "I don't need a wheelchair, I can walk",
        "朋友说我可能需要轮椅，但我不这么认为",
        "说不定到时候我需要轮椅",
        "if I end up in a wheelchair I'll tell you",
        "a friend said I might need a wheelchair, I disagree",
        "maybe I'll need a wheelchair by then",
    ],
)
def test_v191_defect_d2b_denial_hearsay_speculation_creates_no_hard_need(text):
    hard = [c["code"] for c in extract_capabilities_from_text(text) if c["severity"] == "HARD"]
    assert not hard, (
        f"a denial / hearsay / hypothetical statement manufactured a HARD capability {hard}: {text!r}"
    )


def test_v191_defect_d2c_historical_only_statement_is_silent():
    assert codes("我以前坐轮椅，现在好了") == []
    assert codes("I used to need a wheelchair but I recovered") == []


def test_v191_defect_d2d_negation_is_clause_scoped_not_text_scoped():
    """A negation in clause 1 must not silence a capability stated in clause 2."""
    text = "我今天不吃辣，但我用轮椅，必须全程无障碍"
    assert "STEP_FREE_REQUIRED" in codes(text)


# =========================================================================== #
# D3 — one code, one authoritative canonical row
# =========================================================================== #


@pytest.mark.parametrize(
    "items,expected_severity",
    [
        ([{"code": "STEP_FREE_REQUIRED", "severity": "HARD"},
          {"code": "STEP_FREE_REQUIRED", "severity": "SOFT"}], "HARD"),
        ([{"code": "STEP_FREE_REQUIRED", "severity": "SOFT"},
          {"code": "STEP_FREE_REQUIRED", "severity": "HARD"}], "HARD"),
        ([{"code": "AVOID_STAIRS", "severity": "STRONG"},
          {"code": "AVOID_STAIRS", "severity": "HARD"}], "HARD"),
        ([{"code": "AVOID_STAIRS", "severity": "SOFT"},
          {"code": "AVOID_STAIRS", "severity": "HARD"},
          {"code": "AVOID_STAIRS", "severity": "STRONG"}], "HARD"),
    ],
)
def test_v191_defect_d3_duplicate_codes_collapse_to_strongest(items, expected_severity):
    merged = canon(items)
    got = [m["code"] for m in merged]
    assert len(got) == len(set(got)), f"duplicate codes survived canonicalization: {got}"
    assert merged[0]["severity"] == expected_severity, "strongest valid severity must win"


def test_v191_defect_d3_identical_duplicates_collapse():
    merged = canon([{"code": "AVOID_STAIRS", "severity": "HARD"},
                    {"code": "AVOID_STAIRS", "severity": "HARD"}])
    assert len(merged) == 1


def test_v191_defect_d3_code_is_never_reported_as_both_hard_and_soft():
    merged = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "severity": "SOFT", "params": {}},
         *extract_capabilities_from_text("必须全程无障碍，一步楼梯都不能走")]
    )
    summary = summarize_capabilities(merged)
    both = {c["code"] for c in summary["hard"]} & {c["code"] for c in summary["soft"]}
    assert not both, f"the same capability is reported as both HARD and SOFT: {sorted(both)}"


# =========================================================================== #
# D4 — walking numeric forms
# =========================================================================== #


def test_v191_defect_d4_non_positive_walking_limit_is_not_a_usable_cap():
    """A 0 m / negative walking budget is nonsense, not a constraint.

    The raw statement is still recorded for traceability (``stated_m``), but it
    must never become a usable ``max_m``: the gate treats ``max_m`` as a hard
    per-leg budget, so persisting 0 made every leg infeasible and the whole
    Journey unplannable for that traveller.
    """

    caps = [c for c in extract_capabilities_from_text("走路不能超过 0 米") if c["code"].startswith("MAX_")]
    assert caps, "the statement must still be recorded"
    for cap in caps:
        params = cap["params"]
        assert params.get("max_m") is None, f"0 must not be a usable cap: {params}"
        assert params.get("stated_m") == 0, f"the stated value must be preserved: {params}"
        assert params.get("needs_clarification") is True, f"it must ask for clarification: {params}"


@pytest.mark.parametrize(
    "text,expected_m",
    [
        ("走路不能超过 1 米", 1),
        ("走路不能超过 100 米", 100),
        ("走路不能超过 499 米", 499),
        ("走路不能超过 500 米", 500),
        ("走路不能超过 501 米", 501),
        ("走路不能超过 1000 米", 1000),
        ("走路不能超过 10000 米", 10000),
        ("走路不能超过 500m", 500),
        ("我最多走 500 米，超过就走不动了", 500),
        ("走路不能超过 500m，这是硬性要求", 500),
        ("I cannot walk more than 500 metres per leg", 500),
        ("avoid long walks, under 400 m per leg", 400),
    ],
)
def test_v191_defect_d4_walking_limit_is_parsed_with_its_value(text, expected_m):
    caps = [c for c in extract_capabilities_from_text(text) if c["code"].startswith("MAX_")]
    assert caps, f"a stated walking limit produced no capability at all: {text!r}"
    values = [c["params"].get("max_m") for c in caps if c["params"].get("max_m") is not None]
    assert expected_m in values, f"expected max_m={expected_m}, got {values} for {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "走路不能超过 -500 米",
        "走路不能超过 99999999 米",
        "走路不能超过 null 米",
        "走路不能超过 米",
    ],
)
def test_v191_defect_d4_malformed_walking_values_are_rejected_or_clamped(text):
    """A negative / absurd / absent value must never become an authoritative limit."""
    caps = [c for c in extract_capabilities_from_text(text) if c["code"].startswith("MAX_")]
    for c in caps:
        value = c["params"].get("max_m")
        assert value is None or 0 <= value <= 100_000, (
            f"malformed value became an authoritative limit: {c['params']} from {text!r}"
        )
    if caps:
        # If a capability is produced it must be flagged as needing clarification,
        # not silently accepted with a usable number.
        assert any(c["params"].get("needs_clarification") for c in caps), (
            f"malformed walking value produced a clean limit: {caps}"
        )


def test_v191_defect_d4_distinct_values_do_not_collapse():
    values = set()
    for text in ("走路不能超过 100 米", "走路不能超过 500 米", "走路不能超过 1000 米"):
        caps = [c for c in extract_capabilities_from_text(text) if c["code"].startswith("MAX_")]
        values.update(c["params"].get("max_m") for c in caps)
    assert len(values) >= 3, f"distinct walking limits collapsed to one outcome: {values}"


def test_v191_defect_d4_km_form_is_converted_or_rejected():
    caps = [c for c in extract_capabilities_from_text("走路不能超过 1 公里") if c["code"].startswith("MAX_")]
    assert caps, "a kilometre-denominated walking limit produced no capability"
    value = caps[0]["params"].get("max_m")
    assert value in (1000, None), f"1 公里 became max_m={value}"


# =========================================================================== #
# D5 — rest / nap time windows
# =========================================================================== #


@pytest.mark.parametrize(
    "text,code",
    [
        ("老人中午 12 点到 14 点必须回酒店休息", "MIDDAY_REST_WINDOW"),
        ("中午 12:00-14:00 必须回酒店休息", "MIDDAY_REST_WINDOW"),
        ("宝宝下午 1 点到 3 点必须睡午觉，不能安排活动", "INFANT_NAP_WINDOW"),
        ("the baby needs naptime at 13:00-15:00", "INFANT_NAP_WINDOW"),
        ("I must rest at the hotel between 12:00 and 14:00", "MIDDAY_REST_WINDOW"),
    ],
)
def test_v191_defect_d5_rest_and_nap_windows_are_extracted(text, code):
    assert code in codes(text), f"a stated rest/nap window produced no capability: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "中午 12 点到 14 点必须回酒店休息",
        "I must rest between 12:00 and 14:00",
    ],
)
def test_v191_defect_d5_window_params_are_well_formed(text):
    caps = [c for c in extract_capabilities_from_text(text)
            if c["code"] in {"MIDDAY_REST_WINDOW", "INFANT_NAP_WINDOW"}]
    assert caps, f"no window capability extracted from {text!r}"
    for c in caps:
        start, end = c["params"].get("start"), c["params"].get("end")
        assert start and end, f"window has no start/end: {c['params']}"
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
        assert (eh * 60 + em) > (sh * 60 + sm), f"window end is not after start: {c['params']}"


def test_v191_defect_d5_invalid_window_is_not_silently_accepted():
    """14:00 -> 12:00 is invalid; it must not become a usable window."""
    caps = [c for c in extract_capabilities_from_text("中午 14:00 到 12:00 必须回酒店休息")
            if c["code"] in {"MIDDAY_REST_WINDOW", "INFANT_NAP_WINDOW"}]
    for c in caps:
        start, end = c["params"].get("start"), c["params"].get("end")
        if start and end:
            sh, sm = (int(x) for x in str(start).split(":"))
            eh, em = (int(x) for x in str(end).split(":"))
            assert (eh * 60 + em) > (sh * 60 + sm), (
                f"an end-before-start window was accepted as valid: {c['params']}"
            )


# =========================================================================== #
# D7 — scope from language
# =========================================================================== #


@pytest.mark.parametrize(
    "text",
    [
        "我用轮椅，这是永久情况，不只是这次旅行",
        "以后每次旅行我都不能走太多路",
        "不只是这次，我一直用轮椅",
        "permanently, every trip, I need step-free access",
    ],
)
def test_v191_defect_d7_permanent_language_selects_traveler_scope(text):
    caps = extract_capabilities_from_text(text)
    assert caps, f"no capability extracted from {text!r}"
    assert [c["scope"] for c in caps] == ["TRAVELER"], (
        f"permanent / every-trip language did not select TRAVELER scope: {[c['scope'] for c in caps]}"
    )


@pytest.mark.parametrize(
    "text",
    ["这次旅行我不能走太多路", "今天临时腿疼，少走点", "just for today I cannot walk far"],
)
def test_v191_defect_d7_this_trip_language_selects_journey_scope(text):
    caps = extract_capabilities_from_text(text)
    assert caps, f"no capability extracted from {text!r}"
    assert all(c["scope"] == "JOURNEY" for c in caps)


def test_v191_defect_d7_scope_is_distinguishable_from_language():
    """If scope cannot be inferred, the contract must say so rather than guess JOURNEY."""
    permanent = extract_capabilities_from_text("我用轮椅，这是永久情况，不只是这次旅行")
    trip = extract_capabilities_from_text("这次旅行我不能走太多路")
    assert [c["scope"] for c in permanent] != [c["scope"] for c in trip], (
        "permanent and this-trip-only language are indistinguishable"
    )
