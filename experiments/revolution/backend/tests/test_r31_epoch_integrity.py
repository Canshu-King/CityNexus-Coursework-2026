from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.collective_authority import (
    AUTHENTICATED_MEMBER_CONSENT,
    UNANIMOUS_DOMINANCE,
    authority_receipt_for_allowed_gate,
    candidate_surface_fingerprint,
    collective_authority_provenance,
    receipt_validation,
    server_semantic_decision_epoch,
)


class _ReceiptSession:
    def __init__(self) -> None:
        self.added = []

    def get(self, model, key):
        names = {
            "Journey": SimpleNamespace(id="journey-1"),
            "ReplanProposal": SimpleNamespace(
                id="proposal-1",
                journey_id="journey-1",
                proposed_plan_version_id="plan-1",
                status="PRESENTED",
            ),
            "PlanVersion": SimpleNamespace(id="plan-1", journey_id="journey-1"),
        }
        return names.get(getattr(model, "__name__", "")) if key in {
            "journey-1",
            "proposal-1",
            "plan-1",
        } else None

    def scalar(self, _statement):
        return next(iter(self.added), None)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None


def _gate(**overrides):
    value = {
        "allowed": True,
        "classification": UNANIMOUS_DOMINANCE,
        "candidate_fit_matrix": [{"candidate_id": "proposal-1"}],
        "current_decision_epoch": "e-1",
        "current_material_world_epoch": "w-1",
        "candidate_surface_fingerprint": "s-1",
    }
    value.update(overrides)
    return value


def test_r31_unanimous_receipt_never_aliases_missing_epoch_to_surface():
    session = _ReceiptSession()
    with pytest.raises(ValueError, match="COLLECTIVE_AUTHORITY_EPOCH_UNAVAILABLE"):
        authority_receipt_for_allowed_gate(
            session,
            journey_id="journey-1",
            proposal_id="proposal-1",
            plan_version_id="plan-1",
            gate=_gate(current_material_world_epoch=None),
        )
    assert session.added == []


def test_r31_server_semantic_epoch_and_surface_are_distinct_projections():
    matrix = [{"candidate_id": "proposal-1", "member_fits": {"m-1": {"effective_fit": 1.0}}}]
    surface = candidate_surface_fingerprint(matrix)
    epoch = server_semantic_decision_epoch(
        journey_id="journey-1",
        active_plan_version_id="plan-active",
        target_plan_version_id="plan-1",
        classification=UNANIMOUS_DOMINANCE,
        candidate_id="proposal-1",
        candidate_fit_matrix=matrix,
        members=[{"member_id": "m-1", "role": "OWNER"}],
    )
    assert surface
    assert epoch
    assert epoch != surface


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("material_world_epoch", "w-2", "COLLECTIVE_AUTHORITY_RECEIPT_STALE_WORLD_EPOCH"),
        ("decision_epoch", "e-2", "COLLECTIVE_AUTHORITY_RECEIPT_STALE_DECISION_EPOCH"),
    ],
)
def test_r31_same_surface_with_changed_epoch_unspends_old_receipt(field, value, code):
    receipt = SimpleNamespace(
        journey_id="journey-1",
        proposal_id="proposal-1",
        plan_version_id="plan-1",
        authority_mode=UNANIMOUS_DOMINANCE,
        decision_epoch="e-1",
        material_world_epoch="w-1",
        candidate_surface_fingerprint="s-1",
        invalidated_at=None,
        invalidation_code=None,
        id="receipt-1",
    )
    check = receipt_validation(
        receipt,
        journey_id="journey-1",
        proposal_id="proposal-1",
        plan_version_id="plan-1",
        decision_epoch="e-1",
        material_world_epoch="w-1",
        candidate_surface_fingerprint="s-1",
        authority_mode=UNANIMOUS_DOMINANCE,
    )
    assert check["valid"] is True
    current = {
        "decision_epoch": "e-1",
        "material_world_epoch": "w-1",
        "candidate_surface_fingerprint": "s-1",
    }
    current[field] = value
    stale = receipt_validation(
        receipt,
        journey_id="journey-1",
        proposal_id="proposal-1",
        plan_version_id="plan-1",
        decision_epoch=current["decision_epoch"],
        material_world_epoch=current["material_world_epoch"],
        candidate_surface_fingerprint=current["candidate_surface_fingerprint"],
        authority_mode=UNANIMOUS_DOMINANCE,
    )
    assert stale == {"valid": False, "code": code}


def test_r31_missing_receipt_provenance_fails_before_approval_boundary(monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(
        main_module,
        "collective_mutation_gate",
        lambda *_args, **_kwargs: _gate(current_decision_epoch=None),
    )
    result = main_module.collective_authority_preflight(
        _ReceiptSession(),
        SimpleNamespace(id="journey-1"),
        SimpleNamespace(id="proposal-1"),
        SimpleNamespace(id="plan-1"),
    )
    assert result["allowed"] is False
    assert result["code"] == "COLLECTIVE_AUTHORITY_EPOCH_UNAVAILABLE"


class _HumanReceiptSession(_ReceiptSession):
    def __init__(self, receipt) -> None:
        super().__init__()
        self.receipt = receipt

    def get(self, model, key):
        if (
            getattr(model, "__name__", "") == "CollectiveAuthorityReceipt"
            and key == "receipt-human"
        ):
            return self.receipt
        return super().get(model, key)


def test_r31_renewed_human_receipt_validates_current_surface_not_its_own_snapshot():
    receipt = SimpleNamespace(
        id="receipt-human",
        journey_id="journey-1",
        proposal_id="proposal-1",
        plan_version_id="plan-1",
        authority_mode=AUTHENTICATED_MEMBER_CONSENT,
        decision_epoch="e-human-1",
        material_world_epoch="w-1",
        candidate_surface_fingerprint="s-old",
        invalidated_at=None,
        invalidation_code=None,
    )
    session = _HumanReceiptSession(receipt)
    gate = {
        "allowed": True,
        "classification": "RENEWED_COLLECTIVE_CONSENT",
        "authority_receipt_id": receipt.id,
        "resolved_decision_epoch": "e-human-1",
        "current_decision_epoch": "e-human-1",
        "resolved_material_world_epoch": "w-1",
        "current_material_world_epoch": "w-1",
        "candidate_surface_fingerprint": "s-new",
    }
    with pytest.raises(
        ValueError,
        match="COLLECTIVE_AUTHORITY_RECEIPT_STALE_SURFACE",
    ):
        authority_receipt_for_allowed_gate(
            session,
            journey_id="journey-1",
            proposal_id="proposal-1",
            plan_version_id="plan-1",
            gate=gate,
        )

    gate["candidate_surface_fingerprint"] = "s-old"
    assert (
        authority_receipt_for_allowed_gate(
            session,
            journey_id="journey-1",
            proposal_id="proposal-1",
            plan_version_id="plan-1",
            gate=gate,
        )
        is receipt
    )


def test_r32_authority_provenance_names_actual_decision_epoch_builder():
    human = collective_authority_provenance(
        AUTHENTICATED_MEMBER_CONSENT
    )
    algorithmic = collective_authority_provenance(
        UNANIMOUS_DOMINANCE
    )

    assert (
        human["decision_epoch"]["source"]
        == "AUTHENTICATED_MEMBER_COLLECTIVE_DECISION_EPOCH"
    )
    assert (
        algorithmic["decision_epoch"]["source"]
        == "ALGORITHMIC_UNANIMOUS_SEMANTIC_EPOCH"
    )
    assert human["decision_epoch"] != algorithmic["decision_epoch"]
