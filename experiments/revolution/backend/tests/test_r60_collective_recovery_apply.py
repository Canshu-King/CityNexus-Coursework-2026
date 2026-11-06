from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.collective_mediation as collective_mediation
import app.main as main_module
import app.operator_projection as operator_projection_module
from app.collective_authority import AUTHENTICATED_MEMBER_CONSENT
from app.collective_operator import collective_operator_projection
from app.core.config import settings
from app.agent.gateway import AgentToolGateway
from app.db.models import (
    AuditLog,
    CollectiveAuthorityReceipt,
    Event,
    IdempotencyKey,
    Journey,
    JourneyMember,
    JourneyMission,
    JourneyRoleCapability,
    PlanVersion,
    ReplanProposal,
    RecoveryEpoch,
    Traveler,
)
from app.db.session import SessionLocal
from app.schemas import AgentToolCall
from app.traveler_principal import issue_traveler_token
from app.recovery_epoch import recovery_reality_basis, recovery_reality_fingerprint


client = TestClient(main_module.app)
_TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_r60_apply_world() -> dict[str, str | list[dict[str, object]]]:
    now = _now()
    owner_id, admin_id = str(uuid4()), str(uuid4())
    journey_id = str(uuid4())
    owner_member_id, admin_member_id = str(uuid4()), str(uuid4())
    base_id, selected_plan_id, sibling_plan_id = (str(uuid4()) for _ in range(3))
    event_id = str(uuid4())
    selected_proposal_id, sibling_proposal_id = str(uuid4()), str(uuid4())
    room_id, child_id = str(uuid4()), str(uuid4())
    receipt_id, recovery_epoch_id = str(uuid4()), str(uuid4())
    incident_key = f"r60-incident-{journey_id}"
    decision_epoch = "r60-current-decision-epoch"
    material_world_epoch = "r60-current-world-epoch"
    surface_fingerprint = "r60-current-candidate-surface"
    options: list[dict[str, object]] = [
        {
            "proposal_id": selected_proposal_id,
            "plan_id": selected_plan_id,
            "status": "GROUNDED_FEASIBLE",
            "label": "Garden recovery",
            "fit_subject_place_ids": ["place-r60-garden"],
            "fit_subject_tags": ["garden"],
            "member_fits": {},
            "fairness": {"minimum_member_fit": 0.5, "maximum_regret": 0.3, "unacceptable_member_ids": []},
        },
        {
            "proposal_id": sibling_proposal_id,
            "plan_id": sibling_plan_id,
            "status": "GROUNDED_FEASIBLE",
            "label": "Museum recovery",
            "fit_subject_place_ids": ["place-r60-museum"],
            "fit_subject_tags": ["museum"],
            "member_fits": {},
            "fairness": {"minimum_member_fit": 0.4, "maximum_regret": 0.4, "unacceptable_member_ids": []},
        },
    ]
    future_end = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    with SessionLocal() as session:
        session.add_all(
            [
                Traveler(id=owner_id, display_name="R60 owner", locale="en-US", timezone="UTC", created_at=now, updated_at=now),
                Traveler(id=admin_id, display_name="R60 admin", locale="en-US", timezone="UTC", created_at=now, updated_at=now),
            ]
        )
        session.flush()
        journey = Journey(
            id=journey_id,
            traveler_id=owner_id,
            title="R60 collective apply",
            city_code="NARA",
            timezone="UTC",
            start_at=now,
            end_at=future_end,
            budget_currency="JPY",
            budget_amount_minor=10000,
            planning_mode="balanced",
            status="ACTIVE",
            active_plan_version_id=None,
            current_activity_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(journey)
        session.flush()
        session.add_all(
            [
                JourneyMember(id=owner_member_id, journey_id=journey_id, traveler_id=owner_id, role="OWNER", invited_by=owner_id, joined_at=now, preferences_json='{"preferences":[{"key":"garden"}]}', created_at=now, updated_at=now),
                JourneyMember(id=admin_member_id, journey_id=journey_id, traveler_id=admin_id, role="ADMIN", invited_by=owner_id, joined_at=now, preferences_json='{"preferences":[{"key":"museum"}]}', created_at=now, updated_at=now),
            ]
        )
        for plan_id, version_no, status in (
            (base_id, 1, "ACTIVE"),
            (selected_plan_id, 2, "CANDIDATE"),
            (sibling_plan_id, 3, "CANDIDATE"),
        ):
            session.add(
                PlanVersion(
                    id=plan_id,
                    journey_id=journey_id,
                    version_no=version_no,
                    planning_mode="balanced",
                    status=status,
                    lifecycle="ACTIVE",
                    source="INITIAL" if plan_id == base_id else "REPLAN",
                    parent_plan_version_id=base_id if plan_id != base_id else None,
                    total_cost_minor=0,
                    currency="JPY",
                    estimated_duration_min=60,
                    utility_score=0.0,
                    score_breakdown_json="{}",
                    explanation_json="{}",
                    mobility_evidence_json="{}",
                    created_at=now,
                )
            )
        session.flush()
        journey.active_plan_version_id = base_id
        session.add(
            Event(
                id=event_id,
                journey_id=journey_id,
                provider_key="journey-group-presence",
                provenance="SYSTEM_DERIVED",
                event_type="TRAFFIC_DELAY",
                severity="MAJOR",
                status="DETECTED",
                occurred_at=now,
                detected_at=now,
                payload_json="{}",
                created_at=now,
            )
        )
        session.flush()
        recovery_fingerprint = recovery_reality_fingerprint(
            recovery_reality_basis(session, journey)
        )
        session.add(
            RecoveryEpoch(
                id=recovery_epoch_id,
                journey_id=journey_id,
                generation=1,
                active_plan_version_id_at_open=base_id,
                material_world_epoch_at_open=recovery_fingerprint,
                recovery_reality_fingerprint=recovery_fingerprint,
                triggering_event_ids_json=json.dumps([event_id]),
                status="OPEN",
                opened_at=now,
            )
        )
        session.flush()
        for proposal_id, plan_id in (
            (selected_proposal_id, selected_plan_id),
            (sibling_proposal_id, sibling_plan_id),
        ):
            session.add(
                ReplanProposal(
                    id=proposal_id,
                    journey_id=journey_id,
                    trigger_event_id=event_id,
                    base_plan_version_id=base_id,
                    proposed_plan_version_id=plan_id,
                    status="PRESENTED",
                    hard_constraints_passed=True,
                    locked_commitments_preserved=True,
                    utility_score=0.0,
                    score_breakdown_json="{}",
                    diff_json="{}",
                    explanation_json="{}",
                    created_at=now,
                    recovery_epoch_id=recovery_epoch_id,
                )
            )
        clarification = {
            "kind": "COLLECTIVE_TRADEOFF",
            "policy": "MATERIAL_TRADEOFF_REQUIRES_ALL_RELEVANT_MEMBER_RESPONSES",
            "status": "RESOLVED",
            "clarification_id": "r60-resolved-clarification",
            "decision_epoch": decision_epoch,
            "material_world_epoch": material_world_epoch,
            "candidate_surface_fingerprint": surface_fingerprint,
            "trigger_event_id": event_id,
            "base_plan_version_id": base_id,
            "candidate_options": options,
            "required_member_ids": [owner_member_id, admin_member_id],
            "decision_relevant_member_ids": [owner_member_id, admin_member_id],
            "responses": {
                owner_member_id: {"proposal_id": selected_proposal_id},
                admin_member_id: {"proposal_id": selected_proposal_id},
            },
            "selected_proposal_id": selected_proposal_id,
            "authority_receipt_id": receipt_id,
        }
        room = JourneyMission(
            id=room_id,
            journey_id=journey_id,
            traveler_id=owner_id,
            trigger_event_id=event_id,
            user_text="Resolve the current group recovery.",
            requested_autonomy="EXECUTE_WITHIN_BOUNDS",
            status="ANSWERED",
            resolved_intent_json=json.dumps(
                {
                    "group_recovery": {
                        "incident_key": incident_key,
                        "hermes_run_id": "r60-test-run",
                        "requested_by_member_id": owner_member_id,
                        "active_plan_version_id": base_id,
                    }
                },
                sort_keys=True,
            ),
            clarification_json=json.dumps(clarification, sort_keys=True),
            max_cost_delta_minor=2000,
            max_loss_minor=1000,
            max_schedule_drift_min=30,
            protected_activity_ids_json="[]",
            mutable_activity_ids_json="[]",
            allowed_action_classes_json='["REPLAN"]',
            created_at=now,
            started_at=now,
            completed_at=now,
        )
        session.add(room)
        session.flush()
        session.add(
            JourneyMission(
                id=child_id,
                journey_id=journey_id,
                traveler_id=owner_id,
                trigger_event_id=event_id,
                parent_mission_id=room_id,
                user_text="Continue with the group decision.",
                requested_autonomy="EXECUTE_WITHIN_BOUNDS",
                status="ANSWERED",
                resolved_intent_json=json.dumps(
                    {
                        "collective_decision": {
                            "clarification_id": clarification["clarification_id"],
                            "decision_epoch": decision_epoch,
                            "material_world_epoch": material_world_epoch,
                            "candidate_surface_fingerprint": surface_fingerprint,
                            "authority_receipt_id": receipt_id,
                            "selected_proposal_id": selected_proposal_id,
                        }
                    },
                    sort_keys=True,
                ),
                clarification_json="{}",
                created_at=now,
                started_at=now,
                completed_at=now,
            )
        )
        session.flush()
        session.add(
            CollectiveAuthorityReceipt(
                id=receipt_id,
                journey_id=journey_id,
                proposal_id=selected_proposal_id,
                plan_version_id=selected_plan_id,
                authority_mode=AUTHENTICATED_MEMBER_CONSENT,
                decision_epoch=decision_epoch,
                material_world_epoch=material_world_epoch,
                candidate_surface_fingerprint=surface_fingerprint,
                source_mission_id=room_id,
                recovery_epoch_id=recovery_epoch_id,
                required_member_ids_json=json.dumps([owner_member_id, admin_member_id]),
                response_audit_ids_json="[]",
                principal_actor_ids_json=json.dumps([owner_id, admin_id]),
                classification="HUMAN_TRADEOFF_REQUIRED",
                issued_at=now,
            )
        )
        session.commit()
    return {
        "journey_id": journey_id,
        "owner_id": owner_id,
        "admin_id": admin_id,
        "owner_member_id": owner_member_id,
        "admin_member_id": admin_member_id,
        "base_id": base_id,
        "selected_plan_id": selected_plan_id,
        "sibling_plan_id": sibling_plan_id,
        "selected_proposal_id": selected_proposal_id,
        "sibling_proposal_id": sibling_proposal_id,
        "event_id": event_id,
        "room_id": room_id,
        "child_id": child_id,
        "receipt_id": receipt_id,
        "recovery_epoch_id": recovery_epoch_id,
        "incident_key": incident_key,
        "decision_epoch": decision_epoch,
        "material_world_epoch": material_world_epoch,
        "surface_fingerprint": surface_fingerprint,
        "options": options,
    }


def _create_r61_reground_mission(session, seeded) -> JourneyMission:
    journey_id = str(seeded["journey_id"])
    mission_id = str(uuid4())
    now = _now()
    context = {
        "kind": "R61_REGROUNDED_COLLECTIVE_RECOVERY",
        "incident_key": str(seeded["incident_key"]),
        "reground_key": f"group-recovery-reground:{journey_id}:{seeded['incident_key']}:{seeded['recovery_epoch_id']}",
        "recovery_epoch_id": str(seeded["recovery_epoch_id"]),
        "recovery_epoch_generation": 1,
        "trigger_event_id": str(seeded["event_id"]),
        "active_plan_version_id": str(seeded["base_id"]),
        "failed_decision_mission_id": str(seeded["room_id"]),
        "failed_clarification_id": "r60-resolved-clarification",
        "failed_proposal_id": str(seeded["selected_proposal_id"]),
    }
    mission = JourneyMission(
        id=mission_id,
        journey_id=journey_id,
        traveler_id=str(seeded["owner_id"]),
        trigger_event_id=str(seeded["event_id"]),
        user_text="Re-ground the same shared recovery against current reality.",
        objective_codes_json=json.dumps(
            ["COLLECTIVE_JOURNEY_RECOVERY", "COLLECTIVE_RECOVERY_REGROUND"]
        ),
        protected_activity_ids_json="[]",
        mutable_activity_ids_json="[]",
        intent_json="{}",
        resolved_intent_json=json.dumps({"group_recovery": context}, sort_keys=True),
        allowed_action_classes_json='["REQUEST_REPLAN"]',
        parent_mission_id=str(seeded["room_id"]),
        max_cost_delta_minor=1000,
        max_loss_minor=500,
        max_schedule_drift_min=15,
        requested_autonomy="PLAN_ONLY",
        status="RUNNING",
        created_at=now,
        started_at=now,
        clarification_json=json.dumps(
            {
                "kind": "GROUP_RECOVERY_REGROUND",
                "state": "RECOVERY_REGROUNDING",
                "incident_key": str(seeded["incident_key"]),
                "recovery_epoch_id": str(seeded["recovery_epoch_id"]),
                "plan_unchanged": True,
            },
            sort_keys=True,
        ),
    )
    session.add(mission)
    session.flush()
    return mission


def _enable_r60_apply_path(
    monkeypatch, seeded, *, stale: bool = False, mock_resolution: bool = True
) -> None:
    owner_member_id = str(seeded["owner_member_id"])
    admin_member_id = str(seeded["admin_member_id"])
    selected_proposal_id = str(seeded["selected_proposal_id"])
    selected_plan_id = str(seeded["selected_plan_id"])
    receipt_id = str(seeded["receipt_id"])
    recovery_epoch_id = str(seeded["recovery_epoch_id"])
    monkeypatch.setattr(main_module, "group_presence_projection", lambda *_args, **_kwargs: {"state": "GROUP_SPLIT_ADVISORY"})
    monkeypatch.setattr(
        main_module,
        "group_recovery_projection",
        lambda _presence: {
            "can_request_planning": True,
            "affected_member": {"member_id": admin_member_id},
            "shared_commitment": {"activity_id": "r60-rendezvous"},
        },
    )
    monkeypatch.setattr(main_module, "group_recovery_incident_key", lambda **_kwargs: str(seeded["incident_key"]))
    if stale:
        def stale_resolution(*_args, **_kwargs):
            raise HTTPException(409, "STALE_COLLECTIVE_DECISION")

        monkeypatch.setattr(main_module, "_collective_resolution_for_child", stale_resolution)
    elif mock_resolution:
        monkeypatch.setattr(
            main_module,
            "_collective_resolution_for_child",
            lambda _session, _journey, proposal, _mission_id: {
                "status": "RESOLVED",
                "selected_proposal_id": str(proposal.id),
                "selected_candidate_id": selected_plan_id,
                "selected_plan_version_id": selected_plan_id,
                "authority_receipt_id": receipt_id,
                "decision_epoch": str(seeded["decision_epoch"]),
                "material_world_epoch": str(seeded["material_world_epoch"]),
                "candidate_surface_fingerprint": str(seeded["surface_fingerprint"]),
                "recovery_epoch_id": recovery_epoch_id,
            },
        )
    monkeypatch.setattr(
        main_module,
        "validate_recovery_epoch_binding",
        lambda session, _journey_id, epoch_id: session.get(RecoveryEpoch, str(epoch_id)),
    )
    monkeypatch.setattr(main_module, "_proposal_mobility_snapshot", lambda _proposal: object())
    monkeypatch.setattr(
        main_module,
        "validate_plan_feasibility",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True, failures=[]),
    )
    monkeypatch.setattr(
        main_module,
        "collective_authority_preflight",
        lambda *_args, **_kwargs: {
            "allowed": True,
            "classification": "HUMAN_TRADEOFF_REQUIRED",
            "authority_receipt_id": receipt_id,
            "resolved_decision_epoch": str(seeded["decision_epoch"]),
            "current_decision_epoch": str(seeded["decision_epoch"]),
            "resolved_material_world_epoch": str(seeded["material_world_epoch"]),
            "current_material_world_epoch": str(seeded["material_world_epoch"]),
            "candidate_surface_fingerprint": str(seeded["surface_fingerprint"]),
            "recovery_epoch_id": recovery_epoch_id,
        },
    )
    monkeypatch.setattr(main_module, "_revalidate_locked_commitments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_twin_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_module, "_compile_transactional_recovery_if_needed", lambda _session, result: result)


def _apply_headers(traveler_id: str, *, idempotency_key: str = "r60-apply-once-001") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issue_traveler_token(traveler_id)}",
        "Idempotency-Key": idempotency_key,
    }


