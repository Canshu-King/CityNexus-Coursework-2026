"""ORD-0097 - "which plan is now" is answered by the server or by nobody.

`GET /api/v1/journeys/{id}/plans` has never filtered by status, and that part is correct:
CANDIDATE / SELECTED / ACTIVE / SUPERSEDED / REJECTED rows are all history a caller may show.
What was missing is the pointer. Every consumer - the phone UI, and the tests that polluted
each other - had to infer "the current one" from where a row sat in the array, which is not a
fact the endpoint ever promised. Measured here: after `prepare` plus one more `plans/generate`
the current plan is **index 0 of 6**, and after selecting a different candidate it is the
**last** row - so no fixed position, no `version_no` rule and no status test agrees with the
journey's own `active_plan_version_id` in both states.

The endpoint's shape is unchanged in every other way: same rows, same order, same fields, with
`is_current` added. The last gate is the fail-closed face - a journey whose pointer is gone
must name nothing rather than defaulting to the first row, because "assume the top one" is
exactly the bug being removed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Journey, PlanVersion
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


@pytest.fixture()
def _stage(monkeypatch) -> str:
    """reset -> prepare -> a second generate, so one journey holds several versions."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    assert client.post("/api/v1/demo/prepare-scenario").status_code == 200
    journey_id = client.get("/api/v1/demo/scene").json()["journey_id"]
    generated = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={})
    assert generated.status_code == 201, generated.text
    return journey_id


def _listed(journey_id: str) -> list[dict]:
    response = client.get(f"/api/v1/journeys/{journey_id}/plans")
    assert response.status_code == 200, response.text
    return response.json()


def _pointer(journey_id: str) -> str | None:
    with SessionLocal() as session:
        return session.get(Journey, journey_id).active_plan_version_id


def _stored_ids(journey_id: str) -> set[str]:
    with SessionLocal() as session:
        return {
            row.id
            for row in session.scalars(
                select(PlanVersion).where(PlanVersion.journey_id == journey_id)
            ).all()
        }


def test_the_current_plan_is_named_and_is_not_the_last_row(_stage) -> None:
    """The state that kills every position rule: current at index 0, five rows behind it."""

    journey_id = _stage
    items = _listed(journey_id)
    assert len(items) >= 3, items
    current = [item for item in items if item["is_current"]]
    assert len(current) == 1, [
        (item["version_no"], item["status"], item["is_current"]) for item in items
    ]
    assert current[0]["id"] == _pointer(journey_id), "is_current must mirror the pointer"
    assert items[0]["id"] == current[0]["id"], items
    # The rows after it are real versions of the same journey that are *not* current, so
    # "last item", "highest version_no" and "newest row" are each demonstrably wrong here.
    later = [item for item in items if item["version_no"] > current[0]["version_no"]]
    assert later and all(item["is_current"] is False for item in later), later
    assert max(item["version_no"] for item in items) != current[0]["version_no"], items


def test_selecting_another_candidate_moves_the_answer_not_the_order(_stage) -> None:
    """Same array, different truth: the pointer moved to the last row."""

    journey_id = _stage
    items = _listed(journey_id)
    before_ids = [item["id"] for item in items]
    candidate = next(item for item in reversed(items) if item["status"] == "CANDIDATE")
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{candidate['id']}/select", json={}
    )
    assert selected.status_code == 200, selected.text

    after = _listed(journey_id)
    assert [item["id"] for item in after] == before_ids, (
        "the endpoint returns every version in the same order - only `is_current` may differ"
    )
    current = [item for item in after if item["is_current"]]
    assert len(current) == 1 and current[0]["id"] == candidate["id"], after
    assert after.index(current[0]) == len(after) - 1, "this face is only real if order moved"
    assert {item["status"] for item in after} >= {"SELECTED", "CANDIDATE", "REJECTED"}, after
    assert _pointer(journey_id) == candidate["id"]
    # A journey that was never started has no cursor, and switching its selection must not
    # invent one - "the cursor appears after start" is the stage contract (ORD-0078).
    assert selected.json()["current_activity_id"] is None, selected.text


