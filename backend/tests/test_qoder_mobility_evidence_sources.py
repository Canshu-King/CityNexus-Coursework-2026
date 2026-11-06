"""ORD-0079 - which mobility evidence does a recovery read, and can the demo stage replan?

The order was dispatched from a contradiction: `GET /route-evidence` answered
`fallback_reason=NO_FROZEN_MOBILITY_SNAPSHOT` while the same plan row carried a non-empty
`mobility_evidence_json`.  Reading the code names three different supplies of "the mobility
evidence", and the gates below pin that answer down with readings rather than with opinion:

* the **plan's own** sealed payload (`PlanVersion.mobility_evidence_json`, pointed at by
  `PlanVersion.mobility_snapshot_id`) is what `select_plan` validates and what `generate_replan`
  reads first (`frozen_v12_evidence`), so it is the thing a replan is priced with;
* a **proposal's** `diff_json.verified_evidence.mobility.snapshot` is what
  `GET /route-evidence` renders, via `_latest_replan_mobility_snapshot`.  Before any proposal
  exists there is nothing to render, and it says so - which is a true statement about the
  *map layer*, and was never a statement about the plan;
* a **runtime capture** fills legs the sealed matrix does not carry, and only with real
  provider facts (`require_explicit_facts=True` refuses the fixture's blanket constant).

So "no frozen mobility snapshot" and "the plan has 56 sealed pairs" are not in conflict: they
are two supplies, and the second half of this file proves the plan half is usable by walking
the whole of M3 - event, replan, accept, new ACTIVE plan - on the seeded stage.

Nothing here hand-writes evidence.  The only way these gates get a payload is by calling the
product's own routes; the two negative gates *delete* fields to prove the server's new
evidence states discriminate, and put every row back afterwards.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, PlanVersion
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
HARD_QUALITIES = {"EXPLICIT_FROZEN_FIXTURE", "LIVE_PROVIDER_CAPTURE"}


def _scene() -> dict:
    response = client.get("/api/v1/demo/scene")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def _executed_stage(monkeypatch) -> dict:
    """reset -> prepare -> start, i.e. the stage exactly as the panel puts it on screen."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    assert client.post("/api/v1/demo/prepare-scenario").status_code == 200
    started = client.post("/api/v1/demo/start-scenario")
    assert started.status_code == 200, started.text
    scene = _scene()
    assert scene["journey_status"] == "ACTIVE", scene
    assert scene["current_activity_id"], scene
    return scene


def _plan_row(plan_id: str) -> tuple[PlanVersion, dict, list[Activity]]:
    with SessionLocal() as session:
        plan = session.get(PlanVersion, plan_id)
        assert plan is not None, plan_id
        evidence = json.loads(plan.mobility_evidence_json or "{}")
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == plan.id)
            .order_by(Activity.sequence_no)
        ).all()
        session.expunge_all()
    return plan, evidence, list(activities)


def _event(
    journey_id: str,
    cursor_id: str | None,
    *,
    grounded: bool = True,
    event_type: str = "TRAFFIC_DELAY",
) -> str:
    payload: dict = {"delay_minutes": 25} if grounded else {}
    body = {
        "event_type": event_type,
        "severity": "MAJOR",
        "provenance": "SIMULATED",
        "affected_activity_id": cursor_id if grounded else None,
        "payload": {**payload, **({"affected_activity_id": cursor_id} if grounded else {})},
    }
    response = client.post(f"/api/v1/journeys/{journey_id}/events", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _replan(journey_id: str, event_id: str):
    return client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": 2},
        headers={"Idempotency-Key": f"ord79-{event_id}"},
    )


def test_the_selected_plan_carries_sealed_evidence_and_select_does_not_swap_the_pointer(
    _executed_stage,
) -> None:
    """The order's first question: is `mobility_snapshot_id` still the plan's own after select?

    It is: the column, the payload's `snapshot_id`, and what the scene read publishes are one
    and the same value, and the legs the plan walks are each covered by a fact whose quality is
    hard-eligible - i.e. the seeded stage is not running on the fixture's blanket constant.
    """

    scene = _executed_stage
    plan, evidence, activities = _plan_row(str(scene["plan_version_id"]))
    assert evidence.get("snapshot_id"), "the generated plan must carry a sealed snapshot"
    assert plan.mobility_snapshot_id == evidence["snapshot_id"] == scene["evidence_snapshot_id"], (
        plan.mobility_snapshot_id,
        evidence.get("snapshot_id"),
        scene["evidence_snapshot_id"],
    )
    assert scene["evidence_reference_consistent"] is True, scene
    # Measured, not assumed: a plan with no pairs would satisfy the pointer equality above while
    # still being unable to replan, so the count is asserted from the payload itself.
    assert len(evidence.get("facts") or []) > 0, evidence.keys()
    assert scene["mobility_legs_total"] == max(len(activities) - 1, 0), (
        scene["mobility_legs_total"],
        len(activities),
    )
    assert scene["mobility_legs_total"] > 0, "the seeded plan must walk at least one leg"
    assert scene["mobility_legs_hard_eligible"] == scene["mobility_legs_total"], scene
    qualities = {str(item.get("fact_quality")) for item in evidence["facts"]}
    assert qualities & HARD_QUALITIES, qualities
    assert plan.status == "ACTIVE", plan.status


