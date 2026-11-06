"""RX-4: a paid C4 commitment must still be built around by candidate generation.

The defect class this gate exists for: a traveller pays for something real, and
`plans/generate` then hands back candidates that silently ignore that fact.  The
gate walks the product's own endpoints - demo reset, generate, select, booking,
payment capture, inbox import + confirmation - and asserts the anchor survives
as a *semantic* identity (place, paid time, semantic key), not as an id match.

Three scenarios, deliberately:

  * the booked-and-captured cruise, which the API does protect;
  * a confirmed external ARRIVAL reservation, which has no template activity and
    therefore exists in a candidate only because the C4 commitment forces it; and
  * a confirmed reservation whose location cannot be resolved, which nothing
    else can pin.

The first two are protected twice over - the schedule-pinning pass and the
hard-constraint assembly each cover the other, so removing one is invisible from
outside.  The third has exactly one guard, and that is the assertion which can
fail.  ``test_cruise_protection_is_not_discriminating`` pins down why the first
scenario cannot carry the gate on its own.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

for _key in ("DEEPSEEK_API_KEY", "CITYNEXUS_AI_API_KEY"):
    os.environ.pop(_key, None)
os.environ.setdefault("CITYNEXUS_AI_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from app.db.models import Activity, Commitment, Journey
from app.db.session import SessionLocal
from app.main import app
from app.planning.anchors import structural_place_id
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

CRUISE_PLACE = "place-river-cruise"
ARRIVAL_REF = "RX4-ARRIVAL-C4-GATE"


# --------------------------------------------------------------------------
# product-path helpers
# --------------------------------------------------------------------------
def _candidate_plans(client: TestClient, journey_id: str, count: int = 3) -> list[dict]:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": count}
    )
    assert response.status_code == 201, response.text
    plans = response.json()
    assert len(plans) == count, f"expected {count} candidates, got {len(plans)}"
    return plans


def _semantic_identity(activity: dict[str, Any]) -> str:
    """The identity a candidate carries for the node it schedules.

    A generated activity exposes its stable semantic key in metadata only when
    the planner materialised the node from an anchor; a plain template activity
    is identified by its place.  Reading the metadata first is what keeps this
    gate from passing on a bare ``place_id`` match.
    """
    metadata = activity.get("metadata") or {}
    return str(metadata.get("semantic_key") or activity.get("place_id") or "")


def _active_c4_commitments(journey_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(Commitment).filter(Commitment.journey_id == journey_id).all()
        return [
            {
                "id": row.id,
                "level": row.level,
                "status": row.status,
                "activity_id": row.activity_id,
                "policy": json.loads(row.policy_json or "{}"),
            }
            for row in rows
        ]


def _paid_cruise_journey(client: TestClient) -> tuple[str, dict[str, Any]]:
    """Book the cruise and capture its payment: one genuine, paid C4 anchor.

    Capture happens *before* ``/start`` because booking a travelling activity is
    refused afterwards; ordering the flow any other way leaves the gate with an
    uncaptured booking and nothing to protect.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = _candidate_plans(client, journey_id, count=1)[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    cruise = next(item for item in plan["activities"] if item["place_id"] == CRUISE_PLACE)
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert booking.status_code == 201, booking.text
    body = booking.json()
    capture = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": body["id"],
            "transaction_type": "CAPTURE",
            "amount": body["amount"],
        },
        headers={"Idempotency-Key": f"rx4-capture-{body['id']}"},
    )
    assert capture.status_code == 201, capture.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    commitments = [item for item in _active_c4_commitments(journey_id) if item["status"] == "ACTIVE"]
    assert len(commitments) == 1, f"expected exactly one active commitment, got {commitments}"
    anchored = next(
        (item for item in commitments if item["level"] == "C4_Anchored"),
        None,
    )
    assert anchored is not None, (
        "a captured payment must promote its Commitment to C4_Anchored; "
        f"the fixture holds {[(item['level'], item['status']) for item in commitments]}"
    )
    return journey_id, anchored


