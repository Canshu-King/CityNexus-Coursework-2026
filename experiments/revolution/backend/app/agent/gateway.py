from __future__ import annotations

import hashlib
import json
import re
from statistics import median
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.approval import (
    approval_fingerprint,
    approval_payload,
    consume_action_approval,
    create_or_reuse_approval,
)
from app.agent.contracts import (
    AgentAction,
    AuthoritativeActionFacts,
    AuthorizationDecision,
    AuthorizationResult,
)
from app.agent.delegation_policy import (
    evaluate_action,
    get_or_create_policy,
    policy_from_model,
)
from app.agent.tool_catalog import RUNTIME_MCP_ALLOWLIST_V2, tool_catalog, tool_catalog_v2
from app.commerce.idempotency import canonical_request_signature
from app.commerce.service_impl import CommerceConflict, CommerceValidationError
from app.core.audit import record_audit
from app.fact_acquisition import acquire_place_facts
from app.db.models import (
    Activity,
    AgentRun,
    Booking,
    CityService,
    Commitment,
    Event,
    IdempotencyKey,
    Journey,
    JourneyMission,
    PaymentTransaction,
    Place,
    RecoveryDiscoveryAttempt,
    PlanVersion,
    ReplanProposal,
    WebObservation,
)
from app.recovery_epoch import (
    RecoveryEpochSuperseded,
    ensure_recovery_epoch,
    semantic_plan_identity,
    validate_recovery_epoch_binding,
)
from app.schemas import AgentToolCall, ReplanGenerateRequest
from app.services import (
    _place_verified_cost,
    _place_window,
    activity_schema,
    generate_plans as compose_plans,
    journey_schema,
    plan_schema,
    service_schema,
    validate_plan_feasibility,
)
from app.replanning.provider_snapshots import MobilitySnapshot


_MEDIATION_ACTIVE_REALITY_MUTATIONS = {
    "accept_replan",
    "activate_standby",
    "replace_activity",
    "reschedule_activity",
    "create_booking",
    "cancel_refundable_booking",
    "request_commitment_change",
    "apply_clarified_horizon_choice",
}


def _mission_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_disagreement_mediator_mission(mission: JourneyMission | None) -> bool:
    if mission is None:
        return False
    mediation = _mission_json(mission.resolved_intent_json).get("collective_mediation")
    return (
        isinstance(mediation, dict)
        and mediation.get("kind") == "DISAGREEMENT_MEDIATION"
    )


def _journey_has_unresolved_collective_mediation(
    session: Session,
    journey_id: str,
) -> bool:
    """Compatibility wrapper around the shared Journey social-authority lock."""
    from app.collective_mediation import journey_has_unresolved_collective_mediation

    return journey_has_unresolved_collective_mediation(session, journey_id)


def _agent_key(kind: str, *parts: object) -> str:
    material = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"agent.{kind}:{digest}"


def _read_idempotent(session: Session, key: str, signature: str) -> dict[str, Any] | None:
    row = session.get(IdempotencyKey, key)
    if row is None:
        return None
    if row.request_hash != signature:
        raise HTTPException(409, "same agent request id used for a different request")
    stored = json.loads(row.response_json or "{}")
    if not stored:
        raise HTTPException(409, "agent idempotency record is incomplete")
    return stored


def _write_idempotent(
    session: Session,
    *,
    key: str,
    operation: str,
    signature: str,
    response: dict[str, Any],
) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    session.add(
        IdempotencyKey(
            key=key,
            operation=operation,
            request_hash=signature,
            response_status=200,
            response_json=json.dumps(response, sort_keys=True, default=str),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=7)).isoformat(),
        )
    )


_CURRENT_REPLAN_PROPOSAL_FIELDS = frozenset({
    "id", "journey_id", "trigger_event_id", "base_plan_version_id",
    "proposed_plan_version_id", "status", "hard_constraints_passed",
    "locked_commitments_preserved", "utility_score", "score_breakdown",
    "diff", "explanation",
})
_LEGACY_REPLAN_PROPOSAL_FIELDS = frozenset({
    "id", "status", "hard_constraints_passed", "locked_commitments_preserved",
})
_REPLAN_CACHE_UNAVAILABLE = "AGENT_IDEMPOTENCY_RESPONSE_SCHEMA_UNAVAILABLE"


def _actionable_events(session: Session, journey_id: str) -> list[Event]:
    """Return all open events in deterministic display order.

    Recency is useful for presentation, but it is not a semantic priority rule.
    Callers must therefore keep the complete compact choice set whenever more
    than one event is open.
    """

    return list(
        session.scalars(
            select(Event)
            .where(
                Event.journey_id == journey_id,
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
            .order_by(Event.detected_at.desc(), Event.id.desc())
        ).all()
    )


def _event_choice(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "type": event.event_type,
        "severity": event.severity,
        "affected_scope": event.affected_activity_id,
        "provenance": event.provenance,
    }


def _resolve_actionable_event(
    session: Session, journey_id: str, event_id: str | None = None
) -> tuple[Event | None, list[Event], str | None]:
    events = _actionable_events(session, journey_id)
    if event_id:
        selected = next((item for item in events if item.id == str(event_id)), None)
        if selected is None:
            return None, events, "EVENT_NOT_ACTIONABLE"
        return selected, events, None
    if len(events) == 1:
        return events[0], events, None
    if len(events) > 1:
        return None, events, "EVENT_SELECTION_REQUIRED"
    return None, events, None


def _latest_actionable_event(
    session: Session, journey_id: str, event_id: str | None = None
) -> Event | None:
    """Resolve only an explicit or unambiguous event; never guess on ambiguity."""

    selected, _events, _error = _resolve_actionable_event(session, journey_id, event_id)
    return selected

_CITY_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "博物馆": ("museum",),
    "美术馆": ("gallery", "museum"),
    "图书馆": ("library",),
    "文化": ("culture", "museum", "library", "theatre"),
    "室内": ("indoor", "museum", "library", "theatre"),
    "历史": ("historic", "attraction", "museum"),
    # English category words are common model-generated search queries.  They
    # must expand in the same open-world search path as the Chinese aliases;
    # otherwise q="culture" is applied literally before category="culture"
    # can expose museum/gallery/heritage Places.
    "culture": ("museum", "gallery", "library", "theatre", "heritage", "attraction"),
    "cultural": ("museum", "gallery", "library", "theatre", "heritage", "attraction"),
    "indoor": ("museum", "gallery", "library", "theatre", "attraction"),
    "outdoor": ("park", "garden", "viewpoint", "heritage", "attraction"),
}
_CITY_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "culture": ("museum", "gallery", "library", "theatre", "heritage", "attraction"),
    "cultural": ("museum", "gallery", "library", "theatre", "heritage", "attraction"),
    "indoor": ("museum", "gallery", "library", "theatre", "attraction"),
    "outdoor": ("park", "garden", "viewpoint", "heritage", "attraction"),
    # "experience" is the bounded open-world category used by the opportunity
    # mission.  Treat it as a discovery umbrella, not a literal Place
    # category, so the Agent can inspect real provider-backed alternatives.
    "experience": (
        "museum", "gallery", "library", "theatre", "heritage", "attraction",
        "park", "garden", "viewpoint", "restaurant", "cafe",
    ),
}


def _city_search_terms(query: str | None) -> tuple[str, ...]:
    if not query:
        return ()
    normalized = query.casefold().strip()
    terms: list[str] = [normalized]
    terms.extend(token for token in re.findall(r"[\w\u4e00-\u9fff]+", normalized) if len(token) > 1)
    for phrase, aliases in _CITY_SEARCH_ALIASES.items():
        if phrase in normalized:
            terms.extend(aliases)
    return tuple(dict.fromkeys(term for term in terms if term))


def _city_category_terms(category: str | None) -> tuple[str, ...]:
    if not category:
        return ()
    normalized = category.casefold().strip()
    return tuple(dict.fromkeys((normalized, *_CITY_CATEGORY_ALIASES.get(normalized, ()))))


def _agent_place_metadata(place: Place) -> dict[str, Any]:
    """Project bounded fact metadata for the Agent search surface.

    Canonical Place metadata may contain captured article text and provider response
    bodies.  Those are useful evidence at the fact-acquisition boundary, but sending
    them with every search result starves the model of context for the next strategy
    decision.  Keep statuses, amounts, identity bindings, and source candidates; the
    full capture remains persisted on the Place and is returned by acquisition receipts.
    """

    raw = json.loads(place.metadata_json or "{}") if place.metadata_json else {}
    if not isinstance(raw, dict):
        return {}
    compact: dict[str, Any] = {
        key: raw[key]
        for key in ("discovery_mode", "osm_id", "duration_min", "price_known", "place_role")
        if key in raw
    }
    strategy_estimate = raw.get("strategy_estimate")
    if isinstance(strategy_estimate, dict):
        compact["strategy_estimate"] = strategy_estimate
    cost = raw.get("cost_evidence")
    if isinstance(cost, dict):
        compact["cost_evidence"] = {
            key: cost[key]
            for key in ("status", "amount_minor", "currency", "source", "source_url")
            if key in cost
        }
    binding = raw.get("fact_source_binding")
    if isinstance(binding, dict):
        compact["fact_source_binding"] = {
            key: binding[key]
            for key in ("provider_entity", "source_url", "source_class", "approval")
            if key in binding
        }
    acquisition = raw.get("fact_acquisition")
    if isinstance(acquisition, dict):
        official = acquisition.get("official_web")
        summary: dict[str, Any] = {"status": acquisition.get("status")}
        if isinstance(official, dict):
            summary["official_web"] = {
                key: official[key]
                for key in ("status", "source", "source_url", "provider")
                if key in official
            }
        compact["fact_acquisition"] = summary
    provider_tags = raw.get("tags")
    if isinstance(provider_tags, dict):
        candidate_urls = {
            key: provider_tags[key]
            for key in ("website", "contact:website", "url")
            if provider_tags.get(key)
        }
        if candidate_urls:
            compact["source_candidates"] = candidate_urls
    return compact


def _upgrade_cached_request_replan_response(
    session: Session,
    stored: dict[str, Any],
    *,
    journey_id: str,
    trigger_event_id: str,
) -> dict[str, Any]:
    """Project only persisted legacy proposals while keeping frozen response facts."""
    proposals = stored.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)

    upgraded: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    changed = False
    for cached in proposals:
        if not isinstance(cached, dict):
            raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
        proposal_id = cached.get("id")
        if not isinstance(proposal_id, str) or not proposal_id.strip() or proposal_id in seen_ids:
            raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
        seen_ids.add(proposal_id)
        if _CURRENT_REPLAN_PROPOSAL_FIELDS.issubset(cached):
            if (
                cached["journey_id"] != journey_id
                or cached["trigger_event_id"] != trigger_event_id
                or not isinstance(cached["status"], str)
                or not isinstance(cached["hard_constraints_passed"], bool)
                or not isinstance(cached["locked_commitments_preserved"], bool)
                or isinstance(cached["utility_score"], bool)
                or not isinstance(cached["utility_score"], (int, float))
                or not isinstance(cached["score_breakdown"], dict)
                or not isinstance(cached["diff"], dict)
                or not isinstance(cached["explanation"], dict)
            ):
                raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
            upgraded.append(cached)
            continue
        if set(cached) != _LEGACY_REPLAN_PROPOSAL_FIELDS:
            raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
        if (
            not isinstance(cached["status"], str)
            or not isinstance(cached["hard_constraints_passed"], bool)
            or not isinstance(cached["locked_commitments_preserved"], bool)
        ):
            raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
        row = session.get(ReplanProposal, proposal_id)
        if (row is None or row.journey_id != journey_id
                or row.trigger_event_id != trigger_event_id):
            raise HTTPException(409, _REPLAN_CACHE_UNAVAILABLE)
        from app.main import _replan_payload

        current = _replan_payload(row)
        for field in ("status", "hard_constraints_passed", "locked_commitments_preserved"):
            current[field] = cached[field]
        upgraded.append(current)
        changed = True
    return {**stored, "proposals": upgraded} if changed else stored


def _json(value: object) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _require_journey(session: Session, journey_id: UUID | None) -> Journey:
    if journey_id is None:
        raise HTTPException(422, "journey_id is required for this Agent tool")
    journey = session.get(Journey, str(journey_id))
    if journey is None:
        raise HTTPException(404, f"Journey {journey_id} was not found")
    return journey


def _facts_for_accept(
    session: Session,
    proposal: ReplanProposal,
    journey: Journey,
) -> tuple[AuthoritativeActionFacts, dict[str, Any]]:
    try:
        diff = json.loads(proposal.diff_json or "{}")
    except json.JSONDecodeError:
        diff = {}
    verified = diff.get("verified_evidence", {})
    envelope = diff.get("after_feasibility_envelope", {})
    mobility = verified.get("mobility", {})
    impact_resolution = verified.get("impact_resolution", {})
    trigger = session.get(Event, proposal.trigger_event_id) if proposal.trigger_event_id else None
    preserved = bool(proposal.locked_commitments_preserved)
    preserved = preserved and diff.get("preserved_protected", True) is not False
    preserved = preserved and verified.get("commitment_preservation", {}).get("preserved", True)
    degraded_providers = envelope.get("degraded_providers") or []
    provider_degraded = bool(
        degraded_providers
        or mobility.get("degraded") is True
        or mobility.get("snapshot", {}).get("degraded") is True
    )
    base_current = journey.active_plan_version_id == proposal.base_plan_version_id
    trigger_actionable = trigger is not None and trigger.status in {"DETECTED", "ACKNOWLEDGED"}
    selected_activity_id = impact_resolution.get("selected_activity_id")
    selected_activity = (
        session.get(Activity, str(selected_activity_id)) if selected_activity_id else None
    )
    # Provider-verified incidents may carry an affected Activity directly even
    # when the semantic impact resolver did not produce an independent target.
    # The server still verifies ownership, active-plan membership and state;
    # the model's semantic judgement is allowed to remain AI_ONLY inside the
    # user's autonomy envelope.
    if selected_activity is None and trigger is not None and trigger.affected_activity_id:
        candidate = session.get(Activity, trigger.affected_activity_id)
        if (
            candidate is not None
            and candidate.plan_version_id == proposal.base_plan_version_id
            and candidate.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
        ):
            selected_activity = candidate
            selected_activity_id = candidate.id
    if selected_activity is None:
        # A semantic model judgement may identify a removable activity only in
        # the candidate diff. The server still owns the final target check:
        # resolve the first removed base-plan activity and verify its state and
        # ownership before applying the autonomy envelope.
        removed_ids = diff.get("removed") or []
        if isinstance(removed_ids, list):
            for raw_id in removed_ids:
                candidate = session.get(Activity, str(raw_id))
                if candidate is None:
                    candidate = session.scalar(
                        select(Activity).where(
                            Activity.plan_version_id == proposal.base_plan_version_id,
                            Activity.place_id == str(raw_id),
                            Activity.status.not_in(("COMPLETED", "SKIPPED", "CANCELLED")),
                        ).order_by(Activity.sequence_no).limit(1)
                    )
                if (
                    candidate is not None
                    and candidate.plan_version_id == proposal.base_plan_version_id
                    and candidate.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
                ):
                    selected_activity = candidate
                    selected_activity_id = candidate.id
                    break
    impact_target_server_validated = bool(
        selected_activity is not None
        and selected_activity.plan_version_id == proposal.base_plan_version_id
        and selected_activity.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
    )
    impact_maturity_override: str | None = None
    if (
        not impact_target_server_validated
        and trigger is not None
        and trigger.event_type == "OPPORTUNITY_WINDOW"
        and trigger.provenance == "SYSTEM_DERIVED"
        and diff.get("candidate_family") == "F4_OPPORTUNITY_INSERTION"
    ):
        # An additive opportunity has no disrupted Activity, so the generic
        # removed/affected-activity resolver cannot identify its target.  The
        # server-derived signal carries a bounded current/next scope instead;
        # revalidate that scope and the proposal's own evidence before allowing
        # the normal autonomy policy to decide whether acceptance is automatic.
        try:
            signal = json.loads(trigger.payload_json or "{}")
        except json.JSONDecodeError:
            signal = {}
        current_id = str(signal.get("current_execution_activity_id") or "")
        next_id = str(signal.get("next_activity_id") or "")
        current = session.get(Activity, current_id) if current_id else None
        next_activity = session.get(Activity, next_id) if next_id else None
        opportunity = (verified.get("opportunity") or {}).get("evidence") or {}
        incident = verified.get("incident_recovery_evidence") or {}
        incident_mobility = incident.get("mobility_snapshot") or {}
        added = {str(item) for item in (diff.get("added") or [])}
        candidate_place_id = str(opportunity.get("candidate_place_id") or "")
        current_location = (
            current.place_id or current.city_service_id if current is not None else None
        )
        target_scope_valid = (
            signal.get("active_plan_version_id") == proposal.base_plan_version_id
            and signal.get("current_execution_activity_id") == current_id
            and signal.get("next_activity_id") == next_id
            and current is not None
            and current.plan_version_id == proposal.base_plan_version_id
            and current.status in {"ARRIVED", "IN_PROGRESS"}
            and current_location == signal.get("current_execution_location")
            and next_activity is not None
            and next_activity.plan_version_id == proposal.base_plan_version_id
            and next_activity.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
            and opportunity.get("fits") is True
            and candidate_place_id in added
            and incident.get("event_id") == trigger.id
            and incident.get("event_type") == "OPPORTUNITY_WINDOW"
            and incident_mobility.get("snapshot_id")
            and incident_mobility.get("degraded") is False
        )
        if target_scope_valid:
            selected_activity = current
            selected_activity_id = current.id
            impact_target_server_validated = True
            impact_maturity_override = "VALIDATED_IMPACT"
    try:
        schedule_drift_min = abs(int(diff.get("end_time_delta_min") or 0))
    except (TypeError, ValueError):
        schedule_drift_min = 0
    try:
        loss_delta_minor = max(0, int(diff.get("loss_delta_minor") or 0))
    except (TypeError, ValueError):
        loss_delta_minor = 0
    facts = AuthoritativeActionFacts(
        hard_constraints_passed=bool(proposal.hard_constraints_passed),
        locked_commitments_preserved=preserved,
        c4_affected=not preserved,
        cost_delta_minor=int(diff.get("cost_delta_minor") or 0),
        provider_degraded=provider_degraded,
        event_severity=trigger.severity if trigger is not None else None,
        proposal_presented=proposal.status == "PRESENTED",
        base_plan_current=base_current,
        trigger_actionable=trigger_actionable,
        impact_maturity=(
            impact_resolution.get("maturity_stage")
            or impact_maturity_override
            or ("AI_ONLY" if impact_target_server_validated else None)
        ),
        impact_target_server_validated=impact_target_server_validated,
        loss_delta_minor=loss_delta_minor,
        schedule_drift_min=schedule_drift_min,
        affected_activity_type=selected_activity.activity_type if selected_activity else None,
        commitment_management_requested=bool(diff.get("commitment_management_requested")),
        commitment_levels=tuple(
            str(item)
            for item in verified.get("commitment_preservation", {}).get("protected_levels", [])
            if item
        ),
    )
    evidence = {
        "hard_constraints_passed": facts.hard_constraints_passed,
        "locked_commitments_preserved": facts.locked_commitments_preserved,
        "c4_affected": facts.c4_affected,
        "cost_delta_minor": facts.cost_delta_minor,
        "provider_degraded": facts.provider_degraded,
        "event_severity": facts.event_severity,
        "proposal_presented": facts.proposal_presented,
        "base_plan_current": facts.base_plan_current,
        "trigger_actionable": facts.trigger_actionable,
        "impact_maturity": facts.impact_maturity,
        "impact_target_server_validated": facts.impact_target_server_validated,
        "loss_delta_minor": facts.loss_delta_minor,
        "schedule_drift_min": facts.schedule_drift_min,
        "affected_activity_type": facts.affected_activity_type,
        "commitment_management_requested": facts.commitment_management_requested,
        "commitment_levels": list(facts.commitment_levels),
    }
    return facts, evidence


