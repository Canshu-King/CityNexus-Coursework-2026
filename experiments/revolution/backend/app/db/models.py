from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
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


class OvernightStewardCycle(Base):
    """One durable closeout and morning handoff per Journey local day and plan."""

    __tablename__ = "overnight_steward_cycles"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_key: Mapped[str] = mapped_column(String, unique=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    local_date: Mapped[str] = mapped_column(String)
    next_local_date: Mapped[str] = mapped_column(String)
    plan_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="DAY_CLOSING", server_default="DAY_CLOSING")
    consumed_outcome_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    learning_summary_by_traveler_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    briefing_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    morning_ready_at: Mapped[str | None] = mapped_column(String, nullable=True)


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
    clarification_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")


class JourneyIntent(Base):
    """A member-scoped, time-bounded steering request for one running Journey."""

    __tablename__ = "journey_intents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    created_by_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    created_by_member_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_text: Mapped[str] = mapped_column(Text)
    intent_kind: Mapped[str] = mapped_column(String)
    scope_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_start_at: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_end_at: Mapped[str | None] = mapped_column(String, nullable=True)
    local_date: Mapped[str | None] = mapped_column(String, nullable=True)
    target_activity_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    canonical_dimensions_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    preference_hints_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    semantic_envelope_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    source: Mapped[str] = mapped_column(String, default="USER_TEMPORARY_INTENT", server_default="USER_TEMPORARY_INTENT")
    compiler_provider_key: Mapped[str] = mapped_column(String, default="deterministic", server_default="deterministic")
    compiler_model_key: Mapped[str] = mapped_column(String, default="fallback", server_default="fallback")
    compiler_confidence: Mapped[float] = mapped_column(default=0.0, server_default="0")
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    evaluation_state: Mapped[str] = mapped_column(String, default="QUEUED", server_default="QUEUED")
    semantic_fingerprint: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    superseded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expired_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("journey_id", "semantic_fingerprint"),
        Index("idx_journey_intents_active_scope", "journey_id", "status", "scope_start_at", "scope_end_at"),
    )


class MemberSideQuest(Base):
    """Private execution state for one traveller's stop during an R63 split."""

    __tablename__ = "member_side_quests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(String)
    traveler_id: Mapped[str] = mapped_column(String)
    planned_separation_intent_id: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str] = mapped_column(String)
    rendezvous_activity_id: Mapped[str] = mapped_column(String)
    rendezvous_place_id: Mapped[str] = mapped_column(String)
    rendezvous_title: Mapped[str] = mapped_column(String)
    rendezvous_place_name: Mapped[str] = mapped_column(String)
    rendezvous_scheduled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    side_place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    side_title: Mapped[str] = mapped_column(Text)
    destination_authority: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    arrived_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    invalidated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "destination_authority IN ('EXACT_KNOWN_PLACE', 'DESCRIPTIVE_ONLY')",
            name="ck_member_side_quests_destination_authority",
        ),
        CheckConstraint(
            "state IN ('DESTINATION_UNRESOLVED', 'READY', 'EN_ROUTE', 'ARRIVED', "
            "'COMPLETED', 'CANCELLED', 'INVALIDATED')",
            name="ck_member_side_quests_state",
        ),
        Index(
            "ux_member_side_quests_one_open_per_split",
            "journey_id",
            "member_id",
            "planned_separation_intent_id",
            unique=True,
            sqlite_where=text(
                "state IN ('DESTINATION_UNRESOLVED', 'READY', 'EN_ROUTE', 'ARRIVED')"
            ),
        ),
        Index(
            "idx_member_side_quests_journey_member_updated",
            "journey_id",
            "member_id",
            "updated_at",
        ),
    )


class MemberLiveMomentScoutReceipt(Base):
    """Short-lived member-private results from an explicitly consented OSM lookup."""

    __tablename__ = "member_live_moment_scout_receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(String)
    traveler_id: Mapped[str] = mapped_column(String)
    location_observation_id: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str] = mapped_column(String)
    split_intent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    rendezvous_activity_id: Mapped[str] = mapped_column(String)
    rendezvous_place_id: Mapped[str] = mapped_column(String)
    budget_minutes: Mapped[int] = mapped_column(Integer)
    interest: Mapped[str] = mapped_column(String)
    candidates_json: Mapped[str] = mapped_column(Text)
    provider_attempts_json: Mapped[str] = mapped_column(Text)
    provider_key: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="OPEN", server_default="OPEN")
    selected_candidate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    side_quest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'SELECTING', 'SELECTED', 'EXPIRED')",
            name="ck_member_live_moment_scout_receipts_status",
        ),
        Index(
            "idx_member_live_moment_scout_receipts_owner_expiry",
            "journey_id",
            "member_id",
            "expires_at",
        ),
    )


