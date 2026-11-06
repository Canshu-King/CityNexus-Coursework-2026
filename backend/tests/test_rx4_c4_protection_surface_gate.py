"""RX-4: the four `CommitmentModel.level == "C4_Anchored"` predicates in main.py,
gated against the product surface each one is supposed to control.

The package dispatched by line number, so sites are named here by the function
that owns them and by what they are for:

    site1  _trip_studio_authoritative_gate   -> trip-studio preview / accept
    site2  journey_route_evidence           -> GET  /journeys/{id}/route-evidence
    site3  generate_replan                  -> POST /agent/tools/request_replan
    site4  _revalidate_locked_commitments   -> POST /replans/{id}/accept

Call counters were installed first and read before any assertion was written
(raw/counters_step1.txt): `POST /journeys/{id}/plans/generate` reaches **none**
of the four - every delta is 0 - which is why this file drives the endpoints that
do. A gate that only calls `plans/generate` cannot observe these predicates no
matter how strictly it asserts the result, and that is what the previous revision
of this package got wrong.

Dispatched under the name `test_rx4_c4_generate_gate.py`; that name was later
reserved for the 7-test generate guard, so this slice ships as
`test_rx4_c4_protection_surface_gate.py`. Same assertions, same numbers.

Each assertion demands a field the commitment is supposed to *cause*:

  * site2 - the route overlay's protection badge, bound to the committing Activity;
  * site3 - the replan's protected-commitment counts and anchor ids;
  * and the control that proves the badge is not decoration: the same booked
    Activity, unpaid, holding an ACTIVE C3_Guaranteed commitment, gets none.

site1 and site4 are diagnosed in RX4_GATE.md rather than asserted here, because
both are provably redundant on every state the API can reach; a passing test over
them would be the fake green the dispatch warns against.
"""

from __future__ import annotations

import json
import os
from typing import Any

for _key in ("DEEPSEEK_API_KEY", "CITYNEXUS_AI_API_KEY"):
    os.environ.pop(_key, None)
