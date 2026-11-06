"""ORD-0360: one node, one ACTIVE anchor - and when something still blocks, say which commitment.

The defect this gate exists for: a traveller confirms an external reservation for their arrival,
then confirms a second one for the *same* node (a re-issued ticket, a second import of the same
journey leg).  The only guard on that path keyed on ``confirmation_ref``, so two different
references produced two ACTIVE C4 commitments; ``resolve_plan_anchors`` then emitted two anchors
for one node, and ``validate_anchor_timeline`` found the pair overlapping.  Generation answered
with the traveller's own duplicate dressed up as an unsatisfiable constraint
(``C4_ANCHOR_OVERLAP:<key>|<key>`` -> "No feasible plan candidates satisfy the journey
constraints"), and the door that could have retired an anchor - ``POST /api/v1/bookings/<id>/cancel`` -
answers 422 for an imported reservation, so there was no way out.

Every scenario walks the product's own endpoints.  Assertions read rows and the sentence actually
produced, never a status code alone, and counts are computed from what was read.
"""

from __future__ import annotations

import json
import os

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CITYNEXUS_AI_API_KEY"):
    os.environ.pop(_key, None)
os.environ.setdefault("CITYNEXUS_AI_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import AuditLog, Booking, Event, Journey
from app.db.session import SessionLocal, engine, ensure_schema
from app.main import app
from app.planning.anchors import anchor_claim_identity, structural_place_id
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

FIRST_REF = "ORD0360-ARRIVAL-FIRST"
SECOND_REF = "ORD0360-ARRIVAL-REISSUED"
DEPARTURE_REF = "ORD0360-DEPARTURE-TOO-EARLY"
MID_TRIP_PLACE = "place-river-cruise"
BASE_SENTENCE = "No feasible plan candidates satisfy the journey constraints"
RETIRE_MARKER = "SUPERSEDED_BY_NEWER_CONFIRMATION"
RETIRE_EVENT = "C4_ANCHOR_SUPERSEDED_BY_NEWER_CONFIRMATION"
INDEX_NAMES = (
    "uq_commitments_one_active_arrival_anchor",
    "uq_commitments_one_active_departure_anchor",
    "uq_commitments_one_active_mid_trip_anchor",
)


def _reservation(ref: str, *, role: str, arrives: str, departs: str) -> dict:
    return {
        "reservation_type": "RAIL",
        "provider_key": "ord0360-gate",
        "confirmation_ref": ref,
        "origin": {"name": "Tianjin West Station", "latitude": 39.1366, "longitude": 117.1696},
        "destination": {"name": "Tianjin Binhai Station", "latitude": 39.1244, "longitude": 117.3462},
        "scheduled_departure_at": departs,
        "scheduled_arrival_at": arrives,
        "price_amount_minor": 5500,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": role,
    }


def _arrival(ref: str, arrives: str = "2026-10-10T09:15:00+08:00") -> dict:
    return _reservation(ref, role="ARRIVAL", arrives=arrives,
                        departs="2026-10-10T08:00:00+08:00")


def _journey_with_a_selected_plan(client: TestClient) -> str:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert plans.status_code == 201, plans.text
    assert plans.json(), "generation has to work before an anchor can break it"
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plans.json()[0]['id']}/select"
    )
    assert selected.status_code == 200, selected.text
    return journey_id


def _confirm(client: TestClient, journey_id: str, body: dict) -> dict:
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": body}},
    )
    assert imported.status_code == 201, imported.text
    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _commitments(journey_id: str) -> list[dict]:
    """Every commitment row of one journey, read back with sqlite's own rowid."""
    with engine.begin() as conn:
        found = conn.exec_driver_sql(
            "SELECT rowid, id, status, level, kind, external_ref, activity_id, policy_json"
            " FROM commitments WHERE journey_id = ? ORDER BY rowid",
            (journey_id,),
        ).fetchall()
    keys = ("rowid", "id", "status", "level", "kind", "external_ref", "activity_id", "policy_json")
    rows = []
    for item in found:
        row = dict(zip(keys, item))
        row["policy"] = json.loads(row.pop("policy_json") or "{}")
        row["identity"] = anchor_claim_identity(row["policy"], journey_id=journey_id)
        rows.append(row)
    return rows