class AgentToolGateway:
    """The only server entry point for the V1 Agent tool surface.

    The gateway accepts typed intent and resolves every policy and feasibility
    fact from CityNexus persistence. It never accepts a client supplied gate
    result, provider state, cost delta, or C4 assertion.
    """

    def __init__(self, session: Session, *, bound_run_id: str | None = None):
        self.session = session
        # Internal model runtimes inject the exact durable AgentRun that owns
        # this Gateway turn.  Direct/MCP callers may omit it, but the server
        # must never infer an internal run by "latest mission run" ordering.
        self.bound_run_id = str(bound_run_id) if bound_run_id else None

    def _native_plan_boundary(self, request: AgentToolCall) -> bool:
        """Keep native MCP runs proposal-only at the CityNexus boundary.

        Hermes receives the same V2 surface as other agents, but a native MCP
        caller cannot present a user-bound approval receipt merely by choosing
        a tool argument.  Treat the MCP transport identity and an explicit
        PLAN_ONLY mission as confirmation-required even when a traveler policy
        would otherwise allow automatic mutation.
        """

        actor = str(request.agent_id or "").strip().lower()
        if actor in {"mcp-client", "hermes", "hermes-native"}:
            return True
        if request.mission_id is not None:
            mission = self.session.get(JourneyMission, str(request.mission_id))
            return mission is not None and mission.requested_autonomy == "PLAN_ONLY"
        return False

    def _bound_authorization(
        self, request: AgentToolCall, auth: AuthorizationResult
    ) -> AuthorizationResult:
        if self._native_plan_boundary(request) and auth.decision == AuthorizationDecision.ALLOW_AUTO:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_CONFIRMATION,
                auth.reason_codes + ("CITYNEXUS_NATIVE_PLAN_ONLY_BOUNDARY",),
                auth.policy_version,
            )
        return auth


    def _bound_recovery_epoch_id(
        self, request: AgentToolCall, journey: Journey
    ) -> str | None:
        """Resolve recovery causality from server context, never mission recency."""

        if self.bound_run_id is not None:
            bound_run = self.session.get(AgentRun, self.bound_run_id)
            if (
                bound_run is None
                or bound_run.journey_id != journey.id
                or (
                    request.mission_id is not None
                    and bound_run.mission_id != str(request.mission_id)
                )
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "AGENT_RUN_RECOVERY_BINDING_INVALID",
                        "bound_run_id": self.bound_run_id,
                    },
                )
            if not bound_run.recovery_epoch_id:
                raise HTTPException(
                    409,
                    detail={
                        "code": "RECOVERY_EPOCH_UNAVAILABLE",
                        "bound_run_id": bound_run.id,
                    },
                )
            try:
                validate_recovery_epoch_binding(
                    self.session,
                    journey.id,
                    bound_run.recovery_epoch_id,
                )
            except RecoveryEpochSuperseded as exc:
                raise HTTPException(409, detail=exc.facts)
            return str(bound_run.recovery_epoch_id)

        if request.mission_id is not None:
            # Native/legacy transport has no local AgentRun row. Bind it to the
            # server's current generation rather than guessing another run.
            return str(ensure_recovery_epoch(self.session, journey).id)
        return None

    @staticmethod
    def _r61_reground_context(mission: JourneyMission | None) -> dict[str, Any] | None:
        if mission is None:
            return None
        try:
            objectives = json.loads(mission.objective_codes_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            objectives = []
        context = _mission_json(mission.resolved_intent_json).get("group_recovery")
        if (
            "COLLECTIVE_RECOVERY_REGROUND" not in objectives
            or not isinstance(context, dict)
            or context.get("kind") != "R61_REGROUNDED_COLLECTIVE_RECOVERY"
        ):
            return None
        return context

    def _r61_current_scope(
        self,
        journey: Journey,
        mission: JourneyMission,
        *,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], Event, list[ReplanProposal]]:
        """Resolve only the current incident/epoch proposal surface for R61."""
        context = self._r61_reground_context(mission)
        if context is None or mission.requested_autonomy != "PLAN_ONLY":
            raise HTTPException(409, "R61_REGROUND_MISSION_BINDING_INVALID")
        if mission.status not in {"CREATED", "RUNNING"}:
            raise HTTPException(409, "R61_REGROUND_MISSION_NOT_ACTIVE")
        if journey.status != "ACTIVE" or not journey.active_plan_version_id:
            raise HTTPException(409, "R61_JOURNEY_NOT_ACTIVE")
        if str(context.get("active_plan_version_id") or "") != str(journey.active_plan_version_id):
            raise HTTPException(409, "STALE_COLLECTIVE_RECOVERY_PLAN")

        expected_trigger_id = str(
            mission.trigger_event_id or context.get("trigger_event_id") or ""
        )
        if not expected_trigger_id or (
            trigger_event_id is not None and str(trigger_event_id) != expected_trigger_id
        ):
            raise HTTPException(409, "R61_TRIGGER_SCOPE_MISMATCH")
        trigger = self.session.get(Event, expected_trigger_id)
        if (
            trigger is None
            or trigger.journey_id != journey.id
            or trigger.status not in {"DETECTED", "ACKNOWLEDGED"}
        ):
            raise HTTPException(409, "R61_INCIDENT_NOT_ACTIONABLE")

        epoch_id = str(context.get("recovery_epoch_id") or "")
        try:
            epoch = validate_recovery_epoch_binding(self.session, journey.id, epoch_id)
        except RecoveryEpochSuperseded as exc:
            raise HTTPException(409, detail=exc.facts) from exc
        bound_generation = context.get("recovery_epoch_generation")
        if bound_generation is not None:
            try:
                generation_matches = int(bound_generation) == int(epoch.generation)
            except (TypeError, ValueError, OverflowError):
                generation_matches = False
            if not generation_matches:
                raise HTTPException(409, "RECOVERY_EPOCH_GENERATION_MISMATCH")

        proposals = self.session.scalars(
            select(ReplanProposal)
            .where(
                ReplanProposal.journey_id == journey.id,
                ReplanProposal.trigger_event_id == trigger.id,
                ReplanProposal.base_plan_version_id == journey.active_plan_version_id,
                ReplanProposal.recovery_epoch_id == epoch.id,
                ReplanProposal.status == "PRESENTED",
            )
            .order_by(ReplanProposal.created_at, ReplanProposal.id)
        ).all()
        return context, trigger, proposals

    @staticmethod
    def catalog() -> list[dict[str, object]]:
        return tool_catalog()

    @staticmethod
    def catalog_v2() -> list[dict[str, object]]:
        return tool_catalog_v2()

    def dispatch(self, tool_name: str, request: AgentToolCall) -> dict[str, Any]:
        if tool_name not in RUNTIME_MCP_ALLOWLIST_V2:
            raise HTTPException(404, "Agent tool is not in the CityNexus allowlist")
        handlers = {
            "inspect_journey": self.inspect_journey,
            "inspect_twin": self.inspect_twin,
            "list_replans": self.list_replans,
            "conclude_recovery": self.conclude_recovery,
            "apply_clarified_horizon_choice": self.apply_clarified_horizon_choice,
            "search_city_services": self.search_city_services,
            "discover_recovery_options": self.discover_recovery_options,
            "research_place_sources": self.research_place_sources,
            "acquire_place_facts": self.acquire_place_facts,
            "estimate_place_facts": self.estimate_place_facts,
            "generate_plans": self.generate_plans,
            "request_replan": self.request_replan,
            "request_collective_decision": self.request_collective_decision,
            "accept_replan": self.accept_replan,
            "activate_standby": self.activate_standby,
            "replace_activity": self.replace_activity,
            "reschedule_activity": self.reschedule_activity,
            "create_booking": self.create_booking,
            "cancel_refundable_booking": self.cancel_refundable_booking,
            "request_commitment_change": self.request_commitment_change,
        }
        scope_journey_id = request.journey_id
        if scope_journey_id is None and request.proposal_id is not None:
            proposal = self.session.get(ReplanProposal, str(request.proposal_id))
            scope_journey_id = UUID(proposal.journey_id) if proposal is not None else None
        record_audit(
            self.session,
            action="agent_tool_requested",
            entity_type="AgentToolRequest",
            entity_id=request.request_id,
            journey_id=str(scope_journey_id) if scope_journey_id is not None else None,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "tool": tool_name,
                "request_id": request.request_id,
                "proposal_id": str(request.proposal_id) if request.proposal_id else None,
                "trigger_event_id": str(request.trigger_event_id)
                if request.trigger_event_id
                else None,
            },
        )
        # Persist the request even when the handler later returns a policy
        # denial or an idempotent receipt. This makes the feed an audit
        # projection rather than a React-side activity log.
        self.session.commit()

        # Explicit member disagreement is a Journey-level social-authority
        # lock, not a cooperative-model convention. Candidate generation and a
        # fresh collective round remain allowed; active reality cannot change
        # from this mediator or from a newly started Agent mission until that
        # mediator lineage reaches a server-owned RESOLVED round.
        if (
            tool_name in _MEDIATION_ACTIVE_REALITY_MUTATIONS
            and scope_journey_id is not None
            and _journey_has_unresolved_collective_mediation(
                self.session, str(scope_journey_id)
            )
        ):
            mission = (
                self.session.get(JourneyMission, str(request.mission_id))
                if request.mission_id is not None
                else None
            )
            record_audit(
                self.session,
                action="agent.collective_mediation.mutation_denied",
                entity_type="JourneyMission" if mission is not None else "Journey",
                entity_id=mission.id if mission is not None else str(scope_journey_id),
                journey_id=str(scope_journey_id),
                actor_type="AI_AGENT",
                actor_id=request.agent_id,
                after={
                    "tool": tool_name,
                    "request_id": request.request_id,
                    "reason": "FRESH_COLLECTIVE_DECISION_REQUIRED",
                    "mission_id": mission.id if mission is not None else None,
                },
            )
            self.session.commit()
            raise HTTPException(
                409,
                "MEDIATION_REQUIRES_FRESH_COLLECTIVE_DECISION",
            )
        return handlers[tool_name](request)

    def inspect_journey(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        from app.agent.journey_intent import active_journey_intents
        from app.execution.clock import execution_clock
        from app.main import active_journey_members_with_capabilities
        commitments = self.session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
            )
        ).all()
        protected = [
            {
                "id": item.id,
                "activity_id": item.activity_id,
                "kind": item.kind,
                "level": item.level,
                "protected": item.level in {"C3_Guaranteed", "C4_Anchored"},
                "external_ref": item.external_ref,
            }
            for item in commitments
        ]
        return {
            "tool": "inspect_journey",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "journey": _json(journey_schema(journey)),
            "active_journey_intents": active_journey_intents(
                self.session,
                journey,
                now=execution_clock.now(journey.timezone),
            ),
            "protected_commitments": [item for item in protected if item["protected"]],
            "collective_journey": (
                active_journey_members_with_capabilities(self.session, journey)
                if isinstance(self.session, Session)
                else {"members": [], "member_count": 0, "authority_basis": "JourneyRoleCapability"}
            ),
        }

    def inspect_twin(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        from app.main import _ensure_opportunity_window_event, _twin_snapshot
        from app.main import active_journey_members_with_capabilities
        from app.agent.predictive import standby_portfolio
        from app.execution.traveler_twin import refresh_snapshot, snapshot_payload
        # Opportunity is a server-derived signal.  Materialise it while the
        # model is inspecting the live Twin so the Agent discovers the window
        # through the same surface as every other current-world fact.
        opportunity_event = None
        if getattr(journey, "status", None) == "ACTIVE" and getattr(
            journey, "active_plan_version_id", None
        ):
            opportunity_event = _ensure_opportunity_window_event(self.session, journey)
        if opportunity_event is not None:
            self.session.commit()
        # A mission may be created before the event is attached. Expose the
        # server-owned choice set so an autonomous model can discover reality
        # through inspection. Recency is not silently treated as relevance.
        trigger, actionable_events, selection_error = _resolve_actionable_event(
            self.session, journey.id, request.trigger_event_id
        )

        savepoint = self.session.begin_nested()
        try:
            twin = _twin_snapshot(self.session, journey)
            traveler_id = getattr(journey, "traveler_id", None)
            traveler_twin = (
                snapshot_payload(refresh_snapshot(self.session, traveler_id))
                if traveler_id
                else {}
            )
        finally:
            savepoint.rollback()
        compact_twin = {
            "explicit_preferences": traveler_twin.get("explicit_preferences", []),
            "learned_soft_preferences": traveler_twin.get("learned_soft_preferences", []),
            "conflicts": traveler_twin.get("conflicts", []),
            "confidence": traveler_twin.get("confidence", 0.0),
            "evidence_count": traveler_twin.get("evidence_count", 0),
        }
        twin["traveler_twin_context"] = compact_twin
        from app.intelligence.decision_sensitivity import decision_sensitivity_receipt
        windows = twin.get("context", {}).get("remaining_opportunity_horizon", {}).get("windows", [])
        collective = (
            active_journey_members_with_capabilities(self.session, journey, candidate_windows=windows)
            if isinstance(self.session, Session)
            else {"members": [], "member_count": 0, "authority_basis": "JourneyRoleCapability", "candidate_fit_matrix": [], "mediation": {"classification": "INSUFFICIENT_WORLD_FACTS"}}
        )
        # Existing presented proposals are part of the current shared reality,
        # not an answer supplied by the mission. Expose their compact,
        # server-derived collective receipt during inspection so the model can
        # recognize a human trade-off without guessing from member names or
        # preloaded strategy hints.
        presented_collective: dict[str, Any] = {
            "classification": "INSUFFICIENT_WORLD_FACTS",
            "options": [],
        }
        if trigger is not None and hasattr(journey, "active_plan_version_id"):
            from app.main import collective_proposal_receipt
            presented = self.session.scalars(
                select(ReplanProposal).where(
                    ReplanProposal.journey_id == journey.id,
                    ReplanProposal.trigger_event_id == trigger.id,
                    ReplanProposal.status == "PRESENTED",
                ).order_by(ReplanProposal.created_at, ReplanProposal.id)
            ).all()
            # Some older unit seams use one untyped ``scalars`` mock for both
            # event and proposal queries.  The production query is typed, but
            # do not let that legacy read-only seam turn an inspection into a
            # collective-projection error.
            if presented and all(
                hasattr(item, "proposed_plan_version_id") for item in presented
            ):
                receipt = collective_proposal_receipt(self.session, journey, presented)
            else:
                receipt = {"collective_mediation": {}, "proposal_collective_decisions": {}}
            mediation = receipt.get("collective_mediation") or {}
            compact_options: list[dict[str, Any]] = []
            seen_option_shapes: set[str] = set()
            for item in receipt.get("proposal_collective_decisions", {}).values():
                if item.get("status") != "GROUNDED_FEASIBLE":
                    continue
                plan_id = str(item.get("plan_id") or "")
                shape = json.dumps(
                    {
                        "places": item.get("fit_subject_place_ids") or [],
                        "tags": item.get("fit_subject_tags") or [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                option_key = shape if shape != "{}" else plan_id
                if option_key in seen_option_shapes:
                    continue
                seen_option_shapes.add(option_key)
                compact_options.append({
                    "proposal_id": str(item.get("proposal_id")),
                    "plan_id": plan_id,
                    "status": item.get("status"),
                    "member_fits": item.get("member_fits") or {},
                    "fairness": item.get("fairness") or {},
                })
            presented_collective = {
                "classification": mediation.get("classification"),
                "decision_relevant_member_ids": mediation.get("decision_relevant_member_ids") or [],
                "options": compact_options,
                "fit_subject_basis": receipt.get("fit_subject_basis"),
            }
        mission_preferences = {}
        if request.mission_id is not None:
            mission = self.session.get(JourneyMission, str(request.mission_id))
            if mission is not None:
                try:
                    mission_preferences = json.loads(mission.preferences_json or "{}")
                except json.JSONDecodeError:
                    mission_preferences = {}
        decision_receipt = decision_sensitivity_receipt(
            windows,
            traveler_twin_context=compact_twin,
            current_mission_preferences=mission_preferences,
        )
        return {
            "tool": "inspect_twin",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "twin": twin,
            "traveler_twin_context": compact_twin,
            "standby_portfolio": (
                standby_portfolio(self.session, journey, trigger)
                if selection_error is None
                else []
            ),
            "actionable_events": [_event_choice(item) for item in actionable_events],
            "event_selection_required": selection_error == "EVENT_SELECTION_REQUIRED",
            "event_selection_error": selection_error,
            "decision_sensitivity": decision_receipt,
            "collective_journey": collective,
            "presented_collective_proposals": presented_collective,
            "current_event": (
                {"id": trigger.id, "event_type": trigger.event_type, "provenance": trigger.provenance,
                 "severity": trigger.severity, "affected_activity_id": trigger.affected_activity_id}
                if trigger is not None else None
            ),
        }

    # The agent surface used to hand back *every* persisted proposal with its full
    # diff / explanation / score breakdown. Measured on the Hermes lane DB (22
    # proposals) one `list_replans` call returned 2,922,724 bytes — a single tool
    # read larger than any model's usable context, so the first thing an agent does
    # after a replan blew the window it was trying to reason about.
    #
    # The bound below degrades the OLDEST rows' heavy fields first (the newest
    # proposal is the one an agent actually acts on) and always says so in the
    # envelope. A small read — which is what every existing witness uses — still
    # gets the untouched 12-field shape, so this is not a contract loosening.
    LIST_REPLANS_MAX_ITEMS = 20
    LIST_REPLANS_BYTE_BUDGET = 200_000
    LIST_REPLANS_HEAVY_FIELDS = ("diff", "explanation", "score_breakdown")

    def list_replans(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        from app.main import _replan_payload

        mission = (
            self.session.get(JourneyMission, str(request.mission_id))
            if request.mission_id is not None
            else None
        )
        r61_context = self._r61_reground_context(mission)
        if r61_context is not None:
            expected_trigger = str(
                mission.trigger_event_id or r61_context.get("trigger_event_id") or ""
            )
            if request.trigger_event_id is not None and str(request.trigger_event_id) != expected_trigger:
                raise HTTPException(409, "R61_TRIGGER_SCOPE_MISMATCH")
            if expected_trigger:
                request = request.model_copy(
                    update={"trigger_event_id": UUID(expected_trigger)}
                )

        trigger, actionable_events, selection_error = _resolve_actionable_event(
            self.session, journey.id, request.trigger_event_id
        )
        if selection_error is not None:
            return {
                "tool": "list_replans",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "error": selection_error,
                "message": (
                    "Select one actionable event by passing trigger_event_id before "
                    "requesting event-scoped recovery."
                ),
                "actionable_events": [_event_choice(item) for item in actionable_events],
                "event_scope": None,
                "proposals": [],
                "total": 0,
                "returned": 0,
                "truncated": False,
            }
        if not actionable_events and request.trigger_event_id is None:
            return {
                "tool": "list_replans",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "error": "NO_ACTIONABLE_EVENT",
                "message": "No event-scoped recovery is available until a truthful actionable event exists.",
                "actionable_events": [],
                "event_scope": None,
                "proposals": [],
                "total": 0,
                "returned": 0,
                "truncated": False,
            }
        if r61_context is not None:
            if mission is None or trigger is None:
                raise HTTPException(409, "R61_REGROUND_SCOPE_UNAVAILABLE")
            _context, _trigger, proposals = self._r61_current_scope(
                journey, mission, trigger_event_id=trigger.id
            )
            proposals.reverse()
        else:
            filters = [ReplanProposal.journey_id == journey.id]
            if trigger is not None:
                # Do not make an autonomous run compare the current disruption with
                # stale proposals generated for an older event. An empty scoped set
                # is a truthful signal to request a fresh deterministic replan.
                filters.append(ReplanProposal.trigger_event_id == trigger.id)
            proposals = self.session.scalars(
                select(ReplanProposal)
                .where(*filters)
                .order_by(ReplanProposal.created_at.desc(), ReplanProposal.id.desc())
            ).all()
        total = len(proposals)
        items = [_replan_payload(item) for item in proposals[: self.LIST_REPLANS_MAX_ITEMS]]

        omitted: list[str] = []

        def payload_bytes() -> int:
            return len(json.dumps(items, ensure_ascii=False, default=str).encode("utf-8"))

        for item in reversed(items):
            if payload_bytes() <= self.LIST_REPLANS_BYTE_BUDGET:
                break
            for field in self.LIST_REPLANS_HEAVY_FIELDS:
                if field in item:
                    item.pop(field)
                    omitted.append(f"{item['id']}:{field}")

        from app.main import collective_proposal_receipt
        collective_receipt = collective_proposal_receipt(self.session, journey, proposals)

        return {
            "tool": "list_replans",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "proposals": items,
            "order": "newest_first",
            "total": total,
            "returned": len(items),
            "truncated": total > len(items),
            "byte_budget": self.LIST_REPLANS_BYTE_BUDGET,
            "detail_fields_omitted": omitted,
            "event_scope": str(trigger.id) if trigger is not None else None,
            **collective_receipt,
        }

    def conclude_recovery(self, request: AgentToolCall) -> dict[str, Any]:
        """Return a typed conclusion marker; runtime validates it against the trace.

        This is deliberately not a domain mutation. It gives the persisted
        AgentRun a machine-readable conclusion without allowing model prose to
        masquerade as a completed recovery.
        """

        reason = (request.reason or "").strip()
        requested = request.outcome
        if requested not in {
            "MUTATION_EXECUTED",
            "APPROVAL_PENDING",
            "NO_ACTION",
            "UNRESOLVED",
            "CLARIFICATION_REQUIRED",
        }:
            raise HTTPException(422, "outcome must be a supported recovery conclusion")
        if request.mission_id is not None:
            mission = self.session.get(JourneyMission, str(request.mission_id))
            if self._r61_reground_context(mission) is not None:
                return self._conclude_r61_reground(request, requested, mission)
        if requested == "CLARIFICATION_REQUIRED":
            if request.journey_id is None or request.mission_id is None:
                raise HTTPException(422, "CLARIFICATION_REQUIRES_JOURNEY_AND_MISSION")
            if not request.question or not request.option_keys or not request.preference_dimension:
                raise HTTPException(422, "CLARIFICATION_REQUIRES_QUESTION_OPTIONS_AND_DIMENSION")
            from app.main import _twin_snapshot
            from app.intelligence.decision_sensitivity import (
                clarification_epoch_fingerprint,
                decision_sensitivity_receipt,
            )
            journey = _require_journey(self.session, request.journey_id)
            mission = self.session.get(JourneyMission, str(request.mission_id))
            if mission is None or mission.journey_id != journey.id:
                raise HTTPException(404, "JourneyMission was not found")
            twin = _twin_snapshot(self.session, journey)
            windows = twin.get("context", {}).get("remaining_opportunity_horizon", {}).get("windows", [])
            mission_preferences = {}
            try:
                mission_preferences = json.loads(mission.preferences_json or "{}")
            except json.JSONDecodeError:
                mission_preferences = {}
            decision = decision_sensitivity_receipt(
                windows,
                current_mission_preferences=mission_preferences,
            )
            if not decision.get("decision_sensitive"):
                raise HTTPException(422, "CLARIFICATION_NOT_DECISION_SENSITIVE")
            server_keys = [str(item["key"]) for item in decision["options"]]
            if set(request.option_keys) != set(server_keys):
                raise HTTPException(422, "CLARIFICATION_OPTIONS_NOT_SERVER_DERIVED")
            fingerprint = clarification_epoch_fingerprint(
                journey_id=journey.id,
                active_plan_version_id=journey.active_plan_version_id,
                current_activity_id=journey.current_activity_id,
                windows=windows,
            )
            clarification_id = str(UUID(bytes=__import__("uuid").uuid4().bytes))
            payload = {
                "clarification_id": clarification_id,
                "decision_epoch": fingerprint,
                "active_plan_version_id": journey.active_plan_version_id,
                "current_activity_id": journey.current_activity_id,
                "question": request.question,
                "option_keys": server_keys,
                "preference_dimension": request.preference_dimension,
                "option_to_strategy": {item["key"]: item["action"] for item in decision["options"]},
                "option_windows": {item["key"]: item["window_id"] for item in decision["options"]},
                "reason": "DECISION_SENSITIVE_HUMAN_VALUE",
            }
            mission.clarification_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            mission.status = "WAITING_FOR_USER"
            self.session.commit()
            return {
                "tool": "conclude_recovery",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "requested_outcome": requested,
                "reason": "DECISION_SENSITIVE_HUMAN_VALUE",
                "mission_status": "WAITING_FOR_USER",
                "clarification": payload,
                "decision_sensitivity": decision,
            }
        if requested == "NO_ACTION" and request.mission_id is not None:
            child = self.session.get(JourneyMission, str(request.mission_id))
            if child is not None and child.parent_mission_id:
                explicit = json.loads(child.preferences_json or "{}").get("current_explicit_mission", {})
                if explicit.get("strategy") == "DEFER":
                    raise HTTPException(409, "CLARIFIED_DEFER_REQUIRES_EXECUTION_EFFECT")
        if request.horizon_decision == "TAKE_NOW":
            if requested != "NO_ACTION" or request.journey_id is None or request.mission_id is None:
                raise HTTPException(422, "TAKE_NOW_REQUIRES_NON_MUTATING_CHILD_CONCLUSION")
            child = self.session.get(JourneyMission, str(request.mission_id))
            if child is None or child.journey_id != request.journey_id or child.parent_mission_id is None:
                raise HTTPException(409, "TAKE_NOW_REQUIRES_CLARIFICATION_CHILD")
            parent = self.session.get(JourneyMission, child.parent_mission_id)
            try:
                explicit = json.loads(child.preferences_json or "{}").get("current_explicit_mission", {})
                clarification_payload = json.loads(parent.clarification_json or "{}") if parent else {}
                answered = clarification_payload.get("answer", {})
                resolved_intent = json.loads(child.resolved_intent_json or "{}")
            except json.JSONDecodeError as exc:
                raise HTTPException(409, "TAKE_NOW_CLARIFICATION_PAYLOAD_INVALID") from exc
            if (
                parent is None
                or parent.status != "ANSWERED"
                or explicit.get("strategy") != "TAKE_NOW"
                or explicit.get("decision_option") != "TAKE_NOW"
                or answered.get("option_key") != "TAKE_NOW"
            ):
                raise HTTPException(409, "TAKE_NOW_CLARIFICATION_NOT_AUTHORIZED")
            from app.main import _twin_snapshot
            from app.intelligence.decision_sensitivity import clarification_epoch_fingerprint

            twin = _twin_snapshot(self.session, _require_journey(self.session, request.journey_id))
            windows = twin.get("context", {}).get("remaining_opportunity_horizon", {}).get("windows", [])
            current_fingerprint = clarification_epoch_fingerprint(
                journey_id=request.journey_id,
                active_plan_version_id=twin.get("active_plan_version_id") or resolved_intent.get("active_plan_version_id"),
                current_activity_id=twin.get("current_activity_id"),
                windows=windows,
            )
            if current_fingerprint != clarification_payload.get("decision_epoch"):
                raise HTTPException(409, "CLARIFICATION_STALE_RECOMPUTE_REQUIRED")
        if request.horizon_decision == "DEFER":
            if requested != "NO_ACTION" or request.journey_id is None:
                raise HTTPException(422, "DEFER requires a non-mutating NO_ACTION conclusion")
            from app.main import _twin_snapshot
            journey = _require_journey(self.session, request.journey_id)
            horizon = (_twin_snapshot(self.session, journey).get("context", {}).get("remaining_opportunity_horizon", {}).get("windows", []))
            if len(horizon) < 2:
                raise HTTPException(422, "DEFER_REQUIRES_TWO_GROUNDED_HORIZON_WINDOWS")
            first, later = horizon[0], horizon[1]
            first_candidate, later_candidate = first.get("candidate", {}), later.get("candidate", {})
            first_cost = first_candidate.get("cost_evidence", {}).get("amount_minor")
            later_cost = later_candidate.get("cost_evidence", {}).get("amount_minor")
            budget = int(first.get("optional_budget_remaining_minor") or 0)
            explicit_strategy = None
            if request.mission_id is not None:
                mission = self.session.get(JourneyMission, str(request.mission_id))
                if mission is not None and mission.journey_id == journey.id:
                    try:
                        mission_preferences = json.loads(mission.preferences_json or "{}")
                    except json.JSONDecodeError:
                        mission_preferences = {}
                    explicit_strategy = (
                        mission_preferences.get("current_explicit_mission", {}).get("strategy")
                    )
            if first.get("status") != "GROUNDED_FEASIBLE" or later.get("status") != "GROUNDED_FEASIBLE":
                raise HTTPException(422, "DEFER_REQUIRES_GROUNDED_FEASIBLE_WINDOWS")
            if not isinstance(first_cost, int) or not isinstance(later_cost, int) or first_cost > budget or later_cost > budget:
                raise HTTPException(422, "DEFER_REQUIRES_EACH_WINDOW_TO_BE_FEASIBLE")
            later_is_preferred = (
                explicit_strategy == "DEFER"
                or (later_candidate.get("preference_fit") or 0) > (first_candidate.get("preference_fit") or 0)
            )
            if first_cost + later_cost <= budget or not later_is_preferred:
                raise HTTPException(422, "DEFER_REQUIRES_HIGHER_VALUE_LATER_WINDOW_AND_SCARCE_BUDGET")
        return {
            "tool": "conclude_recovery",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "requested_outcome": requested,
            "reason": reason or None,
            "horizon_decision": request.horizon_decision,
        }

    def _conclude_r61_reground(
        self,
        request: AgentToolCall,
        requested: str,
        mission: JourneyMission,
    ) -> dict[str, Any]:
        if requested != "NO_ACTION":
            raise HTTPException(
                422,
                "R61_REGROUND_TERMINAL_OUTCOME_MUST_BE_NO_ACTION_OR_FRESH_COLLECTIVE_DECISION",
            )
        if request.horizon_decision not in {None, "NO_ACTION"}:
            raise HTTPException(422, "R61_REGROUND_DOES_NOT_ACCEPT_HORIZON_ACTIONS")
        if request.journey_id is None or str(request.journey_id) != mission.journey_id:
            raise HTTPException(409, "R61_REGROUND_JOURNEY_MISMATCH")

        prior = _mission_json(mission.clarification_json)
        terminal_states = {
            "RECOVERY_REGROUNDED_NO_ACTION",
            "RECOVERY_REGROUNDED_DOMINANT",
            "RECOVERY_REGROUND_NO_FEASIBLE_OPTION",
        }
        if (
            mission.status == "COMPLETED"
            and prior.get("kind") == "GROUP_RECOVERY_REGROUND"
            and prior.get("outcome") == "NO_ACTION"
            and prior.get("state") in terminal_states
        ):
            return {
                "tool": "conclude_recovery",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "requested_outcome": "NO_ACTION",
                "reason": prior["state"],
                "replayed": True,
                "r61_completion": {
                    "state": prior["state"],
                    "incident_key": prior.get("incident_key"),
                    "recovery_epoch_id": prior.get("recovery_epoch_id"),
                    "collective_classification": prior.get("collective_classification"),
                    "current_proposal_ids": prior.get("current_proposal_ids") or [],
                    "grounded_feasible_proposal_ids": prior.get("grounded_feasible_proposal_ids") or [],
                    "current_options": prior.get("current_options") or [],
                    "plan_unchanged": prior.get("plan_unchanged") is True,
                    "mutation_count": 0,
                },
            }

        journey = _require_journey(self.session, request.journey_id)
        context, trigger, proposals = self._r61_current_scope(
            journey,
            mission,
            trigger_event_id=(str(request.trigger_event_id) if request.trigger_event_id else None),
        )
        from app.main import collective_proposal_receipt

        receipt = collective_proposal_receipt(self.session, journey, proposals) if proposals else {
            "collective_mediation": {"classification": "NO_CURRENT_PROPOSALS"},
            "proposal_collective_decisions": {},
        }
        mediation = receipt.get("collective_mediation") or {}
        classification = str(mediation.get("classification") or "INSUFFICIENT_WORLD_FACTS")
        decisions = receipt.get("proposal_collective_decisions") or {}
        feasible_ids = sorted(
            str(proposal_id)
            for proposal_id, decision in decisions.items()
            if isinstance(decision, dict)
            and decision.get("status") == "GROUNDED_FEASIBLE"
        )

        if proposals and classification in {"INSUFFICIENT_WORLD_FACTS", "AUTHORITY_CONFLICT"}:
            raise HTTPException(409, f"R61_COLLECTIVE_FACTS_OR_AUTHORITY_INCOMPLETE:{classification}")
        if not proposals:
            state = "RECOVERY_REGROUNDED_NO_ACTION"
        elif not feasible_ids:
            state = "RECOVERY_REGROUND_NO_FEASIBLE_OPTION"
        elif classification in {"UNANIMOUS_DOMINANCE", "COMPROMISE_AVAILABLE"}:
            state = "RECOVERY_REGROUNDED_DOMINANT"
        elif classification == "HUMAN_TRADEOFF_REQUIRED":
            state = "RECOVERY_REGROUNDED_NO_ACTION"
        else:
            raise HTTPException(409, f"R61_COLLECTIVE_CLASSIFICATION_UNRESOLVED:{classification}")

        fit_place_ids = sorted(
            {
                str(place_id)
                for decision in decisions.values()
                if isinstance(decision, dict)
                for place_id in (decision.get("fit_subject_place_ids") or [])
                if place_id
            }
        )
        fit_places = {
            str(place.id): str(place.name)
            for place in self.session.scalars(
                select(Place).where(Place.id.in_(fit_place_ids))
            ).all()
        } if fit_place_ids else {}
        current_options = []
        for proposal in proposals:
            decision = decisions.get(str(proposal.id), {})
            subject_place_ids = [
                str(value)
                for value in (decision.get("fit_subject_place_ids") or [])
                if value
            ]
            current_options.append(
                {
                    "proposal_id": str(proposal.id),
                    "status": str(decision.get("status") or "REVALIDATION_REQUIRED"),
                    "place_names": [fit_places[value] for value in subject_place_ids if value in fit_places],
                    "fit_subject_tags": list(decision.get("fit_subject_tags") or [])[:8],
                }
            )

        if str(journey.active_plan_version_id or "") != str(context.get("active_plan_version_id") or ""):
            raise HTTPException(409, "STALE_COLLECTIVE_RECOVERY_PLAN")
        completed_at = datetime.now(timezone.utc).isoformat()
        clarification = {
            "kind": "GROUP_RECOVERY_REGROUND",
            "state": state,
            "incident_key": context.get("incident_key"),
            "reground_key": context.get("reground_key"),
            "failed_proposal_id": context.get("failed_proposal_id"),
            "old_clarification_id": context.get("failed_clarification_id"),
            "recovery_epoch_id": context.get("recovery_epoch_id"),
            "recovery_epoch_generation": context.get("recovery_epoch_generation"),
            "trigger_event_id": trigger.id,
            "current_proposal_ids": [str(item.id) for item in proposals],
            "grounded_feasible_proposal_ids": feasible_ids,
            "current_options": current_options,
            "collective_classification": classification,
            "outcome": "NO_ACTION",
            "completed_at": completed_at,
            "plan_unchanged": True,
        }
        mission.clarification_json = json.dumps(clarification, ensure_ascii=False, sort_keys=True)
        mission.status = "COMPLETED"
        mission.completed_at = completed_at
        resolved = _mission_json(mission.resolved_intent_json)
        current_context = resolved.get("group_recovery")
        if isinstance(current_context, dict):
            current_context["terminal_state"] = state
            current_context["terminal_at"] = completed_at
            resolved["group_recovery"] = current_context
            mission.resolved_intent_json = json.dumps(resolved, ensure_ascii=False, sort_keys=True)
        record_audit(
            self.session,
            action="journey.group_recovery.reground.concluded",
            entity_type="JourneyMission",
            entity_id=mission.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "state": state,
                "incident_key": context.get("incident_key"),
                "reground_key": context.get("reground_key"),
                "recovery_epoch_id": context.get("recovery_epoch_id"),
                "trigger_event_id": trigger.id,
                "collective_classification": classification,
                "current_proposal_ids": [str(item.id) for item in proposals],
                "grounded_feasible_proposal_ids": feasible_ids,
                "current_options": current_options,
                "plan_unchanged": True,
                "mutation_count": 0,
            },
        )
        # Native Hermes calls arrive through the public tool route rather than
        # the local AgentRun loop, so this terminal state must commit here.
        # The saved conclusion is also the idempotent replay receipt.
        self.session.commit()
        return {
            "tool": "conclude_recovery",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "requested_outcome": "NO_ACTION",
            "reason": state,
            "r61_completion": {
                "state": state,
                "incident_key": context.get("incident_key"),
                "recovery_epoch_id": context.get("recovery_epoch_id"),
                "collective_classification": classification,
                "current_proposal_ids": [str(item.id) for item in proposals],
                "grounded_feasible_proposal_ids": feasible_ids,
                "current_options": current_options,
                "plan_unchanged": True,
                "mutation_count": 0,
            },
        }

    def apply_clarified_horizon_choice(self, request: AgentToolCall) -> dict[str, Any]:
        """Apply the server-derived TAKE_LATER choice; never accept an activity ID."""
        if request.journey_id is None or request.mission_id is None:
            raise HTTPException(422, "CLARIFIED_CHOICE_REQUIRES_JOURNEY_AND_MISSION")
        journey = _require_journey(self.session, request.journey_id)
        child = self.session.get(JourneyMission, str(request.mission_id))
        if child is None or child.journey_id != journey.id or child.parent_mission_id is None:
            raise HTTPException(409, "CLARIFIED_CHOICE_REQUIRES_CHILD_MISSION")
        parent = self.session.get(JourneyMission, child.parent_mission_id)
        if parent is None or parent.status != "ANSWERED":
            raise HTTPException(409, "CLARIFIED_CHOICE_PARENT_NOT_ANSWERED")
        try:
            child_preferences = json.loads(child.preferences_json or "{}")
            resolved = json.loads(child.resolved_intent_json or "{}")
            clarification = json.loads(parent.clarification_json or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(409, "CLARIFIED_CHOICE_PAYLOAD_INVALID") from exc
        explicit = child_preferences.get("current_explicit_mission") or resolved.get("current_explicit_mission") or {}
        if explicit.get("strategy") != "DEFER" or explicit.get("decision_option") != "TAKE_LATER":
            raise HTTPException(409, "CLARIFIED_CHOICE_IS_NOT_TAKE_LATER")
        if clarification.get("answer", {}).get("option_key") != "TAKE_LATER":
            raise HTTPException(409, "CLARIFIED_CHOICE_PARENT_ANSWER_MISMATCH")
        from app.main import _twin_snapshot, _skip_current_activity
        from app.intelligence.decision_sensitivity import clarification_epoch_fingerprint
        twin = _twin_snapshot(self.session, journey)
        windows = twin.get("context", {}).get("remaining_opportunity_horizon", {}).get("windows", [])
        current_fingerprint = clarification_epoch_fingerprint(
            journey_id=journey.id,
            active_plan_version_id=journey.active_plan_version_id,
            current_activity_id=journey.current_activity_id,
            windows=windows,
        )
        if current_fingerprint != clarification.get("decision_epoch"):
            raise HTTPException(409, "CLARIFICATION_STALE_RECOMPUTE_REQUIRED")
        later_window = next((item for item in windows if item.get("window_id") == clarification.get("option_windows", {}).get("TAKE_LATER")), None)
        now_window = next((item for item in windows if item.get("window_id") == clarification.get("option_windows", {}).get("TAKE_NOW")), None)
        if now_window is None or later_window is None:
            raise HTTPException(409, "CLARIFIED_CHOICE_HORIZON_RECOMPUTE_REQUIRED")
        if now_window.get("status") != "GROUNDED_FEASIBLE" or later_window.get("status") != "GROUNDED_FEASIBLE":
            raise HTTPException(422, "CLARIFIED_CHOICE_REQUIRES_TWO_GROUNDED_FEASIBLE_WINDOWS")
        now_cost = now_window.get("candidate", {}).get("cost_evidence", {}).get("amount_minor")
        later_cost = later_window.get("candidate", {}).get("cost_evidence", {}).get("amount_minor")
        budget = int(now_window.get("optional_budget_remaining_minor") or 0)
        if not isinstance(now_cost, int) or not isinstance(later_cost, int) or now_cost > budget or later_cost > budget or now_cost + later_cost <= budget:
            raise HTTPException(422, "CLARIFIED_CHOICE_RESOURCE_CONFLICT_RECOMPUTE_REQUIRED")
        current_id = str(now_window.get("candidate", {}).get("activity_id") or "")
        later_id = str(later_window.get("candidate", {}).get("activity_id") or "")
        activity = self.session.get(Activity, current_id)
        later_activity = self.session.get(Activity, later_id)
        if activity is None or later_activity is None or journey.current_activity_id != activity.id:
            raise HTTPException(409, "CLARIFIED_CHOICE_CURRENT_ACTIVITY_CHANGED")
        if activity.status not in {"ACTIVE", "NOT_STARTED"} or activity.locked:
            raise HTTPException(409, "CLARIFIED_CHOICE_W1_NOT_SKIPPABLE")
        active_booking = self.session.scalar(
            select(Booking).where(
                Booking.journey_id == journey.id,
                Booking.activity_id == activity.id,
                Booking.status.not_in(("CANCELLED", "REFUNDED", "EXPIRED")),
            )
        )
        if active_booking is not None:
            raise HTTPException(409, "CLARIFIED_CHOICE_BOOKING_REQUIRES_NORMAL_APPROVAL")
        _, receipt = _skip_current_activity(
            self.session,
            journey,
            activity,
            actor_type="AI_AGENT",
            reason="CLARIFIED_DEFER_FOR_LATER_OPTION",
            mission_id=child.id,
            clarification_id=clarification.get("clarification_id"),
        )
        receipt["tool"] = "apply_clarified_horizon_choice"
        receipt["decision"] = AuthorizationDecision.ALLOW_AUTO.value
        receipt["strategy"] = "DEFER"
        receipt["later_activity_id"] = later_activity.id
        return receipt

    def research_place_sources(self, request: AgentToolCall) -> dict[str, Any]:
        """Return bounded, non-authoritative source leads for one server Place."""
        if request.journey_id is None or not request.place_id:
            raise HTTPException(422, "journey_id and place_id are required")
        journey = _require_journey(self.session, request.journey_id)
        place = self.session.get(Place, request.place_id)
        if place is None or place.city_code != journey.city_code:
            raise HTTPException(404, "Place is not part of the Journey city")
        from app.core.config import settings
        from app.integrations.web.searxng import SearXNGSearchProvider
        from app.integrations.web.research_service import build_source_query
        from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
        from app.integrations.ai.web_research import WebResearchProvider

        query = build_source_query(place, journey)
        drafts: list[Any] = []
        provider_status = "SOURCE_DISCOVERY_UNAVAILABLE"
        if settings.ai_api_key and settings.deepseek_web_search_enabled:
            client = DeepSeekResponsesClient(
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                base_url=settings.deepseek_api_url,
                reasoning_effort="low",
                timeout_seconds=settings.impact_timeout_seconds,
            )
            try:
                drafts = list(WebResearchProvider(client).search(query)[:5])
                provider_status = "CAPTURED" if drafts else "NO_RESULTS"
            except DeepSeekResponsesError:
                provider_status = "FAILED_EXTERNAL"
        elif settings.searxng_url:
            provider = SearXNGSearchProvider(settings.searxng_url)
            try:
                drafts = list(provider.search(query, limit=5))
                provider_status = "CAPTURED" if drafts else "NO_RESULTS"
            except Exception:
                provider_status = "FAILED_EXTERNAL"
            finally:
                provider.close()
        rows: list[WebObservation] = []
        for item in drafts[:5]:
            fingerprint = str(getattr(item, "evidence_fingerprint", "")) or hashlib.sha256(
                f"{item.query}|{item.url}|{item.title}|{item.content_excerpt if hasattr(item, 'content_excerpt') else item.content}".encode()
            ).hexdigest()
            existing = self.session.scalar(select(WebObservation).where(
                WebObservation.journey_id == journey.id,
                WebObservation.evidence_fingerprint == fingerprint,
            ))
            if existing is None:
                existing = WebObservation(
                    id=str(uuid4()), journey_id=journey.id,
                    query=item.query, url=item.url, domain=item.domain, title=item.title,
                    source_class=getattr(item, "source_class", "OPEN_WEB"),
                    provider_key=item.provider_key, model_key=getattr(item, "model_key", ""),
                    captured_at=item.captured_at, evidence_fingerprint=fingerprint,
                    content_excerpt=getattr(item, "content_excerpt", getattr(item, "content", ""))[:2000],
                    authority_status="NON_AUTHORITATIVE", created_at=item.captured_at,
                )
                self.session.add(existing)
            rows.append(existing)
        self.session.commit()
        return {
            "tool": "research_place_sources", "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "journey_id": journey.id, "place_id": place.id, "query": query,
            "status": provider_status, "authority": "NON_AUTHORITATIVE",
            "observations": [{"id": row.id, "url": row.url, "domain": row.domain, "title": row.title,
                              "provider": row.provider_key, "captured_at": row.captured_at,
                              "authority_status": row.authority_status} for row in rows[:5]],
        }

    def search_city_services(self, request: AgentToolCall) -> dict[str, Any]:
        journey = None
        city_code = request.city_code
        if city_code is None and request.journey_id is not None:
            journey = _require_journey(self.session, request.journey_id)
            city_code = journey.city_code
        if not city_code:
            raise HTTPException(422, "city_code or journey_id is required for this Agent tool")
        services = self.session.scalars(
            select(CityService).where(CityService.city_code == city_code).order_by(CityService.id)
        ).all()
        if request.service_type:
            services = [
                item
                for item in services
                if item.service_type.lower() == request.service_type.lower()
            ]
        search_terms = _city_search_terms(request.q)
        if search_terms:
            services = [
                item
                for item in services
                if any(
                    term in item.name.casefold() or term in item.service_type.casefold()
                    for term in search_terms
                )
            ]
        places = self.session.scalars(
            select(Place).where(Place.city_code == city_code).order_by(Place.id)
        ).all()
        discovery_receipt: dict[str, Any] | None = None
        # An unseeded live Journey must be able to enter the same real discovery
        # path used by the product surface.  The Agent still receives only the
        # materialized provider-backed rows; it never supplies or invents IDs.
        if not services and not places and journey is not None:
            from app.core.config import settings
            from app.integrations.discovery.real_city import capture_real_city_candidates

            if settings.real_city_enabled and settings.real_city_mode == "live":
                discovery_query = request.q or request.category or "attraction"
                discovered = capture_real_city_candidates(
                    self.session,
                    journey,
                    query=discovery_query,
                    radius_m=5000,
                    limit=8,
                    mode="live",
                )
                self.session.commit()
                provider_status = discovered.get("provider_status") or {}
                discovery_receipt = {
                    "attempted": True,
                    "query": discovery_query,
                    "mode": discovered.get("mode"),
                    "provenance": discovered.get("provenance"),
                    "runtime_provenance": discovered.get("runtime_provenance"),
                    "candidate_count": len(discovered.get("candidates") or []),
                    "provider_status": provider_status,
                }
                services = self.session.scalars(
                    select(CityService).where(CityService.city_code == city_code).order_by(CityService.id)
                ).all()
                places = self.session.scalars(
                    select(Place).where(Place.city_code == city_code).order_by(Place.id)
                ).all()
        category_terms = _city_category_terms(request.category)
        if category_terms:
            places = [
                item
                for item in places
                if any(term in item.category.casefold() for term in category_terms)
            ]
        if search_terms:
            places = [
                item
                for item in places
                if any(
                    term in item.name.casefold()
                    or item.name.casefold() in term
                    or term in item.category.casefold()
                    or term in item.tags_json.casefold()
                    for term in search_terms
                )
            ]
        result = {
            "tool": "search_city_services",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "city_code": city_code,
            "services": [_json(service_schema(item)) for item in services],
            "places": [
                {
                    "id": item.id,
                    "name": item.name,
                    "category": item.category,
                    "city_code": item.city_code,
                    # Keep provider identity first-class in the agent result.  A real
                    # catalogue may contain multiple rows with the same display label
                    # (for example an OSM way and a separate OSM node).  The agent must
                    # be able to distinguish those rows without guessing from the name
                    # or silently taking the first result.
                    "provider_key": item.provider_key,
                    "external_id": item.external_id,
                    "location": {"latitude": item.latitude, "longitude": item.longitude},
                    "tags": json.loads(item.tags_json or "[]"),
                    "opening_hours": json.loads(item.opening_hours_json or "{}"),
                    "metadata": _agent_place_metadata(item),
                }
                for item in places
            ],
        }
        if discovery_receipt is not None:
            result["discovery"] = discovery_receipt
        return result

    def discover_recovery_options(self, request: AgentToolCall) -> dict[str, Any]:
        """Expand one recovery epoch with a bounded, server-anchored city search."""
        if request.journey_id is None or request.trigger_event_id is None or request.mission_id is None:
            raise HTTPException(422, "journey_id, trigger_event_id, and mission_id are required")
        journey = _require_journey(self.session, request.journey_id)
        mission = self.session.get(JourneyMission, str(request.mission_id))
        if mission is None or mission.journey_id != journey.id:
            raise HTTPException(404, "Journey mission was not found")
        trigger = self.session.get(Event, str(request.trigger_event_id))
        if trigger is None or trigger.journey_id != journey.id or trigger.status not in {"DETECTED", "ACKNOWLEDGED"}:
            raise HTTPException(409, "discovery trigger is not actionable")
        epoch_id = self._bound_recovery_epoch_id(request, journey)
        if not epoch_id:
            epoch_id = str(ensure_recovery_epoch(self.session, journey).id)
        query = " ".join(str(request.q or request.category or "").split()).strip()
        if not query:
            raise HTTPException(422, "a recovery search intent is required")
        category = " ".join(str(request.category or "").split()).strip() or None
        radius_m = 2500
        candidate_limit = 8
        query_hash = hashlib.sha256(
            json.dumps(
                {"journey_id": journey.id, "recovery_epoch_id": epoch_id, "query": query.casefold(), "category": category.casefold() if category else None, "radius_m": radius_m, "limit": candidate_limit},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self.session.scalar(
            select(RecoveryDiscoveryAttempt).where(
                RecoveryDiscoveryAttempt.recovery_epoch_id == epoch_id,
                RecoveryDiscoveryAttempt.query_hash == query_hash,
            )
        )
        if existing is not None:
            place_ids = json.loads(existing.materialized_place_ids_json or "[]")
            return {
                "tool": "discover_recovery_options",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "status": "REUSED",
                "recovery_epoch_id": epoch_id,
                "attempt_id": existing.id,
                "query": existing.query,
                "candidate_place_ids": place_ids,
                "raw_candidate_count": existing.raw_candidate_count,
                "provider_mode": existing.provider_mode,
                "runtime_provenance": existing.runtime_provenance,
                "idempotent_reuse": True,
            }
        attempts = self.session.scalars(
            select(RecoveryDiscoveryAttempt).where(
                RecoveryDiscoveryAttempt.recovery_epoch_id == epoch_id
            )
        ).all()
        if len(attempts) >= 2:
            return {
                "tool": "discover_recovery_options",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "status": "DISCOVERY_EXHAUSTED",
                "recovery_epoch_id": epoch_id,
                "attempt_count": len(attempts),
                "max_attempts": 2,
                "candidate_place_ids": [],
                "idempotent_reuse": False,
            }
        from app.core.config import settings
        from app.integrations.discovery.real_city import capture_real_city_candidates

        record_audit(
            self.session,
            action="agent.recovery_discovery.started",
            entity_type="JourneyMission",
            entity_id=mission.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={"recovery_epoch_id": epoch_id, "query": query, "attempt_count": len(attempts) + 1},
        )
        mode = str(settings.real_city_mode or "replay").lower()
        if mode not in {"live", "captured_replay", "replay"}:
            mode = "replay"
        now = datetime.now(timezone.utc).isoformat()
        try:
            captured = capture_real_city_candidates(
                self.session,
                journey,
                query=query,
                radius_m=radius_m,
                limit=candidate_limit,
                mode=mode,
            )
            candidates = captured.get("candidates") or []
            provider_status = captured.get("provider_status") or {}
            runtime_provenance = str(captured.get("runtime_provenance") or captured.get("provenance") or mode.upper())
            status = "COMPLETED"
            if mode == "live" and str(captured.get("mode") or "").endswith("unavailable"):
                status = "DEGRADED"
        except Exception as exc:
            candidates = []
            provider_status = {"failure": "PROVIDER_DISCOVERY_FAILED", "detail": str(exc)[:240]}
            runtime_provenance = "PROVIDER_DEGRADED"
            status = "DEGRADED"
            captured = {"mode": mode}
        place_ids = [str(item.get("materialized_place_id") or item.get("id")) for item in candidates if item.get("materialized_place_id") or item.get("id")]
        attempt = RecoveryDiscoveryAttempt(
            id=str(uuid4()), journey_id=journey.id, recovery_epoch_id=epoch_id,
            mission_id=mission.id, query_hash=query_hash, query=query, category=category,
            radius_m=radius_m, candidate_limit=candidate_limit, status=status,
            provider_mode=str(captured.get("mode") or mode), runtime_provenance=runtime_provenance,
            raw_candidate_count=len(candidates), materialized_place_ids_json=json.dumps(place_ids),
            provider_status_json=json.dumps(provider_status, ensure_ascii=False, sort_keys=True),
            created_at=now, updated_at=now,
        )
        self.session.add(attempt)
        record_audit(
            self.session,
            action="agent.recovery_discovery.completed",
            entity_type="RecoveryDiscoveryAttempt",
            entity_id=attempt.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "attempt_id": attempt.id, "recovery_epoch_id": epoch_id,
                "query": query, "status": status,
                "raw_candidate_count": len(candidates), "materialized_place_ids": place_ids,
                "runtime_provenance": runtime_provenance,
            },
        )
        self.session.commit()
        return {
            "tool": "discover_recovery_options",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "status": status,
            "recovery_epoch_id": epoch_id,
            "attempt_id": attempt.id,
            "query": query,
            "candidate_place_ids": place_ids,
            "raw_candidate_count": len(candidates),
            "provider_mode": attempt.provider_mode,
            "runtime_provenance": runtime_provenance,
            "idempotent_reuse": False,
            "creates_proposal": False,
            "mutates_active_plan": False,
        }

    def acquire_place_facts(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        if not request.place_id:
            raise HTTPException(422, "place_id is required for acquire_place_facts")
        place = self.session.get(Place, request.place_id)
        if place is None or place.city_code != journey.city_code:
            raise HTTPException(404, "Place is not part of the Journey city")
        bindings = None
        binding_origin = "agent"
        if request.source_observation_id is not None:
            observation = self.session.get(WebObservation, str(request.source_observation_id))
            if observation is None or observation.journey_id != journey.id:
                raise HTTPException(409, "SOURCE_OBSERVATION_NOT_OWNED_BY_JOURNEY")
            if observation.authority_status != "NON_AUTHORITATIVE":
                raise HTTPException(409, "SOURCE_OBSERVATION_STATE_INVALID")
            bindings = {place.id: {"source_url": observation.url, "source_class": observation.source_class,
                                   "source_observation_id": observation.id}}
            binding_origin = "agent_observation"
        elif request.source_url:
            bindings = {
                place.id: {
                    "source_url": request.source_url,
                    "source_class": request.source_class,
                }
            }
        receipt = acquire_place_facts(
            self.session,
            journey,
            place_ids=[place.id],
            source_bindings=bindings,
            binding_origin=binding_origin,
        )
        # Fact acquisition mutates canonical Place rows.  This gateway is the
        # product write boundary, so a successful tool call must survive the
        # request session before the next planner/read operation begins.
        self.session.commit()
        return {
            "tool": "acquire_place_facts",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "place_id": place.id,
            "receipt": receipt,
        }

    def estimate_place_facts(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        if not request.place_id:
            raise HTTPException(422, "place_id is required for estimate_place_facts")
        place = self.session.get(Place, request.place_id)
        if place is None or place.city_code != journey.city_code:
            raise HTTPException(404, "Place is not part of the Journey city")
        peers = self.session.scalars(
            select(Place).where(Place.city_code == journey.city_code, Place.category == place.category)
        ).all()
        peer_costs = [
            value for peer in peers
            if json.loads(peer.metadata_json or "{}").get("cost_evidence", {}).get("currency") == journey.budget_currency
            and (value := _place_verified_cost(peer)) is not None
        ]
        peer_windows = [window for peer in peers if (window := _place_window(peer)) is not None]
        estimate: dict[str, Any] = {
            "place_id": place.id,
            "authority_eligible": False,
            "basis": "same-city verified peers in the same provider category",
            "opening_hours": {"status": "ESTIMATED_AGENT"},
            "cost_evidence": {"status": "ESTIMATED_AGENT", "currency": journey.budget_currency},
        }
        if peer_windows:
            opens = [window[0] for window in peer_windows]
            closes = [window[1] for window in peer_windows]
            estimate["opening_hours"].update(
                {"open_minutes": int(median(opens)), "close_minutes": int(median(closes)), "peer_count": len(peer_windows)}
            )
        if peer_costs:
            estimate["cost_evidence"].update(
                {"amount_minor": int(median(peer_costs)), "peer_count": len(peer_costs)}
            )
        if (
            request.estimated_open_minutes is not None
            and request.estimated_close_minutes is not None
            and request.estimated_close_minutes > request.estimated_open_minutes
        ):
            estimate["basis"] = "agent_bounded_hypothesis"
            estimate["opening_hours"].update(
                {
                    "open_minutes": request.estimated_open_minutes,
                    "close_minutes": request.estimated_close_minutes,
                    "basis": "AGENT_PROVIDED_HYPOTHESIS",
                }
            )
        if request.estimated_cost_minor is not None:
            estimate["basis"] = "agent_bounded_hypothesis"
            estimate["cost_evidence"].update(
                {
                    "amount_minor": request.estimated_cost_minor,
                    "basis": "AGENT_PROVIDED_HYPOTHESIS",
                }
            )
        metadata = json.loads(place.metadata_json or "{}") if place.metadata_json else {}
        metadata["strategy_estimate"] = {
            "status": "ESTIMATED_AGENT",
            "authority_eligible": False,
            "basis": estimate["basis"],
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "opening_hours": dict(estimate["opening_hours"]),
            "cost_evidence": dict(estimate["cost_evidence"]),
        }
        place.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        # Estimates are strategy observations, not canonical Place facts.  Persisting them lets
        # the next deterministic compose explain how the Agent's decision was made without
        # promoting the values into opening_hours_json or cost_evidence.
        self.session.commit()
        return {
            "tool": "estimate_place_facts",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "estimate": estimate,
            "warning": (
                "ESTIMATED_AGENT never becomes canonical/provider truth. It may support a selectable "
                "coursework-only candidate only when the explicit isolated lab estimate envelope, "
                "live mobility evidence, and deterministic authority gate all pass; selection remains separate."
            ),
        }

    def generate_plans(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        from app.main import active_journey_members_with_capabilities
        requested = list(dict.fromkeys(str(item) for item in (request.place_ids or [])))
        if len(requested) < 2:
            raise HTTPException(422, "place_ids must contain at least two distinct server-owned Place IDs")
        rows = self.session.scalars(select(Place).where(Place.id.in_(requested))).all()
        found = {row.id: row for row in rows}
        missing = [place_id for place_id in requested if place_id not in found]
        wrong_city = [place_id for place_id in requested if place_id in found and found[place_id].city_code != journey.city_code]
        if missing or wrong_city:
            raise HTTPException(
                422,
                _json({
                    "code": "PLACE_IDS_NOT_IN_JOURNEY_CITY",
                    "missing_place_ids": missing,
                    "wrong_city_place_ids": wrong_city,
                }),
            )
        try:
            plans = compose_plans(
                self.session,
                journey,
                request.planning_mode or journey.planning_mode,
                request.candidate_count,
                place_ids=requested,
            )
        except (TypeError, ValueError) as exc:
            self.session.rollback()
            raise HTTPException(422, str(exc)) from exc
        self.session.commit()
        summaries: list[dict[str, Any]] = []
        collective_windows: list[dict[str, Any]] = []
        from app.collective_mediation import plan_collective_fit_subject
        active_projection = None
        if journey.active_plan_version_id:
            active_plan = self.session.get(PlanVersion, journey.active_plan_version_id)
            if active_plan is not None:
                active_projection = plan_schema(self.session, active_plan).model_dump(mode="json")
        for plan in plans:
            projected = plan_schema(self.session, plan).model_dump(mode="json")
            explanation = projected.get("explanation") or {}
            mobility = projected.get("mobility_evidence") or {}
            subject = plan_collective_fit_subject(
                (active_projection or {}).get("activities", []), projected.get("activities", [])
            )
            activity_place_ids = [item.get("place_id") for item in projected.get("activities", []) if item.get("place_id")]
            place_ids = list(subject["fit_subject_place_ids"])
            places_for_fit = self.session.scalars(select(Place).where(Place.id.in_(place_ids))).all() if place_ids else []
            tags: list[str] = []
            for place in places_for_fit:
                tags.append(str(place.category or ""))
                tags.extend(json.loads(place.tags_json or "[]"))
            subject["fit_subject_tags"] = sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()})
            hard_status = "GROUNDED_FEASIBLE" if projected["selectable"] else "REVALIDATION_REQUIRED"
            collective_windows.append({
                "window_id": projected["id"],
                "status": hard_status,
                "candidate": {
                    "place_id": projected["id"],
                    "ordered_place_ids": place_ids,
                    "tags": subject["fit_subject_tags"],
                    **subject,
                },
            })
            summaries.append(
                {
                    "id": projected["id"],
                    "version_no": projected["version_no"],
                    "status": projected["status"],
                    "selectable": projected["selectable"],
                    "place_ids": activity_place_ids,
                    "fit_subject_place_ids": place_ids,
                    "unresolved_facts": explanation.get("unresolved_facts", []),
                    "authority_state": explanation.get("authority_state"),
                    "mobility_provider": mobility.get("source_provider"),
                    "mobility_snapshot_id": projected.get("mobility_snapshot_id"),
                    **subject,
                }
            )
        collective = active_journey_members_with_capabilities(
            self.session, journey, candidate_windows=collective_windows
        )
        for summary, row in zip(summaries, collective.get("candidate_fit_matrix", [])):
            summary["collective_fairness"] = row.get("fairness", {})
            summary["collective_member_fits"] = row.get("member_fits", {})
        collective.pop("members", None)
        return {
            "tool": "generate_plans",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "candidate_count": len(summaries),
            "plans": summaries,
            "collective_mediation": collective,
            "authority_note": "Candidates are not selected or active; selection remains a separate deterministic authority decision.",
        }

    def request_replan(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        mission = (
            self.session.get(JourneyMission, str(request.mission_id))
            if request.mission_id is not None
            else None
        )
        r61_context = self._r61_reground_context(mission)
        if r61_context is not None:
            expected_trigger = str(
                mission.trigger_event_id or r61_context.get("trigger_event_id") or ""
            )
            if request.trigger_event_id is not None and str(request.trigger_event_id) != expected_trigger:
                raise HTTPException(409, "R61_TRIGGER_SCOPE_MISMATCH")
            _context, _trigger, _proposals = self._r61_current_scope(
                journey, mission, trigger_event_id=expected_trigger
            )
            request = request.model_copy(
                update={"trigger_event_id": UUID(expected_trigger)}
            )
        trigger, events, selection_error = _resolve_actionable_event(
            self.session, journey.id, request.trigger_event_id
        )
        if selection_error is not None or trigger is None:
            return {
                "tool": "request_replan",
                "error": selection_error or "NO_ACTIONABLE_EVENT",
                "executed": False,
                "actionable_events": [_event_choice(item) for item in events],
            }
        request = request.model_copy(update={"trigger_event_id": UUID(trigger.id)})
        policy_row = get_or_create_policy(self.session, journey.traveler_id)
        policy = policy_from_model(policy_row)
        protected_activity_ids: list[UUID] = []
        if request.mission_id is not None:
            mission = self.session.get(JourneyMission, str(request.mission_id))
            if mission is None or mission.journey_id != journey.id:
                raise HTTPException(404, "Journey mission was not found")
            protected_activity_ids = [
                UUID(item) for item in json.loads(mission.protected_activity_ids_json or "[]")
            ]
        auth = evaluate_action(policy, AgentAction.REQUEST_REPLAN, AuthoritativeActionFacts())
        run_epoch_id = self._bound_recovery_epoch_id(request, journey)
        signature = canonical_request_signature(
            {
                "tool": "request_replan",
                "request_id": request.request_id,
                "agent_id": request.agent_id,
                "journey_id": journey.id,
                "trigger_event_id": str(request.trigger_event_id),
                "candidate_count": request.candidate_count,
                "solver_mode": request.solver_mode,
                "place_ids": list(request.place_ids or []),
                "mission_id": str(request.mission_id) if request.mission_id else None,
            }
        )
        # request_id is the caller's idempotency scope. Keep the proposal or
        # Journey out of the key so reuse with different arguments becomes a
        # deterministic conflict instead of a second mutation.
        key = _agent_key("request_replan", request.agent_id, request.request_id)
        stored = _read_idempotent(self.session, key, signature)
        if stored is not None:
            upgraded = _upgrade_cached_request_replan_response(
                self.session,
                stored,
                journey_id=journey.id,
                trigger_event_id=str(request.trigger_event_id),
            )
            cached_filters = [
                ReplanProposal.journey_id == journey.id,
                ReplanProposal.trigger_event_id == str(request.trigger_event_id),
            ]
            if run_epoch_id:
                cached_filters.append(
                    ReplanProposal.recovery_epoch_id == str(run_epoch_id)
                )
            cached_rows = self.session.scalars(
                select(ReplanProposal)
                .where(*cached_filters)
                .order_by(ReplanProposal.created_at, ReplanProposal.id)
            ).all()
            from app.main import collective_proposal_receipt
            upgraded.update(collective_proposal_receipt(self.session, journey, cached_rows))
            return upgraded

        from app.main import generate_replan

        # Agent tool calls may omit the optional bounded search width.  Keep
        # the request deterministic instead of passing None into the public
        # ReplanGenerateRequest, where it becomes an opaque 500.
        candidate_count = int(request.candidate_count or 3)
        inner_key = _agent_key(
            "replan_generation", request.agent_id, request.request_id, journey.id
        )
        previous_epoch_context = self.session.info.get(
            "recovery_epoch_id"
        )
        previous_run_context = self.session.info.get("source_agent_run_id")
        if run_epoch_id:
            self.session.info["recovery_epoch_id"] = str(run_epoch_id)
        if self.bound_run_id:
            self.session.info["source_agent_run_id"] = self.bound_run_id
        try:
            generated = generate_replan(
                UUID(journey.id),
                ReplanGenerateRequest(
                    trigger_event_id=request.trigger_event_id,
                    candidate_count=candidate_count,
                    solver_mode=request.solver_mode,
                    protected_activity_ids=protected_activity_ids,
                    place_ids=request.place_ids,
                ),
                idempotency_key=inner_key,
                session=self.session,
            )
        finally:
            if previous_epoch_context is None:
                self.session.info.pop("recovery_epoch_id", None)
            else:
                self.session.info["recovery_epoch_id"] = previous_epoch_context
            if previous_run_context is None:
                self.session.info.pop("source_agent_run_id", None)
            else:
                self.session.info["source_agent_run_id"] = previous_run_context

        if run_epoch_id:
            try:
                validate_recovery_epoch_binding(
                    self.session, journey.id, str(run_epoch_id)
                )
            except RecoveryEpochSuperseded as exc:
                raise HTTPException(409, detail=exc.facts)

        proposal_filters = [
            ReplanProposal.journey_id == journey.id,
            ReplanProposal.trigger_event_id == str(request.trigger_event_id),
        ]
        if run_epoch_id:
            proposal_filters.append(
                ReplanProposal.recovery_epoch_id == str(run_epoch_id)
            )
        proposals = self.session.scalars(
            select(ReplanProposal)
            .where(*proposal_filters)
            .order_by(ReplanProposal.created_at, ReplanProposal.id)
        ).all()
        canonical_by_identity: dict[str, ReplanProposal] = {}
        for item in proposals:
            item.semantic_identity = semantic_plan_identity(
                self.session, item.proposed_plan_version_id
            )
            if item.semantic_identity:
                canonical = canonical_by_identity.setdefault(item.semantic_identity, item)
                if canonical.id != item.id:
                    item.canonical_proposal_id = canonical.id
        self.session.flush()
        actionable_proposals = [
            item for item in proposals if not item.canonical_proposal_id
        ]
        response = {
            "tool": "request_replan",
            "decision": auth.decision.value,
            "reason_codes": list(auth.reason_codes),
            "policy_version": auth.policy_version,
            "request_id": request.request_id,
            "proposal": generated,
            "proposals": [
                {
                    "id": item.id,
                    "status": item.status,
                    "hard_constraints_passed": bool(item.hard_constraints_passed),
                    "locked_commitments_preserved": bool(item.locked_commitments_preserved),
                }
                for item in actionable_proposals
            ],
        }
        from app.main import collective_proposal_receipt
        response.update(collective_proposal_receipt(self.session, journey, actionable_proposals))
        record_audit(
            self.session,
            action="agent.request_replan",
            entity_type="ReplanProposal",
            entity_id=str(generated["id"]),
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "tool": "request_replan",
                "policy_version": auth.policy_version,
                "authorization_decision": auth.decision.value,
                "reason_codes": list(auth.reason_codes),
                "before_status": None,
                "after_status": "PRESENTED",
                "request_id": request.request_id,
                "journey_id": journey.id,
            },
        )
        _write_idempotent(
            self.session,
            key=key,
            operation="agent.request_replan",
            signature=signature,
            response=response,
        )
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            stored = _read_idempotent(self.session, key, signature)
            if stored is not None:
                return _upgrade_cached_request_replan_response(
                    self.session,
                    stored,
                    journey_id=journey.id,
                    trigger_event_id=str(request.trigger_event_id),
                )
            raise
        return response

    def request_collective_decision(self, request: AgentToolCall) -> dict[str, Any]:
        """Create a server-owned shared decision only for a real trade-off.

        The model supplies scope, never options or members.  This tool creates
        no plan and changes no active reality; it only moves the mission into a
        durable member-response state.
        """
        if request.mission_id is None or request.trigger_event_id is None:
            raise HTTPException(422, "mission_id and trigger_event_id are required")
        journey = _require_journey(self.session, request.journey_id)
        mission = self.session.get(JourneyMission, str(request.mission_id))
        if mission is None or mission.journey_id != journey.id:
            raise HTTPException(404, "Journey mission was not found")
        r61_context = self._r61_reground_context(mission)
        if r61_context is not None:
            _context, trigger, proposals = self._r61_current_scope(
                journey, mission, trigger_event_id=str(request.trigger_event_id)
            )
        else:
            trigger = self.session.get(Event, str(request.trigger_event_id))
            proposals = self.session.scalars(
                select(ReplanProposal).where(
                    ReplanProposal.journey_id == journey.id,
                    ReplanProposal.trigger_event_id == str(request.trigger_event_id),
                    ReplanProposal.status == "PRESENTED",
                ).order_by(ReplanProposal.created_at, ReplanProposal.id)
            ).all()
        if trigger is None or trigger.journey_id != journey.id or trigger.status not in {"DETECTED", "ACKNOWLEDGED"}:
            raise HTTPException(409, "collective decision trigger is not actionable")
        from app.main import (
            active_journey_members_with_capabilities,
            collective_material_world_basis,
            collective_proposal_receipt,
        )
        from app.collective_authority import candidate_surface_fingerprint
        from app.collective_mediation import collective_material_world_epoch
        receipt = collective_proposal_receipt(self.session, journey, proposals)
        mediation = receipt.get("collective_mediation") or {}
        options = []
        seen_option_shapes: set[str] = set()
        for item in receipt.get("proposal_collective_decisions", {}).values():
            if item.get("status") != "GROUNDED_FEASIBLE":
                continue
            plan_id = str(item.get("plan_id") or "")
            shape = json.dumps(
                {
                    # A plan's changed-place/tag surface is a set.  Preserve
                    # the first proposal for an equivalent future even when
                    # independently generated plans serialize those values in
                    # a different order.
                    "places": sorted(
                        {
                            str(value)
                            for value in (item.get("fit_subject_place_ids") or [])
                            if value
                        }
                    ),
                    "tags": sorted(
                        {
                            str(value).strip().lower()
                            for value in (item.get("fit_subject_tags") or [])
                            if str(value).strip()
                        }
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            option_key = shape if shape != "{}" else plan_id
            if option_key in seen_option_shapes:
                continue
            seen_option_shapes.add(option_key)
            options.append(item)
        # Freeze the physical basis for each choice in this decision epoch.
        # Labels and preference fit alone must not keep an old answer current
        # after the selected route/canonical facts have materially changed.
        for option in options:
            plan = self.session.get(PlanVersion, str(option.get("plan_id") or ""))
            if plan is None:
                raise HTTPException(409, "COLLECTIVE_OPTION_PLAN_UNAVAILABLE")
            option["material_world_epoch"] = collective_material_world_epoch(
                collective_material_world_basis(
                    self.session,
                    journey,
                    plan,
                    fit_subject_place_ids=list(option.get("fit_subject_place_ids") or []),
                )
            )
        try:
            mission_intent = json.loads(mission.resolved_intent_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            mission_intent = {}
        mediation_intent = (
            mission_intent.get("collective_mediation")
            if isinstance(mission_intent, dict)
            else None
        )
        is_disagreement_mediator = (
            isinstance(mediation_intent, dict)
            and mediation_intent.get("kind") == "DISAGREEMENT_MEDIATION"
        )

        if len(options) < 2:
            raise HTTPException(409, "COLLECTIVE_TRADEOFF_NOT_REQUIRED")
        if (
            not is_disagreement_mediator
            and mediation.get("classification") != "HUMAN_TRADEOFF_REQUIRED"
        ):
            raise HTTPException(409, "COLLECTIVE_TRADEOFF_NOT_REQUIRED")

        # A mediator child must make measurable progress. Proposal ids alone
        # are not progress, and after explicit disagreement even a server-
        # classified COMPROMISE_AVAILABLE option requires renewed human
        # consent instead of becoming automatic social authority.
        mediation_progress = None
        if is_disagreement_mediator:
            source_id = str(mediation_intent.get("source_mission_id") or "")
            source = self.session.get(JourneyMission, source_id) if source_id else None
            if source is None or source.journey_id != journey.id:
                raise HTTPException(409, "MEDIATION_PROGRESS_CONTEXT_MISSING")
            try:
                source_clarification = json.loads(source.clarification_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                source_clarification = {}
            from app.collective_mediation import mediation_progress_gate

            mediation_progress = mediation_progress_gate(
                source_clarification.get("candidate_options") or [],
                options,
            )
            if not mediation_progress.get("allowed"):
                raise HTTPException(409, "MEDIATION_NO_PROGRESS")
        members = active_journey_members_with_capabilities(self.session, journey).get("members", [])
        member_by_id = {str(item.get("member_id")): item for item in members}
        if is_disagreement_mediator:
            relevant_ids = {
                str(item)
                for item in (mediation_intent.get("required_member_ids") or [])
            }
        else:
            relevant_ids = {
                str(item)
                for item in (mediation.get("decision_relevant_member_ids") or [])
            }
        # A member may affect fairness without holding Journey mutation
        # authority. Keep their fit in the decision epoch, but never place a
        # read-only role into the set whose signatures can mint collective
        # mutation authority.
        authorizing_member_ids = {
            member_id
            for member_id, item in member_by_id.items()
            if bool(item.get("can_authorize_mutation"))
        }
        required_ids = sorted(relevant_ids.intersection(authorizing_member_ids))
        if not required_ids:
            raise HTTPException(409, "COLLECTIVE_MUTATION_AUTHORITY_CONFLICT")
        from app.collective_mediation import collective_decision_epoch
        decision_epoch = collective_decision_epoch(
            journey_id=journey.id,
            mission_id=mission.id,
            trigger_event_id=trigger.id,
            base_plan_version_id=journey.active_plan_version_id,
            options=options,
            required_member_ids=required_ids,
            decision_relevant_member_ids=sorted(relevant_ids),
            members=members,
        )
        surface_fingerprint = candidate_surface_fingerprint(
            receipt.get("candidate_fit_matrix") or []
        )
        existing_clarification = _mission_json(mission.clarification_json)
        if existing_clarification.get("kind") == "COLLECTIVE_TRADEOFF":
            if (
                str(existing_clarification.get("trigger_event_id") or "") != str(trigger.id)
                or str(existing_clarification.get("base_plan_version_id") or "")
                != str(journey.active_plan_version_id or "")
            ):
                existing_clarification["status"] = "STALE"
                mission.clarification_json = json.dumps(
                    existing_clarification, ensure_ascii=False, sort_keys=True
                )
                self.session.commit()
                raise HTTPException(409, "STALE_COLLECTIVE_DECISION")
            if existing_clarification.get("status") == "WAITING_FOR_MEMBERS":
                if (
                    existing_clarification.get("decision_epoch") != decision_epoch
                    or existing_clarification.get("candidate_surface_fingerprint")
                    != surface_fingerprint
                ):
                    existing_clarification["status"] = "STALE"
                    mission.clarification_json = json.dumps(
                        existing_clarification, ensure_ascii=False, sort_keys=True
                    )
                    self.session.commit()
                    raise HTTPException(409, "STALE_COLLECTIVE_DECISION")
                return {
                    "tool": "request_collective_decision",
                    "decision": AuthorizationDecision.ALLOW_AUTO.value,
                    "executed": False,
                    "reused": True,
                    "mission_status": mission.status,
                    "clarification": existing_clarification,
                    "collective_mediation": mediation,
                }
            # A resolved, stale, or mediation-required round is durable history.
            # Never replace it with an empty response map on tool replay.
            return {
                "tool": "request_collective_decision",
                "decision": AuthorizationDecision.ALLOW_AUTO.value,
                "executed": False,
                "reused": True,
                "mission_status": mission.status,
                "clarification": existing_clarification,
                "collective_mediation": mediation,
            }
        clarification_id = str(uuid4())
        payload = {
            "kind": "COLLECTIVE_TRADEOFF",
            "policy": (
                "POST_DISAGREEMENT_REQUIRES_RENEWED_MEMBER_CONSENT"
                if is_disagreement_mediator
                else "MATERIAL_TRADEOFF_REQUIRES_ALL_RELEVANT_MEMBER_RESPONSES"
            ),
            "clarification_id": clarification_id,
            "decision_epoch": decision_epoch,
            "candidate_surface_fingerprint": surface_fingerprint,
            "trigger_event_id": trigger.id,
            "base_plan_version_id": journey.active_plan_version_id,
            "candidate_options": options,
            "required_member_ids": required_ids,
            "decision_relevant_member_ids": sorted(relevant_ids),
            "responses": {},
            "status": "WAITING_FOR_MEMBERS",
            "selected_proposal_id": None,
            "selected_candidate_id": None,
        }
        if r61_context is not None:
            payload["state"] = "RECOVERY_REGROUNDED_DECISION_REQUIRED"
            payload["r61_reground"] = {
                "incident_key": r61_context.get("incident_key"),
                "reground_key": r61_context.get("reground_key"),
                "recovery_epoch_id": r61_context.get("recovery_epoch_id"),
                "recovery_epoch_generation": r61_context.get("recovery_epoch_generation"),
                "plan_unchanged": str(journey.active_plan_version_id)
                == str(r61_context.get("active_plan_version_id") or ""),
            }
        if mediation_progress is not None:
            payload["mediation_progress"] = mediation_progress
        mission.clarification_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        mission.status = "WAITING_FOR_MEMBERS"
        mission.completed_at = None
        record_audit(
            self.session,
            action="agent.collective_decision.requested",
            entity_type="JourneyMission",
            entity_id=mission.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "clarification_id": clarification_id,
                "status": "WAITING_FOR_MEMBERS",
                "required_member_ids": required_ids,
                "mediation_progress": mediation_progress,
            },
        )
        # One durable cue per affected member and decision epoch. Notification
        # delivery remains the existing infrastructure; this key is the
        # collective decision boundary so Guardian pulses cannot fan out spam.
        from app.operations.v9 import notify

        for required_member_id in required_ids:
            notify(
                self.session,
                journey.id,
                dedupe_key=(
                    f"collective-decision:{journey.id}:"
                    f"{decision_epoch}:{required_member_id}"
                ),
                kind="COLLECTIVE_DECISION",
                priority="HIGH",
                payload={
                    "member_id": required_member_id,
                    "decision_epoch": decision_epoch,
                    "message": "The shared trip needs your choice.",
                },
            )
        self.session.commit()
        return {
            "tool": "request_collective_decision",
            "decision": AuthorizationDecision.ALLOW_AUTO.value,
            "executed": False,
            "mission_status": "WAITING_FOR_MEMBERS",
            "clarification": payload,
            "collective_mediation": mediation,
        }

    def activate_standby(self, request: AgentToolCall) -> dict[str, Any]:
        """Governed handover of a fresh standby branch; distinct from generic select."""
        if request.standby_plan_id is None or request.trigger_event_id is None:
            raise HTTPException(422, "standby_plan_id and trigger_event_id are required")
        journey = _require_journey(self.session, request.journey_id)
        standby = self.session.get(PlanVersion, str(request.standby_plan_id))
        if standby is None or standby.journey_id != journey.id:
            raise HTTPException(404, "standby plan was not found for this Journey")
        if standby.lifecycle != "STANDBY" or standby.status != "CANDIDATE":
            raise HTTPException(409, "only a live STANDBY branch can be activated")
        if standby.parent_plan_version_id != journey.active_plan_version_id:
            raise HTTPException(409, "standby parent is no longer the current reality")
        trigger = self.session.get(Event, str(request.trigger_event_id))
        if trigger is None or trigger.journey_id != journey.id or trigger.status not in {"DETECTED", "ACKNOWLEDGED"}:
            raise HTTPException(409, "activation trigger is not actionable")
        if trigger.provenance == "SIMULATED":
            raise HTTPException(409, "synthetic trigger cannot activate a standby branch")
        from app.agent.standby import standby_trigger_match
        current = self.session.get(PlanVersion, journey.active_plan_version_id)
        match = standby_trigger_match(standby, trigger, current)
        if not match.compatible:
            raise HTTPException(409, f"STANDBY_TRIGGER_MISMATCH:{match.code}")
        evidence = json.loads(standby.mobility_evidence_json or "{}")
        expires = evidence.get("expires_at")
        try:
            expired = not expires or datetime.fromisoformat(str(expires)) <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            raise HTTPException(409, "standby mobility evidence has expired")
        mobility = MobilitySnapshot.from_evidence(evidence)
        gate = validate_plan_feasibility(self.session, journey, standby, mobility_provider=mobility)
        if not gate.passed:
            raise HTTPException(409, f"standby is no longer feasible: {', '.join(gate.failures)}")
        from app.main import collective_mutation_gate
        collective_gate = collective_mutation_gate(
            self.session,
            journey,
            standby,
            hard_feasible_plan_ids={str(standby.id)},
        )
        if not collective_gate.get("allowed"):
            raise HTTPException(
                409,
                f"{collective_gate.get('code')}: collective mediation must be resolved before standby activation",
            )
        policy_row = get_or_create_policy(self.session, journey.traveler_id)
        auth = self._bound_authorization(
            request,
            evaluate_action(
                policy_from_model(policy_row), AgentAction.ACCEPT_REPLAN,
                AuthoritativeActionFacts(
                    hard_constraints_passed=True,
                    locked_commitments_preserved=True,
                    base_plan_current=True,
                    trigger_actionable=True,
                    event_severity=trigger.severity,
                    cost_delta_minor=standby.total_cost_minor - (self.session.get(PlanVersion, journey.active_plan_version_id).total_cost_minor if journey.active_plan_version_id else 0),
                ),
            ),
        )
        approval = None
        if auth.decision in {AuthorizationDecision.REQUIRE_CONFIRMATION, AuthorizationDecision.REQUIRE_STEP_UP}:
            approval_fingerprint_value = approval_fingerprint(
                action=AgentAction.ACTIVATE_STANDBY.value,
                journey_id=journey.id,
                target_type="PlanVersion",
                target_id=standby.id,
                policy_version=auth.policy_version,
                decision=auth.decision.value,
                facts={"trigger_event_id": trigger.id, "expires_at": expires},
                proposal_diff={"standby_plan_id": standby.id},
            )
            if request.approval_id is not None:
                approval = consume_action_approval(
                    self.session, approval_id=str(request.approval_id), journey_id=journey.id,
                    action=AgentAction.ACTIVATE_STANDBY.value, target_type="PlanVersion",
                    target_id=standby.id, request_fingerprint=approval_fingerprint_value,
                    actor_id=request.agent_id,
                )
            else:
                approval = create_or_reuse_approval(
                    self.session, traveler_id=journey.traveler_id, journey_id=journey.id,
                    action=AgentAction.ACTIVATE_STANDBY.value, target_type="PlanVersion",
                    target_id=standby.id, request_fingerprint=approval_fingerprint_value,
                    actor_id=request.agent_id, required_decision=auth.decision.value,
                )
            if approval.status != "CONSUMED":
                self.session.commit()
                return {"tool": "activate_standby", "decision": auth.decision.value,
                        "executed": False, "user_confirmed": False,
                        **approval_payload(approval), "plan_id": standby.id,
                        "trigger_event_id": trigger.id}
        base = current
        if base is None:
            raise HTTPException(409, "Journey has no active reality")
        from app.main import _switch_active_plan_atomically
        old_current = self.session.get(Activity, journey.current_activity_id) if journey.current_activity_id else None
        _switch_active_plan_atomically(self.session, base=base, proposed=standby, decided_at=datetime.now(timezone.utc).isoformat())
        standby.lifecycle = "ACTIVE"
        base.lifecycle = "RETIRED"
        journey.active_plan_version_id = standby.id
        replacement = self.session.scalar(select(Activity).where(Activity.plan_version_id == standby.id, Activity.sequence_no == (old_current.sequence_no if old_current else 1)))
        journey.current_activity_id = replacement.id if replacement else None
        if replacement:
            replacement.status = "ACTIVE"
        siblings = self.session.scalars(
            select(PlanVersion).where(
                PlanVersion.journey_id == journey.id,
                PlanVersion.parent_plan_version_id == base.id,
                PlanVersion.lifecycle == "STANDBY",
                PlanVersion.id != standby.id,
            )
        ).all()
        for sibling in siblings:
            sibling.lifecycle = "RETIRED"
            sibling.status = "REJECTED"
            record_audit(
                self.session, action="standby.retired", entity_type="PlanVersion",
                entity_id=sibling.id, journey_id=journey.id, actor_type="SYSTEM",
                actor_id="standby-activation", after={"reason": "parent_superseded", "parent_plan_version_id": base.id},
            )
        record_audit(self.session, action="standby.activated", entity_type="PlanVersion", entity_id=standby.id,
                     journey_id=journey.id, actor_type="AI_AGENT", actor_id=request.agent_id,
                     after={"trigger_event_id": trigger.id, "base_plan_version_id": base.id,
                            "authorization_decision": auth.decision.value})
        self.session.commit()
        return {"tool": "activate_standby", "decision": auth.decision.value, "executed": True,
                "plan_id": standby.id, "previous_active_plan_id": base.id, "trigger_event_id": trigger.id}

    def accept_replan(self, request: AgentToolCall) -> dict[str, Any]:
        if request.proposal_id is None:
            raise HTTPException(422, "proposal_id is required for accept_replan")
        proposal = self.session.get(ReplanProposal, str(request.proposal_id))
        if proposal is None:
            raise HTTPException(404, "Replan proposal was not found")
        journey = _require_journey(self.session, request.journey_id or UUID(proposal.journey_id))
        if proposal.journey_id != journey.id:
            raise HTTPException(403, "proposal does not belong to this Journey")
        policy_row = get_or_create_policy(self.session, journey.traveler_id)
        policy = policy_from_model(policy_row)
        facts, evidence = _facts_for_accept(self.session, proposal, journey)
        auth = self._bound_authorization(
            request, evaluate_action(policy, AgentAction.ACCEPT_REPLAN, facts)
        )
        signature = canonical_request_signature(
            {
                "tool": "accept_replan",
                "request_id": request.request_id,
                "agent_id": request.agent_id,
                "journey_id": journey.id,
                "proposal_id": proposal.id,
                "approval_id": str(request.approval_id) if request.approval_id else None,
            }
        )
        # The request id is the idempotency identity; proposal_id remains in
        # the canonical signature and therefore cannot silently change it.
        key = _agent_key("accept_replan", request.agent_id, request.request_id)
        stored = _read_idempotent(self.session, key, signature)
        if stored is not None:
            return stored

        if not facts.base_plan_current or not facts.trigger_actionable:
            # Staleness is a domain conflict. The policy result is still
            # recorded in the response for a caller that is comparing receipts.
            raise HTTPException(409, "replan proposal is stale or its trigger is resolved")

        from app.main import _collective_resolution_for_child
        resolved_collective_decision = _collective_resolution_for_child(
            self.session, journey, proposal, str(request.mission_id) if request.mission_id else None
        )

        try:
            proposal_diff = json.loads(proposal.diff_json or "{}")
        except json.JSONDecodeError:
            proposal_diff = {}
        target_plan = self.session.get(
            PlanVersion, proposal.proposed_plan_version_id
        )
        if target_plan is None or target_plan.journey_id != journey.id:
            raise HTTPException(409, "replan target plan is missing")

        from app.main import (
            _record_collective_mutation_denial,
            collective_authority_preflight,
        )

        authority_preflight = collective_authority_preflight(
            self.session,
            journey,
            proposal,
            target_plan,
            hard_feasible_plan_ids=(
                {str(target_plan.id)}
                if facts.hard_constraints_passed
                else set()
            ),
            resolved_decision=resolved_collective_decision,
        )
        if authority_preflight.get("allowed") is not True:
            _record_collective_mutation_denial(
                self.session,
                journey,
                target_plan,
                authority_preflight,
                mutation_kind="ACCEPT_REPLAN",
                proposal_id=proposal.id,
            )
            raise HTTPException(
                409,
                f"{authority_preflight.get('code')}: "
                "collective authority is not currently spendable",
            )
        preflight_receipt_id = (
            str(authority_preflight.get("authority_receipt_id") or "")
            or None
        )
        preflight_recovery_epoch_id = (
            str(authority_preflight.get("recovery_epoch_id") or "") or None
        )
        approval_signature = approval_fingerprint(
            action=AgentAction.ACCEPT_REPLAN.value,
            journey_id=journey.id,
            target_type="ReplanProposal",
            target_id=proposal.id,
            policy_version=auth.policy_version,
            decision=auth.decision.value,
            facts=evidence,
            proposal_diff=proposal_diff,
        )
        approval = None
        if auth.decision in {
            AuthorizationDecision.REQUIRE_CONFIRMATION,
            AuthorizationDecision.REQUIRE_STEP_UP,
        }:
            if request.approval_id is not None:
                approval = consume_action_approval(
                    self.session,
                    approval_id=str(request.approval_id),
                    journey_id=journey.id,
                    action=AgentAction.ACCEPT_REPLAN.value,
                    target_type="ReplanProposal",
                    target_id=proposal.id,
                    request_fingerprint=approval_signature,
                    actor_id=request.agent_id,
                    recovery_epoch_id=preflight_recovery_epoch_id,
                )
            else:
                approval = create_or_reuse_approval(
                    self.session,
                    traveler_id=journey.traveler_id,
                    journey_id=journey.id,
                    action=AgentAction.ACCEPT_REPLAN.value,
                    target_type="ReplanProposal",
                    target_id=proposal.id,
                    request_fingerprint=approval_signature,
                    actor_id=request.agent_id,
                    required_decision=auth.decision.value,
                    recovery_epoch_id=preflight_recovery_epoch_id,
                )
            if preflight_receipt_id:
                existing_receipt_id = str(
                    approval.authority_receipt_id or ""
                )
                if (
                    existing_receipt_id
                    and existing_receipt_id != preflight_receipt_id
                ):
                    self.session.rollback()
                    raise HTTPException(
                        409,
                        "APPROVAL_AUTHORITY_RECEIPT_MISMATCH",
                    )
                approval.authority_receipt_id = preflight_receipt_id

        approved_receipt = approval is not None and approval.status == "CONSUMED"
        if auth.decision == AuthorizationDecision.ALLOW_AUTO or approved_receipt:
            from app.main import _accept_replan_impl

            response_reason_codes = auth.reason_codes + (
                ("USER_APPROVAL_RECEIPT_CONSUMED",) if approved_receipt else ()
            )

            inner_key = _agent_key(
                "replan_accept", request.agent_id, request.request_id, proposal.id
            )
            result = _accept_replan_impl(
                UUID(proposal.id),
                inner_key,
                self.session,
                actor_type="AI_AGENT",
                actor_id=request.agent_id,
                policy_version=auth.policy_version,
                authorization_decision=auth.decision.value,
                reason_codes=response_reason_codes,
                resolved_decision=resolved_collective_decision,
                expected_authority_receipt_id=preflight_receipt_id,
                expected_recovery_epoch_id=preflight_recovery_epoch_id,
            )
            receipt_id = result.get("collective_authority_receipt_id") if isinstance(result, dict) else None
            if approval is not None and receipt_id:
                approval.authority_receipt_id = str(receipt_id)
            response = {
                "tool": "accept_replan",
                "decision": auth.decision.value,
                "reason_codes": list(response_reason_codes),
                "policy_version": auth.policy_version,
                "request_id": request.request_id,
                "executed": True,
                "user_confirmed": False,
                "facts": evidence,
                "result": result,
            }
            if receipt_id:
                # Surface the exact server-issued authority at the Gateway
                # boundary so continuation/pending state can bind the same
                # receipt without parsing nested domain payloads.
                response["collective_authority_receipt_id"] = str(receipt_id)
            if approval is not None:
                response.update(approval_payload(approval))
            after_status = "ACCEPTED"
        else:
            response = {
                "tool": "accept_replan",
                "decision": auth.decision.value,
                "reason_codes": list(auth.reason_codes),
                "policy_version": auth.policy_version,
                "request_id": request.request_id,
                "executed": False,
                "user_confirmed": False,
                "facts": evidence,
                "proposal_id": proposal.id,
                "status": proposal.status,
            }
            if approval is not None:
                response.update(approval_payload(approval))
            if preflight_receipt_id:
                response["collective_authority_receipt_id"] = (
                    preflight_receipt_id
                )
            after_status = proposal.status

        record_audit(
            self.session,
            action="agent.accept_replan",
            entity_type="ReplanProposal",
            entity_id=proposal.id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            before={
                "status": "PRESENTED",
                "policy_version": auth.policy_version,
            },
            after={
                "tool": "accept_replan",
                "proposal_id": proposal.id,
                "status": after_status,
                "policy_version": auth.policy_version,
                "authorization_decision": auth.decision.value,
                "reason_codes": response.get("reason_codes", list(auth.reason_codes)),
                "request_id": request.request_id,
                "user_confirmed": False,
                "approval_id": response.get("approval_id"),
                "approval_status": response.get("status"),
                "collective_authority_receipt_id": response.get(
                    "collective_authority_receipt_id"
                ),
            },
        )
        _write_idempotent(
            self.session,
            key=key,
            operation="agent.accept_replan",
            signature=signature,
            response=response,
        )
        self.session.commit()
        return response

    def _typed_activity(self, journey: Journey, activity_id: UUID | None) -> Activity:
        if activity_id is None:
            raise HTTPException(422, "activity_id is required for this Agent tool")
        activity = self.session.get(Activity, str(activity_id))
        if activity is None:
            raise HTTPException(404, "Activity was not found")
        if activity.plan_version_id != journey.active_plan_version_id:
            raise HTTPException(409, "activity does not belong to the active Journey plan")
        if activity.status in {"COMPLETED", "SKIPPED", "CANCELLED"}:
            raise HTTPException(409, "terminal activity cannot be changed")
        return activity

    def _typed_commitments(self, journey: Journey, activity_id: str) -> list[Commitment]:
        return self.session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.activity_id == activity_id,
                Commitment.status == "ACTIVE",
            )
        ).all()

    def _typed_facts(
        self,
        *,
        policy,
        activity: Activity | None,
        commitments: list[Commitment],
        cost_delta_minor: int = 0,
        loss_delta_minor: int = 0,
        schedule_drift_min: int = 0,
        action_reversible: bool = True,
        commitment_management_requested: bool = False,
        booking_type: str | None = None,
    ) -> tuple[AuthoritativeActionFacts, dict[str, Any]]:
        protected = any(item.level in policy.protected_commitment_levels for item in commitments)
        c4_affected = bool(activity and activity.locked) or protected
        facts = AuthoritativeActionFacts(
            hard_constraints_passed=True,
            locked_commitments_preserved=not c4_affected,
            c4_affected=c4_affected,
            cost_delta_minor=max(0, int(cost_delta_minor)),
            loss_delta_minor=max(0, int(loss_delta_minor)),
            schedule_drift_min=max(0, int(schedule_drift_min)),
            affected_activity_type=activity.activity_type if activity else None,
            affected_booking_type=booking_type,
            commitment_management_requested=commitment_management_requested,
            commitment_levels=tuple(item.level for item in commitments),
            action_reversible=action_reversible,
        )
        return facts, {
            "hard_constraints_passed": facts.hard_constraints_passed,
            "locked_commitments_preserved": facts.locked_commitments_preserved,
            "c4_affected": facts.c4_affected,
            "cost_delta_minor": facts.cost_delta_minor,
            "loss_delta_minor": facts.loss_delta_minor,
            "schedule_drift_min": facts.schedule_drift_min,
            "affected_activity_type": facts.affected_activity_type,
            "affected_booking_type": facts.affected_booking_type,
            "commitment_management_requested": facts.commitment_management_requested,
            "commitment_levels": list(facts.commitment_levels),
            "action_reversible": facts.action_reversible,
        }

    def _run_typed_mutation(
        self,
        request: AgentToolCall,
        journey: Journey,
        *,
        action: AgentAction,
        target_type: str,
        target_id: str,
        facts: AuthoritativeActionFacts,
        evidence: dict[str, Any],
        arguments: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        auth = self._bound_authorization(request, evaluate_action(policy, action, facts))
        tool_name = action.value.lower()
        signature = canonical_request_signature(
            {
                "tool": tool_name,
                "request_id": request.request_id,
                "agent_id": request.agent_id,
                "journey_id": journey.id,
                "arguments": arguments,
                "approval_id": str(request.approval_id) if request.approval_id else None,
            }
        )
        key = _agent_key(tool_name, request.agent_id, request.request_id)
        stored = _read_idempotent(self.session, key, signature)
        if stored is not None:
            return stored

        approval = None
        if auth.decision in {
            AuthorizationDecision.REQUIRE_CONFIRMATION,
            AuthorizationDecision.REQUIRE_STEP_UP,
        }:
            approval_signature = approval_fingerprint(
                action=action.value,
                journey_id=journey.id,
                target_type=target_type,
                target_id=target_id,
                policy_version=auth.policy_version,
                decision=auth.decision.value,
                facts=evidence,
                proposal_diff=arguments,
            )
            if request.approval_id is not None:
                approval = consume_action_approval(
                    self.session,
                    approval_id=str(request.approval_id),
                    journey_id=journey.id,
                    action=action.value,
                    target_type=target_type,
                    target_id=target_id,
                    request_fingerprint=approval_signature,
                    actor_id=request.agent_id,
                )
            else:
                approval = create_or_reuse_approval(
                    self.session,
                    traveler_id=journey.traveler_id,
                    journey_id=journey.id,
                    action=action.value,
                    target_type=target_type,
                    target_id=target_id,
                    request_fingerprint=approval_signature,
                    actor_id=request.agent_id,
                    required_decision=auth.decision.value,
                )

        approved_receipt = approval is not None and approval.status == "CONSUMED"
        executed = auth.decision == AuthorizationDecision.ALLOW_AUTO or approved_receipt
        response: dict[str, Any] = {
            "tool": tool_name,
            "decision": auth.decision.value,
            "reason_codes": list(auth.reason_codes)
            + (["USER_APPROVAL_RECEIPT_CONSUMED"] if approved_receipt else []),
            "policy_version": auth.policy_version,
            "request_id": request.request_id,
            "executed": executed,
            "user_confirmed": False,
            "facts": evidence,
            "target_type": target_type,
            "target_id": target_id,
        }
        if approval is not None:
            approval_view = approval_payload(approval)
            response.update(
                {
                    "approval_id": approval_view["approval_id"],
                    "approval_status": approval_view["status"],
                    "approval_method": approval_view["approval_method"],
                    "approval_expires_at": approval_view["expires_at"],
                }
            )
        if executed:
            try:
                response["result"] = execute()
            except CommerceConflict as exc:
                self.session.rollback()
                raise HTTPException(409, str(exc)) from exc
            except (CommerceValidationError, ValueError) as exc:
                self.session.rollback()
                raise HTTPException(422, str(exc)) from exc

        record_audit(
            self.session,
            action=f"agent.{tool_name}",
            entity_type=target_type,
            entity_id=target_id,
            journey_id=journey.id,
            actor_type="AI_AGENT",
            actor_id=request.agent_id,
            after={
                "tool": tool_name,
                "target_type": target_type,
                "target_id": target_id,
                "status": "EXECUTED" if executed else "PENDING",
                "authorization_decision": auth.decision.value,
                "reason_codes": response["reason_codes"],
                "request_id": request.request_id,
                "approval_id": response.get("approval_id"),
                "approval_status": response.get("approval_status"),
            },
        )
        _write_idempotent(
            self.session,
            key=key,
            operation=f"agent.{tool_name}",
            signature=signature,
            response=response,
        )
        self.session.commit()
        return response

    def replace_activity(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        target = self._typed_activity(journey, request.activity_id)
        replacement = None
        replacement_place = None
        replacement_service = None
        if request.replacement_activity_id is not None:
            replacement = self._typed_activity(journey, request.replacement_activity_id)
            if target.id == replacement.id:
                raise HTTPException(422, "replacement_activity_id must differ from activity_id")
            if replacement.locked:
                raise HTTPException(409, "replacement activity is protected")
            if self._typed_commitments(journey, replacement.id):
                raise HTTPException(409, "replacement activity already has an active commitment")
        elif request.replacement_place_id is not None:
            replacement_place = self.session.get(Place, request.replacement_place_id)
            if replacement_place is None:
                raise HTTPException(404, "replacement Place was not found")
            if replacement_place.city_code != journey.city_code:
                raise HTTPException(409, "replacement Place is outside the Journey city")
            if replacement_place.id == target.place_id:
                raise HTTPException(422, "replacement_place_id must differ from the target Place")
            replacement_service = self.session.scalar(
                select(CityService)
                .where(
                    CityService.city_code == journey.city_code,
                    CityService.place_id == replacement_place.id,
                )
                .order_by(CityService.id)
            )
        else:
            raise HTTPException(
                422,
                "replace_activity requires replacement_activity_id or replacement_place_id",
            )
        commitments = self._typed_commitments(journey, target.id)
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        if replacement is not None:
            replacement_cost_minor = replacement.estimated_cost_minor
            replacement_end_at = datetime.fromisoformat(replacement.end_at)
            replacement_travel_from_prev_min = replacement.travel_from_prev_min
        else:
            assert replacement_place is not None
            metadata = json.loads(replacement_place.metadata_json or "{}")
            duration_min = int(metadata.get("duration_min", 60))
            replacement_cost_minor = int(metadata.get("cost_minor", 0))
            replacement_end_at = datetime.fromisoformat(target.start_at) + timedelta(
                minutes=duration_min
            )
            previous = self.session.scalar(
                select(Activity).where(
                    Activity.plan_version_id == journey.active_plan_version_id,
                    Activity.sequence_no == target.sequence_no - 1,
                )
            )
            previous_location = previous.place_id or previous.city_service_id if previous else None
            if previous_location is None or previous_location == replacement_place.id:
                replacement_travel_from_prev_min = 0
            else:
                from app.integrations.mock.providers import MockMobilityProvider

                replacement_travel_from_prev_min = MockMobilityProvider().estimate_minutes(
                    previous_location,
                    replacement_place.id,
                )
        drift = abs(
            int((replacement_end_at - datetime.fromisoformat(target.end_at)).total_seconds() // 60)
        )
        facts, evidence = self._typed_facts(
            policy=policy,
            activity=target,
            commitments=commitments,
            cost_delta_minor=replacement_cost_minor - target.estimated_cost_minor,
            schedule_drift_min=drift,
            commitment_management_requested=bool(commitments),
        )
        arguments = {
            "activity_id": target.id,
            "replacement_activity_id": replacement.id if replacement is not None else None,
            "replacement_place_id": replacement_place.id if replacement_place is not None else None,
        }

        def execute() -> dict[str, Any]:
            target_snapshot = activity_schema(target).model_dump(mode="json")
            if replacement is not None:
                for field in (
                    "activity_type",
                    "title",
                    "place_id",
                    "city_service_id",
                    "start_at",
                    "end_at",
                    "travel_from_prev_min",
                    "estimated_cost_minor",
                    "currency",
                    "metadata_json",
                ):
                    setattr(target, field, getattr(replacement, field))
                replacement.status = "CANCELLED"
            else:
                assert replacement_place is not None
                target.title = replacement_place.name
                target.place_id = replacement_place.id
                target.city_service_id = (
                    replacement_service.id if replacement_service is not None else None
                )
                target.end_at = replacement_end_at.isoformat()
                target.travel_from_prev_min = replacement_travel_from_prev_min
                target.estimated_cost_minor = replacement_cost_minor
                target.currency = journey.budget_currency
                target.metadata_json = json.dumps(
                    {
                        "replacement_source": "SERVER_PLACE",
                        "place_id": replacement_place.id,
                        "city_service_id": target.city_service_id,
                    },
                    sort_keys=True,
                )
            from app.main import _assert_selected_plan_feasible

            plan = self.session.get(PlanVersion, journey.active_plan_version_id)
            _assert_selected_plan_feasible(self.session, journey, plan)
            return {
                "activity": activity_schema(target).model_dump(mode="json"),
                "replaced_activity": target_snapshot,
                "replacement_source": "SERVER_PLACE"
                if replacement_place is not None
                else "SCHEDULED_ACTIVITY",
            }

        return self._run_typed_mutation(
            request,
            journey,
            action=AgentAction.REPLACE_ACTIVITY,
            target_type="Activity",
            target_id=target.id,
            facts=facts,
            evidence=evidence,
            arguments=arguments,
            execute=execute,
        )

    def reschedule_activity(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        target = self._typed_activity(journey, request.activity_id)
        if request.start_at is None or request.end_at is None:
            raise HTTPException(422, "start_at and end_at are required for reschedule_activity")
        if request.end_at <= request.start_at:
            raise HTTPException(422, "end_at must be after start_at")
        commitments = self._typed_commitments(journey, target.id)
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        drift = max(
            abs(
                int(
                    (request.start_at - datetime.fromisoformat(target.start_at)).total_seconds()
                    // 60
                )
            ),
            abs(
                int((request.end_at - datetime.fromisoformat(target.end_at)).total_seconds() // 60)
            ),
        )
        facts, evidence = self._typed_facts(
            policy=policy,
            activity=target,
            commitments=commitments,
            schedule_drift_min=drift,
            commitment_management_requested=bool(commitments),
        )
        arguments = {
            "activity_id": target.id,
            "start_at": request.start_at.isoformat(),
            "end_at": request.end_at.isoformat(),
        }

        def execute() -> dict[str, Any]:
            target.start_at = request.start_at.isoformat()
            target.end_at = request.end_at.isoformat()
            for commitment in commitments:
                policy_json = json.loads(commitment.policy_json or "{}")
                policy_json.update(
                    {
                        "scheduled_start_at": target.start_at,
                        "scheduled_end_at": target.end_at,
                    }
                )
                commitment.policy_json = json.dumps(policy_json, sort_keys=True)
                commitment.updated_at = datetime.now(timezone.utc).isoformat()
            from app.main import _assert_selected_plan_feasible

            plan = self.session.get(PlanVersion, journey.active_plan_version_id)
            _assert_selected_plan_feasible(self.session, journey, plan)
            return {"activity": activity_schema(target).model_dump(mode="json")}

        return self._run_typed_mutation(
            request,
            journey,
            action=AgentAction.RESCHEDULE_ACTIVITY,
            target_type="Activity",
            target_id=target.id,
            facts=facts,
            evidence=evidence,
            arguments=arguments,
            execute=execute,
        )

    def create_booking(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        activity = self._typed_activity(journey, request.activity_id)
        commitments = self._typed_commitments(journey, activity.id)
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        facts, evidence = self._typed_facts(
            policy=policy,
            activity=activity,
            commitments=commitments,
            cost_delta_minor=activity.estimated_cost_minor,
            commitment_management_requested=True,
            booking_type="booking",
        )
        arguments = {"activity_id": activity.id, "provider_key": request.provider_key}

        def execute() -> dict[str, Any]:
            from app.commerce.service_impl import create_booking as commerce_create_booking
            from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS
            from app.main import _assert_selected_plan_feasible, _mock_booking_provider

            provider, provider_failure = _mock_booking_provider(request.provider_key)
            item = commerce_create_booking(
                self.session,
                journey_id=journey.id,
                activity_id=activity.id,
                provider=provider,
                source_strength_codes=SOURCE_COMMITMENT_STRENGTHS,
                booking_source_type_code="booking",
                assert_selected_plan_feasible=_assert_selected_plan_feasible,
                force_failure=provider_failure,
            )
            from app.agent.transactional_operator import provider_truth_label
            return {"booking_id": item.id, "status": item.status, "amount_minor": item.amount_minor, "provider_label": provider_truth_label(item.provider_key)}

        return self._run_typed_mutation(
            request,
            journey,
            action=AgentAction.CREATE_BOOKING,
            target_type="Activity",
            target_id=activity.id,
            facts=facts,
            evidence=evidence,
            arguments=arguments,
            execute=execute,
        )

    def cancel_refundable_booking(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        if request.booking_id is None:
            raise HTTPException(422, "booking_id is required for cancel_refundable_booking")
        booking = self.session.get(Booking, str(request.booking_id))
        if booking is None or booking.journey_id != journey.id:
            raise HTTPException(404, "Booking was not found for this Journey")
        activity = (
            self._typed_activity(journey, UUID(booking.activity_id))
            if booking.activity_id
            else None
        )
        commitments = self._typed_commitments(journey, activity.id) if activity else []
        cancellation = json.loads(booking.cancellation_policy_json or "{}")
        paid = self.session.scalar(
            select(PaymentTransaction.id).where(
                PaymentTransaction.booking_id == booking.id,
                PaymentTransaction.status == "SUCCEEDED",
            )
        )
        refundable = cancellation.get("cancellable_before_payment") is not False and not paid
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        facts, evidence = self._typed_facts(
            policy=policy,
            activity=activity,
            commitments=commitments,
            loss_delta_minor=max(0, int(cancellation.get("loss_minor", 0) or 0)),
            action_reversible=refundable,
            commitment_management_requested=True,
            booking_type=str(cancellation.get("booking_type") or "booking"),
        )
        arguments = {"booking_id": booking.id, "provider_key": booking.provider_key}

        def execute() -> dict[str, Any]:
            from app.commerce.service_impl import cancel_booking as commerce_cancel_booking
            from app.main import _mock_booking_provider

            provider, provider_failure = _mock_booking_provider(booking.provider_key)
            updated = commerce_cancel_booking(
                self.session,
                booking_id=booking.id,
                provider=provider,
                force_failure=provider_failure,
            )
            from app.agent.transactional_operator import provider_truth_label
            return {"booking_id": updated.id, "status": updated.status, "provider_label": provider_truth_label(updated.provider_key)}

        return self._run_typed_mutation(
            request,
            journey,
            action=AgentAction.CANCEL_REFUNDABLE_BOOKING,
            target_type="Booking",
            target_id=booking.id,
            facts=facts,
            evidence=evidence,
            arguments=arguments,
            execute=execute,
        )

    def request_commitment_change(self, request: AgentToolCall) -> dict[str, Any]:
        journey = _require_journey(self.session, request.journey_id)
        if request.commitment_id is None:
            raise HTTPException(422, "commitment_id is required for request_commitment_change")
        commitment = self.session.get(Commitment, str(request.commitment_id))
        if commitment is None or commitment.journey_id != journey.id:
            raise HTTPException(404, "Commitment was not found for this Journey")
        activity = (
            self._typed_activity(journey, UUID(commitment.activity_id))
            if commitment.activity_id
            else None
        )
        policy = policy_from_model(get_or_create_policy(self.session, journey.traveler_id))
        facts, evidence = self._typed_facts(
            policy=policy,
            activity=activity,
            commitments=[commitment],
            commitment_management_requested=True,
        )
        arguments = {"commitment_id": commitment.id, "reason": request.reason or ""}

        def execute() -> dict[str, Any]:
            record_audit(
                self.session,
                action="agent.commitment_change.requested",
                entity_type="Commitment",
                entity_id=commitment.id,
                journey_id=journey.id,
                actor_type="AI_AGENT",
                actor_id=request.agent_id,
                after={"reason": request.reason, "level": commitment.level, "status": "REQUESTED"},
            )
            return {"commitment_id": commitment.id, "status": "REQUESTED", "reason": request.reason}

        return self._run_typed_mutation(
            request,
            journey,
            action=AgentAction.REQUEST_COMMITMENT_CHANGE,
            target_type="Commitment",
            target_id=commitment.id,
            facts=facts,
            evidence=evidence,
            arguments=arguments,
            execute=execute,
        )
