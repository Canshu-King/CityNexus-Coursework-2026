"""R20 semantic trigger authority tests.

These tests intentionally exercise the shared matcher without HTTP or model
runtime noise.  Gateway integration remains the authority boundary; the pure
matcher proves that an actionable event is not a wildcard activation token.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent.standby import contract_for_simulation, standby_trigger_match


def _standby(contract):
    return SimpleNamespace(
        id="standby-weather",
        lifecycle="STANDBY",
        parent_plan_version_id="active-v1",
        mobility_evidence_json=json.dumps(
            {"expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        ),
        trigger_contract_json=json.dumps(contract),
    )


def _event(event_type, *, provenance="PROVIDER_VERIFIED", severity="MAJOR", activity="a1"):
    return SimpleNamespace(
        event_type=event_type,
        provenance=provenance,
        severity=severity,
        affected_activity_id=activity,
        payload_json=json.dumps({"place_id": "place-1"}),
    )


def _current():
    return SimpleNamespace(id="active-v1")


def test_r20_weather_branch_rejects_place_closure_event():
    contract = contract_for_simulation(
        scenario_type="WEATHER_SHIFT", dropped_activity_ids=["a1"], dropped_place_ids=["place-1"],
        simulation_id="sim-weather", parent_plan_id="active-v1"
    )
    result = standby_trigger_match(_standby(contract), _event("CLOSURE"), _current())
    assert result.compatible is False
    assert result.code == "TYPE_MISMATCH"


def test_r20_matching_weather_event_is_compatible():
    contract = contract_for_simulation(
        scenario_type="WEATHER_SHIFT", dropped_activity_ids=["a1"], dropped_place_ids=["place-1"],
        simulation_id="sim-weather", parent_plan_id="active-v1"
    )
    result = standby_trigger_match(_standby(contract), _event("WEATHER"), _current())
    assert result.compatible is True
    assert result.code == "MATCH"


def test_r20_parent_mismatch_is_not_compatible():
    contract = contract_for_simulation(
        scenario_type="PLACE_CLOSURE", dropped_activity_ids=["a1"], dropped_place_ids=["place-1"],
        simulation_id="sim-closure", parent_plan_id="active-v1"
    )
    result = standby_trigger_match(_standby(contract), _event("CLOSURE", provenance="USER_REPORTED"), SimpleNamespace(id="active-v2"))
    assert result.compatible is False
    assert result.code == "PARENT_STALE"


def test_r20_matching_closure_event_is_compatible():
    contract = contract_for_simulation(
        scenario_type="PLACE_CLOSURE", dropped_activity_ids=["a1"], dropped_place_ids=["place-1"],
        simulation_id="sim-closure", parent_plan_id="active-v1"
    )
    result = standby_trigger_match(_standby(contract), _event("CLOSURE", provenance="USER_REPORTED"), _current())
    assert result.compatible is True
    assert result.code == "MATCH"