def _confirmed_arrival_journey(client: TestClient, *, confirm: bool) -> tuple[str, dict, str]:
    """A demo journey holding a real external rail arrival, confirmed or not.

    Confirmed yields a C4 commitment with no Activity behind it, so the only
    reason an arrival node can appear in a candidate is the commitment itself.
    Un-confirmed yields the same artefact without the commitment, which is the
    control that stops the confirmed case from passing by construction.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = _candidate_plans(client, journey_id, count=1)[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    reservation = {
        "reservation_type": "RAIL",
        "provider_key": "rx4-gate",
        "confirmation_ref": ARRIVAL_REF,
        "origin": {
            "name": "Tianjin West Station",
            "latitude": 39.1366,
            "longitude": 117.1696,
        },
        "destination": {
            "name": "Tianjin Binhai Station",
            "latitude": 39.1244,
            "longitude": 117.3462,
        },
        # Inside the demo journey window (2026-10-10 09:00-19:00 +08:00) and
        # deliberately *not* on a template boundary, so a candidate that places
        # the node at a rounded or inherited time is visibly wrong.
        "scheduled_departure_at": "2026-10-10T08:00:00+08:00",
        "scheduled_arrival_at": "2026-10-10T09:15:00+08:00",
        "price_amount_minor": 5500,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": "ARRIVAL",
    }
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": reservation}},
    )
    assert imported.status_code == 201, imported.text
    draft_id = imported.json()["id"]
    if confirm:
        confirmed = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft_id}/confirm")
        assert confirmed.status_code == 200, confirmed.text
    anchored = next(
        (
            item
            for item in _active_c4_commitments(journey_id)
            if item["status"] == "ACTIVE" and item["level"] == "C4_Anchored"
        ),
        None,
    )
    if confirm:
        assert anchored is not None, "a confirmed external reservation must yield an active C4"
    else:
        assert anchored is None, "an unconfirmed draft must not create a C4 commitment"
    return journey_id, anchored or {}, structural_place_id(journey_id, "ARRIVAL")


UNRESOLVABLE_PLACE = "place-rx4-not-in-this-city"


def _unresolvable_paid_anchor_journey(client: TestClient) -> tuple[str, dict, str]:
    """A paid C4 anchor whose location no planner can reach.

    Nothing can pin this into a schedule - not a locked Activity, not a
    materialised anchor Place - so the hard constraint assembled from the active
    C4 commitments is the only thing left standing between the traveller and a
    candidate list that quietly leaves the commitment out.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = _candidate_plans(client, journey_id, count=1)[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    reservation = {
        "reservation_type": "FERRY",
        "provider_key": "rx4-gate",
        "confirmation_ref": "RX4-UNRESOLVABLE-C4-GATE",
        "origin": {"name": "Tianjin Port", "latitude": 39.0, "longitude": 117.7},
        "destination": {"name": "Nowhere Terminal", "latitude": 38.0, "longitude": 118.0},
        "scheduled_departure_at": "2026-10-10T13:00:00+08:00",
        "scheduled_arrival_at": "2026-10-10T13:40:00+08:00",
        "price_amount_minor": 3200,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": "MID_TRIP",
        "semantic_node_key": UNRESOLVABLE_PLACE,
    }
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": reservation}},
    )
    assert imported.status_code == 201, imported.text
    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    anchored = next(
        (
            item
            for item in _active_c4_commitments(journey_id)
            if item["status"] == "ACTIVE" and item["level"] == "C4_Anchored"
        ),
        None,
    )
    assert anchored is not None
    assert anchored["activity_id"] is None
    assert anchored["policy"]["semantic_key"] == UNRESOLVABLE_PLACE
    return journey_id, anchored, UNRESOLVABLE_PLACE


def _anchor_matches(plan: dict[str, Any], semantic_key: str) -> list[dict[str, Any]]:
    return [
        activity
        for activity in plan["activities"]
        if _semantic_identity(activity) == semantic_key
    ]


