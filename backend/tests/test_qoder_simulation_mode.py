"""ORD-0058 - Simulation Mode has to be a claim a process makes, not a route anyone can hit.

Three things are pinned here, one per door:

* `POST /api/v1/demo/reset` deletes 52 tables and previously needed no flag, env or auth.  With
  the switch off it must answer a named 403 **and delete nothing**, which is why the gate counts
  rows rather than trusting the status code;
* `GET /api/v1/demo/scene` is the one read that was missing: a client that wants a banner or a
  Today header had to call `new Date()` and so invented a second clock.  The gate asserts the
  reported moment is the seeded scenario's, not this machine's;
* the flagged `projection_now` inference is settled by measurement - a `?now=` without an offset
  used to reach a naive-vs-aware comparison, i.e. a 500 for a query-string detail, and now gets a
  named 422 while the offset-aware form keeps working (the control half of that gate).

The seed frozen-evidence TTL alignment is deliberately not covered by this file: it ships as its
own slice with the two regression sets the order names, because guessing there would weaken a
fail-closed gate on the frozen list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Activity, Journey, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app, demo_mode_enabled
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TABLES = ("travelers", "journeys", "plan_versions", "activities")


def _counts() -> dict[str, int]:
    with SessionLocal() as session:
        return {
            "travelers": session.scalar(select(func.count(Traveler.id))) or 0,
            "journeys": session.scalar(select(func.count(Journey.id))) or 0,
            "plan_versions": session.scalar(select(func.count(PlanVersion.id))) or 0,
            "activities": session.scalar(select(func.count(Activity.id))) or 0,
        }


@pytest.fixture()
def _stage_on(monkeypatch) -> None:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200


def _scene() -> dict:
    response = client.get("/api/v1/demo/scene")
    assert response.status_code == 200, response.text
    return response.json()


def _journey_in_scene() -> Journey:
    """Load the journey the scene itself named, so the gate cannot drift from the route."""

    scene = _scene()
    assert scene["journey_id"], scene
    with SessionLocal() as session:
        row = session.get(Journey, scene["journey_id"])
    assert row is not None, scene
    return row


def test_the_reset_route_is_closed_without_the_switch_and_deletes_nothing(monkeypatch) -> None:
    """The refusal has to be proven by the rows, not by the status code.

    A 403 that fires *after* `reset_all` would look identical to a good guard and still cost
    every traveller in the library.
    """

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: False)
    # A row the frozen seed cannot produce: without this, "counts unchanged" passes even when
    # the route destroys everything and re-seeds identical rows, which is exactly what the first
    # kill-evidence batch proved (mutant T2 survived on that blind spot).
    with SessionLocal() as session:
        traveler = session.scalar(select(Traveler.id))
    assert traveler is not None, "needs a seeded traveler to own the marker journey"
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": "marker row",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-12-01T09:00:00+08:00",
            "end_at": "2026-12-02T18:00:00+08:00",
            "budget": {"amount_minor": 20000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    marker_id = created.json()["id"]

    before = _counts()
    assert before["journeys"] > 0, "needs a populated library so deletion would be observable"
    response = client.post("/api/v1/demo/reset")
    assert response.status_code == 403, response.text
    body = response.json()["error"]
    assert body["code"] == "DEMO_MODE_REQUIRED", body
    assert body["message_key"] == "demo.reset_disabled", body
    assert body["details"]["flag"] == "CITYNEXUS_DEMO_MODE", body["details"]
    assert body["details"]["tables_protected"] is True, body["details"]
    assert _counts() == before, "a refused reset must not have touched a single row"
    with SessionLocal() as session:
        assert session.get(Journey, marker_id) is not None, (
            "the refused reset destroyed a journey the seed cannot reproduce - the guard fires"
            " after the deletion, not before it"
        )
        assert session.scalar(select(func.count(Traveler.id))) > 0


def test_the_same_call_is_allowed_once_the_process_claims_the_stage(_stage_on) -> None:
    """The guard discriminates on the flag and on nothing else."""

    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "reset"
    assert UUID(payload["traveler_id"]), payload
    assert _counts()["journeys"] > 0


def test_the_scene_read_reports_the_scenario_clock_and_not_this_machine(_stage_on) -> None:
    """`effective_now` must be the journey's own moment.

    If it ever becomes `Date.now()` on the server, the banner and Today header agree with the
    device instead of the scene, which is the disagreement this read exists to remove.
    """

    journey = _journey_in_scene()
    scene = _scene()
    assert scene["demo_mode"] is True, scene
    assert scene["journey_status"] == journey.status, (scene["journey_status"], journey.status)
    assert scene["journey_id"] == journey.id, (scene["journey_id"], journey.id)
    assert scene["basis"] in {"CURSOR", "JOURNEY_START"}, scene
    moment = datetime.fromisoformat(str(scene["effective_now"]))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    drift = abs((datetime.now(timezone.utc) - moment).total_seconds())
    assert drift > 86_400, f"the scene clock is within a day of wall time: {drift}s"
    assert scene["scene_start_at"] <= scene["scene_end_at"], scene
    assert scene["evidence_state"] in {
        "REFERENCED",
        "FROZEN_WITHOUT_TTL",
        "EVIDENCE_REFERENCE_INCONSISTENT",
        "NO_FROZEN_EVIDENCE",
    }, scene


def test_reset_leaves_a_bookable_stage_and_start_scenario_makes_it_executable(_stage_on) -> None:
    """The two halves of ORD-0078, measured through the routes a client would call.

    Before this split, reset ran all the way to ACTIVE, which made `POST /bookings` answer
    422 "booking requires a PLANNED Journey" forever on the demo stage - the failure was
    measured by the commander, not inferred.  Starting is now an action, and the cursor the
    panel needs only appears after it.
    """

    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    assert client.post("/api/v1/demo/prepare-scenario").status_code == 200
    scene = _scene()
    journey_id = scene["journey_id"]
    # ORD-0078 is about the *PLANNED* gate, so the assertion has to distinguish that refusal
    # from a legitimate one: a stop with no supported commerce service is not bookable for
    # reasons this order does not touch, and pretending otherwise would test nothing.
    with SessionLocal() as session:
        activity_ids = [
            row.id
            for row in session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == scene["plan_version_id"])
                .order_by(Activity.sequence_no)
            ).all()
        ]
    assert activity_ids, scene
    codes: list[tuple[str, int]] = []
    booked_status = 0
    for candidate in activity_ids:
        response = client.post(
            f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": candidate}
        )
        codes.append((candidate, response.status_code))
        assert "PLANNED" not in response.text, (
            "reset put the journey out of bookable state again: " + response.text[:200]
        )
        if response.status_code < 400:
            booked_status = response.status_code
            break
    assert booked_status == 201, codes

    started = client.post("/api/v1/demo/start-scenario")
    assert started.status_code == 200, started.text[:300]
    body = started.json()
    assert body["status"] == "started", body
    assert body["journey_status"] == "ACTIVE", body
    assert body["current_activity_id"], body

    after_start = _scene()
    assert after_start["journey_status"] == "ACTIVE", after_start
    assert after_start["basis"] == "CURSOR", after_start
    cursor = after_start["effective_now"]

    completed = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{body['current_activity_id']}/complete", json={}
    )
    assert completed.status_code < 400, (completed.status_code, completed.text[:300])
    moved = _scene()
    assert moved["current_activity_id"] != body["current_activity_id"], moved
    assert moved["effective_now"] != cursor, (moved["effective_now"], cursor)


def test_start_scenario_is_behind_the_same_switch_as_reset(monkeypatch) -> None:
    """A state-changing demo action gets the same refusal, with its own key."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: False)
    response = client.post("/api/v1/demo/start-scenario")
    assert response.status_code == 403, response.text[:200]
    body = response.json()["error"]
    assert body["code"] == "DEMO_MODE_REQUIRED", body
    assert body["message_key"] == "demo.start_disabled", body


