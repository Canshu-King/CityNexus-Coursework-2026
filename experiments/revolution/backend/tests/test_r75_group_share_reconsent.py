"""R75 regression: replacing a stale/expired receipt respects the active uniqueness slot.

Uses an isolated real SQLite session and the actual partial-unique index;
never calls providers, starts a Journey, or fakes collective authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import MemberPlaceGroupShareConsent
from app.execution import member_place_group_share as sharing


def _row(*, row_id: str, expires_at: str, status: str = "ACTIVE") -> MemberPlaceGroupShareConsent:
    stamp = datetime(2026, 10, 9, tzinfo=timezone.utc).isoformat()
    return MemberPlaceGroupShareConsent(
        id=row_id,
        journey_id="journey",
        invitation_id="invitation",
        owner_member_id="owner",
        owner_traveler_id="traveler",
        owner_split_intent_id="split",
        owner_side_quest_id="quest",
        base_plan_version_id="base",
        base_cursor_activity_id=None,
        place_id="place",
        place_snapshot_json="{}",
        rendezvous_activity_id="rendezvous-activity",
        rendezvous_place_id="rendezvous-place",
        audience_snapshot_json="[]",
        audience_snapshot_hash="old-audience",
        plan_snapshot_hash="old-plan",
        source_snapshot_hash="old-source",
        status=status,
        consented_at=stamp,
        expires_at=expires_at,
        created_at=stamp,
        updated_at=stamp,
    )


@pytest.mark.parametrize("reason,expected", [
    ("expired", "EXPIRED"),
    ("changed_audience", "STALE"),
])
def test_r75_reconfirm_releases_active_slot_before_replacement(monkeypatch, reason, expected):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MemberPlaceGroupShareConsent.__table__.create(engine)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    expiry = now - timedelta(seconds=1) if reason == "expired" else now + timedelta(hours=1)
    with Session(engine) as session:
        session.add(_row(row_id="original", expires_at=expiry.isoformat()))
        session.commit()
        if reason == "changed_audience":
            # Trigger the actual stale-consent path without needing a full
            # in-force Journey fixture. This is the only domain seam stubbed.
            monkeypatch.setattr(
                sharing, "current_group_share",
                lambda *_args, **_kwargs: {"shared": False},
            )
        result = sharing.consent_row_for_snapshot(
            session=session,
            journey=SimpleNamespace(id="journey"),
            invitation=SimpleNamespace(id="invitation"),
            owner=SimpleNamespace(id="owner"),
            traveler_id="traveler",
            facts={
                "audience_snapshot_hash": "new-audience",
                "plan_snapshot_hash": "new-plan",
                "source_snapshot_hash": "new-source",
            },
            now=now,
        )
        assert result is None
        session.add(
            _row(
                row_id="replacement",
                expires_at=(now + timedelta(hours=1)).isoformat(),
            )
        )
        session.commit()
        states = list(
            session.execute(
                select(MemberPlaceGroupShareConsent.id, MemberPlaceGroupShareConsent.status)
            )
        )
        assert sorted(states) == [("original", expected), ("replacement", "ACTIVE")]
        assert session.scalar(
            select(func.count()).select_from(MemberPlaceGroupShareConsent).where(
                MemberPlaceGroupShareConsent.status == "ACTIVE",
            )
        ) == 1
    engine.dispose()


def test_r75_receipt_retirement_rolls_back_if_replacement_insert_fails():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MemberPlaceGroupShareConsent.__table__.create(engine)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(_row(
            row_id="original",
            expires_at=(now - timedelta(seconds=1)).isoformat(),
        ))
        session.commit()
        sharing.consent_row_for_snapshot(
            session=session,
            journey=SimpleNamespace(id="journey"),
            invitation=SimpleNamespace(id="invitation"),
            owner=SimpleNamespace(id="owner"),
            traveler_id="traveler",
            facts={},
            now=now,
        )
        session.add(_row(
            row_id="invalid",
            expires_at=(now + timedelta(hours=1)).isoformat(),
            status="NOT_A_STATE",
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        original = session.get(MemberPlaceGroupShareConsent, "original")
        assert original is not None and original.status == "ACTIVE"
    engine.dispose()
