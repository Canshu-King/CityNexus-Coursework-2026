"""Repository-owned stateful truth fuzzing for the Journey domain.

This is the EXTENDED truth gate, not the normal one.  It is meant to run before an
integration freeze and before a release candidate - not every time a stylesheet
changes.  Select it with:

    pytest -q backend/tests/stateful

Why it exists here rather than as a borrowed script: the invariants below were each
broken at some point in this project's history by an operation sequence nobody had
thought to write a test for.  A stateful machine finds those combinations; a list of
hand-written scenarios finds only the ones already imagined.

No third-party property library is required.  A deterministic PRNG plus a printed
seed is enough to make a failure reproducible, and it keeps the extended gate
runnable in an environment that installs nothing new.

The invariants are the catastrophic ones, and they are asserted after *every*
operation, not only at the end of a run:

    at most one ACTIVE PlanVersion per Journey
    the Journey pointer names that ACTIVE plan
    at most one ACCEPTED proposal claiming a win
    zero active C4 commitments anchored outside the active plan
    a stale or unresolvable cursor never fabricates a projected step
    one idempotency key never produces two plan transitions
"""

from __future__ import annotations

import json
import os
import random
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    Activity,
    Commitment,
    Journey,
    PlanVersion,
    ReplanProposal,
)
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

# Reproducible, and printed so a failure can be replayed exactly.
BASE_SEED = int(os.getenv("CITYNEXUS_STATEFUL_SEED", "20260920"))
CASES = int(os.getenv("CITYNEXUS_STATEFUL_CASES", "12"))
STEPS = int(os.getenv("CITYNEXUS_STATEFUL_STEPS", "12"))


def _journey_state(journey_id: str) -> dict[str, Any]:
    """Read authoritative truth from the rows, never from a projection."""
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        active = list(
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
        anchored = []
        for commitment in session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey_id,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
                Commitment.activity_id.is_not(None),
            )
        ).all():
            activity = session.get(Activity, commitment.activity_id)
            if activity is None or activity.plan_version_id != journey.active_plan_version_id:
                anchored.append(commitment.id)
        cursor = journey.current_activity_id
        cursor_resolves = True
        if cursor is not None:
            row = session.get(Activity, cursor)
            cursor_resolves = row is not None and row.plan_version_id == journey.active_plan_version_id
        return {
            "active_plan_ids": sorted(item.id for item in active),
            "pointer": journey.active_plan_version_id,
            "accepted": sorted(item.id for item in accepted),
            "accepted_plans": sorted({item.proposed_plan_version_id for item in accepted}),
            "accepted_plan_statuses": {
                (
                    session.get(PlanVersion, item.proposed_plan_version_id).status
                    if session.get(PlanVersion, item.proposed_plan_version_id)
                    else "MISSING"
                )
                for item in accepted
            },
            "dangling_c4": sorted(anchored),
            "cursor": cursor,
            "cursor_resolves": cursor_resolves,
            "journey_status": journey.status,
        }


def _assert_invariants(journey_id: str, history: list[str]) -> None:
    state = _journey_state(journey_id)
    context = f"after {json.dumps(history[-6:], ensure_ascii=False)}"
    assert len(state["active_plan_ids"]) <= 1, (
        f"a Journey holds {len(state['active_plan_ids'])} ACTIVE plans {context}: "
        f"{state['active_plan_ids']}"
    )
    if state["pointer"] is not None:
        # While a Journey is travelling, the plan it names must be *the* ACTIVE plan.
        # Before that, selection leaves the plan in SELECTED and the pointer ahead of
        # it, which is legal: `/start` is what promotes it.
        if state["journey_status"] == "ACTIVE":
            assert state["active_plan_ids"] == [state["pointer"]], (
                f"a travelling Journey points at {state['pointer']} while the ACTIVE set "
                f"is {state['active_plan_ids']} {context}"
            )
        else:
            assert not state["active_plan_ids"] or state["active_plan_ids"] == [state["pointer"]], (
                f"a planned Journey carries {len(state['active_plan_ids'])} ACTIVE plans "
                f"{state['active_plan_ids']} {context}"
            )
    assert len(state["accepted"]) <= 1, (
        f"more than one proposal claims it won the same Journey {context}: {state['accepted']}"
    )
    if state["accepted"]:
        # "This proposal was accepted" is a fact about a moment, not a permanent
        # claim on the ACTIVE slot: a traveller may move to another plan afterwards,
        # which supersedes the winner.  What must never happen is an ACCEPTED
        # proposal whose plan is still CANDIDATE or was REJECTED - that would mean the
        # acceptance recorded a win it never delivered.
        assert state["accepted_plan_statuses"] <= {"ACTIVE", "SUPERSEDED"}, (
            f"an accepted proposal's plan is in {state['accepted_plan_statuses']} "
            f"{context}: acceptance claimed a win the plan state does not show"
        )
    assert not state["dangling_c4"], (
        f"protected commitments are anchored outside the active plan {context}: "
        f"{state['dangling_c4']}"
    )
    if state["cursor"] is not None and not state["cursor_resolves"]:
        # A stale cursor is legal state; projecting a step from it is not.
        projection = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]
        assert projection.get("cursor_status") == "STALE", (
            f"the projection calls an unresolvable cursor {projection.get('cursor_status')} {context}"
        )
        execution = client.get(f"/api/v1/journeys/{journey_id}/offline-snapshot").json()["snapshot"][
            "execution"
        ]
        assert execution["now_activity_id"] is None, (
            "a stale cursor fabricated a current step in the projection"
        )
        assert execution["next_activity_id"] is None, (
            "a stale cursor fabricated a next step in the projection"
        )