# --------------------------------------------------------------------------
# fixture truth: the payment really did produce a durable C4 commitment
# --------------------------------------------------------------------------
def test_capture_promotes_a_real_active_c4_anchored_commitment() -> None:
    client = TestClient(app)
    _journey_id, anchored = _paid_cruise_journey(client)
    policy = anchored["policy"]

    assert anchored["level"] == "C4_Anchored"
    assert anchored["status"] == "ACTIVE"
    assert policy["payment_succeeded"] is True
    assert policy["source_type"] == "booking"
    assert policy["semantic_key"] == CRUISE_PLACE
    assert policy["scheduled_start_at"] and policy["scheduled_end_at"]
    source_refs = policy.get("source_refs") or {}
    assert source_refs.get("payment_transaction_id"), (
        "the commitment must record the payment it was created from, otherwise "
        "'protected' is a claim with no evidence behind it"
    )


# --------------------------------------------------------------------------
# the gate: every candidate keeps the paid anchor's place, time and meaning
# --------------------------------------------------------------------------
def test_generated_candidates_keep_the_paid_cruise_anchor() -> None:
    client = TestClient(app)
    journey_id, anchored = _paid_cruise_journey(client)
    policy = anchored["policy"]

    for plan in _candidate_plans(client, journey_id, count=3):
        matches = _anchor_matches(plan, policy["semantic_key"])
        assert len(matches) == 1, (
            f"candidate {plan['id']} dropped or duplicated the paid anchor "
            f"{policy['semantic_key']}: {matches}"
        )
        anchored_activity = matches[0]
        assert anchored_activity["place_id"] == CRUISE_PLACE
        assert anchored_activity["locked"] is True
        assert anchored_activity["start_at"] == policy["scheduled_start_at"], (
            "the candidate reschedules a paid reservation: "
            f"{anchored_activity['start_at']} != {policy['scheduled_start_at']}"
        )
        assert anchored_activity["end_at"] == policy["scheduled_end_at"]


def test_no_active_c4_anchor_is_left_unbuilt_by_generation() -> None:
    """One invariant, stated generically over commitments rather than by name.

    Hard-coding the cruise would let the gate keep passing if protection keyed
    off a literal place.  This iterates whatever the database actually holds.
    """
    client = TestClient(app)
    journey_id, anchored = _paid_cruise_journey(client)
    commitments = [
        item
        for item in _active_c4_commitments(journey_id)
        if item["status"] == "ACTIVE" and item["level"] == "C4_Anchored"
    ]
    assert commitments == [anchored]

    for plan in _candidate_plans(client, journey_id, count=3):
        for commitment in commitments:
            semantic_key = str(commitment["policy"].get("semantic_key") or "")
            assert semantic_key, f"commitment {commitment['id']} carries no semantic anchor"
            assert _anchor_matches(plan, semantic_key), (
                f"candidate {plan['id']} generates no activity for paid commitment "
                f"{commitment['id']} ({semantic_key})"
            )


# --------------------------------------------------------------------------
# the discriminating case: an anchor that exists nowhere but in the commitment
# --------------------------------------------------------------------------
def test_confirmed_external_arrival_is_built_into_every_candidate() -> None:
    client = TestClient(app)
    journey_id, anchored, arrival_place = _confirmed_arrival_journey(client, confirm=True)
    policy = anchored["policy"]
    assert policy["anchor_role"] == "ARRIVAL"
    assert anchored["activity_id"] is None, (
        "the fixture only discriminates while nothing in the selected plan pins "
        "this node; an activity_id here means the control collapsed"
    )

    for plan in _candidate_plans(client, journey_id, count=3):
        matches = _anchor_matches(plan, arrival_place)
        assert len(matches) == 1, (
            f"candidate {plan['id']} ignored the confirmed arrival reservation "
            f"{ARRIVAL_REF}: expected the {arrival_place} node"
        )
        arrival = matches[0]
        assert arrival["place_id"] == arrival_place
        assert (arrival.get("metadata") or {}).get("semantic_key") == arrival_place, (
            "the anchor node must carry the commitment's semantic key, not merely "
            "reuse its place id"
        )
        assert arrival["locked"] is True
        assert arrival["start_at"] == policy["scheduled_start_at"], (
            f"arrival scheduled at {arrival['start_at']}, but the traveller paid "
            f"to arrive at {policy['scheduled_start_at']}"
        )
        assert datetime.fromisoformat(arrival["start_at"]).utcoffset() == datetime.fromisoformat(
            policy["scheduled_start_at"]
        ).utcoffset()
        assert arrival["sequence_no"] == min(
            item["sequence_no"] for item in plan["activities"]
        ), "an arrival anchor must open the day, not be appended to it"


