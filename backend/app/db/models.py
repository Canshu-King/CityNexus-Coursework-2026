from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


EXECUTION_STATES = (
    "NOT_STARTED",
    "EN_ROUTE",
    "ARRIVED",
    "IN_PROGRESS",
    "COMPLETED",
    "SKIPPED",
)


class Traveler(Base):
    __tablename__ = "travelers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    locale: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class Preference(Base):
    __tablename__ = "preferences"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String)
    preference_key: Mapped[str] = mapped_column(String)
    value_json: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column()
    source: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("traveler_id", "category", "preference_key"),)


class JourneyOutcomeEvent(Base):
    """Append-only execution facts used by Reflect and TravelerTwin learning."""

    __tablename__ = "journey_outcome_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String)
    actor_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at_actual: Mapped[str] = mapped_column(String)
    planned_start_at: Mapped[str | None] = mapped_column(String, nullable=True)
    planned_end_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="DOMAIN", server_default="DOMAIN")
    related_proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    related_agent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)


class TravelerPreferenceEvidence(Base):
    """Inspectable, decayable evidence for a learned soft preference."""

    __tablename__ = "traveler_preference_evidence"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=True
    )
    evidence_type: Mapped[str] = mapped_column(String)
    preference_dimension: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    strength: Mapped[float] = mapped_column(default=0.0)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source: Mapped[str] = mapped_column(String)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    explanation: Mapped[str] = mapped_column(Text, default="", server_default="")
    related_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")


class TravelerCapability(Base):
    """Explicit capability facts; never populated from behavioral learning."""

    __tablename__ = "traveler_capabilities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=True
    )
    code: Mapped[str] = mapped_column(String)
    capability_class: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    params_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    scope: Mapped[str] = mapped_column(String, default="TRAVELER", server_default="TRAVELER")
    source: Mapped[str] = mapped_column(String, default="USER_CONFIRMED", server_default="USER_CONFIRMED")
    provenance: Mapped[str] = mapped_column(String, default="USER_STATED", server_default="USER_STATED")
    explicit: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("traveler_id", "journey_id", "scope", "code"),)


class TravelerTwinSnapshot(Base):
    """Materialized cross-Journey TravelerTwin projection."""

    __tablename__ = "traveler_twin_snapshots"
    traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE"), primary_key=True
    )
    explicit_preferences_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    learned_soft_preferences_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    conflicts_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    explicit_capabilities_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    confidence: Mapped[float] = mapped_column(default=0.0, server_default="0")
    evidence_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provenance_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    updated_at: Mapped[str] = mapped_column(String)


