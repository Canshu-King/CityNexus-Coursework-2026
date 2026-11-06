"""CX-SOL-001 — the execution cursor is an assertion about identity, not a hint.

Before this pass the projection had one branch for "the cursor is absent" and
"the cursor names something that is not on this plan", and that branch was
*fall back to the first pending activity*.  The two cases are not alike:

    null cursor      -> the journey has started and nothing is done yet.  Showing
                        the first pending step is the truth.
    unresolvable     -> the record contradicts itself.  Showing the first pending
                        step is a fabrication the traveller will act on: they are
                        told they are at the museum when the authoritative row
                        says they were at a dinner that no longer exists.

So the cursor now has three states (VALID / ABSENT_FALLBACK / STALE), STALE
projects no Now, no Next and no NextLeg, and every consumer can see which state
it is in instead of inferring one from list order.

The second half of this file is a differential oracle: an independent reference
projection computed straight from the Activity and JourneyLeg rows is compared
against what the product serves, field by field, for every cursor state.  Where
authoritative truth exists, zero divergence is allowed; where it does not, the
product must fail closed rather than produce an equivalent-looking value.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Journey, JourneyLeg
from app.db.session import SessionLocal
from app.operations.v9 import refresh_offline_snapshot
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)

TERMINAL = {"COMPLETED", "SKIPPED", "CANCELLED"}


# --------------------------------------------------------------------------
# fixture helpers, all through the product's own endpoints
# --------------------------------------------------------------------------
def _started_journey() -> tuple[str, list[dict[str, Any]]]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan["activities"]


def _set_cursor(journey_id: str, cursor: str | None, statuses: dict[str, str] = {}) -> None:
    """Move the persisted cursor, then refresh the projection as the product does."""
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.current_activity_id = cursor
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
        for item in activities:
            if item.id in statuses:
                item.status = statuses[item.id]
        refresh_offline_snapshot(session, journey)
        session.commit()


def _plan_rows(journey_id: str) -> tuple[list[Activity], list[JourneyLeg], str | None]:
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        )
        legs = list(
            session.scalars(
                select(JourneyLeg)
                .where(JourneyLeg.plan_version_id == journey.active_plan_version_id)
                .order_by(JourneyLeg.sequence_no)
            ).all()
        )
        return activities, legs, journey.current_activity_id



def _off_plan_activity_id(journey_id: str) -> str:
    """A real Activity row that is not on the active plan.

    The foreign key on `journeys.current_activity_id` means a made-up id cannot
    be persisted at all, so it cannot be the test.  A pointer into another plan
    of the same journey satisfies the key and is still stale for the plan being
    executed, which is exactly the state the guard has to survive.
    """
    candidates = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    ).json()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        for plan in candidates:
            if plan["id"] == journey.active_plan_version_id:
                continue
            for item in plan["activities"]:
                return str(item["id"])
    raise AssertionError("no off-plan activity is available to act as a stale cursor")


def _served_projection(journey_id: str) -> dict[str, Any]:
    """What the product actually serves to a client, after a projection refresh."""
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        refresh_offline_snapshot(session, journey)
        session.commit()
    snapshot = client.get(f"/api/v1/journeys/{journey_id}/offline-snapshot").json()["snapshot"]
    return snapshot["execution"]


def _reference_projection(journey_id: str) -> dict[str, Any]:
    """Recompute the window straight from the rows, without the product's code."""
    activities, legs, cursor = _plan_rows(journey_id)
    by_id = {item.id: index for index, item in enumerate(activities)}
    if cursor is None:
        index = next(
            (i for i, item in enumerate(activities) if item.status not in TERMINAL),
            len(activities),
        )
        status = "ABSENT_FALLBACK"
    elif cursor not in by_id:
        index = len(activities)
        status = "STALE"
    else:
        index = by_id[cursor]
        status = "VALID"
    now = (
        activities[index]
        if index < len(activities) and activities[index].status not in TERMINAL
        else None
    )
    nxt = next(
        (item for item in activities[index + 1 :] if item.status not in TERMINAL), None
    )
    # Independent re-derivation of the same rule the product now applies: a leg is the
    # transfer *out of* Now, so an index with no resolved Now gets no leg even when the
    # leg list is longer than the activity list.  If the oracle kept `legs[index]` alone
    # it would leak on exactly the shape ORD-0006 constructs, and the comparison would
    # then report the oracle's own mistake as a product divergence.
    leg = legs[index] if now is not None and 0 <= index < len(legs) else None
    return {
        "cursor_status": status,
        "persisted_cursor": cursor,
        "now_activity_id": now.id if now else None,
        "next_activity_id": nxt.id if nxt else None,
        "next_leg_id": leg.id if leg else None,
        "next_mode": leg.mode if leg else "NONE",
        "next_eta_min": leg.duration_min if leg else 0,
    }


