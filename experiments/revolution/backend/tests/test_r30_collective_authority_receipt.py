from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.collective_authority import (
    AUTHENTICATED_MEMBER_CONSENT,
    UNANIMOUS_DOMINANCE,
    authority_receipt_for_allowed_gate,
    issue_collective_authority_receipt,
    receipt_validation,
)
from app.db.models import (
    AuditLog,
    CollectiveAuthorityReceipt,
    Journey,
    PlanVersion,
    ReplanProposal,
)


def _receipt(**overrides):
    values = {
        "id": "receipt-1",
        "journey_id": "journey-1",
        "proposal_id": "proposal-1",
        "plan_version_id": "plan-1",
        "authority_mode": UNANIMOUS_DOMINANCE,
        "decision_epoch": "decision-1",
        "material_world_epoch": "world-1",
        "candidate_surface_fingerprint": "surface-1",
        "source_mission_id": None,
        "source_agent_run_id": None,
        "required_member_ids_json": "[]",
        "response_audit_ids_json": "[]",
        "principal_actor_ids_json": "[]",
        "classification": "UNANIMOUS_DOMINANCE",
        "issued_at": "2026-10-06T00:00:00+00:00",
        "invalidated_at": None,
        "invalidation_code": None,
    }
    values.update(overrides)
    return CollectiveAuthorityReceipt(**values)


def _validate(receipt, **overrides):
    values = {
        "journey_id": "journey-1",
        "proposal_id": "proposal-1",
        "plan_version_id": "plan-1",
        "decision_epoch": "decision-1",
        "material_world_epoch": "world-1",
        "candidate_surface_fingerprint": "surface-1",
        "authority_mode": UNANIMOUS_DOMINANCE,
    }
    values.update(overrides)
    return receipt_validation(receipt, **values)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("journey_id", "journey-other", "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_JOURNEY"),
        ("proposal_id", "proposal-other", "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_PROPOSAL"),
        ("plan_version_id", "plan-other", "COLLECTIVE_AUTHORITY_RECEIPT_WRONG_PLAN"),
        ("decision_epoch", "decision-new", "COLLECTIVE_AUTHORITY_RECEIPT_STALE_DECISION_EPOCH"),
        ("material_world_epoch", "world-new", "COLLECTIVE_AUTHORITY_RECEIPT_STALE_WORLD_EPOCH"),
        (
            "candidate_surface_fingerprint",
            "surface-new",
            "COLLECTIVE_AUTHORITY_RECEIPT_STALE_SURFACE",
        ),
    ],
)
def test_r30_authority_receipt_exact_scope_rejects_mismatch(field, value, code):
    check = _validate(_receipt(), **{field: value})
    assert check == {"valid": False, "code": code}


def test_r30_authority_receipt_accepts_only_exact_current_scope():
    check = _validate(_receipt())
    assert check["valid"] is True
    assert check["receipt_id"] == "receipt-1"
    assert check["authority_mode"] == UNANIMOUS_DOMINANCE


def test_r30_invalidated_authority_receipt_remains_history_but_is_unspendable():
    check = _validate(
        _receipt(
            invalidated_at="2026-10-06T00:10:00+00:00",
            invalidation_code="WORLD_CHANGED",
        )
    )
    assert check == {"valid": False, "code": "WORLD_CHANGED"}


def test_r30_authority_mode_cannot_be_substituted():
    check = _validate(
        _receipt(authority_mode=AUTHENTICATED_MEMBER_CONSENT),
        authority_mode=UNANIMOUS_DOMINANCE,
    )
    assert check == {
        "valid": False,
        "code": "COLLECTIVE_AUTHORITY_RECEIPT_MODE_MISMATCH",
    }


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _AuthoritySession:
    def __init__(self, audits):
        self.journey = SimpleNamespace(id="journey-1")
        self.proposal = SimpleNamespace(
            id="proposal-1",
            journey_id="journey-1",
            proposed_plan_version_id="plan-1",
            status="PRESENTED",
        )
        self.plan = SimpleNamespace(id="plan-1", journey_id="journey-1")
        self.audits = list(audits)
        self.added = []

    def get(self, model, key):
        if model is Journey and key == "journey-1":
            return self.journey
        if model is ReplanProposal and key == "proposal-1":
            return self.proposal
        if model is PlanVersion and key == "plan-1":
            return self.plan
        return None

    def scalars(self, _statement):
        return _ScalarRows(self.audits)

    def scalar(self, _statement):
        return next(
            (
                item
                for item in self.added
                if isinstance(item, CollectiveAuthorityReceipt)
                and item.invalidated_at is None
            ),
            None,
        )

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None


def _audit(
    *,
    audit_id: str,
    member_id: str,
    traveler_id: str,
    clarification_id: str = "clarification-current",
    mission_id: str = "mission-current",
):
    return SimpleNamespace(
        id=audit_id,
        action="agent.collective_decision.response",
        entity_type="JourneyMission",
        entity_id=mission_id,
        journey_id="journey-1",
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=traveler_id,
        after_json=json.dumps(
            {
                "clarification_id": clarification_id,
                "member_id": member_id,
                "traveler_id": traveler_id,
                "proposal_id": "proposal-1",
                "decision_epoch": "decision-1",
                "status": "RESOLVED",
            }
        ),
    )