def test_r60_apply_requires_signed_in_force_mutation_member_and_empty_body(monkeypatch) -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    seeded = _seed_r60_apply_world()
    _enable_r60_apply_path(monkeypatch, seeded)
    path = f"/api/v1/journeys/{seeded['journey_id']}/group-recovery/apply"
    try:
        unauthenticated = client.post(path, json={})
        assert unauthenticated.status_code == 401

        forged = client.post(
            path,
            headers=_apply_headers(str(seeded["owner_id"])),
            json={"proposal_id": str(seeded["sibling_proposal_id"]), "member_id": str(seeded["admin_member_id"]), "traveler_id": str(seeded["admin_id"])},
        )
        assert forged.status_code == 422

        with SessionLocal() as session:
            read_only = session.get(JourneyMember, str(seeded["admin_member_id"]))
            assert read_only is not None
            read_only.role = "COLLABORATOR"
            session.commit()
        read_only_response = client.post(
            path,
            headers=_apply_headers(str(seeded["admin_id"])),
            json={},
        )
        assert read_only_response.status_code == 403
        assert "JOURNEY_UPDATE" in str(read_only_response.json())
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r60_signed_apply_spends_only_server_selected_recovery_once(monkeypatch) -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    seeded = _seed_r60_apply_world()
    _enable_r60_apply_path(monkeypatch, seeded)
    path = f"/api/v1/journeys/{seeded['journey_id']}/group-recovery/apply"
    headers = _apply_headers(str(seeded["owner_id"]))
    try:
        arbitrary_proposal = client.post(
            path,
            headers=headers,
            json={"proposal_id": str(seeded["sibling_proposal_id"])},
        )
        assert arbitrary_proposal.status_code == 422

        generic_accept = client.post(
            f"/api/v1/replans/{seeded['selected_proposal_id']}/accept",
            headers={"Idempotency-Key": "r60-generic-must-refuse"},
        )
        assert generic_accept.status_code == 409
        assert "GROUP_RECOVERY_REQUIRES_AUTHENTICATED_APPLY" in str(generic_accept.json())

        first = client.post(path, headers=headers, json={})
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["group_recovery_apply"]["result"] == "APPLIED"
        assert body["group_recovery_apply"]["selected_proposal_id"] == seeded["selected_proposal_id"]
        assert body["journey"]["active_plan_version_id"] == seeded["selected_plan_id"]

        replay = client.post(path, headers=headers, json={})
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["journey"]["active_plan_version_id"] == seeded["selected_plan_id"]

        with SessionLocal() as session:
            journey = session.get(Journey, str(seeded["journey_id"]))
            base = session.get(PlanVersion, str(seeded["base_id"]))
            selected = session.get(PlanVersion, str(seeded["selected_plan_id"]))
            proposal = session.get(ReplanProposal, str(seeded["selected_proposal_id"]))
            sibling = session.get(ReplanProposal, str(seeded["sibling_proposal_id"]))
            event = session.get(Event, str(seeded["event_id"]))
            audits = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == str(seeded["journey_id"]),
                    AuditLog.action == "replan.accepted",
                )
            ).all()
            idempotency = session.scalars(
                select(IdempotencyKey).where(
                    IdempotencyKey.operation == "replan.accept",
                    IdempotencyKey.key.like("replan.accept:gr-%"),
                )
            ).all()
            assert journey is not None and journey.active_plan_version_id == str(seeded["selected_plan_id"])
            assert base is not None and base.status == "SUPERSEDED"
            assert selected is not None and selected.status == "ACTIVE"
            assert proposal is not None and proposal.status == "ACCEPTED"
            assert sibling is not None and sibling.status == "INVALIDATED"
            assert event is not None and event.status == "RESOLVED"
            assert len(audits) == 1
            spend = json.loads(audits[0].after_json or "{}")["group_recovery_spend"]
            assert audits[0].actor_type == "AUTHENTICATED_TRAVELER"
            assert audits[0].actor_id == str(seeded["owner_id"])
            assert spend["journey_member_id"] == str(seeded["owner_member_id"])
            assert spend["member_role"] == "OWNER"
            assert spend["before_plan_version_id"] == str(seeded["base_id"])
            assert spend["after_plan_version_id"] == str(seeded["selected_plan_id"])
            assert spend["authority_receipt_id"] == str(seeded["receipt_id"])
            assert len(idempotency) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r60_stale_apply_preserves_old_answers_and_reuses_one_successor_room(monkeypatch) -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    policy_key = {"role": "ADMIN", "capability": "JOURNEY_UPDATE"}
    with SessionLocal() as session:
        original_policy = session.get(JourneyRoleCapability, policy_key)
        original_policy_values = (
            {
                "scope_message_key": original_policy.scope_message_key,
                "created_at": original_policy.created_at,
            }
            if original_policy is not None
            else None
        )
        if original_policy is None:
            session.add(
                JourneyRoleCapability(
                    role="ADMIN",
                    capability="JOURNEY_UPDATE",
                    scope_message_key="test.r60.admin.update",
                    created_at=_now(),
                )
            )
            session.commit()
    seeded = _seed_r60_apply_world()
    _enable_r60_apply_path(monkeypatch, seeded, mock_resolution=False)
    options = seeded["options"]
    option_by_id = {str(item["proposal_id"]): item for item in options}

    # Bind the durable receipt to the actually current two-updater epoch. The
    # test then removes ADMIN's mutation capability before the signed apply.
    with SessionLocal() as session:
        journey = session.get(Journey, str(seeded["journey_id"]))
        room = session.get(JourneyMission, str(seeded["room_id"]))
        child = session.get(JourneyMission, str(seeded["child_id"]))
        receipt = session.get(CollectiveAuthorityReceipt, str(seeded["receipt_id"]))
        assert journey is not None and room is not None and child is not None and receipt is not None
        clarification = json.loads(room.clarification_json or "{}")
        for option in clarification["candidate_options"]:
            plan = session.get(PlanVersion, str(option["plan_id"]))
            assert plan is not None
            option["material_world_epoch"] = collective_mediation.collective_material_world_epoch(
                main_module.collective_material_world_basis(
                    session,
                    journey,
                    plan,
                    fit_subject_place_ids=list(option.get("fit_subject_place_ids") or []),
                )
            )
        selected_option = next(
            item for item in clarification["candidate_options"]
            if item["proposal_id"] == str(seeded["selected_proposal_id"])
        )
        relevant_ids = clarification["decision_relevant_member_ids"]
        members = main_module.active_journey_members_with_capabilities(session, journey)["members"]
        current_epoch = collective_mediation.collective_decision_epoch(
            journey_id=journey.id,
            mission_id=room.id,
            trigger_event_id=str(seeded["event_id"]),
            base_plan_version_id=str(seeded["base_id"]),
            options=clarification["candidate_options"],
            required_member_ids=clarification["required_member_ids"],
            decision_relevant_member_ids=relevant_ids,
            members=members,
        )
        clarification["decision_epoch"] = current_epoch
        clarification["material_world_epoch"] = selected_option["material_world_epoch"]
        room.clarification_json = json.dumps(clarification, sort_keys=True)
        child_intent = json.loads(child.resolved_intent_json or "{}")
        child_intent["collective_decision"]["decision_epoch"] = current_epoch
        child_intent["collective_decision"]["material_world_epoch"] = selected_option["material_world_epoch"]
        child.resolved_intent_json = json.dumps(child_intent, sort_keys=True)
        receipt.decision_epoch = current_epoch
        receipt.material_world_epoch = selected_option["material_world_epoch"]
        session.commit()

        policy = session.get(JourneyRoleCapability, policy_key)
        assert policy is not None
        session.delete(policy)
        session.commit()

    def current_receipt(_session, _journey, _proposals):
        return {
            "proposal_collective_decisions": option_by_id,
            "candidate_fit_matrix": [{"candidate_id": "r60-current-surface"}],
            "collective_mediation": {
                "classification": "HUMAN_TRADEOFF_REQUIRED",
                "decision_relevant_member_ids": [
                    str(seeded["owner_member_id"]),
                    str(seeded["admin_member_id"]),
                ],
            },
        }

    monkeypatch.setattr(main_module, "collective_proposal_receipt", current_receipt)
    monkeypatch.setattr(
        collective_mediation,
        "collective_decision_epoch",
        lambda **kwargs: f"r60-reconsent-{kwargs['mission_id']}-{len(kwargs['options'])}",
    )
    path = f"/api/v1/journeys/{seeded['journey_id']}/group-recovery/apply"
    headers = _apply_headers(str(seeded["owner_id"]), idempotency_key="REDACTED_FOR_PUBLIC_RELEASE")
    try:
        stale = client.post(path, headers=headers, json={})
        assert stale.status_code == 409, stale.text
        assert "COLLECTIVE_CONSENT_STALE_RECONSENT_REQUIRED" in str(stale.json())

        with SessionLocal() as session:
            journey = session.get(Journey, str(seeded["journey_id"]))
            old_room = session.get(JourneyMission, str(seeded["room_id"]))
            old_receipt = session.get(CollectiveAuthorityReceipt, str(seeded["receipt_id"]))
            assert journey is not None and journey.active_plan_version_id == str(seeded["base_id"])
            assert old_room is not None
            old = json.loads(old_room.clarification_json or "{}")
            assert old["status"] == "STALE"
            assert set(old["responses"]) == {
                str(seeded["owner_member_id"]),
                str(seeded["admin_member_id"]),
            }
            assert old_receipt is not None and old_receipt.invalidated_at is not None
            successor_rows = session.scalars(
                select(JourneyMission).where(
                    JourneyMission.parent_mission_id == str(seeded["room_id"])
                )
            ).all()
            successor = next(
                row for row in successor_rows
                if json.loads(row.resolved_intent_json or "{}").get("group_recovery", {}).get(
                    "reconsent_for_clarification_id"
                ) == old["clarification_id"]
            )
            first_successor_id = successor.id
            fresh = json.loads(successor.clarification_json or "{}")
            assert fresh["status"] == "WAITING_FOR_MEMBERS"
            assert fresh["required_member_ids"] == [str(seeded["owner_member_id"])]
            assert fresh["decision_epoch"] != old["decision_epoch"]
            assert fresh["responses"] == {}

        repeated = client.post(path, headers=headers, json={})
        assert repeated.status_code == 409
        assert "COLLECTIVE_CONSENT_RECONSENT_IN_PROGRESS" in str(repeated.json())
        with SessionLocal() as session:
            successors = session.scalars(
                select(JourneyMission).where(
                    JourneyMission.parent_mission_id == str(seeded["room_id"])
                )
            ).all()
            current = [
                row for row in successors
                if json.loads(row.resolved_intent_json or "{}").get("group_recovery", {}).get(
                    "reconsent_for_clarification_id"
                ) == "r60-resolved-clarification"
            ]
            assert len(current) == 1
            assert current[0].id == first_successor_id
    finally:
        with SessionLocal() as session:
            current_policy = session.get(JourneyRoleCapability, policy_key)
            if current_policy is not None:
                session.delete(current_policy)
            if original_policy_values is not None:
                session.add(
                    JourneyRoleCapability(
                        role="ADMIN",
                        capability="JOURNEY_UPDATE",
                        scope_message_key=original_policy_values["scope_message_key"],
                        created_at=original_policy_values["created_at"],
                    )
                )
            session.commit()
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r60_only_consent_staleness_opens_reconsent_not_execution_failure() -> None:
    assert (
        main_module._group_recovery_reconsent_code("STALE_COLLECTIVE_DECISION")
        == "STALE_COLLECTIVE_DECISION"
    )
    assert (
        main_module._group_recovery_reconsent_code(
            {"code": "RECOVERY_EPOCH_SUPERSEDED"}
        )
        == "RECOVERY_EPOCH_SUPERSEDED"
    )
    assert (
        main_module._group_recovery_reconsent_code(
            "COLLECTIVE_AUTHORITY_RECEIPT_STALE_WORLD_EPOCH"
        )
        == "COLLECTIVE_AUTHORITY_RECEIPT_STALE_WORLD_EPOCH"
    )

    # These failures say the concrete future is no longer executable. Human
    # consent cannot repair them and must not be laundered into a fresh vote.
    assert (
        main_module._group_recovery_reconsent_code(
            "replan is no longer feasible: protected commitment violated"
        )
        is None
    )
    assert (
        main_module._group_recovery_reconsent_code(
            "replan mobility evidence is missing or invalid; regenerate proposal"
        )
        is None
    )
    assert (
        main_module._group_recovery_reconsent_code(
            "accepted replan cannot remap an active C4 commitment"
        )
        is None
    )

    available = main_module._group_recovery_stale_or_replan_detail(
        cause="STALE_COLLECTIVE_DECISION",
        reconsent={"available": True, "mission_id": "fresh-room"},
    )
    assert available["code"] == "COLLECTIVE_CONSENT_STALE_RECONSENT_REQUIRED"
    unavailable = main_module._group_recovery_stale_or_replan_detail(
        cause="RECOVERY_EPOCH_SUPERSEDED",
        reconsent={"available": False},
    )
    assert unavailable["code"] == "GROUP_RECOVERY_REPLAN_REQUIRED"
    assert unavailable["consent_stale"] is True


