from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select
from test_agent_runtime_gate import _agent_call, _make_proposal
from test_impact_resolver import FakeProvider, _candidate
from test_solver_runtime_gate import _generate, _prepare_disruption, _proposals, client

import app.intelligence.impact_resolver as impact_module
from app.agent.contracts import (
    AgentAction,
    AgentMode,
    AuthoritativeActionFacts,
    AuthorizationDecision,
    DelegationPolicy,
)
from app.agent.delegation_policy import evaluate_action
from app.db.models import ActionApproval, Event, ImpactAssessment
from app.db.session import SessionLocal
from app.intelligence.impact_contract import ImpactProviderResponse


def _ambiguous_ai_event(monkeypatch):
    journey_id, plan, _ = _prepare_disruption()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    created = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"text": "An ambiguous city notice reports a temporary disruption."},
        },
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert created.json()["affected_activity_id"] is None
    monkeypatch.setattr(
        impact_module,
        "_build_provider",
        lambda: FakeProvider(
            response=ImpactProviderResponse(candidates=[_candidate(target["id"])])
        ),
    )
    return journey_id, target, event_id


def test_aav2_01_ai_only_hypothesis_does_not_mutate_event_truth(monkeypatch):
    journey_id, _target, event_id = _ambiguous_ai_event(monkeypatch)
    _generate(journey_id, event_id, count=1)
    with SessionLocal() as session:
        row = session.get(Event, event_id)
        assert row is not None
        assert row.affected_activity_id is None


def test_aav2_02_ai_only_hypothesis_cannot_self_escalate_on_retry(monkeypatch):
    """An AI-only hypothesis must never escalate into validated truth.

    The AI provider *may* name a structurally eligible target, but the result
    stays ``AI_ONLY`` and a retry without the provider must not promote it to
    ``VALIDATED_IMPACT``.
    """

    journey_id, _target, event_id = _ambiguous_ai_event(monkeypatch)
    _generate(journey_id, event_id, count=1)
    first = _proposals(journey_id, event_id)[-1]["diff"]["verified_evidence"]["impact_resolution"]
    assert first["maturity_stage"] == "AI_ONLY"

    # With the AI provider gone the target is no longer grounded at all, so a
    # retry must be refused rather than silently promoted.  This is a stronger
    # guarantee than "does not escalate": an AI-only hypothesis can never
    # become actionable truth.
    monkeypatch.setattr(impact_module, "_build_provider", lambda: None)
    retry = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": 1},
        headers={"Idempotency-Key": f"aav2-retry-{uuid4()}"},
    )
    assert retry.status_code == 422, retry.text
    assert "REPLAN_TARGET_UNRESOLVED" in retry.text

    with SessionLocal() as session:
        row = session.get(Event, event_id)
        assert row is not None
        assert row.affected_activity_id is None, "an AI guess mutated event truth"



def test_aav2_03_agent_cannot_self_assert_user_confirmation():
    journey_id, _event_id, proposal_id = _make_proposal()
    response = _agent_call(
        "accept_replan",
        journey_id=journey_id,
        proposal_id=proposal_id,
        user_confirmed=True,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is False
    assert body["decision"] == "REQUIRE_CONFIRMATION"


def test_aav2_03b_server_approval_receipt_is_scoped_and_one_use():
    journey_id, _event_id, proposal_id = _make_proposal()
    pending = _agent_call(
        "accept_replan",
        journey_id=journey_id,
        proposal_id=proposal_id,
        request_id="approval-pending-" + str(uuid4()),
    )
    assert pending.status_code == 200, pending.text
    approval_id = pending.json()["approval_id"]
    assert pending.json()["status"] == "PENDING"

    approved = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    accepted = _agent_call(
        "accept_replan",
        journey_id=journey_id,
        proposal_id=proposal_id,
        approval_id=approval_id,
        request_id="approval-consume-" + str(uuid4()),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["executed"] is True
    assert "USER_APPROVAL_RECEIPT_CONSUMED" in accepted.json()["reason_codes"]

    with SessionLocal() as session:
        row = session.get(ActionApproval, approval_id)
        assert row is not None
        assert row.status == "CONSUMED"


def test_aav2_04_advisory_mode_never_auto_executes_commitment_management():
    policy = DelegationPolicy(
        mode=AgentMode.ADVISORY,
        allow_auto_commitment_management=True,
        protected_commitment_levels=(),
        max_auto_loss_minor=1000,
    )
    facts = AuthoritativeActionFacts(
        hard_constraints_passed=True,
        commitment_management_requested=True,
        loss_delta_minor=0,
    )
    result = evaluate_action(policy, AgentAction.MODIFY_C4, facts)
    assert result.decision != AuthorizationDecision.ALLOW_AUTO


def test_aav2_05_ai_only_candidate_not_persisted_as_validated_activity_id(monkeypatch):
    journey_id, target, event_id = _ambiguous_ai_event(monkeypatch)
    _generate(journey_id, event_id, count=1)
    with SessionLocal() as session:
        row = session.scalar(
            select(ImpactAssessment)
            .where(ImpactAssessment.event_id == event_id)
            .order_by(ImpactAssessment.created_at.desc())
        )
        assert row is not None
        assert row.maturity_stage == "AI_ONLY"
        assert target["id"] not in json.loads(row.validated_activity_ids_json or "[]")