def _active_anchors(journey_id: str) -> list[dict]:
    return [
        row for row in _commitments(journey_id)
        if row["status"] == "ACTIVE" and row["identity"]
    ]


def _by_identity(rows: list[dict]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for row in rows:
        seen.setdefault(str(row["identity"]), []).append(str(row["external_ref"]))
    return seen


def _generate(client: TestClient, journey_id: str, count: int = 3):
    return client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": count}
    )


def _blocked_message(client: TestClient, journey_id: str) -> str:
    """Read the sentence the traveller would be handed - and fail if it is not a block."""
    response = _generate(client, journey_id)
    if response.status_code < 400:
        pytest.fail(
            "this scenario has to stay blocked, otherwise nothing about the wording is measured: "
            f"{response.status_code}"
        )
    return str((response.json().get("error") or {}).get("message") or "")


def _blocked_journey(client: TestClient) -> str:
    """An arrival and a departure fifteen minutes apart: two honest anchors, one impossible gap."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(
        client, journey_id,
        _reservation(DEPARTURE_REF, role="DEPARTURE", arrives="2026-10-10T09:40:00+08:00",
                     departs="2026-10-10T09:20:00+08:00"),
    )
    return journey_id


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# the duplicate itself
# ---------------------------------------------------------------------------
def test_ORD0360_A1_second_confirmation_of_one_node_retires_the_older_anchor(client):
    """Two confirmations for one arrival leave exactly one ACTIVE anchor: the newer claim."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(client, journey_id, _arrival(SECOND_REF, "2026-10-10T10:05:00+08:00"))

    rows = _commitments(journey_id)
    active = [row for row in rows if row["status"] == "ACTIVE"]
    retired = [row for row in rows if row["status"] != "ACTIVE"]
    assert len(rows) == 2, f"both confirmations must still exist as rows, got {rows}"
    assert len(active) == 1, f"more than one ACTIVE anchor survived: {active}"
    assert active[0]["external_ref"] == SECOND_REF, (
        "the survivor must be the traveller's later statement about the node, got "
        f"{active[0]['external_ref']!r}"
    )
    assert [row["external_ref"] for row in retired] == [FIRST_REF]
    assert retired[0]["policy"].get("retire_reason") == RETIRE_MARKER, (
        f"the retired row must say why it stopped anchoring: {retired[0]['policy']}"
    )
    assert retired[0]["policy"].get("superseded_by_confirmation_ref") == SECOND_REF
    duplicated = {k: v for k, v in _by_identity(active).items() if len(v) > 1}
    assert duplicated == {}, f"a node still holds two ACTIVE claims: {duplicated}"


def test_ORD0360_A2_the_default_path_generates_again_after_the_second_confirmation(client):
    """The point of the order: a duplicate claim must not keep generation from answering.

    Before the fix this call returned 422 with ``C4_ANCHOR_OVERLAP`` naming one key twice.
    """
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(client, journey_id, _arrival(SECOND_REF, "2026-10-10T10:05:00+08:00"))

    response = _generate(client, journey_id)
    assert response.status_code == 201, (
        f"generation is still blocked by the duplicate: {response.status_code} {response.text}"
    )
    candidates = response.json()
    assert len(candidates) == 3, f"expected three candidates, got {len(candidates)}"
    activities = [item for plan in candidates for item in plan.get("activities") or []]
    assert activities, "a candidate list with no activities proves nothing about the anchor"