def test_switching_while_a_plan_is_executing_still_supersedes_it(_stage) -> None:
    """The regression face of scoping that guard: the executing case is unchanged.

    Measured before the guard was scoped correctly, this call answered 409 for *both* cases;
    the point of narrowing it was to let the un-started case through without weakening the
    one the guard exists for - so that one is asserted here, not assumed.
    """

    journey_id = _stage
    started = client.post("/api/v1/demo/start-scenario")
    assert started.status_code == 200, started.text
    executing = next(item for item in _listed(journey_id) if item["is_current"])
    assert executing["status"] == "ACTIVE", executing
    assert client.get(f"/api/v1/journeys/{journey_id}").json()["current_activity_id"], "need a cursor"

    candidate = next(
        item for item in _listed(journey_id) if item["status"] == "CANDIDATE"
    )
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{candidate['id']}/select", json={}
    )
    assert response.status_code == 200, response.text

    after = {item["id"]: item for item in _listed(journey_id)}
    assert after[executing["id"]]["status"] == "SUPERSEDED", after[executing["id"]]
    assert after[executing["id"]]["is_current"] is False, after
    assert after[candidate["id"]]["is_current"] is True, after[candidate["id"]]
    moved = client.get(f"/api/v1/journeys/{journey_id}").json()["current_activity_id"]
    assert moved, "the abandoning of an executing plan must re-point the cursor, not drop it"
    with SessionLocal() as session:
        cursor_owner = session.scalar(
            select(Activity).where(Activity.id == moved)
        )
        assert cursor_owner.plan_version_id == candidate["id"], (
            cursor_owner.plan_version_id,
            candidate["id"],
        )


def test_a_journey_without_a_pointer_names_nothing_rather_than_the_first_row(_stage) -> None:
    """Fail-closed on a state the product cannot produce, so a lost pointer is visible.

    Written straight to the row and restored afterwards: the corruption is the point - a
    caller must see "nothing is current" instead of an endpoint quietly electing index 0.
    """

    journey_id = _stage
    items = _listed(journey_id)
    original = _pointer(journey_id)
    assert original is not None
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        journey.active_plan_version_id = None
        session.commit()
    try:
        orphaned = _listed(journey_id)
        assert [item["id"] for item in orphaned] == [item["id"] for item in items]
        assert any(item["status"] in {"SELECTED", "ACTIVE"} for item in orphaned), orphaned
        assert not [item for item in orphaned if item["is_current"]], orphaned
    finally:
        with SessionLocal() as session:
            session.get(Journey, journey_id).active_plan_version_id = original
            session.commit()
    restored = _listed(journey_id)
    assert [item["id"] for item in restored if item["is_current"]] == [
        item["id"] for item in restored if item["id"] == original
    ], restored


def test_the_field_adds_nothing_else_and_costs_the_list_no_extra_queries(_stage) -> None:
    """`is_current` is the only change: every row, field and order the list had before stays.

    Also pins that the projection is supplied by the caller that already loaded the journey,
    so naming the current plan cannot turn a list read into N+1 selects.
    """


    journey_id = _stage
    items = _listed(journey_id)
    assert set(items[0]) >= {
        "id",
        "version_no",
        "status",
        "activities",
        "mobility_snapshot_id",
        "stale",
        "selectable",
        "is_current",
    }, sorted(items[0])
    assert {item["id"] for item in items} == _stored_ids(journey_id), "no row dropped or added"
    assert all(item["version_no"] >= 1 for item in items)

    from app import services

    with SessionLocal() as session:
        plan = session.scalar(
            select(PlanVersion).where(PlanVersion.journey_id == journey_id).order_by(PlanVersion.id)
        )
        assert plan is not None
        # An explicit None is a *known* answer ("this journey has no pointer"), so the
        # projection must not go looking for one, and must not elect a row by default.
        assert services.plan_schema(
            session, plan, current_plan_version_id=None
        ).is_current is False
        assert services.plan_schema(session, plan).is_current == (
            plan.id == _pointer(journey_id)
        )
