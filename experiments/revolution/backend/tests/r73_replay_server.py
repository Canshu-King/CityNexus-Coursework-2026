"""Loopback-only TEST_REPLAY server for the R73 signed UI acceptance path.

This module is deliberately outside ``app.main``. It is launched only by the
dedicated Playwright config, patches route acquisition with explicit frozen
fixtures, and exposes two authenticated setup/evidence endpoints that do not
exist in the product application.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

if os.environ.get("CITYNEXUS_R73_TEST_REPLAY") != "1":
    raise RuntimeError("R73 replay server requires CITYNEXUS_R73_TEST_REPLAY=1")

FIXTURE_TOKEN = os.environ.get("CITYNEXUS_R73_TEST_FIXTURE_TOKEN", "")
if len(FIXTURE_TOKEN) < 32:
    raise RuntimeError("R73 replay server requires a per-run fixture token")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import Header, HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db.models import (  # noqa: E402
    Activity,
    AgentRun,
    Commitment,
    Event as EventModel,
    Journey,
    JourneyMember,
    MemberPlaceInvitation,
    MemberPlacePromotion,
    MemberSideQuest,
    PlanVersion,
    Place,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal  # noqa: E402
from app.execution.clock import execution_clock  # noqa: E402
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureResult  # noqa: E402
from app.main import app  # noqa: E402
import app.main as main_module  # noqa: E402
from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, MobilitySnapshot  # noqa: E402


SOURCE_KEY = "R73-TEST-REPLAY-EXPLICIT-FIXTURE"


def _require_fixture_token(value: str | None) -> None:
    if not value or not hmac.compare_digest(value, FIXTURE_TOKEN):
        raise HTTPException(status_code=404, detail="fixture route unavailable")


def _fixture_opportunity_window(session, journey):
    invitation = session.scalar(
        select(MemberPlaceInvitation)
        .where(
            MemberPlaceInvitation.journey_id == str(journey.id),
            MemberPlaceInvitation.state == "OPEN",
        )
        .order_by(MemberPlaceInvitation.created_at.desc())
    )
    current = session.get(Activity, str(journey.current_activity_id or ""))
    if invitation is None or current is None:
        return None
    rendezvous = session.get(Activity, str(invitation.rendezvous_activity_id))
    if rendezvous is None:
        return None
    current_end = datetime.fromisoformat(current.end_at)
    rendezvous_start = datetime.fromisoformat(rendezvous.start_at)
    departure = rendezvous_start - timedelta(minutes=15)
    available = int((departure - current_end).total_seconds() // 60)
    if available < 30:
        return None
    now_text = execution_clock.now().astimezone(timezone.utc).isoformat()
    payload = {
        "signal_kind": "OPPORTUNITY_WINDOW",
        "opens_at": current.end_at,
        "must_depart_by": departure.isoformat(),
        "available_minutes": available,
        "minimum_actionable_window_min": 30,
        "current_activity_id": current.id,
        "current_execution_activity_id": current.id,
        "current_execution_location": current.place_id or current.city_service_id,
        "next_activity_id": rendezvous.id,
        "next_activity_start": rendezvous.start_at,
        "next_commitment_start": rendezvous.start_at,
        "next_commitment_semantic_key": rendezvous.place_id or rendezvous.city_service_id,
        "safety_buffer_min": 15,
        "active_plan_version_id": journey.active_plan_version_id,
        "twin_snapshot_at": now_text,
        "mobility_snapshot_id": "R73-TEST-REPLAY-EXPLICIT-FIXTURE",
        "mobility_provider": "EXPLICIT_FROZEN_FIXTURE",
        "fixture_class": "TEST_REPLAY",
    }
    event = EventModel(
        id=str(uuid4()),
        journey_id=str(journey.id),
        provider_key="citynexus-twin-opportunity",
        provenance="SYSTEM_DERIVED",
        event_type="OPPORTUNITY_WINDOW",
        severity="INFO",
        status="DETECTED",
        affected_activity_id=None,
        occurred_at=current.end_at,
        detected_at=now_text,
        resolved_at=None,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        created_at=now_text,
    )
    session.add(event)
    session.flush()
    return event


def _capture_frozen_route(_session, _journey, pairs, *, operation_key, captured_at=None, **_kwargs):
    facts = {tuple(pair): 6 for pair in pairs}
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key=SOURCE_KEY,
        minutes=facts,
        captured_at=captured_at,
        operation_key=operation_key,
        fact_quality=EXPLICIT_FROZEN_FIXTURE,
        provider_capabilities={"fixture_class": "TEST_REPLAY"},
    )
    return MobilitySnapshotCaptureResult(
        snapshot=snapshot,
        failed_primary_pairs=(),
        fallback_pairs=(),
    )


main_module._ensure_opportunity_window_event = _fixture_opportunity_window
main_module.capture_runtime_mobility_snapshot = _capture_frozen_route


@app.post("/__test/r73/prepare/{journey_id}")
def prepare_r73_fixture(
    journey_id: str,
    payload: dict[str, str],
    x_r73_test_fixture_token: str | None = Header(default=None, alias="X-R73-Test-Fixture-Token"),
):
    _require_fixture_token(x_r73_test_fixture_token)
    side_quest_id = str(payload.get("side_quest_id") or "")
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        side_quest = session.get(MemberSideQuest, side_quest_id)
        if (
            journey is None
            or side_quest is None
            or str(side_quest.journey_id) != journey_id
            or not journey.active_plan_version_id
            or str(side_quest.active_plan_version_id) != str(journey.active_plan_version_id)
        ):
            raise HTTPException(status_code=409, detail="R73_TEST_REPLAY_FIXTURE_BINDING_MISMATCH")
        place = session.get(Place, str(side_quest.side_place_id or ""))
        current = session.get(Activity, str(journey.current_activity_id or ""))
        rendezvous = session.get(Activity, str(side_quest.rendezvous_activity_id))
        rows = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == str(journey.active_plan_version_id))
            .order_by(Activity.sequence_no)
        ).all()
        if place is None or current is None or rendezvous is None or rendezvous not in rows:
            raise HTTPException(status_code=409, detail="R73_TEST_REPLAY_FIXTURE_PLAN_INCOMPLETE")

        active_members = session.scalars(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.joined_at.is_not(None),
                JourneyMember.removed_at.is_(None),
            )
        ).all()
        if len(active_members) < 2:
            raise HTTPException(status_code=409, detail="R73_TEST_REPLAY_REQUIRES_SIGNED_A_AND_B")

        now = execution_clock.now().astimezone(timezone.utc)
        current.start_at = (now - timedelta(minutes=60)).isoformat()
        current.end_at = (now - timedelta(minutes=15)).isoformat()
        current.status = "IN_PROGRESS"
        original_duration = max(
            30,
            int((datetime.fromisoformat(rendezvous.end_at) - datetime.fromisoformat(rendezvous.start_at)).total_seconds() // 60),
        )
        anchor_start = now + timedelta(minutes=180)
        rendezvous.start_at = anchor_start.isoformat()
        rendezvous.end_at = (anchor_start + timedelta(minutes=original_duration)).isoformat()
        cursor = datetime.fromisoformat(rendezvous.end_at)
        for row in rows:
            if row.sequence_no <= rendezvous.sequence_no:
                continue
            duration = max(
                30,
                int((datetime.fromisoformat(row.end_at) - datetime.fromisoformat(row.start_at)).total_seconds() // 60),
            )
            start = cursor + timedelta(minutes=20)
            row.start_at = start.isoformat()
            row.end_at = (start + timedelta(minutes=duration)).isoformat()
            cursor = datetime.fromisoformat(row.end_at)

        for activity in rows:
            if activity.place_id:
                scheduled_place = session.get(Place, str(activity.place_id))
                if scheduled_place is not None:
                    scheduled_place.opening_hours_json = json.dumps({
                        "status": "NORMALIZED_VALID",
                        "open": "00:00",
                        "close": "24:00",
                        "intervals": [{"days": ["*"], "open": "00:00", "close": "24:00"}],
                        "fixture_class": "TEST_REPLAY",
                    })
        place.opening_hours_json = json.dumps({
            "status": "NORMALIZED_VALID",
            "open": "00:00",
            "close": "24:00",
            "intervals": [{"days": ["*"], "open": "00:00", "close": "24:00"}],
            "fixture_class": "TEST_REPLAY",
        })
        metadata = json.loads(place.metadata_json or "{}")
        metadata["tags"] = {**metadata.get("tags", {}), "fee": "no", "opening_hours": "24/7"}
        metadata["opening_hours_fixture"] = {
            "fixture_class": "TEST_REPLAY",
            "source": "R73_CONTROLLED_ACTIVE_PLAN_FIXTURE",
        }
        metadata["cost_evidence"] = {
            "status": "KNOWN",
            "amount_minor": 0,
            "currency": None,
            "currency_agnostic": True,
            "evidence_basis": "OSM_FEE_NO",
            "source": SOURCE_KEY,
            "fixture_class": "TEST_REPLAY",
        }
        place.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        journey.start_at = current.start_at

        semantic_key = json.loads(rendezvous.metadata_json or "{}").get("semantic_key") or rendezvous.place_id or rendezvous.city_service_id
        commitment_ref = f"R73_TEST_REPLAY_RENDEZVOUS:{side_quest_id}"
        existing = session.scalar(select(Commitment).where(Commitment.external_ref == commitment_ref))
        if existing is None:
            session.add(Commitment(
                id=str(uuid4()),
                journey_id=journey_id,
                activity_id=rendezvous.id,
                kind="APPOINTMENT",
                level="C4_Anchored",
                status="ACTIVE",
                monetary_value_minor=0,
                currency=str(journey.budget_currency or "CNY"),
                external_ref=commitment_ref,
                policy_json=json.dumps({
                    "semantic_key": str(semantic_key or ""),
                    "scheduled_start_at": rendezvous.start_at,
                    "evidence_class": "TEST_REPLAY",
                }),
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            ))
        session.commit()
        return {
            "fixture_class": "TEST_REPLAY",
            "route_source": SOURCE_KEY,
            "journey_id": journey_id,
            "side_quest_id": side_quest_id,
            "place_id": place.id,
            "active_plan_version_id": str(journey.active_plan_version_id),
            "rendezvous_activity_id": rendezvous.id,
            "current_activity_id": current.id,
            "active_member_count": len(active_members),
            "rendezvous_commitment_ref": commitment_ref,
        }


@app.get("/__test/r73/evidence/{journey_id}")
def r73_evidence(
    journey_id: str,
    x_r73_test_fixture_token: str | None = Header(default=None, alias="X-R73-Test-Fixture-Token"),
):
    _require_fixture_token(x_r73_test_fixture_token)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        if journey is None or not journey.active_plan_version_id:
            raise HTTPException(status_code=404, detail="JOURNEY_NOT_FOUND")
        active_plan = session.get(PlanVersion, str(journey.active_plan_version_id))
        activities = session.scalars(
            select(Activity).where(Activity.plan_version_id == str(journey.active_plan_version_id))
        ).all()
        invitation = session.scalar(
            select(MemberPlaceInvitation)
            .where(MemberPlaceInvitation.journey_id == journey_id)
            .order_by(MemberPlaceInvitation.created_at.desc())
        )
        promotions = []
        if invitation is not None:
            promotions = [
                {
                    "id": row.id,
                    "place_id": row.place_id,
                    "state": row.state,
                    "applied_plan_version_id": row.applied_plan_version_id,
                    "active_plan_version_id": row.active_plan_version_id,
                }
                for row in session.scalars(
                    select(MemberPlacePromotion).where(
                        MemberPlacePromotion.journey_id == journey_id
                    )
                ).all()
            ]
        commitment_ref = (
            f"R73_TEST_REPLAY_RENDEZVOUS:{invitation.owner_side_quest_id}"
            if invitation
            else ""
        )
        commitment = session.scalar(select(Commitment).where(Commitment.external_ref == commitment_ref)) if commitment_ref else None
        protected_activity = session.get(Activity, str(commitment.activity_id)) if commitment and commitment.activity_id else None
        mobility = json.loads(active_plan.mobility_evidence_json or "{}") if active_plan else {}
        return {
            "fixture_class": "TEST_REPLAY",
            "active_plan_version_id": str(journey.active_plan_version_id),
            "active_plan_status": active_plan.status if active_plan else None,
            "active_plan_count": session.scalar(
                select(func.count()).select_from(PlanVersion).where(
                    PlanVersion.journey_id == journey_id,
                    PlanVersion.status == "ACTIVE",
                )
            ),
            "plan_ids_and_statuses": [
                {"id": row.id, "status": row.status}
                for row in session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all()
            ],
            "activities": [
                {"id": row.id, "place_id": row.place_id, "activity_type": row.activity_type, "sequence_no": row.sequence_no}
                for row in activities
            ],
            "rendezvous_commitment_activity_plan_id": protected_activity.plan_version_id if protected_activity else None,
            "mobility_source_provider": mobility.get("source_provider"),
            "mobility_fact_qualities": sorted({str(item.get("fact_quality")) for item in mobility.get("facts", []) if isinstance(item, dict)}),
            "agent_run_count": session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id)
            ),
            "provider_call_count": session.scalar(select(func.count()).select_from(ProviderCall)),
            "route_acquisition_operation_count": session.scalar(
                select(func.count()).select_from(RouteAcquisitionOperation).where(
                    RouteAcquisitionOperation.journey_id == journey_id
                )
            ),
            "promotions": promotions,
        }


def main() -> None:
    import uvicorn

    host = "127.0.0.1"
    port = int(os.environ["CITYNEXUS_BACKEND_PORT"])
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