class MemberSideQuestReturnReport(Base):
    """Member-owned, idempotent report that a completed private stop is heading back."""

    __tablename__ = "member_side_quest_return_reports"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(String)
    traveler_id: Mapped[str] = mapped_column(String)
    side_quest_id: Mapped[str] = mapped_column(
        ForeignKey("member_side_quests.id", ondelete="CASCADE"), unique=True
    )
    planned_separation_intent_id: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String,
        default="RETURN_STARTED_REPORTED",
        server_default="RETURN_STARTED_REPORTED",
    )
    reported_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status = 'RETURN_STARTED_REPORTED'",
            name="ck_member_side_quest_return_reports_status",
        ),
        Index(
            "idx_member_side_quest_return_reports_journey_member",
            "journey_id",
            "member_id",
            "reported_at",
        ),
    )


class PlannedSplitMeetupHandshake(Base):
    """A member-reported meetup arrival acknowledged by another current member."""

    __tablename__ = "planned_split_meetup_handshakes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    owner_member_id: Mapped[str] = mapped_column(String)
    owner_traveler_id: Mapped[str] = mapped_column(String)
    planned_separation_intent_id: Mapped[str] = mapped_column(String)
    member_side_quest_id: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str] = mapped_column(String)
    rendezvous_activity_id: Mapped[str] = mapped_column(String)
    rendezvous_place_id: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    reported_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    responder_member_id: Mapped[str | None] = mapped_column(String, nullable=True)
    responder_traveler_id: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'PEER_ACKNOWLEDGED', 'NOT_CONFIRMED', 'EXPIRED', 'INVALIDATED')",
            name="ck_planned_split_meetup_handshakes_state",
        ),
        Index(
            "ux_planned_split_meetup_handshakes_one_pending_per_owner",
            "journey_id",
            "owner_member_id",
            unique=True,
            sqlite_where=text("state = 'PENDING'"),
        ),
        Index(
            "idx_planned_split_meetup_handshakes_journey_reported",
            "journey_id",
            "reported_at",
        ),
    )


class MemberPlaceInvitation(Base):
    """A member's explicit, time-bounded disclosure of one public Place to companions."""

    __tablename__ = "member_place_invitations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    owner_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    owner_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    owner_split_intent_id: Mapped[str] = mapped_column(ForeignKey("journey_intents.id", ondelete="CASCADE"))
    owner_side_quest_id: Mapped[str] = mapped_column(ForeignKey("member_side_quests.id", ondelete="CASCADE"))
    active_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="CASCADE"))
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    place_provider_key: Mapped[str] = mapped_column(String)
    place_external_id: Mapped[str] = mapped_column(String)
    rendezvous_activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id", ondelete="CASCADE"))
    rendezvous_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    disclosure_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    state: Mapped[str] = mapped_column(String, default="OPEN", server_default="OPEN")
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    withdrawn_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "state IN ('OPEN', 'WITHDRAWN', 'EXPIRED', 'STALE')",
            name="ck_member_place_invitations_state",
        ),
        Index(
            "ux_member_place_invitations_one_open_per_quest",
            "journey_id",
            "owner_side_quest_id",
            unique=True,
            sqlite_where=text("state = 'OPEN'"),
        ),
        Index(
            "idx_member_place_invitations_journey_owner_created",
            "journey_id",
            "owner_member_id",
            "created_at",
        ),
    )


class MemberPlaceInvitationRecipient(Base):
    """One addressed companion's own response and resulting R63/R64 execution objects."""

    __tablename__ = "member_place_invitation_recipients"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("member_place_invitations.id", ondelete="CASCADE")
    )
    recipient_member_id: Mapped[str] = mapped_column(
        ForeignKey("journey_members.id", ondelete="CASCADE")
    )
    recipient_traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(String, default="INVITED", server_default="INVITED")
    invited_at: Mapped[str] = mapped_column(String)
    responded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    accepted_split_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_intents.id", ondelete="SET NULL"), nullable=True
    )
    accepted_side_quest_id: Mapped[str | None] = mapped_column(
        ForeignKey("member_side_quests.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("invitation_id", "recipient_member_id"),
        CheckConstraint(
            "state IN ('INVITED', 'ACCEPTANCE_NEEDS_COMPLETION', 'ACCEPTED_OWN_STOP_READY', 'DECLINED')",
            name="ck_member_place_invitation_recipients_state",
        ),
        Index(
            "idx_member_place_invitation_recipients_member_state",
            "recipient_member_id",
            "state",
            "invited_at",
        ),
    )


class MemberPlaceInvitationProgress(Base):
    """A participant's explicit, revocable disclosure about one invited public Place."""

    __tablename__ = "member_place_invitation_progress"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("member_place_invitations.id", ondelete="CASCADE")
    )
    member_id: Mapped[str] = mapped_column(String)
    traveler_id: Mapped[str] = mapped_column(String)
    signal_type: Mapped[str] = mapped_column(String)
    active_plan_version_id: Mapped[str] = mapped_column(String)
    planned_separation_intent_id: Mapped[str] = mapped_column(String)
    side_quest_id: Mapped[str] = mapped_column(String)
    place_id: Mapped[str] = mapped_column(String)
    consent_version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    reported_at: Mapped[str] = mapped_column(String)
    withdrawn_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint(
            "invitation_id", "member_id", "signal_type",
            name="ux_member_place_invitation_progress_member_signal",
        ),
        CheckConstraint(
            "signal_type IN ('AT_INVITED_PLACE_SELF_REPORTED', 'HEADING_BACK_SELF_REPORTED')",
            name="ck_member_place_invitation_progress_signal",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'WITHDRAWN')",
            name="ck_member_place_invitation_progress_status",
        ),
        Index(
            "idx_member_place_invitation_progress_invitation_status",
            "invitation_id",
            "status",
            "signal_type",
        ),
    )


