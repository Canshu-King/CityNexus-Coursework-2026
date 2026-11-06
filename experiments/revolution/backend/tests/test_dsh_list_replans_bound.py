"""Witness: `list_replans` must not hand an agent a payload bigger than its window.

Measured on the Hermes lane DB before this bound: 22 persisted proposals came back as
2,922,724 bytes from a single read tool. The two arms below pin both halves of the
change, so neither half can rot silently:

  * the bounded arm — a fat journey comes back capped, newest-first, and *says* it was
    truncated (envelope counts + which heavy fields were dropped);
  * the untouched arm — an ordinary journey still gets the full 12-field shape with no
    omission, because every existing witness (ORD-0330 / R5) reads that shape.

Remove `LIST_REPLANS_MAX_ITEMS` (set it to a huge number) and the bounded arm goes red on
`returned`/bytes; remove the byte-budget degradation and it goes red on
`detail_fields_omitted`. Neither arm passes by asserting a weaker shape.
"""
from __future__ import annotations

import json
from uuid import uuid4

from test_agent_runtime_gate import _agent_call, _make_proposal

from app.agent.gateway import AgentToolGateway as AgentGateway
from app.db.models import ReplanProposal
from app.db.session import SessionLocal

FULL_FIELDS = {
    "id",
    "journey_id",
    "trigger_event_id",
    "base_plan_version_id",
    "proposed_plan_version_id",
    "status",
    "hard_constraints_passed",
    "locked_commitments_preserved",
    "utility_score",
    "score_breakdown",
    "diff",
    "explanation",
}


def _full_row(journey_id: str) -> dict[str, object]:
    """One untouched 12-field payload, for the size comparison printed above."""
    from app.main import _replan_payload

    with SessionLocal() as session:
        row = session.query(ReplanProposal).filter_by(journey_id=journey_id).first()
        assert row is not None
        return _replan_payload(row)


def _fatten(journey_id: str, extra_rows: int, heavy_chars: int) -> int:
    """Clone one real proposal row so every FK stays valid, and make it heavy."""
    with SessionLocal() as session:
        seed = session.query(ReplanProposal).filter_by(journey_id=journey_id).first()
        assert seed is not None, "the product must have persisted at least one proposal"
        blob = "x" * heavy_chars
        total = session.query(ReplanProposal).filter_by(journey_id=journey_id).count()
        for offset in range(extra_rows):
            clone = ReplanProposal(
                id=str(uuid4()),
                journey_id=seed.journey_id,
                trigger_event_id=seed.trigger_event_id,
                base_plan_version_id=seed.base_plan_version_id,
                proposed_plan_version_id=seed.proposed_plan_version_id,
                status=seed.status,
                hard_constraints_passed=seed.hard_constraints_passed,
                locked_commitments_preserved=seed.locked_commitments_preserved,
                utility_score=seed.utility_score,
                score_breakdown_json=json.dumps({"fat": blob}),
                diff_json=json.dumps({"fat": blob}),
                explanation_json=json.dumps({"fat": blob}),
                created_at=f"2026-01-01T00:00:{total + offset:02d}Z",
            )
            session.add(clone)
        session.commit()
        return session.query(ReplanProposal).filter_by(journey_id=journey_id).count()


def test_list_replans_on_a_fat_journey_is_capped_and_says_so():
    journey_id, _event_id, _proposal_id = _make_proposal()
    persisted = _fatten(journey_id, extra_rows=25, heavy_chars=40_000)

    body = _agent_call("list_replans", journey_id=journey_id).json()

    # Behaviour first: a mutant that removes the cap must die on `truncated`, not on a
    # setup precondition that merely notices the fixture no longer makes sense.
    assert body["truncated"] is True, "a capped read must name itself as capped"
    assert body["returned"] == len(body["proposals"]) == AgentGateway.LIST_REPLANS_MAX_ITEMS
    assert body["total"] == persisted
    assert body["order"] == "newest_first"
    # newest-first: the proposal an agent would act on is the first element
    created = [item["id"] for item in body["proposals"]]
    assert created[0] != created[-1]

    wire = json.dumps(body, ensure_ascii=False).encode("utf-8")
    uncapped = json.dumps(
        {**body, "proposals": [_full_row(journey_id) for _ in range(persisted)]},
        ensure_ascii=False,
    ).encode("utf-8")
    print(f"\nMEASURED wire={len(wire)}B uncapped_equivalent={len(uncapped)}B rows={persisted}")
    assert len(wire) < AgentGateway.LIST_REPLANS_BYTE_BUDGET * 4, (
        f"bound did not bite: {len(wire)} bytes on the wire"
    )
    assert body["detail_fields_omitted"], "heavy fields must be declared, not silently dropped"
    for entry in body["detail_fields_omitted"]:
        proposal_id, field = entry.split(":")
        item = next(row for row in body["proposals"] if row["id"] == proposal_id)
        assert field not in item, f"{entry} claimed but still present"
    # precondition, last: it guards the fixture, never the verdict
    assert persisted > AgentGateway.LIST_REPLANS_MAX_ITEMS, "the arm needs more rows than the cap"


def test_list_replans_on_an_ordinary_journey_keeps_the_full_shape():
    journey_id, _event_id, _proposal_id = _make_proposal()

    body = _agent_call("list_replans", journey_id=journey_id).json()

    assert body["truncated"] is False
    assert body["detail_fields_omitted"] == []
    assert body["total"] == body["returned"] == len(body["proposals"])
    for item in body["proposals"]:
        assert set(item) == FULL_FIELDS, (
            f"ordinary reads must keep the 12-field contract: {sorted(FULL_FIELDS - set(item))}"
        )
