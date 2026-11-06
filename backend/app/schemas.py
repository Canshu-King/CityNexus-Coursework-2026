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
    trigger_event_id: UUID | None = None
    candidate_count: int = Field(default=3, ge=1, le=5)
    solver_mode: Literal["deterministic", "ortools", "auto", "v2"] | None = None
    city_code: str | None = Field(default=None, min_length=1, max_length=32)
    q: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    service_type: str | None = Field(default=None, max_length=80)
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


class GuardianAutopilotStart(BaseModel):
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


class JourneyAbort(BaseModel):
    """What the traveller says when they call their own trip off (ORD-0520 中止).

    `reason` is required and kept as given: an empty box must not become a stored fact, and the
    door does not summarise or translate what was written.  `actor_traveler_id` names who the
    request speaks for -- the same declared actor the member doors take, and for the same reason
    it is a claim to be compared against the row, not proof of who typed it (see the note on
    `/travelers/{traveler_id}/recycle-bin/journeys` in `contracts/openapi.yaml`).
    """

    reason: str = Field(min_length=1, max_length=240)
    actor_traveler_id: UUID


class JourneyLiveView(BaseModel):
    """The server's own answer to "does this trip belong on 实时行程".

    ORD-0520 ships this so the judgement is not made on the device.  A screen that filtered
    `status != "CANCELLED"` itself would hold a second copy of the rule, and the two could
    disagree the day the rule grows a case the client has never heard of.  `reason_code` is the
    server's token for the exclusion and is NULL when nothing excludes the trip; it is the same
    token the abort door answers with, not a new word.
    """

    included: bool
    reason_code: Literal["JOURNEY_ABORTED"] | None = None


class Journey(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    traveler_id: UUID
    title: str
    city_code: str
    timezone: str
    start_at: datetime
    end_at: datetime
    # Whether `start_at`/`end_at` are really in `timezone`.  A column may hold local wall-clock with
    # no offset, so without this the payload named a zone beside instants that carried none -- the
    # same two-fields-disagreeing shape the execution clock answers for.
    timezone_honoured: bool = True
    budget: Money
    planning_mode: PlanningMode = "balanced"
    status: Literal["DRAFT", "PLANNED", "ACTIVE", "COMPLETED", "CANCELLED"]
    active_plan_version_id: UUID | None = None
    current_activity_id: UUID | None = None
    # ORD-0520: an aborted trip stays in `listJourneys` -- 中止 is not 丢弃, and the recycle bin
    # has its own column and its own door for that.  These three are what makes the difference
    # readable: the moment, the traveller's own reason, and the live-screen decision.
    aborted_at: datetime | None = None
    abort_reason: str | None = None
    live_view: JourneyLiveView


#: ORD-0477: the orders `GET /api/v1/journeys` answers by, named once here so the door's validator,
#: the hand contract (`contracts/openapi.yaml`), FastAPI's own `/openapi.json` and the screen's
#: control all read one vocabulary.  A traveller never sees these tokens; the words they read are
#: in `frontend/src/features/access/accessCopy.ts`, keyed by the tier.
JourneySortTier = Literal["RECENT_UPDATE", "DEPARTURE_DATE", "TITLE"]
JOURNEY_SORT_TIERS: tuple[JourneySortTier, ...] = ("RECENT_UPDATE", "DEPARTURE_DATE", "TITLE")


class RecycleBinJourney(Journey):
    """One trip the recycle bin holds, carrying the two facts a screen must not infer.

    `deleted_at` is the stored value of `journeys.deleted_at`, and `bin_state` is the only word
    this shape can hold: a row appears here because that column is not NULL, so no client has to
    conclude "it was deleted" from its absence in `listJourneys`.
    """

    deleted_at: datetime
    bin_state: Literal["IN_RECYCLE_BIN"]


class TravelerRecycleBinJourney(RecycleBinJourney):
    """One binned trip, answered from one traveller's seat (ORD-0531).

    The two extra names are the whole point of this shape.  A bin row used to say only "this is in
    the bin", which left a screen to decide whether it may offer 找回 by comparing ids it was never
    given a rule about -- the same mistake ORD-0239 names for every other derived state on this
    product.  So the server says it:

    * `ownership` -- whose trip this is, relative to the traveller named in the path.  The column
      already exists (`journeys.traveler_id`); no new column was needed to know it, so none was
      invented.  `NOT_STATED` is reserved for a row the store cannot attribute, and a screen must
      treat it as no authority rather than as a missing value to guess at.
    * `recovery` -- whether this traveller may take the trip back, and if not, in which of the three
      ways not.  It is decided from `journey_role_capabilities`, read through the same helper the
      member doors use, so narrowing that table narrows this answer the same instant.

    A refused row still appears: an owner who shared a trip must be able to see that it is there and
    read why it is not theirs to restore.  What may not happen is a row that looks restorable.
    """

    ownership: Literal[
        "OWNED_BY_THIS_TRAVELER", "OWNED_BY_ANOTHER_TRAVELER", "NOT_STATED"
    ]
    member_role: Literal["OWNER", "ADMIN", "COLLABORATOR"] | None = None
    recovery: Literal[
        "RECOVERY_ALLOWED",
        "RECOVERY_REFUSED_NOT_OWNER",
        "RECOVERY_REFUSED_ROLE_CANNOT_RESTORE",
        "RECOVERY_REFUSED_NO_SEAT",
    ]


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
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=10000)
    captured_at: datetime


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
    # A coordinate means nothing without the system it is written in, and a place
    # read from a provider is only checkable if the payload says which provider.
    # No defaults on purpose: a producer has to state these, so the value that
    # reaches a traveller is always read from the row, never typed at the call site.
    provider_key: str
    coordinate_system: str
    # What the provider actually handed us; None means it was never recorded --
    # which is a different fact from "WGS84" and must not collapse into it.
    source_coordinate_system: str | None


