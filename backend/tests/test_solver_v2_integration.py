from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from test_solver_runtime_gate import _generate, _prepare_disruption, _proposals, client

from app.db.models import Activity, PlanVersion
from app.db.session import SessionLocal


def test_replan_v2_materializes_multi_slot_candidates_through_the_existing_gate(monkeypatch):
    monkeypatch.setenv("CITYNEXUS_SOLVER", "v2")
    journey_id, _base, event = _prepare_disruption()
    chosen = _generate(journey_id, event["id"], count=3)
    proposals = _proposals(journey_id, event["id"])

    assert proposals
    assert any(
        item["diff"]["verified_evidence"]["generator"]["kind"]
        in {"ORTOOLS_CP_SAT_V2", "CITYNEXUS_SOLVER_V2_ENUMERATION"}
        for item in proposals
    )
    assert any(
        item["diff"]["verified_evidence"]["generator"]
        .get("objective", {})
        .get("changed_activity_count", 0)
        >= 2
        for item in proposals
    )
    v2 = next(
        item
        for item in proposals
        if item["diff"]["verified_evidence"]["generator"]["kind"]
        in {"ORTOOLS_CP_SAT_V2", "CITYNEXUS_SOLVER_V2_ENUMERATION"}
    )
    assert v2["locked_commitments_preserved"] is True
    assert v2["diff"]["verified_evidence"]["feasibility_authority"] == (
        "CITYNEXUS_DETERMINISTIC_GATE"
    )
    assert v2["diff"]["verified_evidence"]["generator"]["mobility_snapshot_id"]

    with SessionLocal() as session:
        plan = session.get(PlanVersion, v2["proposed_plan_version_id"])
        assert plan is not None
        rows = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == plan.id)
            .order_by(Activity.sequence_no)
        ).all()
        assert rows
        assert any(item.place_id == "place-river-cruise" for item in rows)

    # The endpoint still uses the ordinary proposal/acceptance contract.
    accepted = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": f"v2-accept-{uuid4()}"},
    )
    assert accepted.status_code == 200, accepted.text


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import SOLVER_V2_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