def test_a_plan_without_a_declared_ttl_is_frozen_evidence_not_absent_evidence(
    _executed_stage,
) -> None:
    """The truth defect the contradiction was really about.

    `evidence_state` used to test `expires_at` and nothing else, so this plan - 56 sealed pairs,
    five of them consumed by the successful replan gated below - answered
    `NO_FROZEN_EVIDENCE`.  A panel that repeats the server then tells a traveller this trip has
    no travel data.  The honest answer is a fourth state, and it must not be reached by writing
    an expiry the providers never declared.
    """

    scene = _executed_stage
    _plan, evidence, _activities = _plan_row(str(scene["plan_version_id"]))
    assert evidence.get("expires_at") is None, "the seed declares no TTL; nothing here adds one"
    assert scene["evidence_expires_at"] is None, scene
    assert scene["evidence_state"] == "FROZEN_WITHOUT_TTL", scene
    assert scene["mobility_legs_hard_eligible"] > 0, scene


def test_route_evidence_before_any_proposal_reads_the_proposal_supply_not_the_plan(
    _executed_stage,
) -> None:
    """The two answers coexist, and neither one is a lie about the other.

    This is the pairing that dissolves the reported contradiction: the map read says it has no
    frozen snapshot *to draw a proposal's route from*, while the plan read says it has full
    leg coverage to price a replan with.  If either ever starts implying the other, this gate
    goes red - which is the point, because the confusion cost the order its first framing.
    """

    scene = _executed_stage
    journey_id = scene["journey_id"]
    response = client.get(f"/api/v1/journeys/{journey_id}/route-evidence")
    assert response.status_code == 200, response.text
    before = response.json()
    assert before["proposal_id"] is None, before
    assert before["snapshot_id"] is None, before
    assert before["frozen"] is False, before
    assert before["old_route"]["properties"]["fallback_reason"] == "NO_FROZEN_MOBILITY_SNAPSHOT", (
        before["old_route"]["properties"]
    )
    # ...and at the same moment the plan's own evidence is intact and hard-eligible.
    assert scene["evidence_snapshot_id"], scene
    assert scene["mobility_legs_hard_eligible"] == scene["mobility_legs_total"] > 0, scene


def test_the_m3_slice_completes_on_the_sealed_evidence(_executed_stage) -> None:
    """must 2: event -> replan -> accept -> a new ACTIVE plan, on the demo stage, no hand-fed facts."""

    scene = _executed_stage
    journey_id = scene["journey_id"]
    event_id = _event(journey_id, scene["current_activity_id"])
    generated = _replan(journey_id, event_id)
    assert generated.status_code == 201, generated.text
    proposal = generated.json()
    assert proposal["proposed_plan_version_id"], proposal
    assert proposal["hard_constraints_passed"] is True, proposal
    assert proposal["locked_commitments_preserved"] is True, proposal

    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept", headers={"Idempotency-Key": "ord79-accept"}
    )
    assert accepted.status_code == 200, accepted.text
    after = _scene()
    assert after["plan_version_id"] != scene["plan_version_id"], after
    assert after["journey_status"] == "ACTIVE", after
    with SessionLocal() as session:
        new_plan = session.get(PlanVersion, after["plan_version_id"])
        base_plan = session.get(PlanVersion, scene["plan_version_id"])
        assert new_plan is not None and new_plan.status == "ACTIVE", new_plan
        assert base_plan is not None and base_plan.status == "SUPERSEDED", base_plan.status
        new_evidence = json.loads(new_plan.mobility_evidence_json or "{}")
        base_evidence = json.loads(base_plan.mobility_evidence_json or "{}")
        # Measured, and it corrects what I first asserted here: the candidate is priced with a
        # snapshot re-captured at the incident instant (`generate_replan` -> `proposed
        # .mobility_snapshot_id = mobility.snapshot_id`), so its pointer legitimately differs
        # from the base plan's.  Drift is only a defect when a pointer stops naming its own
        # payload, or when the re-capture quietly loses legs - so both are pinned, not equality.
        assert new_plan.mobility_snapshot_id == new_evidence.get("snapshot_id"), (
            new_plan.mobility_snapshot_id,
            new_evidence.get("snapshot_id"),
        )
        assert base_plan.mobility_snapshot_id == base_evidence.get("snapshot_id"), (
            base_plan.mobility_snapshot_id,
            base_evidence.get("snapshot_id"),
        )
        assert new_plan.mobility_snapshot_id != base_plan.mobility_snapshot_id, (
            new_plan.mobility_snapshot_id,
            base_plan.mobility_snapshot_id,
        )
        assert str(new_plan.mobility_snapshot_id) == str(after["evidence_snapshot_id"]), after
        # Same provider, same hard-eligible quality: a re-capture must not launder a constant.
        assert new_evidence.get("source_provider") == base_evidence.get("source_provider"), (
            new_evidence.get("source_provider"),
            base_evidence.get("source_provider"),
        )
        assert {
            str(item.get("fact_quality")) for item in new_evidence.get("facts") or []
        } & HARD_QUALITIES, new_evidence.get("facts")
    assert after["evidence_state"] == "FROZEN_WITHOUT_TTL", after
    assert after["mobility_legs_hard_eligible"] == after["mobility_legs_total"] > 0, after

    # The map read now has a proposal to render, and stops reporting a fallback.
    route = client.get(
        f"/api/v1/journeys/{journey_id}/route-evidence",
        params={"proposal_id": proposal["id"]},
    )
    assert route.status_code == 200, route.text
    body = route.json()
    assert body["frozen"] is True, body
    assert body["snapshot_id"], body
    assert body["provider"] == "mock-mobility", body
    assert body["old_route"]["properties"]["fallback"] is False, body["old_route"]["properties"]
    assert body["new_route"]["properties"]["provider_backed"] is True, body["new_route"]["properties"]