class TravelerAgentPolicy(Base):
    __tablename__ = "traveler_agent_policies"
    traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String, default="ADVISORY", server_default="ADVISORY")
    auto_accept_replan: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    max_auto_replan_cost_delta_minor: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    max_auto_loss_minor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_auto_schedule_drift_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    allowed_activity_types_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    allowed_booking_types_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    protected_commitment_levels_json: Mapped[str] = mapped_column(
        Text,
        default='["C3_Guaranteed", "C4_Anchored"]',
        server_default='["C3_Guaranteed", "C4_Anchored"]',
    )
    allow_auto_commitment_management: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    allow_auto_when_provider_degraded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    allow_auto_on_critical_event: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class Journey(Base):
    __tablename__ = "journeys"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String)
    city_code: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    start_at: Mapped[str] = mapped_column(String)
    end_at: Mapped[str] = mapped_column(String)
    budget_currency: Mapped[str] = mapped_column(String)
    budget_amount_minor: Mapped[int] = mapped_column(Integer)
    planning_mode: Mapped[str] = mapped_column(
        String, default="balanced", server_default="balanced"
    )
    status: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    current_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    # ORD-0383 slice 1: NULL means this trip was never deleted.  A deletion records the moment
    # here rather than adding a word to `status`, whose CHECK is closed and whose values say
    # what the trip was doing, not whether a recycle bin still shows it.
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # ORD-0520 中止: the moment the traveller called this trip off and the sentence they gave for
    # it.  `status` holds the fact that it is over (`CANCELLED`, a word the CHECK in
    # `backend/db/schema.sql` already carried and that no door wrote before this order); these two
    # hold why and when, which a status cannot.  NULL on every row that was never aborted.
    aborted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    abort_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class JourneyMission(Base):
    """One-run natural-language intent bounded by the global Agent policy."""

    __tablename__ = "journey_missions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    trigger_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_text: Mapped[str] = mapped_column(Text)
    objective_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    protected_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    mutable_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    intent_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    resolved_intent_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    allowed_action_classes_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    unresolved_references_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    compiler_provider_key: Mapped[str] = mapped_column(
        String, default="deterministic", server_default="deterministic"
    )
    compiler_model_key: Mapped[str] = mapped_column(
        String,
        default="mission-compiler-v2-fallback",
        server_default="mission-compiler-v2-fallback",
    )
    compiler_confidence: Mapped[float] = mapped_column(default=1.0, server_default="1.0")
    reasoning_summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    is_standing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    parent_mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    max_cost_delta_minor: Mapped[int] = mapped_column(Integer, default=0)
    max_loss_minor: Mapped[int] = mapped_column(Integer, default=0)
    max_schedule_drift_min: Mapped[int] = mapped_column(Integer, default=0)
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    requested_autonomy: Mapped[str] = mapped_column(String, default="PLAN_ONLY")
    status: Mapped[str] = mapped_column(String, default="CREATED")
    created_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRun(Base):
    """Persisted execution envelope for a model-backed Journey Agent run."""

    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("journey_missions.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    model_key: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class AgentRunStep(Base):
    """A compact, actionable trace of one model-selected tool call."""

    __tablename__ = "agent_run_steps"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="COMPLETED")
    created_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class AutopilotEnrollment(Base):
    """Per-Journey standing subscription for the process-global worker."""

    __tablename__ = "autopilot_enrollments"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    interval_seconds: Mapped[float] = mapped_column(default=60.0, server_default="60")
    provider_mesh_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    last_pulse_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class GuardianRun(Base):
    """Durable proactive scan and its idempotent recovery outcome."""

    __tablename__ = "guardian_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    standing_mission_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_missions.id", ondelete="SET NULL"), nullable=True
    )
    trigger_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("replan_proposals.id", ondelete="SET NULL"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="COMPLETED")
    action_status: Mapped[str] = mapped_column(String, default="NO_ACTION")
    evidence_fingerprint: Mapped[str] = mapped_column(String)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    active_plan_before_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_plan_after_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_kind: Mapped[str] = mapped_column(String, default="REACTIVE", server_default="REACTIVE")
    resilience_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cooldown_until: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("journey_id", "evidence_fingerprint"),)


class ProviderObservation(Base):
    """Frozen provider evidence captured by the server-owned signal pipeline."""

    __tablename__ = "provider_observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    observation_type: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    incident_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="REPLAY", server_default="REPLAY")
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)


class CitySignalFixture(Base):
    """Server-owned deterministic provider fixture used by offline demos/tests."""

    __tablename__ = "city_signal_fixtures"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    provider_key: Mapped[str] = mapped_column(String, default="city-signal-fixture")
    state: Mapped[str] = mapped_column(String, default="NORMAL")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(String)


class GuardianPulse(Base):
    """One server-owned sensing/evaluation cycle."""

    __tablename__ = "guardian_pulses"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    provider_key: Mapped[str] = mapped_column(String)
    observation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    guardian_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class JourneyResilienceSnapshot(Base):
    """Deterministic risk projection; AI may explain it but cannot rewrite it."""

    __tablename__ = "journey_resilience_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    components_json: Mapped[str] = mapped_column(Text, default="{}")
    activity_risks_json: Mapped[str] = mapped_column(Text, default="[]")
    commitment_buffers_json: Mapped[str] = mapped_column(Text, default="[]")
    failure_modes_json: Mapped[str] = mapped_column(Text, default="[]")
    recommendation_json: Mapped[str] = mapped_column(Text, default="{}")
    provenance_json: Mapped[str] = mapped_column(Text, default="[]")
    fingerprint: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("journey_id", "fingerprint"),)


