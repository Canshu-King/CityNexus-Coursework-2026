"""The plan handover must leave exactly one ACTIVE plan.

Found by the repository-owned stateful machine at 60 cases x 30 steps
(seed 20260921), and pinned here as a deterministic case so it cannot depend on a
random draw to stay fixed:

    generate -> start -> ... -> generate -> select(replacement)

`select_plan` only ever retired other SELECTED plans, so the plan the traveller was
executing stayed ACTIVE with nothing pointing at it.  The unique index cannot see that
shape - there is still exactly one ACTIVE row - which is what makes it a real hole
rather than a cosmetic one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Journey, PlanVersion
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def test_selecting_a_replacement_retires_the_plan_being_executed() -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    first = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{first['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    with SessionLocal() as session:
        started = session.get(Journey, journey_id)
        assert started.status == "ACTIVE"
        executed_plan = started.active_plan_version_id

    # A replacement arrives while the Journey is travelling.  This is the documented
    # resolution path for a hard recovery, not an illegal switch.
    more = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 3}
    ).json()
    replacement = next(item for item in more if item["id"] != executed_plan)
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{replacement['id']}/select"
    )
    assert selected.status_code == 200, selected.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        active = list(
            session.scalars(
                select(PlanVersion).where(
                    PlanVersion.journey_id == journey_id, PlanVersion.status == "ACTIVE"
                )
            ).all()
        )
        retired = session.get(PlanVersion, executed_plan)
        assert retired.status == "SUPERSEDED", (
            "the plan the traveller was executing was abandoned still-ACTIVE, so the "
            "database holds a plan nothing points at and the unique index cannot see "
            "the corruption"
        )
        # Selecting returns the Journey to PLANNED with the successor SELECTED, so the
        # honest rule is "no orphan": either nothing is ACTIVE yet, or the one ACTIVE
        # row is the plan the Journey names.
        assert not active or [item.id for item in active] == [journey.active_plan_version_id], (
            f"ACTIVE set {[item.id for item in active]} disagrees with the pointer "
            f"{journey.active_plan_version_id}"
        )
        # The cursor named a step on the retired plan; leaving it there would be a
        # stale cursor by definition.
        cursor = session.get(Activity, journey.current_activity_id)
        assert cursor is not None and cursor.plan_version_id == journey.active_plan_version_id, (
            "the execution cursor was left pointing into the retired plan"
        )

    projection = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]
    assert projection["cursor_status"] == "VALID", projection["cursor_status"]
