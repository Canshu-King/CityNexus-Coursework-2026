from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError  # not re-exported by `pydantic` at 2.13.5

from app.capabilities import capability_write_refusal

PlanningMode = Literal["balanced", "budget", "explore", "strict_schedule"]


class Money(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        return value.upper()


class TravelerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = None
    locale: str = "en-GB"
    timezone: str


class Traveler(TravelerCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    updated_at: datetime


class PreferenceInput(BaseModel):
    category: str = Field(min_length=1)
    key: str = Field(min_length=1)
    value: Any
    weight: float = Field(ge=0, le=1)


class Preference(PreferenceInput):
    id: UUID
    traveler_id: UUID


class CapabilityInput(BaseModel):
    """Validated explicit capability input (JSON uses ``class``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: str = Field(min_length=2, max_length=64)
    capability_class: str = Field(default="SOFT_PREFERENCE", alias="class")
    severity: Literal["HARD", "STRONG", "SOFT"] = "SOFT"
    params: dict[str, Any] = Field(default_factory=dict)
    scope: Literal["TRAVELER", "JOURNEY"] = "TRAVELER"
    source: str = Field(default="USER_CONFIRMED", min_length=1, max_length=64)
    provenance: str = Field(default="USER_STATED", min_length=1, max_length=120)
    explicit: bool = True
    confirmed: bool = False
    active: bool = True

    @model_validator(mode="after")
    def _refuse_rules_the_product_cannot_hold_to(self) -> "CapabilityInput":
        """Two checks, in order, on the request body - before any row can exist.

        This is deliberately the model rather than the route handlers: `CapabilityInput` is the body
        type of all four capability write routes (`main.py` `PUT /travelers/{id}/capabilities`,
        `POST /travelers/{id}/capabilities/confirm`, the two journey twins) plus the shared
        `_capability_input_rows` helper, and it is neither constructed internally nor used as a
        response model - so one validator here covers every door a client can knock on, with no edit
        to `main.py` (that file is another lane's write domain).  A pydantic failure becomes FastAPI's
        existing 422, so the session is never touched.

        The two refusals keep distinct `type` values on purpose (`main.py`'s handler derives
        `message_key` from `type`, never from the English message), and the rules themselves come from
        `capabilities.py` so this schema cannot grow its own opinion about what is executable.
        `STRONG` is checked like `SOFT`: only `HARD` items reach the feasibility gate.
        """

        refusal = capability_write_refusal(self.code, self.severity)
        if refusal == "UNKNOWN_RULE_CODE":
            raise PydanticCustomError(
                "unknown_rule_code",
                "UNKNOWN_RULE_CODE: {code} is not a capability rule this product understands",
                {"code": str(self.code), "severity": str(self.severity)},
            )
        if refusal == "HARD_RULE_NOT_EXECUTABLE":
            raise PydanticCustomError(
                "hard_rule_not_executable",
                "HARD_RULE_NOT_EXECUTABLE: {code} is a preference, so HARD cannot be enforced "
                "against the plan",
                {"code": str(self.code), "severity": str(self.severity)},
            )
        return self


class AgentPolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ADVISORY", "ASSISTED", "AUTONOMOUS"] = "ADVISORY"
    auto_accept_replan: bool = False
    max_auto_replan_cost_delta_minor: int = Field(default=0, ge=0)
    max_auto_loss_minor: int = Field(default=0, ge=0)
    max_auto_schedule_drift_min: int = Field(default=0, ge=0)
    allowed_activity_types: list[str] = Field(default_factory=list, max_length=20)
    allowed_booking_types: list[str] = Field(default_factory=list, max_length=20)
    protected_commitment_levels: list[str] = Field(
        default_factory=lambda: ["C3_Guaranteed", "C4_Anchored"], max_length=5
    )
    allow_auto_commitment_management: bool = False
    allow_auto_when_provider_degraded: bool = False
    allow_auto_on_critical_event: bool = False


class AgentPolicy(AgentPolicyPayload):
    traveler_id: UUID
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=128)
    agent_id: str = Field(default="hermes", min_length=1, max_length=128)
    journey_id: UUID | None = None
    mission_id: UUID | None = None
    proposal_id: UUID | None = None
    standby_plan_id: UUID | None = None
    trigger_event_id: UUID | None = None
    candidate_count: int = Field(default=3, ge=1, le=5)
    solver_mode: Literal["deterministic", "ortools", "auto", "v2"] | None = None
    city_code: str | None = Field(default=None, min_length=1, max_length=32)
    q: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    radius_m: int | None = Field(default=None, ge=250, le=5000)
    discovery_limit: int | None = Field(default=None, ge=1, le=8)
    service_type: str | None = Field(default=None, max_length=80)
    place_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_url: str | None = Field(default=None, min_length=1, max_length=2048)
    source_observation_id: UUID | None = None
    source_class: Literal[
        "OFFICIAL_WEB",
        "OFFICIAL_OPERATOR",
        "OFFICIAL_TICKETING",
        "MUNICIPAL_AUTHORITY",
    ] = "OFFICIAL_WEB"
    estimated_open_minutes: int | None = Field(default=None, ge=0, le=1440)
    estimated_close_minutes: int | None = Field(default=None, ge=0, le=1440)
    estimated_cost_minor: int | None = Field(default=None, ge=0)
    # A replan hint may contain one newly discovered Place. The gateway still
    # enforces the larger input required by generic plan generation; keeping
    # this field at one lets F4 opportunity insertion use a single bounded hint.
    place_ids: list[str] | None = Field(default=None, min_length=1, max_length=12)
    planning_mode: PlanningMode | None = None
    approval_id: UUID | None = None
    activity_id: UUID | None = None
    replacement_activity_id: UUID | None = None
    replacement_place_id: str | None = Field(default=None, min_length=1, max_length=128)
    booking_id: UUID | None = None
    commitment_id: UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    provider_key: str = Field(default="mock-booking", min_length=1, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    outcome: Literal[
        "MUTATION_EXECUTED",
        "APPROVAL_PENDING",
        "NO_ACTION",
        "UNRESOLVED",
        "CLARIFICATION_REQUIRED",
    ] | None = None
    horizon_decision: Literal["TAKE_NOW", "DEFER", "NO_ACTION"] | None = None
    question: str | None = Field(default=None, min_length=1, max_length=500)
    option_keys: list[str] | None = Field(default=None, min_length=2, max_length=3)
    preference_dimension: str | None = Field(default=None, min_length=1, max_length=120)
    # Compatibility-only input. The gateway never treats this caller-owned
    # boolean as authorization; approvals are server-issued and user-bound.
    user_confirmed: bool = False


class AgentApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_method: Literal["USER_CONFIRMATION", "STEP_UP", "WEBAUTHN"] = "USER_CONFIRMATION"
    # A process-scoped demo proof for REQUIRE_STEP_UP. The production seam is
    # replaceable by a verified WebAuthn assertion without trusting the method
    # label itself.
    step_up_code: str | None = Field(default=None, min_length=4, max_length=128)


class AgentCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=8, max_length=2000)
    requested_autonomy: Literal["PLAN_ONLY", "EXECUTE_WITHIN_BOUNDS"] = "EXECUTE_WITHIN_BOUNDS"


class AgentRunOutcome(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    reason_code: str | None = None
    user_summary: str | None = None
    recommended_action: str | None = None


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: UUID | None = None
    mission_id: UUID
    journey_id: UUID | None = None
    provider_key: str | None = None
    runtime_identity: str | None = None
    model_key: str | None = None
    status: str
    completion_contract: dict[str, Any] | None = None
    outcome: str | AgentRunOutcome | None = None
    failure_reason: str | None = None
    mission_status: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    mission: dict[str, Any] | None = None
    tool_steps: list[dict[str, Any]] = Field(default_factory=list)
    native_events_url: str | None = None
    events_url: str | None = None
    timeline_url: str | None = None
    initial_events: list[dict[str, Any]] | None = None
    protected_commitments: list[dict[str, Any]] | None = None


class AgentCommandResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: UUID | None = None
    mission: dict[str, Any] = Field(default_factory=dict)
    run: AgentRunResponse


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clarification_id: str = Field(min_length=1, max_length=128)
    option_key: str = Field(min_length=1, max_length=64)
    scope: Literal["THIS_JOURNEY", "REMEMBER_FOR_FUTURE_TRIPS"] = "THIS_JOURNEY"


class GuardianAutopilotStart(BaseModel):
    interval_seconds: float | None = Field(default=None, gt=0, le=3600)


class JourneyOperatorStartRequest(BaseModel):
    """Plain-language one-click activation for the Journey Operator."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(
        default=(
            "Take care of this trip. Protect confirmed commitments, keep changes reasonable, "
            "and ask me before important changes."
        ),
        min_length=8,
        max_length=2000,
    )
    requested_autonomy: Literal["PLAN_ONLY", "EXECUTE_WITHIN_BOUNDS"] = "EXECUTE_WITHIN_BOUNDS"
    interval_seconds: float | None = Field(default=None, gt=0, le=3600)


class CitySignalFixtureRequest(BaseModel):
    state: Literal["NORMAL", "DISRUPTED", "OUTAGE", "RISK_FORECAST"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ResilienceRefreshRequest(BaseModel):
    mode: Literal["replay", "captured_replay", "live"] = "replay"


class RealCityDiscoveryRequest(BaseModel):
    query: str = Field(default="indoor cafe", min_length=1, max_length=120)
    radius_m: int = Field(default=2500, ge=100, le=5000)
    limit: int = Field(default=8, ge=1, le=20)
    mode: Literal["replay", "captured_replay", "live"] = "captured_replay"


class BootstrapDiscoveryRequest(BaseModel):
    """Pre-plan discovery for a new Journey with no materialized catalogue."""

    query: str = Field(default="cafe", min_length=1, max_length=120)
    radius_m: int = Field(default=2500, ge=100, le=5000)
    limit: int = Field(default=3, ge=1, le=3)
    source_kind: Literal["FOREGROUND_GPS", "MANUAL_PUBLIC_CENTER"]
    provider_consent: bool = False
    latitude: float
    longitude: float
    coordinate_system: Literal["WGS84"] = "WGS84"
    accuracy_m: float | None = Field(default=None, gt=0, le=1000)
    mode: Literal["live"] = "live"


class BootstrapCitySearchRequest(BaseModel):
    """One explicit, bounded city lookup before a traveler taps a public area."""

    query: str = Field(min_length=2, max_length=120)
    provider_consent: bool = False
    limit: int = Field(default=3, ge=1, le=3)


class BootstrapSelectRequest(BaseModel):
    receipt_id: UUID
    candidate_id: UUID


class BootstrapMapPreviewRequest(BaseModel):
    """Release one selected public destination to an external map."""

    receipt_id: UUID
    candidate_id: UUID
    public_map_consent: bool = False
    latitude: float
    longitude: float
    coordinate_system: Literal["WGS84"] = "WGS84"


class BootstrapShortlistSaveRequest(BaseModel):
    receipt_id: UUID
    candidate_id: UUID


class BootstrapShortlistPreviewRequest(BaseModel):
    """Release a saved public destination using fresh per-click consent."""

    public_map_consent: bool = False


class BootstrapShortlistNavigationRequest(BaseModel):
    """Release a saved public destination for destination-only walking directions."""

    public_map_consent: bool = False


class BootstrapTwoStopWalkRequest(BaseModel):
    """Release a walking leg between two explicitly ordered saved public stops."""

    first_shortlist_id: UUID
    second_shortlist_id: UUID
    public_map_consent: bool = False


class BootstrapTwoStopWalkabilityRequest(BaseModel):
    """Check provider-backed walking evidence for two saved public stops."""

    first_shortlist_id: UUID
    second_shortlist_id: UUID
    public_route_consent: bool = False


class BootstrapAdaptiveShortWalkRequest(BaseModel):
    """Compare the current and one alternative second stop for a short-walk preference."""

    first_shortlist_id: UUID
    current_second_shortlist_id: UUID
    alternative_second_shortlist_id: UUID
    max_walk_minutes: int = Field(ge=1, le=180)
    public_route_consent: bool = False


class BootstrapPocketWalkSaveRequest(BaseModel):
    """Persist two ordered saved public stops for a member-private Pocket Walk."""

    first_shortlist_id: UUID
    second_shortlist_id: UUID
    max_walk_minutes: int | None = Field(default=None, ge=1, le=180)


class BootstrapPocketWalkGoTransitionRequest(BaseModel):
    """One revision-bound user self-report for the private Pocket Walk GO MODE."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "START",
        "REPORT_FIRST_DONE",
        "START_SECOND",
        "HEAD_TO_SECOND",
        "REPORT_WALK_DONE",
        "PAUSE",
        "RESUME",
        "UNDO",
    ]
    expected_revision: int = Field(ge=0)
    pocket_walk_revision: int = Field(ge=1)
    idempotency_key: UUID


class BootstrapSharedWalkObjectRequest(BaseModel):
    """One public OSM identity parsed from an untrusted friend share."""

    model_config = ConfigDict(extra="forbid")

    osm_type: Literal["node", "way", "relation"]
    osm_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=32)
    source_url: str = Field(min_length=1, max_length=240)


class BootstrapSharedWalkLookupRequest(BaseModel):
    """Exact two-object lookup after an explicit recipient consent."""

    model_config = ConfigDict(extra="forbid")

    objects: list[BootstrapSharedWalkObjectRequest] = Field(min_length=2, max_length=2)
    provider_consent: bool = False


class BootstrapSharedWalkPocketWalkSaveRequest(BaseModel):
    """Save two freshly verified shared identities as this member's Pocket Walk."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    first_candidate_id: UUID
    second_candidate_id: UUID
    max_walk_minutes: int | None = Field(default=None, ge=1, le=180)


class BootstrapDiscoveryPocketWalkSaveRequest(BaseModel):
    """Save two current manual-center discoveries as this member's Pocket Walk."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    first_candidate_id: UUID
    second_candidate_id: UUID
    max_walk_minutes: int | None = Field(default=None, ge=1, le=180)


class RealCityIntelligenceRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=1000)
    radius_m: int = Field(default=3000, ge=100, le=5000)
    limit: int = Field(default=8, ge=1, le=10)
    mode: Literal["replay", "captured_replay", "live"] = "captured_replay"
    candidate_count: int = Field(default=2, ge=1, le=4)
    require_fully_grounded: bool = False


class JourneyAnchor(BaseModel):
    at: datetime
    location: str = Field(min_length=1, max_length=240)
    place_id: str | None = None
    notes: str | None = Field(default=None, max_length=500)


class JourneyCreate(BaseModel):
    traveler_id: UUID
    title: str = Field(min_length=1, max_length=160)
    city_code: str = Field(min_length=1)
    timezone: str
    start_at: datetime
    end_at: datetime
    budget: Money
    planning_mode: PlanningMode = "balanced"
    arrival_anchor: JourneyAnchor | None = None
    departure_anchor: JourneyAnchor | None = None
    party_size: int = Field(default=1, ge=1, le=20)
    occupancy: dict[str, Any] = Field(default_factory=dict)
    stay_budget: Money | None = None

    @model_validator(mode="after")
    def valid_window(self) -> JourneyCreate:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class JourneyPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    start_at: datetime | None = None
    end_at: datetime | None = None
    budget: Money | None = None


class Journey(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    traveler_id: UUID
    title: str
    city_code: str
    timezone: str
    start_at: datetime
    end_at: datetime
    budget: Money
    planning_mode: PlanningMode = "balanced"
    status: Literal["DRAFT", "PLANNED", "ACTIVE", "COMPLETED", "CANCELLED"]
    active_plan_version_id: UUID | None = None
    current_activity_id: UUID | None = None


class RecycleBinJourney(Journey):
    """One trip the recycle bin holds, carrying the two facts a screen must not infer.

    `deleted_at` is the stored value of `journeys.deleted_at`, and `bin_state` is the only word
    this shape can hold: a row appears here because that column is not NULL, so no client has to
    conclude "it was deleted" from its absence in `listJourneys`.
    """

    deleted_at: datetime
    bin_state: Literal["IN_RECYCLE_BIN"]


class MobilityModeRequest(BaseModel):
    mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"]


class WeatherDisruptionRequest(BaseModel):
    state: Literal["NORMAL", "RAIN", "SEVERE_RAIN", "TRANSIT_DISRUPTION"]


class StayPlanGenerateRequest(BaseModel):
    query: str = Field(default="stay-aware journey", min_length=1, max_length=500)
    weather_state: Literal["NORMAL", "RAIN", "SEVERE_RAIN", "TRANSIT_DISRUPTION"] = "NORMAL"
    candidate_count: int = Field(default=4, ge=3, le=8)
    sort: Literal["BEST_FOR_JOURNEY", "CHEAPEST", "LEAST_TRAVEL", "MOST_FLEXIBLE"] = (
        "BEST_FOR_JOURNEY"
    )


class StayModeChangeRequest(BaseModel):
    mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"]
    from_key: str | None = None
    to_key: str | None = None


class StayAutopilotRecoveryRequest(BaseModel):
    state: Literal["RAIN", "SEVERE_RAIN", "TRANSIT_DISRUPTION"] = "RAIN"


class LuggageContextRequest(BaseModel):
    baggage_present: bool = True
    baggage_size_class: Literal["NONE", "CABIN", "MEDIUM", "HEAVY"] = "MEDIUM"
    luggage_drop_required: bool = True


class ReservationImportRequest(BaseModel):
    artifact_type: Literal["STRUCTURED_JSON", "ICS", "TEXT"]
    payload: dict[str, Any]


class ReservationTextExtractionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class ReservationVisionRequest(BaseModel):
    image_url: str = Field(min_length=8, max_length=12_000_000)
    hint: str = Field(default="booking confirmation", max_length=200)


class ReservationDraftPatchRequest(BaseModel):
    reservation_type: str | None = Field(default=None, min_length=1, max_length=40)
    confirmation_ref: str | None = Field(default=None, max_length=120)
    origin: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None
    scheduled_departure_at: str | None = None
    scheduled_arrival_at: str | None = None
    price_amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    anchor_role: Literal["ARRIVAL", "DEPARTURE", "MID_TRIP", "UNKNOWN"] | None = None
    semantic_node_key: str | None = Field(default=None, max_length=200)
    semantic_target: dict[str, Any] | None = None


class WebResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    source_class: Literal["OFFICIAL_NOTICE", "OPERATOR_NOTICE", "OPEN_WEB"] = "OPEN_WEB"


class ScheduleChangeRequest(BaseModel):
    estimated_arrival_at: datetime
    status: Literal["DELAYED", "CANCELLED", "RESCHEDULED"] = "DELAYED"
    provider_key: str = Field(default="captured-transit-status", min_length=1, max_length=80)
    captured_at: datetime
    service_alert: str | None = Field(default=None, max_length=500)


class LocationObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=10000)
    captured_at: datetime


class GroupPresenceRouteRequest(BaseModel):
    """Request one member-specific route from a fresh, server-bound location sample."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID
    requested_mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] = "WALK"


class RendezvousBuddyPrivateRouteRequest(BaseModel):
    """One private route to an accepted R79 place after a separate disclosure action."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID
    requested_mode: Literal["WALK"] = "WALK"
    external_provider_disclosure_confirmed: bool = False


class JourneyRendezvousSignalCreateRequest(BaseModel):
    """The client may confirm the displayed target, but never choose a destination."""

    model_config = ConfigDict(extra="forbid")

    signal_kind: Literal["HEADING_TO_RENDEZVOUS", "NEEDS_COORDINATION"]
    expected_active_plan_version_id: UUID
    expected_current_activity_id: UUID | None = None
    expected_rendezvous_activity_id: UUID
    expected_public_place_id: str = Field(min_length=1, max_length=128)
    expected_audience_sha256: str = Field(
        min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
    )


class RendezvousBuddyHandoffOfferRequest(BaseModel):
    """An authorized helper selects only a server-suggested public Place."""

    model_config = ConfigDict(extra="forbid")

    help_signal_id: UUID
    expected_active_plan_version_id: UUID
    expected_current_activity_id: UUID | None = None
    expected_rendezvous_activity_id: UUID
    expected_original_public_place_id: str = Field(min_length=1, max_length=128)
    expected_audience_sha256: str = Field(
        min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
    )
    handoff_place_id: str = Field(min_length=1, max_length=128)
    execution_window_minutes: Literal[30, 60, 90] | None = None


class RendezvousBuddyHandoffDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "DECLINE"]


class RendezvousBuddyExecutionWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_window_minutes: Literal[30, 60, 90]


class RendezvousBuddyExecutionReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_kind: Literal["BUDDY_MET_ACK", "CANNOT_MEET", "CANCEL_PARTICIPATION"]


class MemberSideQuestCreateRequest(BaseModel):
    """Create or replace the authenticated member's private side-quest destination."""

    model_config = ConfigDict(extra="forbid")

    destination_text: str | None = Field(default=None, max_length=240)
    place_id: str | None = Field(default=None, min_length=1, max_length=128)


class MemberPlaceInvitationCreateRequest(BaseModel):
    """The owner must explicitly confirm disclosure of one exact public Place."""

    model_config = ConfigDict(extra="forbid")

    confirm_public_place_sharing: Literal[True]
    audience_snapshot: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class MemberPlaceInvitationDecisionRequest(BaseModel):
    """An addressed companion must make an explicit accept/decline decision."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "DECLINE"]
    confirm_exact_public_place: Literal[True] | None = None
    feasibility_token: str | None = Field(default=None, min_length=16, max_length=12000)
    acknowledge_feasibility_unknown: Literal[True] | None = None
    acknowledge_feasibility_risk: Literal[True] | None = None


class MemberPlaceInvitationFeasibilityPreviewRequest(BaseModel):
    """One explicitly consented, recipient-only route feasibility request."""

    model_config = ConfigDict(extra="forbid")

    consent_route_provider: Literal[True]
    location_observation_id: UUID | None = None
    dwell_minutes: Literal[10, 20, 30] = 20
    requested_mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] = "WALK"


class MemberPlaceInvitationProgressRequest(BaseModel):
    """An invitation participant explicitly shares or withdraws one coarse status."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "SHARE_ARRIVAL",
        "WITHDRAW_ARRIVAL",
        "SHARE_HEADING_BACK",
        "WITHDRAW_HEADING_BACK",
    ]
    share_audience_token: str | None = Field(default=None, min_length=64, max_length=64)


class MemberPlacePromotionCreateRequest(BaseModel):
    """One deliberate request to propose an invited Place as a shared stop."""

    model_config = ConfigDict(extra="forbid")

    dwell_minutes: Literal[10, 20, 30]


class MemberPlaceGroupShareConsentRequest(BaseModel):
    """Explicit confirmation of a frozen whole-Journey Place audience preview."""

    model_config = ConfigDict(extra="forbid")

    confirm_full_journey_place_sharing: Literal[True]
    audience_snapshot: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    plan_snapshot: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    source_snapshot: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class MemberPlacePromotionDecisionRequest(BaseModel):
    """One current Journey member's independent decision on a shared-stop proposal."""

    model_config = ConfigDict(extra="forbid")

    clarification_id: str = Field(min_length=1, max_length=128)
    decision: Literal["APPROVE", "DECLINE"]


class SharedStopCoPlanningGrantCreateRequest(BaseModel):
    """Offer one named Journey collaborator bounded authority for one public Place."""

    model_config = ConfigDict(extra="forbid")

    grantee_member_id: str = Field(min_length=1, max_length=128)


class SharedStopCoPlanningGrantDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "DECLINE"]


