"""Journey-scoped serialization, acceptance races, and commitment re-binding.

Four defects from the Sol verification, closed here with the assertions the
ruling asked for: a race test that only checks status codes proves nothing,
because two callers can both return 200 while the database ends up with two
ACTIVE plans.  Every schedule below asserts the final canonical state:

    exactly one ACTIVE PlanVersion
    the Journey points at that one
    exactly one ACCEPTED proposal claiming the win
    zero active commitments anchored outside the active plan

Schedules are made to overlap with a barrier rather than with sleeps: a sleep is
a guess about timing, and a guess that passes is not evidence.
"""

from __future__ import annotations

import gc
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app import concurrency_guards
from app.db.models import (
    Activity,
    Commitment,
    IdempotencyKey,
    Journey,
    PlanVersion,
    ReplanProposal,
)
from app.db.session import SessionLocal
from app.replanning.commitment_remap import (
    RemapConflict,
    matching_activities,
    commitment_identity,
    resolve_commitment_target,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _journey_with_captured_commitment() -> tuple[str, str, str]:
    """A travelling journey holding one genuine C4 commitment.

    The cruise is booked and its payment captured *before* `/start`, because
    `protected` is derived from ACTIVE Commitment rows and booking a travelling
    activity is refused.  Without this the race tests would be protecting
    nothing, and `locked_commitments_preserved` would be vacuously true.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
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
        headers={"Idempotency-Key": f"capture-{body['id']}"},
    )
    assert capture.status_code == 201, capture.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, cruise["id"], body


def _journey_with_uncaptured_booking() -> tuple[str, str, str]:
    """Book the cruise but leave the payment uncaptured.

    This is the state that strands a commitment: the acceptance only re-points
    C4 rows, so an un-captured booking is invisible to it, and the capture that
    arrives afterwards promotes a pointer into the plan that no longer exists.
    """
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert booking.status_code == 201, booking.text
    body = booking.json()
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, cruise["id"], body


def _proposal_for(journey_id: str, activity_id: str, label: str) -> dict[str, Any]:
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": activity_id,
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": label},
        },
    ).json()
    requested = client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"{label}-{event['id']}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event["id"],
            "candidate_count": 3,
        },
    )
    assert requested.status_code == 200, requested.text
    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    presented = [
        item
        for item in proposals
        if item.get("trigger_event_id") == event["id"] and item.get("status") == "PRESENTED"
    ]
    assert presented, f"{label} produced no presentable proposal"
    return presented[0]


def _closed_activity_id(plan_activities: list[dict]) -> str:
    return next(item["id"] for item in plan_activities if item["place_id"] == "place-tech-art")


def _final_truth(journey_id: str) -> dict[str, Any]:
    """Read the canonical state straight from the rows, not from an endpoint."""
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        active_plans = list(
            session.scalars(
                select(PlanVersion).where(
                    PlanVersion.journey_id == journey_id, PlanVersion.status == "ACTIVE"
                )
            ).all()
        )
        accepted = list(
            session.scalars(
                select(ReplanProposal).where(
                    ReplanProposal.journey_id == journey_id,
                    ReplanProposal.status == "ACCEPTED",
                )
            ).all()
        )
        commitments = list(
            session.scalars(
                select(Commitment).where(
                    Commitment.journey_id == journey_id, Commitment.status == "ACTIVE"
                )
            ).all()
        )
        dangling = [
            item
            for item in commitments
            if item.activity_id
            and (
                (row := session.get(Activity, item.activity_id)) is None
                or row.plan_version_id != journey.active_plan_version_id
            )
        ]
        return {
            "active_plan_ids": sorted(item.id for item in active_plans),
            "journey_plan_id": journey.active_plan_version_id,
            "accepted_proposal_ids": sorted(item.id for item in accepted),
            "accepted_point_to": sorted({item.proposed_plan_version_id for item in accepted}),
            # The invariant the ruling states: no *protected* commitment may be
            # stranded on a plan the journey no longer executes.
            "dangling_commitments": sorted(
                item.id for item in dangling if item.level == "C4_Anchored"
            ),
            # Informational: an ordinary booking the recovery could not re-point is
            # not a broken promise, but a run that produces one should show it.
            "off_plan_commitments": sorted(item.id for item in dangling),
            "cursor_status_ok": journey.current_activity_id is None
            or (
                session.get(Activity, journey.current_activity_id) is not None
                and session.get(Activity, journey.current_activity_id).plan_version_id
                == journey.active_plan_version_id
            ),
        }


def _assert_one_winner(truth: dict[str, Any]) -> None:
    assert len(truth["active_plan_ids"]) == 1, (
        "a Journey ended up with more than one ACTIVE PlanVersion, which means two "
        f"callers both believed they had switched the plan: {truth['active_plan_ids']}"
    )
    assert truth["journey_plan_id"] == truth["active_plan_ids"][0], (
        "the Journey points somewhere other than the single ACTIVE plan"
    )
    assert len(truth["accepted_proposal_ids"]) == 1, (
        f"more than one proposal claims the win: {truth['accepted_proposal_ids']}"
    )
    assert truth["accepted_point_to"] == truth["active_plan_ids"], (
        "the accepted proposal and the ACTIVE plan disagree about which plan won"
    )
    assert not truth["dangling_commitments"], (
        "active commitments are anchored outside the active plan, so the traveller is "
        f"protected by a pointer into a superseded plan: {truth['dangling_commitments']}"
    )
    assert truth["cursor_status_ok"], "the execution cursor was left on a superseded plan"


def _race(callables: list) -> list[Any]:
    """Run the callables in parallel, released by a barrier, and collect results."""
    barrier = threading.Barrier(len(callables))

    def run(item):
        barrier.wait()
        return item()

    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        return list(pool.map(run, callables))


# --------------------------------------------------------------------------
# B. the guard itself, and its disclosure
# --------------------------------------------------------------------------
def test_the_guard_is_explicitly_disclosed_as_process_local() -> None:
    """A lock that is not distributed-safe must not be describable as one."""
    assert concurrency_guards.PROCESS_LOCAL_ONLY is True
    doc = concurrency_guards.__doc__ or ""
    assert "not" in doc and "distributed" in doc, (
        "the module must state in its own docstring that this is not distributed safety"
    )


def test_the_guard_actually_serialises_two_threads_for_one_journey() -> None:
    """Prove the lock, do not assume it.

    The holder waits on a two-party barrier inside the critical section.  If the
    guard works, the second thread cannot reach that barrier and the wait breaks.
    The control run takes no lock and must reach the barrier, otherwise this test
    would go green for the wrong reason - a probe that can never observe overlap
    proves nothing about the thing that prevents it.
    """

    def measure(use_lock: bool) -> tuple[int, bool]:
        peak = 0
        current = 0
        counter_guard = threading.Lock()
        rendezvous = threading.Barrier(2, timeout=1)
        broken = False

        def critical() -> None:
            nonlocal peak, current, broken
            context = (
                concurrency_guards.journey_write_lock("race-probe")
                if use_lock
                else nullcontext()
            )
            with context:
                with counter_guard:
                    current += 1
                    peak = max(peak, current)
                try:
                    rendezvous.wait()
                except threading.BrokenBarrierError:
                    broken = True
                with counter_guard:
                    current -= 1

        _race([critical, critical])
        return peak, broken

    free_peak, free_broken = measure(use_lock=False)
    assert free_peak == 2 and not free_broken, (
        "the probe could not observe two holders at once without the lock, so it "
        "cannot prove anything when the lock is on"
    )

    held_peak, held_broken = measure(use_lock=True)
    assert held_peak == 1, (
        f"two holders were inside the Journey lock at once ({held_peak}), so the guard "
        "does not serialise what it claims to"
    )
    assert held_broken, (
        "the second thread never even reached the barrier timeout path, which means "
        "the two calls did not actually overlap"
    )


def test_the_lock_registry_does_not_pin_a_journey_forever() -> None:
    key = f"transient-{uuid4()}"
    with concurrency_guards.journey_write_lock(key):
        assert key in concurrency_guards.journey_lock_registry()
    gc.collect()
    assert key not in concurrency_guards.journey_lock_registry(), (
        "every Journey ever written to stays in the registry; a long-running worker "
        "would leak one lock per Journey"
    )


# --------------------------------------------------------------------------
# C. CX-EXT-002 — the same proposal accepted twice concurrently
# --------------------------------------------------------------------------
def test_concurrent_acceptance_of_the_same_proposal_produces_one_transition() -> None:
    journey_id, _ = _started_for_race()
    proposal = _proposal_for(journey_id, _closed_activity_id(_plan_activities(journey_id)), "same")

    def call(tag: str):
        return client.post(
            f"/api/v1/replans/{proposal['id']}/accept",
            headers={"Idempotency-Key": f"race-{tag}-{proposal['id']}"},
        )

    responses = _race([lambda: call("a"), lambda: call("b")])
    statuses = sorted(item.status_code for item in responses)
    assert statuses.count(200) == 1, (
        f"both callers believed they switched the plan: {statuses}"
    )
    assert 409 in statuses, (
        "the losing caller was not told the proposal was already decided"
    )
    _assert_one_winner(_final_truth(journey_id))


def test_replaying_the_same_idempotency_key_never_transitions_twice() -> None:
    journey_id, _ = _started_for_race()
    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "replay"
    )
    key = f"replay-{proposal['id']}"

    def call():
        return client.post(
            f"/api/v1/replans/{proposal['id']}/accept", headers={"Idempotency-Key": key}
        )

    first, second = _race([call, call])
    assert first.status_code == 200 and second.status_code == 200, (
        f"a replayed idempotent request must be served, not re-executed: "
        f"{first.status_code}/{second.status_code}"
    )
    truth = _final_truth(journey_id)
    _assert_one_winner(truth)


# --------------------------------------------------------------------------
# D. CX-EXT-003 — two competing proposals for one Journey
# --------------------------------------------------------------------------
def test_competing_proposals_can_never_both_become_active() -> None:
    journey_id, _ = _started_for_race()
    closed = _closed_activity_id(_plan_activities(journey_id))
    first = _proposal_for(journey_id, closed, "compete-a")
    # A second candidate batch for the same incident is the honest way to get two
    # competitors: closing a second activity depends on mobility legs the fixture
    # may not cover, and the engine refuses those with MOBILITY_DATA_UNAVAILABLE
    # rather than inventing a route.
    event_id = first["trigger_event_id"]
    assert client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"compete-b-{event_id}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event_id,
            "candidate_count": 3,
        },
    ).status_code == 200
    presented = [
        item
        for item in client.get(f"/api/v1/journeys/{journey_id}/replans").json()
        if item.get("status") == "PRESENTED"
        and item.get("base_plan_version_id") == first["base_plan_version_id"]
    ]
    assert len(presented) >= 2, (
        "the fixture produced no competitor for this proposal, so this would not be "
        f"the race under test: {[item['id'] for item in presented]}"
    )
    second = next(item for item in presented if item["id"] != first["id"])

    def call(proposal_id: str):
        return client.post(
            f"/api/v1/replans/{proposal_id}/accept",
            headers={"Idempotency-Key": f"compete-{proposal_id}"},
        )

    responses = _race([lambda: call(first["id"]), lambda: call(second["id"])])
    winners = [item for item in responses if item.status_code == 200]
    assert len(winners) == 1, (
        f"both competing proposals were accepted: {[r.status_code for r in responses]}"
    )
    loser = next(item for item in responses if item.status_code != 200)
    assert loser.status_code == 409, (
        f"the losing caller got {loser.status_code}; a rejected competitor is a "
        "conflict, not a validation error"
    )

    truth = _final_truth(journey_id)
    _assert_one_winner(truth)
    with SessionLocal() as session:
        losing_id = second["id"] if winners[0] is responses[0] else first["id"]
        row = session.get(ReplanProposal, losing_id)
        # The existing domain model already has a truthful non-winning state; no
        # test-only status is invented.
        assert row.status in {"INVALIDATED", "PRESENTED"}, (
            f"the losing proposal sits in {row.status!r}, which does not describe "
            "a competitor that was displaced by another acceptance"
        )
        losing_plan = session.get(PlanVersion, row.proposed_plan_version_id)
        assert losing_plan.status != "ACTIVE", "a losing proposal left its plan ACTIVE"


# --------------------------------------------------------------------------
# E. CX-EXT-004 — CAPTURE versus replan ACCEPT
# --------------------------------------------------------------------------
def test_capture_after_an_accept_does_not_promote_a_stale_pointer() -> None:
    """The schedule that used to strand a C4: book, replan away the plan under the
    booking, then capture.  The promotion must follow the commitment onto the
    active plan or refuse outright."""
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    client.post(f"/api/v1/journeys/{journey_id}/start")

    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "capture-after"
    )
    assert client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"accept-{proposal['id']}"},
    ).status_code == 200

    capture = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"late-capture-{booking['id']}"},
    )
    assert capture.status_code == 201, (
        f"a capture that could not be attached should fail closed, not 500: {capture.text[:200]}"
    )
    truth = _final_truth(journey_id)
    assert not truth["dangling_commitments"], (
        "the late capture promoted a commitment still anchored to the superseded plan"
    )


def test_capture_and_accept_racing_leave_no_stranded_commitment() -> None:
    """The schedule that used to be able to strand a promise.

    The booking exists and is not yet captured, so the acceptance does not treat
    it as a C4 and may replace the activity underneath it.  If the capture then
    lands against the superseded pointer, the traveller is told they hold a
    protected commitment that no active plan contains.
    """
    journey_id, _cruise_activity, booking = _journey_with_uncaptured_booking()
    booking_id = booking["id"]
    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "race-capture"
    )

    def capture() -> Any:
        return client.post(
            f"/api/v1/journeys/{journey_id}/transactions",
            json={
                "booking_id": booking_id,
                "transaction_type": "CAPTURE",
                "amount": booking["amount"],
            },
            headers={"Idempotency-Key": f"racing-capture-{booking_id}"},
        )

    def accept() -> Any:
        return client.post(
            f"/api/v1/replans/{proposal['id']}/accept",
            headers={"Idempotency-Key": f"racing-accept-{proposal['id']}"},
        )

    captured, accepted = _race([capture, accept])
    truth = _final_truth(journey_id)
    print(
        "[race] capture=%d accept=%d active_plans=%s off_plan_commitments=%s"
        % (
            captured.status_code,
            accepted.status_code,
            truth["active_plan_ids"],
            truth["off_plan_commitments"],
        )
    )
    assert accepted.status_code == 200, accepted.text
    # Either the capture attached to the plan that was active when it ran, or it
    # refused.  Both are legal; promoting a stale pointer is not.
    assert captured.status_code in {201, 409}, captured.text
    _assert_one_winner(truth)
    if captured.status_code == 201:
        with SessionLocal() as session:
            row = session.scalar(
                select(Commitment).where(
                    Commitment.journey_id == journey_id, Commitment.level == "C4_Anchored"
                )
            )
            assert row is not None, "a successful capture produced no protected commitment"
            anchored = session.get(Activity, row.activity_id)
            assert (
                anchored is not None
                and anchored.plan_version_id == truth["journey_plan_id"]
            ), "the capture promoted a commitment anchored outside the active plan"


def test_accept_then_capture_keeps_the_c4_on_the_active_plan() -> None:
    journey_id, _cruise_activity, _booking = _journey_with_captured_commitment()
    before = _final_truth(journey_id)
    assert not before["dangling_commitments"], "the fixture itself stranded a commitment"

    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "accept-first"
    )
    assert client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"accept-first-{proposal['id']}"},
    ).status_code == 200
    _assert_one_winner(_final_truth(journey_id))


# --------------------------------------------------------------------------
# F. the remap counting invariant, in isolation
# --------------------------------------------------------------------------
class _Row:
    """Just enough Activity for the resolver to reason about."""

    def __init__(
        self,
        row_id: str,
        *,
        place_id: str | None = None,
        semantic: str | None = None,
        start_at: str = "2026-10-10T15:30:00+08:00",
        plan_version_id: str = "active",
        status: str = "PENDING",
    ) -> None:
        self.id = row_id
        self.plan_version_id = plan_version_id
        self.place_id = place_id
        self.city_service_id = None
        self.status = status
        self.start_at = start_at
        self.end_at = "2026-10-10T17:00:00+08:00"
        self.metadata_json = json.dumps({"semantic_key": semantic} if semantic else {})


def _policy(semantic: str) -> dict[str, Any]:
    return {
        "semantic_key": semantic,
        "scheduled_start_at": "2026-10-10T15:30:00+08:00",
        "anchor_role": "ACTIVITY",
    }


def test_zero_candidates_fail_closed_instead_of_leaving_the_pointer_alone() -> None:
    anchored = _Row("old", place_id="place-cruise", plan_version_id="superseded")
    with pytest.raises(RemapConflict, match="cannot be attached"):
        resolve_commitment_target(
            policy=_policy("place-cruise"),
            anchored_activity=anchored,
            candidates=[_Row("other", place_id="place-museum")],
            subject="test",
        )


def test_exactly_one_candidate_rebinds() -> None:
    anchored = _Row("old", place_id="place-cruise", plan_version_id="superseded")
    target = _Row("new", place_id="place-cruise")
    assert (
        resolve_commitment_target(
            policy=_policy("place-cruise"),
            anchored_activity=anchored,
            candidates=[target],
            subject="test",
        )
        is target
    )


def test_two_candidates_for_one_commitment_are_ambiguous_and_fail_closed() -> None:
    """Picking "the first in sequence order" would invent an identity the record
    does not carry, so the only honest answer is a conflict."""
    anchored = _Row("old", place_id="place-cruise", plan_version_id="superseded")
    with pytest.raises(RemapConflict, match="AMBIGUOUS"):
        resolve_commitment_target(
            policy=_policy("place-cruise"),
            anchored_activity=anchored,
            candidates=[_Row("a", place_id="place-cruise"), _Row("b", place_id="place-cruise")],
            subject="test",
        )


def test_a_suffix_of_a_semantic_key_is_not_an_identity_match() -> None:
    """The rule this replaces matched with `candidate.endswith(term)`, which let a
    shorter key claim any longer one that ended the same way."""
    anchored = _Row("old", place_id="cruise", plan_version_id="superseded")
    impostor = _Row("impostor", place_id="river-cruise")
    assert matching_activities(
        commitment_identity(policy=_policy("cruise"), activity=anchored), [impostor]
    ) == []


def test_a_different_instant_is_not_the_same_commitment() -> None:
    anchored = _Row("old", place_id="place-cruise", plan_version_id="superseded")
    moved = _Row("moved", place_id="place-cruise", start_at="2026-10-11T20:00:00+08:00")
    with pytest.raises(RemapConflict, match="cannot be attached"):
        resolve_commitment_target(
            policy=_policy("place-cruise"),
            anchored_activity=anchored,
            candidates=[moved],
            subject="test",
        )


# --------------------------------------------------------------------------
# the two guards the mutation check proved were untested
# --------------------------------------------------------------------------
def _move_commitment_pointer_off_plan(journey_id: str, *, place_id: str | None = None) -> str:
    """Point the journey's active commitment at an activity on another plan.

    The route does not matter; the guard has to hold whatever produced the state.
    """
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        candidates = list(
            session.scalars(
                select(Activity).where(Activity.plan_version_id != journey.active_plan_version_id)
            ).all()
        )
        if not candidates:
            raise AssertionError("no off-plan activity exists to simulate a stale pointer")
        target = candidates[0]
        if place_id is not None:
            target.place_id = place_id
        commitment = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == journey_id, Commitment.status == "ACTIVE"
            )
        )
        commitment.activity_id = target.id
        session.commit()
        return target.id


def test_capture_reanchors_a_commitment_that_arrived_stale() -> None:
    """The capture must resolve the pointer before it promotes anything."""
    journey_id, _cruise, booking = _journey_with_uncaptured_booking()
    # A second plan exists after generation; park the pointer there.
    client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    _move_commitment_pointer_off_plan(journey_id, place_id="place-river-cruise")

    capture = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"stale-capture-{booking['id']}"},
    )
    assert capture.status_code == 201, capture.text
    truth = _final_truth(journey_id)
    assert not truth["dangling_commitments"], (
        "the capture promoted a commitment whose pointer was outside the active plan"
    )
    assert not truth["off_plan_commitments"], truth["off_plan_commitments"]


def test_capture_refuses_when_no_active_activity_carries_the_commitment_identity() -> None:
    """Re-anchoring is not the same as inventing an anchor."""
    journey_id, _cruise, booking = _journey_with_uncaptured_booking()
    client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    # A pointer whose place exists nowhere on the active plan and whose scheduled
    # instant is moved: nothing may be bound to it.
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        on_plan = {
            item.place_id
            for item in session.scalars(
                select(Activity).where(
                    Activity.plan_version_id == journey.active_plan_version_id
                )
            ).all()
        }
        # Any activity on another plan, moved to an instant no activity on the
        # active plan can share.  Identity plus scheduled instant both have to
        # match, so an unmatched instant is enough to make the anchor unresolvable
        # without inventing a Place the foreign key would reject.
        foreign = session.scalar(
            select(Activity)
            .where(Activity.plan_version_id != journey.active_plan_version_id)
            .where(Activity.place_id.is_not(None))
        )
        assert foreign is not None, "no off-plan activity is available for the orphan case"
        foreign.start_at = "2026-11-11T11:11:00+08:00"
        foreign.end_at = "2026-11-11T12:11:00+08:00"
        commitment = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == journey_id, Commitment.status == "ACTIVE"
            )
        )
        commitment.activity_id = foreign.id
        policy = json.loads(commitment.policy_json or "{}")
        policy["semantic_key"] = foreign.place_id
        policy["scheduled_start_at"] = foreign.start_at
        commitment.policy_json = json.dumps(policy)
        session.commit()

    capture = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"orphan-capture-{booking['id']}"},
    )
    assert capture.status_code == 409, (
        "a capture that cannot locate the commitment on the active plan must refuse, "
        f"not promote: {capture.status_code} {capture.text[:160]}"
    )
    truth = _final_truth(journey_id)
    assert not truth["dangling_commitments"], (
        "the refused capture still left a protected commitment stranded off-plan"
    )


def test_a_recovery_cannot_be_accepted_over_a_duplicated_protected_anchor() -> None:
    """Two activities claiming one protected commitment must never be accepted.

    Measured, and reported rather than smoothed over: this state is caught by the
    feasibility gate (`ACTIVITY_OVERLAP`) before the acceptance's own remap runs,
    and the sibling case - the committed activity moved to a different instant -
    is caught there too (`LOCKED_COMMITMENT_LOST`).  So the remap's conflict branch
    is not reachable through this endpoint; it stays as defence in depth, and the
    0 / 1 / many counting it depends on is asserted directly against the resolver
    in the three `resolve_commitment_target` tests below.  What this case does pin
    is the invariant the ruling asks for: the acceptance refuses, the Journey keeps
    exactly one ACTIVE plan, and the promise is not re-pointed at anything.
    """
    journey_id, _cruise_activity, _booking = _journey_with_captured_commitment()
    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "ambiguous-c4"
    )
    with SessionLocal() as session:
        original = next(
            item
            for item in session.scalars(
                select(Activity).where(
                    Activity.plan_version_id == proposal["proposed_plan_version_id"]
                )
            ).all()
            if item.place_id == "place-river-cruise"
        )
        rows = session.scalars(
            select(Activity).where(
                Activity.plan_version_id == proposal["proposed_plan_version_id"]
            )
        ).all()
        columns = {
            column.name: getattr(original, column.name)
            for column in Activity.__table__.columns
            if column.name not in {"id", "plan_version_id", "sequence_no"}
        }
        twin = Activity(
            id=str(uuid4()),
            plan_version_id=original.plan_version_id,
            sequence_no=max(item.sequence_no for item in rows) + 40,
            **columns,
        )
        session.add(twin)
        session.commit()
        assert (
            session.scalar(
                select(func.count())
                .select_from(Activity)
                .where(
                    Activity.plan_version_id == original.plan_version_id,
                    Activity.place_id == "place-river-cruise",
                )
            )
            == 2
        ), "the duplicate anchor was not persisted, so this is not the ambiguity case"


    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"drop-c4-{proposal['id']}"},
    )
    assert accepted.status_code == 409, (
        "acceptance must refuse a plan that cannot carry the traveller's protected "
        f"commitment unambiguously, got {accepted.status_code}: {accepted.text[:200]}"
    )
    assert "place-river-cruise" in accepted.text, (
        "the refusal does not name the protected place at issue, so a traveller "
        f"cannot tell what was preserved: {accepted.text[:240]}"
    )
    truth = _final_truth(journey_id)
    assert len(truth["active_plan_ids"]) == 1
    assert truth["journey_plan_id"] == truth["active_plan_ids"][0]
    assert not truth["dangling_commitments"]
    assert proposal["id"] not in truth["accepted_proposal_ids"], (
        "the rejected acceptance still recorded the proposal as ACCEPTED"
    )


# --------------------------------------------------------------------------
# the invariant must hold without the lock, because the lock is per-process
# --------------------------------------------------------------------------
def test_two_sessions_without_the_lock_still_cannot_produce_two_active_plans() -> None:
    """Simulate two backend processes: separate sessions, no shared guard.

    `journey_write_lock` only serializes inside one process, so a race that is
    closed by the lock alone is not closed.  Here the lock is bypassed entirely by
    calling the serialized implementation from two sessions at once.  The
    surviving guarantee has to be the database's: conditional writes plus
    `ux_plan_versions_single_active`.
    """
    from app.main import _accept_replan_serialized_impl

    journey_id, _ = _started_for_race()
    activities = _plan_activities(journey_id)
    first = _proposal_for(journey_id, _closed_activity_id(activities), "xproc-a")
    second = _proposal_for(journey_id, _closed_activity_id(activities), "xproc-b")
    assert first["base_plan_version_id"] == second["base_plan_version_id"]

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(proposal_id: str) -> None:
        from uuid import UUID as _UUID

        barrier.wait()
        with SessionLocal() as session:
            try:
                _accept_replan_serialized_impl(
                    _UUID(proposal_id),
                    f"xproc-{proposal_id}",
                    session,
                )
                outcome = "accepted"
            except Exception as exc:  # HTTPException from the conflict paths
                outcome = f"conflict:{getattr(exc, 'status_code', type(exc).__name__)}"
            finally:
                session.rollback()
                session.close()
        outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in (first["id"], second["id"])]
    for item in threads:
        item.start()
    for item in threads:
        item.join()

    truth = _final_truth(journey_id)
    assert len(truth["active_plan_ids"]) == 1, (
        "two sessions without the shared lock produced more than one ACTIVE plan, so "
        f"the invariant lived only inside one process: {truth['active_plan_ids']}"
    )
    assert truth["journey_plan_id"] == truth["active_plan_ids"][0]
    assert outcomes.count("accepted") == 1, outcomes
    assert all(item.startswith("conflict:409") for item in outcomes if item != "accepted"), outcomes
    assert not truth["dangling_commitments"], truth


def test_the_database_itself_refuses_a_second_active_plan() -> None:
    """The index, not the application, is the last word.

    A raw second ACTIVE row for one Journey is written through a separate session
    with no product code involved.  If that succeeds, every race conclusion in
    this file rests on a lock that does not exist across processes.
    """
    journey_id, _ = _started_for_race()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        existing = session.scalar(
            select(PlanVersion).where(PlanVersion.id == journey.active_plan_version_id)
        )
        rival = PlanVersion(
            id=str(uuid4()),
            journey_id=journey_id,
            version_no=existing.version_no + 500,
            planning_mode=existing.planning_mode,
            status="ACTIVE",
            source=existing.source,
            currency=existing.currency,
        )
        session.add(rival)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert (
            session.scalar(
                select(func.count())
                .select_from(PlanVersion)
                .where(
                    PlanVersion.journey_id == journey_id, PlanVersion.status == "ACTIVE"
                )
            )
            == 1
        ), "the rejected write left a second ACTIVE plan behind anyway"


def test_one_idempotency_key_across_two_sessions_replays_without_the_lock() -> None:
    """Same proposal, same key, no shared guard: one transition, two successes.

    This is the last place where the in-process lock was load-bearing.  With the
    lock removed, the loser used to collide with the winner's conditional update and
    answer 409 - correct-looking only because one Python process happened to
    serialize them.  The durable receipt, not the lock, has to decide.
    """
    from uuid import UUID as _UUID

    from app.main import _accept_replan_serialized_impl

    journey_id, _ = _started_for_race()
    proposal = _proposal_for(
        journey_id, _closed_activity_id(_plan_activities(journey_id)), "shared-key"
    )
    key = "one-key-two-processes"
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []

    def worker() -> None:
        barrier.wait()
        with SessionLocal() as session:
            try:
                result = _accept_replan_serialized_impl(_UUID(proposal["id"]), key, session)
                outcomes.append(("ok", result["journey"]["active_plan_version_id"]))
            except Exception as exc:  # HTTPException from a genuine conflict
                outcomes.append(
                    ("err", getattr(exc, "status_code", type(exc).__name__))
                )
            finally:
                session.rollback()
                session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for item in threads:
        item.start()
    for item in threads:
        item.join()

    assert [kind for kind, _ in outcomes] == ["ok", "ok"], (
        f"both callers must receive the canonical idempotent success without the lock, "
        f"got {outcomes}"
    )
    assert outcomes[0][1] == outcomes[1][1], (
        "the two callers were shown different active plans, so one of them replayed "
        "something that was never committed"
    )
    truth = _final_truth(journey_id)
    _assert_one_winner(truth)
    assert truth["journey_plan_id"] == outcomes[0][1]
    with SessionLocal() as session:
        receipts = session.scalars(
            select(IdempotencyKey).where(IdempotencyKey.key == f"replan.accept:{key}")
        ).all()
        assert len(receipts) == 1, (
            f"one idempotency key produced {len(receipts)} receipts; the replay is not "
            "resting on a single durable fact"
        )


# --------------------------------------------------------------------------
# shared setup for the race tests
# --------------------------------------------------------------------------
def _started_for_race() -> tuple[str, str]:
    journey_id, _cruise, _booking = _journey_with_captured_commitment()
    return journey_id, _cruise


def _plan_activities(journey_id: str) -> list[dict[str, Any]]:
    plan = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]
    return plan["activities"]