# ---------------------------------------------------------------------------
# operations.  Each returns a short label so a failure can name its own history.
# ---------------------------------------------------------------------------
def _op_generate_plan(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    candidate_count = rng.choice([1, 2, 3])
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": candidate_count}
    )
    assert response.status_code in {200, 201}, response.text
    history.append(f"generate({candidate_count})->{response.status_code}")


def _op_select_plan(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    plans = client.get(f"/api/v1/journeys/{journey_id}/plans").json()
    candidates = [item for item in plans if item.get("status") in {"CANDIDATE", "SELECTED"}]
    if not candidates:
        history.append("select(no-candidate)")
        return
    chosen = rng.choice(candidates)
    response = client.post(f"/api/v1/journeys/{journey_id}/plans/{chosen['id']}/select")
    assert response.status_code in {200, 409}, response.text
    history.append(f"select({chosen['id'][:8]})->{response.status_code}")


def _op_start(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    response = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert response.status_code in {200, 409, 422}, response.text
    history.append(f"start->{response.status_code}")


def _op_book_cruise(
    journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]
) -> None:
    plan = client.get(f"/api/v1/journeys/{journey_id}/experience").json().get("active_plan")
    if not plan:
        history.append("book(no-plan)")
        return
    cruise = next(
        (item for item in plan["activities"] if item.get("place_id") == "place-river-cruise"), None
    )
    if cruise is None:
        history.append("book(no-cruise)")
        return
    response = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert response.status_code in {201, 409, 422}, response.text
    if response.status_code == 201:
        # There is no "list my bookings" endpoint, so the machine remembers what it
        # created instead of inventing a read that the product does not offer.
        ledger.append(response.json())
    history.append(f"book->{response.status_code}")


def _op_capture(
    journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]
) -> None:
    if not ledger:
        history.append("capture(no-booking)")
        return
    booking = rng.choice(ledger)
    response = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking.get("amount") or {"amount_minor": 100, "currency": "CNY"},
        },
        headers={"Idempotency-Key": f"stateful-{booking['id']}-{rng.randrange(10**6)}"},
    )
    assert response.status_code in {201, 409, 422}, response.text
    history.append(f"capture->{response.status_code}")


def _op_closure_event(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    plan = client.get(f"/api/v1/journeys/{journey_id}/experience").json().get("active_plan")
    if not plan or not plan["activities"]:
        history.append("closure(no-plan)")
        return
    target = rng.choice(plan["activities"])
    response = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": target["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "stateful truth fuzz"},
        },
    )
    assert response.status_code in {201, 409, 422}, response.text
    history.append(f"closure->{response.status_code}")


def _op_request_replan(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    events = client.get(f"/api/v1/journeys/{journey_id}/events").json()
    actionable = [item for item in events if item.get("status") == "DETECTED"]
    if not actionable:
        history.append("request_replan(no-event)")
        return
    event = rng.choice(actionable)
    response = client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"stateful-{event['id']}-{rng.randrange(10**6)}",
            "agent_id": "citynexus-stateful-probe",
            "journey_id": journey_id,
            "trigger_event_id": event["id"],
            "candidate_count": rng.choice([1, 3]),
        },
    )
    assert response.status_code in {200, 409, 422}, response.text
    history.append(f"request_replan->{response.status_code}")


def _op_accept(
    journey_id: str,
    rng: random.Random,
    history: list[str],
    ledger: list[dict[str, Any]],
    *,
    shared_key: str,
) -> None:
    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    presented = [item for item in proposals if item.get("status") == "PRESENTED"]
    if not presented:
        history.append("accept(no-proposal)")
        return
    proposal = rng.choice(presented)
    # Half the time reuse one shared key across different proposals and callers,
    # which is where a replay contract that leans on a process-local lock shows up.
    key = shared_key if rng.random() < 0.5 else f"stateful-{proposal['id']}-{rng.randrange(10**6)}"
    response = client.post(
        f"/api/v1/replans/{proposal['id']}/accept", headers={"Idempotency-Key": key}
    )
    assert response.status_code in {200, 409, 422}, response.text
    history.append(f"accept({proposal['id'][:8]},{key[:18]})->{response.status_code}")


def _op_transition(journey_id: str, rng: random.Random, history: list[str], ledger: list[dict[str, Any]]) -> None:
    plan = client.get(f"/api/v1/journeys/{journey_id}/experience").json().get("active_plan")
    if not plan or not plan["activities"]:
        history.append("transition(no-plan)")
        return
    target = rng.choice(plan["activities"])
    action = rng.choice(["complete", "skip"])
    response = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{target['id']}/{action}"
    )
    assert response.status_code in {200, 409, 422}, response.text
    history.append(f"{action}->{response.status_code}")


@pytest.mark.parametrize("case", range(CASES))
def test_journey_truth_invariants_survive_random_operation_orders(case: int) -> None:
    seed = BASE_SEED + case
    rng = random.Random(seed)
    history: list[str] = [f"seed={seed}"]
    shared_key = f"stateful-shared-key-{seed}"

    try:
        journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
        history.append(f"reset({journey_id[:8]})")
        ledger: list[dict[str, Any]] = []
        operations = [
            _op_generate_plan,
            _op_select_plan,
            _op_start,
            _op_book_cruise,
            _op_capture,
            _op_closure_event,
            _op_request_replan,
            lambda *args: _op_accept(*args, shared_key=shared_key),
            _op_transition,
        ]
        for _ in range(STEPS):
            rng.choice(operations)(journey_id, rng, history, ledger)
            _assert_invariants(journey_id, history)
    except AssertionError as failure:  # pragma: no cover - the diagnostic is the test
        raise AssertionError(
            f"{failure}\n\njourney truth sequence (seed {seed}):\n  " + "\n  ".join(history)
        ) from failure