class CounterfactualSimulation(Base):
    """Bounded what-if result that never mutates domain truth."""

    __tablename__ = "counterfactual_simulations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_resilience_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    scenario_key: Mapped[str] = mapped_column(String)
    scenario_type: Mapped[str] = mapped_column(String)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    feasible: Mapped[bool] = mapped_column(Boolean)
    c4_preserved: Mapped[bool] = mapped_column(Boolean)
    cost_delta_minor: Mapped[int] = mapped_column(Integer, default=0)
    schedule_drift_min: Mapped[int] = mapped_column(Integer, default=0)
    plan_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("journey_id", "scenario_key"),)


class RealPlaceCandidate(Base):
    """Provider-backed discovery evidence before/after Place materialization."""

    __tablename__ = "real_place_candidates"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=True
    )
    provider_key: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    city_code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    opening_hours_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    materialized_place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="DISCOVERED")

    __table_args__ = (UniqueConstraint("journey_id", "provider_key", "external_id"),)


class JourneyConstraint(Base):
    __tablename__ = "constraints"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    rule_code: Mapped[str] = mapped_column(String)
    params_json: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class Place(Base):
    __tablename__ = "places"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    city_code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    opening_hours_json: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String)
    # Arbitration R4 (ORD-0095-1): the geometry stored here is WGS-84 and the row says so itself,
    # instead of leaving that to whoever reads the column.  `source_coordinate_system` records
    # what a provider actually handed us (a future GCJ-02 source), so an inbound conversion can
    # never be mistaken for the system the *stored* geometry is in.  Additive: no existing column
    # changed, and legacy rows take WGS-84 through the server default.
    coordinate_system: Mapped[str] = mapped_column(String, default="WGS84", server_default="WGS84")
    source_coordinate_system: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("provider_key", "external_id"),)


class CityService(Base):
    __tablename__ = "city_services"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    place_id: Mapped[str | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    city_code: Mapped[str] = mapped_column(String)
    service_type: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    availability_json: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("provider_key", "external_id"),)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    planning_mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    parent_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    total_cost_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String)
    estimated_duration_min: Mapped[int] = mapped_column(Integer)
    utility_score: Mapped[float | None] = mapped_column(nullable=True)
    score_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mobility_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mobility_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("journey_id", "version_no"),)


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="CASCADE"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    activity_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    place_id: Mapped[str | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    city_service_id: Mapped[str | None] = mapped_column(
        ForeignKey("city_services.id", ondelete="SET NULL"), nullable=True
    )
    start_at: Mapped[str] = mapped_column(String)
    end_at: Mapped[str] = mapped_column(String)
    travel_from_prev_min: Mapped[int] = mapped_column(Integer)
    estimated_cost_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    locked: Mapped[bool] = mapped_column(Boolean)
    metadata_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("plan_version_id", "sequence_no"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    journey_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


# === WP04 COMMERCE ORM SCAFFOLD (generated) ===
class Commitment(Base):
    __tablename__ = "commitments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String)
    level: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    monetary_value_minor: Mapped[int] = mapped_column(Integer, default=0)
    monetary_value_status: Mapped[str] = mapped_column(String, default="KNOWN", server_default="KNOWN")
    currency: Mapped[str] = mapped_column(String)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    commitment_id: Mapped[str | None] = mapped_column(
        ForeignKey("commitments.id", ondelete="SET NULL"), nullable=True
    )
    provider_key: Mapped[str] = mapped_column(String)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    amount_status: Mapped[str] = mapped_column(String, default="KNOWN", server_default="KNOWN")
    currency: Mapped[str] = mapped_column(String)
    cancellation_policy_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    commitment_id: Mapped[str | None] = mapped_column(
        ForeignKey("commitments.id", ondelete="SET NULL"), nullable=True
    )
    provider_key: Mapped[str] = mapped_column(String)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String)
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class PaymentTransaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    booking_id: Mapped[str | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    provider_key: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    transaction_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("provider_key", "idempotency_key"),)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String)
    request_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String)