class MomentScoutRequest(BaseModel):
    """One-time owner-only discovery request; location is always server-bound."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID
    budget_minutes: Literal[15, 30, 45] = 30
    interest: Literal["ANYTHING", "COFFEE_FOOD", "CULTURE", "NATURE"] = "ANYTHING"


class MomentScoutSelectRequest(MomentScoutRequest):
    """Explicitly confirm one candidate from a fresh private Moment Scout result."""

    place_id: str = Field(min_length=1, max_length=128)
    scout_context: str = Field(min_length=32, max_length=64)


class LiveMomentScoutRequest(BaseModel):
    """One bounded external lookup, called only after explicit location-transfer consent."""

    model_config = ConfigDict(extra="forbid")

    consent_external_location: Literal[True]
    location_observation_id: UUID
    budget_minutes: Literal[15, 30, 45] = 30
    interest: Literal["ANYTHING", "COFFEE_FOOD", "CULTURE", "NATURE"] = "ANYTHING"
    radius_m: int = Field(default=1200, ge=800, le=2000)


class LiveMomentScoutSelectRequest(BaseModel):
    """Select a server-held live result and confirm its exact shared rendezvous."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    candidate_id: UUID
    confirm_return_to_rendezvous: Literal[True]


class MemberSideQuestFitPreviewRequest(BaseModel):
    """One-time owner-only detour fit check; current location is optional for UNKNOWN."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID | None = None
    dwell_minutes: Literal[10, 20, 30] = 20
    requested_mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] = "WALK"


class MemberSideQuestNavigateRequest(BaseModel):
    """Route only the authenticated member from a fresh one-time location to their stop."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID
    requested_mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] = "WALK"