class MemberPlaceGroupShareConsent(Base):
    """A source-owned, revocable receipt for widening one exact Place disclosure."""

    __tablename__ = "member_place_group_share_consents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("member_place_invitations.id", ondelete="CASCADE")
    )
    owner_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    owner_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    owner_split_intent_id: Mapped[str] = mapped_column(ForeignKey("journey_intents.id", ondelete="CASCADE"))
    owner_side_quest_id: Mapped[str] = mapped_column(ForeignKey("member_side_quests.id", ondelete="CASCADE"))
    base_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="RESTRICT"))
    base_cursor_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    place_snapshot_json: Mapped[str] = mapped_column(Text)
    rendezvous_activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id", ondelete="RESTRICT"))
    rendezvous_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    audience_snapshot_json: Mapped[str] = mapped_column(Text)
    audience_snapshot_hash: Mapped[str] = mapped_column(String)
    plan_snapshot_hash: Mapped[str] = mapped_column(String)
    source_snapshot_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    consented_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    withdrawn_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'WITHDRAWN', 'EXPIRED', 'STALE')",
            name="ck_member_place_group_share_consents_status",
        ),
        Index(
            "ux_member_place_group_share_consents_one_active",
            "journey_id",
            "invitation_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "idx_member_place_group_share_consents_invitation_created",
            "journey_id",
            "invitation_id",
            "created_at",
        ),
    )


class MemberPlacePromotion(Base):
    """One explicitly proposed, collectively authorized shared stop."""

    __tablename__ = "member_place_promotions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("member_place_invitations.id", ondelete="CASCADE")
    )
    actor_member_id: Mapped[str] = mapped_column(
        ForeignKey("journey_members.id", ondelete="CASCADE")
    )
    actor_traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE")
    )
    active_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    rendezvous_activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="RESTRICT")
    )
    current_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    trigger_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("replan_proposals.id", ondelete="SET NULL"), nullable=True
    )
    mission_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_missions.id", ondelete="SET NULL"), nullable=True
    )
    request_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    dwell_minutes: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="PENDING_ROUTE", server_default="PENDING_ROUTE")
    state_detail_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    decision_epoch: Mapped[str | None] = mapped_column(String, nullable=True)
    material_world_epoch: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_surface_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "dwell_minutes IN (10, 20, 30)",
            name="ck_member_place_promotions_dwell",
        ),
        CheckConstraint(
            "state IN ('PENDING_ROUTE', 'WAITING_FOR_MEMBERS', 'AUTHORIZED', 'APPLIED', "
            "'DECLINED', 'STALE', 'BLOCKED')",
            name="ck_member_place_promotions_state",
        ),
        UniqueConstraint(
            "journey_id", "request_key", name="ux_member_place_promotions_request_key"
        ),
        Index(
            "ux_member_place_promotions_one_pending",
            "journey_id",
            "active_plan_version_id",
            "invitation_id",
            "place_id",
            unique=True,
            sqlite_where=text(
                "state IN ('PENDING_ROUTE', 'WAITING_FOR_MEMBERS', 'AUTHORIZED')"
            ),
            postgresql_where=text(
                "state IN ('PENDING_ROUTE', 'WAITING_FOR_MEMBERS', 'AUTHORIZED')"
            ),
        ),
        Index(
            "idx_member_place_promotions_journey_state_updated",
            "journey_id",
            "state",
            "updated_at",
        ),
    )