def test_the_scene_clock_moves_with_the_scenario_and_never_with_the_device(_stage_on) -> None:
    """Change the journey's window and the reported moment moves; wall time cannot make it move.

    The contract this read exists to provide: the scenario's clock is the answer.  A client
    that called `new Date()` would keep disagreeing with the seeded day no matter what the
    journey said, and the banner would look right while the plan underneath was elsewhere.
    """

    before = _scene()
    assert before["basis"] in {"CURSOR", "JOURNEY_START"}, before
    journey = _journey_in_scene()
    shifted = "2027-03-04T09:00:00+08:00"
    with SessionLocal() as session:
        row = session.get(Journey, journey.id)
        row.start_at = shifted
        row.end_at = "2027-03-06T18:00:00+08:00"
        session.commit()
    after = _scene()
    # Which moment wins is stated, not guessed: an ACTIVE journey is read from its cursor, so
    # moving the window must not move it, while a journey with no cursor is anchored on the
    # window itself.  Either way the answer is the scenario's instant and never wall time.
    if after["basis"] == "CURSOR":
        assert after["effective_now"] == before["effective_now"], (before, after)
    else:
        assert after["scene_start_at"] == shifted, after
        assert after["effective_now"] == shifted, after
    assert after["effective_now"] != datetime.now(timezone.utc).isoformat(), after