# --------------------------------------------------------------------------
# A. the truth table
# --------------------------------------------------------------------------
def test_null_cursor_is_a_legal_pre_execution_state_and_projects_the_first_step() -> None:
    journey_id, activities = _started_journey()
    _set_cursor(journey_id, None)

    served = _served_projection(journey_id)
    assert served["cursor_status"] == "ABSENT_FALLBACK"
    # Falling back is allowed here, and it is the truth: nothing has happened yet.
    assert served["now_activity_id"] == activities[0]["id"]
    assert served["next_activity_id"] == activities[1]["id"]


def test_valid_cursor_projects_exactly_that_activity_and_not_its_neighbours() -> None:
    journey_id, activities = _started_journey()
    target = activities[2]["id"]
    _set_cursor(journey_id, target)

    served = _served_projection(journey_id)
    assert served["cursor_status"] == "VALID"
    assert served["persisted_cursor"] == target
    assert served["now_activity_id"] == target
    assert served["next_activity_id"] == activities[3]["id"]


def test_cursor_naming_an_off_plan_activity_fails_closed_and_invents_nothing() -> None:
    journey_id, _ = _started_journey()
    _set_cursor(journey_id, _off_plan_activity_id(journey_id))

    served = _served_projection(journey_id)
    assert served["cursor_status"] == "STALE"
    # The three fabricated fields are the defect: each one looks plausible and
    # none of them is supported by the record.
    assert served["now_activity_id"] is None
    assert served["next_activity_id"] is None
    assert served["next_leg_id"] is None
    assert served["now_state"] == "STALE_CURSOR"
    assert served["next_mode"] == "NONE"
    assert served["next_eta_min"] == 0


def test_cursor_left_on_a_superseded_plan_after_a_replan_fails_closed() -> None:
    """The realistic stale case: the cursor still names a real row, but a row on
    the plan the accepted recovery replaced."""
    journey_id, activities = _started_journey()
    base_cursor = activities[1]["id"]
    _set_cursor(journey_id, base_cursor)

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": activities[3]["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "stale cursor after replan"},
        },
    ).json()
    client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"stale-{event['id']}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event["id"],
            "candidate_count": 3,
        },
    )
    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    proposal = next(
        item
        for item in proposals
        if item.get("trigger_event_id") == event["id"] and item.get("status") == "PRESENTED"
    )
    assert client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"stale-accept-{proposal['id']}"},
    ).status_code == 200

    # A correctly carried-forward cursor is VALID on the child plan.
    served = _served_projection(journey_id)
    assert served["cursor_status"] == "VALID", (
        "acceptance did not carry the execution position onto the child plan, so the "
        "traveller's current step was silently reset or dropped"
    )

    # Now simulate the failure mode the guard exists for: a cursor that still
    # names the superseded plan's row.
    _set_cursor(journey_id, base_cursor)
    stale = _served_projection(journey_id)
    assert stale["cursor_status"] == "STALE"
    assert stale["now_activity_id"] is None
    assert stale["next_activity_id"] is None


def test_experience_projection_reports_the_cursor_state_next_to_the_cursor() -> None:
    journey_id, activities = _started_journey()
    _set_cursor(journey_id, _off_plan_activity_id(journey_id))

    plan = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]
    listed = {item["id"] for item in plan["activities"]}
    assert plan["current_activity_id"] not in listed
    assert plan["cursor_status"] == "STALE", (
        "/experience served a cursor and an activity list that do not intersect, with "
        "no way for the consumer to notice"
    )
    # The lifecycle status is what lets a client tell "done" from "pending" without
    # re-deriving it from list order.
    assert all(item.get("status") for item in plan["activities"])


def test_edge_device_is_told_the_position_is_unresolved_not_that_the_journey_ended() -> None:
    journey_id, _ = _started_journey()
    device = client.post(
        f"/api/v1/journeys/{journey_id}/edge/devices",
        json={"display_name": "oracle device"},
    ).json()
    headers = {"X-Edge-Token": device["device_token"]}

    _set_cursor(journey_id, _off_plan_activity_id(journey_id))
    snapshot = client.get(
        f"/api/v1/edge/devices/{device['device_id']}/journey-snapshot", headers=headers
    ).json()

    assert snapshot["cursor_status"] == "STALE"
    assert snapshot["now_title"] == "Execution position unresolved"
    # "Journey complete" is the pre-fix wording and it is a lie: the journey is
    # active, the record is inconsistent.
    assert snapshot["next_title"] == "Next step unavailable"
    assert snapshot["now_state"] == "STALE_CURSOR"