class SharedStopCoPlanningGrant(Base):
    """A member's short-lived, one-promotion authority to answer an R73 decision."""

    __tablename__ = "shared_stop_coplanning_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    invitation_id: Mapped[str] = mapped_column(
        ForeignKey("member_place_invitations.id", ondelete="CASCADE")
    )
    base_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    grantee_member_id: Mapped[str] = mapped_column(
        ForeignKey("journey_members.id", ondelete="CASCADE")
    )
    grantee_traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE")
    )
    grantor_member_id: Mapped[str] = mapped_column(
        ForeignKey("journey_members.id", ondelete="CASCADE")
    )
    grantor_traveler_id: Mapped[str] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE")
    )
    promotion_id: Mapped[str | None] = mapped_column(
        ForeignKey("member_place_promotions.id", ondelete="CASCADE"), nullable=True
    )
    active_slot: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    purpose: Mapped[str] = mapped_column(String, default="R73_SHARED_STOP_ONLY")
    status: Mapped[str] = mapped_column(String, default="OFFERED", server_default="OFFERED")
    request_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    declined_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_epoch: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "purpose = 'R73_SHARED_STOP_ONLY'",
            name="ck_shared_stop_coplanning_grant_purpose",
        ),
        CheckConstraint(
            "status IN ('OFFERED', 'ACCEPTED', 'DECLINED', 'REVOKED')",
            name="ck_shared_stop_coplanning_grant_status",
        ),
        UniqueConstraint(
            "journey_id", "request_key", name="ux_shared_stop_coplanning_request_key"
        ),
        Index(
            "idx_shared_stop_coplanning_grantee_scope",
            "journey_id",
            "invitation_id",
            "base_plan_version_id",
            "grantee_member_id",
            "status",
        ),
        Index(
            "ux_shared_stop_coplanning_one_unbound_active_grant",
            "journey_id",
            "invitation_id",
            "base_plan_version_id",
            "grantee_member_id",
            unique=True,
            sqlite_where=text("active_slot = 1"),
            postgresql_where=text("active_slot = true"),
        ),
        Index(
            "idx_shared_stop_coplanning_promotion",
            "promotion_id",
            "decision_epoch",
        ),
    )


class RecoveryEpoch(Base):
    """Server-owned recovery reality binding for one Journey."""

    __tablename__ = "recovery_epochs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    generation: Mapped[int] = mapped_column(Integer)
    active_plan_version_id_at_open: Mapped[str | None] = mapped_column(String, nullable=True)
    material_world_epoch_at_open: Mapped[str | None] = mapped_column(String, nullable=True)
    recovery_reality_fingerprint: Mapped[str] = mapped_column(String)
    triggering_event_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    status: Mapped[str] = mapped_column(String, default="OPEN", server_default="OPEN")
    opened_at: Mapped[str] = mapped_column(String)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_by_recovery_epoch_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("journey_id", "generation"),
        Index(
            "ux_recovery_epochs_one_open",
            "journey_id",
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
        Index("idx_recovery_epochs_journey_status", "journey_id", "status", "generation"),
    )


class OperatorRecoveryResume(Base):
    """Durable resume intent for one unresolved Operator recovery epoch."""

    __tablename__ = "operator_recovery_resumes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    recovery_epoch_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="CASCADE")
    )
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    incident_fingerprint: Mapped[str] = mapped_column(String)
    standing_mission_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_missions.id", ondelete="SET NULL"), nullable=True
    )
    recovery_mission_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_missions.id", ondelete="SET NULL"), nullable=True
    )
    guardian_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("guardian_runs.id", ondelete="SET NULL"), nullable=True
    )
    active_plan_version_id_at_block: Mapped[str | None] = mapped_column(String, nullable=True)
    material_world_epoch_at_block: Mapped[str | None] = mapped_column(String, nullable=True)
    blocked_reason: Mapped[str] = mapped_column(String)
    failure_classification: Mapped[str] = mapped_column(String)
    provider_key: Mapped[str | None] = mapped_column(String, nullable=True)
    model_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    next_retry_at: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    safe_to_resume: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    owner_runtime_key: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_claimed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    resumed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("journey_id", "recovery_epoch_id"),
        Index("idx_operator_recovery_resumes_due", "journey_id", "status", "next_retry_at"),
    )


class RecoveryDiscoveryAttempt(Base):
    """Durable bounded open-world discovery inside one recovery epoch."""

    __tablename__ = "recovery_discovery_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    recovery_epoch_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="CASCADE")
    )
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    guardian_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    query_hash: Mapped[str] = mapped_column(String)
    query: Mapped[str] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    radius_m: Mapped[int] = mapped_column(Integer, default=2500, server_default="2500")
    candidate_limit: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    status: Mapped[str] = mapped_column(String, default="COMPLETED", server_default="COMPLETED")
    provider_mode: Mapped[str] = mapped_column(String)
    runtime_provenance: Mapped[str] = mapped_column(String)
    raw_candidate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    materialized_place_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    deep_candidate_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    provider_status_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("recovery_epoch_id", "query_hash"),
        Index("idx_recovery_discovery_epoch", "journey_id", "recovery_epoch_id", "created_at"),
    )


class RecoveryExecutionPlan(Base):
    """Frozen server-owned commerce work for one accepted recovery."""

    __tablename__ = "recovery_execution_plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    recovery_epoch_id: Mapped[str] = mapped_column(ForeignKey("recovery_epochs.id", ondelete="CASCADE"))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("replan_proposals.id", ondelete="CASCADE"))
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="PLANNED", server_default="PLANNED")
    semantic_fingerprint: Mapped[str] = mapped_column(String)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("action_approvals.id", ondelete="SET NULL"), nullable=True)
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("recovery_epoch_id", "proposal_id"),)