def test_r61_unexecutable_proposal_invalidates_old_authority_and_reuses_one_reground(
    monkeypatch,
) -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    seeded = _seed_r60_apply_world()
    _enable_r60_apply_path(monkeypatch, seeded)
    hermes_checks = []
    monkeypatch.setattr(
        main_module,
        "native_hermes_status",
        lambda: hermes_checks.append("called") or {"ready": False, "provider": "fixture", "model": "fixture"},
    )

    def fail_execution(*_args, **_kwargs):
        raise HTTPException(
            409,
            "replan is no longer feasible: protected commitment violated",
        )

    monkeypatch.setattr(main_module, "_accept_replan_impl", fail_execution)
    path = f"/api/v1/journeys/{seeded['journey_id']}/group-recovery/apply"
    try:
        response = client.post(
            path,
            headers=_apply_headers(
                str(seeded["owner_id"]),
                idempotency_key="r60-invalid-plan-not-revote",
            ),
            json={},
        )
        assert response.status_code == 409, response.text
        envelope = response.json()["error"]
        detail = envelope["details"]
        assert envelope["reason_code"] == "GROUP_RECOVERY_REPLAN_REQUIRED"
        assert detail["code"] == "GROUP_RECOVERY_REPLAN_REQUIRED"
        assert detail["state"] == "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED"
        assert detail["cause_class"] == "PROTECTED_COMMITMENT_CHANGED"
        assert detail["reground"]["reused"] is False
        assert detail["plan_unchanged"] is True
        first_reground_id = detail["reground"]["mission_id"]

        with SessionLocal() as session:
            journey = session.get(Journey, str(seeded["journey_id"]))
            room = session.get(JourneyMission, str(seeded["room_id"]))
            receipt = session.get(
                CollectiveAuthorityReceipt, str(seeded["receipt_id"])
            )
            proposal = session.get(
                ReplanProposal, str(seeded["selected_proposal_id"])
            )
            assert journey is not None
            assert journey.active_plan_version_id == str(seeded["base_id"])
            assert room is not None
            stored = json.loads(room.clarification_json or "{}")
            assert stored["status"] == "STALE"
            assert stored["r61_state"] == "RECOVERY_REPLAN_REQUIRED"
            assert set(stored["responses"]) == {
                str(seeded["owner_member_id"]),
                str(seeded["admin_member_id"]),
            }
            assert receipt is not None and receipt.invalidated_at is not None
            assert proposal is not None and proposal.status == "INVALIDATED"
            reground = session.get(JourneyMission, first_reground_id)
            assert reground is not None
            assert reground.status == "FAILED_EXTERNAL"
            assert reground.requested_autonomy == "PLAN_ONLY"
            reground_context = json.loads(reground.resolved_intent_json or "{}")["group_recovery"]
            assert reground_context["recovery_epoch_id"] == str(seeded["recovery_epoch_id"])
            reground_clarification = json.loads(reground.clarification_json or "{}")
            assert reground_clarification["kind"] == "GROUP_RECOVERY_REGROUND"
            assert reground_clarification["state"] == "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED"
            assert reground_clarification.get("responses") is None
            reconsent_audits = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == str(seeded["journey_id"]),
                    AuditLog.action == "journey.group_recovery.reconsent.started",
                )
            ).all()
            assert reconsent_audits == []
            replan_required = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == str(seeded["journey_id"]),
                    AuditLog.action == "journey.group_recovery.replan_required",
                )
            ).all()
            assert len(replan_required) == 1

        repeated = client.post(
            path,
            headers=_apply_headers(
                str(seeded["owner_id"]),
                idempotency_key="r60-invalid-plan-not-revote",
            ),
            json={},
        )
        assert repeated.status_code == 409, repeated.text
        repeated_detail = repeated.json()["error"]["details"]
        assert repeated_detail["reground"]["mission_id"] == first_reground_id
        assert repeated_detail["reground"]["reused"] is True
        assert hermes_checks == ["called"]
        with SessionLocal() as session:
            reground_missions = [
                row
                for row in session.scalars(
                    select(JourneyMission).where(
                        JourneyMission.journey_id == str(seeded["journey_id"]),
                        JourneyMission.is_standing.is_(False),
                    )
                ).all()
                if json.loads(row.resolved_intent_json or "{}").get("group_recovery", {}).get("kind")
                == "R61_REGROUNDED_COLLECTIVE_RECOVERY"
            ]
            assert len(reground_missions) == 1

        # A fresh read request projects the persisted R61 state; it does not
        # depend on the original apply response or browser memory.
        monkeypatch.setattr(
            operator_projection_module,
            "group_presence_projection",
            lambda *_args, **_kwargs: {"state": "GROUP_SPLIT_ADVISORY"},
        )
        monkeypatch.setattr(
            operator_projection_module,
            "group_recovery_projection",
            lambda _presence: {
                "can_request_planning": True,
                "affected_member": {"member_id": str(seeded["admin_member_id"])},
                "shared_commitment": {"activity_id": "r60-rendezvous"},
                "options": [],
            },
        )
        monkeypatch.setattr(
            operator_projection_module,
            "group_recovery_incident_key",
            lambda **_kwargs: str(seeded["incident_key"]),
        )
        refreshed = client.get(
            f"/api/v1/journeys/{seeded['journey_id']}/operator",
            headers={"Authorization": f"Bearer {issue_traveler_token(str(seeded['owner_id']))}"},
        )
        assert refreshed.status_code == 200, refreshed.text
        projected = refreshed.json()["group_recovery"]
        assert projected["state"] == "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED"
        assert projected["old_decision"]["clarification_id"] == "r60-resolved-clarification"
        assert projected["failed_proposal_id"] == str(seeded["selected_proposal_id"])
        assert projected["reground_mission"]["id"] == first_reground_id
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r61_superseded_consent_without_current_options_starts_one_reground(
    monkeypatch,
) -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", _TEST_SECRET)
    seeded = _seed_r60_apply_world()
    _enable_r60_apply_path(monkeypatch, seeded)
    monkeypatch.setattr(
        main_module,
        "native_hermes_status",
        lambda: {"ready": False, "provider": "fixture", "model": "fixture"},
    )

    def stale_epoch(*_args, **_kwargs):
        raise HTTPException(409, "RECOVERY_EPOCH_SUPERSEDED")

    monkeypatch.setattr(main_module, "_accept_replan_impl", stale_epoch)
    with SessionLocal() as session:
        epoch = session.get(RecoveryEpoch, str(seeded["recovery_epoch_id"]))
        assert epoch is not None
        epoch.status = "SUPERSEDED"
        epoch.closed_at = _now()
        epoch.close_reason = "R61_TEST_WORLD_CHANGED"
        session.commit()

    path = f"/api/v1/journeys/{seeded['journey_id']}/group-recovery/apply"
    headers = _apply_headers(
        str(seeded["owner_id"]),
        idempotency_key="r61-superseded-consent-reground",
    )
    try:
        first = client.post(path, headers=headers, json={})
        assert first.status_code == 409, first.text
        detail = first.json()["error"]["details"]
        assert detail["code"] == "GROUP_RECOVERY_REPLAN_REQUIRED"
        assert detail["state"] == "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED"
        assert detail["cause_class"] == "PROPOSAL_NO_LONGER_CURRENT"
        reground_id = detail["reground"]["mission_id"]
        assert detail["reground"]["reused"] is False
        assert detail["reground"]["current_recovery_epoch_generation"] == 2

        with SessionLocal() as session:
            journey = session.get(Journey, str(seeded["journey_id"]))
            old_room = session.get(JourneyMission, str(seeded["room_id"]))
            old_receipt = session.get(
                CollectiveAuthorityReceipt, str(seeded["receipt_id"])
            )
            old_proposal = session.get(
                ReplanProposal, str(seeded["selected_proposal_id"])
            )
            reground = session.get(JourneyMission, reground_id)
            assert journey is not None
            assert journey.active_plan_version_id == str(seeded["base_id"])
            assert old_room is not None and old_receipt is not None and old_proposal is not None
            old_state = json.loads(old_room.clarification_json or "{}")
            assert old_state["status"] == "STALE"
            assert set(old_state["responses"]) == {
                str(seeded["owner_member_id"]),
                str(seeded["admin_member_id"]),
            }
            assert old_receipt.invalidated_at is not None
            assert old_proposal.status == "INVALIDATED"
            assert reground is not None and reground.status == "FAILED_EXTERNAL"
            reground_context = json.loads(reground.resolved_intent_json or "{}")["group_recovery"]
            assert reground_context["recovery_epoch_id"] != str(seeded["recovery_epoch_id"])
            assert reground_context["recovery_epoch_generation"] == 2
            assert json.loads(reground.clarification_json or "{}").get("responses") is None

        replay = client.post(path, headers=headers, json={})
        assert replay.status_code == 409, replay.text
        replay_detail = replay.json()["error"]["details"]
        assert replay_detail["reground"]["mission_id"] == reground_id
        assert replay_detail["reground"]["reused"] is True
        with SessionLocal() as session:
            regrounds = [
                row
                for row in session.scalars(
                    select(JourneyMission).where(
                        JourneyMission.journey_id == str(seeded["journey_id"]),
                        JourneyMission.is_standing.is_(False),
                    )
                ).all()
                if json.loads(row.resolved_intent_json or "{}").get("group_recovery", {}).get(
                    "kind"
                ) == "R61_REGROUNDED_COLLECTIVE_RECOVERY"
            ]
            assert [row.id for row in regrounds] == [reground_id]
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r60_reconsent_lineage_keeps_original_group_recovery_root() -> None:
    seeded = _seed_r60_apply_world()
    successor_id = str(uuid4())
    now = _now()
    with SessionLocal() as session:
        session.add(
            JourneyMission(
                id=successor_id,
                journey_id=str(seeded["journey_id"]),
                traveler_id=str(seeded["owner_id"]),
                trigger_event_id=str(seeded["event_id"]),
                parent_mission_id=str(seeded["room_id"]),
                user_text="Fresh consent round.",
                requested_autonomy="EXECUTE_WITHIN_BOUNDS",
                status="WAITING_FOR_MEMBERS",
                resolved_intent_json=json.dumps(
                    {
                        "group_recovery": {
                            "incident_key": str(seeded["incident_key"]),
                            "reconsent_for_clarification_id": "old-round",
                            "reconsent_generation": 1,
                        }
                    },
                    sort_keys=True,
                ),
                clarification_json=json.dumps(
                    {"kind": "COLLECTIVE_TRADEOFF", "status": "WAITING_FOR_MEMBERS"},
                    sort_keys=True,
                ),
                created_at=now,
                started_at=now,
            )
        )
        session.commit()

        root, context = main_module._group_recovery_lineage_context(
            session, successor_id
        )
        assert root is not None
        assert root.id == str(seeded["room_id"])
        assert context["incident_key"] == str(seeded["incident_key"])
        assert "reconsent_for_clarification_id" not in context