class IdempotencyKey(Base):
    # ORD-0340: `expires_at` stays what it always meant (how long a finished receipt may be
    # replayed). The lease is a separate clock: `claim_token` names the attempt that owns the
    # row now, and `claim_deadline` is when another attempt may take it over.
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    operation: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_deadline: Mapped[str | None] = mapped_column(String, nullable=True)


class ActionApproval(Base):
    """Server-issued, scoped user approval receipt for a gated agent action."""

    __tablename__ = "action_approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String)
    target_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    required_decision: Mapped[str] = mapped_column(String, default="REQUIRE_CONFIRMATION")
    approval_method: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    consumed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    provenance: Mapped[str] = mapped_column(String, default="SIMULATED")
    event_type: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    affected_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[str] = mapped_column(String)
    detected_at: Mapped[str] = mapped_column(String)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)


class ImpactAssessment(Base):
    """AI/deterministic impact provenance kept separate from Event truth."""

    __tablename__ = "impact_assessments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    model_key: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    maturity_stage: Mapped[str] = mapped_column(String, default="AI_ONLY")
    selected_corroboration_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    independent_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    selected_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    primary_recovery_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    secondary_risk_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    validated_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    rejected_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    semantic_reason: Mapped[str] = mapped_column(Text, default="")
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)


class JourneyTwin(Base):
    __tablename__ = "journey_twins"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    active_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    current_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    latest_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    projection_status: Mapped[str] = mapped_column(String)
    snapshot_at: Mapped[str] = mapped_column(String)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    upcoming_commitments_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[str] = mapped_column(String)


class ReplanProposal(Base):
    __tablename__ = "replan_proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    trigger_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    base_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    proposed_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String)
    hard_constraints_passed: Mapped[bool] = mapped_column(Boolean)
    locked_commitments_preserved: Mapped[bool] = mapped_column(Boolean)
    utility_score: Mapped[float] = mapped_column()
    score_breakdown_json: Mapped[str] = mapped_column(Text)
    diff_json: Mapped[str] = mapped_column(Text)
    explanation_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)


class JourneyTripContext(Base):
    """Multi-day arrival/departure anchors and party context for V8 planning."""

    __tablename__ = "journey_trip_contexts"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    arrival_anchor_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    departure_anchor_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    party_size: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    occupancy_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    stay_budget_amount_minor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stay_budget_currency: Mapped[str] = mapped_column(String, default="CNY", server_default="CNY")
    # ORD-0355: whether the traveller ever named a lodging budget at all.  The amount column
    # above cannot carry that fact - `0` there means both "the traveller said zero" and "nobody
    # asked", and the second reading was being sent to suppliers as the first.  Three states on
    # purpose: True declared, False not declared, NULL recorded by a build that never asked and
    # therefore cannot say.  NULL is deliberately not folded into False, because a legacy row
    # may hold an amount a traveller really typed.
    stay_budget_declared: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AccommodationOfferSnapshot(Base):
    """Immutable captured or live accommodation evidence used by stay-aware planning."""

    __tablename__ = "accommodation_offer_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    external_offer_id: Mapped[str] = mapped_column(String)
    property_name: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    coordinate_crs: Mapped[str] = mapped_column(String, default="WGS84", server_default="WGS84")
    room_type: Mapped[str] = mapped_column(String)
    occupancy_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    check_in_at: Mapped[str] = mapped_column(String)
    check_out_at: Mapped[str] = mapped_column(String)
    nightly_amount_minor: Mapped[int] = mapped_column(Integer)
    taxes_amount_minor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String)
    cancellation_policy_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    luggage_drop_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    refundable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="CAPTURED", server_default="CAPTURED")

    __table_args__ = (UniqueConstraint("journey_id", "provider_key", "external_offer_id"),)