def test_ORD0360_A3_the_retired_claim_keeps_its_row_its_booking_and_a_durable_record(client):
    """Nothing is deleted: the money fact stays, the planning claim is retired and recorded."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(client, journey_id, _arrival(SECOND_REF, "2026-10-10T10:05:00+08:00"))
    retired = [row for row in _commitments(journey_id) if row["status"] == "CANCELLED"]
    assert len(retired) == 1, f"expected exactly one retired anchor row, got {retired}"

    with SessionLocal() as session:
        booking = session.scalars(select(Booking).where(Booking.external_ref == FIRST_REF)).one_or_none()
        kinds = [
            json.loads(item.payload_json or "{}").get("kind")
            for item in session.scalars(select(Event).where(Event.journey_id == journey_id)).all()
        ]
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.journey_id == journey_id,
                AuditLog.action == "commitment.anchor_superseded",
            )
        ).all()
    assert booking is not None, "the first ticket's Booking disappeared with the anchor"
    assert booking.status == "CONFIRMED", (
        f"retiring an anchor must not rewrite what the traveller paid for: {booking.status}"
    )
    assert booking.commitment_id == retired[0]["id"]
    assert RETIRE_EVENT in kinds, f"no durable Event records which anchor was retired: {kinds}"
    assert len(audits) == 1, f"the retirement must be auditable exactly once, got {len(audits)}"


def test_ORD0360_A4_no_journey_in_the_store_holds_two_ACTIVE_anchors_for_one_node(client):
    """The invariant, scanned rather than assumed.

    Read over every journey in this database, not only the one this test created: a rule that
    holds just for the row I inserted is not a rule.
    """
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(client, journey_id, _arrival(SECOND_REF, "2026-10-10T10:05:00+08:00"))

    with SessionLocal() as session:
        scanned = [str(item) for item in session.scalars(select(Journey.id)).all()]
    offenders = {}
    for one in scanned:
        bad = {k: v for k, v in _by_identity(_active_anchors(one)).items() if len(v) > 1}
        if bad:
            offenders[one] = bad
    assert offenders == {}, f"ACTIVE anchors still collide somewhere: {offenders}"
    assert journey_id in scanned, "this test's own journey must be inside the scanned set"


# ---------------------------------------------------------------------------
# the same rule on the other anchor shapes
# ---------------------------------------------------------------------------
def test_ORD0360_A5_mid_trip_anchors_of_one_place_also_collapse(client):
    """MID_TRIP keeps a real place key, so that key is its identity - twice means two ACTIVE."""
    journey_id = _journey_with_a_selected_plan(client)
    first = _reservation("ORD0360-MID-FIRST", role="MID_TRIP",
                         arrives="2026-10-10T11:00:00+08:00",
                         departs="2026-10-10T10:00:00+08:00")
    first["semantic_node_key"] = MID_TRIP_PLACE
    _confirm(client, journey_id, first)
    again = dict(first, confirmation_ref="ORD0360-MID-SECOND",
                 scheduled_arrival_at="2026-10-10T14:00:00+08:00",
                 scheduled_departure_at="2026-10-10T13:00:00+08:00")
    _confirm(client, journey_id, again)

    active = _active_anchors(journey_id)
    assert len(active) == 1, (
        f"one place claimed twice is still two ACTIVE anchors: {_by_identity(active)}"
    )
    assert active[0]["external_ref"] == "ORD0360-MID-SECOND"
    assert active[0]["identity"] == MID_TRIP_PLACE, (
        f"identity drifted away from the stored place key: {active[0]['identity']!r}"
    )


def test_ORD0360_A6_arrival_and_departure_stay_two_separate_claims(client):
    """The retire must not be a "one journey, one anchor" rule wearing a longer name."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(
        client, journey_id,
        _reservation(DEPARTURE_REF, role="DEPARTURE", arrives="2026-10-10T18:20:00+08:00",
                     departs="2026-10-10T17:40:00+08:00"),
    )
    active = _active_anchors(journey_id)
    assert len(active) == 2, f"arrival and departure are different nodes: {_by_identity(active)}"
    assert {row["external_ref"] for row in active} == {FIRST_REF, DEPARTURE_REF}