os.environ.setdefault("CITYNEXUS_AI_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from app.db.models import Commitment
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

CRUISE_PLACE = "place-river-cruise"
DISPLACED_PLACE = "place-tech-art"


@pytest.fixture
def client() -> Any:
    return TestClient(app)


# --------------------------------------------------------------------------
# product-path helpers
# --------------------------------------------------------------------------
def _commitments(journey_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(Commitment).filter(Commitment.journey_id == journey_id).all()
        return [
            {
                "level": row.level,
                "status": row.status,
                "activity_id": row.activity_id,
                "policy": json.loads(row.policy_json or "{}"),
            }
            for row in rows
        ]


def _journey(client: TestClient, *, capture: bool) -> tuple[str, dict, dict, dict]:
    """A travelling journey with the cruise booked, its payment captured or not.

    ``capture=False`` is the control half of every assertion below: the same
    Activity, the same booking, one ACTIVE **C3_Guaranteed** commitment instead of
    a C4, because nothing was paid.  Anything asserted must appear for the paid
    journey and not for that one, or it is measuring the booking rather than the
    commitment level.  Capture happens before ``/start`` because booking a
    travelling activity is refused once the journey is under way.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert response.status_code == 201, response.text
    plan = response.json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    cruise = next(a for a in plan["activities"] if a["place_id"] == CRUISE_PLACE)
    displaced = next(a for a in plan["activities"] if a["place_id"] == DISPLACED_PLACE)

    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert booking.status_code == 201, booking.text
    body = booking.json()
    if capture:
        transaction = client.post(
            f"/api/v1/journeys/{journey_id}/transactions",
            json={
                "booking_id": body["id"],
                "transaction_type": "CAPTURE",
                "amount": body["amount"],
            },
            headers={"Idempotency-Key": f"rx4c-{body['id']}"},
        )
        assert transaction.status_code == 201, transaction.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    rows = _commitments(journey_id)
    if capture:
        anchored = [
            item for item in rows if item["level"] == "C4_Anchored" and item["status"] == "ACTIVE"
        ]
        assert len(anchored) == 1, f"a captured payment must leave exactly one active C4: {rows}"
        assert anchored[0]["activity_id"] == cruise["id"]
        assert anchored[0]["policy"].get("payment_succeeded") is True
    else:
        assert not [item for item in rows if item["level"] == "C4_Anchored"], (
            "the control must hold no C4 commitment, or the comparison proves nothing"
        )
        guaranteed = [
            item
            for item in rows
            if item["level"] == "C3_Guaranteed" and item["status"] == "ACTIVE"
        ]
        assert len(guaranteed) == 1, f"the unpaid booking should hold one active C3: {rows}"
        assert guaranteed[0]["activity_id"] == cruise["id"]
    return journey_id, plan, cruise, displaced


def _c4_features(body: dict[str, Any]) -> list[dict[str, Any]]:
    features = ((body.get("markers") or {}).get("features")) or []
    return [
        item
        for item in features
        if str((item.get("properties") or {}).get("role")) == "c4"
    ]


def _closure_replan(client: TestClient, journey_id: str, displaced_id: str) -> dict[str, Any]:
    """Close an unrelated activity and ask for a replan built around the paid one."""
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": displaced_id,
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "RX4-C4-GATE"},
        },
    )
    assert event.status_code == 201, event.text
    event_id = event.json()["id"]
    requested = client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"RX4C-{event_id}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event_id,
            "candidate_count": 3,
        },
    )
    assert requested.status_code == 200, requested.text
    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    presented = [
        item
        for item in proposals
        if item.get("status") == "PRESENTED" and item.get("trigger_event_id") == event_id
    ]
    assert presented, f"the closure produced no presented proposal: {proposals}"
    return presented[0]


# --------------------------------------------------------------------------
# site2 - journey_route_evidence
# --------------------------------------------------------------------------
def test_route_evidence_badges_the_paid_commitment(client: TestClient) -> None:
    """The map may only claim protection for a durable, paid commitment."""
    journey_id, _plan, cruise, _displaced = _journey(client, capture=True)

    response = client.get(f"/api/v1/journeys/{journey_id}/route-evidence")
    assert response.status_code == 200, response.text
    marked = _c4_features(response.json())

    assert len(marked) == 1, (
        "expected exactly one protection badge for the single paid commitment, got "
        f"{[item.get('properties') for item in marked]}"
    )
    properties = marked[0]["properties"]
    assert properties["role"] == "c4"
    assert properties["title"] == "C4 protected commitment"
    assert properties["activity_id"] == cruise["id"], (
        "the badge must sit on the Activity the commitment names, not merely somewhere "
        "on the same journey"
    )
    assert (marked[0].get("geometry") or {}).get("coordinates"), (
        "a protection badge that points at no location is not information"
    )


def test_unpaid_booking_on_the_same_activity_is_not_badged_as_protected(
    client: TestClient,
) -> None:
    """Control: identical journey and booking, no captured payment.

    Without this, the badge assertion above would pass on anything that merely has
    a booked cruise, and could never fail for the reason it exists.
    """
    journey_id, _plan, _cruise, _displaced = _journey(client, capture=False)

    response = client.get(f"/api/v1/journeys/{journey_id}/route-evidence")
    assert response.status_code == 200, response.text
    marked = _c4_features(response.json())

    assert marked == [], (
        "an unpaid C3 booking is advertised as a protected C4 commitment: "
        f"{[item.get('properties') for item in marked]}"
    )


# --------------------------------------------------------------------------
# site3 - generate_replan
# --------------------------------------------------------------------------
def test_replan_counts_the_paid_commitment_among_what_it_preserves(
    client: TestClient,
) -> None:
    """A replan must state, in its own numbers, that a paid commitment survived.

    Measured under the site3 mutation, so these are the fields that actually
    depend on the active-C4 query and not on the Activity's lock flag:
    ``preserved_protected`` and ``kept`` stay populated either way - they are
    unioned with locked activities - while the anchor ids, the protected count
    and the envelope's preserved count all collapse to zero and a second,
    unconstrained candidate is offered.  Asserting ``preserved_protected`` alone
    would have looked right and killed nothing.
    """
    journey_id, _plan, cruise, displaced = _journey(client, capture=True)

    proposal = _closure_replan(client, journey_id, displaced["id"])
    diff = proposal.get("diff") or {}
    partition = diff.get("execution_partition") or {}
    preservation = ((diff.get("verified_evidence") or {}).get("commitment_preservation")) or {}
    envelope = diff.get("after_feasibility_envelope") or {}

    assert proposal["locked_commitments_preserved"] is True
    assert cruise["id"] in (partition.get("c4_anchor_ids") or []), (
        "the replanner did not treat the paid commitment as a fixed anchor: "
        f"{partition.get('c4_anchor_ids')}"
    )
    assert preservation.get("protected_count") == 1, (
        f"the proposal claims {preservation.get('protected_count')} protected commitments "
        "while one is paid and ACTIVE"
    )
    assert preservation.get("preserved") is True
    assert envelope.get("preserved_commitment_count") == 1, envelope
    assert CRUISE_PLACE in (diff.get("kept") or []), diff.get("kept")
    assert DISPLACED_PLACE in (diff.get("removed") or []), diff.get("removed")