def _issue(session, **overrides):
    values = {
        "journey_id": "journey-1",
        "proposal_id": "proposal-1",
        "plan_version_id": "plan-1",
        "authority_mode": AUTHENTICATED_MEMBER_CONSENT,
        "decision_epoch": "decision-1",
        "material_world_epoch": "world-1",
        "candidate_surface_fingerprint": "surface-1",
        "classification": "AUTHENTICATED_MEMBER_CONSENT",
        "required_member_ids": ["member-a", "member-b"],
        "response_audit_ids": ["audit-a", "audit-b"],
        "principal_actor_ids": ["traveler-a", "traveler-b"],
        "source_mission_id": "mission-current",
        "source_clarification_id": "clarification-current",
    }
    values.update(overrides)
    return issue_collective_authority_receipt(session, **values)


def test_r30_human_receipt_requires_same_clarification_and_exact_principals():
    session = _AuthoritySession(
        [
            _audit(
                audit_id="audit-a",
                member_id="member-a",
                traveler_id="traveler-a",
            ),
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    receipt = _issue(session)
    assert receipt.authority_mode == AUTHENTICATED_MEMBER_CONSENT
    assert json.loads(receipt.required_member_ids_json) == ["member-a", "member-b"]
    assert json.loads(receipt.principal_actor_ids_json) == [
        "traveler-a",
        "traveler-b",
    ]


def test_r30_human_receipt_rejects_historical_clarification_audit():
    session = _AuthoritySession(
        [
            _audit(
                audit_id="audit-a",
                member_id="member-a",
                traveler_id="traveler-a",
                clarification_id="clarification-old",
            ),
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    with pytest.raises(ValueError, match="another clarification"):
        _issue(session)


def test_r30_human_receipt_rejects_forged_principal_set():
    session = _AuthoritySession(
        [
            _audit(
                audit_id="audit-a",
                member_id="member-a",
                traveler_id="traveler-a",
            ),
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    with pytest.raises(ValueError, match="principal set"):
        _issue(
            session,
            principal_actor_ids=["traveler-a", "traveler-forged"],
        )


def test_r30_human_receipt_rejects_cross_mission_response_audit():
    session = _AuthoritySession(
        [
            _audit(
                audit_id="audit-a",
                member_id="member-a",
                traveler_id="traveler-a",
                mission_id="mission-old",
            ),
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    with pytest.raises(ValueError, match="invalid response audits"):
        _issue(session)



def test_r30_human_receipt_rejects_audit_for_other_proposal():
    wrong = _audit(
        audit_id="audit-a",
        member_id="member-a",
        traveler_id="traveler-a",
    )
    wrong.after_json = json.dumps(
        {
            **json.loads(wrong.after_json),
            "proposal_id": "proposal-other",
        }
    )
    session = _AuthoritySession(
        [
            wrong,
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    with pytest.raises(ValueError, match="selected another proposal"):
        _issue(session)


def test_r30_human_receipt_rejects_audit_from_stale_decision_epoch():
    wrong = _audit(
        audit_id="audit-a",
        member_id="member-a",
        traveler_id="traveler-a",
    )
    wrong.after_json = json.dumps(
        {
            **json.loads(wrong.after_json),
            "decision_epoch": "decision-old",
        }
    )
    session = _AuthoritySession(
        [
            wrong,
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    with pytest.raises(ValueError, match="decision epoch mismatch"):
        _issue(session)



def test_r30_phase_a_does_not_require_external_agent_choreography():
    import inspect

    import run_r30_live

    source = inspect.getsource(run_r30_live._run_full)
    assert "_run_first_agent" not in source
    assert "_first_mission_id" not in source
    assert "_reach_post_consent_interlock(setup=setup)" in source


def test_r30_verifier_does_not_require_pre_shock_external_agent_runs():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "r30_machine_verdict.py"
    ).read_text(encoding="utf-8")

    assert "real_external_round1_agent" not in source
    assert "real_external_mediator" not in source
    assert "pre_shock_human_authority_receipt" in source
    assert "real_external_post_shock_recovery_agent" in source



def test_r32_unanimous_receipt_inherits_proposal_agent_run_lineage():
    session = _AuthoritySession([])
    session.proposal.source_agent_run_id = "agent-run-r2"

    receipt = authority_receipt_for_allowed_gate(
        session,
        journey_id="journey-1",
        proposal_id="proposal-1",
        plan_version_id="plan-1",
        gate={
            "allowed": True,
            "classification": UNANIMOUS_DOMINANCE,
            "recovery_epoch_id": "recovery-r2",
            "current_decision_epoch": "decision-r2",
            "current_material_world_epoch": "world-r2",
            "candidate_surface_fingerprint": "surface-r2",
        },
    )

    assert receipt is not None
    assert receipt.authority_mode == UNANIMOUS_DOMINANCE
    assert receipt.recovery_epoch_id == "recovery-r2"
    assert receipt.source_agent_run_id == "agent-run-r2"


def test_r30_exact_receipt_issuance_is_idempotent():
    session = _AuthoritySession(
        [
            _audit(
                audit_id="audit-a",
                member_id="member-a",
                traveler_id="traveler-a",
            ),
            _audit(
                audit_id="audit-b",
                member_id="member-b",
                traveler_id="traveler-b",
            ),
        ]
    )
    first = _issue(session)
    second = _issue(session)

    assert second is first
    assert len(
        [
            item
            for item in session.added
            if isinstance(item, CollectiveAuthorityReceipt)
        ]
    ) == 1