class RecoveryExecutionAction(Base):
    """One idempotent action in a frozen recovery execution plan."""

    __tablename__ = "recovery_execution_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_plan_id: Mapped[str] = mapped_column(ForeignKey("recovery_execution_plans.id", ondelete="CASCADE"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    target_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    provider_key: Mapped[str | None] = mapped_column(String, nullable=True)
    semantic_fingerprint: Mapped[str] = mapped_column(String)
    authority_decision: Mapped[str] = mapped_column(String, default="REQUIRE_CONFIRMATION", server_default="REQUIRE_CONFIRMATION")
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("execution_plan_id", "semantic_fingerprint"),)


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
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )


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


class AgentPendingAction(Base):
    """Durable human-in-the-loop continuation owned by one AgentRun."""

    __tablename__ = "agent_pending_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("action_approvals.id", ondelete="CASCADE"), unique=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    mission_id: Mapped[str] = mapped_column(ForeignKey("journey_missions.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    originating_step_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_run_steps.id", ondelete="SET NULL"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    claimed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    resumed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String)
    authority_receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RESUMING','RESUMED','FAILED','EXPIRED','SUPERSEDED')",
            name="ck_agent_pending_actions_status",
        ),
    )


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
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )
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


class MemberDraftShortlistItem(Base):
    """One member's public-only POI bookmark inside a DRAFT Journey."""

    __tablename__ = "member_draft_shortlist_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    provider_key: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column()
    longitude: Mapped[float] = mapped_column()
    coordinate_system: Mapped[str] = mapped_column(String, default="WGS84", server_default="WGS84")
    source_url: Mapped[str] = mapped_column(Text)
    source_provenance: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    opening_hours_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    cost_evidence_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    source_tags_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    distance_m: Mapped[float | None] = mapped_column(nullable=True)
    distance_basis: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="SAVED", server_default="SAVED")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("journey_id", "member_id", "external_id"),
        CheckConstraint("status = 'SAVED'", name="ck_member_draft_shortlist_items_status"),
        Index(
            "idx_member_draft_shortlist_items_owner",
            "journey_id",
            "member_id",
            "traveler_id",
            "updated_at",
        ),
    )


class MemberDraftResumeIntent(Base):
    """Private UI resume pointer for one member's saved draft destination."""

    __tablename__ = "member_draft_resume_intents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    shortlist_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_shortlist_items.id", ondelete="CASCADE")
    )
    selected_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("journey_id", "member_id", "traveler_id"),
        Index(
            "idx_member_draft_resume_intents_owner",
            "journey_id",
            "member_id",
            "traveler_id",
            "updated_at",
        ),
    )


class MemberDraftPocketWalk(Base):
    """Private ordered two-stop intent for a member's draft Pocket Walk."""

    __tablename__ = "member_draft_pocket_walks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    first_shortlist_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_shortlist_items.id", ondelete="CASCADE")
    )
    second_shortlist_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_shortlist_items.id", ondelete="CASCADE")
    )
    max_walk_minutes: Mapped[int | None] = mapped_column(nullable=True)
    # R108 uses this as the conditional version of the exact ordered pair.  It
    # advances whenever a saved pair or one of its source snapshots changes.
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("journey_id", "member_id", "traveler_id"),
        CheckConstraint(
            "first_shortlist_id <> second_shortlist_id",
            name="ck_member_draft_pocket_walk_distinct_stops",
        ),
        CheckConstraint(
            "max_walk_minutes IS NULL OR (max_walk_minutes >= 1 AND max_walk_minutes <= 180)",
            name="ck_member_draft_pocket_walk_max_minutes",
        ),
        Index(
            "idx_member_draft_pocket_walk_owner",
            "journey_id",
            "member_id",
            "traveler_id",
            "updated_at",
        ),
    )