def test_the_seeded_stage_is_executable_and_both_branches_share_one_key_set(_stage_on) -> None:
    """After the button a teacher presses, the panel must have a real step to take.

    Two halves, both asserted:

    (a) `demo/reset` seeds only: the journey stays DRAFT and the library holds **no**
        PlanVersion.  Seeding a plan from here is what leaked a SELECTED plan into `GET /plans`
        (which does not filter by status) and turned 35 gates red, one of them in isolation -
        a fixture door may reset state and nothing else;
    (b) `demo/prepare-scenario` reaches PLANNED + SELECTED through the real pipeline and answers
        with the advance as an object (ids stringified), which is the state the booking validator
        requires;
    (c) the populated stage and an empty stage answer with **equal key sets** - a strict client
        shape check treats a missing key as "cannot read the envelope", which would render an
        honest empty stage as an outage (the ORD-0070 family, from the server side).
    """

    response = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "simulation_advance" not in payload, "reset must not run the pipeline any more"
    with SessionLocal() as session:
        seeded_plans = session.scalars(select(PlanVersion)).all()
        journey = session.get(Journey, payload["journey_id"]) if "journey_id" in payload else session.scalar(
            select(Journey).order_by(Journey.created_at.desc())
        )
    assert seeded_plans == [], [plan.status for plan in seeded_plans]
    assert journey.status == "DRAFT", journey.status

    prepared = client.post("/api/v1/demo/prepare-scenario")
    assert prepared.status_code == 200, prepared.text[:400]
    body = prepared.json()
    assert body["status"] == "prepared", body
    assert body["journey_status"] == "PLANNED", body
    assert body["plan_version_id"], body
    advance = body["advance"]
    assert isinstance(advance, dict), f"advance came back as {type(advance).__name__}"
    steps = {item["step"]: item for item in advance["steps"]}
    assert "plans/generate" in steps and "select" in steps, advance
    assert "start" not in steps, f"prepare must not start: {advance}"
    assert all(item.get("ok") for item in advance["steps"]), advance
    assert isinstance(advance["total_ms"], float), advance

    scene = _scene()
    assert scene["journey_status"] == "PLANNED", scene
    assert scene["plan_version_id"] == body["plan_version_id"], (scene, body)
    assert scene["basis"] == "JOURNEY_START", scene
    assert scene["current_activity_id"] is None, scene
    assert scene["effective_now"], scene
    with SessionLocal() as session:
        plan = session.get(PlanVersion, scene["plan_version_id"])
        assert plan is not None and plan.status == "SELECTED", (plan.status if plan else None)
        journey = session.get(Journey, scene["journey_id"])
        assert journey.active_plan_version_id == scene["plan_version_id"], journey.active_plan_version_id
    populated = set(scene)

    # Empty stage: remove the journeys the seed created, then restore them by resetting again so
    # no other module inherits a wiped library.
    try:
        with SessionLocal() as session:
            for row in session.scalars(select(Journey)).all():
                session.delete(row)
            session.commit()
        empty = _scene()
        assert empty["evidence_state"] == "NO_SCENARIO", empty
        assert set(empty) == populated, (sorted(populated - set(empty)), sorted(set(empty) - populated))
        assert empty["plan_version_id"] is None, empty
    finally:
        assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200


def _cursor_id(scene: dict) -> str | None:
    with SessionLocal() as session:
        row = session.get(Journey, scene["journey_id"])
    return row.current_activity_id if row is not None else None



def test_a_naive_projection_instant_is_refused_by_name_instead_of_500(_stage_on) -> None:
    """The order's "measure it before you rely on it" item, plus its control half."""

    journey = _journey_in_scene()
    naive = client.get(f"/api/v1/journeys/{journey.id}/twin?now=2026-10-01T09:00")
    assert naive.status_code != 500, naive.text[:300]
    assert naive.status_code == 422, naive.text[:300]
    body = naive.json()["error"]
    assert body["code"] == "TWIN_NOW_OFFSET_REQUIRED", body
    assert body["message_key"] == "twin.now_offset_required", body
    assert body["details"]["offset_required"] is True, body["details"]
    aware = client.get(f"/api/v1/journeys/{journey.id}/twin?now=2026-10-01T09:00:00%2B08:00")
    assert aware.status_code == 200, aware.text[:300]
