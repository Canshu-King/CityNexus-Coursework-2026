from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.agent.contracts import (
    AgentAction,
    AgentMode,
    AuthoritativeActionFacts,
    AuthorizationDecision,
    AuthorizationResult,
    DelegationPolicy,
)
from app.db.models import TravelerAgentPolicy

READ_ACTIONS = frozenset(
    {
        AgentAction.INSPECT_JOURNEY,
        AgentAction.INSPECT_TWIN,
        AgentAction.LIST_REPLANS,
        AgentAction.SEARCH_CITY_SERVICES,
    }
)

TYPED_MUTATION_ACTIONS = frozenset(
    {
        AgentAction.REPLACE_ACTIVITY,
        AgentAction.RESCHEDULE_ACTIVITY,
        AgentAction.CREATE_BOOKING,
        AgentAction.CANCEL_REFUNDABLE_BOOKING,
        AgentAction.REQUEST_COMMITMENT_CHANGE,
    }
)


def _json_list(value: str | None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return default
    if not isinstance(parsed, list):
        return default
    return tuple(dict.fromkeys(str(item).strip() for item in parsed if str(item).strip()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _activity_type_within_envelope(
    activity_type: str | None,
    allowed_activity_types: tuple[str, ...],
) -> bool:
    """Match the stable visit/activity vocabulary across plan generations."""

    if not activity_type or not allowed_activity_types:
        return True
    actual = activity_type.lower()
    allowed = {item.lower() for item in allowed_activity_types}
    if actual in allowed:
        return True
    # V12 authoritative plans persisted generic POI rows as ``activity``;
    # the public autonomy envelope calls the same reversible class ``visit``.
    return {actual, "activity", "visit"} <= {"activity", "visit"} and bool(
        allowed.intersection({"activity", "visit"})
    )


def policy_from_model(row: TravelerAgentPolicy) -> DelegationPolicy:
    return DelegationPolicy(
        mode=AgentMode(str(row.mode)),
        auto_accept_replan=bool(row.auto_accept_replan),
        max_auto_replan_cost_delta_minor=int(row.max_auto_replan_cost_delta_minor),
        max_auto_loss_minor=int(row.max_auto_loss_minor),
        max_auto_schedule_drift_min=int(row.max_auto_schedule_drift_min),
        allowed_activity_types=_json_list(row.allowed_activity_types_json),
        allowed_booking_types=_json_list(row.allowed_booking_types_json),
        protected_commitment_levels=_json_list(
            row.protected_commitment_levels_json,
            ("C3_Guaranteed", "C4_Anchored"),
        ),
        allow_auto_commitment_management=bool(row.allow_auto_commitment_management),
        allow_auto_when_provider_degraded=bool(row.allow_auto_when_provider_degraded),
        allow_auto_on_critical_event=bool(row.allow_auto_on_critical_event),
        version=int(row.version),
    )


def policy_payload(row: TravelerAgentPolicy) -> dict[str, Any]:
    policy = policy_from_model(row)
    return {
        "traveler_id": row.traveler_id,
        "mode": policy.mode.value,
        "auto_accept_replan": policy.auto_accept_replan,
        "max_auto_replan_cost_delta_minor": policy.max_auto_replan_cost_delta_minor,
        "max_auto_loss_minor": policy.max_auto_loss_minor,
        "max_auto_schedule_drift_min": policy.max_auto_schedule_drift_min,
        "allowed_activity_types": list(policy.allowed_activity_types),
        "allowed_booking_types": list(policy.allowed_booking_types),
        "protected_commitment_levels": list(policy.protected_commitment_levels),
        "allow_auto_commitment_management": policy.allow_auto_commitment_management,
        "allow_auto_when_provider_degraded": policy.allow_auto_when_provider_degraded,
        "allow_auto_on_critical_event": policy.allow_auto_on_critical_event,
        "version": policy.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_or_create_policy(session: Session, traveler_id: str) -> TravelerAgentPolicy:
    row = session.get(TravelerAgentPolicy, str(traveler_id))
    if row is not None:
        return row
    now = _now()
    row = TravelerAgentPolicy(
        traveler_id=str(traveler_id),
        mode=AgentMode.ADVISORY.value,
        auto_accept_replan=False,
        max_auto_replan_cost_delta_minor=0,
        max_auto_loss_minor=0,
        max_auto_schedule_drift_min=0,
        allowed_activity_types_json="[]",
        allowed_booking_types_json="[]",
        protected_commitment_levels_json=json.dumps(["C3_Guaranteed", "C4_Anchored"]),
        allow_auto_commitment_management=False,
        allow_auto_when_provider_degraded=False,
        allow_auto_on_critical_event=False,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def update_policy(
    session: Session,
    traveler_id: str,
    *,
    mode: AgentMode,
    auto_accept_replan: bool,
    max_auto_replan_cost_delta_minor: int,
    max_auto_loss_minor: int,
    max_auto_schedule_drift_min: int,
    allowed_activity_types: list[str],
    allowed_booking_types: list[str],
    protected_commitment_levels: list[str],
    allow_auto_commitment_management: bool,
    allow_auto_when_provider_degraded: bool,
    allow_auto_on_critical_event: bool,
) -> TravelerAgentPolicy:
    row = get_or_create_policy(session, traveler_id)
    row.mode = mode.value
    row.auto_accept_replan = bool(auto_accept_replan)
    row.max_auto_replan_cost_delta_minor = int(max_auto_replan_cost_delta_minor)
    row.max_auto_loss_minor = int(max_auto_loss_minor)
    row.max_auto_schedule_drift_min = int(max_auto_schedule_drift_min)
    row.allowed_activity_types_json = json.dumps(
        list(
            dict.fromkeys(item.strip().lower() for item in allowed_activity_types if item.strip())
        ),
        sort_keys=True,
    )
    row.allowed_booking_types_json = json.dumps(
        list(dict.fromkeys(item.strip().lower() for item in allowed_booking_types if item.strip())),
        sort_keys=True,
    )
    row.protected_commitment_levels_json = json.dumps(
        list(dict.fromkeys(item.strip() for item in protected_commitment_levels if item.strip())),
        sort_keys=True,
    )
    row.allow_auto_commitment_management = bool(allow_auto_commitment_management)
    row.allow_auto_when_provider_degraded = bool(allow_auto_when_provider_degraded)
    row.allow_auto_on_critical_event = bool(allow_auto_on_critical_event)
    row.version = int(row.version) + 1
    row.updated_at = _now()
    session.flush()
    return row


def evaluate_action(
    policy: DelegationPolicy,
    action: AgentAction,
    facts: AuthoritativeActionFacts,
) -> AuthorizationResult:
    if action in READ_ACTIONS:
        return AuthorizationResult(
            AuthorizationDecision.ALLOW_AUTO,
            ("READ_ONLY_TOOL",),
            policy.version,
        )

    if action == AgentAction.REQUEST_REPLAN:
        return AuthorizationResult(
            AuthorizationDecision.ALLOW_AUTO,
            ("PROPOSAL_ONLY", "DOMAIN_TRUTH_NOT_ACTIVATED"),
            policy.version,
        )

    if action in TYPED_MUTATION_ACTIONS:
        if facts.hard_constraints_passed is not True:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("HARD_CONSTRAINTS_NOT_VERIFIED",),
                policy.version,
            )
        if facts.c4_affected or facts.locked_commitments_preserved is False:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("PROTECTED_COMMITMENT_MANAGEMENT_REQUIRES_STEP_UP",),
                policy.version,
            )
        if policy.mode == AgentMode.ADVISORY or not policy.auto_accept_replan:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("USER_CONFIRMATION_REQUIRED",),
                policy.version,
            )
        if facts.cost_delta_minor > policy.max_auto_replan_cost_delta_minor:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_COST_LIMIT_EXCEEDED",),
                policy.version,
            )
        if facts.loss_delta_minor > policy.max_auto_loss_minor:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_LOSS_LIMIT_EXCEEDED",),
                policy.version,
            )
        if not facts.action_reversible:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("ACTION_NOT_REVERSIBLE",),
                policy.version,
            )
        if facts.schedule_drift_min > policy.max_auto_schedule_drift_min:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_SCHEDULE_DRIFT_LIMIT_EXCEEDED",),
                policy.version,
            )
        if (
            facts.affected_activity_type
            and policy.allowed_activity_types
            and not _activity_type_within_envelope(
                facts.affected_activity_type, policy.allowed_activity_types
            )
        ):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("ACTIVITY_TYPE_OUTSIDE_AUTONOMY_ENVELOPE",),
                policy.version,
            )
        if (
            action in {AgentAction.CREATE_BOOKING, AgentAction.CANCEL_REFUNDABLE_BOOKING}
            and facts.affected_booking_type
            and policy.allowed_booking_types
            and facts.affected_booking_type.lower() not in policy.allowed_booking_types
        ):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("BOOKING_TYPE_OUTSIDE_AUTONOMY_ENVELOPE",),
                policy.version,
            )
        if facts.commitment_management_requested and not policy.allow_auto_commitment_management:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("COMMITMENT_MANAGEMENT_NOT_PREAUTHORIZED",),
                policy.version,
            )
        if facts.provider_degraded and not policy.allow_auto_when_provider_degraded:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("PROVIDER_EVIDENCE_DEGRADED",),
                policy.version,
            )
        if action == AgentAction.REQUEST_COMMITMENT_CHANGE:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("COMMITMENT_CHANGE_REQUEST_REQUIRES_STEP_UP",),
                policy.version,
            )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW_AUTO,
            ("DELEGATION_POLICY_MATCHED", "DETERMINISTIC_GATE_VERIFIED"),
            policy.version,
        )

    if action == AgentAction.ACCEPT_REPLAN:
        if facts.hard_constraints_passed is not True:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("HARD_CONSTRAINTS_NOT_VERIFIED",),
                policy.version,
            )
        if facts.locked_commitments_preserved is not True or facts.c4_affected:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("C4_OR_LOCKED_COMMITMENT_NOT_PRESERVED",),
                policy.version,
            )
        if not facts.proposal_presented:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("PROPOSAL_NOT_PRESENTED",),
                policy.version,
            )
        if not facts.base_plan_current:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("BASE_PLAN_NOT_CURRENT",),
                policy.version,
            )
        if not facts.trigger_actionable:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("TRIGGER_EVENT_NOT_ACTIONABLE",),
                policy.version,
            )
        if not facts.impact_target_server_validated:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("IMPACT_TARGET_NOT_SERVER_VALIDATED",),
                policy.version,
            )
        if facts.impact_maturity not in {
            "AI_ONLY",
            "STRUCTURALLY_ELIGIBLE",
            "CORROBORATED",
            "VALIDATED_IMPACT",
        }:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("IMPACT_MATURITY_UNAVAILABLE",),
                policy.version,
            )
        if policy.mode == AgentMode.ADVISORY or not policy.auto_accept_replan:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("USER_CONFIRMATION_REQUIRED",),
                policy.version,
            )
        if facts.cost_delta_minor > policy.max_auto_replan_cost_delta_minor:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_COST_LIMIT_EXCEEDED",),
                policy.version,
            )
        if facts.loss_delta_minor > policy.max_auto_loss_minor:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_LOSS_LIMIT_EXCEEDED",),
                policy.version,
            )
        if facts.schedule_drift_min > policy.max_auto_schedule_drift_min:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_SCHEDULE_DRIFT_LIMIT_EXCEEDED",),
                policy.version,
            )
        if (
            facts.affected_activity_type
            and policy.allowed_activity_types
            and not _activity_type_within_envelope(
                facts.affected_activity_type, policy.allowed_activity_types
            )
        ):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("ACTIVITY_TYPE_OUTSIDE_AUTONOMY_ENVELOPE",),
                policy.version,
            )
        if (
            action in {AgentAction.CREATE_BOOKING, AgentAction.CANCEL_REFUNDABLE_BOOKING}
            and facts.affected_booking_type
            and policy.allowed_booking_types
            and facts.affected_booking_type.lower() not in policy.allowed_booking_types
        ):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("BOOKING_TYPE_OUTSIDE_AUTONOMY_ENVELOPE",),
                policy.version,
            )
        if facts.commitment_management_requested:
            if not policy.allow_auto_commitment_management:
                return AuthorizationResult(
                    AuthorizationDecision.REQUIRE_STEP_UP,
                    ("COMMITMENT_MANAGEMENT_NOT_PREAUTHORIZED",),
                    policy.version,
                )
            if any(
                level in policy.protected_commitment_levels for level in facts.commitment_levels
            ):
                return AuthorizationResult(
                    AuthorizationDecision.REQUIRE_STEP_UP,
                    ("PROTECTED_COMMITMENT_MANAGEMENT_REQUIRES_STEP_UP",),
                    policy.version,
                )
        if facts.provider_degraded and not policy.allow_auto_when_provider_degraded:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("PROVIDER_EVIDENCE_DEGRADED",),
                policy.version,
            )
        if (
            str(facts.event_severity or "").upper() == "CRITICAL"
            and not policy.allow_auto_on_critical_event
        ):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("CRITICAL_EVENT_REQUIRES_CONFIRMATION",),
                policy.version,
            )
        if facts.impact_maturity == "AI_ONLY":
            return AuthorizationResult(
                AuthorizationDecision.ALLOW_AUTO,
                (
                    "AI_SEMANTIC_JUDGMENT_WITHIN_ENVELOPE",
                    "DETERMINISTIC_GATE_VERIFIED",
                ),
                policy.version,
            )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW_AUTO,
            ("DELEGATION_POLICY_MATCHED", "DETERMINISTIC_GATE_VERIFIED"),
            policy.version,
        )

    if action == AgentAction.MODIFY_C4:
        if policy.mode == AgentMode.ADVISORY:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("ADVISORY_MODE_REQUIRES_STEP_UP",),
                policy.version,
            )
        if policy.mode == AgentMode.ASSISTED:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("ASSISTED_COMMITMENT_MANAGEMENT_REQUIRES_STEP_UP",),
                policy.version,
            )
        if not policy.allow_auto_commitment_management:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_STEP_UP,
                ("COMMITMENT_MANAGEMENT_NOT_PREAUTHORIZED",),
                policy.version,
            )
        if facts.hard_constraints_passed is not True:
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                ("COMMITMENT_MANAGEMENT_FACTS_NOT_VERIFIED",),
                policy.version,
            )
        if facts.provider_degraded:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("PROVIDER_EVIDENCE_DEGRADED",),
                policy.version,
            )
        if facts.loss_delta_minor > policy.max_auto_loss_minor:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                ("AUTO_LOSS_LIMIT_EXCEEDED",),
                policy.version,
            )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW_AUTO,
            ("COMMITMENT_MANAGEMENT_PREAUTHORIZED", "DETERMINISTIC_GATE_VERIFIED"),
            policy.version,
        )

    if action in {AgentAction.PAYMENT, AgentAction.REFUND}:
        return AuthorizationResult(
            AuthorizationDecision.REQUIRE_STEP_UP,
            ("PROTECTED_ACTION_REQUIRES_STEP_UP",),
            policy.version,
        )

    return AuthorizationResult(
        AuthorizationDecision.DENY,
        ("ACTION_NOT_ALLOWLISTED",),
        policy.version,
    )


def default_policy_payload(traveler_id: UUID | str) -> dict[str, Any]:
    """Return the conservative policy without allowing an agent to mutate it."""
    return {
        "traveler_id": str(traveler_id),
        "mode": AgentMode.ADVISORY.value,
        "auto_accept_replan": False,
        "max_auto_replan_cost_delta_minor": 0,
        "max_auto_loss_minor": 0,
        "max_auto_schedule_drift_min": 0,
        "allowed_activity_types": [],
        "allowed_booking_types": [],
        "protected_commitment_levels": ["C3_Guaranteed", "C4_Anchored"],
        "allow_auto_commitment_management": False,
        "allow_auto_when_provider_degraded": False,
        "allow_auto_on_critical_event": False,
        "version": 1,
    }