def test_unconfirmed_reservation_does_not_create_an_anchor_node() -> None:
    """Control for the test above.

    Without this, `test_confirmed_external_arrival_is_built_into_every_candidate`
    could pass on a node the planner emits regardless of any commitment, and a
    gate that passes for that reason kills no mutation.
    """
    client = TestClient(app)
    journey_id, anchored, arrival_place = _confirmed_arrival_journey(client, confirm=False)
    assert anchored == {}
    assert _active_c4_commitments(journey_id) == []

    for plan in _candidate_plans(client, journey_id, count=3):
        assert _anchor_matches(plan, arrival_place) == [], (
            f"candidate {plan['id']} materialises the arrival anchor {arrival_place} "
            "for a reservation nobody confirmed"
        )


# --------------------------------------------------------------------------
# the load-bearing case: nothing pins this anchor, only the commitment does
# --------------------------------------------------------------------------
def test_unresolvable_paid_anchor_refuses_generation_instead_of_omitting_it() -> None:
    """Refuse loudly, or do not pretend to have planned the trip.

    Every other scenario in this file is protected twice over, so removing one
    guard changes nothing measurable.  This one has no second guard: no locked
    Activity refers to it and no anchor Place can be materialised for it.  Drop
    the active-C4 hard-constraint assembly and generation answers 201 with
    candidates that leave the paid commitment out and say nothing - which is the
    defect this package was opened for.
    """
    client = TestClient(app)
    journey_id, anchored, semantic_key = _unresolvable_paid_anchor_journey(client)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    )
    if response.status_code == 201:
        offenders = [
            plan["id"]
            for plan in response.json()
            if not _anchor_matches(plan, semantic_key)
        ]
        pytest.fail(
            f"generation returned candidates that silently drop paid commitment "
            f"{anchored['id']} ({semantic_key}): {offenders}. An unresolvable anchor "
            "must be refused and named, never reported as satisfied."
        )
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "C4_ANCHOR_UNRESOLVED" in message, message
    assert semantic_key in message, (
        f"the refusal must name the anchor it cannot honour, got: {message}"
    )


# --------------------------------------------------------------------------
# recorded limitation: why the cruise scenario alone is not enough
# --------------------------------------------------------------------------
def test_cruise_protection_is_not_discriminating() -> None:
    """The demo cruise is locked before anyone pays for it.

    Pinned as a test rather than a comment because it is the reason the arrival
    scenario has to exist: if the suite only asserted the cruise survives
    generation, removing every C4 predicate from the generate path would still
    be green.  Measured, not assumed - see RX4_GATE.md.
    """
    client = TestClient(app)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    assert _active_c4_commitments(journey_id) == [], "a fresh demo journey must hold no commitment"

    for plan in _candidate_plans(client, journey_id, count=3):
        matches = _anchor_matches(plan, CRUISE_PLACE)
        assert len(matches) == 1
        assert matches[0]["locked"] is True, (
            "the fixture no longer shows a locked cruise without a commitment; "
            "the cruise assertions may now discriminate, so revisit this gate "
            "and the arrival scenario it justifies"
        )


