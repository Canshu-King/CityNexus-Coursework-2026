from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.capabilities import (
    accessibility_state,
    canonicalize_capabilities,
    evaluate_accessibility_requirement,
    extract_capabilities_from_text,
    normalize_osm_tags,
)
from app.main import app
from app.replanning.feasibility import activity_from_record, build_context, evaluate_feasibility
from app.replanning.provider_snapshots import MobilitySnapshot, TravelEstimateFact
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(app)


def test_capability_interpretation_preserves_hard_need_and_rejects_negation():
    items = extract_capabilities_from_text("I must use step-free routes and walk less than 500 m per leg")
    assert {item["code"] for item in items} == {"STEP_FREE_REQUIRED", "MAX_WALKING_PER_LEG_M"}
    assert all(item["severity"] == "HARD" for item in items)
    assert extract_capabilities_from_text("I used to use a wheelchair, not now") == ()


def test_unknown_accessibility_is_not_accessible():
    assert accessibility_state({}) == "ACCESSIBILITY_UNVERIFIED"
    assert accessibility_state({"step_free": True}) == "ACCESSIBILITY_VERIFIED"
    assert accessibility_state({"step_free": False}) == "ACCESSIBILITY_CONFLICT"
    item = canonicalize_capabilities(
        [{"code": "STEP_FREE_REQUIRED", "class": "SOFT_PREFERENCE", "severity": "HARD", "params": {}}]
    )[0]
    assert item["class"] == "HARD_ACCESSIBILITY_NEED"
    assert item["severity"] == "HARD"


def test_feasibility_blocks_unknown_and_conflicting_step_free_evidence():
    start = datetime(2026, 9, 15, 9, tzinfo=timezone.utc)
    journey = SimpleNamespace(
        id="j1", city_code="CNX", start_at=start, end_at=datetime(2026, 9, 15, 18, tzinfo=timezone.utc),
        budget_amount_minor=10000, budget_currency="CNY",
    )
    activity = SimpleNamespace(
        key="p1", place_id="p1", city_service_id=None, start_at=start, end_at=datetime(2026, 9, 15, 10, tzinfo=timezone.utc),
        cost_minor=100, currency="CNY", locked=False,
    )
    constraint = {"kind": "HARD", "category": "HARD_ACCESSIBILITY_NEED", "rule_code": "STEP_FREE_REQUIRED", "params": {}, "active": True}
    provider = MobilitySnapshot(facts={}, source_provider_key="frozen", captured_at=start.isoformat())
    context = build_context(journey, [constraint], {"p1": {"id": "p1", "city_code": "CNX", "opening_hours": {"open": "08:00", "close": "20:00"}}}, {}, mobility_provider=provider)
    result = evaluate_feasibility(context, [activity])
    assert not result.passed
    assert "NEEDS_LIVE_EVIDENCE:p1" in result.failures


def test_capability_confirmation_endpoint_materializes_twin_evidence():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    capability = [{"code": "STEP_FREE_REQUIRED", "class": "HARD_ACCESSIBILITY_NEED", "severity": "HARD", "params": {}}]
    assert client.put(f"/api/v1/travelers/{ids['traveler_id']}/capabilities", json=capability).status_code == 409
    response = client.post(f"/api/v1/travelers/{ids['traveler_id']}/capabilities/confirm", json=capability)
    assert response.status_code == 200
    twin = response.json()["traveler_twin"]
    assert twin["explicit_capabilities"][0]["code"] == "STEP_FREE_REQUIRED"
    assert twin["explicit_capabilities"][0]["confirmed"] is True


def test_confirmation_is_upsert_and_put_is_replace():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    base = f"/api/v1/travelers/{ids['traveler_id']}/capabilities"
    a = {"code": "STROLLER", "severity": "STRONG", "params": {"present": True}}
    b = {"code": "INDOOR_PREFERRED", "severity": "SOFT", "params": {"preferred": True}}
    assert client.post(base + "/confirm", json=[a]).status_code == 200
    result = client.post(base + "/confirm", json=[b])
    assert {x["code"] for x in result.json()["capabilities"]} >= {"STROLLER", "INDOOR_PREFERRED"}
    replaced = client.put(base, json=[b])
    assert {x["code"] for x in replaced.json()["capabilities"]} == {"INDOOR_PREFERRED"}


def test_accessibility_evidence_is_requirement_scoped_and_tags_normalized():
    assert normalize_osm_tags(["toilets:wheelchair=yes", "ramp=yes"]) == {"toilets:wheelchair": "yes", "ramp": "yes"}
    assert evaluate_accessibility_requirement("STEP_FREE_REQUIRED", {"osm_tags": ["toilets:wheelchair=yes"]}) == "ACCESSIBILITY_UNVERIFIED"
    assert evaluate_accessibility_requirement("STEP_FREE_REQUIRED", {"osm_tags": ["ramp=yes"]}) == "ACCESSIBILITY_UNVERIFIED"
    assert evaluate_accessibility_requirement("STEP_FREE_REQUIRED", {"subject": "route", "osm_tags": ["route_step_free=yes"]}) == "ACCESSIBILITY_VERIFIED"
    assert evaluate_accessibility_requirement("STEP_FREE_REQUIRED", {"subject": "route", "osm_tags": ["ramp=yes"]}) == "NEEDS_LIVE_EVIDENCE"


def test_break_interval_uses_continuous_span_not_old_gap_direction():
    start = datetime(2026, 9, 15, 9, tzinfo=timezone.utc)
    journey = SimpleNamespace(id="j1", city_code="CNX", start_at=start, end_at=datetime(2026, 9, 15, 18, tzinfo=timezone.utc), budget_amount_minor=10000, budget_currency="CNY")
    activities = [
        SimpleNamespace(key="a", place_id=None, city_service_id=None, metadata={}, start_at=start, end_at=datetime(2026, 9, 15, 10, tzinfo=timezone.utc), cost_minor=0, currency="CNY", locked=False),
        SimpleNamespace(key="b", place_id=None, city_service_id=None, metadata={}, start_at=datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 15, 13, tzinfo=timezone.utc), cost_minor=0, currency="CNY", locked=False),
    ]
    constraint = {"kind": "HARD", "category": "CARE_REQUIREMENT", "rule_code": "REST_EVERY_MIN", "params": {"interval_min": 120}, "active": True}
    result = evaluate_feasibility(build_context(journey, [constraint], {}, {}), [activity_from_record(item) for item in activities])
    assert "BREAK_INTERVAL_EXCEEDED:REST_EVERY_MIN" in result.failures


def test_v1921_numeric_language_and_invalid_window_are_conservative():
    walking = extract_capabilities_from_text("走路不能超过 500 米")
    assert walking[0]["code"] == "MAX_WALKING_PER_LEG_M" and walking[0]["params"]["max_m"] == 500
    walking_km = extract_capabilities_from_text("走路不能超过 1 公里")
    assert walking_km[0]["params"]["max_m"] == 1000
    assert not extract_capabilities_from_text("I can walk 500 m to the station")
    invalid = extract_capabilities_from_text("午休必须 14:00 到 12:00")
    assert invalid and invalid[0]["params"].get("needs_clarification") is True
