"""V19.2 Real-Phone Shadow-Trial Gate — contract tests.

Convention (identical to the V19.1 tests patch):

  ``test_v192_guard_*``   behaviour that is already correct; must not regress.
  ``test_v192_defect_*``  a still-open defect, locked on purpose so the fix is
                          observable as a fail -> pass flip.

Everything runs in-process against a throwaway SQLite database in the system
temp directory.  No production module is modified and nothing is written into
the repository.

Run from the repository root:
    cd backend && python -m pytest tests/test_v192_shadow_trial_gate.py -q

Evidence for every assertion: see the round's reports at the lab root —
P0_GATE_TABLE.md, NUMERIC_TIME_BOUNDARY_RESULTS.md,
CAPABILITY_SEMANTICS_RESULTS.md, C4_ANCHOR_AND_IDEMPOTENCY_RESULTS.md,
MIDTRIP_AND_STALE_LIFECYCLE_RESULTS.md, REGRESSION_LIST.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import v192_harness as H  # noqa: E402


# ===========================================================================
# GUARDS — raw language must not invent a need for the traveller
# ===========================================================================
THIRD_PARTY = [
    "my friend needs a wheelchair, not me",
    "my mother uses a wheelchair",
    "我朋友需要轮椅",
    "我妈妈坐轮椅",
    "I am not a wheelchair user",
]

FACILITY_DESCRIPTION = [
    "the station has an accessible toilet",
    "车站有无障碍厕所",
]

SPECULATION = [
    "说不定到时候我需要轮椅",
    "if I end up in a wheelchair I'll tell you",
]

MANDATED = [
    "I need a wheelchair, I cannot walk",
    "我全程需要无障碍通道，必须有电梯",
]


@pytest.mark.parametrize("text", THIRD_PARTY)
def test_v192_guard_third_party_subject_is_not_hard(text):
    items = H.extract(text)
    assert "STEP_FREE_REQUIRED" not in H.codes(items), (
        f"a third party's need must not become the traveller's HARD need: {text} -> {items}"
    )


@pytest.mark.parametrize("text", FACILITY_DESCRIPTION)
def test_v192_guard_facility_description_is_not_hard(text):
    items = H.extract(text)
    assert "STEP_FREE_REQUIRED" not in H.codes(items), (
        f"describing a facility must not assert a personal need: {text} -> {items}"
    )


@pytest.mark.parametrize("text", SPECULATION)
def test_v192_guard_speculation_is_not_hard(text):
    items = H.extract(text)
    assert "STEP_FREE_REQUIRED" not in H.codes(items), (
        f"speculation must not be promoted to a HARD need: {text} -> {items}"
    )


@pytest.mark.parametrize("text", MANDATED)
def test_v192_guard_mandated_need_stays_hard(text):
    items = H.extract(text)
    assert "STEP_FREE_REQUIRED" in H.codes(items), (
        f"an explicit, unconditional need was over-filtered: {text} -> {items}"
    )


# ===========================================================================
# GUARDS — scope-aware canonicalization
# ===========================================================================
def test_v192_guard_scope_dedup_keeps_both_rows():
    out = H.canon(
        [
            {"code": "AVOID_STAIRS", "severity": "HARD", "scope": "TRAVELER", "params": {"note": "traveler"}},
            {"code": "AVOID_STAIRS", "severity": "HARD", "scope": "JOURNEY", "params": {"note": "journey"}},
        ]
    )
    assert sorted(i["scope"] for i in out) == ["JOURNEY", "TRAVELER"], out
    assert {i["params"].get("note") for i in out} == {"traveler", "journey"}, out


def test_v192_guard_duplicate_merge_takes_stricter_bound():
    out = H.canon(
        [
            {"code": "MAX_WALKING_PER_LEG_M", "severity": "HARD", "scope": "JOURNEY", "params": {"max_m": 800}},
            {"code": "MAX_WALKING_PER_LEG_M", "severity": "HARD", "scope": "JOURNEY", "params": {"max_m": 400}},
        ]
    )
    assert len(out) == 1, out
    assert out[0]["params"]["max_m"] == 400, out


# ===========================================================================
# GUARDS — provider evidence scope
# ===========================================================================
FACILITY_TAGS = [
    ("toilets:wheelchair=yes", {"toilets:wheelchair": "yes"}),
    ("ramp=yes", {"ramp": "yes"}),
    ("elevator=yes", {"elevator": "yes"}),
    ("ramp:wheelchair=yes", {"ramp:wheelchair": "yes"}),
    ("highway=elevator", {"highway": "elevator"}),
    ("kerb=lowered", {"kerb": "lowered"}),
    ("tactile_paving=yes", {"tactile_paving": "yes"}),
    ("entrance=yes", {"entrance": "yes"}),
    ("wheelchair=yes", {"wheelchair": "yes"}),
    ("wheelchair=designated", {"wheelchair": "designated"}),
    ("wheelchair=limited", {"wheelchair": "limited"}),
    ("wheelchair=no", {"wheelchair": "no"}),
]


@pytest.mark.parametrize("name,tags", FACILITY_TAGS, ids=[n for n, _ in FACILITY_TAGS])
def test_v192_guard_facility_tags_never_verify_step_free(name, tags):
    state = H.requirement_state("STEP_FREE_REQUIRED", {"osm_tags": tags})
    assert state != "ACCESSIBILITY_VERIFIED", (
        f"{name} must not satisfy STEP_FREE_REQUIRED (got {state})"
    )


def test_v192_guard_route_without_evidence_stays_unknown():
    assert (
        H.requirement_state("STEP_FREE_REQUIRED", {"subject": "route"}) == "NEEDS_LIVE_EVIDENCE"
    )
    assert (
        H.requirement_state(
            "STEP_FREE_REQUIRED", {"subject": "route", "osm_tags": {"wheelchair": "yes"}}
        )
        == "NEEDS_LIVE_EVIDENCE"
    )


POSITIVE_EVIDENCE = [
    ("step_free: yes", {"step_free": "yes"}),
    ("step_free_verified: yes", {"step_free_verified": "yes"}),
    ("osm_tags step_free_verified", {"osm_tags": {"step_free_verified": "yes"}}),
    ("route_step_free_verified", {"subject": "route", "route_step_free_verified": "yes"}),
]


@pytest.mark.parametrize("name,facts", POSITIVE_EVIDENCE, ids=[n for n, _ in POSITIVE_EVIDENCE])
def test_v192_guard_explicit_step_free_evidence_verifies(name, facts):
    assert H.requirement_state("STEP_FREE_REQUIRED", facts) == "ACCESSIBILITY_VERIFIED", name


def test_v192_guard_tag_container_shapes_survive():
    assert H.normalize_tags({"wheelchair": "yes"}) == {"wheelchair": "yes"}
    assert H.normalize_tags({"wheelchair": ["yes"]}) == {"wheelchair": "yes"}
    assert H.normalize_tags({"ramp": ("yes",)}) == {"ramp": "yes"}
    assert H.normalize_tags(["wheelchair=yes", "ramp=yes"]) == {"wheelchair": "yes", "ramp": "yes"}


def test_v192_guard_forward_time_window_is_accepted():
    params = H.params_of(H.extract("午休 12:00 到 14:00"), "MIDDAY_REST_WINDOW")
    assert params.get("start") == "12:00" and params.get("end") == "14:00", params


def test_v192_guard_reversed_time_window_is_rejected():
    params = H.params_of(H.extract("午休 15:00 到 13:00"), "MIDDAY_REST_WINDOW")
    assert (
        params == {}
        or params.get("needs_clarification") is True
        or params.get("defaulted_window") is True
    ), f"a reversed window was persisted as clean data: {params}"


# ===========================================================================
# GUARDS — HTTP lifecycle
# ===========================================================================
def test_v192_guard_no_5xx_on_the_happy_path():
    H.reset_demo()
    safe = H.safe_client()
    jid = H.DEMO_JOURNEY_ID
    gen = safe.post(f"/api/v1/journeys/{jid}/plans/generate", json={})
    assert gen.status_code < 400, gen.text
    plans = H.list_plans()
    assert plans, "generate produced no plans"
    sel = safe.post(f"/api/v1/journeys/{jid}/plans/{plans[0]['id']}/select")
    assert sel.status_code < 400, sel.text
    st = safe.post(f"/api/v1/journeys/{jid}/start")
    assert st.status_code < 500, st.text


def test_v192_guard_c4_confirmation_is_idempotent():
    H.reset_demo()
    jid = H.DEMO_JOURNEY_ID
    reservation = H.c4_reservation(
        confirmation_ref="C4-GUARD-IDEMPOTENT",
        departure_at="2026-10-10T08:00:00+08:00",
        arrival_at="2026-10-10T12:30:00+08:00",
    )
    d1 = H.inbox_import(jid, reservation)
    d2 = H.inbox_import(jid, reservation)
    assert d1.status_code in (200, 201), d1.text
    assert d2.status_code in (200, 201), d2.text
    c1 = H.inbox_confirm(jid, d1.json()["id"])
    assert c1.status_code == 200, c1.text
    assert len(H.commitment_rows()) == 1
    H.inbox_confirm(jid, d1.json()["id"])          # same draft twice
    assert len(H.commitment_rows()) == 1, "re-confirming the same draft duplicated the commitment"
    H.inbox_confirm(jid, d2.json()["id"])          # sibling draft, same reference
    assert len(H.commitment_rows()) == 1, "a sibling draft duplicated the commitment"


def test_v192_guard_stale_candidate_is_refused():
    H.reset_demo()
    jid = H.DEMO_JOURNEY_ID
    H.generate_plans()
    plans = H.list_plans()
    assert len(plans) >= 2, f"need >=2 plans, got {len(plans)}"
    plan_id = plans[1]["id"]
    original = H.expire_plan_evidence(plan_id)
    try:
        r = H.select_plan(jid, plan_id)
        assert r.status_code in (409, 422), (
            f"a candidate on expired frozen evidence was selectable: {r.status_code} {r.text[:200]}"
        )
    finally:
        H.set_plan_evidence_json(plan_id, original)


def test_v192_guard_generator_and_validator_agree():
    H.reset_demo()
    H.generate_plans()
    plans = H.list_plans()
    assert plans, "generate produced no plans"
    from app.db.models import Journey as JourneyModel
    from app.db.models import PlanVersion

    rejected = []
    with H._session() as session:
        journey_row = session.get(JourneyModel, H.DEMO_JOURNEY_ID)
        for plan in plans:
            plan_row = session.get(PlanVersion, plan["id"])
            verdict = H.validate_plan(session, journey_row, plan_row)
            if not getattr(verdict, "passed", False):
                rejected.append((plan["id"], list(getattr(verdict, "failures", []) or [])))
    assert rejected == [], f"the generator offered plans the validator rejects: {rejected}"


# ===========================================================================
# DEFECTS — numeric truth
# ===========================================================================
def test_v192_defect_decimal_kilometre_limit_is_not_corrupted():
    for text in ("走路不能超过 1.5 公里", "走路不能超过 1.5 km"):
        got = H.params_of(H.extract(text), "MAX_WALKING_PER_LEG_M").get("max_m", "MISSING")
        assert got == 1500, f"{text} -> max_m={got!r}, expected 1500"


def test_v192_defect_sub_kilometre_limit_is_not_corrupted():
    got = H.params_of(H.extract("走路不能超过 0.5 公里"), "MAX_WALKING_PER_LEG_M").get(
        "max_m", "MISSING"
    )
    assert got == 500, f"走路不能超过 0.5 公里 -> max_m={got!r}, expected 500"


def test_v192_defect_minutes_value_does_not_become_a_metres_cap():
    items = H.extract("连续走不能超过 20 分钟")
    metres = H.params_of(items, "MAX_WALKING_PER_LEG_M").get("max_m")
    assert metres != 20, (
        "a 20-minute duration was persisted as a 20-metre distance cap "
        f"(extracted codes={H.codes(items)})"
    )


def test_v192_defect_daily_limit_is_not_also_a_per_leg_limit():
    items = H.extract("我每天走路不能超过 3000 米")
    assert "MAX_WALKING_PER_LEG_M" not in H.codes(items), (
        f"a daily walking budget was also published as a per-leg cap: {items}"
    )


def test_v192_defect_zero_metre_limit_is_not_authoritative():
    params = H.params_of(H.extract("走路不能超过 0 米"), "MAX_WALKING_PER_LEG_M")
    assert not (params.get("max_m") == 0 and not params.get("needs_clarification")), (
        f"a local fallback persisted `0 m` as a clean authoritative cap: {params}"
    )


NATURAL_PHRASINGS = [
    "每段不超过 100 米",
    "单段步行不超过 100 米",
    "每次走路不超过 100 米",
    "不要走超过 100 米",
]


@pytest.mark.parametrize("text", NATURAL_PHRASINGS)
def test_v192_defect_natural_walking_phrasing_reaches_the_parser(text):
    got = H.params_of(H.extract(text), "MAX_WALKING_PER_LEG_M").get("max_m", "MISSING")
    assert got != "MISSING", f"no per-leg walking limit was produced for: {text}"


# ===========================================================================
# DEFECTS — safety-relevant needs silently dropped
# ===========================================================================
HEDGED_FIRST_PERSON = ["我可能需要轮椅", "maybe I need a wheelchair"]

COMPANION_NEEDS = ["我妈妈要午休", "我父亲走不了远路"]

CONDITIONAL_STEP_FREE = [
    "如果可能的话我需要无障碍通道",
    "if possible I need step-free access",
]


@pytest.mark.parametrize("text", HEDGED_FIRST_PERSON)
def test_v192_defect_hedged_first_person_need_is_not_silently_dropped(text):
    items = H.extract(text)
    assert items, f"a hedged first-person accessibility need produced nothing: {text}"


@pytest.mark.parametrize("text", COMPANION_NEEDS)
def test_v192_defect_companion_need_is_not_silently_dropped(text):
    items = H.extract(text)
    assert items, f"a travelling companion's need produced nothing: {text}"


@pytest.mark.parametrize("text", CONDITIONAL_STEP_FREE)
def test_v192_defect_conditional_step_free_request_is_not_silently_dropped(text):
    items = H.extract(text)
    assert items, f"a conditional step-free request produced nothing: {text}"


# ===========================================================================
# DEFECTS — evidence interpretation
# ===========================================================================
def test_v192_defect_evidence_status_verified_is_honoured():
    state = H.requirement_state("STEP_FREE_REQUIRED", {"evidence_status": "VERIFIED"})
    assert "UNVERIFIED" not in state, (
        f"provider evidence_status=VERIFIED was reported as {state}"
    )


def test_v192_defect_nested_step_count_is_a_conflict():
    state = H.requirement_state(
        "STEP_FREE_REQUIRED", {"osm_tags": {"wheelchair": "yes", "step_count": "5"}}
    )
    assert state == "ACCESSIBILITY_CONFLICT", (
        f"wheelchair=yes beside step_count=5 (nested) was reported as {state}"
    )


def test_v192_defect_flat_steps_tag_is_a_conflict():
    state = H.requirement_state("STEP_FREE_REQUIRED", {"wheelchair": "yes", "steps": "yes"})
    assert state == "ACCESSIBILITY_CONFLICT", (
        f"wheelchair=yes beside steps=yes was reported as {state}"
    )


def test_v192_defect_nap_window_is_flagged_when_times_are_invented():
    params = H.params_of(H.extract("宝宝需要午睡"), "INFANT_NAP_WINDOW")
    assert params.get("defaulted_window") or params.get("needs_clarification"), (
        f"an invented nap window was persisted as clean data: {params}"
    )


# ===========================================================================
# DEFECTS — planning truth
# ===========================================================================
C4_ANCHORS = [
    ("ARRIVAL", "2026-10-10T12:30:00+08:00", None),
    ("DEPARTURE", "2026-10-10T12:30:00+08:00", None),
    ("MID_TRIP", "2026-10-10T12:30:00+08:00", "place-old-town"),
]


@pytest.mark.parametrize(
    "role,arrival,node", C4_ANCHORS, ids=[f"{r}-{n or 'synthetic'}" for r, _, n in C4_ANCHORS]
)
def test_v192_defect_c4_anchor_does_not_block_all_plans(role, arrival, node):
    H.reset_demo()
    jid = H.DEMO_JOURNEY_ID
    imp = H.inbox_import(
        jid,
        H.c4_reservation(
            confirmation_ref=f"C4-DEFECT-{role}",
            departure_at="2026-10-10T08:00:00+08:00",
            arrival_at=arrival,
            anchor_role=role,
            semantic_node_key=node,
        ),
    )
    assert imp.status_code in (200, 201), imp.text
    conf = H.inbox_confirm(jid, imp.json()["id"])
    assert conf.status_code == 200, conf.text
    status, body = H.generate_plans()
    plans = H.list_plans()
    assert plans, (
        f"a confirmed {role} anchor left the journey with zero plans "
        f"(generate={status}, body={json.dumps(body, ensure_ascii=False)[:240]})"
    )


def test_v192_defect_start_refuses_expired_evidence():
    H.reset_demo()
    jid = H.DEMO_JOURNEY_ID
    H.generate_plans()
    plans = H.list_plans()
    assert plans, "generate produced no plans"
    plan_id = plans[0]["id"]
    assert H.select_plan(jid, plan_id).status_code < 400
    original = H.expire_plan_evidence(plan_id)
    try:
        r = H.start_journey(jid)
        status = H.journey(jid).get("status")
        assert not (r.status_code == 200 and status == "ACTIVE"), (
            "a journey started and stayed ACTIVE on expired frozen mobility evidence "
            f"(start={r.status_code}, journey={status})"
        )
    finally:
        H.set_plan_evidence_json(plan_id, original)


def test_v192_defect_midtrip_hard_need_produces_a_review():
    H.reset_demo()
    H.clear_capabilities()
    H.set_place_evidence({"step_free": True, "wheelchair_access": True})
    jid = H.DEMO_JOURNEY_ID
    H.generate_plans()
    plans = H.list_plans()
    assert plans, "generate produced no plans"
    assert H.select_plan(jid, plans[0]["id"]).status_code < 400
    assert H.start_journey(jid).status_code < 400
    before = H.plan_signatures()
    r = H.confirm_journey_capabilities([H.capability("STEP_FREE_REQUIRED", "HARD")])
    assert r.status_code in (200, 201), r.text
    after = H.plan_signatures()
    assert before == after, "plans were silently rewritten under the traveller mid-trip"
    replans = H.list_replans()
    blob = json.dumps(H.experience(), ensure_ascii=False).lower()
    assert replans or "review" in blob, (
        "a mid-trip HARD need produced neither a replan proposal nor a review signal"
    )