# --------------------------------------------------------------------------
# ORD-0021 (scope-corrected): three premises that were assertions in the retired
# gate are now only prose.  They are re-pinned here, and nothing else:
# the refusal itself stays owned by
# `test_unresolvable_paid_anchor_refuses_generation_instead_of_omitting_it`.
# --------------------------------------------------------------------------
def _import_unresolvable_reservation(
    client: TestClient, journey_id: str, *, ref: str, confirm: bool
) -> str | None:
    """Bring the same artefact onto the journey, confirmed or not.

    Confirmation is the only thing that varies between the control generation and
    the refusal, so this stays the single lever in the node below.
    """
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={
            "artifact_type": "STRUCTURED_JSON",
            "payload": {
                "reservation": {
                    "reservation_type": "FERRY",
                    "provider_key": "rx4-gate",
                    "confirmation_ref": ref,
                    "origin": {"name": "Tianjin Port", "latitude": 39.0, "longitude": 117.7},
                    "destination": {
                        "name": "Nowhere Terminal",
                        "latitude": 38.0,
                        "longitude": 118.0,
                    },
                    "scheduled_departure_at": "2026-10-10T13:00:00+08:00",
                    "scheduled_arrival_at": "2026-10-10T13:40:00+08:00",
                    "price_amount_minor": 3200,
                    "currency": "CNY",
                    "price_status": "KNOWN",
                    "anchor_role": "MID_TRIP",
                    "semantic_node_key": UNRESOLVABLE_PLACE,
                }
            },
        },
    )
    assert imported.status_code == 201, imported.text
    draft_id = imported.json()["id"]
    if not confirm:
        return None
    confirmed = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["commitment"]["id"]


def test_nothing_outside_the_commitment_pins_the_unreachable_anchor() -> None:
    """Why the refusal can only be the C4 constraint assembly talking.

    Three premises, asserted rather than described:

      1. an identical journey carrying the same reservation *unconfirmed* - so no
         ACTIVE C4 - still generates, and generates plural;
      2. no Activity anywhere in the database carries the unreachable node, so the
         fixture does not hand the answer to the generator;
      3. the plan this journey is actually executing pins no such node either, so
         no second guard is quietly responsible.

    The control generation is deliberately the **first** call to
    ``plans/generate`` in this node: if generation ever refuses everything, item 1
    is what goes red, which is the only proof that these three are assertions and
    not decoration.  The refusal itself is asserted by
    ``test_unresolvable_paid_anchor_refuses_generation_instead_of_omitting_it``
    and is not repeated here.
    """
    client = TestClient(app)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    _import_unresolvable_reservation(
        client, journey_id, ref="RX4-PREMISE-UNCONFIRMED", confirm=False
    )
    assert [
        item for item in _active_c4_commitments(journey_id) if item["level"] == "C4_Anchored"
    ] == [], "an unconfirmed draft must not create an ACTIVE C4 commitment"

    # (1) same shape, no ACTIVE C4: generation succeeds and offers candidates.
    control = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    )
    assert control.status_code == 201, (
        "the control journey holds no ACTIVE C4 and was still refused: "
        f"{control.text[:200]} - generation is refusing indiscriminately, so the "
        "named anchor refusal elsewhere in this file proves nothing about the "
        "commitment"
    )
    assert len(control.json()) == 3, f"expected 3 candidates, got {len(control.json())}"
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{control.json()[0]['id']}/select"
    ).status_code == 200

    with SessionLocal() as session:
        # (2) the node exists nowhere in the seeded data.
        strays = session.query(Activity).filter(Activity.place_id == UNRESOLVABLE_PLACE).count()
        assert strays == 0, (
            f"the seeded fixture already holds {strays} Activity rows for "
            f"{UNRESOLVABLE_PLACE}; the refusal would then be about data, not the commitment"
        )

        # (3) and the live plan pins none of it either.
        active_plan_id = session.get(Journey, journey_id).active_plan_version_id
        assert active_plan_id, "the control journey must be executing a plan"
        pinned = (
            session.query(Activity)
            .filter_by(place_id=UNRESOLVABLE_PLACE, plan_version_id=active_plan_id)
            .count()
        )
        assert pinned == 0, "the ACTIVE plan already pins the node; another guard is in play"