class CollectionFolderCreate(BaseModel):
    """ORD-0444: what creating a shelf asks for -- its name and whose it is.

    The name's bounds copy `TravelerCreate.display_name` above rather than inventing a third rule,
    and the name is stored verbatim: this version has no rename door, so what is written here is
    what the shelf reads for as long as it exists.

    `owner_traveler_id` is a string because `travelers.id` is a string column, and the door refuses
    it with a name when no traveller carries it (`MEMBER_TRAVELER_UNKNOWN`) rather than storing an
    orphan.  It is what the caller asserts, not what this server authenticates -- the anonymous read
    is still the product's default.
    """

    name: str = Field(min_length=1, max_length=120)
    owner_traveler_id: str = Field(min_length=1, max_length=64)


class CollectionItemCreate(BaseModel):
    """Which place goes on which shelf, and the note its owner wrote with it.

    `place_id` is a string because `places.id` is a string column, not a UUID
    (`backend/app/db/models.py` -- `Place.id`), and a UUID-typed parameter would answer a real
    place with `validation.not_an_id`.  `note` is optional and stays optional: "no note was
    written" must not be flattened into "an empty note".
    """

    place_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=280)


class CollectionItem(BaseModel):
    """One place as the shelf answers it -- the reference resolved, not copied.

    `name`, `category`, `city_code`, `location`, `provider_key` and `coordinate_system` are read
    from the `places` row at answer time by `collection_item_schema`, so a shelf cannot show a
    name that the place row no longer holds.  There is no `address` field here, and there is no
    cover image: `places` has neither column nor metadata key for one, so a screen that displayed
    「地址」 would be displaying a label over nothing.
    """

    id: str
    folder_id: str
    place_id: str
    name: str
    category: str
    city_code: str
    location: GeoPoint
    provider_key: str
    coordinate_system: str
    note: str | None
    added_at: str