def test_ORD0360_A7_the_same_confirmation_ref_is_still_refused(client):
    """The old guard stays: replaying one provider reference is not a new claim."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={"artifact_type": "STRUCTURED_JSON",
              "payload": {"reservation": _arrival(FIRST_REF, "2026-10-10T12:00:00+08:00")}},
    )
    assert imported.status_code == 201, imported.text
    replay = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )
    assert replay.status_code == 409, f"a replayed reference must still conflict: {replay.text}"
    assert "DUPLICATE_CONFIRMATION_REF" in replay.text, replay.text
    assert [row["external_ref"] for row in _active_anchors(journey_id)] == [FIRST_REF], (
        f"the replay must not have moved the anchor: {_active_anchors(journey_id)}"
    )


# ---------------------------------------------------------------------------
# what the traveller is told when something genuinely still blocks
# ---------------------------------------------------------------------------
def test_ORD0360_A8_a_still_blocked_generation_names_both_commitments(client):
    """The sentence must point at the reservations that are in each other's way."""
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    _confirm(
        client, journey_id,
        _reservation(DEPARTURE_REF, role="DEPARTURE", arrives="2026-10-10T09:40:00+08:00",
                     departs="2026-10-10T09:20:00+08:00"),
    )
    message = _blocked_message(client, journey_id)
    active = _active_anchors(journey_id)
    assert "C4_ANCHOR_OVERLAP" in message, (
        f"the machine code has to survive for the gates that read it: {message!r}"
    )
    assert "blocking commitment" in message, (
        f"the sentence must name the commitments, not only the node: {message!r}"
    )
    for row in active:
        assert row["external_ref"] in message, f"an ACTIVE anchor is unnamed: {row}"
        assert row["id"] in message, f"an anchor is described without its row: {row}"
        assert row["policy"]["scheduled_start_at"] in message, (
            f"an anchor is named without the time it claims: {row}"
        )
    named = [row for row in _commitments(journey_id) if row["id"] in message]
    assert len(named) == len(active), (
        f"the sentence named {len(named)} commitments while {len(active)} anchors are ACTIVE - "
        "it is naming something other than what blocks"
    )


def test_ORD0360_A9_a_block_is_never_reported_as_the_network_or_as_vagueness(client):
    """The failure this order is about reached the traveller as "计划不可行"; that is the bug.

    Pinned on the same scenario A8 measures, so a mutate that swaps the named sentence for a
    generic one has exactly this assertion to turn red.
    """
    _journey_id = _blocked_journey(client)
    message = _blocked_message(client, _journey_id)
    assert message.startswith(BASE_SENTENCE), (
        f"the base sentence is read by an existing gate and must stay byte-identical: {message!r}"
    )
    assert "blocking commitment" in message, (
        "a code with nobody behind it is the answer this order exists to retire: "
        f"{message!r}"
    )
    lowered = message.lower()
    for forbidden in ("network", "unavailable", "temporarily", "retry", "网络", "不可用"):
        assert forbidden not in lowered, (
            f"an anchor conflict must not be attributed to the network or to a retry: {message!r}"
        )
    assert _commitments(_journey_id) and all(
        row["status"] == "ACTIVE" for row in _active_anchors(_journey_id)
    ), "both anchors of a genuine conflict must still be ACTIVE, or this is not a real block"