def test_a_recovery_refused_for_a_missing_target_is_not_blamed_on_mobility(_executed_stage) -> None:
    """The failure face, kept distinguishable.

    Measured, including the half that surprised me: a WEATHER event with nothing grounded onto
    it is refused with `REPLAN_TARGET_UNRESOLVED`, while the same ungrounded TRAFFIC_DELAY is
    rescued by the deterministic impact fallback (it proposes removing the current stop and
    answers 201).  Only the refused shape belongs in this gate, and what it has to prove is that
    the refusal names the missing target and not the evidence - otherwise the next investigation
    chases mobility facts that were never the problem, which is exactly how this order was
    originally framed.
    """

    scene = _executed_stage
    event_id = _event(
        scene["journey_id"], scene["current_activity_id"], grounded=False, event_type="WEATHER"
    )
    response = _replan(scene["journey_id"], event_id)
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert "REPLAN_TARGET_UNRESOLVED" in body["message"], body
    assert "MOBILITY" not in body["message"].upper(), body


def test_the_read_fails_closed_when_the_plan_really_has_nothing(_executed_stage) -> None:
    """Prove the new states discriminate, by removing the evidence and reading again.

    Without this gate `FROZEN_WITHOUT_TTL` could be returned unconditionally and every other
    assertion above would still pass.  The row is put back at the end, in the same shape.
    """

    scene = _executed_stage
    plan_id = str(scene["plan_version_id"])
    with SessionLocal() as session:
        plan = session.get(PlanVersion, plan_id)
        assert plan is not None
        original_payload, original_pointer = plan.mobility_evidence_json, plan.mobility_snapshot_id
        plan.mobility_evidence_json = json.dumps({"facts": []})
        plan.mobility_snapshot_id = None
        session.commit()
    try:
        empty = _scene()
        assert empty["evidence_state"] == "NO_FROZEN_EVIDENCE", empty
        assert empty["evidence_snapshot_id"] is None, empty
        assert empty["mobility_legs_total"] > 0, empty
        assert empty["mobility_legs_hard_eligible"] == 0, empty
    finally:
        with SessionLocal() as session:
            plan = session.get(PlanVersion, plan_id)
            plan.mobility_evidence_json = original_payload
            plan.mobility_snapshot_id = original_pointer
            session.commit()
    restored = _scene()
    assert restored["evidence_state"] == "FROZEN_WITHOUT_TTL", restored
    assert restored["evidence_snapshot_id"] == scene["evidence_snapshot_id"], restored


def test_a_pointer_that_disagrees_with_its_own_payload_is_reported_as_inconsistent(
    _executed_stage,
) -> None:
    """The fourth state exists so a corrupted reference cannot hide behind a healthy count."""

    scene = _executed_stage
    plan_id = str(scene["plan_version_id"])
    with SessionLocal() as session:
        plan = session.get(PlanVersion, plan_id)
        assert plan is not None
        original_pointer = plan.mobility_snapshot_id
        plan.mobility_snapshot_id = "00000000-0000-4000-8000-000000000000"
        session.commit()
    try:
        drifted = _scene()
        assert drifted["evidence_state"] == "EVIDENCE_REFERENCE_INCONSISTENT", drifted
        assert drifted["evidence_reference_consistent"] is False, drifted
        # Counted from the payload, which is still readable: the inconsistency is reported
        # alongside the facts rather than replacing them.
        assert drifted["mobility_legs_total"] == scene["mobility_legs_total"], drifted
    finally:
        with SessionLocal() as session:
            plan = session.get(PlanVersion, plan_id)
            plan.mobility_snapshot_id = original_pointer
            session.commit()
    assert _scene()["evidence_state"] == "FROZEN_WITHOUT_TTL", _scene()
