import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agent.gateway import (
    _read_idempotent,
    _upgrade_cached_request_replan_response,
)
from app.db.models import IdempotencyKey, ReplanProposal


JOURNEY = "journey-a"
TRIGGER = "event-a"


def proposal(proposal_id="proposal-a", *, journey=JOURNEY, trigger=TRIGGER, status="PRESENTED"):
    return SimpleNamespace(
        id=proposal_id,
        journey_id=journey,
        trigger_event_id=trigger,
        base_plan_version_id="base-plan",
        proposed_plan_version_id="candidate-plan",
        status=status,
        hard_constraints_passed=True,
        locked_commitments_preserved=True,
        utility_score=0.5,
        score_breakdown_json="{}",
        diff_json="{}",
        explanation_json="{}",
    )


def legacy(row):
    return {
        "id": row.id,
        "status": "PRESENTED",
        "hard_constraints_passed": True,
        "locked_commitments_preserved": True,
    }


class Session:
    def __init__(self, rows=()):
        self.rows = {getattr(row, "id", getattr(row, "key", None)): row for row in rows}
        self.lookups = []

    def get(self, model, key):
        self.lookups.append((model, key))
        return self.rows.get(key)


def upgrade(session, stored):
    return _upgrade_cached_request_replan_response(
        session, stored, journey_id=JOURNEY, trigger_event_id=TRIGGER
    )


def assert_unavailable(session, stored):
    before = dict(session.rows)
    with pytest.raises(HTTPException) as error:
        upgrade(session, stored)
    assert error.value.status_code == 409
    assert error.value.detail == "AGENT_IDEMPOTENCY_RESPONSE_SCHEMA_UNAVAILABLE"
    assert session.rows == before


def test_current_cache_passthrough_without_lookup():
    from app.main import _replan_payload

    row = proposal()
    session = Session([row])
    stored = {"proposals": [_replan_payload(row)], "request_id": "original"}
    assert upgrade(session, stored) is stored
    assert session.lookups == []
    assert list(session.rows) == [row.id]


def test_legacy_cache_rehydrates_in_original_order_without_writes():
    rows = [proposal("proposal-b"), proposal("proposal-a")]
    session = Session(rows)
    stored = {"proposals": [legacy(row) for row in rows]}
    frozen = json.dumps(stored, sort_keys=True)
    result = upgrade(session, stored)
    assert [item["id"] for item in result["proposals"]] == [row.id for row in rows]
    assert all("diff" in item and "proposed_plan_version_id" in item for item in result["proposals"])
    assert json.dumps(stored, sort_keys=True) == frozen
    assert session.lookups == [(ReplanProposal, row.id) for row in rows]
    assert set(session.rows) == {row.id for row in rows}


def test_missing_proposal_fails_closed():
    assert_unavailable(Session(), {"proposals": [legacy(proposal())]})


@pytest.mark.parametrize("row", [
    proposal(journey="journey-b"),
    proposal(trigger="event-b"),
])
def test_cached_identity_outside_authority_scope_fails_closed(row):
    assert_unavailable(Session([row]), {"proposals": [legacy(row)]})


def test_historical_status_and_booleans_are_preserved():
    row = proposal(status="ACCEPTED")
    row.hard_constraints_passed = False
    row.locked_commitments_preserved = False
    result = upgrade(Session([row]), {"proposals": [legacy(row)]})
    item = result["proposals"][0]
    assert item["status"] == "PRESENTED"
    assert item["hard_constraints_passed"] is True
    assert item["locked_commitments_preserved"] is True
    assert item["proposed_plan_version_id"] == row.proposed_plan_version_id


@pytest.mark.parametrize("proposals", [None, {}, [], [None], [{"id": "x"}],
                                      [legacy(proposal()), legacy(proposal())]])
def test_malformed_cache_fails_closed(proposals):
    assert_unavailable(Session([proposal()]), {"proposals": proposals})


def test_existing_signature_conflict_remains_a_conflict():
    row = SimpleNamespace(request_hash="original", response_json='{"proposals":[]}', key="key")
    session = Session([row])
    with pytest.raises(HTTPException) as error:
        _read_idempotent(session, "key", "changed")
    assert error.value.status_code == 409
    assert error.value.detail == "same agent request id used for a different request"
    assert session.lookups == [(IdempotencyKey, "key")]