class StayJourneyOption(Base):
    """Ranked joint stay + journey option produced by the bounded V8 optimizer."""

    __tablename__ = "stay_journey_options"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    accommodation_offer_id: Mapped[str] = mapped_column(
        ForeignKey("accommodation_offer_snapshots.id", ondelete="CASCADE")
    )
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String, default="CANDIDATE", server_default="CANDIDATE")
    stay_total_minor: Mapped[int] = mapped_column(Integer)
    daily_travel_minor: Mapped[int] = mapped_column(Integer)
    mobility_total_minor: Mapped[int] = mapped_column(Integer)
    walking_min: Mapped[int] = mapped_column(Integer)
    protected_margin_min: Mapped[int] = mapped_column(Integer)
    arrival_fit: Mapped[float] = mapped_column()
    departure_fit: Mapped[float] = mapped_column()
    cancellation_score: Mapped[float] = mapped_column()
    days_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    leg_modes_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    journey_impact_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    hard_constraints_passed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    optimizer_kind: Mapped[str] = mapped_column(
        String, default="BOUNDED_DETERMINISTIC", server_default="BOUNDED_DETERMINISTIC"
    )
    weather_state: Mapped[str] = mapped_column(String, default="NORMAL", server_default="NORMAL")
    materialized_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("journey_id", "rank"),)


class MobilityFact(Base):
    """Frozen, mode-aware route facts; unknown provider costs remain unknown."""

    __tablename__ = "mobility_facts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    option_id: Mapped[str | None] = mapped_column(
        ForeignKey("stay_journey_options.id", ondelete="CASCADE"), nullable=True
    )
    from_key: Mapped[str] = mapped_column(String)
    to_key: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    duration_min: Mapped[int] = mapped_column(Integer)
    distance_m: Mapped[int] = mapped_column(Integer)
    cost_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_status: Mapped[str] = mapped_column(String)
    walking_min: Mapped[int] = mapped_column(Integer)
    transfers: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    weather_exposure: Mapped[str] = mapped_column(String)
    reliability_score: Mapped[float] = mapped_column()
    provider_key: Mapped[str] = mapped_column(String)
    provenance: Mapped[str] = mapped_column(String)
    geometry_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    crs: Mapped[str] = mapped_column(String, default="WGS84", server_default="WGS84")
    paid_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("journey_id", "from_key", "to_key", "mode"),)


class JourneyLeg(Base):
    """One concrete provider-backed connection in an authoritative PlanVersion."""

    __tablename__ = "journey_legs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="CASCADE"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    from_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    to_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    from_key: Mapped[str] = mapped_column(String)
    to_key: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    depart_at: Mapped[str] = mapped_column(String)
    arrive_at: Mapped[str] = mapped_column(String)
    duration_min: Mapped[int] = mapped_column(Integer)
    distance_m: Mapped[int] = mapped_column(Integer)
    cost_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_status: Mapped[str] = mapped_column(String)
    walking_min: Mapped[int] = mapped_column(Integer)
    transfers: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reliability_score: Mapped[float] = mapped_column()
    weather_exposure: Mapped[str] = mapped_column(String)
    geometry_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    crs: Mapped[str] = mapped_column(String, default="WGS84", server_default="WGS84")
    provider_key: Mapped[str] = mapped_column(String)
    provenance: Mapped[str] = mapped_column(String)
    provider_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("mobility_facts.id", ondelete="SET NULL"), nullable=True
    )
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("plan_version_id", "sequence_no"),)