class MemberDraftPocketWalkProgress(Base):
    """Server-owned, member-private self-reported GO MODE progress.

    This row deliberately carries no GPS, provider, arrival, Journey execution,
    or plan facts.  The shortlist identities and pocket revision bind the
    self-report to the exact two-stop intention the traveler reviewed.
    """

    __tablename__ = "member_draft_pocket_walk_progress"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    pocket_walk_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_pocket_walks.id", ondelete="CASCADE")
    )
    first_shortlist_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_shortlist_items.id", ondelete="CASCADE")
    )
    second_shortlist_id: Mapped[str] = mapped_column(
        ForeignKey("member_draft_shortlist_items.id", ondelete="CASCADE")
    )
    pocket_walk_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    state: Mapped[str] = mapped_column(String, default="NOT_STARTED", server_default="NOT_STARTED")
    paused_from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    progress_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    progress_class: Mapped[str] = mapped_column(
        String, default="USER_REPORTED_PROGRESS", server_default="USER_REPORTED_PROGRESS"
    )
    last_operation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("journey_id", "member_id", "traveler_id"),
        CheckConstraint(
            "state IN ('NOT_STARTED', 'HEADING_TO_FIRST', 'FIRST_STOP_DONE_SELF_REPORTED', "
            "'HEADING_TO_SECOND', 'WALK_DONE_SELF_REPORTED', 'PAUSED')",
            name="ck_member_draft_pocket_walk_progress_state",
        ),
        CheckConstraint(
            "progress_class = 'USER_REPORTED_PROGRESS'",
            name="ck_member_draft_pocket_walk_progress_class",
        ),
        Index(
            "idx_member_draft_pocket_walk_progress_owner",
            "journey_id",
            "member_id",
            "traveler_id",
            "updated_at",
        ),
    )


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
    # Lifecycle is deliberately separate from status: CANDIDATE/SELECTED describe
    # the generic plan state, while STANDBY is a governed, non-current branch.
    lifecycle: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    trigger_contract_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
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
    authority_receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )


class CollectiveAuthorityReceipt(Base):
    """Server-issued proof that one exact collective future is executable.

    The receipt is deliberately separate from a planning proposal, member
    response, or user approval.  Those rows are evidence used to issue it;
    this row is the durable authority boundary consumed by a mutation.
    """

    __tablename__ = "collective_authority_receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("replan_proposals.id", ondelete="CASCADE"))
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="CASCADE"))
    authority_mode: Mapped[str] = mapped_column(String)
    decision_epoch: Mapped[str] = mapped_column(String)
    material_world_epoch: Mapped[str] = mapped_column(String)
    candidate_surface_fingerprint: Mapped[str] = mapped_column(String)
    source_mission_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_missions.id", ondelete="SET NULL"), nullable=True
    )
    source_agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )
    required_member_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    response_audit_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    principal_actor_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    classification: Mapped[str] = mapped_column(String)
    issued_at: Mapped[str] = mapped_column(String)
    invalidated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    invalidation_code: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_collective_authority_receipts_target", "journey_id", "proposal_id", "plan_version_id"),
    )


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
    recovery_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_epochs.id", ondelete="SET NULL"), nullable=True
    )
    source_agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    semantic_identity: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)


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


class JourneyLaunchContext(Base):
    """Durable planned launch origin, separate from execution truth."""

    __tablename__ = "journey_launch_contexts"
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), primary_key=True
    )
    source_kind: Mapped[str] = mapped_column(String)
    authority_kind: Mapped[str] = mapped_column(String)
    place_id: Mapped[str | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    target_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String)
    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)
    coordinate_crs: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_from: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_until: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_method: Mapped[str | None] = mapped_column(String, nullable=True)
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


class JourneyInboxItem(Base):
    __tablename__ = "journey_inbox_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    source_kind: Mapped[str] = mapped_column(String)
    raw_text_hash: Mapped[str] = mapped_column(String)
    raw_text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[str] = mapped_column(String)
    semantic_kind: Mapped[str] = mapped_column(String)
    reservation_type: Mapped[str] = mapped_column(String)
    confirmation_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    change_kind: Mapped[str] = mapped_column(String)
    extracted_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    provenance: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    linked_reservation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    linked_commitment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reality_claim_id: Mapped[str | None] = mapped_column(String, nullable=True)
    impact_status: Mapped[str] = mapped_column(String)
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("journey_id", "raw_text_hash"),)


class RealityClaim(Base):
    __tablename__ = "reality_claims"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    fact_domain: Mapped[str] = mapped_column(String)
    fact_key: Mapped[str] = mapped_column(String)
    subject_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[str] = mapped_column(String)
    value_json: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String)
    source_ref: Mapped[str] = mapped_column(String)
    observed_at: Mapped[str] = mapped_column(String)
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    authority_class: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    fingerprint: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("journey_id", "fingerprint"),)


class RealityConflict(Base):
    __tablename__ = "reality_conflicts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    fact_domain: Mapped[str] = mapped_column(String)
    fact_key: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(Integer)
    claim_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    claim_set_fingerprint: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    selected_claim_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_claim_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_provenance: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        UniqueConstraint("journey_id", "fact_key", "generation"),
        UniqueConstraint("journey_id", "fact_key", "claim_set_fingerprint"),
    )


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
    # NULL means the historical/browser row was never bound to a verified traveler principal.
    # Do not infer identity later from Journey.traveler_id or request-body claims.
    traveler_id: Mapped[str | None] = mapped_column(
        ForeignKey("travelers.id", ondelete="SET NULL"), nullable=True
    )
    journey_member_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_members.id", ondelete="SET NULL"), nullable=True
    )
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


