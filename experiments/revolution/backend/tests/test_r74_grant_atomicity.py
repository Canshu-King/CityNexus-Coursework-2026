"""Narrow R74 regression: scoped grants must bind as an all-member cohort.

This checks the domain helper without creating a provider route, Agent run, or
synthetic authorization receipt. Codex should additionally run its persisted
signed two-/three-member integration test after pulling this commit.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.shared_stop_coplanning as scope


class _Session:
    def __init__(self, members: dict[str, SimpleNamespace]) -> None:
        self.members = members

    def get(self, _model, key: str):
        return self.members.get(str(key))


def _grant(member_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"grant-{member_id}",
        journey_id="journey",
        invitation_id="invitation",
        base_plan_version_id="base-plan",
        grantee_member_id=member_id,
        grantee_traveler_id=f"traveler-{member_id}",
        grantor_member_id="owner",
        grantor_traveler_id="traveler-owner",
        purpose=scope.GRANT_PURPOSE,
        status="ACCEPTED",
        accepted_at="2026-10-09T00:00:00+00:00",
        expires_at="2026-10-10T00:00:00+00:00",
        promotion_id=None,
        decision_epoch=None,
        updated_at="2026-10-09T00:00:00+00:00",
    )


def test_r74_grants_are_not_partially_bound_if_any_required_member_lacks_one(monkeypatch):
    members = {
        name: SimpleNamespace(id=name, journey_id="journey", traveler_id=f"traveler-{name}")
        for name in ("b", "c")
    }
    grants = {"b": _grant("b")}
    session = _Session(members)
    monkeypatch.setattr(scope, "member_is_in_force", lambda _member, now=None: True)
    monkeypatch.setattr(scope, "member_has_capability", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        scope,
        "find_accepted_grant",
        lambda _session, **kwargs: grants.get(kwargs["grantee"].id),
    )

    with pytest.raises(HTTPException) as failure:
        scope.decision_scope_facts(
            session,
            journey_id="journey",
            invitation_id="invitation",
            base_plan_version_id="base-plan",
            promotion_id="promotion",
            decision_epoch=None,
            required_member_ids=["b", "c"],
            bind_unbound=True,
        )
    assert failure.value.status_code == 409
    assert failure.value.detail == "SCOPED_COPLANNING_GRANT_REQUIRED_OR_STALE"
    assert grants["b"].promotion_id is None
    assert grants["b"].updated_at == "2026-10-09T00:00:00+00:00"

    # Both accepted members are now available: exactly this successful call
    # may bind them, and its decision epoch material must name the promotion.
    grants["c"] = _grant("c")
    facts, bound = scope.decision_scope_facts(
        session,
        journey_id="journey",
        invitation_id="invitation",
        base_plan_version_id="base-plan",
        promotion_id="promotion",
        decision_epoch=None,
        required_member_ids=["b", "c"],
        bind_unbound=True,
    )
    assert set(bound) == {"b", "c"}
    assert {row["promotion_id"] for row in facts} == {"promotion"}
    assert all(item.promotion_id == "promotion" for item in grants.values())