class JourneyOperationsContext(Base):
    __tablename__ = "journey_operations_contexts"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    baggage_present: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    baggage_size_class: Mapped[str] = mapped_column(String, default="NONE", server_default="NONE")
    luggage_drop_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    constraints_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class ExternalReservationDraft(Base):
    __tablename__ = "external_reservation_drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    artifact_type: Mapped[str] = mapped_column(String)
    raw_payload_json: Mapped[str] = mapped_column(Text)
    extracted_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    unresolved_fields_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    extraction_mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="DRAFT", server_default="DRAFT")
    created_at: Mapped[str] = mapped_column(String)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class TransportReservation(Base):
    __tablename__ = "transport_reservations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_reservation_drafts.id", ondelete="SET NULL"), nullable=True
    )
    reservation_type: Mapped[str] = mapped_column(String)
    provider_key: Mapped[str] = mapped_column(String)
    confirmation_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    origin_json: Mapped[str] = mapped_column(Text)
    destination_json: Mapped[str] = mapped_column(Text)
    scheduled_departure_at: Mapped[str] = mapped_column(String)
    scheduled_arrival_at: Mapped[str] = mapped_column(String)
    estimated_arrival_at: Mapped[str | None] = mapped_column(String, nullable=True)
    actual_arrival_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    realtime_status: Mapped[str] = mapped_column(
        String, default="UNKNOWN_REALTIME", server_default="UNKNOWN_REALTIME"
    )
    passenger_scope_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    change_policy_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    price_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    price_status: Mapped[str] = mapped_column(String, default="UNKNOWN", server_default="UNKNOWN")
    source_provenance: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    booking_id: Mapped[str | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    commitment_id: Mapped[str | None] = mapped_column(
        ForeignKey("commitments.id", ondelete="SET NULL"), nullable=True
    )
    __table_args__ = (UniqueConstraint("journey_id", "confirmation_ref"),)


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    priority: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    delivery_channels_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class OfflineJourneySnapshot(Base):
    __tablename__ = "offline_journey_snapshots"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    valid_until: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text)
    provider_freshness_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    updated_at: Mapped[str] = mapped_column(String)


class LocationObservation(Base):
    __tablename__ = "location_observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    accuracy_m: Mapped[float] = mapped_column()
    captured_at: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(
        String, default="FOREGROUND_WEB", server_default="FOREGROUND_WEB"
    )


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    endpoint_hash: Mapped[str] = mapped_column(String, unique=True)
    subscription_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class EdgeDevice(Base):
    __tablename__ = "edge_devices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class EdgeAction(Base):
    __tablename__ = "edge_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("edge_devices.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)


class EdgeObservation(Base):
    __tablename__ = "edge_observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("edge_devices.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    observation_type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)


class WebObservation(Base):
    """Inspectable, non-authoritative evidence from DeepSeek server-side search."""

    __tablename__ = "web_observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    source_class: Mapped[str] = mapped_column(String, default="OPEN_WEB", server_default="OPEN_WEB")
    provider_key: Mapped[str] = mapped_column(String)
    model_key: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    evidence_fingerprint: Mapped[str] = mapped_column(String)
    content_excerpt: Mapped[str] = mapped_column(Text, default="", server_default="")
    authority_status: Mapped[str] = mapped_column(
        String, default="NON_AUTHORITATIVE", server_default="NON_AUTHORITATIVE"
    )
    created_at: Mapped[str] = mapped_column(String)


class StayGenerationOperation(Base):
    """One asynchronous stay-generation job, with the state machine it is allowed to run.

    ORD-0057 (arbitration `5749459724` §1): a cold chain of Photon -> three Overpass
    mirrors -> Nominatim was measured at ~115s inside a POST, so the request had to stop
    being the thing that carries the work.  This row is what survives a reload: the client
    keeps an `operation_id` and reads *this* record, which is also why the state, the phase
    and the result live in the database rather than in a process-local future.
    """

    __tablename__ = "stay_generation_operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), index=True
    )
    # Derived from the inputs that can change the answer.  Two taps of the same button
    # produce the same fingerprint, and the partial unique index below is what stops the
    # second one from starting a second provider chain.
    fingerprint: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="QUEUED", server_default="QUEUED")
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # `outcome` is the four-way answer that `top_k: []` could never express.
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    queued_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    abandon_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String)