def test_r61_plan_only_reground_has_research_tools_but_no_mutation_authority() -> None:
    from app.agent.mission_plan import build_mission_plan

    plan = build_mission_plan(
        autonomy="PLAN_ONLY",
        objective_codes=[
            "COLLECTIVE_JOURNEY_RECOVERY",
            "COLLECTIVE_RECOVERY_REGROUND",
        ],
    )
    allowed = set(plan.allowed_tools)
    assert {
        "inspect_journey",
        "inspect_twin",
        "list_replans",
        "search_city_services",
        "acquire_place_facts",
        "estimate_place_facts",
        "generate_plans",
        "request_replan",
        "request_collective_decision",
        "conclude_recovery",
    } <= allowed
    assert not {
        "accept_replan",
        "activate_standby",
        "replace_activity",
        "reschedule_activity",
        "create_booking",
        "cancel_refundable_booking",
        "request_commitment_change",
    } & allowed


def test_r61_terminal_conclusion_is_epoch_scoped_and_keeps_active_plan_unchanged(
    monkeypatch,
) -> None:
    seeded = _seed_r60_apply_world()
    stale_epoch_id, stale_proposal_id = str(uuid4()), str(uuid4())
    now = _now()
    with SessionLocal() as session:
        journey = session.get(Journey, str(seeded["journey_id"]))
        assert journey is not None
        session.add(
            RecoveryEpoch(
                id=stale_epoch_id,
                journey_id=journey.id,
                generation=0,
                active_plan_version_id_at_open=str(seeded["base_id"]),
                material_world_epoch_at_open=None,
                recovery_reality_fingerprint="superseded-r60-world",
                triggering_event_ids_json=json.dumps([seeded["event_id"]]),
                status="SUPERSEDED",
                opened_at=now,
                closed_at=now,
                close_reason="R61_TEST_OLD_WORLD",
            )
        )
        session.flush()
        session.add(
            ReplanProposal(
                id=stale_proposal_id,
                journey_id=journey.id,
                trigger_event_id=str(seeded["event_id"]),
                base_plan_version_id=str(seeded["base_id"]),
                proposed_plan_version_id=str(seeded["sibling_plan_id"]),
                status="PRESENTED",
                hard_constraints_passed=True,
                locked_commitments_preserved=True,
                utility_score=0.0,
                score_breakdown_json="{}",
                diff_json="{}",
                explanation_json="{}",
                created_at=now,
                recovery_epoch_id=stale_epoch_id,
            )
        )
        mission = _create_r61_reground_mission(session, seeded)
        session.commit()
        mission_id = mission.id

    observed_proposal_ids: list[str] = []

    def current_receipt(_session, _journey, proposals):
        observed_proposal_ids.extend(str(item.id) for item in proposals)
        current_ids = {str(item.id) for item in proposals}
        return {
            "collective_mediation": {"classification": "UNANIMOUS_DOMINANCE"},
            "proposal_collective_decisions": {
                proposal_id: {
                    "proposal_id": proposal_id,
                    "status": "GROUNDED_FEASIBLE",
                    "plan_id": str(seeded["selected_plan_id"]),
                }
                for proposal_id in current_ids
            },
        }

    monkeypatch.setattr(main_module, "collective_proposal_receipt", current_receipt)
    with SessionLocal() as session:
        journey = session.get(Journey, str(seeded["journey_id"]))
        assert journey is not None
        before_plan_id = journey.active_plan_version_id
        gateway = AgentToolGateway(session)
        result = gateway.conclude_recovery(
            AgentToolCall(
                request_id="r61-terminal-conclusion",
                journey_id=str(seeded["journey_id"]),
                mission_id=mission_id,
                trigger_event_id=str(seeded["event_id"]),
                outcome="NO_ACTION",
            )
        )
        session.commit()
        assert result["r61_completion"]["state"] == "RECOVERY_REGROUNDED_DOMINANT"
        assert result["r61_completion"]["plan_unchanged"] is True
        assert result["r61_completion"]["mutation_count"] == 0
        replay = gateway.conclude_recovery(
            AgentToolCall(
                request_id="r61-terminal-conclusion-replay",
                journey_id=str(seeded["journey_id"]),
                mission_id=mission_id,
                trigger_event_id=str(seeded["event_id"]),
                outcome="NO_ACTION",
            )
        )
        assert replay["replayed"] is True
        assert replay["r61_completion"] == result["r61_completion"]
        assert str(stale_proposal_id) not in observed_proposal_ids
        assert set(observed_proposal_ids) == {
            str(seeded["selected_proposal_id"]),
            str(seeded["sibling_proposal_id"]),
        }
        session.refresh(journey)
        assert journey.active_plan_version_id == before_plan_id == str(seeded["base_id"])
        mission = session.get(JourneyMission, mission_id)
        assert mission is not None
        assert mission.status == "COMPLETED"
        state = json.loads(mission.clarification_json)
        assert state["state"] == "RECOVERY_REGROUNDED_DOMINANT"
        assert state["current_proposal_ids"] == sorted(observed_proposal_ids)
        assert state["grounded_feasible_proposal_ids"] == sorted(observed_proposal_ids)