class RouteAcquisitionOperation(Base):
    """Durable lifecycle for one exact on-demand mobility edge."""

    __tablename__ = "route_acquisition_operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String)
    origin_key: Mapped[str] = mapped_column(String)
    target_key: Mapped[str] = mapped_column(String)
    target_activity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_mode: Mapped[str] = mapped_column(String)
    provider_policy_version: Mapped[str] = mapped_column(String)
    route_truth_policy_version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="QUEUED", server_default="QUEUED")
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    queued_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
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
    preferences_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class JourneyMembershipInvitation(Base):
    """A recipient-owned invitation; membership begins only after that traveller accepts."""

    __tablename__ = "journey_membership_invitations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    target_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    inviter_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    inviter_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    base_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="RESTRICT"))
    base_cursor_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    proposed_role: Mapped[str] = mapped_column(String, default="COLLABORATOR", server_default="COLLABORATOR")
    title_snapshot: Mapped[str] = mapped_column(String)
    city_code_snapshot: Mapped[str] = mapped_column(String)
    approximate_start_date: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="OFFERED", server_default="OFFERED")
    request_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    declined_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_member_id: Mapped[str | None] = mapped_column(
        ForeignKey("journey_members.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "state IN ('OFFERED','ACCEPTED','DECLINED','REVOKED','EXPIRED')",
            name="ck_journey_membership_invitations_state",
        ),
        CheckConstraint(
            "proposed_role = 'COLLABORATOR'",
            name="ck_journey_membership_invitations_role",
        ),
        UniqueConstraint(
            "journey_id", "inviter_member_id", "request_key",
            name="uq_journey_membership_invitations_request",
        ),
        Index(
            "ux_journey_membership_invitations_one_offered",
            "journey_id", "target_traveler_id",
            unique=True,
            sqlite_where=text("state = 'OFFERED'"),
            postgresql_where=text("state = 'OFFERED'"),
        ),
        Index(
            "idx_journey_membership_invitations_recipient",
            "target_traveler_id", "state", "expires_at",
        ),
    )


class JourneyRendezvousMemberSignal(Base):
    """One member's voluntary, coarse status for one exact shared rendezvous."""

    __tablename__ = "journey_rendezvous_member_signals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    journey_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    active_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="RESTRICT"))
    current_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    rendezvous_activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id", ondelete="RESTRICT"))
    public_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    signal_kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    audience_member_ids_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    reported_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "signal_kind IN ('HEADING_TO_RENDEZVOUS','NEEDS_COORDINATION')",
            name="ck_journey_rendezvous_member_signals_kind",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','REVOKED','REPLACED','STALE','EXPIRED')",
            name="ck_journey_rendezvous_member_signals_status",
        ),
        UniqueConstraint(
            "journey_id", "journey_member_id", "idempotency_key",
            name="uq_journey_rendezvous_member_signals_request",
        ),
        Index(
            "ux_journey_rendezvous_member_signals_one_active",
            "journey_id", "journey_member_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "idx_journey_rendezvous_member_signals_expiry",
            "journey_id", "status", "expires_at",
        ),
    )


class JourneyRendezvousSignalAcknowledgement(Base):
    """A signed companion's one-time acknowledgement of a current help request."""

    __tablename__ = "journey_rendezvous_signal_acknowledgements"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_member_signals.id", ondelete="CASCADE"), unique=True
    )
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    requester_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    responder_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    responder_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    acknowledged_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class JourneyRendezvousBuddyHandoff(Base):
    """Pair-private offer to meet at one public Place after a signed help request."""

    __tablename__ = "journey_rendezvous_buddy_handoffs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_member_signals.id", ondelete="CASCADE")
    )
    acknowledgement_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_signal_acknowledgements.id", ondelete="CASCADE")
    )
    requester_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    requester_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    helper_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    helper_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    active_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="RESTRICT"))
    current_activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    original_rendezvous_activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="RESTRICT")
    )
    original_public_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    handoff_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    handoff_place_snapshot_json: Mapped[str] = mapped_column(Text)
    audience_member_ids_json: Mapped[str] = mapped_column(Text)
    audience_sha256: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="OFFERED", server_default="OFFERED")
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    offered_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    responded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('OFFERED','ACCEPTED','DECLINED','REVOKED','CANCELLED','EXPIRED','STALE')",
            name="ck_journey_rendezvous_buddy_handoffs_status",
        ),
        UniqueConstraint(
            "journey_id", "helper_member_id", "idempotency_key",
            name="uq_journey_rendezvous_buddy_handoffs_request",
        ),
        Index(
            "ux_journey_rendezvous_buddy_handoffs_one_current",
            "signal_id",
            unique=True,
            sqlite_where=text("status IN ('OFFERED','ACCEPTED')"),
            postgresql_where=text("status IN ('OFFERED','ACCEPTED')"),
        ),
        Index(
            "idx_journey_rendezvous_buddy_handoffs_pair_status_expiry",
            "journey_id", "requester_member_id", "helper_member_id", "status", "expires_at",
        ),
    )