class CatalogHydrationOperation(Base):
    """One asynchronous "prepare this destination's catalog" job, and the state it may hold.

    ORD-0095-1 (CITYNEXUS-ANYWHERE §6/§7).  The lifecycle is deliberately *not* invented here:
    `status` plus `phase` are the two axes `stay_generation_operations` already proved - `phase`
    says how far the work genuinely got and `status` says whether the job may still move -
    because a single axis cannot tell "abandoned but still running" from "finished".  The one
    addition is the arbitration's structural terminal state `UNAVAILABLE`: "no traceable source
    for this destination" is a fact about the destination, not a timeout, and it must not be
    written as `FAILED` (retryable) nor as a runtime failure.

    `fingerprint` is the durable idempotency key: canonical destination + the three policy
    versions that can change the answer.  The partial unique index in `db/schema.sql` is what
    enforces "at most one open job per fingerprint" - a process-local lock cannot, since two
    uvicorn workers do not share one.
    """

    __tablename__ = "catalog_hydration_operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String)
    # Null is a legal, expected value: "this request has no destination identity yet" is a fact
    # about the request, and the NOT NULL that used to sit here is what forced the build to write
    # `pending:<the traveller's words>` into an identity column (ORD-0167 / arbiter `e4b7faa3`).
    canonical_destination_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The user's own text, byte for byte, for the screen and the audit trail.  Never folded,
    # never punctuation-stripped, and never an identifier.
    pending_destination_label: Mapped[str | None] = mapped_column(String, nullable=True)
    # The folded form the resolver matched on - kept apart so a match can be explained without
    # claiming the machine saw what the traveller typed.  Also not an identifier.
    resolver_query_normalized: Mapped[str | None] = mapped_column(String, nullable=True)
    # CORRELATION ONLY, NEVER IDENTITY: a token that ties polls, abandons and audit lines to this
    # operation.  It must not be joined to `places`, must not reach the planner, and must not
    # appear in a fingerprint - convergence for a repeated request is the fingerprint's job.
    destination_pending_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # Whether `pending_destination_label` is the exact input or the best a legacy row survived.
    pending_label_fidelity: Mapped[str | None] = mapped_column(String, nullable=True)
    # Audit, not state: the key this row carried before the identity fix recomputed it, so an
    # old job can still be traced to the fingerprint it was opened under.
    legacy_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    catalog_schema_version: Mapped[str] = mapped_column(String)
    readiness_policy_version: Mapped[str] = mapped_column(String)
    provider_policy_version: Mapped[str] = mapped_column(String)
    # Optional: hydration is a destination-scoped job, so an id-less request still converges on
    # the same row.  Where a journey asked for it, the pointer records who is waiting.
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String, default="QUEUED", server_default="QUEUED")
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    queued_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    abandon_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String)