# --------------------------------------------------------------------------
# H. differential oracle
# --------------------------------------------------------------------------
def _oracle_case(journey_id: str) -> None:
    reference = _reference_projection(journey_id)
    served = _served_projection(journey_id)
    diverging = {
        field: (reference[field], served.get(field))
        for field in (
            "cursor_status",
            "now_activity_id",
            "next_activity_id",
            "next_leg_id",
            "next_mode",
            "next_eta_min",
        )
        if reference[field] != served.get(field)
    }
    assert not diverging, (
        f"the product projection disagrees with the rows for cursor "
        f"{reference['cursor_status']}: {diverging}"
    )


def test_projection_matches_the_reference_for_every_cursor_state() -> None:
    for label, mutate in (
        ("valid", lambda acts, jid: _set_cursor(jid, acts[1]["id"])),
        ("null", lambda acts, jid: _set_cursor(jid, None)),
        (
            "off-plan cursor",
            lambda acts, jid: _set_cursor(jid, _off_plan_activity_id(jid)),
        ),
        (
            "cursor on a terminal step",
            lambda acts, jid: _set_cursor(
                jid, acts[1]["id"], {acts[1]["id"]: "COMPLETED"}
            ),
        ),
        (
            "null cursor with the first steps completed",
            lambda acts, jid: _set_cursor(
                jid, None, {acts[0]["id"]: "COMPLETED", acts[1]["id"]: "SKIPPED"}
            ),
        ),
    ):
        journey_id, activities = _started_journey()
        mutate(activities, journey_id)
        _oracle_case(journey_id)
        assert _reference_projection(journey_id)["cursor_status"] is not None, label


def test_projection_matches_the_reference_after_an_accepted_replan() -> None:
    journey_id, activities = _started_journey()
    _set_cursor(journey_id, activities[1]["id"])
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": activities[3]["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "post-replan oracle"},
        },
    ).json()
    client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"oracle-{event['id']}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event["id"],
            "candidate_count": 3,
        },
    )
    proposal = next(
        item
        for item in client.get(f"/api/v1/journeys/{journey_id}/replans").json()
        if item.get("trigger_event_id") == event["id"] and item.get("status") == "PRESENTED"
    )
    assert client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"oracle-accept-{proposal['id']}"},
    ).status_code == 200
    with SessionLocal() as session:
        accepted_journey = session.get(Journey, journey_id)
        current = session.get(Activity, accepted_journey.current_activity_id)
        assert accepted_journey.status == "ACTIVE"
        assert current.status == "ACTIVE"
    _oracle_case(journey_id)



# --------------------------------------------------------------------------
# ORD-0006 / ORD-0007: the leg tuple, and the two consumers of "is there a Next"
# --------------------------------------------------------------------------
def _legs_beyond_the_last_activity(journey_id: str) -> int:
    """Bring the ACTIVE plan to ``len(legs) == len(activities) + 1``.

    Written from scratch because no shipped path builds this shape: the demo plan
    stores no legs at all, and every product writer pairs consecutive activities, so it
    produces ``len(activities) - 1`` of them.  The schema permits more - a leg's
    Activity endpoints are nullable, and a provider-backed connection between two place
    keys is a complete fact on its own - which is exactly why the projection must not
    infer a Next from the leg list being longer than the activity list.

    So this case is contract hardening, not a reproduction: it pins the rule "STALE (or
    finished) means the whole tuple is empty" against the only state in which the old
    index-bound version could be caught lying.  Each leg carries its own duration and
    the AUTO mode, so a leaked one is recognisable in a projection instead of being
    mistaken for the legitimate answer of a different index.
    """
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.active_plan_version_id
        activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        )
        assert activities, "the demo plan has no activities to hang legs on"
        existing = list(
            session.scalars(
                select(JourneyLeg)
                .where(JourneyLeg.plan_version_id == journey.active_plan_version_id)
                .order_by(JourneyLeg.sequence_no)
            ).all()
        )
        target = len(activities) + 1
        sequence = max([leg.sequence_no for leg in existing], default=0)
        for offset in range(target - len(existing)):
            sequence += 1
            session.add(
                JourneyLeg(
                    id=f"leg-qoder-overflow-{journey_id[-6:]}-{sequence}",
                    journey_id=journey.id,
                    plan_version_id=journey.active_plan_version_id,
                    sequence_no=sequence,
                    from_key=f"place-qoder-overflow-{sequence}",
                    to_key=f"place-qoder-overflow-{sequence + 1}",
                    mode="AUTO",
                    depart_at=activities[-1].end_at,
                    arrive_at=activities[-1].end_at,
                    duration_min=29 + offset,
                    distance_m=1200,
                    cost_status="UNKNOWN",
                    walking_min=0,
                    transfers=0,
                    reliability_score=0.0,
                    weather_exposure="UNKNOWN",
                    provider_key="qoder-overflow-gate",
                    # Both columns are NOT NULL, and the honest value is a synthetic
                    # fixture: these legs exist to make the leg list longer than the
                    # activity list, not to pretend a provider measured them.
                    provenance="SYNTHETIC_FIXTURE",
                    captured_at=activities[-1].end_at,
                )
            )
        session.commit()
        return sequence