class JourneyRendezvousBuddyExecution(Base):
    """Pair-private, mutually consented finite execution window for one handoff."""

    __tablename__ = "journey_rendezvous_buddy_executions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    handoff_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_buddy_handoffs.id", ondelete="CASCADE"), unique=True
    )
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_member_signals.id", ondelete="CASCADE")
    )
    acknowledgement_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_signal_acknowledgements.id", ondelete="CASCADE")
    )
    requester_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    requester_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    helper_member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    helper_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    active_plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id", ondelete="RESTRICT"))
    current_activity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    original_rendezvous_activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="RESTRICT")
    )
    original_public_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    original_place_snapshot_json: Mapped[str] = mapped_column(Text)
    handoff_place_id: Mapped[str] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"))
    handoff_place_snapshot_json: Mapped[str] = mapped_column(Text)
    audience_member_ids_json: Mapped[str] = mapped_column(Text)
    audience_sha256: Mapped[str] = mapped_column(String)
    execution_window_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="WINDOW_OFFERED", server_default="WINDOW_OFFERED")
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    helper_consent_traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    requester_consent_traveler_id: Mapped[str | None] = mapped_column(
        ForeignKey("travelers.id", ondelete="CASCADE"), nullable=True
    )
    helper_consented_at: Mapped[str] = mapped_column(String)
    requester_consented_at: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_at: Mapped[str] = mapped_column(String)
    proposal_expires_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('WINDOW_OFFERED','ACTIVE','MUTUALLY_ACKNOWLEDGED','REPLAN_REQUIRED','CANCELLED','EXPIRED','STALE')",
            name="ck_journey_rendezvous_buddy_executions_status",
        ),
        CheckConstraint(
            "execution_window_minutes IN (30,60,90)",
            name="ck_journey_rendezvous_buddy_executions_window",
        ),
        UniqueConstraint(
            "journey_id", "helper_member_id", "idempotency_key",
            name="uq_journey_rendezvous_buddy_executions_request",
        ),
        Index(
            "idx_journey_rendezvous_buddy_executions_pair_status_expiry",
            "journey_id", "requester_member_id", "helper_member_id", "status", "expires_at",
        ),
    )


class JourneyRendezvousBuddyExecutionReceipt(Base):
    """One signed self-report per member for one exact private execution window."""

    __tablename__ = "journey_rendezvous_buddy_execution_receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_buddy_executions.id", ondelete="CASCADE")
    )
    member_id: Mapped[str] = mapped_column(ForeignKey("journey_members.id", ondelete="CASCADE"))
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    receipt_kind: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    self_reported_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "receipt_kind IN ('BUDDY_MET_ACK','CANNOT_MEET','CANCEL_PARTICIPATION')",
            name="ck_journey_rendezvous_buddy_execution_receipts_kind",
        ),
        UniqueConstraint(
            "execution_id", "member_id",
            name="uq_journey_rendezvous_buddy_execution_receipts_member",
        ),
        UniqueConstraint(
            "execution_id", "idempotency_key",
            name="uq_journey_rendezvous_buddy_execution_receipts_idempotency",
        ),
        Index(
            "idx_journey_rendezvous_buddy_execution_receipts_journey",
            "journey_id", "execution_id", "self_reported_at",
        ),
    )


class PrivateBuddyRouteLocationBinding(Base):
    """Write-once context receipt for one R80 private location observation.

    The observation itself contains coordinates. This table stores only the exact
    authorization scope needed to decide whether that sample may still be used by
    a private handoff route. Old observations are deliberately not backfilled.
    """

    __tablename__ = "private_buddy_route_location_bindings"
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("location_observations.id", ondelete="CASCADE"), primary_key=True
    )
    journey_id: Mapped[str] = mapped_column(ForeignKey("journeys.id", ondelete="CASCADE"))
    handoff_id: Mapped[str] = mapped_column(
        ForeignKey("journey_rendezvous_buddy_handoffs.id", ondelete="CASCADE")
    )
    journey_member_id: Mapped[str] = mapped_column(
        ForeignKey("journey_members.id", ondelete="CASCADE")
    )
    traveler_id: Mapped[str] = mapped_column(ForeignKey("travelers.id", ondelete="CASCADE"))
    active_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE")
    )
    current_activity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    destination_place_id: Mapped[str] = mapped_column(String)
    destination_place_snapshot_sha256: Mapped[str] = mapped_column(String)
    audience_sha256: Mapped[str] = mapped_column(String)
    handoff_accepted_at: Mapped[str] = mapped_column(String)
    captured_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    bound_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "length(destination_place_snapshot_sha256) = 64",
            name="ck_private_buddy_route_binding_destination_hash",
        ),
        CheckConstraint(
            "length(audience_sha256) = 64",
            name="ck_private_buddy_route_binding_audience_hash",
        ),
        Index(
            "idx_private_buddy_route_bindings_context_expiry",
            "journey_id", "handoff_id", "journey_member_id", "traveler_id", "expires_at",
        ),
    )


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