class JourneyShareGrant(Base):
    """One share grant: a token, a visibility, and the two moments that end it.

    `expires_at` / `revoked_at` are NULL when that end was never set.  They are not given a
    DEFAULT and never carry a placeholder instant, so "no deadline" is not stored as a date.
    """

    __tablename__ = "journey_share_grants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE")
    )
    token: Mapped[str] = mapped_column(String, unique=True)
    visibility: Mapped[str] = mapped_column(String)
    # ORD-0466: a grant can be cut for one plan version instead of for the journey as it stands.
    # NULL is not "no version picked yet" -- it is the journey-level grant every pre-ORD-0466 row
    # already is, so no old row has to be re-read as an unfinished choice.
    #
    # Deliberately a plain column, not a ForeignKey: each thing a foreign key would do here breaks a
    # promise the share doors already keep.  ON DELETE CASCADE deletes the grant with the version, so
    # a holder presenting the link is told 404 "never existed" when the real fact is 409 "the version
    # behind it is gone" -- the two-number rule ORD-0434 set (`backend/app/main.py`, the token read).
    # ON DELETE SET NULL silently widens a version grant into a whole-journey grant.  NO ACTION lets a
    # grant block somebody deleting a version.  So the id is checked where it is created, named where
    # it stops resolving, and the row survives to be revoked.
    plan_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class JourneyMember(Base):
    """One traveller's place in one journey: a role, an optional lifetime, a withdrawal moment.

    The unique index in `db/schema.sql` is the single-set promise; `expires_at` NULL means the
    membership was given no end, and `removed_at` NULL means it was never taken away.
    """

    __tablename__ = "journey_members"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String)
    invited_by: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class JourneyRoleCapability(Base):
    """Role -> one capability, with the message key that states the role's scope in a sentence.

    The table holds a key, never the sentence: a server-authored string reaching a screen is the
    recurring defect, so the words stay in the client's dictionary.
    """

    __tablename__ = "journey_role_capabilities"
    role: Mapped[str] = mapped_column(String, primary_key=True)
    capability: Mapped[str] = mapped_column(String, primary_key=True)
    scope_message_key: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class JourneyCheckin(Base):
    """ORD-0476: one footprint stamp -- a traveller was at one target of one journey.

    Built from zero.  The word `check_in` existed on this layer only as
    :attr:`Stay.check_in_at` -- an accommodation arrival time, a different fact about a different
    thing -- so nothing here inherits a shape.

    The two target columns are nullable *and* cascade to NULL, and that pair of choices is the
    measured one: `activities` rows are deleted when a plan version is superseded
    (`backend/app/main.py`, `backend/app/operations/v9.py`) and `places` rows are pruned by
    the catalog.  A RESTRICT would have made those doors fail, and a CASCADE would have let a replan
    erase the traveller's own history.  So the stamp outlives its target and keeps saying which one
    it was, through `target_kind` and `target_key`, which the door writes once and nothing later
    re-derives -- after a cascade there is no row left to derive from.

    `target_key` is also what makes 重复打卡 durable.  A `UNIQUE (journey_id, activity_id)` over a
    NULL-able column is not a constraint SQLite enforces, because NULLs are distinct, and a SET NULL
    cascade could mint or remove a duplicate long after the tap.  One immutable key per stamp,
    unique per journey, is the half a second uvicorn worker cannot race past.
    """

    __tablename__ = "journey_checkins"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE")
    )
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    place_id: Mapped[str | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    target_kind: Mapped[str] = mapped_column(String)
    target_key: Mapped[str] = mapped_column(String)
    stamped_at: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(
        String, default="FOREGROUND_WEB", server_default="FOREGROUND_WEB"
    )

    # Only the UNIQUE is declared here, as on the sibling container tables: the scan index belongs
    # to `backend/db/schema.sql`, which is what `ensure_schema()` actually applies.
    __table_args__ = (UniqueConstraint("journey_id", "target_key"),)
class CollectionFolder(Base):
    """ORD-0444: a named shelf a traveller keeps places on -- the 「收藏」 container.

    Three columns carry the shelf itself: its name, whose it is, and the two moments.  The owner is
    `NOT NULL` on purpose.  A nullable `traveler_id` would have been worse than none at all -- it
    would look like ownership while nothing wrote it, and 「我的收藏」 would then be a claim the row
    cannot support.  It is an *asserted* membership, not an authenticated one: the creating caller
    names the traveller and the door checks that id exists in `travelers`, which is the same stance
    `journey_share_grants.created_by` takes while the anonymous read remains the default.

    `name` is not unique.  Two shelves can both read 「周末」, so the id is what addresses one, and
    the door that adds a place takes the id and never a name.
    """

    __tablename__ = "collection_folders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    owner_traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class CollectionItem(Base):
    """One place on one shelf, plus the note its owner wrote about it.

    The row stores a *reference* to `places` and no copy of the place's fields, so a shelf cannot
    show a name that the place row has since changed.  `note` is nullable because "no note was
    written" is a fact a screen has to be able to tell apart from "an empty note".

    `UNIQUE(folder_id, place_id)` is the durable half of the duplicate refusal: two workers
    handling two taps of the same place onto the same shelf cannot both win, which a
    select-then-insert check inside one process could not promise.
    """

    __tablename__ = "collection_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey("collection_folders.id", ondelete="CASCADE"))
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id"))
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[str] = mapped_column(String)

    __table_args__ = (UniqueConstraint("folder_id", "place_id"),)