class GroupRecoveryRequest(BaseModel):
    """Ask the existing Journey Agent to prepare one bounded group recovery proposal."""

    model_config = ConfigDict(extra="forbid")


class GroupRecoveryApplyRequest(BaseModel):
    """Spend the server-selected collective recovery; callers choose no proposal."""

    model_config = ConfigDict(extra="forbid")


class LaunchOriginRequest(BaseModel):
    """Explicit planned-origin confirmation; coordinates and authority are server-owned."""

    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["PLACE", "SELECTED_STAY", "ARRIVAL_ANCHOR"]
    place_id: str | None = None


class RouteAcquireRequest(BaseModel):
    """Request exactly one server-resolved route edge; no client coordinates."""

    model_config = ConfigDict(extra="forbid")

    target_activity_id: str | None = None
    requested_mode: Literal["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"] = "WALK"


class InTransitRerouteRequest(BaseModel):
    """Reroute from one server-stored current location sample to the exact active stop."""

    model_config = ConfigDict(extra="forbid")

    location_observation_id: UUID


class ExecutionTruthAttestation(BaseModel):
    """Optional evidence class for one execution-state transition.

    Omitting the body intentionally means SIMULATED_STATE_TRANSITION.  Stronger
    labels require evidence and are checked server-side; callers cannot promote
    an HTTP state-machine exercise into physical presence by choosing a string.
    """

    model_config = ConfigDict(extra="forbid")

    truth_kind: Literal[
        "SIMULATED_STATE_TRANSITION",
        "OPERATOR_CONFIRMED_EXECUTION",
        "DEVICE_LOCATION_VERIFIED_EXECUTION",
    ] = "SIMULATED_STATE_TRANSITION"
    operator_confirmed: bool = False
    location_observation_id: UUID | None = None


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2048)
    keys: dict[str, str] = Field(default_factory=dict)


