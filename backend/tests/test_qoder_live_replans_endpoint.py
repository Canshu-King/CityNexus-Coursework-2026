"""T2 v5 blocker-1 — REAL /replans path with a live provider configured.

V4 closed the *primitive* (`require_explicit_facts` hard capture never falls back
to mock) and the *offline* endpoint (no explicit fact -> refused), but GPT found
the real bypass: ``generate_replan`` does its initial BULK mobility capture
through the *soft* runtime path.  With a live routing provider configured, a leg
the live provider failed would fall back to the deterministic mock, and if that
leg happened to be an explicit demo-matrix pair it was tagged
``EXPLICIT_FROZEN_FIXTURE`` -> accepted by ``hard_minutes()`` -> the later strict
per-pair recapture never fired.  Two tests each green, the composition leaky.

These drive the actual ``POST /api/v1/journeys/{id}/replans`` with live routing
configured and a fake Valhalla that fails specific legs the mock still has as
explicit fixture facts, proving the bypass is closed and candidate behaviour is
preserved.

Layer label: API/service integration (live-provider composition).
"""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

import app.integrations.mobility.runtime as runtime
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

# The two legs each golden same-slot replacement candidate needs (both are
# explicit entries in data/mock/mobility_matrix.json — exactly the fixture values
# that used to be laundered into a hard fact when the live provider failed).
_CITY_PARK_LEGS = ("place-food-hall|place-city-park", "place-city-park|place-river-cruise")
_DESIGN_LEGS = ("place-food-hall|place-design-library", "place-design-library|place-river-cruise")

# Mutable switch so the provider can be healthy during setup (so a plan exists)
# and degrade only at the replan call, mirroring a live outage at incident time.
_LIVE_STATE: dict = {"failing": set(), "fail_all": False}


class _FakeValhalla:
    provider_key = "valhalla"

    def __init__(self, **_kwargs):
        pass

    def status_capabilities(self):
        return {"status": "OK", "has_live_traffic": False}

    def estimate_minutes(self, origin_key, destination_key):
        if _LIVE_STATE["fail_all"] or (origin_key, destination_key) in _LIVE_STATE["failing"]:
            raise RuntimeError("live routing unavailable for leg")
        return 12

    def fact_quality(self, origin_key, destination_key):
        return "LIVE_PROVIDER_CAPTURE"


def _install_live(monkeypatch):
    _LIVE_STATE["failing"] = set()
    _LIVE_STATE["fail_all"] = False
    monkeypatch.setattr(
        runtime, "settings", types.SimpleNamespace(live_valhalla_url="http://valhalla.test")
    )
    monkeypatch.setattr(
        runtime, "ValhallaHTTPMobilityProvider", lambda **kwargs: _FakeValhalla(**kwargs)
    )


def _degrade(*legs, all_legs=False):
    _LIVE_STATE["failing"] = {tuple(p.split("|")) for p in legs}
    _LIVE_STATE["fail_all"] = all_legs


def _drive_to_closure():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    generated = client.post(f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1})
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "v5 live endpoint"},
        },
    )
    assert event.status_code == 201, event.text
    return jid, event.json()["id"]


def _replan(jid, event_id, count=3):
    return client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event_id, "candidate_count": count},
    )


def test_live_provider_healthy_control_produces_proposal(monkeypatch) -> None:
    _install_live(monkeypatch)
    jid, event = _drive_to_closure()
    assert _replan(jid, event).status_code == 201


def test_L1_live_failure_on_mock_explicit_leg_is_not_rescued(monkeypatch) -> None:
    # city-park's legs fail live but EXIST as explicit mock matrix facts.  design-
    # library's legs are fine on live.  The mock must not rescue city-park: the
    # candidate is excluded even though the matrix has a number for it, while the
    # fully-live candidate still produces a proposal (candidate-scoped).
    _install_live(monkeypatch)
    jid, event = _drive_to_closure()
    _degrade(*_CITY_PARK_LEGS)
    response = _replan(jid, event)
    assert response.status_code == 201, response.text
    proposals = client.get(f"/api/v1/journeys/{jid}/replans").json()
    added = {str((p.get("diff") or {}).get("added")) for p in proposals}
    assert not any("place-city-park" in a for a in added), (
        "a live-failed candidate was rescued by an explicit mock fallback: " + str(added)
    )


def test_L3_all_candidates_unavailable_reports_data_not_no_alternative(monkeypatch) -> None:
    # Provider fully degraded: the failure is missing travel evidence, NOT
    # "no replacement place exists" (REPLAN_NO_ALTERNATIVE).
    _install_live(monkeypatch)
    jid, event = _drive_to_closure()
    _degrade(all_legs=True)
    response = _replan(jid, event)
    assert response.status_code == 422, response.text
    assert "MOBILITY_DATA_UNAVAILABLE" in response.text, response.text
    assert "NO_ALTERNATIVE" not in response.text, response.text


def test_L2_fully_live_candidate_survives_when_another_is_live_failed(monkeypatch) -> None:
    # design-library's legs fail live (mock has them explicit); city-park's legs
    # succeed on live -> 201 offering city-park only.
    _install_live(monkeypatch)
    jid, event = _drive_to_closure()
    _degrade(*_DESIGN_LEGS)
    response = _replan(jid, event)
    assert response.status_code == 201, response.text
    proposals = client.get(f"/api/v1/journeys/{jid}/replans").json()
    added = {str((p.get("diff") or {}).get("added")) for p in proposals}
    assert any("place-city-park" in a for a in added), "trustworthy live candidate dropped: " + str(added)
    assert not any("place-design-library" in a for a in added), (
        "a live-failed candidate was rescued by an explicit mock fallback: " + str(added)
    )


def test_L4_accepted_hard_evidence_contains_no_mock_fallback(monkeypatch) -> None:
    from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, LIVE_PROVIDER_CAPTURE

    HARD_ELIGIBLE = {EXPLICIT_FROZEN_FIXTURE, LIVE_PROVIDER_CAPTURE}
    _install_live(monkeypatch)
    jid, event = _drive_to_closure()
    _degrade(*_DESIGN_LEGS)
    assert _replan(jid, event).status_code == 201
    proposals = client.get(f"/api/v1/journeys/{jid}/replans").json()
    accepted = [p for p in proposals if "place-city-park" in str((p.get("diff") or {}).get("added"))]
    assert accepted, proposals
    facts = accepted[0]["diff"]["verified_evidence"]["mobility"]["snapshot"]["facts"]
    # No mock-sourced fact may carry a hard-eligible quality in the accepted evidence.
    laundered = [
        f for f in facts
        if f.get("provider") == "mock-mobility" and f.get("fact_quality") in HARD_ELIGIBLE
    ]
    assert not laundered, (
        "a live-failed leg was laundered into hard truth via an explicit mock fallback: "
        + str([(f.get("origin_key"), f.get("destination_key")) for f in laundered])
    )
    # Every city-park leg that the accepted decision relies on must be live-measured.
    city_park_facts = [
        f for f in facts
        if "place-city-park" in (f.get("origin_key"), f.get("destination_key"))
    ]
    assert city_park_facts and all(f.get("provider") == "valhalla" for f in city_park_facts), city_park_facts
