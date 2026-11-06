from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.mobility.snapshot_capture import capture_mobility_snapshot
from app.integrations.mock.providers import MockMobilityProvider
from app.replanning.provider_snapshots import (
    LIVE_PROVIDER_CAPTURE,
    MobilitySnapshot,
    TravelEstimateFact,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


class _Provider:
    def __init__(self, key: str, *, fail: set[tuple[str, str]] | None = None):
        self.provider_key = key
        self.fail = fail or set()
        self.mock = MockMobilityProvider()

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        pair = (origin_key, destination_key)
        if pair in self.fail:
            raise RuntimeError("provider unavailable")
        return self.mock.estimate_minutes(origin_key, destination_key)

    def fact_quality(self, origin_key: str, destination_key: str) -> str:
        # Provider-contract double: it stands for a healthy capturing routing
        # provider (valhalla primary, or its degraded fallback), which attests
        # LIVE for a measurement, mirroring app.integrations.mobility.
        # valhalla_http.fact_quality.  A failed pair raises so the capture falls
        # back rather than fabricating (V4 #2: doubles declare their own honest
        # quality instead of relying on provider-name laundering).
        if (origin_key, destination_key) in self.fail:
            raise RuntimeError("provider unavailable")
        return LIVE_PROVIDER_CAPTURE


def _prepare_disruption() -> tuple[str, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    generated = client.post(f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1})
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text

    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]})
    assert booking.status_code == 201, booking.text
    booking_body = booking.json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking_body["id"],
            "transaction_type": "CAPTURE",
            "amount": booking_body["amount"],
        },
        headers={"Idempotency-Key": f"gpt-capture-{uuid4()}"},
    )
    assert capture.status_code == 201, capture.text
    started = client.post(f"/api/v1/journeys/{jid}/start")
    assert started.status_code == 200, started.text

    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        completed = client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
        assert completed.status_code == 200, completed.text

    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "gpt provider hardening"},
        },
    )
    assert event.status_code == 201, event.text
    return jid, event.json()


def _generate_with_partial_fallback(monkeypatch):
    jid, event = _prepare_disruption()
    primary = _Provider("valhalla", fail={("place-tech-art", "place-river-cruise")})
    fallback = _Provider("mock-mobility")

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        return capture_mobility_snapshot(
            pairs,
            primary=primary,
            fallback=fallback,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", capture_runtime)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
        headers={"Idempotency-Key": f"gpt-replan-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    return jid, proposals


def test_snapshot_facts_are_structurally_immutable():
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="valhalla",
        minutes={("a", "b"): 10},
        operation_key="replan:event-1",
    )
    original = snapshot.estimate_minutes("a", "b")
    with pytest.raises(TypeError):
        snapshot.facts[("a", "b")] = TravelEstimateFact(
            origin_key="a",
            destination_key="b",
            minutes=999,
            provider_key="evil",
            captured_at=snapshot.captured_at,
        )
    assert snapshot.estimate_minutes("a", "b") == original


def test_twin_reports_effective_provider_for_fallback_pair(monkeypatch):
    jid, proposals = _generate_with_partial_fallback(monkeypatch)
    assert proposals
    mobility = proposals[0]["diff"]["verified_evidence"]["mobility"]
    assert mobility["degraded"] is True
    facts = {
        (item["origin_key"], item["destination_key"]): item
        for item in mobility["snapshot"]["facts"]
    }
    assert facts[("place-tech-art", "place-river-cruise")]["provider"] == "mock-mobility"

    twin = client.get(
        f"/api/v1/journeys/{jid}/twin",
        params={"now": "2026-10-10T13:25:00+08:00"},
    )
    assert twin.status_code == 200, twin.text
    ctx = twin.json()["context"]["mobility"]
    assert ctx["provider"] == "mock-mobility"
    assert ctx["source_provider"] == "valhalla"
    assert ctx["degraded"] is True


def test_degraded_mobility_reduces_robustness_evidence_and_score(monkeypatch):
    _jid, proposals = _generate_with_partial_fallback(monkeypatch)
    assert proposals
    for proposal in proposals:
        diff = proposal["diff"]
        mobility = diff["verified_evidence"]["mobility"]
        if not mobility["degraded"]:
            continue
        envelope = diff["after_feasibility_envelope"]
        assert "valhalla" in envelope["degraded_providers"]
        assert "PROVIDER_DEGRADED" in envelope["risk_reason_codes"]
        assert envelope["robust"] is False
        assert proposal["score_breakdown"]["robustness"] == 0.0
        break
    else:
        raise AssertionError("expected at least one degraded proposal")


class _ConstantProvider:
    provider_key = "valhalla"

    def __init__(self, minutes: int):
        self.minutes = minutes

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        return 0 if origin_key == destination_key else self.minutes

    def fact_quality(self, origin_key: str, destination_key: str) -> str:
        # Healthy capturing routing provider (valhalla): attests LIVE for each
        # measurement, mirroring app.integrations.mobility.valhalla_http.
        return LIVE_PROVIDER_CAPTURE


def _snapshot_route_total(plan: dict, snapshot_payload: dict) -> int:
    facts = {
        (item["origin_key"], item["destination_key"]): int(item["minutes"])
        for item in snapshot_payload["facts"]
    }
    activities = sorted(plan["activities"], key=lambda item: item["sequence_no"])
    total = 0
    for previous, current in zip(activities, activities[1:]):
        origin = previous.get("place_id") or previous.get("city_service_id")
        destination = current.get("place_id") or current.get("city_service_id")
        if not origin or not destination or origin == destination:
            continue
        total += facts[(origin, destination)]
    return total


def test_travel_delta_compares_base_and_candidate_under_same_frozen_snapshot(monkeypatch):
    jid, event = _prepare_disruption()
    primary = _ConstantProvider(1)

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        return capture_mobility_snapshot(
            pairs,
            primary=primary,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", capture_runtime)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
        headers={"Idempotency-Key": f"gpt-delta-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    assert proposals
    for proposal in proposals:
        snapshot = proposal["diff"]["verified_evidence"]["mobility"]["snapshot"]
        base = client.get(f"/api/v1/plans/{proposal['base_plan_version_id']}").json()
        candidate = client.get(f"/api/v1/plans/{proposal['proposed_plan_version_id']}").json()
        expected_delta = _snapshot_route_total(candidate, snapshot) - _snapshot_route_total(
            base, snapshot
        )
        assert proposal["diff"]["travel_delta_min"] == expected_delta


def test_accept_revalidates_with_the_same_persisted_frozen_snapshot(monkeypatch):
    _jid, proposals = _generate_with_partial_fallback(monkeypatch)
    chosen = proposals[0]
    expected_snapshot_id = chosen["diff"]["verified_evidence"]["mobility"]["snapshot"][
        "snapshot_id"
    ]

    original = main_module.validate_plan_feasibility
    seen = []

    def wrapped(*args, **kwargs):
        provider = kwargs.get("mobility_provider")
        seen.append(provider)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "validate_plan_feasibility", wrapped)
    response = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": f"gpt-accept-{uuid4()}"},
    )
    assert response.status_code == 200, response.text
    assert seen
    provider = seen[-1]
    assert isinstance(provider, MobilitySnapshot)
    assert provider.snapshot_id == expected_snapshot_id