def _edge_view(journey_id: str) -> dict[str, Any]:
    """What the companion device is told, which used to be a second opinion."""
    created = client.post(
        f"/api/v1/journeys/{journey_id}/edge/devices",
        json={"display_name": "differential device"},
    )
    assert created.status_code in (200, 201), created.text
    device = created.json()
    snapshot = client.get(
        f"/api/v1/edge/devices/{device['device_id']}/journey-snapshot",
        headers={"X-Edge-Token": device["device_token"]},
    )
    assert snapshot.status_code == 200, snapshot.text
    return snapshot.json()


def test_a_stale_cursor_with_more_legs_than_activities_nulls_the_whole_tuple() -> None:
    """ORD-0006: STALE means Now, Next *and* the NextLeg are all empty.

    Asserted as one tuple on purpose.  Checking ``now_activity_id`` alone is what let
    a leg through with nothing in front of it, and the leak only appears in this
    shape, so a plan with more legs than activities is part of the fixture, not an
    incidental detail.  It must hold in the stored snapshot, in what the Today/Live
    surface is served, and in what the device is told.
    """
    journey_id, activities = _started_journey()
    _legs_beyond_the_last_activity(journey_id)
    activities_now, legs_now, _cursor = _plan_rows(journey_id)
    stored_legs, stored_activities = len(legs_now), len(activities_now)
    assert stored_legs > stored_activities, (
        "the fixture no longer has more legs than activities, so this case "
        "measures nothing"
    )

    _set_cursor(journey_id, _off_plan_activity_id(journey_id))
    projection = _served_projection(journey_id)
    assert projection["cursor_status"] == "STALE"
    assert projection == {
        **projection,
        "now": None,
        "now_activity_id": None,
        "now_state": "STALE_CURSOR",
        "next": None,
        "next_activity_id": None,
        "next_state": None,
        "next_leg_id": None,
        "next_mode": "NONE",
        "next_eta_min": 0,
    }, f"a stale cursor still projected part of the window: {projection}"
    assert projection["persisted_cursor"] not in {item["id"] for item in activities}

    edge = _edge_view(journey_id)
    assert edge["execution"]["next_leg_id"] is None
    assert (edge["mode"], edge["eta_min"], edge["next_leg_id"]) == ("NONE", 0, None), edge


def test_a_finished_journey_with_extra_legs_projects_no_departure_step() -> None:
    """The second leak path, reached without a corrupt cursor at all.

    A null cursor with every activity terminal is a legal end state: the absent-cursor
    fallback searches for the first pending step, finds none, and lands on the same
    out-of-range index a stale cursor does.  A device told "Journey complete" and two
    minutes later "walk 23 minutes" is the same fabrication in cleaner clothes.
    """
    journey_id, activities = _started_journey()
    _legs_beyond_the_last_activity(journey_id)
    _set_cursor(journey_id, None, {item["id"]: "COMPLETED" for item in activities})

    projection = _served_projection(journey_id)
    assert projection["cursor_status"] == "ABSENT_FALLBACK"
    assert projection["now_activity_id"] is None
    assert projection["now_state"] == "COMPLETE"
    assert projection["next_activity_id"] is None
    assert (projection["next_leg_id"], projection["next_mode"], projection["next_eta_min"]) == (
        None,
        "NONE",
        0,
    ), f"a finished journey still projected a leg: {projection}"

    edge = _edge_view(journey_id)
    assert (edge["mode"], edge["eta_min"], edge["next_leg_id"]) == ("NONE", 0, None), edge
    assert edge["next_title"] == "Journey complete"