class EdgeDeviceCreateRequest(BaseModel):
    display_name: str = Field(default="Trip companion", min_length=1, max_length=80)


class EdgeActionRequest(BaseModel):
    action: Literal["ACKNOWLEDGE", "NEXT", "REJECT_PROPOSAL", "SOS"]
    payload: dict[str, Any] = Field(default_factory=dict)


class EdgeObservationRequest(BaseModel):
    observation_type: Literal["RAIN", "SOS", "TEMP", "ACK"]
    payload: dict[str, Any] = Field(default_factory=dict)
    option_id: UUID | None = None


class ConstraintInput(BaseModel):
    kind: Literal["HARD", "SOFT"]
    category: str
    rule_code: str
    params: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0)


class Constraint(ConstraintInput):
    id: UUID
    journey_id: UUID
    active: bool


class GeoPoint(BaseModel):
    latitude: float
    longitude: float


class Place(BaseModel):
    id: str
    name: str
    category: str
    city_code: str
    location: GeoPoint
    tags: list[str] = Field(default_factory=list)
    # Display names are not identities: a live catalogue can carry an OSM way and
    # node with the same label.  The public catalogue must expose the provider
    # entity so a user-visible fact can be traced to the exact row.
    external_id: str
    # A coordinate means nothing without the system it is written in, and a place
    # read from a provider is only checkable if the payload says which provider.
    # No defaults on purpose: a producer has to state these, so the value that
    # reaches a traveller is always read from the row, never typed at the call site.
    provider_key: str
    coordinate_system: str
    # What the provider actually handed us; None means it was never recorded --
    # which is a different fact from "WGS84" and must not collapse into it.
    source_coordinate_system: str | None
    # These are the place's own captured facts.  In particular, UNKNOWN cost is
    # represented by the evidence status and a null amount; the compatibility
    # cost_minor=0 storage field is deliberately not projected as price truth.
    opening_hours: dict[str, Any]
    cost_evidence: dict[str, Any]