def test_r61_malformed_bound_epoch_generation_fails_closed(monkeypatch) -> None:
    seeded = _seed_r60_apply_world()
    with SessionLocal() as session:
        mission = _create_r61_reground_mission(session, seeded)
        context = json.loads(mission.resolved_intent_json)
        context["group_recovery"]["recovery_epoch_generation"] = "not-a-generation"
        mission.resolved_intent_json = json.dumps(context)
        session.commit()
        mission_id = mission.id

    with SessionLocal() as session:
        gateway = AgentToolGateway(session)
        with pytest.raises(HTTPException) as exc_info:
            gateway.list_replans(
                AgentToolCall(
                    request_id="r61-malformed-epoch-generation",
                    journey_id=str(seeded["journey_id"]),
                    mission_id=mission_id,
                    trigger_event_id=str(seeded["event_id"]),
                )
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "RECOVERY_EPOCH_GENERATION_MISMATCH"


def test_r61_fresh_collective_room_uses_current_epoch_only_and_has_no_old_answers(
    monkeypatch,
) -> None:
    seeded = _seed_r60_apply_world()
    stale_epoch_id, stale_proposal_id = str(uuid4()), str(uuid4())
    now = _now()
    with SessionLocal() as session:
        journey = session.get(Journey, str(seeded["journey_id"]))
        assert journey is not None
        session.add(
            RecoveryEpoch(
                id=stale_epoch_id,
                journey_id=journey.id,
                generation=0,
                active_plan_version_id_at_open=str(seeded["base_id"]),
                material_world_epoch_at_open=None,
                recovery_reality_fingerprint="superseded-r60-world",
                triggering_event_ids_json=json.dumps([seeded["event_id"]]),
                status="SUPERSEDED",
                opened_at=now,
                closed_at=now,
                close_reason="R61_TEST_OLD_WORLD",
            )
        )
        session.flush()
        session.add(
            ReplanProposal(
                id=stale_proposal_id,
                journey_id=journey.id,
                trigger_event_id=str(seeded["event_id"]),
                base_plan_version_id=str(seeded["base_id"]),
                proposed_plan_version_id=str(seeded["sibling_plan_id"]),
                status="PRESENTED",
                hard_constraints_passed=True,
                locked_commitments_preserved=True,
                utility_score=0.0,
                score_breakdown_json="{}",
                diff_json="{}",
                explanation_json="{}",
                created_at=now,
                recovery_epoch_id=stale_epoch_id,
            )
        )
        mission = _create_r61_reground_mission(session, seeded)
        session.commit()
        mission_id = mission.id

    observed_proposal_ids: list[str] = []
    matrix = [
        {"candidate_id": str(seeded["selected_proposal_id"])},
        {"candidate_id": str(seeded["sibling_proposal_id"])},
    ]

    def current_receipt(_session, _journey, proposals):
        observed_proposal_ids.extend(str(item.id) for item in proposals)
        return {
            "collective_mediation": {
                "classification": "HUMAN_TRADEOFF_REQUIRED",
                "decision_relevant_member_ids": [
                    str(seeded["owner_member_id"]),
                    str(seeded["admin_member_id"]),
                ],
            },
            "proposal_collective_decisions": {
                str(seeded["selected_proposal_id"]): {
                    "proposal_id": str(seeded["selected_proposal_id"]),
                    "plan_id": str(seeded["selected_plan_id"]),
                    "status": "GROUNDED_FEASIBLE",
                    "fit_subject_place_ids": ["place-r60-garden"],
                    "fit_subject_tags": ["garden"],
                    "member_fits": {},
                    "fairness": {},
                },
                str(seeded["sibling_proposal_id"]): {
                    "proposal_id": str(seeded["sibling_proposal_id"]),
                    "plan_id": str(seeded["sibling_plan_id"]),
                    "status": "GROUNDED_FEASIBLE",
                    "fit_subject_place_ids": ["place-r60-museum"],
                    "fit_subject_tags": ["museum"],
                    "member_fits": {},
                    "fairness": {},
                },
            },
            "candidate_fit_matrix": matrix,
            "member_count": 2,
        }

    monkeypatch.setattr(main_module, "collective_proposal_receipt", current_receipt)
    monkeypatch.setattr(
        main_module,
        "active_journey_members_with_capabilities",
        lambda *_args, **_kwargs: {
            "members": [
                {"member_id": str(seeded["owner_member_id"]), "can_authorize_mutation": True},
                {"member_id": str(seeded["admin_member_id"]), "can_authorize_mutation": True},
            ],
            "member_count": 2,
            "candidate_fit_matrix": matrix,
        },
    )
    with SessionLocal() as session:
        gateway = AgentToolGateway(session)
        result = gateway.request_collective_decision(
            AgentToolCall(
                request_id="r61-fresh-room-request",
                journey_id=str(seeded["journey_id"]),
                mission_id=mission_id,
                trigger_event_id=str(seeded["event_id"]),
            )
        )
        assert result["mission_status"] == "WAITING_FOR_MEMBERS"
        clarification = result["clarification"]
        assert clarification["status"] == "WAITING_FOR_MEMBERS"
        assert clarification["responses"] == {}
        assert clarification["state"] == "RECOVERY_REGROUNDED_DECISION_REQUIRED"
        assert clarification["r61_reground"]["plan_unchanged"] is True
        assert str(stale_proposal_id) not in observed_proposal_ids
        assert set(observed_proposal_ids) == {
            str(seeded["selected_proposal_id"]),
            str(seeded["sibling_proposal_id"]),
        }
        stored_mission = session.get(JourneyMission, mission_id)
        assert stored_mission is not None
        persisted = json.loads(stored_mission.clarification_json)
        assert persisted["responses"] == {}
        assert persisted["r61_reground"]["recovery_epoch_id"] == str(
            seeded["recovery_epoch_id"]
        )


def test_r61_typed_terminal_receipt_completes_plan_only_run_after_proposal_generation() -> None:
    from app.agent.runtime import _server_completion_contract, _terminal_agent_status

    trace = [
        {
            "tool_name": "request_replan",
            "status": "COMPLETED",
            "result": {"proposal": {"id": "current-proposal"}},
        },
        {
            "tool_name": "conclude_recovery",
            "status": "COMPLETED",
            "result": {
                "requested_outcome": "NO_ACTION",
                "r61_completion": {
                    "state": "RECOVERY_REGROUNDED_DOMINANT",
                    "incident_key": "incident-current",
                    "recovery_epoch_id": "epoch-current",
                    "collective_classification": "UNANIMOUS_DOMINANCE",
                    "current_proposal_ids": ["current-proposal"],
                    "grounded_feasible_proposal_ids": ["current-proposal"],
                    "plan_unchanged": True,
                    "mutation_count": 0,
                },
            },
        },
    ]
    completion = _server_completion_contract(trace, run_status="COMPLETED")
    assert completion["kind"] == "NO_ACTION"
    assert completion["r61_state"] == "RECOVERY_REGROUNDED_DOMINANT"
    assert _terminal_agent_status(
        trace, has_final_text=False, clarification=False
    ) == ("COMPLETED", "COMPLETED")


def test_r60_operator_hides_apply_when_bound_recovery_epoch_no_longer_matches_reality(
    monkeypatch,
) -> None:
    seeded = _seed_r60_apply_world()
    options = seeded["options"]

    def current_receipt(_session, _journey, _proposals):
        return {
            "proposal_collective_decisions": {
                str(item["proposal_id"]): item for item in options
            },
            "candidate_fit_matrix": [{"candidate_id": "r60-operator-surface"}],
            "collective_mediation": {
                "classification": "HUMAN_TRADEOFF_REQUIRED",
                "decision_relevant_member_ids": [
                    str(seeded["owner_member_id"]),
                    str(seeded["admin_member_id"]),
                ],
            },
        }

    monkeypatch.setattr(main_module, "collective_proposal_receipt", current_receipt)
    monkeypatch.setattr(
        collective_mediation,
        "collective_decision_epoch",
        lambda **_kwargs: str(seeded["decision_epoch"]),
    )
    import app.collective_authority as collective_authority

    monkeypatch.setattr(
        collective_authority,
        "candidate_surface_fingerprint",
        lambda _surface: str(seeded["surface_fingerprint"]),
    )

    with SessionLocal() as session:
        journey = session.get(Journey, str(seeded["journey_id"]))
        admin_member = session.get(
            JourneyMember, str(seeded["admin_member_id"])
        )
        assert journey is not None and admin_member is not None
        # Keep both required signers on a role the default capability table
        # actually allows to edit; this test isolates RecoveryEpoch freshness.
        admin_member.role = "OWNER"
        session.commit()
        fresh = collective_operator_projection(
            session,
            journey,
            viewer_member_id=str(seeded["owner_member_id"]),
        )
        assert fresh["state"] == "COLLECTIVE_AUTHORIZED"
        assert fresh["recovery_epoch_current"] is True
        assert fresh["viewer_can_apply"] is True

        now = _now()
        session.add(
            Event(
                id=str(uuid4()),
                journey_id=journey.id,
                provider_key="r60-current-world-change",
                provenance="SYSTEM_DERIVED",
                event_type="WEATHER",
                severity="MAJOR",
                status="DETECTED",
                occurred_at=now,
                detected_at=now,
                payload_json=json.dumps(
                    {"incident_fingerprint": "r60-new-world-fact"},
                    sort_keys=True,
                ),
                created_at=now,
            )
        )
        session.commit()

        stale = collective_operator_projection(
            session,
            journey,
            viewer_member_id=str(seeded["owner_member_id"]),
        )
        assert stale["recovery_epoch_current"] is False
        assert stale["state"] == "COLLECTIVE_STALE"
        assert stale["viewer_can_apply"] is False