class CollectionFolder(BaseModel):
    """A shelf and everything on it, in one answer, because the list screen needs both.

    `item_count` is computed from the `items` array of this same payload (`collection_folder_schema`
    in `backend/app/services.py`), so the number a traveller reads cannot describe a different read
    from the rows under it.

    `owner_traveler_id` is the 成员归属 written on the row, and it is what the read is scoped by:
    `GET /api/v1/collections?traveler_id=…` answers the shelves carrying that id and no others.  So
    「我的收藏」 is now a claim the row supports rather than a name a screen hopes.  There is still
    no pagination and no sort parameter, and this shape advertises neither.
    """

    id: str
    name: str
    owner_traveler_id: str
    created_at: str
    updated_at: str
    item_count: int
    items: list[CollectionItem]


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

    Measured on the tree: the cache-hit return (real_city.py) and the main
    return (real_city.py) carry the same 11 keys, so nothing here is optional
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
    # The place's own name, read off the row this activity points at; None when there is no row to
    # read it from.  A traveller's screen says this word first and falls back to `title` only when
    # the store has no name, because `title` is what a planner typed, which is not always the name
    # of the thing itself (`src/lib/mapStopLabels.ts`'s `stopLabel()` follows exactly this order).
    place_name: str | None = None
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
    source: Literal["INITIAL", "REPLAN", "MANUAL"]
    parent_plan_version_id: UUID | None = None
    total_cost: Money
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
    place_ids: list[str] | None = Field(default=None, min_length=2, max_length=12)
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


class AuthoritativeComposeRequest(BaseModel):
    """User-selected bounded input for the V12 multi-day planner."""

    place_ids: list[str] = Field(min_length=2, max_length=12)
    candidate_count: int = Field(default=3, ge=1, le=5)
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
    # ORD-0466: cut the grant for one plan version.  Absent means a journey-level grant, which is
    # what every grant this table already holds is; it is not an unfinished choice.
    plan_version_id: str | None = None


class ShareGrant(BaseModel):
    id: str
    journey_id: str
    visibility: ShareVisibility
    created_by: str
    expires_at: str | None
    revoked_at: str | None
    created_at: str
    updated_at: str
    plan_version_id: str | None = None
    # The token is returned only where it can be acted on: list answers carry it so the owner can
    # build or revoke the link, and the create answer carries it once, at creation.
    token: str


class ShareGrantRevoked(BaseModel):
    id: str
    journey_id: str
    visibility: ShareVisibility
    revoked_at: str
    already_revoked: bool


# ORD-0465 -- what the holder of a share token is shown when the grant still reads.
#
# Why the stops ride on THIS door and are not fetched from another one: every other journey door is
# either one that writes while you read it (`/journeys/{id}/experience`, which is why this screen does
# not open it) or one that never heard of a grant (`/journeys/{id}`, `/plans/{id}`).  A landing screen
# that assembled itself from those would keep working after the deadline passed and after the owner
# withdrew the link -- which would make both of those facts decorative.  One door, one check.
#
# The three models below describe the `active_plan` half of that body.  The door assembles its own
# dict rather than declaring a response model, because ORD-0466's contract is that `shared_plan` is
# ABSENT on a journey-level grant, and a response model with a default would put a null in that key
# and read as "this grant covered a version and it is missing".  `contracts/openapi.yaml` is where
# the body's shape is written down, and
# `tests/test_qoder_ord0465_shared_landing.py` reads it from there.
class SharedActivityCoordinate(BaseModel):
    """Where the place an activity names actually is, in the capability vocabulary this product froze.

    The four `state` words are `CAPABILITY_STATES` (`app/anywhere_capability.py`) and the `reason`
    words are the `REASON_*` beside it.  They are re-declared here as a `Literal` because a contract
    needs the enumeration, and `tests/test_qoder_ord0465_shared_landing.py` pins both lists against
    the source so this cannot drift into a fifth word.  Re-declaring and pinning beats importing:
    `app/anywhere_capability.py` is a service module and this file is the contract.

    Nothing here invents a position.  When there is no place row to join, or the row carries no
    coordinate, `latitude`/`longitude` stay None and `state`/`reason` say which of those two cases this
    is -- the same answer `_active_plan_payload` gives the owner's own screen (`app/stay_mobility.py`).
    """

    state: Literal["READY", "HYDRATABLE", "UNKNOWN_RETRYABLE", "UNAVAILABLE"]
    reason: str | None
    latitude: float | None
    longitude: float | None
    coordinate_system: str | None
    source_coordinate_system: str | None = None