class RealCityDiscoveryCandidate(BaseModel):
    """One place candidate as `materialize_candidates` emits it -- the single dict
    site the screen's place card reads.

    ``extra="allow"`` is load-bearing, not a courtesy: FastAPI filters a response
    through its model, so a strict model here would silently drop any key a live
    provider carries that the replay shape did not.  Declaring a contract must not
    become a way of un-sending facts.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    external_id: str
    provider_key: str
    name: str
    category: str
    latitude: float
    longitude: float
    tags: list[str]
    distance_m: float | None
    opening_hours: dict[str, Any]
    metadata: dict[str, Any]
    cost_evidence: dict[str, Any]
    materialized_place_id: str
    coordinate_system: str
    source_coordinate_system: str | None


class RealCityDiscoveryResponse(BaseModel):
    """The wrapper both `capture_real_city_candidates` return sites build.

    Measured on the tree: the cache-hit return (real_city.py:1346) and the main
    return (real_city.py:1550) carry the same 11 keys, so nothing here is optional
    just in case.
    """

    model_config = ConfigDict(extra="allow")

    journey_id: str
    city_code: str
    city_context: str
    mode: str
    captured_at: str
    expires_at: str
    provider_status: dict[str, Any]
    provider_evidence: dict[str, Any]
    provenance: str
    runtime_provenance: str
    candidates: list[RealCityDiscoveryCandidate]


class CityService(BaseModel):
    id: str
    name: str
    service_type: str
    city_code: str
    place_id: str | None = None


class Activity(BaseModel):
    id: UUID
    sequence_no: int = Field(ge=1)
    title: str
    activity_type: str = "visit"
    place_id: str | None = None
    city_service_id: str | None = None
    start_at: datetime
    end_at: datetime
    travel_from_prev_min: int = Field(default=0, ge=0)
    estimated_cost: Money
    # Whether the amount is a real, provider-backed fact.  ``KNOWN_FREE`` is a
    # genuine 0 (a free attraction), ``UNKNOWN`` means no price evidence exists,
    # and ``KNOWN_PAID`` carries a real amount.  Collapsing UNKNOWN into 0 made
    # free attractions read "暂无价格" and paid ones look free.
    cost_status: str = "UNKNOWN"
    status: Literal["PLANNED", "READY", "NOT_STARTED", "EN_ROUTE", "ARRIVED", "IN_PROGRESS", "ACTIVE", "COMPLETED", "SKIPPED", "CANCELLED"]
    locked: bool = False
    # The map and travel surfaces resolve a stop's pin from its metadata
    # (latitude/longitude/crs/role).  Without projecting it a real plan shows no
    # coordinates at all.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoreBreakdown(BaseModel):
    commitment_preservation: float = Field(ge=0, le=1)
    stability: float = Field(ge=0, le=1)
    time_reliability: float = Field(ge=0, le=1)
    preference_fit: float = Field(ge=0, le=1)
    mobility_fit: float = Field(ge=0, le=1)
    budget_fit: float = Field(ge=0, le=1)
    robustness: float = Field(ge=0, le=1)
    opportunity_value: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)


class PlanVersion(BaseModel):
    id: UUID
    journey_id: UUID
    version_no: int = Field(ge=1)
    planning_mode: PlanningMode
    status: Literal["CANDIDATE", "SELECTED", "ACTIVE", "SUPERSEDED", "REJECTED"]
    lifecycle: Literal["ACTIVE", "STANDBY", "RETIRED"] = "ACTIVE"
    trigger_contract: dict[str, Any] = Field(default_factory=dict)
    source: Literal["INITIAL", "REPLAN", "MANUAL"]
    parent_plan_version_id: UUID | None = None
    total_cost: Money
    # A persisted integer total is retained for compatibility, but a live plan
    # may deliberately exclude one or more unresolved prices.  This status is
    # the public distinction between a known subtotal and a claimed total.
    total_cost_status: Literal["KNOWN", "PARTIAL_UNKNOWN"] = "KNOWN"
    utility_score: float | None = Field(default=None, ge=0, le=1)
    score_breakdown: ScoreBreakdown | None = None
    activities: list[Activity]
    mobility_snapshot_id: str | None = None
    mobility_evidence: dict[str, Any] = Field(default_factory=dict)
    explanation: dict[str, Any] = Field(default_factory=dict)
    # ORD-0386: a version list cannot be read as history without the moment each row was
    # written. `version_no` orders the chain but says nothing about when, and the screen that
    # shows "版本 3 / 版本 4" without a time is the one that makes a traveller guess whether
    # the old plan was this morning or last month.
    created_at: str | None = None
    # Stale/recompose-required projection.  A plan whose frozen evidence has
    # expired must not look like an ordinary current recommendation; selection
    # revalidation remains the final guard.
    stale: bool = False
    selectable: bool = True
    stale_reason: str | None = None
    # ORD-0097: "which of these is the plan the journey is running", answered by the server.
    # `GET /journeys/{id}/plans` returns every version - CANDIDATE, SELECTED, ACTIVE,
    # SUPERSEDED, REJECTED - so an array position is not a fact anyone may read as truth.
    # This mirrors `journey.active_plan_version_id` exactly: not derived from `status`, and
    # not derived from order. With no pointer, nothing is current (all False).
    is_current: bool = False


class GeneratePlansRequest(BaseModel):
    planning_mode: PlanningMode | None = None
    candidate_count: int = Field(default=3, ge=1, le=5)
    place_ids: list[str] | None = Field(default=None, min_length=1, max_length=12)
    # The zero-catalogue bootstrap may hand native planning exactly one selected
    # public Place.  It is opt-in so existing explicit-POI callers retain the
    # two-stop contract and its refusal code.
    allow_single_place: bool = False
    # Explicit traveller-owned acknowledgement for a provider Place whose
    # admission price is UNKNOWN.  Unknown admission is never treated as free,
    # included in the subtotal, or eligible for booking/payment.
    allow_unknown_cost: bool = False
    #: ORD-0272: *whose* batch this is - business semantics, so it lives in the body.
    #: `Idempotency-Key` stays the transport-level "is this the same send" control; merging the
    #: two into one key would make "same mission, different request" and "same request, resent"
    #: indistinguishable in storage, which is the mistake ORD-0262 keeps tripping over.
    mission_id: str | None = None


class CatalogHydrationRequest(BaseModel):
    """Ask the backend to prepare one destination's catalog (ORD-0095-1 §6).

    `query` is what the traveller typed, not an internal code: the whole point of the operation
    is that a person can ask about a place they can name.  `journey_id` is optional because the
    job belongs to the *destination* - two travellers preparing the same county under the same
    policy share one job, and the fingerprint says why they did.
    """

    query: str = Field(min_length=1, max_length=120)
    journey_id: UUID | None = None
    requested_categories: list[str] | None = Field(default=None, max_length=6)


class FactAcquisitionRequest(BaseModel):
    """Acquire source-linked facts for existing provider Places."""

    place_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    source_bindings: dict[str, "FactSourceBindingRequest"] | None = None


class FactSourceBindingRequest(BaseModel):
    """Bind an operator-confirmed official page to one existing provider Place."""

    source_url: str = Field(min_length=1, max_length=2048)
    source_class: Literal[
        "OFFICIAL_WEB",
        "OFFICIAL_OPERATOR",
        "OFFICIAL_TICKETING",
        "MUNICIPAL_AUTHORITY",
    ] = "OFFICIAL_WEB"


class AuthoritativeComposeRequest(BaseModel):
    """User-selected bounded input for the V12 multi-day planner."""

    place_ids: list[str] = Field(min_length=2, max_length=12)
    candidate_count: int = Field(default=3, ge=1, le=5)
    # R25 closure may only promote a plan whose selected mobility evidence is
    # provider-confirmed.  The planner already has this bounded mode; expose it
    # at the product boundary instead of letting synthetic alternate modes make
    # a live plan look authoritative.
    require_fully_grounded: bool = False
    stay_offer_id: UUID | None = None
    reservation_id: UUID | None = None


class TripStudioPreviewRequest(BaseModel):
    activity_ids: list[UUID] = Field(min_length=2, max_length=40)
    draft_order: list[UUID] | None = None


class TripStudioAcceptRequest(BaseModel):
    activity_ids: list[UUID] = Field(min_length=2, max_length=40)
    preview_id: UUID | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    trace_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class EventCreate(BaseModel):
    provider_key: str | None = None
    provenance: Literal["SIMULATED", "PROVIDER_VERIFIED", "USER_REPORTED", "SYSTEM_DERIVED"] = (
        "SIMULATED"
    )
    event_type: Literal[
        "TRAFFIC_DELAY",
        "CLOSURE",
        "WEATHER",
        "SCHEDULE_DELAY",
        "OPPORTUNITY_WINDOW",
        "SERVICE_UNAVAILABLE",
        "USER_CHANGE",
        "OTHER",
    ]
    severity: Literal["INFO", "MINOR", "MAJOR", "CRITICAL"] = "INFO"
    affected_activity_id: UUID | None = None
    occurred_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Event(EventCreate):
    id: UUID
    journey_id: UUID
    status: Literal["DETECTED", "ACKNOWLEDGED", "RESOLVED", "IGNORED"]
    detected_at: datetime
    # Resolution is part of the user-visible event lifecycle.  Keeping it out
    # of the response made a resolved event look indistinguishable from one
    # that had merely been observed.
    resolved_at: datetime | None = None
    resolution_reason: str | None = None


class OutcomeEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "ACTIVITY_STARTED",
        "ACTIVITY_COMPLETED",
        "ACTIVITY_SKIPPED",
        "MANUAL_OVERRIDE",
        "AGENT_PROPOSED",
        "USER_ACCEPTED",
        "USER_REJECTED",
    ]
    occurred_at_actual: datetime | None = None
    activity_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_type: str = Field(default="USER", min_length=1, max_length=40)
    related_proposal_id: UUID | None = None
    related_agent_run_id: UUID | None = None


class TravelerTwinClearRequest(BaseModel):
    confirm: bool = True


class JourneyTwin(BaseModel):
    journey_id: UUID
    active_plan_version_id: UUID | None = None
    current_activity_id: UUID | None = None
    latest_event_id: UUID | None = None
    projection_status: Literal["READY", "STALE", "DEGRADED"]
    snapshot_at: datetime
    upcoming_commitments: list[Commitment] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ReplanProposal(BaseModel):
    id: UUID
    journey_id: UUID
    trigger_event_id: UUID | None = None
    base_plan_version_id: UUID
    proposed_plan_version_id: UUID
    # Product-visible provenance for autonomous recovery. These are durable
    # server-owned bindings; clients may explain that an Agent proposed the
    # change, but must never treat either identifier as mutation authority.
    recovery_epoch_id: UUID | None = None
    source_agent_run_id: UUID | None = None
    status: Literal["GENERATED", "PRESENTED", "ACCEPTED", "REJECTED", "EXPIRED", "INVALIDATED"]
    hard_constraints_passed: bool
    locked_commitments_preserved: bool
    utility_score: float = Field(ge=0, le=1)
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
    explanation: dict[str, Any] = Field(default_factory=dict)


class ReplanGenerateRequest(BaseModel):
    trigger_event_id: UUID
    candidate_count: int = Field(default=3, ge=1, le=5)
    solver_mode: Literal["deterministic", "ortools", "auto", "v2"] | None = None
    protected_activity_ids: list[UUID] = Field(default_factory=list, max_length=50)
    # Candidate hints are not authority; the server reloads and revalidates
    # every Place and every mobility edge before materializing a plan.
    place_ids: list[str] | None = Field(default=None, min_length=1, max_length=12)
    # R73's typed adapter binds a user-selected dwell to one server-derived
    # invited public Place. It never grants authority to apply the result.
    promotion_id: UUID | None = None
    promotion_invitation_id: UUID | None = None
    promotion_dwell_minutes: Literal[10, 20, 30] | None = None


# === WP04 COMMERCE API SCHEMAS (generated) ===
class BookingSlot(BaseModel):
    start_at: datetime
    end_at: datetime
    provider_key: str
    provider_slot_ref: str


class BookingCreate(BaseModel):
    activity_id: UUID
    provider_key: str = "mock-booking"
    # Deterministic failure controls are test-fixture switches only.  They can
    # request a provider failure, never forge provider success.
    failure_token: Literal["FORCE_BOOKING_FAIL"] | None = None
    force_failure: bool = False


class Booking(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    journey_id: UUID
    activity_id: UUID | None
    commitment_id: UUID | None
    provider_key: str
    provider_label: str | None = None
    external_ref: str | None
    status: Literal["DRAFT", "PENDING", "CONFIRMED", "COMPLETED", "CANCELLED", "FAILED"]
    amount: Money
    amount_status: Literal["FREE_KNOWN", "KNOWN", "ESTIMATED", "UNKNOWN"] = "KNOWN"
    cancellation_policy: dict


class OrderItemInput(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=99)


class OrderCreate(BaseModel):
    activity_id: UUID
    items: list[OrderItemInput] = Field(min_length=1, max_length=50)
    provider_key: str = "mock-booking"


class OrderItem(BaseModel):
    """Server-priced order line returned after catalog authority is applied."""

    sku: str
    quantity: int = Field(ge=1, le=99)
    unit_amount_minor: int = Field(ge=0)
    line_total_minor: int = Field(ge=0)


class Order(BaseModel):
    id: UUID
    journey_id: UUID
    activity_id: UUID | None
    commitment_id: UUID | None
    provider_key: str
    external_ref: str | None
    status: Literal["DRAFT", "PENDING", "CONFIRMED", "FULFILLED", "CANCELLED", "FAILED"]
    amount: Money
    items: list[OrderItem]


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    booking_id: UUID | None = None
    order_id: UUID | None = None
    transaction_type: Literal["AUTHORIZE", "CAPTURE", "REFUND", "VOID"]
    amount: Money
    provider_key: str = "mock-payment"
    # Deterministic failure controls are test-fixture switches only; sensitive
    # card credentials are rejected by the extra='forbid' boundary above.
    failure_token: Literal["FORCE_PAYMENT_FAIL"] | None = None
    force_failure: bool = False

    @model_validator(mode="after")
    def exactly_one_target(self) -> TransactionCreate:
        if (self.booking_id is None) == (self.order_id is None):
            raise ValueError("exactly one of booking_id or order_id is required")
        return self


class Transaction(BaseModel):
    id: UUID
    journey_id: UUID
    booking_id: UUID | None
    order_id: UUID | None
    provider_key: str
    transaction_type: Literal["AUTHORIZE", "CAPTURE", "REFUND", "VOID"]
    status: Literal["INITIATED", "AUTHORIZED", "SUCCEEDED", "FAILED", "REFUNDED", "VOIDED"]
    amount: Money
    external_ref: str | None


class Commitment(BaseModel):
    id: UUID
    journey_id: UUID
    activity_id: UUID | None
    kind: Literal["BOOKING", "ORDER", "PAYMENT", "APPOINTMENT", "OTHER"]
    # The exact five allowed source-domain codes MUST be injected from the frozen YAML.
    level: str
    status: Literal["PROPOSED", "ACTIVE", "FULFILLED", "CANCELLED", "BREACHED"]
    monetary_value: Money
    monetary_value_status: Literal["FREE_KNOWN", "KNOWN", "ESTIMATED", "UNKNOWN"] = "KNOWN"
    external_ref: str | None
    policy: dict

    @field_validator("level")
    @classmethod
    def exact_commitment_strength(cls, value: str) -> str:
        # Keep the API boundary aligned with the exact five values reconciled from
        # the frozen domain YAML; an invented sixth label must fail validation.
        from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS

        if value not in SOURCE_COMMITMENT_STRENGTHS:
            raise ValueError("level must be one of the frozen commitment strength codes")
        return value


# ORD-0434 step 1.  `PRIVATE` and `INVITEES_ONLY` are stored here but not yet honoured by the
# token read: there is no member data to check against until step 2, and a grant must not read as
# "open" just because the server cannot yet tell who is holding the link.
ShareVisibility = Literal["PRIVATE", "LINK_READ", "INVITEES_ONLY", "PUBLIC"]


class ShareGrantCreate(BaseModel):
    visibility: ShareVisibility
    # What the creating device says it is.  Not authenticated in this step; see the table comment.
    created_by: str = Field(min_length=1, max_length=120)
    expires_at: str | None = None


class ShareGrant(BaseModel):
    id: str
    journey_id: str
    visibility: ShareVisibility
    created_by: str
    expires_at: str | None
    revoked_at: str | None
    created_at: str
    updated_at: str
    # The token is returned only where it can be acted on: list answers carry it so the owner can
    # build or revoke the link, and the create answer carries it once, at creation.
    token: str


class ShareGrantRevoked(BaseModel):
    id: str
    journey_id: str
    visibility: ShareVisibility
    revoked_at: str
    already_revoked: bool


# ORD-0434 step 2.  The member-state words are prefixed on purpose: bare EXPIRED and WITHDRAWN are
# already accommodation-offer statuses (`app/stay_mobility.py:1963`), and borrowing them here would
# make one word mean two things in two different answers.
MemberRole = Literal["OWNER", "ADMIN", "COLLABORATOR"]
MemberState = Literal["MEMBER_IN_FORCE", "MEMBER_LAPSED", "MEMBER_WITHDRAWN"]
JourneyCapability = Literal[
    "JOURNEY_READ", "JOURNEY_UPDATE", "JOURNEY_EXECUTION_UPDATE", "PLACE_EDIT", "MEMBER_MANAGE", "SHARE_GRANT_MANAGE"
]


class JourneyMemberCreate(BaseModel):
    traveler_id: UUID
    role: MemberRole
    invited_by: str = Field(min_length=1, max_length=120)
    # None means the membership was given no end.  No default instant, no far-future sentinel.
    expires_at: str | None = None


class JourneyMembershipInvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_traveler_id: UUID


class JourneyMembershipInvitationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ACCEPT", "DECLINE"]


class JourneyMembershipInvitationInboxItem(BaseModel):
    invitation_id: str
    inviter_display_name: str
    journey_title: str
    city_code: str
    approximate_start_date: str
    proposed_role: Literal["COLLABORATOR"]
    expires_at: str
    state: Literal["OFFERED"]


class JourneyMember(BaseModel):
    id: str
    journey_id: str
    traveler_id: str
    role: MemberRole
    invited_by: str
    expires_at: str | None
    joined_at: str | None
    removed_at: str | None
    created_at: str
    updated_at: str
    member_state: MemberState


class JourneyMemberPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferences: list[PreferenceInput] = Field(default_factory=list, max_length=20)


class CollectiveClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clarification_id: str = Field(min_length=1, max_length=128)
    proposal_id: UUID


class JourneyRoleRule(BaseModel):
    """One role, the capabilities the server holds for it, and the key that states its scope."""

    role: MemberRole
    capabilities: list[JourneyCapability]
    scope_message_key: str
