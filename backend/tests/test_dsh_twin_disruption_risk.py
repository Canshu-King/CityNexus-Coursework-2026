"""ORD-0432: an unresolved MAJOR disruption must not leave `robust` reading "nothing to see".

Two arms, one product mouth each side of the rule:

  * MAJOR   -> the summary names `DISRUPTION_MAJOR` and `robust` is false.
  * MINOR   -> the summary stays clean and `robust` is true, so the rule cannot be "any event".

`disruption_risk_codes` is checked separately because the ordering and the ignored severities are
the part a reader cannot see from an HTTP response.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.intelligence.feasibility_envelope import (
    UNRESOLVED_DISRUPTION_SEVERITIES,
    disruption_risk_codes,
)
from app.intelligence.reason_codes import ReasonCode
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
client = TestClient(app)
NOW = "2026-10-10T13:00:00+08:00"


@pytest.fixture(autouse=True)
def _leave_the_stage_as_found():
    """This module starts the demo journey and leaves MAJOR/MINOR events on its active plan.

    Measured on `4ee14229…`: thirteen reds with this file in the run, nine without it, while the
    same five files pass in isolation -- so the residue is read by modules further down the
    sequence that do not claim the reset door themselves. Restoring the stage after each test is
    the fixture's job; no assertion here is weakened by it.
    """
    yield
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200


def _active_journey() -> tuple[str, str]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    activity_id = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")["id"]
    return journey_id, activity_id


def _envelope(journey_id: str) -> dict:
    twin = client.get(f"/api/v1/journeys/{journey_id}/twin", params={"now": NOW}).json()
    return twin["context"]["feasibility_envelope"]


def _post_event(journey_id: str, activity_id: str, severity: str) -> None:
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": severity,
            "affected_activity_id": activity_id,
            "payload": {"reason": "venue shut for the afternoon"},
        },
    )
    assert event.status_code == 201


def test_major_closure_makes_the_summary_risky_and_names_the_severity():
    journey_id, activity_id = _active_journey()
    before = _envelope(journey_id)
    _post_event(journey_id, activity_id, "MAJOR")
    after = _envelope(journey_id)

    assert after["robust"] is False
    assert after["risk_reason_codes"] == ["DISRUPTION_MAJOR"]

    # preconditions last: the same journey read clean before the event, so the flip is the event's
    assert before["robust"] is True
    assert before["risk_reason_codes"] == []


def test_minor_note_does_not_make_the_summary_risky():
    journey_id, activity_id = _active_journey()
    before = _envelope(journey_id)
    _post_event(journey_id, activity_id, "MINOR")
    after = _envelope(journey_id)

    assert after["robust"] is True
    assert after["risk_reason_codes"] == []
    assert before["robust"] is True


def test_only_the_named_severities_become_a_risk():
    named = UNRESOLVED_DISRUPTION_SEVERITIES
    assert disruption_risk_codes(["MAJOR"]) == (named["MAJOR"].value,)
    assert disruption_risk_codes(["CRITICAL"]) == (named["CRITICAL"].value,)
    assert disruption_risk_codes(["INFO", "MINOR", "WEATHER"]) == ()
    assert disruption_risk_codes([]) == ()


def test_the_worst_open_severity_is_the_one_a_reader_sees_first():
    named = UNRESOLVED_DISRUPTION_SEVERITIES
    worst_first = (named["CRITICAL"].value, named["MAJOR"].value)
    assert disruption_risk_codes(["MAJOR", "CRITICAL"]) == worst_first
    assert disruption_risk_codes(["CRITICAL", "MAJOR"]) == worst_first
    assert disruption_risk_codes(["MAJOR", "MAJOR"]) == (named["MAJOR"].value,)


def test_the_contract_declares_exactly_the_words_the_server_can_produce():
    """One vocabulary, three artifacts: the enum, the severity table, the payload on the wire."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = set(
        contract["components"]["schemas"]["FeasibilityEnvelope"]["properties"]["risk_reason_codes"]
        ["items"]["enum"]
    )
    produced = {ReasonCode.PROVIDER_DEGRADED.value} | {
        code.value for code in UNRESOLVED_DISRUPTION_SEVERITIES.values()
    }
    assert declared == produced

    wired = contract["components"]["schemas"]["JourneyTwin"]["properties"]["context"]["properties"]
    assert wired["feasibility_envelope"] == {
        "$ref": "#/components/schemas/FeasibilityEnvelope"
    }