class SharedStop(BaseModel):
    """One stop, projected for someone who did not plan it.

    Deliberately absent, and not by accident:
      - `locked` and `protected` are the owner's editing flags. A reader cannot unlock anything, so
        showing them would advertise a control this screen does not have (ORD-0385: copy may not
        exceed the mechanism).
      - `metadata` is the store's own record of what a transition wrote; the position a traveller is
        shown comes from the place row, which is projected as `coordinate` instead.
      - `city_service_id` and `place_id` are identities this screen never uses to draw anything.
    """

    id: UUID
    sequence_no: int
    title: str
    # The place's own name, read off the row this activity points at; None when there is no row to
    # read it from.  The screen's rule is `place_name` first and `title` only when the store has no
    # name -- `title` is what a planner typed, which is not always the name of the thing itself.
    place_name: str | None
    activity_type: str
    start_at: datetime
    end_at: datetime
    coordinate: SharedActivityCoordinate


class SharedPlan(BaseModel):
    """The plan this grant lets its holder read, as a list of stops and nothing else.

    `legs` are absent on purpose: a leg's geometry comes from a routing provider, and on this
    deployment that provider is not configured (`project-provider-reality-osm`), so the only shapes
    available are frozen fixture geometry.  Drawing a route line the server cannot honestly name is a
    second claim about how the traveller moves, and none of the four states this screen exists to
    report needs it.
    """

    id: UUID
    version_no: int
    # Which generation of the trip this is, in the server's own words rather than a screen's guess.
    # `source` has traveller-facing labels already on the front
    # (`frontend/src/lib/displayVocabulary.ts`, `PLAN_SOURCE`), and all three of its values are in
    # that table, so the share screen can name a first generation apart from a replan without printing
    # a machine word.  The plan's `status` is deliberately NOT carried: two of its five values
    # (`SELECTED`, `SUPERSEDED`) are absent from the shared status table
    # (`frontend/src/i18n/travelerLabels.ts`), so a screen that received the field would be handed a
    # token it can only echo.  `tests/test_qoder_ord0465_shared_landing.py` pins that every value of
    # every word this answer offers has a label, which is why `source` stayed and `status` did not.
    source: Literal["INITIAL", "REPLAN", "MANUAL"]
    stops: list[SharedStop]


# ORD-0434 step 2.  The member-state words are prefixed on purpose: bare EXPIRED and WITHDRAWN are
# already accommodation-offer statuses (`app/stay_mobility.py`), and borrowing them here would
# make one word mean two things in two different answers.
MemberRole = Literal["OWNER", "ADMIN", "COLLABORATOR"]
MemberState = Literal["MEMBER_IN_FORCE", "MEMBER_LAPSED", "MEMBER_WITHDRAWN"]
JourneyCapability = Literal[
    "JOURNEY_READ", "JOURNEY_UPDATE", "PLACE_EDIT", "MEMBER_MANAGE", "SHARE_GRANT_MANAGE"
]


class JourneyMemberCreate(BaseModel):
    traveler_id: UUID
    role: MemberRole
    invited_by: str = Field(min_length=1, max_length=120)
    # None means the membership was given no end.  No default instant, no far-future sentinel.
    expires_at: str | None = None


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


class JourneyRoleRule(BaseModel):
    """One role, the capabilities the server holds for it, and the key that states its scope."""

    role: MemberRole
    capabilities: list[JourneyCapability]
    scope_message_key: str


# --- ORD-0503: the two member write doors, and the one read door that carries the cap ----------
# `actor_traveler_id` is a claim the caller makes about who is asking, not an authenticated session:
# this product has no auth (`app/main.py` has no dependency that resolves a caller), and a door that
# pretended otherwise would be the invented-default defect again.  What the door does with the claim
# is the load-bearing part -- it is checked against the member set and the seeded ACL table, so a
# caller who names somebody else is refused by name rather than trusted.


class JourneyMemberRoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: MemberRole
    actor_traveler_id: UUID


class JourneyMemberSelfExit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    traveler_id: UUID