def test_edge_and_v9_agree_on_whether_a_next_step_exists() -> None:
    """ORD-0007: one question, one answer.

    ``edge_snapshot`` used to call the shared projection with an empty leg list and
    then recompute the window from scratch, so a single response carried both answers
    in different fields: ``execution`` naming a next activity while the device fields
    said there was none.  The reproduced shape is a cursor left on a step the traveller
    finished while later steps are still pending - Now is genuinely empty, Next is not.
    """
    journey_id, activities = _started_journey()
    _set_cursor(journey_id, activities[1]["id"], {activities[1]["id"]: "COMPLETED"})

    projection = _served_projection(journey_id)
    assert projection["cursor_status"] == "VALID", projection
    assert projection["now_activity_id"] is None, projection
    assert projection["next_activity_id"], (
        "this case is only worth running while the shared projection does see a next step"
    )

    edge = _edge_view(journey_id)
    assert edge["next_state"] == projection["next_state"], (edge["next_state"], projection)
    assert edge["next_state"], edge
    assert edge["execution"]["next_activity_id"] == projection["next_activity_id"]
    assert (edge["mode"], edge["eta_min"]) == (
        projection["next_mode"],
        projection["next_eta_min"],
    ), "the device got a different leg answer than the snapshot it also carries"


def test_edge_and_v9_agree_when_there_really_is_no_next_step() -> None:
    """The other half of the same invariant, so agreement is not one-sided.

    With the cursor on the final pending step there is genuinely nothing after it:
    both consumers must answer null, and the surface must not fall back to "the first
    activity" or to a leftover leg to fill the gap.
    """
    journey_id, activities = _started_journey()
    last = activities[-1]["id"]
    _set_cursor(journey_id, last)

    projection = _served_projection(journey_id)
    assert projection["cursor_status"] == "VALID"
    assert projection["now_activity_id"] == last
    assert projection["next_activity_id"] is None

    edge = _edge_view(journey_id)
    assert edge["next_state"] is None, edge
    assert edge["next_title"] == "Journey complete"
    assert edge["execution"]["next_activity_id"] is None
    assert (edge["mode"], edge["eta_min"]) == (
        projection["next_mode"],
        projection["next_eta_min"],
    )

def _edge_sweep(journey_id: str) -> list[tuple[str, dict, dict]]:
    """Every cursor state the device can be handed, with both copies of the answer."""
    activities, _legs, _cursor = _plan_rows(journey_id)
    cases: list[tuple[str, dict, dict]] = []

    _set_cursor(journey_id, activities[1].id)
    cases.append(("mid-journey cursor", _edge_view(journey_id), _served_projection(journey_id)))

    _set_cursor(journey_id, activities[-1].id)
    cases.append(("final cursor", _edge_view(journey_id), _served_projection(journey_id)))

    _set_cursor(journey_id, activities[1].id, {activities[1].id: "COMPLETED"})
    cases.append(
        ("cursor on a finished step, later steps pending", _edge_view(journey_id), _served_projection(journey_id))
    )

    _legs_beyond_the_last_activity(journey_id)
    _set_cursor(journey_id, _off_plan_activity_id(journey_id))
    cases.append(("stale cursor with extra legs", _edge_view(journey_id), _served_projection(journey_id)))

    _set_cursor(journey_id, None, {item.id: "COMPLETED" for item in activities})
    cases.append(("finished journey", _edge_view(journey_id), _served_projection(journey_id)))

    return cases


def test_the_edge_copy_honours_the_same_leg_rule_as_the_projection() -> None:
    """Two consumers of one tuple, and the tuple's own structural rule on both.

    The device fields must not only equal the projection - they must each satisfy
    ``leg id is None <=> (mode, eta) == ("NONE", 0)`` on their own, because a response
    that is consistently wrong still leaves a watch face rendering a trip nobody promised.
    """
    journey_id, _activities = _started_journey()
    seen = 0
    for label, edge, projection in _edge_sweep(journey_id):
        seen += 1
        for side, values in (
            ("edge", (edge["next_leg_id"], edge["mode"], edge["eta_min"])),
            ("projection", (projection["next_leg_id"], projection["next_mode"], projection["next_eta_min"])),
        ):
            leg_id, mode, eta = values
            missing = leg_id is None
            sentinel = (mode, eta) == ("NONE", 0)
            assert missing == sentinel, (
                f"{label}: {side} encodes absence inconsistently "
                f"(leg_id={leg_id!r} mode={mode!r} eta={eta!r})"
            )
        assert (edge["next_leg_id"], edge["mode"], edge["eta_min"]) == (
            projection["next_leg_id"],
            projection["next_mode"],
            projection["next_eta_min"],
        ), f"{label}: the device was told a different leg story than the snapshot it also carries"
    assert seen == 5, f"only {seen} of the 5 cursor states were exercised"