# ---------------------------------------------------------------------------
# the schema-level guarantee, and the repair that lets it exist
# ---------------------------------------------------------------------------
def test_ORD0360_A10_a_legacy_duplicate_is_repaired_at_boot_not_by_hand(client):
    """A database that already holds two ACTIVE anchors must boot with one, and say so.

    The three partial unique indexes come from ``db/schema.sql``; on a file that already violates
    them the repair has to run first, or the app cannot start at all.  This drops the indexes to
    imitate an older generation, plants the duplicate straight through SQL, then re-applies the
    schema through the product's own ``ensure_schema``.
    """
    journey_id = _journey_with_a_selected_plan(client)
    key = structural_place_id(journey_id, "ARRIVAL")
    planted = [
        ("legacy-old", "LEGACY-OLD", "2026-01-01T00:00:00", "2026-10-10T09:15:00+08:00"),
        ("legacy-new", "LEGACY-NEW", "2026-02-01T00:00:00", "2026-10-10T10:05:00+08:00"),
    ]
    with engine.begin() as conn:
        for name in INDEX_NAMES:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        for row_id, ref, created, scheduled in planted:
            conn.exec_driver_sql(
                "INSERT INTO commitments (id,journey_id,kind,level,status,monetary_value_minor,"
                "currency,external_ref,policy_json,created_at,updated_at)"
                " VALUES (?,?,'BOOKING','C4_Anchored','ACTIVE',5500,'CNY',?,?,?,?)",
                (row_id, journey_id, ref,
                 json.dumps({"anchor_role": "ARRIVAL", "semantic_key": key,
                             "scheduled_start_at": scheduled}, sort_keys=True),
                 created, created),
            )
        before = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM commitments WHERE journey_id = ? AND status = 'ACTIVE'",
            (journey_id,),
        ).fetchone()[0]
    assert before == 2, f"the planted duplicate did not land: {before}"

    ensure_schema(engine)

    with engine.begin() as conn:
        after = conn.exec_driver_sql(
            "SELECT external_ref, status, json_extract(policy_json,'$.retire_reason')"
            " FROM commitments WHERE id IN ('legacy-old','legacy-new') ORDER BY external_ref"
        ).fetchall()
        kinds = [
            json.loads(item[0]).get("kind")
            for item in conn.exec_driver_sql(
                "SELECT payload_json FROM events WHERE journey_id = ?", (journey_id,)
            ).fetchall()
        ]
        indexes = sorted(
            item[0]
            for item in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_commitments%'"
            ).fetchall()
        )
    states = {str(ref): (str(status), str(reason)) for ref, status, reason in after}
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM commitments WHERE id IN ('legacy-old','legacy-new')"
        )
        conn.exec_driver_sql(
            "DELETE FROM events WHERE json_extract(payload_json, '$.repair') ="
            " 'ensure_schema' AND journey_id = ?",
            (journey_id,),
        )
    assert states["LEGACY-NEW"][0] == "ACTIVE", f"the newest claim must survive: {states}"
    assert states["LEGACY-OLD"][0] == "CANCELLED", f"the older claim must be retired: {states}"
    assert states["LEGACY-OLD"][1] == RETIRE_MARKER, states
    assert RETIRE_EVENT in kinds, f"the boot repair must leave a durable record: {kinds}"
    assert indexes == sorted(INDEX_NAMES), f"the schema did not end up enforcing the rule: {indexes}"


def test_ORD0360_A11_the_store_itself_refuses_a_second_ACTIVE_anchor_for_one_node(client):
    """The last line of defence is the database, not the one route that remembers to retire.

    Written straight through SQL, bypassing the endpoint: anything asking for a second ACTIVE
    claim on one node - a future route, a repair, another lane's code path - is refused by SQLite,
    while a *different* node on the same journey stays legal.
    """
    journey_id = _journey_with_a_selected_plan(client)
    _confirm(client, journey_id, _arrival(FIRST_REF))
    key = _active_anchors(journey_id)[0]["identity"]

    def insert(row_id: str, role: str, semantic_key: str):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO commitments (id,journey_id,kind,level,status,monetary_value_minor,"
                "currency,external_ref,policy_json,created_at,updated_at)"
                " VALUES (?,?,'BOOKING','C4_Anchored','ACTIVE',0,'CNY',?,?,?,?)",
                (row_id, journey_id, row_id,
                 json.dumps({"anchor_role": role, "semantic_key": semantic_key,
                             "scheduled_start_at": "2026-10-10T12:00:00+08:00"}, sort_keys=True),
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )

    with pytest.raises(IntegrityError):
        insert("direct-same-node", "ARRIVAL", key)
    insert("direct-other-node", "MID_TRIP", MID_TRIP_PLACE)
    with engine.begin() as conn:
        kept = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM commitments WHERE journey_id = ? AND status = 'ACTIVE'",
            (journey_id,),
        ).fetchone()[0]
    assert kept == 2, (
        f"a second node on the same journey must stay legal; rows left ACTIVE: {kept}"
    )
    assert [row["external_ref"] for row in _active_anchors(journey_id)] == [FIRST_REF, "direct-other-node"]
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM commitments WHERE id = 'direct-other-node'")