class MemberRoleLimits(BaseModel):
    """The cap this server enforces, answered by the server rather than typed on a screen.

    `max_administrators` is the same module constant the role door compares against, and
    `contracts/openapi.yaml` names it a third time as `x-citynexus-max-administrators-per-journey`;
    `tests/test_qoder_ord0503_member_doors.py` fails if those two spellings ever disagree, so the
    roster line on the screen cannot quietly diverge from what the door refuses on.
    """

    max_administrators: int = Field(ge=1)
    admin_count_in_force: int = Field(ge=0)
    roles: list[MemberRole]
# --- ORD-0437: the POI text/link import channel ---------------------------------------------
# The parse side carries no provider field on purpose: extraction is local, so a response that
# claimed a provider would be a lie waiting to be written.  `unsupported_forms` is the named list the
# screen reads to say what it cannot do, which is what stops a link-only paste from failing quietly.


class PoiImportParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=20000)


class PoiImportItem(BaseModel):
    title: str
    note: str | None = None
    char_count: int
    status: Literal["RESOLVED", "AMBIGUOUS", "UNRESOLVED"]
    place_id: str | None = None
    place_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    coordinate_system: str | None = None
    matches: list[str] = Field(default_factory=list)
    addable: bool = False


class PoiImportParseResponse(BaseModel):
    source_channel: Literal["LOCAL_TEXT_PARSER"]
    forms: list[str]
    unsupported_forms: list[str]
    link_hosts: list[str]
    counts: dict[str, int]
    items: list[PoiImportItem]
    char_count: int


class PoiImportAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_ids: list[str] = Field(min_length=1, max_length=30)
    # A day the traveller did not choose still has to be explained, so the source of the day is a
    # field of the write rather than a detail the panel happens to render.
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class PoiImportAddedActivity(BaseModel):
    id: str
    title: str
    place_id: str
    sequence_no: int
    start_at: str
    end_at: str


class PoiImportAddResponse(BaseModel):
    journey_id: str
    plan_version_id: str
    day: str
    day_source: Literal["TRAVELLER_CHOSEN", "JOURNEY_FIRST_DAY"]
    added: list[PoiImportAddedActivity]
    # A replay of the same tap is not a second success: the places that were already on that day
    # come back here by name, so the panel can say 已在计划里 instead of counting them again.
    already_present: list[str] = Field(default_factory=list)
    # Where the client must read back from before anything may say 已加入.
    readback_path: str


class JourneyCheckinCreate(BaseModel):
    """ORD-0476: what stamping a footprint asks for -- one target, optionally a note.

    Exactly one of `activity_id` / `place_id` must be present, and the door refuses both readings of
    that with a name (`CHECKIN_TARGET_REQUIRED`, `CHECKIN_TARGET_AMBIGUOUS`): the target is what the
    duplicate promise is keyed on, so "neither" would be a stamp about nothing and "both" would be
    two stamps pretending to be one.

    There is deliberately no `stamped_at` here.  The moment is recorded by the server; a caller-supplied
    instant would be the traveller's device clock presented as a store fact, which is the shape this
    project has had to fix more than once.  An offline queue that carries its own device time is a
    different input and arrives as its own named field, with an offset.
    """

    activity_id: str | None = Field(default=None, min_length=1, max_length=64)
    place_id: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=280)


class JourneyCheckin(BaseModel):
    """One stamp as the server holds it, with the target resolved at read time and never copied.

    `activity_title` / `place_name` come from the rows the stamp points at
    (`checkin_schema` in `backend/app/services.py`), so a stamp cannot show a name the target no
    longer carries.  Both are nullable, and that null is a real fact rather than a placeholder:
    either row can be deleted underneath a stamp (`ON DELETE SET NULL`), and the footprint survives
    its target by design.

    `target_kind` and `target_key` are the stored decision the door made, not a re-derivation, so
    they still say what was stamped after the target is gone.
    """

    id: str
    journey_id: str
    activity_id: str | None
    place_id: str | None
    target_kind: Literal["ACTIVITY", "PLACE"]
    target_key: str
    stamped_at: str
    note: str | None
    source: Literal["FOREGROUND_WEB"]
    activity_title: str | None
    activity_type: str | None
    place_name: str | None
    place_category: str | None
