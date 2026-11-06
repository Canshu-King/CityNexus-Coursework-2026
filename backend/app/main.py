from __future__ import annotations

import asyncio
import json
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.agent.autorun import (
    autorun_status,
    enrolled_journey_ids,
    ensure_enrollment,
    start_autorun,
    stop_autorun,
)
from app.agent.contracts import (
    AgentAction,
    AgentMode,
    AuthoritativeActionFacts,
    AuthorizationDecision,
)
from app.agent.delegation_policy import (
    evaluate_action,
    get_or_create_policy,
    policy_from_model,
    policy_payload,
    update_policy,
)
from app.agent.gateway import AgentToolGateway
from app.calendar_export import (
    ICS_MEDIA_TYPE,
    CalendarActivity,
    build_calendar,
    content_disposition,
)
from app.concurrency_guards import journey_write_lock
from app.replanning.commitment_remap import (
    RemapConflict as CommitmentRemapConflict,
    resolve_commitment_target,
)
from app.agent.guardian import guardian_status, scan_guardian
from app.agent.mission import create_mission, mission_payload
from app.agent.mission_plan import mission_plan_for_model
from app.agent.runtime_events import normalize_hermes_event
from app.agent.orchestrator import configured_journey_agent
from app.agent.predictive import (
    build_resilience_snapshot,
    counterfactual_payload,
    latest_snapshot,
    simulate_counterfactuals,
    snapshot_payload,
)
from app.agent.principal import resolve_agent_principal
from app.agent.ai_availability import ai_availability_state, ai_state_from_failure_reason
from app.agent.runtime_identity import runtime_status
from app.agent.runtime import (
    AgentRuntimeUnavailable,
    DeepSeekJourneyAgent,
    persisted_agent_run_payload,
    unavailable_run_payload,
)
from app.agent.signal_pipeline import fixture_payload, set_fixture_state
from app.capabilities import (
    CAPABILITY_CODES,
    canonicalize_capabilities,
    summarize_capabilities,
)
from app.commerce.idempotency import IdempotencyConflict, canonical_request_signature
from app.commerce.order_pricing import OrderLineRequest, OrderPricingError
from app.commerce.service_impl import (
    CommerceConflict,
    load_catalog,
    service_id_for_activity,
)
from app.commerce.service_impl import (
    cancel_booking as commerce_cancel_booking,
)
from app.commerce.service_impl import (
    create_booking as commerce_create_booking,
)
from app.commerce.service_impl import (
    create_order as commerce_create_order,
)
from app.commerce.service_impl import (
    create_transaction as commerce_create_transaction,
)
from app.commerce.service_impl import (
    get_booking_slots as commerce_get_booking_slots,
)
from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS
from app.commerce.validators import CommerceValidationError, validate_idempotency_key
from app.core.audit import record_audit
from app.core.config import settings
from app.core.temporal import (
    MAX_JOURNEY_DAYS,
    MIN_JOURNEY_DAYS,
    derive_journey_temporal_shape,
)
from app.db.models import (
    AccommodationOfferSnapshot,
    ActionApproval,
    Activity,
    AgentRun,
    AgentRunStep,
    AuditLog,
    AutopilotEnrollment,
    CityService,
    CitySignalFixture,
    CollectionFolder,
    CollectionItem,
    CounterfactualSimulation,
    EdgeDevice,
    ExternalReservationDraft,
    IdempotencyKey,
    ImpactAssessment,
    Journey,
    JourneyCheckin,
    JourneyLeg,
    JourneyConstraint,
    JourneyMission,
    JourneyMember as JourneyMemberModel,
    JourneyRoleCapability,
    JourneyShareGrant,
    JourneyOutcomeEvent,
    JourneyTripContext,
    MobilityFact,
    NotificationEvent,
    OfflineJourneySnapshot,
    PaymentTransaction,
    Place,
    PlanVersion,
    Preference,
    ProviderObservation,
    RealPlaceCandidate,
    StayJourneyOption,
    TransportReservation,
    Traveler,
    TravelerCapability,
    TravelerTwinSnapshot,
    WebObservation,
)
from app.db.models import (
    Booking as BookingModel,
)
from app.db.models import (
    Commitment as CommitmentModel,
)
from app.db.models import (
    Event as EventModel,
)
from app.db.models import (
    JourneyTwin as JourneyTwinModel,
)
from app.db.models import (
    Order as OrderModel,
)
from app.db.models import (
    ReplanProposal as ReplanProposalModel,
)
from app.db.session import SessionLocal, engine, ensure_schema, get_session
from app.db.write_deadline import (
    SCENARIO_AGENT_COMMANDS,
    SCENARIO_DEFAULT_WRITE,
    SCENARIO_PLANS_GENERATE,
    WriteDeadlineExceeded,
    arm_request_budget,
    assert_configuration_reachable,
    boundary_verdict,
    commit_within_budget,
    reset_request_budget,
    start_request_budget,
    write_attempt,
)
from app.demo_scene import (
    DEMO_SCENE_HEADER,
    DEMO_STATE_SUPPLY_UNAVAILABLE,
    SceneIdempotencyConflict,
    demo_scene_token_denied,
    ensure_demo_scene,
)
from app.error_receipt import (  # ORD-0332: 一枚可复述回执号，唯一字段名
    RECEIPT_FIELD,
    arm_receipt_seed,
    clear_receipt_seed,
    current_receipt_seed,
    receipt_id_for,
)
from app.security.credential_literals import (  # ORD-0307: 字面量凭据的常驻启动门
    startup_report as credential_gate_startup,
)
from app.execution.clock import execution_clock
from app.execution.traveler_twin import (
    clear_learned,
    learn_from_journey,
    planning_preference_items,
    capability_payload,
    refresh_snapshot,
)
from app.execution.traveler_twin import (
    snapshot_payload as traveler_twin_payload,
)
from app.integrations.ai.litellm_provider import LiteLLMExplanationProvider
from app.integrations.ai.copilot_contract import (
    CopilotInterpretation,
    classify_copilot_message,
    disruption_event_type,
    disruption_severity,
    simulate_what_if,
    validate_proposed_action,
)
from app.integrations.ai.resilient import ResilientAIExplanationService
from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.integrations.ai.semantic_compiler import (
    apply_semantic_policy,
    compile_semantic_envelope,
)
from app.integrations.ai.event_targeting import resolve_event_target
from app.integrations.ai.web_research import WebResearchProvider
from app.integrations.hermes.native_client import HermesNativeClient, native_hermes_status
from app.integrations.china.amap import amap_status, provider_for_region
from app.integrations.context.open_meteo import OpenMeteoContextProvider
from app.integrations.context.weather_runtime import refresh_weather_runtime
from app.integrations.discovery.real_city import (
    capture_real_city_candidates,
    live_failure_reason,
    normalize_opening_hours,
)
from app.integrations.discovery.orchestration import orchestrate_discovery
from app.integrations.inbox.vision import extract_reservation_image, extract_reservation_text
from app.integrations.inbox.semantic_intake import (
    journey_interpreter_context,
    journey_memory_payload,
    prepare_semantic_intake,
)
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.integrations.mobility.snapshot_capture import (
    MobilitySnapshotCaptureError,
    MobilitySnapshotCaptureResult,
)
from app.integrations.mock.commerce_providers import MockBookingProvider, MockPaymentProvider
from app.integrations.push import push_capability
from app.intelligence.feasibility_envelope import FeasibilityEnvelope, disruption_risk_codes
from app.intelligence.impact_resolver import (
    impact_payload,
    persist_impact_assessment,
    resolve_impact,
)
from app.intelligence.opportunity_window import build_window
from app.intelligence.reason_codes import ReasonCode, VerifiedExplanationFacts
from app.intelligence.replan_diff import PlanActivitySnapshot, diff_plans
from app.operations.v9 import (
    _execution_truth,
    authenticate_edge,
    confirm_reservation_draft,
    edge_snapshot,
    import_reservation_artifact,
    materialize_stay_option,
    notify,
    record_edge_action,
    record_edge_observation,
    record_location,
    refresh_offline_snapshot,
    register_edge_device,
    register_push_subscription,
    set_luggage_context,
)
from app.planning.solver_contract import (
    ReplanSolverProblem,
    SolverAnchor,
    SolverReplacementOption,
)
from app.planning.solver_runtime import (
    SolverAttempt,
    generate_solver_candidates,
    normalize_solver_mode,
)
from app.planning.solver_v2 import (
    SolverV2Anchor,
    SolverV2Option,
    SolverV2Plan,
    SolverV2Problem,
    SolverV2Slot,
    solve_v2,
    solver_v2_problem_fingerprint,
)
from app.replanning.candidate_evidence import verified_interest_preference_fit
from app.replanning.feasibility import (
    CANONICAL_RULE_CODES,
    FeasibilityActivity,
    activity_from_record,
    build_context,
    constraint_write_refusal,
    evaluate_feasibility,
)
from app.replanning.mock_replan_engine import partition_execution, rank_closure_replacements
from app.replanning.opportunity_window_family import (
    WindowReplacementInput,
    evaluate_window_replacement,
)
from app.planning.hard_recovery import (
    acknowledge_recovery,
    open_recovery_event,
    record_replan_outcome,
    recovery_flags,
    recovery_state,
    resolve_recovery_events,
)
from app.replanning.provider_snapshots import (
    HARD_FEASIBLE_QUALITIES,
    MobilitySnapshot,
    TravelEstimateFact,
)
from app.replanning.protected_commitments import active_c4_hard_constraints
from app.replanning.verified_scoring import score_replan_candidate
from app.schemas import (
    AgentApprovalRequest,
    AgentCommandRequest,
    AgentPolicyPayload,
    AgentToolCall,
    BookingCreate,
    CitySignalFixtureRequest,
    ConstraintInput,
    EdgeActionRequest,
    EdgeDeviceCreateRequest,
    EdgeObservationRequest,
    EventCreate,
    GeneratePlansRequest,
    AuthoritativeComposeRequest,
    CatalogHydrationRequest,
    GuardianAutopilotStart,
    JourneyCreate,
    JourneyPatch,
    LocationObservationRequest,
    LuggageContextRequest,
    MobilityModeRequest,
    OrderCreate,
    OutcomeEventCreate,
    PreferenceInput,
    PushSubscriptionRequest,
    RealCityDiscoveryRequest,
    RealCityIntelligenceRequest,
    ReplanGenerateRequest,
    ReservationImportRequest,
    ReservationDraftPatchRequest,
    ReservationTextExtractionRequest,
    ReservationVisionRequest,
    ResilienceRefreshRequest,
    ScheduleChangeRequest,
    ShareGrant,
    ShareGrantCreate,
    ShareGrantRevoked,
    SharedActivityCoordinate,
    SharedPlan,
    SharedStop,
    JourneyCheckin as JourneyCheckinSchema,
    JourneyCheckinCreate,
    JourneyMember,
    JourneyMemberCreate,
    JourneyMemberRoleChange,
    JourneyMemberSelfExit,
    JourneyRoleRule,
    MemberRoleLimits,
    StayAutopilotRecoveryRequest,
    StayModeChangeRequest,
    StayPlanGenerateRequest,
    TransactionCreate,
    TravelerCreate,
    CapabilityInput,
    TripStudioAcceptRequest,
    TripStudioPreviewRequest,
    WeatherDisruptionRequest,
    WebResearchRequest,
)
from app.schemas import (
    AgentPolicy as AgentPolicySchema,
)
from app.schemas import (
    Booking as BookingSchema,
)
from app.schemas import (
    BookingSlot as BookingSlotSchema,
)
from app.schemas import (
    Commitment as CommitmentSchema,
)
from app.schemas import (
    Event as EventSchema,
)
from app.schemas import (
    JOURNEY_SORT_TIERS,
)
from app.schemas import (
    Journey as JourneySchema,
)
from app.schemas import (
    JourneyAbort,
)
from app.schemas import (
    JourneyTwin as JourneyTwinSchema,
)
from app.schemas import (
    Order as OrderSchema,
)
from app.schemas import (
    Place as PlaceSchema,
)
from app.schemas import (
    RecycleBinJourney,
    TravelerRecycleBinJourney,
)
from app.schemas import (
    CollectionFolder as CollectionFolderSchema,
)
from app.schemas import (
    CollectionFolderCreate,
)
from app.schemas import (
    CollectionItem as CollectionItemSchema,
)
from app.schemas import (
    CollectionItemCreate,
)
from app.schemas import (
    RealCityDiscoveryResponse,
)
from app.schemas import (
    ReplanProposal as ReplanProposalSchema,
)
from app.schemas import (
    Transaction as TransactionSchema,
)
from app.schemas import (
    PoiImportAddRequest,
    PoiImportAddResponse,
    PoiImportAddedActivity,
    PoiImportItem,
    PoiImportParseRequest,
    PoiImportParseResponse,
)
from app.poi_import import (
    FORM_PASTED_TEXT as poi_import_FORM_PASTED_TEXT,
    STATUS_RESOLVED as poi_import_STATUS_RESOLVED,
    counts as poi_import_counts,
    detect_forms as poi_import_detect_forms,
    extract as poi_import_extract,
    link_hosts as poi_import_link_hosts,
    resolve as poi_import_resolve,
)
from app.services import (
    _journey_constraints_with_capabilities,
    checkin_schema,
    collection_folder_schema,
    collection_item_schema,
    constraint_schema,
    generate_plans,
    journey_live_view,
    journey_schema,
    parse_dt,
    place_schema,
    plan_schema,
    preference_schema,
    recycle_bin_entry_schema,
    seed_demo,
    service_schema,
    traveler_schema,
    validate_plan_feasibility,
    validate_selected_plan_feasibility,
)
from app.stay_mobility import (
    StayItineraryConflict,
    activity_coordinate,
    capture_accommodation_offers,
    capture_mobility_facts,
    experience_payload,
    fact_payload,
    generate_stay_options,
    journey_boundaries,
    journey_temporal_shape,
    option_payload,
)
from app.planning.authoritative_v12 import compose_authoritative_multiday
from app.stay_generation_operations import (
    ABANDONED,
    FAILED,
    GenerationStoreBusy,
    abandon_operation,
    operation_payload,
    operations_for_journey,
    read_operation,
    reconcile_interrupted_operations,
    submit_operation,
    wait_for_operation,
)

from app.catalog_hydration_operations import (  # ORD-0095-1: the destination-scoped sibling
    HydrationStoreBusy,
    accept_hydration,
    abandon_operation as abandon_hydration,
)
from app.catalog_hydration_operations import (
    operation_payload as hydration_payload,
)
from app.catalog_hydration_operations import (  # ORD-0522: the board's own supply read
    board_supply,
)
from app.catalog_hydration_operations import (
    read_operation as read_hydration,
)
from app.catalog_hydration_operations import (
    reconcile_interrupted_operations as reconcile_hydrations,
)
from app.anywhere_capability import (
    GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE,
    evaluate_capability,
    # ORD-0522: the one classifier of where a place row came from.  Imported rather than re-written,
    # so this screen cannot grow a second opinion about what counts as supply.
    _source_kind,
)

assert_configuration_reachable()   # ORD-0296: refuse to boot a deadline that cannot mean anything
CREDENTIAL_GATE = credential_gate_startup()   # ORD-0307: report→只报计数；strict→命中即拒启动
ensure_schema()
# Rows still open at this point were left by a process that no longer exists, so no thread
# here can ever finish them; they are closed as WORKER_LOST rather than left to be polled
# (see app/stay_generation_operations.py).
reconcile_interrupted_operations()
# Catalog hydrations run on the same promise: a job is durable, a worker is not.  Wrapped,
# because startup must not be held hostage by a cleanup pass on a database that predates this
# table - the row that could not be closed is still reported as open, not silently skipped.
try:  # pragma: no cover - startup hygiene, exercised by the operation's own doors
    reconcile_hydrations()
except Exception:  # noqa: BLE001
    logging.getLogger("citynexus.catalog_hydration").exception(
        "could not reconcile interrupted catalog hydrations at startup"
    )
app = FastAPI(title="CityNexus MVP API", version="0.1.0")
logger = logging.getLogger(__name__)
_trip_studio_accept_lock = RLock()


def create_app() -> FastAPI:
    """Return the configured application for ASGI servers and tests."""
    return app


def trace_id() -> str:
    return str(uuid4())


def error(
    code: str,
    message: str,
    *,
    status: int,
    details: dict | None = None,
    message_key: str | None = None,
    reason_code: str | None = None,
) -> JSONResponse:
    """One envelope for every rejection, and one stable key inside it.

    ORD-0055: a screen must not translate an English sentence.  `message_key` is what it
    looks up, `details` is what it interpolates, and `message` stays what a log line and an
    untranslated fallback are allowed to read.  The default is derived from `code` - never
    from `message` - so a reworded sentence cannot silently change a key.

    `reason_code` (ORD-0101) names *which* condition refused when one code covers several -
    `VALIDATION_ERROR` is correct for both "this city has no catalog" and "the catalog could
    not satisfy the constraints", and those two sentences mean different things to a
    traveller.  It is emitted only when a raise site supplies it, so every existing body is
    unchanged, and `message` is never rewritten to carry it.
    """

    body: dict[str, object] = {
        "code": code,
        "message": message,
        "message_key": message_key or f"error.{code.lower()}",
        "trace_id": trace_id(),
        "details": details,
    }
    if reason_code:
        body["reason_code"] = reason_code
    # ORD-0332: one number the traveller can read back to us.  Written only when there is a request
    # to be a receipt for -- an empty string would be a number-shaped lie for the screen to refuse.
    seed = current_receipt_seed()
    receipt = receipt_id_for(seed, code)
    if receipt is not None:
        body[RECEIPT_FIELD] = receipt
    # The other half of "念得出就查得到": the receipt, the route, the class and the envelope's own
    # trace id on one line.  5xx at WARNING because that is the class a screen calls 结果未知 and an
    # operator must be able to grep without re-configuring anything; 4xx at INFO so a routine
    # validation refusal cannot flood the log.  `details` is deliberately not dumped here.
    where = f"{seed.method} {seed.path}" if seed is not None else "no-request-context"
    line = (f"{RECEIPT_FIELD}={receipt or 'NOT-EMITTED'} code={code} status={status} "
            f"route={where} trace_id={body['trace_id']}")
    (logger.warning if status >= 500 else logger.info)(line)
    return JSONResponse(status_code=status, content={"error": body})


# Pydantic's `type` is the stable half of a validation error; `msg` is prose that pydantic
# itself rewrites between versions.  Anything unlisted still gets a key built from `type`
# (below), so a new rule cannot reach a screen without one.
VALIDATION_MESSAGE_KEYS = {
    "missing": "validation.field_missing",
    "value_error.missing": "validation.field_missing",
    "string_type": "validation.not_text",
    "int_parsing": "validation.not_whole_number",
    "int_gt": "validation.too_small",
    "greater_than": "validation.too_small",
    "greater_than_equal": "validation.too_small",
    "less_than": "validation.too_large",
    "too_short": "validation.too_short",
    "too_long": "validation.too_long",
    "json_invalid": "validation.not_json",
    "uuid_parsing": "validation.not_an_id",
    "date_from_datetime_parsing": "validation.not_a_date",
    "datetime_from_date_parsing": "validation.not_a_date",
    "value_error.datetime": "validation.not_a_date",
    "value_error.uuid.byte_length": "validation.not_an_id",
    "literal_error": "validation.not_an_option",
    "extra_forbidden": "validation.unknown_field",
}

HTTP_MESSAGE_KEYS = {
    404: "resource.not_found",
    409: "request.conflict",
    422: "request.invalid",
    403: "request.forbidden",
    423: "request.locked",
}


def validation_message_key(item: dict) -> str:
    """Name a validation failure by its rule type, never by its English message."""

    kind = str(item.get("type") or "")
    known = VALIDATION_MESSAGE_KEYS.get(kind)
    if known:
        return known
    cleaned = kind.replace("_", ".").strip(".") or "unknown"
    return f"validation.{cleaned}"


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = []
    for item in exc.errors():
        payload = {
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg"),
            "type": item.get("type"),
        }
        # `loc` decides *which field* to mark and `message_key`/`type` decide *what to say*;
        # the two are separate on purpose, because matching on `msg` was how a reworded
        # pydantic string used to turn a form back into an untranslatable block.
        payload["message_key"] = validation_message_key(payload)
        safe_errors.append(payload)
    return error(
        "VALIDATION_ERROR",
        "Request validation failed",
        status=422,
        details={"errors": safe_errors},
        message_key="request.invalid",
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = {404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_ERROR"}.get(
        exc.status_code, "REQUEST_ERROR"
    )
    return error(
        code,
        detail,
        status=exc.status_code,
        # ORD-0101: a raise site that knows *which* condition refused may attach the name and
        # the counts without rewriting `message`. Both are read as optional attributes, so
        # every existing `raise HTTPException(...)` renders exactly as it did before.
        reason_code=getattr(exc, "reason_code", None),
        details=getattr(exc, "error_details", None),
        message_key=getattr(exc, "message_key", None)
        or HTTP_MESSAGE_KEYS.get(exc.status_code, f"error.{code.lower()}"),
    )


# The journey window is one authority (`app/core/temporal.py`) and every public entry that
# needs it gets here, so the conversion from "domain rule" to "HTTP answer" happens once
# rather than at each door - and each door keeps the same code and the same numbers.
# Before this, a nine-day trip reached the deepest call and came back as
# `500` + `ValueError: V13_JOURNEY_DAYS_OUT_OF_BOUNDS:9`; on a phone that reads as a
# frozen button, and it left an unusable Journey behind (ORD-0033).  A ValueError that is
# not one of these named rejections is still a bug, so it is re-raised and keeps its 500:
# this handler must never launder an unexpected failure into a 4xx.
TEMPORAL_REJECTIONS = {
    "V13_JOURNEY_DAYS_OUT_OF_BOUNDS": (
        f"The trip is longer than the supported journey window of {MAX_JOURNEY_DAYS} days. "
        f"Shorten the dates and try again."
    ),
    "V13_INVALID_ANCHOR_WINDOW": "The departure must be after the arrival.",
}

# A screen looks up this key; the English string above stays what a log reads and what an
# unknown key falls back to.  A test pins that these two maps share exactly one domain, so a
# new named rejection cannot reach a client without a key of its own.
TEMPORAL_MESSAGE_KEYS = {
    "V13_JOURNEY_DAYS_OUT_OF_BOUNDS": "journey.days_out_of_bounds",
    "V13_INVALID_ANCHOR_WINDOW": "journey.anchor_order",
}


@app.exception_handler(WriteDeadlineExceeded)
def write_deadline_answer(_: Request, exc: WriteDeadlineExceeded) -> JSONResponse:
    """The server's own words for "I ran out of write budget".

    ORD-0280 lets a screen show what the server said and nothing else, so this has to be a sentence
    the server actually produced, carrying the two numbers a reader can check it against.
    """

    return error(exc.code, str(exc), status=503, details=exc.details())


@app.middleware("http")
async def error_receipt_per_request(request: Request, call_next):
    """ORD-0332: one request, one receipt seed -- so a rejection has a number worth reading aloud.

    `trace_id` is minted per envelope and cannot be reproduced by a retry; this arms the *call's*
    identity instead, from which `error()` derives the same `error_receipt_id` every time the same
    call fails the same way.  Nothing is hashed that a retry would change: no clock, no worker, no
    process.  Outside a request -- a job, a script, a direct call into `error()` -- no seed exists, and
    the field is simply not written.
    """

    arm_receipt_seed(request)
    try:
        return await call_next(request)
    finally:
        clear_receipt_seed()


@app.middleware("http")
async def write_budget_per_request(request: Request, call_next):
    """ORD-0327: one request, one write clock -- so a traveller's press cannot wait forever.

    Routes used to opt in one at a time (two scenarios did).  This arms a clock for every request, so
    the routes that never named a scenario are no longer the ones a lost lock answers with a bare 500:
    past budget, `write_contention_answer` below replies under `SERVER_WRITE_DEADLINE_EXCEEDED` (503,
    with the two numbers in it); inside budget, the original error propagates untouched and this
    middleware adds nothing.  A route that names its own scenario still replaces the clock when it
    calls `start_request_budget`, and nothing here reaches background jobs -- they run outside any
    request context, so their `OperationalError` stays the error their handlers already catch.
    """

    arm_request_budget(SCENARIO_DEFAULT_WRITE)
    try:
        return await call_next(request)
    finally:
        reset_request_budget()


@app.exception_handler(OperationalError)
async def write_contention_answer(request: Request, exc: OperationalError) -> JSONResponse:
    """The boundary's one judge for "this request's write budget is gone because it lost the lock".

    Two directions, both measured (ORD-0327 ②): `boundary_verdict` returns None unless a clock is
    armed, expired, and the error is lock contention -- and on None this handler re-raises the very
    same exception object, so inside budget the traveller's answer stays exactly today's answer with
    no new name in it.  When the judge does speak, the response is the ORD-0280 envelope: the
    server's own sentence plus the two numbers, never a contentless 500.

    A route that catches `OperationalError` itself keeps handling it first -- this only sees errors
    that were already going to reach the framework.
    """

    named = boundary_verdict(exc)
    if named is None:
        raise exc
    return write_deadline_answer(request, named)


@app.exception_handler(ValueError)
async def temporal_rejection(_: Request, exc: ValueError) -> JSONResponse:
    code, _, argument = str(exc).partition(":")
    if code not in TEMPORAL_REJECTIONS:
        raise exc
    details: dict[str, object] = {"reason": code}
    if code == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS":
        if argument.strip().isdigit():
            details["actual_days"] = int(argument.strip())
        details["min_days"] = MIN_JOURNEY_DAYS
        details["max_days"] = MAX_JOURNEY_DAYS
    return error(
        code,
        TEMPORAL_REJECTIONS[code],
        status=422,
        details=details,
        message_key=TEMPORAL_MESSAGE_KEYS[code],
    )


def require(session: Session, model: type, identifier: str, label: str):
    item = session.get(model, identifier)
    if item is None:
        raise HTTPException(404, f"{label} {identifier} was not found")
    return item


def _record_outcome_event(
    session: Session,
    journey: Journey,
    *,
    activity: Activity | None,
    event_type: str,
    actor_type: str,
    occurred_at_actual: str | None = None,
    payload: dict[str, Any] | None = None,
    related_proposal_id: str | None = None,
    related_agent_run_id: str | None = None,
) -> JourneyOutcomeEvent:
    now = datetime.now(timezone.utc).isoformat()
    event = JourneyOutcomeEvent(
        id=str(uuid4()),
        journey_id=journey.id,
        activity_id=activity.id if activity is not None else None,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=str(journey.traveler_id),
        occurred_at_actual=occurred_at_actual or now,
        planned_start_at=activity.start_at if activity is not None else None,
        planned_end_at=activity.end_at if activity is not None else None,
        source="DOMAIN",
        related_proposal_id=related_proposal_id,
        related_agent_run_id=related_agent_run_id,
        payload_json=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        created_at=now,
    )
    session.add(event)
    session.flush()
    record_audit(
        session,
        action="journey.outcome.recorded",
        entity_type="JourneyOutcomeEvent",
        entity_id=event.id,
        journey_id=journey.id,
        actor_type=actor_type,
        actor_id=str(journey.traveler_id),
        after={
            "event_type": event_type,
            "activity_id": event.activity_id,
            "occurred_at_actual": event.occurred_at_actual,
            "planned_start_at": event.planned_start_at,
            "planned_end_at": event.planned_end_at,
            "payload": payload or {},
        },
    )
    return event


def build_open_meteo_provider(client: httpx.AsyncClient) -> OpenMeteoContextProvider:
    """Build the server-owned provider used only by explicit weather refresh."""

    return OpenMeteoContextProvider(client=client, base_url=settings.open_meteo_url)


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    with SessionLocal() as session:
        session.execute(select(Traveler).limit(1))
    return {
        "status": "ok",
        "database": "ok",
        "ai": "enabled" if settings.ai_enabled and settings.ai_api_key else "disabled",
        # ORD-0359: additive, and the `ai` line above is untouched so a client that still reads
        # it keeps the same two words.  `ai` cannot tell three facts apart; `ai_state` can.
        "ai_state": ai_availability_state(),
        # Echoed for the authoritative E2E gate (frontend/playwright.config.ts).
        # Empty unless this process was started with CITYNEXUS_E2E_RUN_ID, which
        # only a suite that owns the server does.
        "e2e_run_id": settings.e2e_run_id or "",
        # Deliberately not the connection URI: today it is a local SQLite path,
        # but a /health that echoes whatever DATABASE_URL may one day contain
        # (host, user, password) turns a test-provenance field into a leak.  The
        # gate only needs to know *which* throwaway file this process opened.
        "e2e_database_id": (
            settings.database_url.rsplit("/", 1)[-1] if settings.e2e_run_id else ""
        ),
    }


@app.get("/api/v1/agent/tools")
def list_agent_tools() -> list[dict[str, object]]:
    return AgentToolGateway.catalog()


@app.get("/api/v1/agent/tools/v2")
def list_agent_tools_v2() -> list[dict[str, object]]:
    return AgentToolGateway.catalog_v2()


@app.get("/api/v1/agent/runtime-status")
def get_agent_runtime_status() -> dict[str, object]:
    return runtime_status()


@app.get("/api/v1/agent/hermes/capabilities")
def get_native_hermes_capabilities() -> dict[str, Any]:
    """Expose non-secret native Hermes capability facts to the CityNexus UI."""

    return native_hermes_status()


@app.get("/api/v1/agent/hermes/runs/{run_id}")
def get_native_hermes_run(run_id: str) -> dict[str, Any]:
    client = HermesNativeClient()
    try:
        return client.get_run(run_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "native Hermes run status is unavailable") from exc
    finally:
        client.close()


def _persist_native_event(event: dict[str, Any], *, journey_id: str | None = None) -> None:
    """Persist a compact native event so reconnects can recover run history."""
    run_id = str(event.get("run_id") or "")
    if not run_id:
        return
    with SessionLocal() as event_session:
        existing = event_session.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "agent.native_hermes.event",
                AuditLog.entity_id == run_id,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(50)
        ).all()
        signature = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        if any(
            json.dumps(json.loads(row.after_json or "{}"), ensure_ascii=False, sort_keys=True, default=str)
            == signature
            for row in existing
            if row.after_json
        ):
            return
        # ``after_json`` is intentionally the normalized contract, with no
        # raw Hermes arguments or credentials.
        record_audit(
            event_session,
            action="agent.native_hermes.event",
            entity_type="HermesRuntimeEvent",
            entity_id=run_id,
            journey_id=journey_id,
            actor_type="SYSTEM",
            actor_id="hermes-native",
            after=event,
        )
        event_session.commit()


def _native_citynexus_events(
    *,
    run_id: str,
    journey_id: str,
    mission: JourneyMission,
    interpretation: CopilotInterpretation | None = None,
) -> list[dict[str, Any]]:
    """Persist the real CityNexus stages that precede native Hermes.

    These are not simulated Hermes progress. They are the semantic hand-off
    and deterministic mission-plan construction performed by CityNexus before
    submitting a run; Hermes lifecycle and tool events still come from SSE.
    """

    now = datetime.now(timezone.utc).isoformat()
    semantic = normalize_hermes_event(
        {
            "event_type": "semantic.interpreted",
            "run_id": run_id,
            "timestamp": now,
            "source": "CITYNEXUS",
            "status": "completed",
            "result": {
                "compiler": mission.compiler_provider_key,
                "intent": interpretation.intent if interpretation is not None else "MISSION",
                "confidence": (
                    interpretation.confidence
                    if interpretation is not None
                    else mission.compiler_confidence
                ),
            },
        },
        run_id=run_id,
    )
    plan = mission_plan_for_model(mission)
    planned = normalize_hermes_event(
        {
            "event_type": "mission.planned",
            "run_id": run_id,
            "timestamp": now,
            "source": "CITYNEXUS",
            "status": "completed",
            "result": {
                "autonomy": plan["autonomy"],
                "step_count": len(plan["steps"]),
                "protected_activity_count": len(plan["protected_activity_ids"]),
                "mission_id": mission.id,
            },
        },
        run_id=run_id,
    )
    events = [semantic, planned]
    for item in events:
        _persist_native_event(item, journey_id=journey_id)
    return events


def _native_event_stream(run_id: str, journey_id: str | None = None):
    client = HermesNativeClient()
    try:
        try:
            for item in client.iter_events(run_id):
                normalized = normalize_hermes_event(item, run_id=run_id)
                if normalized is None:
                    continue
                _persist_native_event(normalized, journey_id=journey_id)
                yield (
                    f"event: {normalized['event_type']}\n"
                    f"data: {json.dumps(normalized, ensure_ascii=False, sort_keys=True)}\n\n"
                )
        except (httpx.HTTPError, ValueError):
            # Keep the client-facing contract intact when the upstream stream
            # disappears.  This is an honest runtime failure (not a synthetic
            # progress step) and contains only a stable error code.
            failed = normalize_hermes_event(
                {
                    "event_type": "run.failed",
                    "run_id": run_id,
                    "source": "HERMES_NATIVE",
                    "status": "failed",
                    "result": {"error": "runtime_event_stream_unavailable"},
                },
                run_id=run_id,
            )
            if failed is not None:
                _persist_native_event(failed, journey_id=journey_id)
                yield (
                    f"event: {failed['event_type']}\n"
                    f"data: {json.dumps(failed, ensure_ascii=False, sort_keys=True)}\n\n"
                )
    finally:
        client.close()


@app.get("/api/v1/agent/hermes/runs/{run_id}/events")
def stream_native_hermes_events(run_id: str) -> StreamingResponse:
    return StreamingResponse(
        _native_event_stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/journeys/{journey_id}/agent/hermes/runs/{run_id}")
def get_journey_native_hermes_run(
    journey_id: UUID, run_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    require(session, Journey, str(journey_id), "Journey")
    client = HermesNativeClient()
    try:
        payload = client.get_run(run_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "native Hermes run status is unavailable") from exc
    finally:
        client.close()
    payload["runtime_identity"] = "HERMES_NATIVE_MCP"
    payload["journey_id"] = str(journey_id)
    payload["timeline_url"] = f"/api/v1/journeys/{journey_id}/agent/hermes/runs/{run_id}/timeline"
    return payload


@app.get("/api/v1/journeys/{journey_id}/agent/hermes/runs/{run_id}/timeline")
def get_native_hermes_timeline(
    journey_id: UUID,
    run_id: str,
    limit: int = Query(default=100, ge=1, le=250),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == "agent.native_hermes.event",
            AuditLog.entity_id == run_id,
            AuditLog.journey_id == str(journey_id),
        )
        .order_by(AuditLog.created_at, AuditLog.id)
        .limit(limit)
    ).all()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row.after_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    tool_started = [item for item in events if item.get("event_type") == "tool.started"]
    gateway_checks = [item for item in events if item.get("event_type") == "gateway.checked"]
    guardian_checks = [item for item in events if item.get("event_type") == "guardian.checked"]
    terminal = next(
        (
            item
            for item in reversed(events)
            if item.get("event_type") in {"run.completed", "run.failed", "run.stopped"}
        ),
        None,
    )
    # A reconnect can arrive after Hermes has closed its replay window.  In
    # that case the upstream run status is still authoritative; reconcile the
    # missing terminal lifecycle event into the same safe contract instead of
    # leaving the UI permanently in ``running``.  This is status hydration,
    # not simulated progress, and it never copies Hermes output or secrets.
    if terminal is None:
        client = HermesNativeClient()
        try:
            upstream = client.get_run(run_id)
            upstream_status = str(upstream.get("status") or "").lower()
            if upstream_status in {"completed", "failed", "cancelled", "canceled", "stopped", "error"}:
                event_name = str(upstream.get("last_event") or "")
                if event_name not in {"run.completed", "run.failed", "run.stopped"}:
                    event_name = "run.stopped" if upstream_status in {"cancelled", "canceled", "stopped"} else (
                        "run.failed" if upstream_status in {"failed", "error"} else "run.completed"
                    )
                hydrated = normalize_hermes_event(
                    {
                        "event_type": event_name,
                        "run_id": run_id,
                        "status": "stopped" if event_name == "run.stopped" else (
                            "failed" if event_name == "run.failed" else "completed"
                        ),
                        "timestamp": upstream.get("updated_at"),
                        "source": "HERMES_NATIVE",
                        "result": {"upstream_status": upstream_status},
                    },
                    run_id=run_id,
                )
                if hydrated is not None:
                    _persist_native_event(hydrated, journey_id=str(journey_id))
                    events.append(hydrated)
                    terminal = hydrated
        except (httpx.HTTPError, ValueError):
            pass
        finally:
            client.close()
    return {
        "run_id": run_id,
        "journey_id": str(journey_id),
        "runtime_identity": "HERMES_NATIVE_MCP",
        "events": events,
        "observability": {
            "run_id": run_id,
            "runtime_identity": "HERMES_NATIVE_MCP",
            "current_mission": next(
                (
                    item.get("result_summary", {}).get("mission_id")
                    for item in events
                    if isinstance(item.get("result_summary"), dict)
                    and item.get("result_summary", {}).get("mission_id")
                ),
                None,
            ),
            "tool_count": len(tool_started),
            "current_tool": tool_started[-1].get("tool_name") if tool_started else None,
            "completion_status": terminal.get("status") if terminal else "running",
            "gateway_decision": gateway_checks[-1].get("policy_decision") if gateway_checks else None,
            "guardian_decision": guardian_checks[-1].get("policy_decision") if guardian_checks else None,
            "total_duration_ms": sum(
                float(item.get("duration_ms") or 0)
                for item in events
                if isinstance(item.get("duration_ms"), (int, float))
            ),
        },
    }


@app.get("/api/v1/journeys/{journey_id}/agent/hermes/runs/{run_id}/events")
def stream_journey_native_hermes_events(
    journey_id: UUID, run_id: str, session: Session = Depends(get_session)
) -> StreamingResponse:
    require(session, Journey, str(journey_id), "Journey")
    return StreamingResponse(
        _native_event_stream(run_id, str(journey_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v1/journeys/{journey_id}/agent/hermes/runs/{run_id}/stop")
def stop_native_hermes_run(journey_id: UUID, run_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Stop a native run through CityNexus; Hermes is never called directly by the UI."""
    require(session, Journey, str(journey_id), "Journey")
    client = HermesNativeClient()
    try:
        return client.stop(run_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "native Hermes run stop is unavailable") from exc
    finally:
        client.close()


@app.post("/api/v1/agent/tools/v2/{tool_name}")
def call_agent_tool_v2(
    tool_name: str,
    request: Request,
    payload: AgentToolCall,
    session: Session = Depends(get_session),
):
    scope_journey_id = payload.journey_id
    if scope_journey_id is None and payload.proposal_id is not None:
        proposal = session.get(ReplanProposalModel, str(payload.proposal_id))
        if proposal is not None:
            scope_journey_id = UUID(proposal.journey_id)
    principal = resolve_agent_principal(request, session, journey_id=scope_journey_id)
    resolved_payload = payload.model_copy(update={"agent_id": principal.actor_id})
    return AgentToolGateway(session).dispatch(tool_name, resolved_payload)


@app.post("/api/v1/agent/tools/{tool_name}")
def call_agent_tool(
    tool_name: str,
    request: Request,
    payload: AgentToolCall,
    session: Session = Depends(get_session),
):
    scope_journey_id = payload.journey_id
    if scope_journey_id is None and payload.proposal_id is not None:
        proposal = session.get(ReplanProposalModel, str(payload.proposal_id))
        if proposal is not None:
            scope_journey_id = UUID(proposal.journey_id)
    principal = resolve_agent_principal(request, session, journey_id=scope_journey_id)
    # The request's agent_id is only an untrusted hint for compatibility with
    # existing clients. All gateway keys and audit records use this server-
    # resolved principal instead.
    resolved_payload = payload.model_copy(update={"agent_id": principal.actor_id})
    return AgentToolGateway(session).dispatch(tool_name, resolved_payload)


# The one statement of "a reported disruption becomes a domain Event." Two entry points used to each
# build this row themselves -- the natural-language /agent/commands route and the /copilot/messages
# route -- with the same fields but different payload shapes, different provider keys and a different
# audit footprint, so a change to one was invisible to the other. Both now route through this function;
# it reproduces each caller's exact row and audit from the arguments, so the bytes and the side effects
# are unchanged while the logic is one thing to read and one thing to change.
def _capture_reported_disruption(
    session: Session,
    journey: Journey,
    *,
    event_type: str,
    description: str,
    provider_key: str,
    source: str,
    semantic_payload: dict[str, Any],
    semantic_envelope: dict[str, Any],
    occurred_at_fallback: str | None,
    audit_action: str | None,
    commit: bool,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the disruption's target, persist the Event, optionally audit/commit, and return the receipt.

    Returns a REVIEW_REQUIRED receipt (no Event written) when the server cannot anchor the target, which
    is the same early-return both entry points performed.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    target = resolve_event_target(
        session,
        journey,
        event_type=event_type,
        semantic=semantic_envelope,
        now=now_dt,
    )
    resolved = target.resolution != "UNRESOLVED_REVIEW_REQUIRED"
    if not resolved:
        return {
            "id": None,
            "event_type": event_type,
            "status": "REVIEW_REQUIRED",
            "affected_activity_id": None,
            "occurred_at": None,
            "target_resolution": target.resolution,
            "requires_review": True,
            "effective_time": str(semantic_envelope.get("effective_time") or "FUTURE").upper(),
        }
    event = EventModel(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=provider_key,
        provenance="USER_REPORTED",
        event_type=event_type,
        severity=disruption_severity(description, event_type),
        status="DETECTED",
        affected_activity_id=target.activity_id,
        occurred_at=target.occurred_at or occurred_at_fallback,
        detected_at=now,
        payload_json=json.dumps(
            {
                "description": description,
                "source": source,
                "extra_minutes": 15 if event_type == "TRAFFIC_DELAY" else None,
                "semantic": {**semantic_payload, "target_resolution": target.resolution},
                **(payload_extra or {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        created_at=now,
    )
    session.add(event)
    session.flush()
    if audit_action:
        record_audit(
            session,
            action=audit_action,
            entity_type="Event",
            entity_id=event.id,
            journey_id=journey.id,
            actor_type="USER",
            actor_id=journey.traveler_id,
            after={"event_type": event_type, "source": source},
        )
    if commit:
        session.commit()
    return {
        "id": event.id,
        "event_type": event.event_type,
        "status": event.status,
        "affected_activity_id": event.affected_activity_id,
        "occurred_at": event.occurred_at,
        "target_resolution": target.resolution,
        "requires_review": not resolved,
    }


@app.post("/api/v1/journeys/{journey_id}/agent/commands")
def run_agent_command(
    journey_id: UUID,
    payload: AgentCommandRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    start_request_budget(SCENARIO_AGENT_COMMANDS)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    interpretation = _copilot_fallback_or_ai(payload.command, _copilot_context(session, journey))
    event_type = (
        str(interpretation.entities.get("event_type"))
        if interpretation.intent == "DISRUPTION_REPORT"
        and interpretation.entities.get("event_type")
        else None
    )
    event = None
    with write_attempt(SCENARIO_AGENT_COMMANDS):
        if event_type is not None:
            semantic = interpretation.semantic_envelope or {
                "assertion_status": interpretation.entities.get("assertion_status", "ASSERTED"),
                "effective_time": interpretation.entities.get("effective_time", "NOW"),
                "entities": dict(interpretation.entities),
            }
            captured = _capture_reported_disruption(
                session,
                journey,
                event_type=event_type,
                description=payload.command,
                provider_key="user-command",
                source="agent_command",
                semantic_payload=semantic,
                semantic_envelope=semantic,
                occurred_at_fallback=now,
                audit_action=None,
                commit=False,
            )
            if captured.get("requires_review"):
                return {
                    "mission": None,
                    "event_id": None,
                    "review_required": True,
                    "event": {
                        "id": None,
                        "event_type": event_type,
                        "status": "REVIEW_REQUIRED",
                        "affected_activity_id": None,
                        "occurred_at": None,
                        "target_resolution": captured.get("target_resolution"),
                        "effective_time": str(semantic.get("effective_time") or "FUTURE").upper(),
                    },
                }
            event = session.get(EventModel, captured["id"])
            session.flush()
        mission = create_mission(
            session,
            journey,
            command=payload.command,
            requested_autonomy=payload.requested_autonomy,
            trigger_event_id=event.id if event else None,
        )
        record_audit(
            session,
            action="agent.mission.created",
            entity_type="JourneyMission",
            entity_id=mission.id,
            journey_id=journey.id,
            actor_type="USER",
            actor_id=journey.traveler_id,
            after={
                "mission_id": mission.id,
                "event_id": event.id if event else None,
                "requested_autonomy": mission.requested_autonomy,
            },
        )
        commit_within_budget(session, SCENARIO_AGENT_COMMANDS)
    native_status = native_hermes_status()
    if native_status.get("ready"):
        native = HermesNativeClient()
        try:
            native_plan = mission_plan_for_model(mission)
            native_tool_sequence = [
                str(step["tool_name"])
                for step in native_plan["steps"]
                if step.get("tool_name")
            ]
            native_run = native.create_run(
                mission_id=mission.id,
                journey_id=journey.id,
                input_text=payload.command,
                instructions=(
                    "Follow the supplied CityNexus mission plan in order. You MUST call each listed MCP tool "
                    f"in this sequence: {', '.join(native_tool_sequence)}. "
                    f"For every CityNexus MCP call, set journey_id exactly to {journey.id}. "
                    f"The CityNexus mission_id is {mission.id}; do not invent or omit either identifier. "
                    + (
                        f"For request_replan, set trigger_event_id exactly to {mission.trigger_event_id}, "
                        "candidate_count to 3, and solver_mode to deterministic. "
                        if mission.trigger_event_id
                        else "There is no trigger event; do not call request_replan until CityNexus provides one. "
                    )
                    + "Call one MCP tool at a time (never batch or use tool_call metadata), do not repeat a completed step. "
                    "Use only the configured CityNexus MCP tools and the allowed_tools list. "
                    "Respect the requested autonomy boundary; proposal-only work must not mutate. "
                    "CityNexus Gateway and deterministic feasibility are authoritative."
                ),
                autonomy=mission.requested_autonomy,
                mission_plan=native_plan,
                session_id=f"citynexus-{journey.id}-mission-{mission.id}",
            )
            mission.status = "RUNNING"
            mission.started_at = datetime.now(timezone.utc).isoformat()
            commit_within_budget(session, SCENARIO_AGENT_COMMANDS)
            initial_events = _native_citynexus_events(
                run_id=native_run.run_id,
                journey_id=journey.id,
                mission=mission,
            )
            return {
                "mission": mission_payload(mission),
                "event_id": event.id if event else None,
                "run": {
                    "run_id": native_run.run_id,
                    "mission_id": mission.id,
                    "status": native_run.status.upper(),
                    "provider_key": "hermes-native",
                    "model_key": settings.hermes_native_model or f"profile:{settings.hermes_native_profile}",
                    "runtime_identity": "HERMES_NATIVE_MCP",
                    "outcome": None,
                    "failure_reason": None,
                    "tool_steps": [],
                    "native_events_url": f"/api/v1/agent/hermes/runs/{native_run.run_id}/events",
                    "events_url": f"/api/v1/journeys/{journey.id}/agent/hermes/runs/{native_run.run_id}/events",
                    "timeline_url": f"/api/v1/journeys/{journey.id}/agent/hermes/runs/{native_run.run_id}/timeline",
                    "mission_plan": native_plan,
                    "initial_events": initial_events,
                },
            }
        except (httpx.HTTPError, ValueError) as exc:
            record_audit(
                session,
                action="agent.native_hermes.fallback",
                entity_type="JourneyMission",
                entity_id=mission.id,
                journey_id=journey.id,
                actor_type="SYSTEM",
                after={"reason": str(exc)},
            )
            commit_within_budget(session, SCENARIO_AGENT_COMMANDS)
        finally:
            native.close()
    if not settings.ai_api_key and not (settings.hermes_api_url and settings.hermes_api_key):
        run = unavailable_run_payload(session, journey=journey, mission=mission)
        return {
            "mission": mission_payload(mission),
            "event_id": event.id if event else None,
            "run": run,
        }
    runtime = configured_journey_agent(native_factory=DeepSeekJourneyAgent)
    try:
        run = runtime.run(session, journey=journey, mission=mission)
    except AgentRuntimeUnavailable as exc:
        failed_run = session.scalar(
            select(AgentRun)
            .where(AgentRun.mission_id == mission.id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
        run = (
            persisted_agent_run_payload(session, failed_run)
            if failed_run is not None
            else {
                "run_id": None,
                "mission_id": mission.id,
                "status": "FAILED_EXTERNAL",
                "provider_key": "deepseek",
                "model_key": settings.ai_model.removeprefix("deepseek/"),
                "runtime_identity": runtime_status()["runtime_identity"],
                "outcome": None,
                "failure_reason": str(exc),
                "tool_steps": [],
            }
        )
    finally:
        runtime.close()
    return {
        "mission": mission_payload(mission),
        "event_id": event.id if event else None,
        "run": run,
    }


@app.put("/api/v1/journeys/{journey_id}/guardian/mission")
def put_guardian_mission(
    journey_id: UUID,
    payload: AgentCommandRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    current = session.scalars(
        select(JourneyMission).where(
            JourneyMission.journey_id == journey.id,
            JourneyMission.is_standing.is_(True),
            JourneyMission.status == "ACTIVE",
        )
    ).all()
    for mission in current:
        mission.status = "SUPERSEDED"
        mission.completed_at = datetime.now(timezone.utc).isoformat()
    mission = create_mission(
        session,
        journey,
        command=payload.command,
        requested_autonomy=payload.requested_autonomy,
        trigger_event_id=None,
        is_standing=True,
    )
    mission.status = "ACTIVE"
    record_audit(
        session,
        action="guardian.mission.activated",
        entity_type="JourneyMission",
        entity_id=mission.id,
        journey_id=journey.id,
        actor_type="USER",
        actor_id=journey.traveler_id,
        after=mission_payload(mission),
    )
    session.commit()
    return mission_payload(mission)


@app.post("/api/v1/journeys/{journey_id}/guardian/scan")
def run_guardian_scan(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    return scan_guardian(session, journey)


@app.post("/api/v1/journeys/{journey_id}/guardian/autopilot/start")
def start_guardian_autopilot(
    journey_id: UUID,
    payload: GuardianAutopilotStart | None = None,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE":
        raise HTTPException(409, "Guardian Autopilot requires an active Journey")
    mission = session.scalar(
        select(JourneyMission).where(
            JourneyMission.journey_id == journey.id,
            JourneyMission.is_standing.is_(True),
            JourneyMission.status == "ACTIVE",
        )
    )
    if mission is None:
        raise HTTPException(409, "activate a standing Guardian mission before starting Autopilot")
    enrollment = ensure_enrollment(
        session,
        journey,
        enabled=True,
        interval_seconds=payload.interval_seconds if payload else None,
    )
    # Make the subscription visible before the process-global worker can wake.
    session.commit()
    result = start_autorun(interval_seconds=payload.interval_seconds if payload else None)
    record_audit(
        session,
        action="guardian.autopilot.started",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        actor_type="USER",
        actor_id=journey.traveler_id,
        after={
            "enrollment": {"journey_id": enrollment.journey_id, "enabled": enrollment.enabled},
            **result,
        },
    )
    session.commit()
    return {
        "journey_id": journey.id,
        "mission_id": mission.id,
        "enrollment": {
            "journey_id": enrollment.journey_id,
            "enabled": bool(enrollment.enabled),
            "interval_seconds": enrollment.interval_seconds,
            "provider_mesh": json.loads(enrollment.provider_mesh_json or "{}"),
        },
        **result,
    }


@app.post("/api/v1/journeys/{journey_id}/guardian/autopilot/stop")
def stop_guardian_autopilot(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    enrollment = session.get(AutopilotEnrollment, journey.id)
    if enrollment is not None:
        enrollment.enabled = False
        enrollment.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    remaining = enrolled_journey_ids(session)
    result = stop_autorun() if not remaining else autorun_status()
    record_audit(
        session,
        action="guardian.autopilot.stopped",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        actor_type="USER",
        actor_id=journey.traveler_id,
        after={"remaining_enrolled_journeys": remaining, **result},
    )
    session.commit()
    return {
        "journey_id": journey.id,
        "enrollment": {
            "journey_id": journey.id,
            "enabled": False,
            "remaining_enrolled_journeys": remaining,
        },
        **result,
    }


@app.get("/api/v1/journeys/{journey_id}/guardian/autopilot/status")
def get_guardian_autopilot_status(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    enrollment = session.get(AutopilotEnrollment, journey.id)
    return {
        "journey_id": journey.id,
        "enrollment": (
            {
                "journey_id": enrollment.journey_id,
                "enabled": bool(enrollment.enabled),
                "interval_seconds": enrollment.interval_seconds,
                "provider_mesh": json.loads(enrollment.provider_mesh_json or "{}"),
                "last_pulse_at": enrollment.last_pulse_at,
                "last_error": enrollment.last_error,
            }
            if enrollment is not None
            else None
        ),
        **autorun_status(),
    }


@app.post(
    "/api/v1/journeys/{journey_id}/guardian/provider-fixture",
    response_model=dict[str, Any],
)
@app.post(
    "/api/v1/journeys/{journey_id}/guardian/signal-fixture",
    response_model=dict[str, Any],
)
def update_guardian_provider_fixture(
    journey_id: UUID,
    payload: CitySignalFixtureRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    fixture = set_fixture_state(
        session,
        journey,
        state=payload.state,
        payload=payload.payload,
    )
    session.commit()
    return fixture_payload(fixture)


@app.get("/api/v1/journeys/{journey_id}/guardian/status")
def get_guardian_status(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    result = guardian_status(session, journey)
    result["autopilot"] = autorun_status()
    enrollment = session.get(AutopilotEnrollment, journey.id)
    result["autopilot"]["enrollment"] = (
        {
            "journey_id": enrollment.journey_id,
            "enabled": bool(enrollment.enabled),
            "interval_seconds": enrollment.interval_seconds,
            "provider_mesh": json.loads(enrollment.provider_mesh_json or "{}"),
            "last_pulse_at": enrollment.last_pulse_at,
            "last_error": enrollment.last_error,
        }
        if enrollment is not None
        else None
    )
    fixture = session.get(CitySignalFixture, journey.id)
    result["signal_fixture"] = fixture_payload(fixture) if fixture is not None else None
    return result


@app.get("/api/v1/journeys/{journey_id}/resilience")
@app.get("/api/v1/journeys/{journey_id}/guardian/resilience")
def get_journey_resilience(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    snapshot = latest_snapshot(session, journey.id)
    if snapshot is None:
        snapshot = build_resilience_snapshot(session, journey)
        session.commit()
    return snapshot_payload(snapshot)


@app.post("/api/v1/journeys/{journey_id}/resilience/refresh")
@app.post("/api/v1/journeys/{journey_id}/guardian/resilience/refresh")
def refresh_journey_resilience(
    journey_id: UUID,
    payload: ResilienceRefreshRequest | None = None,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    mode = payload.mode if payload else "replay"
    discovery = None
    if mode in {"live", "captured_replay"}:
        try:
            discovery = capture_real_city_candidates(
                session,
                journey,
                query="indoor city service",
                mode=mode,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    snapshot = build_resilience_snapshot(session, journey)
    simulations = simulate_counterfactuals(session, journey, snapshot)
    session.commit()
    return {
        "snapshot": snapshot_payload(snapshot),
        "counterfactuals": [counterfactual_payload(item) for item in simulations],
        "discovery": discovery,
    }


@app.get("/api/v1/journeys/{journey_id}/resilience/counterfactuals")
def list_journey_counterfactuals(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(CounterfactualSimulation)
        .where(CounterfactualSimulation.journey_id == journey.id)
        .order_by(CounterfactualSimulation.created_at, CounterfactualSimulation.scenario_key)
    ).all()
    return {
        "journey_id": journey.id,
        "counterfactuals": [counterfactual_payload(item) for item in rows],
    }


@app.get("/api/v1/journeys/{journey_id}/provider-mesh")
def get_journey_provider_mesh(
    journey_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(ProviderObservation)
        .where(ProviderObservation.journey_id == journey.id)
        .order_by(ProviderObservation.captured_at.desc())
        .limit(30)
    ).all()
    candidates = session.scalars(
        select(RealPlaceCandidate)
        .where(RealPlaceCandidate.journey_id == journey.id)
        .order_by(RealPlaceCandidate.captured_at.desc(), RealPlaceCandidate.name)
        .limit(20)
    ).all()
    return {
        "journey_id": journey.id,
        "providers": [
            {
                "provider_key": row.provider_key,
                "observation_type": row.observation_type,
                "state": row.state,
                "provenance": row.provenance,
                "captured_at": row.captured_at,
                "expires_at": row.expires_at,
            }
            for row in rows
        ],
        "real_place_candidates": [
            {
                "id": row.id,
                "provider_key": row.provider_key,
                "external_id": row.external_id,
                "name": row.name,
                "category": row.category,
                "location": {"latitude": row.latitude, "longitude": row.longitude},
                "tags": json.loads(row.tags_json or "[]"),
                "opening_hours": json.loads(row.opening_hours_json or "{}"),
                "cost_evidence": json.loads(row.metadata_json or "{}").get(
                    "cost_evidence", {"status": "UNKNOWN"}
                ),
                "captured_at": row.captured_at,
                "expires_at": row.expires_at,
                "materialized_place_id": row.materialized_place_id,
                "status": row.status,
            }
            for row in candidates
        ],
    }


@app.post("/api/v1/journeys/{journey_id}/real-city/discover", response_model=RealCityDiscoveryResponse)
@app.post("/api/v1/journeys/{journey_id}/provider-mesh/discover", response_model=RealCityDiscoveryResponse)
def discover_real_city_services(
    journey_id: UUID,
    payload: RealCityDiscoveryRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        result = capture_real_city_candidates(
            session,
            journey,
            query=payload.query,
            radius_m=payload.radius_m,
            limit=payload.limit,
            mode=payload.mode,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    session.commit()
    return result


@app.post("/api/v1/journeys/{journey_id}/real-city/intelligence", status_code=201)
def run_real_city_intelligence(
    journey_id: UUID,
    payload: RealCityIntelligenceRequest,
    session: Session = Depends(get_session),
):
    """Run the V13.4 provider-truth discovery path through authoritative Compose."""

    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        bundle = orchestrate_discovery(
            session,
            journey,
            goal=payload.goal,
            mode=payload.mode,
            radius_m=payload.radius_m,
            limit=payload.limit,
            client=_deepseek_client(),
            require_fully_grounded=payload.require_fully_grounded,
        )
        selected_place_ids = list(bundle["selected_place_ids"])
        if len(selected_place_ids) < 2:
            raise ValueError("V13_DISCOVERY_NO_FEASIBLE_PLACE_SET")
        result = compose_authoritative_multiday(
            session,
            journey,
            place_ids=selected_place_ids,
            candidate_count=payload.candidate_count,
            discovery_evidence=bundle["evidence"],
        )
        if not result.plans:
            raise ValueError("V13_DISCOVERY_COMPOSE_EMPTY")
        chosen = None
        selection_failures: list[str] = []
        for candidate_plan in result.plans:
            candidate_id = str(candidate_plan.id)
            try:
                select_plan(UUID(str(journey.id)), UUID(candidate_id), session)
            except HTTPException as exc:
                session.rollback()
                if exc.status_code not in {409, 422}:
                    raise
                selection_failures.append(str(exc.detail))
                continue
            chosen = session.get(PlanVersion, candidate_id)
            break
        if chosen is None:
            detail = "; ".join(selection_failures[:3]) or "no composed candidate passed the deterministic gate"
            raise HTTPException(422, f"V13_DISCOVERY_NO_FEASIBLE_PLAN: {detail}")
        session.expire_all()
        selected_journey = require(session, Journey, str(journey.id), "Journey")
        twin = _twin_snapshot(session, selected_journey)
        session.commit()
        return {
            **bundle,
            "compose": {
                "optimizer": result.optimizer_kind,
                "frozen_evidence_ids": list(result.evidence_ids),
                "plan_ids": [item.id for item in result.plans],
            },
            "selected_plan_id": chosen.id,
            "selected_plan": plan_schema(session, chosen),
            "journey_twin": twin,
        }
    except DeepSeekResponsesError as exc:
        session.rollback()
        raise HTTPException(502, f"DeepSeek discovery failed: {exc}") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/journeys/{journey_id}/operations/stream")
def journey_operations_stream(
    journey_id: UUID,
    request: Request,
    after: str | None = None,
    timeout_seconds: float = Query(default=30.0, gt=0, le=120),
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    journey_key = journey.id

    async def stream():
        # REST hydration already renders historical truth.  A reconnect can
        # pass its last timestamp; a fresh stream starts at the connection
        # boundary so it does not replay the whole audit log as fake activity.
        cursor = after or datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            with SessionLocal() as stream_session:
                statement = (
                    select(AuditLog)
                    .where(
                        AuditLog.journey_id == journey_key,
                        AuditLog.created_at > cursor,
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
                rows = stream_session.scalars(statement).all()
                if rows:
                    for row in rows:
                        projection = _audit_projection(row)
                        cursor = row.created_at
                        yield (
                            f"id: {row.id}\n"
                            f"event: {projection['event_type']}\n"
                            f"data: {json.dumps(projection, ensure_ascii=False)}\n\n"
                        )
                    continue
            yield ": citynexus-heartbeat\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/journeys/{journey_id}/agent/runs/{run_id}")
def get_agent_run(
    journey_id: UUID,
    run_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    run = session.get(AgentRun, str(run_id))
    if run is None or run.journey_id != journey.id:
        raise HTTPException(404, "agent run was not found")
    mission = session.get(JourneyMission, run.mission_id)
    steps = session.scalars(
        select(AgentRunStep).where(AgentRunStep.run_id == run.id).order_by(AgentRunStep.sequence_no)
    ).all()

    def decode(value: str) -> Any:
        return json.loads(value)

    return {
        "run_id": run.id,
        "mission_id": run.mission_id,
        "journey_id": run.journey_id,
        "provider_key": run.provider_key,
        "model_key": run.model_key,
        "status": run.status,
        "outcome": run.outcome,
        "failure_reason": run.failure_reason,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "mission_status": mission.status if mission else None,
        "mission": mission_payload(mission) if mission else None,
        "tool_steps": [
            {
                "id": step.id,
                "sequence_no": step.sequence_no,
                "tool_name": step.tool_name,
                "arguments": decode(step.arguments_json),
                "result": decode(step.result_json),
                "decision": step.decision,
                "status": step.status,
                "created_at": step.created_at,
                "completed_at": step.completed_at,
            }
            for step in steps
        ],
    }


@app.post("/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve")
def approve_agent_action(
    journey_id: UUID,
    approval_id: UUID,
    payload: AgentApprovalRequest | None = None,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    approval = session.get(ActionApproval, str(approval_id))
    if (
        approval is None
        or approval.journey_id != journey.id
        or approval.traveler_id != journey.traveler_id
    ):
        raise HTTPException(404, "agent approval was not found")
    from app.agent.approval import approve_action_approval

    return approve_action_approval(
        session,
        approval,
        approval_method=payload.approval_method if payload else "USER_CONFIRMATION",
        step_up_code=payload.step_up_code if payload else None,
        actor_id=journey.traveler_id,
    )


def advance_seed_to_executable(session: Session, journey_id: str) -> dict:
    """Push the freshly seeded journey to ACTIVE through the routes a real client would call.

    Deliberately calls the handlers rather than the service layer: `select` and `/start` are where
    the frozen-evidence, feasibility, single-ACTIVE-plan and C4 rules live, and a demo that skipped
    them would be demonstrating a state the product cannot actually reach.
    """

    steps: list[dict[str, object]] = []

    def timed(name: str, call):
        began = time.perf_counter()
        try:
            outcome = call()
        except Exception as exc:  # noqa: BLE001 - a refused or broken step must not 500 reset
            # reset is the fixture door for ~30 test modules and for the panel; if the advance
            # cannot reach ACTIVE, the honest answer is a recorded refusal in the response, not
            # a 500 that takes the whole seeded library down with it.
            if isinstance(exc, HTTPException) and exc.status_code in {404, 409, 422}:
                pass  # a real policy refusal: reported below with its detail
            steps.append(
                {
                    "step": name,
                    "ms": round((time.perf_counter() - began) * 1000.0, 1),
                    "refused": getattr(exc, "status_code", type(exc).__name__),
                    "detail": str(getattr(exc, "detail", None) or exc)[:300],
                }
            )
            try:
                session.rollback()
            except Exception:  # pragma: no cover
                pass
            return None
        steps.append(
            {"step": name, "ms": round((time.perf_counter() - began) * 1000.0, 1), "ok": True}
        )
        return outcome

    try:
        _advance_steps(session, journey_id, timed)
    except Exception as exc:  # noqa: BLE001 - an ORM flush error must not outlive the reset
        # An ORM failure here surfaces at commit time, i.e. outside any single step, so the
        # whole advance needs the same non-fatal treatment: record what happened, roll back to
        # the seeded state, and let reset still answer 200 with an honest refusal attached.
        steps.append(
            {
                "step": "advance",
                "ms": 0.0,
                "refused": type(exc).__name__,
                "detail": str(exc)[:300],
            }
        )
        try:
            session.rollback()
        except Exception:  # pragma: no cover
            pass
    return _advance_report(session, journey_id, steps)


def _advance_steps(session: Session, journey_id: str, timed) -> None:
    # The stage wants ORM rows and it already owns a session, so it calls the generator - never the
    # HTTP route.  A route function called in-process keeps its `Header(default=None)` as a FastAPI
    # sentinel rather than None, which is why reaching through it made this step advance with no
    # plans at all (measured on 2026-09-24: six resident cells red, invisible to the route's own
    # gate).  `timed()` still turns a refusal into a recorded step instead of a failed reset.
    journey = require(session, Journey, str(journey_id), "Journey")

    def _generate() -> list[PlanVersion]:
        try:
            return _generate_plans_answer(session, journey, GeneratePlansRequest())
        except ValueError as exc:
            _plan_generation_refusal(session, journey, exc)   # 与 HTTP 路径同一份耐久事实
            raise

    plans = timed("plans/generate", _generate)
    # In-process, this is the ORM rows rather than the JSON a client would see - the id is the
    # same either way, and guessing the shape is how this line died once.
    plan_id = None
    if plans:
        first = plans[0]
        plan_id = getattr(first, "id", None) or (first.get("id") if isinstance(first, dict) else None)
    # Deliberately stops at SELECTED: `start_journey` flips the journey to ACTIVE, and
    # `commerce/validators.py` refuses a booking unless the journey is PLANNED with a
    # SELECTED plan.  Auto-starting therefore used to make M4's first half impossible to
    # demonstrate; starting is now an explicit action on the stage.
    if plan_id is not None:
        timed("select", lambda: select_plan(UUID(journey_id), UUID(str(plan_id)), session))
    session.commit()


def _advance_report(session: Session, journey_id: str, steps: list[dict]) -> dict:
    row = session.get(Journey, journey_id)
    return {
        "steps": [{key: str(value) if isinstance(value, UUID) else value for key, value in item.items()} for item in steps],
        "total_ms": round(sum(float(item["ms"]) for item in steps), 1),
        # Read back from the row rather than from a local: after a refusal the local can say "I picked X" while the journey still points nowhere.
        "selected_plan_version_id": (
            str(row.active_plan_version_id)
            if row is not None and row.active_plan_version_id
            else None
        ),
        "journey_status": row.status if row is not None else None,
    }


def demo_mode_enabled() -> bool:
    """The one seam that says whether this process is allowed to be a stage.

    A function rather than `settings.demo_mode` at each call site because the guard has to be
    testable in both states inside one process, and `Settings` is frozen.
    """

    return bool(settings.demo_mode)


# ORD-0389: the second half of the door.  `demo_mode` alone meant any caller who reached a
# deployment running with CITYNEXUS_DEMO_MODE=true -- which is exactly how the shared preview is
# started (D:/moodle/_qoder_preview_20260920/run/cmdr_hop1_backend.bat) -- so the switch that
# *enables* the destruction route also *was* the authorization for it.  Measured in the preview
# log: two `POST /api/v1/demo/reset HTTP/1.1 200 OK` at 03:55, no caller identity anywhere.
#
# The token is read from the environment at call time, not from the frozen `Settings`, for the
# same reason `agent.approval` reads CITYNEXUS_DEMO_STEP_UP_CODE at call time: both states have
# to be exercisable inside one process.
DEMO_RESET_ENV = "CITYNEXUS_DEMO_RESET_TOKEN"
DEMO_RESET_HEADER = "X-Demo-Reset-Token"


def demo_reset_token_denied(presented: str | None) -> tuple[str, str] | None:
    """None means allowed.  Otherwise the (code, message) that refuses this request.

    Two named codes, and deliberately nothing that tells the caller whether this deployment
    ever configured a token: an unconfigured server compares against "" and therefore refuses
    every presented token with RESET_TOKEN_INVALID, which is the same body a real mismatch
    produces.  Otherwise the route would double as a probe for "is this box resettable", which
    is the one fact an attacker of a data-destruction endpoint most wants.
    """
    if not presented:
        return (
            "RESET_TOKEN_REQUIRED",
            "No demo reset token was presented; nothing was deleted.",
        )
    expected = os.getenv(DEMO_RESET_ENV) or ""
    if not hmac.compare_digest(presented, expected):
        return (
            "RESET_TOKEN_INVALID",
            "The demo reset token presented does not match; nothing was deleted.",
        )
    return None


@app.post(
    "/api/v1/demo/scene/ensure",
    # ORD-0521: the refusal bodies are part of what this route exists to say, so the application's
    # own spec carries them the same way ORD-0395 put demo/reset's 403 into it.
    responses={
        403: {
            "description": (
                "Scene ensure refused, with a named code in the body: SCENE_TOKEN_REQUIRED when no"
                " token was presented, SCENE_TOKEN_INVALID when the presented token is not the"
                " operator's. Nothing was written in either case."
            )
        },
        422: {
            "description": (
                "Idempotency-Key is required, and a key replayed against a different body answers"
                " IDEMPOTENCY_KEY_REUSE_CONFLICT rather than performing a second board."
            )
        },
        409: {
            "description": (
                "DEMO_STATE_SUPPLY_UNAVAILABLE: this server holds no place row for the requested"
                " destination. The body names the failed hop, how many places it holds there and"
                " the cities its supply does cover, instead of inventing a place. Nothing was"
                " written - the install that had been staged is rolled back."
            )
        },
    },
)
def demo_scene_ensure(
    response: Response,
    x_demo_scene_token: str | None = Header(default=None, alias=DEMO_SCENE_HEADER),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    body: dict | None = None,
    session: Session = Depends(get_session),
):
    """ORD-0521: from an empty store to one trip a phone can actually walk, or a named refusal.

    Sits beside `demo/reset` because it is the same class of door -- guarded, token-gated, demo-
    mode-gated -- and the opposite one in what it touches: `demo/reset` deletes the board before it
    seeds, this door may only add, and only rows this server already holds offline.  The whole
    decision lives in `app/demo_scene.py`, whose docstring records why inventing a place name is
    the failure this order exists to close.
    """

    if not demo_mode_enabled():
        refused = HTTPException(
            403,
            "Ensuring the demo scene needs CITYNEXUS_DEMO_MODE=true; nothing was written.",
        )
        refused.reason_code = "DEMO_MODE_REQUIRED"
        refused.error_details = {"flag": "CITYNEXUS_DEMO_MODE", "rows_protected": True}
        raise refused
    denied = demo_scene_token_denied(x_demo_scene_token)
    if denied is not None:
        code, message = denied
        refused = HTTPException(403, message)
        refused.reason_code = code
        refused.error_details = {
            "header": DEMO_SCENE_HEADER,
            "idempotency_key_present": idempotency_key is not None,
            "rows_protected": True,
        }
        raise refused
    if idempotency_key is None:
        raise HTTPException(422, "Idempotency-Key header is required")
    try:
        decision = ensure_demo_scene(
            session,
            idempotency_key=idempotency_key,
            body=body or {},
            # The same in-process advance `demo/prepare-scenario` uses: plans are built by the
            # generator over ORM rows, never by calling the HTTP handler from inside the app.
            advance=lambda journey_id: advance_seed_to_executable(session, journey_id),
        )
    except SceneIdempotencyConflict as exc:
        refused = HTTPException(422, str(exc))
        refused.reason_code = "IDEMPOTENCY_KEY_REUSE_CONFLICT"
        raise refused
    except CommerceValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if decision.get("state") == DEMO_STATE_SUPPLY_UNAVAILABLE:
        refused = HTTPException(409, decision.get("message", "no real place row to build from"))
        refused.reason_code = DEMO_STATE_SUPPLY_UNAVAILABLE
        refused.error_details = {
            "failed_hop": decision.get("failed_hop"),
            "reason": decision.get("reason"),
            "places_held": decision.get("places_held"),
            "next_hop": decision.get("next_hop"),
        }
        raise refused
    response.status_code = 200
    return decision


@app.post(
    "/api/v1/demo/reset",
    # ORD-0395: the refusal is the behaviour this route exists to have, so the application's own
    # spec has to say it. The hand contract declared the 403 and the generated one did not, which
    # left every consumer of `/openapi.json` unable to see that a missing token is an answer.
    responses={
        403: {
            "description": (
                "Reset refused, with a named code in the body: RESET_TOKEN_REQUIRED when no token was"
                " presented, RESET_TOKEN_INVALID when the presented token is not the operator's."
            )
        }
    },
)
def demo_reset(
    session: Session = Depends(get_session),
    demo_reset_token: str | None = Header(default=None, alias=DEMO_RESET_HEADER),
) -> dict[str, str]:
    # ORD-0058 item 2: this endpoint deletes 52 tables (`services.reset_all`) - every
    # traveler, every audit row, every commitment - with no env, flag or auth in front of it.
    # On a reachable deployment that is not a demo convenience, it is a data-destruction
    # route, so it now belongs to the simulation switch.  Flag off answers a named 403 and
    # touches nothing; the guard is placed before `stop_autorun()` on purpose, because
    # killing the process-global guardian worker would already be a side effect of a
    # request that was refused.
    if not demo_mode_enabled():
        return error(
            "DEMO_MODE_REQUIRED",
            "Resetting the demo scenario needs CITYNEXUS_DEMO_MODE=true; nothing was deleted.",
            status=403,
            details={"flag": "CITYNEXUS_DEMO_MODE", "tables_protected": True},
            message_key="demo.reset_disabled",
        )
    # ORD-0389: the flag being on is not permission.  Demo mode is a property of the deployment,
    # so on its own it authorized whoever happened to be able to reach it.  This second guard
    # stays ahead of `stop_autorun()` for the same reason the first one does: a refused request
    # must not have already stopped the guardian or touched a row.
    denied = demo_reset_token_denied(demo_reset_token)
    if denied is not None:
        code, message = denied
        return error(
            code,
            message,
            status=403,
            details={"header": DEMO_RESET_HEADER, "tables_protected": True},
        )
    # The demo endpoint is also the E2E/runtime reset boundary.  The Guardian
    # worker is process-global while enrollments are Journey-scoped; leaving
    # the worker alive after replacing the demo rows makes the next Journey
    # appear to have an active autopilot with no subscription of its own.
    stop_autorun()
    summary = seed_demo(session)
    return {"status": "reset", **summary}


@app.post("/api/v1/demo/prepare-scenario")
def demo_prepare_scenario(session: Session = Depends(get_session)):
    """Put a plan on the stage: `plans/generate` then `select`, and nothing beyond that.

    Why this is its own action instead of part of reset (measured, not theorised): reset is the
    fixture door for ~140 modules, and seeding a PlanVersion from it leaked a SELECTED plan into
    `GET /plans` - which does not filter by status - and turned 35 gates red, one of them in
    isolation.  A door that resets state must reset state and nothing else.

    It also stops short of `/start`, because `commerce/validators.py` books only against a
    PLANNED journey with a SELECTED plan: the hard-commitment step of the demo lives in this
    window, and auto-starting would erase it.
    """

    if not demo_mode_enabled():
        return error(
            "DEMO_MODE_REQUIRED",
            "Preparing the demo scenario needs CITYNEXUS_DEMO_MODE=true.",
            status=403,
            details={"flag": "CITYNEXUS_DEMO_MODE"},
            message_key="demo.prepare_disabled",
        )
    with SessionLocal() as scene_session:
        journey = scene_session.scalar(
            select(Journey).where(Journey.status == "DRAFT").order_by(Journey.created_at.desc())
        ) or scene_session.scalar(select(Journey).order_by(Journey.created_at.desc()))
        if journey is None:
            return error(
                "DEMO_SCENARIO_MISSING",
                "There is no seeded scenario to prepare; call demo/reset first.",
                status=409,
                details={"journey_id": None},
                message_key="demo.scenario_missing",
            )
        journey_id = journey.id
        advance = advance_seed_to_executable(scene_session, journey_id)
        scene_session.commit()
        row = scene_session.get(Journey, journey_id)
    return {
        "status": "prepared",
        "journey_id": str(journey_id),
        "journey_status": row.status if row is not None else None,
        "plan_version_id": (
            str(row.active_plan_version_id)
            if row is not None and row.active_plan_version_id
            else None
        ),
        # An object, not a JSON string: a client should not have to parse a payload it was
        # handed in a body it already parsed.
        "advance": advance,
    }

@app.post("/api/v1/demo/start-scenario")
def demo_start_scenario(session: Session = Depends(get_session)):
    """Start the seeded journey, through `/start` and with all of its gates intact.

    The second stage of the demo: reset leaves the journey PLANNED with a selected plan so
    booking and transaction can still be demonstrated, and this is the step that turns it into
    an executing journey with a cursor.  Same `CITYNEXUS_DEMO_MODE` guard as reset (it is a
    state-changing demo action), same reporting: the route refuses by name rather than half-
    doing it, and it reports whatever `/start` decided - including a stale-evidence refusal,
    which is a true answer about the scenario and must not be smoothed over.
    """

    if not demo_mode_enabled():
        return error(
            "DEMO_MODE_REQUIRED",
            "Starting the demo scenario needs CITYNEXUS_DEMO_MODE=true.",
            status=403,
            details={"flag": "CITYNEXUS_DEMO_MODE"},
            message_key="demo.start_disabled",
        )
    with SessionLocal() as scene_session:
        journey = scene_session.scalar(
            select(Journey).where(Journey.active_plan_version_id.is_not(None))
        ) or scene_session.scalar(select(Journey).order_by(Journey.created_at.desc()))
        if journey is None:
            return error(
                "DEMO_SCENARIO_MISSING",
                "There is no seeded scenario to start; call demo/reset first.",
                status=409,
                details={"journey_id": None},
                message_key="demo.scenario_missing",
            )
        journey_id = journey.id
        try:
            twin = start_journey(UUID(journey_id), scene_session)
            scene_session.commit()
        except HTTPException as exc:
            scene_session.rollback()
            return error(
                "DEMO_START_REFUSED",
                str(exc.detail),
                status=exc.status_code,
                details={"journey_id": str(journey_id)},
                message_key="demo.start_refused",
            )
    with SessionLocal() as check:
        row = check.get(Journey, journey_id)
    return {
        "status": "started",
        "journey_id": str(journey_id),
        "journey_status": row.status if row is not None else None,
        "current_activity_id": (
            str(row.current_activity_id) if row is not None and row.current_activity_id else None
        ),
        "twin_present": twin is not None,
    }




@app.post("/api/v1/travelers", status_code=201)
def create_traveler(payload: TravelerCreate, session: Session = Depends(get_session)):
    identifier = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    traveler = Traveler(
        id=identifier,
        display_name=payload.display_name,
        email=payload.email,
        locale=payload.locale,
        timezone=payload.timezone,
        created_at=now,
        updated_at=now,
    )
    session.add(traveler)
    session.flush()
    session.add(
        TravelerTwinSnapshot(
            traveler_id=traveler.id,
            explicit_preferences_json="[]",
            learned_soft_preferences_json="[]",
            conflicts_json="[]",
            confidence=0.0,
            evidence_count=0,
            provenance_json=json.dumps({"source": "traveler-create", "updated_at": now}),
            updated_at=now,
        )
    )
    session.commit()
    record_audit(session, action="traveler.created", entity_type="Traveler", entity_id=traveler.id)
    session.commit()
    return traveler_schema(traveler)


@app.get("/api/v1/travelers/{traveler_id}")
def get_traveler(traveler_id: UUID, session: Session = Depends(get_session)):
    return traveler_schema(require(session, Traveler, str(traveler_id), "Traveler"))


@app.get("/api/v1/travelers/{traveler_id}/preferences")
def list_preferences(traveler_id: UUID, session: Session = Depends(get_session)):
    require(session, Traveler, str(traveler_id), "Traveler")
    items = session.scalars(
        select(Preference)
        .where(Preference.traveler_id == str(traveler_id))
        .order_by(Preference.category, Preference.preference_key)
    ).all()
    return [preference_schema(item) for item in items]


@app.put("/api/v1/travelers/{traveler_id}/preferences")
def replace_preferences(
    traveler_id: UUID, payload: list[PreferenceInput], session: Session = Depends(get_session)
):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    keys = [(item.category, item.key) for item in payload]
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for key in keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        raise HTTPException(422, f"Duplicate preferences are not allowed: {sorted(duplicates)}")
    existing = session.scalars(
        select(Preference)
        .where(Preference.traveler_id == traveler.id)
        .order_by(Preference.category, Preference.preference_key)
    ).all()
    before = [preference_schema(item).model_dump(mode="json") for item in existing]
    session.query(Preference).filter(Preference.traveler_id == traveler.id).delete(
        synchronize_session=False
    )
    now = datetime.now(timezone.utc).isoformat()
    for item in payload:
        session.add(
            Preference(
                id=str(uuid4()),
                traveler_id=traveler.id,
                category=item.category,
                preference_key=item.key,
                value_json=json.dumps(item.value, ensure_ascii=False),
                weight=item.weight,
                source="user",
                created_at=now,
                updated_at=now,
            )
        )
    updated = session.scalars(
        select(Preference)
        .where(Preference.traveler_id == traveler.id)
        .order_by(Preference.category, Preference.preference_key)
    ).all()
    after = [preference_schema(item).model_dump(mode="json") for item in updated]
    record_audit(
        session,
        action="preferences.replaced",
        entity_type="Preference",
        entity_id=traveler.id,
        after={"traveler_id": traveler.id, "preferences": after},
        before={"traveler_id": traveler.id, "preferences": before},
    )
    session.commit()
    return [preference_schema(item) for item in updated]


def _capability_input_rows(
    payload: list[CapabilityInput],
    *,
    scope: str,
    journey_id: str | None = None,
    require_confirmation: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Validate API capability inputs without making provider claims."""

    raw = [item.model_dump(by_alias=True, exclude_none=True) for item in payload]
    for item in raw:
        item["scope"] = scope
        if journey_id is not None:
            item["journey_id"] = journey_id
        if require_confirmation and str(item.get("severity", "SOFT")).upper() == "HARD":
            # Reaching this endpoint is the explicit confirmation gesture;
            # callers need not duplicate a second boolean in the payload.
            item["confirmed"] = True
    canonical = canonicalize_capabilities(
        raw,
        source="USER_CONFIRMED" if require_confirmation else "USER_STATED",
        provenance="USER_CONFIRMED" if require_confirmation else "USER_STATED",
        scope=scope,
        explicit=True,
        confirmed=require_confirmation,
    )
    if len(canonical) != len(raw):
        raise HTTPException(status_code=422, detail="one or more capability codes are unsupported")
    if any(str(item.get("scope")) != scope for item in canonical):
        raise HTTPException(status_code=422, detail="capability scope does not match this endpoint")
    if journey_id is not None:
        for item in canonical:
            item["journey_id"] = journey_id
    return canonical


def _replace_capability_rows(
    session: Session,
    *,
    traveler_id: str,
    capabilities: tuple[dict[str, Any], ...],
    scope: str,
    journey_id: str | None = None,
    actor_id: str | None = None,
    replace_missing: bool = True,
) -> list[dict[str, Any]]:
    """Upsert capability facts; optionally deactivate omitted active rows."""

    existing_query = select(TravelerCapability).where(
        TravelerCapability.traveler_id == traveler_id,
        TravelerCapability.scope == scope,
    )
    if journey_id is None:
        existing_query = existing_query.where(TravelerCapability.journey_id.is_(None))
    else:
        existing_query = existing_query.where(TravelerCapability.journey_id == journey_id)
    existing = {
        row.code: row for row in session.scalars(existing_query).all()
    }
    now = datetime.now(timezone.utc).isoformat()
    incoming_codes = {str(item["code"]) for item in capabilities}
    for row in existing.values():
        if replace_missing and row.active and row.code not in incoming_codes:
            row.active = False
            row.updated_at = now
            record_audit(
                session,
                action="traveler.capability.deactivated",
                entity_type="TravelerCapability",
                entity_id=row.id,
                journey_id=journey_id,
                actor_type="USER",
                actor_id=actor_id or traveler_id,
                before={"code": row.code, "active": True},
                after={"code": row.code, "active": False},
            )
    for item in capabilities:
        code = str(item["code"])
        row = existing.get(code)
        confirmed = bool(item.get("confirmed")) or str(item.get("severity")) != "HARD"
        if row is None:
            row = TravelerCapability(
                id=str(uuid4()),
                traveler_id=traveler_id,
                journey_id=journey_id,
                code=code,
                capability_class=str(item.get("class") or "SOFT_PREFERENCE"),
                severity=str(item.get("severity") or "SOFT"),
                params_json=json.dumps(item.get("params") or {}, ensure_ascii=False, sort_keys=True),
                scope=scope,
                source=str(item.get("source") or "USER_CONFIRMED"),
                provenance=str(item.get("provenance") or "USER_STATED"),
                explicit=True,
                confirmed=confirmed,
                active=True,
                created_at=now,
                updated_at=now,
                confirmed_at=now if confirmed else None,
            )
            session.add(row)
            session.flush()
            record_audit(
                session,
                action="traveler.capability.created",
                entity_type="TravelerCapability",
                entity_id=row.id,
                journey_id=journey_id,
                actor_type="USER",
                actor_id=actor_id or traveler_id,
                after={"code": code, "severity": row.severity, "confirmed": confirmed},
            )
        else:
            row.capability_class = str(item.get("class") or row.capability_class)
            row.severity = str(item.get("severity") or row.severity)
            row.params_json = json.dumps(item.get("params") or {}, ensure_ascii=False, sort_keys=True)
            row.source = str(item.get("source") or row.source)
            row.provenance = str(item.get("provenance") or row.provenance)
            row.explicit = True
            row.confirmed = confirmed or bool(row.confirmed)
            row.active = True
            row.updated_at = now
            if row.confirmed and row.confirmed_at is None:
                row.confirmed_at = now
            record_audit(
                session,
                action="traveler.capability.updated",
                entity_type="TravelerCapability",
                entity_id=row.id,
                journey_id=journey_id,
                actor_type="USER",
                actor_id=actor_id or traveler_id,
                after={"code": code, "severity": row.severity, "confirmed": bool(row.confirmed)},
            )
    session.flush()
    return capability_payload(session, traveler_id, journey_id)


@app.get("/api/v1/travelers/{traveler_id}/capabilities")
def list_traveler_capabilities(traveler_id: UUID, session: Session = Depends(get_session)):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    rows = capability_payload(session, traveler.id)
    return {"traveler_id": traveler.id, "capabilities": rows, "summary": summarize_capabilities(rows)}


@app.put("/api/v1/travelers/{traveler_id}/capabilities")
def replace_traveler_capabilities(
    traveler_id: UUID,
    payload: list[CapabilityInput],
    session: Session = Depends(get_session),
):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    capabilities = _capability_input_rows(payload, scope="TRAVELER", require_confirmation=False)
    # Persisting a hard need is a durable authority decision.  Require the
    # explicit confirmation endpoint for those rows, while soft/strong needs
    # may be staged as user-stated context.
    if any(item.get("severity") == "HARD" and not item.get("confirmed") for item in capabilities):
        raise HTTPException(status_code=409, detail="hard capability needs require /confirm")
    rows = _replace_capability_rows(
        session, traveler_id=traveler.id, capabilities=capabilities, scope="TRAVELER", actor_id=traveler.id,
    )
    snapshot = refresh_snapshot(session, traveler.id)
    session.commit()
    return {"traveler_id": traveler.id, "capabilities": rows, "summary": summarize_capabilities(rows), "traveler_twin": traveler_twin_payload(snapshot)}


@app.post("/api/v1/travelers/{traveler_id}/capabilities/confirm")
def confirm_traveler_capabilities(
    traveler_id: UUID,
    payload: list[CapabilityInput],
    session: Session = Depends(get_session),
):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    capabilities = _capability_input_rows(
        payload, scope="TRAVELER", require_confirmation=True
    )
    rows = _replace_capability_rows(
        session, traveler_id=traveler.id, capabilities=capabilities, scope="TRAVELER", actor_id=traveler.id,
        replace_missing=False,
    )
    snapshot = refresh_snapshot(session, traveler.id)
    session.commit()
    return {"traveler_id": traveler.id, "capabilities": rows, "summary": summarize_capabilities(rows), "traveler_twin": traveler_twin_payload(snapshot)}


@app.get("/api/v1/journeys/{journey_id}/capabilities")
def list_journey_capabilities(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = capability_payload(session, journey.traveler_id, journey.id)
    return {"journey_id": journey.id, "capabilities": rows, "summary": summarize_capabilities(rows)}


@app.put("/api/v1/journeys/{journey_id}/capabilities")
def replace_journey_capabilities(
    journey_id: UUID,
    payload: list[CapabilityInput],
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    capabilities = _capability_input_rows(
        payload, scope="JOURNEY", journey_id=journey.id, require_confirmation=False
    )
    if any(item.get("severity") == "HARD" and not item.get("confirmed") for item in capabilities):
        raise HTTPException(status_code=409, detail="hard capability needs require /confirm")
    rows = _replace_capability_rows(
        session,
        traveler_id=journey.traveler_id,
        journey_id=journey.id,
        capabilities=capabilities,
        scope="JOURNEY",
        actor_id=journey.traveler_id,
    )
    snapshot = refresh_snapshot(session, journey.traveler_id)
    session.commit()
    return {"journey_id": journey.id, "capabilities": rows, "summary": summarize_capabilities(rows), "traveler_twin": traveler_twin_payload(snapshot)}


@app.post("/api/v1/journeys/{journey_id}/capabilities/confirm")
def confirm_journey_capabilities(
    journey_id: UUID,
    payload: list[CapabilityInput],
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    capabilities = _capability_input_rows(
        payload, scope="JOURNEY", journey_id=journey.id, require_confirmation=True
    )
    rows = _replace_capability_rows(
        session,
        traveler_id=journey.traveler_id,
        journey_id=journey.id,
        capabilities=capabilities,
        scope="JOURNEY",
        actor_id=journey.traveler_id,
        replace_missing=False,
    )
    feasibility = None
    recovery = None
    if journey.status == "ACTIVE":
        # Re-evaluate the active plan against the newly confirmed capability.
        # A HARD need (or any evidence regression) invalidates it; the active
        # plan itself is never silently rewritten.
        try:
            gate = validate_selected_plan_feasibility(session, journey)
            failures = list(gate.failures) if gate else []
            passed = gate.passed if gate else None
        except ValueError as exc:
            # Stale/inconsistent frozen evidence is itself an invalidation.
            failures = [str(exc)]
            passed = False
        feasibility = {
            "passed": passed,
            "failures": failures,
            "state": (
                "PLAN_FEASIBLE"
                if passed
                else "REPLAN_REQUIRED"
                if passed is False
                else "NO_ACTIVE_PLAN"
            ),
        }
        hard_codes = [
            str(item["code"])
            for item in rows
            if str(item.get("severity", "")).upper() == "HARD"
        ]
        if passed is False:
            # Durable, governed recovery state - not a response-only field.
            hard_code = hard_codes[0] if hard_codes else "EVIDENCE_REGRESSION"
            event = open_recovery_event(
                session,
                journey,
                capability_code=hard_code,
                capability_params={},
                failures=failures,
            )
            record_audit(
                session,
                action="journey.recovery.required",
                entity_type="Event",
                entity_id=event.id,
                journey_id=journey.id,
                actor_type="USER",
                actor_id=journey.traveler_id,
                after={
                    "reason": "HARD_CAPABILITY_INVALIDATES_ACTIVE_PLAN",
                    "capability_code": hard_code,
                    "failures": failures,
                },
            )
            recovery = recovery_state(
                session, journey, is_feasible=False, failures=failures
            )
        elif passed is True:
            resolve_recovery_events(session, journey)
    snapshot = refresh_snapshot(session, journey.traveler_id)
    session.commit()
    return {
        "journey_id": journey.id,
        "capabilities": rows,
        "summary": summarize_capabilities(rows),
        "traveler_twin": traveler_twin_payload(snapshot),
        "active_plan_feasibility": feasibility,
        "recovery": recovery,
    }


@app.post("/api/v1/journeys/{journey_id}/recovery/acknowledge")
def acknowledge_journey_recovery(
    journey_id: UUID, session: Session = Depends(get_session)
):
    """Governed acknowledgement of an open recovery warning.

    This records that the traveller saw the warning.  It deliberately does NOT
    resolve the recovery, does not mark the plan feasible, and does not hide the
    journey truth: ``experience_flags.needs_review`` stays true until a
    revalidated feasible plan is actually selected.
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    event = acknowledge_recovery(session, journey)
    if event is None:
        raise HTTPException(404, "no open recovery to acknowledge")
    failures: list[str] = []
    is_feasible: bool | None = None
    if journey.active_plan_version_id:
        try:
            gate = validate_selected_plan_feasibility(session, journey)
            is_feasible = None if gate is None else bool(gate.passed)
            failures = list(gate.failures) if gate else []
        except ValueError as exc:
            is_feasible = False
            failures = [str(exc)]
    record_audit(
        session,
        action="journey.recovery.acknowledged",
        entity_type="Event",
        entity_id=event.id,
        journey_id=journey.id,
        actor_type="USER",
        actor_id=journey.traveler_id,
        after={"acknowledged": True, "resolved": False, "still_infeasible": is_feasible is False},
    )
    recovery = recovery_state(
        session, journey, is_feasible=is_feasible, failures=failures
    )
    session.commit()
    return {
        "journey_id": journey.id,
        "acknowledged": True,
        "resolved": False,
        "recovery": recovery,
    }


@app.get("/api/v1/travelers/{traveler_id}/traveler-twin")
def get_traveler_twin(traveler_id: UUID, session: Session = Depends(get_session)):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    snapshot = refresh_snapshot(session, traveler.id)
    session.commit()
    return traveler_twin_payload(snapshot)


@app.delete("/api/v1/travelers/{traveler_id}/traveler-twin/learned")
def clear_traveler_twin(traveler_id: UUID, session: Session = Depends(get_session)):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    snapshot = clear_learned(session, traveler.id)
    record_audit(
        session,
        action="traveler.twin.learned_cleared",
        entity_type="TravelerTwinSnapshot",
        entity_id=traveler.id,
        actor_type="USER",
        actor_id=traveler.id,
        after={"traveler_id": traveler.id, "learned_evidence_cleared": True},
    )
    session.commit()
    return traveler_twin_payload(snapshot)


@app.get("/api/v1/journeys/{journey_id}/outcomes")
def list_journey_outcomes(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(JourneyOutcomeEvent)
        .where(JourneyOutcomeEvent.journey_id == journey.id)
        .order_by(JourneyOutcomeEvent.occurred_at_actual, JourneyOutcomeEvent.id)
    ).all()
    return {
        "journey_id": journey.id,
        "events": [
            {
                "id": item.id,
                "activity_id": item.activity_id,
                "event_type": item.event_type,
                "actor_type": item.actor_type,
                "occurred_at_actual": item.occurred_at_actual,
                "planned_start_at": item.planned_start_at,
                "planned_end_at": item.planned_end_at,
                "source": item.source,
                "payload": _json_dict(item.payload_json),
                "related_proposal_id": item.related_proposal_id,
                "related_agent_run_id": item.related_agent_run_id,
            }
            for item in rows
        ],
    }


@app.post("/api/v1/journeys/{journey_id}/outcomes", status_code=201)
def record_journey_outcome(
    journey_id: UUID,
    payload: OutcomeEventCreate,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    activity = session.get(Activity, str(payload.activity_id)) if payload.activity_id else None
    if activity is not None:
        plan_ids = {
            row.id
            for row in session.scalars(
                select(PlanVersion).where(PlanVersion.journey_id == journey.id)
            ).all()
        }
        if activity.plan_version_id not in plan_ids:
            raise HTTPException(422, "activity is not on the Journey")
    item = _record_outcome_event(
        session,
        journey,
        activity=activity,
        event_type=payload.event_type,
        actor_type=payload.actor_type,
        occurred_at_actual=(
            payload.occurred_at_actual.isoformat() if payload.occurred_at_actual else None
        ),
        payload=payload.payload,
        related_proposal_id=str(payload.related_proposal_id)
        if payload.related_proposal_id
        else None,
        related_agent_run_id=(
            str(payload.related_agent_run_id) if payload.related_agent_run_id else None
        ),
    )
    session.commit()
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "activity_id": item.activity_id,
        "event_type": item.event_type,
        "occurred_at_actual": item.occurred_at_actual,
    }


@app.get("/api/v1/travelers/{traveler_id}/agent-policy", response_model=AgentPolicySchema)
def get_agent_policy(traveler_id: UUID, session: Session = Depends(get_session)):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    row = get_or_create_policy(session, traveler.id)
    payload = policy_payload(row)
    session.commit()
    return payload


@app.put("/api/v1/travelers/{traveler_id}/agent-policy", response_model=AgentPolicySchema)
def put_agent_policy(
    traveler_id: UUID,
    payload: AgentPolicyPayload,
    session: Session = Depends(get_session),
):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    before = policy_payload(get_or_create_policy(session, traveler.id))
    row = update_policy(
        session,
        traveler.id,
        mode=AgentMode(payload.mode),
        auto_accept_replan=payload.auto_accept_replan,
        max_auto_replan_cost_delta_minor=payload.max_auto_replan_cost_delta_minor,
        max_auto_loss_minor=payload.max_auto_loss_minor,
        max_auto_schedule_drift_min=payload.max_auto_schedule_drift_min,
        allowed_activity_types=payload.allowed_activity_types,
        allowed_booking_types=payload.allowed_booking_types,
        protected_commitment_levels=payload.protected_commitment_levels,
        allow_auto_commitment_management=payload.allow_auto_commitment_management,
        allow_auto_when_provider_degraded=payload.allow_auto_when_provider_degraded,
        allow_auto_on_critical_event=payload.allow_auto_on_critical_event,
    )
    after = policy_payload(row)
    record_audit(
        session,
        action="agent.policy.updated",
        entity_type="TravelerAgentPolicy",
        entity_id=traveler.id,
        journey_id=None,
        actor_type="traveler",
        actor_id=traveler.id,
        before=before,
        after=after,
    )
    session.commit()
    return after


def suggested_room_count(party_size: int) -> int:
    """Offer a small-group starting point while leaving room choice explicit."""

    if party_size <= 2:
        return 1
    if party_size <= 4:
        return 2
    if party_size <= 6:
        return 3
    return max(3, (party_size + 2) // 3)


@app.post("/api/v1/journeys", status_code=201)
def create_journey(payload: JourneyCreate, session: Session = Depends(get_session)):
    require(session, Traveler, str(payload.traveler_id), "Traveler")
    now = datetime.now(timezone.utc).isoformat()
    occupancy = dict(payload.occupancy)
    occupancy.setdefault("adults", payload.party_size)
    if "rooms" not in occupancy:
        occupancy["rooms"] = suggested_room_count(payload.party_size)
        occupancy["rooms_source"] = "SUGGESTED"
    else:
        occupancy["rooms_source"] = "USER"
    journey = Journey(
        id=str(uuid4()),
        traveler_id=str(payload.traveler_id),
        title=payload.title,
        city_code=payload.city_code,
        timezone=payload.timezone,
        start_at=payload.start_at.isoformat(),
        end_at=payload.end_at.isoformat(),
        budget_currency=payload.budget.currency,
        budget_amount_minor=payload.budget.amount_minor,
        planning_mode=payload.planning_mode,
        status="DRAFT",
        active_plan_version_id=None,
        current_activity_id=None,
        created_at=now,
        updated_at=now,
    )
    now = datetime.now(timezone.utc).isoformat()
    trip_context = JourneyTripContext(
        journey_id=journey.id,
        arrival_anchor_json=json.dumps(
            payload.arrival_anchor.model_dump(mode="json") if payload.arrival_anchor else {},
            ensure_ascii=False,
        ),
        departure_anchor_json=json.dumps(
            payload.departure_anchor.model_dump(mode="json")
            if payload.departure_anchor
            else {},
            ensure_ascii=False,
        ),
        party_size=payload.party_size,
        occupancy_json=json.dumps(occupancy, ensure_ascii=False),
        stay_budget_amount_minor=payload.stay_budget.amount_minor if payload.stay_budget else 0,
        stay_budget_currency=payload.stay_budget.currency if payload.stay_budget else payload.budget.currency,
        # ORD-0355: `stay_budget` absent from the request is the traveller's silence and is
        # stored as such.  The amount above keeps its 0 so no existing reader of that column
        # changes shape; nothing outside this row may read the 0 as a declared price.
        stay_budget_declared=payload.stay_budget is not None,
        created_at=now,
        updated_at=now,
    )
    # ORD-0033 (ruling A): the supported window is a creation rule, checked here on the pair
    # of rows this request is about to write, through the same helper stay pricing uses
    # (`journey_temporal_shape` - an anchor's own moment wins over the journey window).  Two
    # consequences are deliberate: a refused request never reaches `session.add`, so repeated
    # taps cannot pile up unusable journeys (the preview library had five), and the door
    # cannot accept a window that `/stay-options/generate` would later reject.  The raised
    # `V13_…` becomes a named 422 in `temporal_rejection` above.
    journey_temporal_shape(trip_context, journey)
    session.add(journey)
    session.add(trip_context)
    session.commit()
    record_audit(
        session,
        action="journey.created",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
    )
    session.commit()
    return journey_schema(journey)


#: ORD-0477: the name `GET /api/v1/journeys` answers when `sort` is neither absent nor a tier.  It
#: rides in `error.reason_code` under the door's existing `error.code`, like `JOURNEY_NOT_FOUND` and
#: `JOURNEY_ALREADY_DELETED` do, so no new failure family enters the suite (the G6 boundary in
#: `backend/tests/test_qoder_ord0272_continuation.py`).
JOURNEY_SORT_REFUSAL = "JOURNEY_SORT_NOT_RECOGNISED"


@app.get(
    "/api/v1/journeys",
    response_model=list[JourneySchema],
    # ORD-0477: a `sort` this door does not know is an answer the door has to give, so FastAPI's own
    # spec says it out loud too.  The hand contract alone was not enough (ORD-0395: the 403 was in
    # `contracts/openapi.yaml` and missing from `/openapi.json`, so a consumer of the generated spec
    # could not see that a refusal existed).
    responses={
        422: {
            "description": (
                "error.code = VALIDATION_ERROR with error.reason_code = "
                "JOURNEY_SORT_NOT_RECOGNISED - `sort` named no tier this door knows, and the door "
                "refuses instead of answering in its own default order."
            )
        }
    },
)
def list_journeys(
    session: Session = Depends(get_session),
    sort: str | None = Query(
        default=None,
        json_schema_extra={"enum": list(JOURNEY_SORT_TIERS)},
        description=(
            "Which order to answer in: RECENT_UPDATE (journeys.updated_at, newest first), "
            "DEPARTURE_DATE (journeys.start_at, earliest first) or TITLE (journeys.title, "
            "case-folded).  Any other word is refused by name.  No `sort` keeps the order this "
            "door has always answered in - journeys.created_at, then journeys.id."
        ),
    ),
):
    """Every trip on this server that a traveller has not put in the recycle bin.

    ORD-0383 slice 1 keeps this door to one rule - `deleted_at IS NULL` - so it has no
    pagination, no per-traveller scope and no bin listing; those are the next slices' orders.
    A deleted trip stays readable at `GET /api/v1/journeys/{journey_id}` and behind it at the
    sub-resource doors: the query below is the only place on this tree that filters the column,
    and no other module under `backend/app` reads it.  Slice 1 removes a trip from the list,
    not from the store, and the row is what a recycle bin will have to show.

    ORD-0477 adds the order a traveller reads that list in, as one named tier out of
    `JOURNEY_SORT_TIERS`.  Three of its decisions are deliberate:

    * The tier is a *query* value, so `sort` is read as a plain string and checked here rather
      than declared as a `Literal`.  A `Literal` would have pydantic answer an unknown word with
      the generic `VALIDATION_ERROR` and no name of its own, which is the silent-default shape the
      order forbids: `JOURNEY_SORT_NOT_RECOGNISED` (422) says which word was sent and which three
      the door knows, and `error.code` stays in the one family
      `backend/tests/test_qoder_ord0272_continuation.py::test_G6...` keeps the suite to.
    * Without `sort` the SELECT is the one slice 1 shipped, byte for byte.  Clients that do not
      know about tiers keep the answer they had before; only a caller that names a tier gets a
      different order, and no order is implied by an unrecognised word.
    * A tier sorts *instants*, not characters.  `journeys.start_at` is TEXT written straight from
      the posted value (`create_journey` stores `payload.start_at.isoformat()`), so `+08:00` and
      `+00:00` rows coexist on this tree - the fixtures in
      `backend/tests/test_qoder_ord0383_journey_soft_delete.py` insert `+08:00` ones.  Comparing
      those strings would put 09:00+08:00 (01:00 UTC) after 08:00+00:00.  `_journey_instant`
      converts to UTC first; a stored value with no offset is read as UTC, which is stated in the
      hand contract rather than left to whichever reader parses it first.

    The order is applied in this function rather than by SQL `ORDER BY` because the keys are
    parsed values, and because SQLite compares TEXT by code unit: `ORDER BY title` would sort
    `Zoo` before `apple` and give a traveller an order the door never named.
    """

    live = select(Journey).where(Journey.deleted_at.is_(None))
    if sort is None:
        items = session.scalars(live.order_by(Journey.created_at, Journey.id)).all()
        return [journey_schema(item) for item in items]
    if sort not in JOURNEY_SORT_TIERS:
        refused = HTTPException(
            422,
            f"sort={sort} names no journey list order this server knows",
        )
        refused.reason_code = JOURNEY_SORT_REFUSAL
        refused.error_details = {"sort": sort, "supported": list(JOURNEY_SORT_TIERS)}
        raise refused
    rows = _journeys_in_tier_order(list(session.scalars(live).all()), sort)
    return [journey_schema(item) for item in rows]


#: The two tiers that read a timestamp and the one that reads a name, each with the stored column it
#: orders by and the direction a traveller gets.  Nothing here re-reads a column it does not list:
#: `journeys.updated_at` is written by this server's own doors (`update_journey` on PATCH,
#: `create_journey` on create), and `journeys.title` is what `PATCH /api/v1/journeys/{id}` changes.
_JOURNEY_TIER_KEYS = {
    "RECENT_UPDATE": (lambda row: _journey_instant(row.updated_at), True),
    "DEPARTURE_DATE": (lambda row: _journey_instant(row.start_at), False),
    "TITLE": (lambda row: row.title.casefold(), False),
}


def _journey_instant(value: str) -> datetime:
    """The UTC instant a stored `journeys` timestamp names.

    A value with no offset - reachable by posting a naive `start_at` to the create door, which
    stores `payload.start_at.isoformat()` - is read as UTC.  That is a stated rule, not a guess: the
    column is TEXT with no CHECK (`backend/db/schema.sql`, the `journeys` table), so the door has to
    pick a reading, and UTC is the one this server writes for its own timestamps
    (`datetime.now(timezone.utc).isoformat()`).
    """

    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _journeys_in_tier_order(rows: list[Journey], tier: str) -> list[Journey]:
    """`rows` in the order `tier` names, ties broken by `journeys.id` ascending.

    Two passes, because Python's sort is stable: ordering by id first makes every tier's tie-break
    ascending by id even where the tier itself runs newest-first, so the answer is a total order and
    two calls cannot interleave a tie differently.
    """

    key, descending = _JOURNEY_TIER_KEYS[tier]
    ordered = sorted(rows, key=lambda row: row.id)
    ordered.sort(key=key, reverse=descending)
    return ordered



@app.delete("/api/v1/journeys/{journey_id}", status_code=204)
def delete_journey(journey_id: UUID, session: Session = Depends(get_session)):
    """Soft delete: record the moment in `journeys.deleted_at`, keep the row.

    The two refusals carry their own name in `error.reason_code` - `JOURNEY_NOT_FOUND` on the
    404, `JOURNEY_ALREADY_DELETED` on the 409 - because a screen must not say one sentence for
    both: there is no such row, versus that row is already in the bin.  They do not mint a new
    `error.code`: `backend/tests/test_qoder_ord0272_continuation.py::test_G6...` keeps the
    failure vocabulary to the one family this server already answers with, and ORD-0101 built
    `reason_code` for exactly this case.  A second tap leaves the first moment alone rather
    than overwriting it, so a trip cannot look more recently deleted than it is.

    `status` is not touched.  It answers what the trip was doing, and its closed CHECK
    (`backend/db/schema.sql`) has no word for "in the bin" - inventing one would rebuild
    the table for a fact that now has a column of its own.
    """

    journey = session.get(Journey, str(journey_id))
    if journey is None:
        refused = HTTPException(404, f"Journey {journey_id} was not found")
        refused.reason_code = "JOURNEY_NOT_FOUND"
        refused.error_details = {"journey_id": str(journey_id)}
        raise refused
    if journey.deleted_at is not None:
        refused = HTTPException(409, f"Journey {journey_id} is already deleted")
        refused.reason_code = "JOURNEY_ALREADY_DELETED"
        refused.error_details = {"journey_id": str(journey_id), "deleted_at": journey.deleted_at}
        raise refused
    before = journey_schema(journey).model_dump(mode="json")
    journey.deleted_at = datetime.now(timezone.utc).isoformat()
    session.commit()
    record_audit(
        session,
        action="journey.deleted",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        before=before,
        # ORD-0535: the bin row can only say *which* discard it is if each audit row carries the
        # moment this write stored.  `created_at` is when the log line was made, not what the column
        # holds, and the two are separate clock reads -- so the stored value is written down here
        # rather than being inferred from the log's own timestamp.
        after={"deleted_at": journey.deleted_at},
    )
    session.commit()


#: How often the 找回 door re-reads before it admits another writer keeps winning this row.  It is
#: not a wait budget and sets no clock: the lock wait itself has exactly one home,
#: `app/db/write_deadline.py`.  A retry costs one SELECT and one failed UPDATE.
_UNDELETE_WRITE_ATTEMPTS = 3


@app.post("/api/v1/journeys/{journey_id}/undelete", response_model=JourneySchema)
def undelete_journey(journey_id: UUID, session: Session = Depends(get_session)):
    """ORD-0399 找回: clear `journeys.deleted_at`, and the trip is back in the list.

    The word is 找回 / undelete, chosen by measurement rather than taste.  恢复 and `recover*`
    are autonomous recovery in this product (`POST /api/v1/journeys/{journey_id}/recovery/acknowledge`
    above, and `recovery.*` copy keys); 还原 is already the plan-version screen's word for reverting
    to an older plan (`frontend/src/features/studio/planVersionHistoryCopy.ts` states that rule);
    `restore` in this tree means "a reload brings the same draft/panel back" (`main.py`).  An
    audit action of `journey.restored` would have handed 恢复 a fourth meaning.

    `JOURNEY_NOT_DELETED` (409) is the refusal that makes this door honest: bringing back a trip
    that was never in the bin is not a silent success, because a screen reporting "found it again"
    would be reporting something that did not happen.  `JOURNEY_NOT_FOUND` (404) is the same name
    slice 1 uses.  Neither mints an `error.code` - the G6 boundary that
    `backend/tests/test_qoder_ord0272_continuation.py::test_G6...` keeps - both ride in
    `error.reason_code`.

    No `Idempotency-Key`.  Slice 1's DELETE took none and this door does not either: whether the
    write happens is decided by durable state (`deleted_at` NULL or not), and a replayed tap lands
    on `JOURNEY_NOT_DELETED` instead of pretending to be a second success.  The doors that do take
    the header replay a *computation* (plan claim, replan accept), which is a different need.

    Two taps at once is the case that was unguarded when this door was cut, and it is guarded now,
    by the rule the replan accept already states (`_accept_replan_executing`): conditional write,
    not object assignment.  The `UPDATE` carries the deletion moment this tap read, so the database
    decides who cleared the row; a tap that finds a different value loses on `rowcount`, re-reads,
    and refuses with a name instead of writing the same row twice or dropping a second audit row.

    `status` and `updated_at` are not written, for the same reason slice 1 left them alone: the bin
    has one column, and duplicating the fact into `updated_at` would give a reader two authorities.
    """

    for _ in range(_UNDELETE_WRITE_ATTEMPTS):
        journey = session.get(Journey, str(journey_id))
        if journey is None:
            refused = HTTPException(404, f"Journey {journey_id} was not found")
            refused.reason_code = "JOURNEY_NOT_FOUND"
            refused.error_details = {"journey_id": str(journey_id)}
            raise refused
        seen = journey.deleted_at
        if seen is None:
            refused = HTTPException(409, f"Journey {journey_id} was not in the recycle bin")
            refused.reason_code = "JOURNEY_NOT_DELETED"
            refused.error_details = {"journey_id": str(journey_id), "deleted_at": None}
            raise refused
        # 抢不到就不写: the predicate carries the moment this tap read, so the database - not this
        # process's view of an ORM object - decides who cleared the row.  Same shape as
        # `_take_over_lapsed_claims`, `select_plan` and `_accept_replan_executing`; nothing new.
        cleared = session.execute(
            update(Journey)
            .where(Journey.id == str(journey_id), Journey.deleted_at == seen)
            .values(deleted_at=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        if cleared == 1:
            break
        session.rollback()
    else:
        # Every attempt was overtaken.  The truthful words for that are not "it was not in the bin"
        # (it is) and not a silent success (nothing was written here), so the refusal names the
        # state the row is in, reusing the name slice 1 already answers with.
        current = session.get(Journey, str(journey_id))
        refused = HTTPException(409, f"Journey {journey_id} is still in the recycle bin")
        refused.reason_code = "JOURNEY_ALREADY_DELETED"
        refused.error_details = {
            "journey_id": str(journey_id),
            "deleted_at": current.deleted_at if current is not None else None,
        }
        raise refused
    before = {"deleted_at": seen}
    session.commit()
    record_audit(
        session,
        action="journey.undeleted",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        before=before,
        after={"deleted_at": None},
    )
    session.commit()
    return journey_schema(session.get(Journey, str(journey_id)))


#: How often the 中止 door re-reads before it admits that another writer keeps winning this row.
#: Same rule as `_UNDELETE_WRITE_ATTEMPTS`: it is not a wait budget and it sets no clock, because
#: the lock wait has exactly one home (`app/db/write_deadline.py`).  A lost attempt costs one
#: SELECT and one UPDATE that matched nothing.
_ABORT_WRITE_ATTEMPTS = 3


def _abort_refusal(status: int, message: str, reason_code: str, details: dict) -> HTTPException:
    """One shape for every refusal this door makes: the status picks the generic code, the name
    rides in `error.reason_code` (`_share_refusal` above is the same rule for the grant doors)."""

    refused = HTTPException(status, message)
    refused.reason_code = reason_code
    refused.error_details = details
    return refused


@app.post(
    "/api/v1/journeys/{journey_id}/abort",
    response_model=JourneySchema,
    # ORD-0395: a status this decorator does not name never reaches the generated document, and the
    # parity door compares path x method, so an undeclared refusal is a silent agreement between two
    # documents that disagree.  Every class this function can answer is named here.
    responses={
        403: {
            "description": "error.reason_code = ABORT_ACTOR_NOT_OWNER - the actor named on this "
            "request is not the traveller the row belongs to, so it carries no authority over it"
        },
        404: {
            "description": "error.code = NOT_FOUND with error.reason_code = JOURNEY_NOT_FOUND"
        },
        409: {
            "description": "error.code = CONFLICT with error.reason_code = JOURNEY_ALREADY_ABORTED "
            "/ ABORT_JOURNEY_IN_RECYCLE_BIN / ABORT_WRITE_CONTESTED"
        },
    },
)
def abort_journey(
    journey_id: UUID, payload: JourneyAbort, session: Session = Depends(get_session)
):
    """ORD-0520 中止: the trip is called off, with the traveller's reason stored on the row.

    `journeys.status` gains `CANCELLED`, the one word the closed CHECK in `backend/db/schema.sql`
    already held and that no door on this tree wrote before this order (the writers that exist put
    PLANNED / ACTIVE / COMPLETED there).  `aborted_at` and `abort_reason` are written in the same
    UPDATE as the status, so a row cannot be aborted with no reason attached: the reason is what the
    traveller typed, kept as given, and it is read back by `getJourney`, `listJourneys` and
    `getJourneyExperience` rather than living only on the screen that asked for it.

    **中止 is not 丢弃.** Nothing here touches `deleted_at`, so the trip stays in
    `GET /api/v1/journeys` -- a list door that hid an aborted trip would be a recycle bin by another
    name, and the bin's doors (`deleteJourney`, `undeleteJourney`) answer with their own refusals.
    What an aborted trip loses is its place on 实时行程, and that judgement is not left to the
    device: every journey this server answers with carries `live_view` = {included, reason_code},
    computed in one function (`app/services.py` -- `journey_live_view`) from the same durable status
    this door writes.  A screen that filtered `status != "CANCELLED"` itself would hold a second
    copy of that rule.

    Four refusals, each with its own name, because each is a different fact a screen has to say
    differently: `JOURNEY_NOT_FOUND` (404, no such row), `ABORT_ACTOR_NOT_OWNER` (403, the row is
    somebody else's), `ABORT_JOURNEY_IN_RECYCLE_BIN` (409, the owner has already dropped it -- the
    `<feature>_JOURNEY_IN_RECYCLE_BIN` shape the check-in, calendar and share-grant doors use), and
    `JOURNEY_ALREADY_ABORTED` (409, and the body repeats the moment and reason that are already
    stored instead of overwriting them with this tap's).  A fifth, `ABORT_WRITE_CONTESTED`, is the
    honest answer when a tap loses the row three times over to a writer that neither aborted it nor
    binned it; no single screen tap reaches it, and it must never be dressed up as a success.

    `updated_at` is not written, for the same reason 找回 left it alone: `aborted_at` is the
    authority for this moment, and a second timestamp for the same instant hands a reader two.
    No `Idempotency-Key` either -- whether the write happens is decided by durable state, so a
    replayed tap lands on `JOURNEY_ALREADY_ABORTED` rather than pretending to be a second success.
    The conditional UPDATE carries the status and the empty `aborted_at` this tap read, so the
    database, not this process's view of an ORM object, decides who wrote the row.

    What this door does *not* decide is recorded in the contract: an `actor_traveler_id` is a
    declared identity, the same kind the member doors take, and binding a caller to a name is the
    daylight authentication decision this order leaves open.  Cancelling an aborted trip is not
    undoing it -- this server has no un-abort door, so no copy on any screen may promise one; the
    words for getting a trip back belong to the bin (找回 / 还原), and 恢复 stays autonomous recovery.
    """

    for _ in range(_ABORT_WRITE_ATTEMPTS):
        journey = session.get(Journey, str(journey_id))
        if journey is None:
            raise _abort_refusal(
                404,
                f"Journey {journey_id} was not found",
                "JOURNEY_NOT_FOUND",
                {"journey_id": str(journey_id)},
            )
        if journey.traveler_id != str(payload.actor_traveler_id):
            raise _abort_refusal(
                403,
                "The actor named on this request is not the traveller that journey belongs to.",
                "ABORT_ACTOR_NOT_OWNER",
                {
                    "field": "actor_traveler_id",
                    "journey_id": str(journey_id),
                    "journey_traveler_id": journey.traveler_id,
                },
            )
        if journey.deleted_at is not None:
            raise _abort_refusal(
                409,
                f"Journey {journey_id} is in the recycle bin; 中止 is not 丢弃.",
                "ABORT_JOURNEY_IN_RECYCLE_BIN",
                {
                    "journey_id": str(journey_id),
                    "deleted_at": journey.deleted_at,
                    "abort_state": "NOT_WRITTEN",
                },
            )
        # The durable gate is `aborted_at`, not `status`.  Measured with the socket race probe
        # (`review/ord0520/abort_contested_race_probe_r1.py`) against the first version of this door:
        # a tap whose read saw `status` moved off CANCELLED re-aborted the trip, and one journey
        # collected up to fourteen `journey.aborted` audit rows while the traveller's stored reason
        # was replaced by a tap that wrote nothing new.  A word in a mutable column is not a fact
        # about this door; the column this door owns is.
        if journey.aborted_at is not None or journey.status == "CANCELLED":
            raise _abort_refusal(
                409,
                f"Journey {journey_id} was already aborted.",
                "JOURNEY_ALREADY_ABORTED",
                {
                    "journey_id": str(journey_id),
                    "aborted_at": journey.aborted_at,
                    "abort_reason": journey.abort_reason,
                },
            )
        seen_status = journey.status
        seen_aborted_at = journey.aborted_at
        moment = datetime.now(timezone.utc).isoformat()
        # 抢不到就不写: the predicate holds what this tap read, so a tap that raced another one
        # loses on `rowcount` instead of writing over the winner's reason with its own.
        written = session.execute(
            update(Journey)
            .where(
                Journey.id == str(journey_id),
                Journey.status == seen_status,
                # `IS NULL`, not "equals what I read": the write may only ever be the first one.
                Journey.aborted_at.is_(None),
            )
            .values(
                status="CANCELLED",
                aborted_at=moment,
                abort_reason=payload.reason,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if written == 1:
            break
        session.rollback()
    else:
        current = session.get(Journey, str(journey_id))
        if current is None:
            raise _abort_refusal(
                404,
                f"Journey {journey_id} was not found",
                "JOURNEY_NOT_FOUND",
                {"journey_id": str(journey_id)},
            )
        if current.aborted_at is not None or current.status == "CANCELLED":
            raise _abort_refusal(
                409,
                f"Journey {journey_id} was already aborted.",
                "JOURNEY_ALREADY_ABORTED",
                {
                    "journey_id": str(journey_id),
                    "aborted_at": current.aborted_at,
                    "abort_reason": current.abort_reason,
                },
            )
        if current.deleted_at is not None:
            raise _abort_refusal(
                409,
                f"Journey {journey_id} is in the recycle bin; 中止 is not 丢弃.",
                "ABORT_JOURNEY_IN_RECYCLE_BIN",
                {
                    "journey_id": str(journey_id),
                    "deleted_at": current.deleted_at,
                    "abort_state": "NOT_WRITTEN",
                },
            )
        raise _abort_refusal(
            409,
            f"Journey {journey_id} changed while this attempt was running, so nothing was written.",
            "ABORT_WRITE_CONTESTED",
            {
                "journey_id": str(journey_id),
                "status_seen": seen_status,
                "status_now": current.status,
                "attempts": _ABORT_WRITE_ATTEMPTS,
            },
        )

    before = {"status": seen_status, "aborted_at": seen_aborted_at, "abort_reason": None}
    session.commit()
    record_audit(
        session,
        action="journey.aborted",
        entity_type="Journey",
        entity_id=str(journey_id),
        journey_id=str(journey_id),
        before=before,
        after={"status": "CANCELLED", "aborted_at": moment, "abort_reason": payload.reason},
        actor_type="traveler",
        actor_id=str(payload.actor_traveler_id),
    )
    session.commit()
    # The answer must be the row, not this process's memory of it.  `SessionLocal` runs with
    # `expire_on_commit=False` (`app/db/session.py`), and the write above is a conditional UPDATE
    # with `synchronize_session=False`, so nothing puts the new columns back on the loaded object:
    # without this refresh a successful tap would answer 200 with the status and the reason it had
    # *before* the write, and `abort_reason: null` would be the first lie on the screen.  Measured,
    # not assumed -- the arm in `backend/tests/test_qoder_ord0520_journey_abort.py` that reads
    # `body["status"]` was red against the un-refreshed version.
    session.refresh(journey)
    return journey_schema(journey)


@app.get("/api/v1/recycle-bin/journeys", response_model=list[RecycleBinJourney])
def list_recycle_bin_journeys(session: Session = Depends(get_session)):
    """ORD-0402 slice 1: the trips that are in the recycle bin, each with its own deletion moment.

    This is a second door. `GET /api/v1/journeys` (above) keeps its promise of answering only the
    rows with `deleted_at IS NULL`, so nothing here changes what a client of that door sees, and a
    screen does not have to notice an absence to know a trip was dropped - each row below carries
    `deleted_at` as stored and `bin_state` = `IN_RECYCLE_BIN`, derived from that same column
    rather than from the row's absence anywhere.

    This version is the whole bin: no pagination, no per-traveller scope, no filter parameters,
    no date window.  The order says to write that plainly instead of hinting at a filter, so it is
    written here and in the contract, and the list is ordered by `deleted_at` newest first because
    a bin screen reads most naturally that way - that is an ordering, not a subset.

    Read-only.  There is no permanent-delete door on this server (that would be a separate verb
    with its own confirmation), so nothing here may promise one, and the rows stay addressable at
    `GET /api/v1/journeys/{journey_id}` until 找回 clears the column.
    """

    items = session.scalars(
        select(Journey)
        .where(Journey.deleted_at.is_not(None))
        .order_by(Journey.deleted_at.desc(), Journey.id)
    ).all()
    return [recycle_bin_entry_schema(item) for item in items]


#: ORD-0444: the two reads the shelf doors share, each in exactly one SELECT.
#:
#: Both are helpers rather than inline queries because the create door and the list door have to
#: answer the same shelf the same way: a shelf that read its rows one way when the traveller made
#: it and another way when they listed it would give the screen two different numbers for one
#: container.
def _collection_shelves(session: Session, owner_traveler_id: str) -> list[CollectionFolder]:
    """Every shelf one traveller's id is written on, oldest first.

    Ordered by `created_at` with the id behind it so two shelves written in the same instant still
    come back in a stable order.  The scope is a column, not a convention: `owner_traveler_id` is
    `NOT NULL` on the row (`backend/db/schema.sql`), so there is no shelf this filter can silently
    miss, and 「我的收藏」 is now a claim the row itself carries rather than a name a screen hopes.
    There is still no pagination or sort parameter, and that absence is said here rather than left
    to be discovered by trying one.
    """

    return list(
        session.scalars(
            select(CollectionFolder)
            .where(CollectionFolder.owner_traveler_id == owner_traveler_id)
            .order_by(CollectionFolder.created_at, CollectionFolder.id)
        ).all()
    )


def _collection_rows_by_folder(
    session: Session,
    owner_traveler_id: str,
) -> dict[str, list[tuple[CollectionItem, Place]]]:
    """Every shelf row on every shelf one traveller owns, grouped by shelf, in two reads or fewer.

    The join is against `places` rather than a lookup per row, so listing ten shelves costs two
    statements and not eleven.  It is an INNER join, and that is load-bearing rather than
    incidental: `collection_items.place_id` references `places(id)` with no `ON DELETE` clause
    (`backend/db/schema.sql`), and `PRAGMA foreign_keys=ON` is set on every connection
    (`backend/app/db/session.py`), so a place a shelf points at cannot be deleted underneath
    it.  A shelf row therefore always has a place to resolve to, and the join cannot quietly drop
    a row and leave `item_count` short of what was asked for.

    The `collection_folders` leg is here so a scoped read never pulls another traveller's rows into
    memory to filter them out afterwards; it costs nothing on the statement count and it keeps
    "whose shelf" one fact rather than two.

    The order is the shelf's own: first put on, first read.  Nothing downstream re-sorts it, so the
    order the screen renders is the order the server chose.
    """

    grouped: dict[str, list[tuple[CollectionItem, Place]]] = {}
    for item, place in session.execute(
        select(CollectionItem, Place)
        .join(CollectionFolder, CollectionFolder.id == CollectionItem.folder_id)
        .join(Place, Place.id == CollectionItem.place_id)
        .where(CollectionFolder.owner_traveler_id == owner_traveler_id)
        .order_by(CollectionItem.added_at, CollectionItem.id)
    ).all():
        grouped.setdefault(item.folder_id, []).append((item, place))
    return grouped


def _collection_payloads(session: Session, owner_traveler_id: str) -> list[CollectionFolderSchema]:
    """Shelves and rows together, so no door can answer a shelf without its contents."""

    rows = _collection_rows_by_folder(session, owner_traveler_id)
    return [
        collection_folder_schema(folder, rows.get(folder.id, []))
        for folder in _collection_shelves(session, owner_traveler_id)
    ]


@app.post(
    "/api/v1/collections",
    response_model=CollectionFolderSchema,
    status_code=201,
    # The contract declares this refusal, and ORD-0395's rule in this tree is that a code the
    # decorator does not name never reaches `/openapi.json` at all -- so the served artifact and
    # `contracts/openapi.yaml` would disagree while the parity door, which compares paths and
    # methods, stayed green.  Declared here for that reason, and one arm now reads the served
    # document to keep the two from drifting again.
    responses={
        404: {
            "description": "error.reason_code = MEMBER_TRAVELER_UNKNOWN -- no traveller carries the "
            "asserted owner, so no shelf is stored for it"
        }
    },
)
def create_collection(payload: CollectionFolderCreate, session: Session = Depends(get_session)):
    """ORD-0444: make a named shelf, written to one traveller.  The 「收藏」 exit needs a container.

    A name and an owner go in; an id and two timestamps come out.  The name is stored verbatim,
    exactly as `POST /api/v1/travelers` stores `display_name` verbatim, and there is no rename door
    on this server yet -- so what a traveller types here is what the shelf reads for as long as it
    exists, which is why the length bound is the same one `CollectionFolderCreate` copies from that
    door.

    `owner_traveler_id` is asserted by the caller and validated against `travelers`, not
    authenticated: this is the same stance `POST /api/v1/journeys/{id}/members` takes on
    `invited_by` (`backend/app/main.py`), because the anonymous read is still the product's
    default.  What the column does establish is whose shelf a row is, so the scoped read below has
    something to scope on and 「我的收藏」 stops being a hope expressed by a screen.

    `created_at` and `updated_at` are written equal and `updated_at` is then *not* touched by
    adding or removing a row: it records when this shelf row was last written, and adding a place
    writes an `collection_items` row, not this one.  Leaving it alone is what keeps the column
    true; bumping it would put a second, contradicting account of the shelf next to `created_at`.

    The response is the shelf read back through the same helper `GET /api/v1/collections` uses, not
    an object assembled from the request.  An empty shelf is therefore reported as the server now
    holds it -- `item_count` 0 and `items` [] both come out of the read -- rather than from a
    literal someone typed because a new shelf is surely empty.
    """

    identifier = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    owner = payload.owner_traveler_id.strip()
    if not owner or session.get(Traveler, owner) is None:
        # The same words the membership door already uses for this fact, and the same name: an id
        # nothing carries is refused rather than stored and read back as an orphan shelf.
        raise _share_refusal(
            404, "No traveller carries that id.", "MEMBER_TRAVELER_UNKNOWN", {"field": "traveler_id"}
        )
    session.add(
        CollectionFolder(
            id=identifier,
            name=payload.name,
            owner_traveler_id=owner,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    record_audit(
        session,
        action="collection.folder.created",
        entity_type="CollectionFolder",
        entity_id=identifier,
        after={"name": payload.name, "owner_traveler_id": owner},
    )
    session.commit()
    return collection_folder_schema(
        session.get(CollectionFolder, identifier),
        _collection_rows_by_folder(session, owner).get(identifier, []),
    )


@app.get("/api/v1/collections", response_model=list[CollectionFolderSchema])
def list_collections(
    traveler_id: str = Query(...),
    session: Session = Depends(get_session),
):
    """ORD-0444: one traveller's shelves and every row on them, in one answer.

    One door for both halves on purpose.  A screen that had to ask per shelf to learn what was on
    it would render a count it could not tie to the rows under it -- and the count here is the
    length of the same `items` array the rows are drawn from
    (`collection_folder_schema`, `backend/app/services.py`), so the number a traveller reads and
    the list beneath it cannot describe two different reads.

    Empty is a real answer and is answered as one: a shelf with nothing on it comes back with
    `item_count` 0 and `items` [], and a traveller holding no shelf comes back as the empty list --
    not a 404.  Nothing in this payload is inferred from absence; the fields are read from rows.

    `traveler_id` is required, and that is the whole of the scope: a shelf is answered to the id
    written on it and to no one else.  It is a query parameter the way every other filtered read on
    this server asks for one (`city_code` at `backend/app/main.py` is the same shape), not a
    new channel.  What this version does *not* do is authenticate it -- see
    `create_collection`: the id is asserted by whoever calls, so 「我的收藏」 is a statement about
    which row the shelf belongs to, not a statement about who is allowed to look.
    """

    return _collection_payloads(session, traveler_id.strip())


@app.post(
    "/api/v1/collections/{folder_id}/items",
    response_model=CollectionItemSchema,
    status_code=201,
    # ORD-0444, following ORD-0395's rule at `backend/app/main.py`: the refusal is behaviour
    # this door exists to have, so the application's own served spec has to say it.  The hand
    # contract declared the 404 and the 409; without this block `/openapi.json` showed only
    # 201 and 422, and a consumer of the served document could not see that a shelf it does not
    # hold is an answer rather than a crash.  The descriptions name the three tokens the door can
    # emit, so nothing has to be guessed from the status code alone.
    responses={
        404: {
            "description": (
                "Refused with a named code in `error.reason_code`: COLLECTION_FOLDER_NOT_FOUND when"
                " this server holds no shelf with that id (including an id that is not uuid-shaped,"
                " since the path parameter is a plain string), PLACE_NOT_FOUND when the shelf exists"
                " but the place does not.  Nothing was written in either case."
            )
        },
        409: {
            "description": (
                "PLACE_ALREADY_IN_COLLECTION -- that place is already on that shelf.  `error.details`"
                " names the row that won (`existing_item_id`, `existing_added_at`), and the duplicate"
                " refusal holds across workers because `UNIQUE(folder_id, place_id)` is in the table."
            )
        },
    },
)
def add_collection_item(
    folder_id: str, payload: CollectionItemCreate, session: Session = Depends(get_session)
):
    """ORD-0444: put one place on one shelf.  The write the 「收藏」 exit of the chain needs.

    `folder_id` is a plain string, not a `UUID`, and that is the whole of the difference between a
    named refusal and a validation error on the counter-case.  A shelf id this server does not hold
    is answered `404 COLLECTION_FOLDER_NOT_FOUND`; a place id it does not hold is answered
    `404 PLACE_NOT_FOUND`.  Neither is allowed to become a 500, and neither is allowed to come back
    as the generic `resource.not_found` that `require()` answers with, because the two conditions
    need different sentences on a screen: 「这个收藏夹不在这台服务器上」 and 「这个地点不在这台
    服务器上」 are not the same failure, and the traveller did the second one by tapping a place
    that is no longer there.

    A duplicate is refused by name too.  `UNIQUE(folder_id, place_id)`
    (`backend/db/schema.sql`) is what makes that hold across two workers -- a select-then-insert
    check would let two taps of the same place both write, and the second shelf row would be a
    silent nothing that the count still reported.  The loser's `IntegrityError` is rolled back and
    re-read, so the 409 can name the row that is already there instead of the one this tap tried
    to make.

    The reference is stored, not the place's fields: `name`, `category`, `city_code` and the
    coordinate come back resolved from `places` on every read.  No plan, activity or day is touched
    -- that is the 「加进计划」 door's job, and the reason 收藏 is a separate exit at all.
    """

    folder = session.get(CollectionFolder, folder_id)
    if folder is None:
        refused = HTTPException(404, f"Collection folder {folder_id} was not found")
        refused.reason_code = "COLLECTION_FOLDER_NOT_FOUND"
        refused.error_details = {"folder_id": folder_id}
        raise refused
    place = session.get(Place, payload.place_id)
    if place is None:
        # ORD-0513: the sentence names the condition, not the identifier. The ids a technician needs
        # are already in `error.details` two lines below, and the reason token is this branch's machine
        # handle; a uuid inside the human line reaches the traveller twice and says nothing the token
        # does not already say.
        refused = HTTPException(
            404, "That place is not held by this server, so nothing was collected."
        )
        refused.reason_code = "PLACE_NOT_FOUND"
        refused.error_details = {"folder_id": folder_id, "place_id": payload.place_id}
        raise refused

    identifier = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        session.add(
            CollectionItem(
                id=identifier,
                folder_id=folder.id,
                place_id=place.id,
                note=payload.note,
                added_at=now,
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        already = session.execute(
            select(CollectionItem).where(
                CollectionItem.folder_id == folder.id,
                CollectionItem.place_id == place.id,
            )
        ).scalars().first()
        refused = HTTPException(
            409,
            "This place is already on that shelf, so nothing new was written.",
        )
        refused.reason_code = "PLACE_ALREADY_IN_COLLECTION"
        refused.error_details = {
            "folder_id": folder_id,
            "place_id": payload.place_id,
            "existing_item_id": already.id if already is not None else None,
            "existing_added_at": already.added_at if already is not None else None,
        }
        raise refused

    record_audit(
        session,
        action="collection.item.added",
        entity_type="CollectionItem",
        entity_id=identifier,
        after={"folder_id": folder.id, "place_id": place.id, "note": payload.note},
    )
    session.commit()

    stored = session.get(CollectionItem, identifier)
    return collection_item_schema(stored, session.get(Place, stored.place_id))


@app.get("/api/v1/journeys/{journey_id}")
def get_journey(journey_id: UUID, session: Session = Depends(get_session)):
    return journey_schema(require(session, Journey, str(journey_id), "Journey"))


# ORD-0496 slice 1: a trip's own schedule leaves this server as an RFC 5545 calendar.  This slice is
# the door and the contract; the screen entry that calls it is the second slice, deliberately not
# here, so the byte-level rules have one home and one set of arms before a button exists.


def _calendar_journey(session: Session, journey_id: str) -> Journey:
    """The trip, read through the door that already owns reading it -- or a named refusal.

    `get_journey` above is the trip's read authority and this door does not grow a second one: the
    lookup is `require(...)`, so the status and the sentence a traveller gets for "no such trip" are
    that door's.  What `require` cannot carry is `error.reason_code`, and this order asks every
    refusal here to be named, so the name is attached to the exception the read authority itself
    raised rather than rebuilt into a new 404 shape -- and it is the same `JOURNEY_NOT_FOUND`
    `delete_journey` and `undelete_journey` answer with, not a calendar-specific twin of it.

    The recycle bin is a refusal rather than an empty calendar, for the reason the footprint doors
    already state in `_checkin_journey_read`: `list_journeys` keeps its `deleted_at IS NULL` promise,
    so a trip this product refuses to list must not hand out its schedule either.  A binned trip is
    not gone -- 找回 puts it back -- which is why this is 409 and not 404.
    """

    try:
        journey = require(session, Journey, journey_id, "Journey")
    except HTTPException as refused:
        refused.reason_code = "JOURNEY_NOT_FOUND"
        refused.error_details = {"journey_id": journey_id}
        raise
    if journey.deleted_at is not None:
        raise _share_refusal(
            409,
            "This trip is in the recycle bin. 找回 it before exporting its calendar.",
            "CALENDAR_JOURNEY_IN_RECYCLE_BIN",
            {"journey_id": journey.id, "deleted_at": journey.deleted_at},
        )
    return journey


def _calendar_activities(session: Session, plan_version_id: str) -> list[CalendarActivity]:
    """The activity rows of one plan version, in the plan's own order, with nothing added.

    Order is `sequence_no` because that is the durable column -- `activities` carries
    `UNIQUE(plan_version_id, sequence_no)` -- and ordering by `start_at` would compare two ISO
    strings with different offsets as if they were one timeline.  A calendar client sorts by
    `DTSTART` itself; what this door promises is only that it does not reorder the plan's rows by
    guessing.

    Every field is a column value.  A `place_id` that names no row yields no `LOCATION` rather than
    a name this server made up, and a row whose `updated_at` is unusable offers `created_at` as its
    `DTSTAMP` candidate -- still the row's own moment, never the request's.
    """

    rows = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == plan_version_id)
        .order_by(Activity.sequence_no, Activity.id)
    ).all()
    names: dict[str, str | None] = {}
    for row in rows:
        if row.place_id and row.place_id not in names:
            place = session.get(Place, row.place_id)
            names[row.place_id] = place.name if place is not None else None
    return [
        CalendarActivity(
            uid=row.id,
            summary=row.title,
            location=names.get(row.place_id) if row.place_id else None,
            description="\n".join(
                (
                    f"sequence={row.sequence_no}",
                    f"type={row.activity_type}",
                    f"status={row.status}",
                    f"cost_minor={row.estimated_cost_minor} {row.currency}",
                )
            ),
            start_at=row.start_at,
            end_at=row.end_at,
            stamp_at=row.updated_at or row.created_at,
        )
        for row in rows
    ]


@app.get("/api/v1/journeys/{journey_id}/calendar.ics")
def get_journey_calendar(
    journey_id: UUID, session: Session = Depends(get_session)
) -> Response:
    """The trip's active plan as a `.ics` download, generated from the rows this server holds.

    Where the schedule comes from: `journeys.active_plan_version_id`, then that plan version's
    `activities` rows.  The same pointer every other plan door reads, so an export cannot show a
    schedule the product does not consider active.  Three refusals name the states in which there is
    no schedule to export -- no such row, in the recycle bin, and a pointer that does not resolve.
    The last one is `CALENDAR_SOURCE_UNREADABLE` carrying which of its three conditions it is,
    because "the trip has no plan", "the plan row is gone" and "that plan is another trip's" are
    three different facts and a screen has to say one of them rather than "nothing scheduled".

    What is *not* a refusal: a plan whose rows carry no usable clock.  Those activities are skipped
    and counted in `X-CityNexus-Ics-Skipped-No-Clock`, and the arithmetic in the headers is the
    proof that nothing went missing on the way -- `activities = events + skipped_no_clock +
    skipped_no_stamp`.  An empty calendar with `events=0` is the honest answer for a plan that holds
    no clocked activity, and the counts say so out loud.

    `Cache-Control: no-cache` follows the three streaming doors that already carry it, so a phone
    that re-exports after editing the trip revalidates instead of reopening a stale file.
    """

    journey = _calendar_journey(session, str(journey_id))
    plan_id = journey.active_plan_version_id
    if plan_id is None:
        raise _share_refusal(
            409,
            "This trip has no active plan, so there is no schedule here to export.",
            "CALENDAR_SOURCE_UNREADABLE",
            {"journey_id": journey.id, "reason": "ACTIVE_PLAN_NOT_DECLARED"},
        )
    plan = session.get(PlanVersion, plan_id)
    if plan is None:
        raise _share_refusal(
            409,
            "The active plan this trip points at is not on this server any more.",
            "CALENDAR_SOURCE_UNREADABLE",
            {"journey_id": journey.id, "reason": "ACTIVE_PLAN_ROW_ABSENT", "plan_version_id": plan_id},
        )
    if plan.journey_id != journey.id:
        raise _share_refusal(
            409,
            "The active plan this trip points at belongs to a different trip.",
            "CALENDAR_SOURCE_UNREADABLE",
            {
                "journey_id": journey.id,
                "reason": "ACTIVE_PLAN_BELONGS_TO_ANOTHER_JOURNEY",
                "plan_version_id": plan.id,
                "plan_journey_id": plan.journey_id,
            },
        )

    document = build_calendar(journey.title, _calendar_activities(session, plan.id))
    return Response(
        content=document.body,
        media_type=ICS_MEDIA_TYPE,
        headers={
            "Content-Disposition": content_disposition(journey.title, journey.id),
            "Cache-Control": "no-cache",
            "X-CityNexus-Ics-Events": str(document.events),
            "X-CityNexus-Ics-Skipped-No-Clock": str(document.skipped_no_clock),
            "X-CityNexus-Ics-Skipped-No-Stamps": str(document.skipped_no_stamp),
            "X-CityNexus-Ics-Cross-Day": str(document.cross_day_events),
            "X-CityNexus-Ics-Activities": str(document.activities),
            "X-CityNexus-Ics-Whitespace-Trimmed": str(document.trimmed_values),
        },
    )


# ORD-0476 slice 1: the trip footprint.  A stamp says the traveller was at one target of one
# journey; nothing here plans, moves or completes anything, which is what separates this door from
# the 完成 and 加进计划 ones.


def _checkin_target(payload: JourneyCheckinCreate) -> tuple[str, str]:
    """Which target this tap names, as the one immutable key the duplicate promise is keyed on.

    Exactly one of the two fields may be named, and both ways of getting that wrong are refused by
    name rather than guessed: a stamp naming nothing is a footprint of nothing, and one naming both
    would be two stamps arriving as one, after which neither duplicate rule could be stated.

    The key is written here, once, and is never re-derived when the stamp is read.  That is not
    tidiness -- either target column can go NULL underneath a live stamp (`ON DELETE SET NULL`, and
    the replan path at `backend/app/main.py` and `backend/app/operations/v9.py` is what
    triggers it), so a key derived from the columns would change or vanish after the fact and the
    重复打卡 promise would end up depending on rows that no longer exist.
    """

    activity_id = (payload.activity_id or "").strip()
    place_id = (payload.place_id or "").strip()
    if activity_id and place_id:
        raise _share_refusal(
            422,
            "One footprint names one target: an activity or a place, not both.",
            "CHECKIN_TARGET_AMBIGUOUS",
            {"activity_id": activity_id, "place_id": place_id},
        )
    if activity_id:
        return "ACTIVITY", "activity:" + activity_id
    if place_id:
        return "PLACE", "place:" + place_id
    raise _share_refusal(
        422,
        "A footprint has to name what was visited.",
        "CHECKIN_TARGET_REQUIRED",
        {"fields": ["activity_id", "place_id"]},
    )


def _checkin_journey_read(session: Session, journey_id: str) -> Journey:
    """One rule at both doors: a trip in the recycle bin is neither stampable nor stamp-readable.

    409 rather than 404 because the row exists and 找回 puts it back, which is the same reasoning
    `SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN` records at `backend/app/main.py`.  The bin is applied to
    the read door on purpose too: `GET /api/v1/journeys` keeps its `deleted_at IS NULL` promise, so a
    screen that could read a binned trip's footprints would be showing the history of a trip the
    product refuses to list -- and one rule at both doors is cheaper to state than two that differ.
    """

    # ORD-0500: this leg used to fall through to `require()`, which answers
    # "Journey <uuid> was not found" -- an id printed into a traveller's sentence, with
    # `details: null` and no `reason_code`, at both footprint doors.  The door has a name for
    # this case, so it says it, and the identity stays out of the prose.
    journey = session.get(Journey, str(journey_id))
    if journey is None:
        raise _share_refusal(
            404,
            "This server has no such trip, so nothing was stamped or read.",
            "CHECKIN_JOURNEY_UNKNOWN",
            {"field": "journey_id"},
        )
    if journey.deleted_at is not None:
        raise _share_refusal(
            409,
            "This trip is in the recycle bin. 找回 it before stamping or reading its footprints.",
            "CHECKIN_JOURNEY_IN_RECYCLE_BIN",
            {"journey_id": journey.id, "deleted_at": journey.deleted_at},
        )
    return journey


def _checkin_resolve_targets(
    session: Session, journey: Journey, kind: str, target_key: str
) -> tuple[str | None, str | None]:
    """The target has to exist, and an activity has to belong to this journey.

    Two refusals on the activity leg rather than one, because they are two different facts and the
    traveller hears a different sentence for each: a row that does not exist at all
    (`CHECKIN_ACTIVITY_UNKNOWN`) and a row that exists under somebody else's plan version
    (`CHECKIN_ACTIVITY_NOT_IN_JOURNEY`).  Collapsing them would answer "not yours" about a row that
    was never here, which is the kind of claim this project has had to take back before.

    The link is walked through `plan_versions` because that is where it lives: `activities` carries
    `plan_version_id`, not `journey_id` (`backend/app/db/models.py` -- `Activity`).
    """

    identifier = target_key.split(":", 1)[1]
    if kind == "ACTIVITY":
        activity = session.get(Activity, identifier)
        if activity is None:
            raise _share_refusal(
                404,
                "No activity on this server carries that id.",
                "CHECKIN_ACTIVITY_UNKNOWN",
                {"activity_id": identifier},
            )
        plan = session.get(PlanVersion, activity.plan_version_id)
        if plan is None or plan.journey_id != journey.id:
            raise _share_refusal(
                409,
                "That activity belongs to a different trip's plan, so it cannot be stamped here.",
                "CHECKIN_ACTIVITY_NOT_IN_JOURNEY",
                {
                    "activity_id": identifier,
                    "plan_version_id": activity.plan_version_id,
                    "plan_journey_id": plan.journey_id if plan is not None else None,
                    "asked_journey_id": journey.id,
                },
            )
        return identifier, None
    place = session.get(Place, identifier)
    if place is None:
        raise _share_refusal(
            404,
            "No place on this server carries that id.",
            "CHECKIN_PLACE_UNKNOWN",
            {"place_id": identifier},
        )
    return None, identifier


@app.post(
    "/api/v1/journeys/{journey_id}/checkins",
    response_model=JourneyCheckinSchema,
    status_code=201,
    responses={
        404: {
            "description": "error.reason_code = CHECKIN_JOURNEY_UNKNOWN / "
            "CHECKIN_ACTIVITY_UNKNOWN / CHECKIN_PLACE_UNKNOWN"
        },
        409: {
            "description": "error.reason_code = CHECKIN_JOURNEY_IN_RECYCLE_BIN / "
            "CHECKIN_ACTIVITY_NOT_IN_JOURNEY / CHECKIN_DUPLICATE_STAMP"
        },
    },
)
def create_journey_checkin(
    journey_id: UUID,
    payload: JourneyCheckinCreate,
    session: Session = Depends(get_session),
):
    """ORD-0476: stamp one target of this journey, and answer the stamp the store now holds.

    `stamped_at` is the server's instant, not the caller's: the request shape carries no time field
    at all, so a device clock cannot arrive dressed as a store fact.  An offline queue that carries
    its own moment is a named input with an offset requirement, not this door widening its own.

    A replayed tap is not a second success.  The door looks its own key up first -- before asking
    whether the target row still exists, for the reason recorded below -- and answers
    `CHECKIN_DUPLICATE_STAMP` naming the row that already won (its id and its moment), and the
    `UNIQUE (journey_id, target_key)` index in `backend/db/schema.sql` is the durable half of that
    sentence -- two workers taking two taps at the same target cannot both win, and the loser is
    converted into the same named refusal instead of a 500.  There is no idempotency key here because
    the target already is one.

    Nothing else is written: no activity is completed, no plan version touched, no day moved.  That is
    what makes this the 打卡 door rather than a second copy of 完成.
    """

    journey = _checkin_journey_read(session, str(journey_id))
    kind, target_key = _checkin_target(payload)

    # The duplicate answer comes first, before the target row is looked up, and that order is a fact
    # about which row is being spoken of rather than a preference.  Whether this trip already holds a
    # stamp for `target_key` is a statement about *this* table, and it stays true after the target is
    # deleted (`ON DELETE SET NULL`); asking the other table first would tell a traveller who re-taps
    # a superseded stop that the stop never existed -- a claim about a row that has nothing to do with
    # the footprint already on file, and one that would vanish as soon as the catalog moved on.
    existing = session.scalars(
        select(JourneyCheckin).where(
            JourneyCheckin.journey_id == journey.id,
            JourneyCheckin.target_key == target_key,
        )
    ).first()
    if existing is not None:
        raise _share_refusal(
            409,
            "That target is already stamped on this trip.",
            "CHECKIN_DUPLICATE_STAMP",
            {
                "target_key": target_key,
                "existing_checkin_id": existing.id,
                "existing_stamped_at": existing.stamped_at,
            },
        )

    activity_id, place_id = _checkin_resolve_targets(session, journey, kind, target_key)
    identifier = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        session.add(
            JourneyCheckin(
                id=identifier,
                journey_id=journey.id,
                activity_id=activity_id,
                place_id=place_id,
                target_kind=kind,
                target_key=target_key,
                stamped_at=now,
                note=payload.note if payload.note is not None else None,
                source="FOREGROUND_WEB",
            )
        )
        session.commit()
    except IntegrityError as exc:
        # The index caught a race the read above could not see: the same fact, said the same way.
        session.rollback()
        raced = session.scalars(
            select(JourneyCheckin).where(
                JourneyCheckin.journey_id == journey.id,
                JourneyCheckin.target_key == target_key,
            )
        ).first()
        raise _share_refusal(
            409,
            "That target is already stamped on this trip.",
            "CHECKIN_DUPLICATE_STAMP",
            {
                "target_key": target_key,
                "existing_checkin_id": raced.id if raced is not None else None,
                "existing_stamped_at": raced.stamped_at if raced is not None else None,
                "caught_by": "unique_index",
            },
        ) from exc

    record_audit(
        session,
        action="journey.checkin.stamped",
        entity_type="JourneyCheckin",
        entity_id=identifier,
        after={"journey_id": journey.id, "target_key": target_key, "stamped_at": now},
    )
    session.commit()
    stored = session.get(JourneyCheckin, identifier)
    return checkin_schema(
        stored,
        session.get(Activity, stored.activity_id) if stored.activity_id else None,
        session.get(Place, stored.place_id) if stored.place_id else None,
    )


@app.get(
    "/api/v1/journeys/{journey_id}/checkins",
    response_model=list[JourneyCheckinSchema],
    responses={
        409: {"description": "error.reason_code = CHECKIN_JOURNEY_IN_RECYCLE_BIN"}
    },
)
def list_journey_checkins(journey_id: UUID, session: Session = Depends(get_session)):
    """ORD-0476: this trip's footprints, oldest stamp first, each with its target read back.

    Two statements whatever the count, not one per stamp: the activity titles and place names come
    back in `in_()` batches and are matched in memory.  A screen that had to ask per stamp could not
    tie what it renders to one read, and a stamp whose target has since been deleted resolves to
    nulls that mean "the row is gone" -- never to a title invented to fill the cell.

    A trip with no stamps answers `[]`.  Absence is a list, not a 404: the journey resolved and there
    is nothing on it yet, which is a different fact from the trip not existing.
    """

    journey = _checkin_journey_read(session, str(journey_id))
    rows = session.scalars(
        select(JourneyCheckin)
        .where(JourneyCheckin.journey_id == journey.id)
        .order_by(JourneyCheckin.stamped_at, JourneyCheckin.id)
    ).all()
    activity_ids = [r.activity_id for r in rows if r.activity_id]
    place_ids = [r.place_id for r in rows if r.place_id]
    activities = (
        {a.id: a for a in session.scalars(select(Activity).where(Activity.id.in_(activity_ids))).all()}
        if activity_ids
        else {}
    )
    places = (
        {p.id: p for p in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()}
        if place_ids
        else {}
    )
    return [
        checkin_schema(
            r,
            activities.get(r.activity_id) if r.activity_id else None,
            places.get(r.place_id) if r.place_id else None,
        )
        for r in rows
    ]


# ORD-0434: a share grant is a server fact instead of a URL the client assembles
# (`frontend/src/lib/journeyLink.ts`).  The anonymous read above is deliberately untouched - the
# phone demo chain and every existing e2e walk it, and tightening what it accepts is a daylight
# decision.  `PRIVATE` refuses every token read; `INVITEES_ONLY` asks the reader to name a member
# and checks that name against `journey_members`, and a read that names nobody is refused rather
# than waved through.
def _share_refusal(status: int, message: str, reason_code: str, details: dict) -> HTTPException:
    """One shape for every grant refusal: the status picks the code, the name rides in reason_code."""
    refused = HTTPException(status, message)
    refused.reason_code = reason_code
    refused.error_details = details
    return refused


def _expiry_verdict(expires_at: str | None, now: datetime) -> str:
    """One comparison, in one place, for both doors that care about an expiry.

    `PAST` includes the instant that equals `now`: the read door has always treated `expires <= now` as
    spent, so a write that accepted equality would mint a token that is dead in the same breath.  The
    two doors must not hold two definitions of "already over".
    """
    if not expires_at:
        return "NO_EXPIRY"
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return "UNREADABLE"
    if expires.tzinfo is None:
        return "UNREADABLE"
    return "PAST" if expires <= now else "FUTURE"


def _share_grant_state(grant: JourneyShareGrant, now: datetime) -> str:
    """One place decides what this token may do right now, and it fails closed."""
    if grant.revoked_at:
        return "REVOKED"
    verdict = _expiry_verdict(grant.expires_at, now)
    if verdict == "UNREADABLE":
        return "EXPIRY_UNREADABLE"
    if verdict == "PAST":
        return "EXPIRED"
    return "READABLE"


def _member_is_in_force(session: Session, journey_id: str, traveler_id: str, now: datetime) -> bool:
    # A membership counts when it was not withdrawn and its own deadline has not passed.
    member = session.scalars(
        select(JourneyMemberModel).where(
            JourneyMemberModel.journey_id == journey_id,
            JourneyMemberModel.traveler_id == traveler_id,
        )
    ).first()
    if member is None or member.removed_at:
        return False
    if member.expires_at:
        try:
            expires = datetime.fromisoformat(member.expires_at)
        except ValueError:
            return False
        if expires.tzinfo is None or expires <= now:
            return False
    return True


def _share_grant_payload(grant: JourneyShareGrant) -> ShareGrant:
    return ShareGrant(
        id=grant.id,
        journey_id=grant.journey_id,
        visibility=grant.visibility,
        created_by=grant.created_by,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        created_at=grant.created_at,
        updated_at=grant.updated_at,
        plan_version_id=grant.plan_version_id,
        token=grant.token,
    )


@app.post("/api/v1/journeys/{journey_id}/share-grants", status_code=201, response_model=ShareGrant)
def create_share_grant(
    journey_id: UUID, payload: ShareGrantCreate, session: Session = Depends(get_session)
):
    require(session, Journey, str(journey_id), "Journey")
    creator = payload.created_by.strip()
    if not creator:
        raise _share_refusal(
            422,
            "A share grant needs the person or device that created it.",
            "SHARE_GRANT_CREATOR_EMPTY",
            {"field": "created_by"},
        )
    expires_at: str | None = None
    if payload.expires_at is not None:
        raw = payload.expires_at.strip()
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise _share_refusal(
                422,
                "The expiry sent cannot be read as an instant.",
                "SHARE_GRANT_EXPIRY_UNREADABLE",
                {"field": "expires_at"},
            )
        if parsed.tzinfo is None:
            raise _share_refusal(
                422,
                "An expiry without an offset would be a guess about whose clock it is.",
                "SHARE_GRANT_EXPIRY_NO_OFFSET",
                {"field": "expires_at"},
            )
        expires_at = parsed.astimezone(timezone.utc).isoformat()
        # ORD-0460: an expiry already in the past is refused here rather than minted.  The answer this
        # door used to give was 201 plus a token, and the very next read of that token answered 409
        # SHARE_GRANT_EXPIRED -- a grant born dead, with a live string in the caller's clipboard.  No
        # token is generated on this path at all, so nothing can outlive the refusal.
        if _expiry_verdict(expires_at, datetime.now(timezone.utc)) == "PAST":
            raise _share_refusal(
                422,
                "That expiry is already in the past, so no token would work.",
                "SHARE_GRANT_EXPIRY_IN_PAST",
                {"field": "expires_at", "expires_at": expires_at},
            )
    # ORD-0466: a grant may be cut for one plan version.  Three named answers, because "the id is
    # malformed", "there is no such version" and "that version is somebody else's journey" are three
    # different things for the caller to say back to a traveller.
    plan_version_id: str | None = None
    if payload.plan_version_id is not None:
        try:
            version_uuid = str(UUID(payload.plan_version_id.strip()))
        except ValueError:
            raise _share_refusal(
                422,
                "The plan version sent cannot be read as an id.",
                "SHARE_GRANT_VERSION_ID_UNREADABLE",
                {"field": "plan_version_id"},
            ) from None
        plan_row = session.get(PlanVersion, version_uuid)
        if plan_row is None:
            raise _share_refusal(
                422,
                "No plan version on this server carries that id.",
                "SHARE_GRANT_VERSION_UNKNOWN",
                {"field": "plan_version_id", "plan_version_id": version_uuid},
            )
        if plan_row.journey_id != str(journey_id):
            raise _share_refusal(
                409,
                "That plan version belongs to a different journey, so this door will not open it.",
                "SHARE_GRANT_VERSION_NOT_IN_JOURNEY",
                {
                    "field": "plan_version_id",
                    "plan_version_id": version_uuid,
                    "journey_id": str(journey_id),
                },
            )
        plan_version_id = plan_row.id
    # ORD-0541: 重复发起同一枚分享不再长第二行。可复用的档只有一个——那一行的 `visibility` 恰为
    # `LINK_READ`（号令者的裁语），其它档每一次请求照旧是新链接；复用与否读的是这一趟自己从库里取回的
    # 行，不是屏上或调用方存下的说法。已撤销的行不可复用；过期行只有还没到点的才可复用，因为过期的那一枚
    # 正是 ORD-0460 拒绝 mint 的东西，复用它等于把出生即死的链接再递出去一次。
    if payload.visibility == "LINK_READ":
        version_match = (
            JourneyShareGrant.plan_version_id.is_(None)
            if plan_version_id is None
            else JourneyShareGrant.plan_version_id == plan_version_id
        )
        held = session.scalars(
            select(JourneyShareGrant)
            .where(
                JourneyShareGrant.journey_id == str(journey_id),
                JourneyShareGrant.visibility == "LINK_READ",
                JourneyShareGrant.created_by == creator,
                JourneyShareGrant.revoked_at.is_(None),
                version_match,
            )
            .order_by(JourneyShareGrant.created_at, JourneyShareGrant.id)
        ).all()
        now = datetime.now(timezone.utc)
        for row in held:
            if _expiry_verdict(row.expires_at, now) in ("NO_EXPIRY", "FUTURE"):
                return _share_grant_payload(row)
    stamp = datetime.now(timezone.utc).isoformat()
    grant = JourneyShareGrant(
        id=str(uuid4()),
        journey_id=str(journey_id),
        token=secrets.token_urlsafe(24),
        visibility=payload.visibility,
        plan_version_id=plan_version_id,
        created_by=creator,
        expires_at=expires_at,
        revoked_at=None,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(grant)
    session.commit()
    return _share_grant_payload(grant)


def _recycle_bin_in_force_seat(
    session: Session, journey_id: str, traveler_id: str, now: datetime
) -> JourneyMemberModel | None:
    """The seat this traveller actually holds on one trip right now, if any (ORD-0531).

    Read through `_member_state` rather than a fresh `removed_at is None` test, so a withdrawn or
    lapsed seat stops counting here at the same moment it stops counting at the member doors.
    """

    for member in session.scalars(
        select(JourneyMemberModel).where(
            JourneyMemberModel.journey_id == journey_id,
            JourneyMemberModel.traveler_id == traveler_id,
        )
    ).all():
        if _member_state(member, now) == "MEMBER_IN_FORCE":
            return member
    return None


def _traveler_bin_row(
    session: Session, journey: Journey, traveler_id: str, now: datetime
) -> TravelerRecycleBinJourney | None:
    """One bin row said from one seat: whose trip it is, and whether this traveller may 找回 it.

    Returning `None` means the row is not this traveller's to see at all, which is the other half of
    the isolation: a trip neither owned by them nor seated to them does not appear with a refusal,
    because a refusal would still be somebody else's discarded trip.
    """

    base = recycle_bin_entry_schema(journey).model_dump()
    if journey.traveler_id == traveler_id:
        return TravelerRecycleBinJourney(
            **base,
            ownership="OWNED_BY_THIS_TRAVELER",
            member_role=None,
            recovery="RECOVERY_ALLOWED",
        )
    seat = _recycle_bin_in_force_seat(session, journey.id, traveler_id, now)
    if seat is None:
        return None
    may_restore = "JOURNEY_UPDATE" in _role_capabilities(session, seat.role)
    return TravelerRecycleBinJourney(
        **base,
        ownership="OWNED_BY_ANOTHER_TRAVELER",
        member_role=seat.role,
        recovery=(
            "RECOVERY_ALLOWED" if may_restore else "RECOVERY_REFUSED_ROLE_CANNOT_RESTORE"
        ),
    )


@app.get(
    "/api/v1/travelers/{traveler_id}/recycle-bin/journeys",
    response_model=list[TravelerRecycleBinJourney],
)
def list_traveler_recycle_bin_journeys(traveler_id: UUID, session: Session = Depends(get_session)):
    """ORD-0434 step 2 scoped to one traveller, and since ORD-0531 isolated by 归属 and 共享态.

    This is its own door rather than a parameter on `GET /api/v1/recycle-bin/journeys` because that
    one carries a tested promise to advertise no filter, no scope and no page - a promise this order
    has no business bending, and it is untouched.  Naming an owner in a path is not proof of who is
    asking; binding the caller to the name is the daylight decision step 2 does not make, and nothing
    here pretends that binding has happened.

    What changed with ORD-0531 is what a row is allowed to say and who may see it.  The set is now
    "the trips in the bin that this traveller owns, plus the ones an in-force seat reaches", and a
    trip belonging to somebody else with no seat to this traveller is not returned at all.  Ordering
    stays `deleted_at` newest-first, so the two doors still cannot disagree about a row they both
    hold; the two names (`ownership`, `recovery`) are what this door adds, and `recovery` is asked of
    `journey_role_capabilities` rather than decided from a role list written into this function.
    """
    now = datetime.now(timezone.utc)
    viewer = str(traveler_id)
    seated_on = select(JourneyMemberModel.journey_id).where(
        JourneyMemberModel.traveler_id == viewer
    )
    items = session.scalars(
        select(Journey)
        .where(
            Journey.deleted_at.is_not(None),
            or_(Journey.traveler_id == viewer, Journey.id.in_(seated_on)),
        )
        .order_by(Journey.deleted_at.desc(), Journey.id)
    ).all()
    rows = [_traveler_bin_row(session, item, viewer, now) for item in items]
    return [row for row in rows if row is not None]


@app.get("/api/v1/journeys/{journey_id}/share-grants", response_model=list[ShareGrant])
def list_share_grants(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(JourneyShareGrant)
        .where(JourneyShareGrant.journey_id == str(journey_id))
        .order_by(JourneyShareGrant.created_at, JourneyShareGrant.id)
    ).all()
    # Revoked grants stay in the list: an owner must be able to see that a link was withdrawn,
    # and a silently filtered row is indistinguishable from one that never existed.
    return [_share_grant_payload(row) for row in rows]


@app.post(
    "/api/v1/journeys/{journey_id}/share-grants/{grant_id}/revoke",
    response_model=ShareGrantRevoked,
)
def revoke_share_grant(journey_id: UUID, grant_id: str, session: Session = Depends(get_session)):
    grant = session.get(JourneyShareGrant, grant_id)
    if grant is None or grant.journey_id != str(journey_id):
        raise _share_refusal(
            404,
            "No share grant of this journey carries that id.",
            "SHARE_GRANT_UNKNOWN",
            {"field": "grant_id", "journey_id": str(journey_id)},
        )
    if grant.revoked_at:
        raise _share_refusal(
            409,
            "This grant was already withdrawn; withdrawing it again is not a new fact.",
            "SHARE_GRANT_ALREADY_REVOKED",
            {"revoked_at": grant.revoked_at, "grant_id": grant.id},
        )
    stamp = datetime.now(timezone.utc).isoformat()
    grant.revoked_at = stamp
    grant.updated_at = stamp
    session.commit()
    return ShareGrantRevoked(
        id=grant.id,
        journey_id=grant.journey_id,
        visibility=grant.visibility,
        revoked_at=stamp,
        already_revoked=False,
    )


def _shared_plan_projection(
    session: Session, journey: Journey, scoped_plan: PlanVersion | None = None
) -> tuple[str, SharedPlan | None]:
    """The plan this grant covers, as a share holder sees it: stops, their names, and where they are.

    `scoped_plan` is the version an ORD-0466 grant was cut for.  When the caller has one, the guest
    reads THAT version and not the journey's currently-active one -- a screen that listed the active
    plan behind a version-scoped link would show a traveller a set of days the owner deliberately did
    not hand over, and would say nothing while doing it.

    Answers `(plan_state, plan)`, and the state is named rather than left to be inferred from a null.
    Three cases are genuinely different facts and a screen cannot tell them apart from `None` alone:
    this journey never had a plan; it points at a plan row that is not there; it has one.
    `_active_plan_payload` (`app/stay_mobility.py`) is the owner's projection and carries the
    owner's live position, their commitments and their disruption events -- none of which a guest
    asked for, and the first of which is a person's location.

    Read-only: one ordered activity query and one batched place query, the same two reads the owner's
    projection makes.  Nothing here writes, and nothing here calls a provider.
    """
    if scoped_plan is not None:
        return "PLAN_PRESENT", _shared_plan_of(session, scoped_plan)
    if not journey.active_plan_version_id:
        return "NO_PLAN", None
    plan = session.get(PlanVersion, str(journey.active_plan_version_id))
    if plan is None:
        return "PLAN_ROW_MISSING", None
    return "PLAN_PRESENT", _shared_plan_of(session, plan)


def _shared_plan_of(session: Session, plan: PlanVersion) -> SharedPlan:
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    place_ids = sorted({str(item.place_id) for item in activities if item.place_id})
    places: dict[str, Place] = {}
    if place_ids:
        places = {
            place.id: place
            for place in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()
        }
    stops = [
        SharedStop(
            id=item.id,
            sequence_no=item.sequence_no,
            title=item.title,
            place_name=places[str(item.place_id)].name if item.place_id and str(item.place_id) in places else None,
            activity_type=item.activity_type,
            start_at=parse_dt(item.start_at),
            end_at=parse_dt(item.end_at),
            coordinate=SharedActivityCoordinate(
                **activity_coordinate(item.place_id, places)
            ),
        )
        for item in activities
    ]
    return SharedPlan(
        id=plan.id, version_no=plan.version_no, source=plan.source, stops=stops
    )


@app.get("/api/v1/shared/journeys/{token}")
def read_shared_journey(
    token: str,
    member: str | None = None,
    session: Session = Depends(get_session),
):
    """The door a grant token is actually presented at, so expiry and revocation can mean something.

    The body is the accepted `journey_schema` shape plus what a landing screen cannot derive for
    itself: `plan_state` / `active_plan` (ORD-0465, the guest's view of the days this grant covers)
    and `grant_scope` / `shared_plan` (ORD-0466, which version the grant was cut for).  The journey
    half deliberately reuses the one shape the anonymous door returns -- one journey form, not a
    second one that can drift -- and `shared_plan` is present ONLY on a version-scoped grant, because
    a key that is always there with a null in it cannot carry the meaning of "this grant never
    covered a version" (pinned by `tests/test_qoder_ord0466_version_share.py::test_v1_...`).

    The stops come out of this door and no other: every alternative is either a door that writes
    while it answers (`/journeys/{id}/experience`) or one that never heard of a grant, and a screen
    assembled from those would keep working after the deadline passed and after the owner withdrew
    the link.
    """
    grant = session.scalars(
        select(JourneyShareGrant).where(JourneyShareGrant.token == token)
    ).first()
    if grant is None:
        # "never existed" answers 404; a grant that stopped working answers 409.  Two different facts
        # cannot share one number, or a client collapses them into the same sentence.
        raise _share_refusal(
            404, "No share grant carries that token.", "SHARE_GRANT_UNKNOWN", {"field": "token"}
        )
    moment = datetime.now(timezone.utc)
    if grant.visibility == "INVITEES_ONLY" and not member:
        raise _share_refusal(
            409,
            "This grant is for invited members, and the read named nobody.",
            "SHARE_GRANT_MEMBERSHIP_REQUIRED",
            {"grant_id": grant.id, "visibility": grant.visibility},
        )
    if member and grant.visibility == "INVITEES_ONLY":
        if not _member_is_in_force(session, grant.journey_id, member, moment):
            raise _share_refusal(
                409,
                "The member named is not in force on this journey.",
                "SHARE_GRANT_NOT_A_MEMBER",
                {"grant_id": grant.id, "member_field": "member"},
            )
    if grant.visibility == "PRIVATE":
        raise _share_refusal(
            409,
            "A private grant answers no token read.",
            "SHARE_GRANT_NOT_LINK_READABLE",
            {"grant_id": grant.id, "visibility": grant.visibility},
        )
    state = _share_grant_state(grant, moment)
    if state != "READABLE":
        details: dict[str, str] = {"grant_id": grant.id, "state": state}
        if state == "REVOKED":
            details["revoked_at"] = str(grant.revoked_at)
        elif state in ("EXPIRED", "EXPIRY_UNREADABLE"):
            details["expires_at"] = str(grant.expires_at)
        elif state == "NOT_LINK_READABLE":
            details["visibility"] = grant.visibility
        raise _share_refusal(
            409,
            "This share grant does not allow a read right now.",
            f"SHARE_GRANT_{state}",
            details,
        )
    journey = session.get(Journey, grant.journey_id)
    if journey is None:
        raise _share_refusal(
            404,
            "The journey behind this grant is no longer readable.",
            "SHARE_GRANT_JOURNEY_GONE",
            {"grant_id": grant.id},
        )
    # ORD-0465 ④: the owner put this trip in the recycle bin, and a grant does not outlive that.
    #
    # Before this branch the door answered 200 with the whole trip, because `journey_schema` carries no
    # `deleted_at` (`backend/app/schemas.py`) and this door never looked at the column -- so the
    # recipient of a share link read a trip its owner had thrown away, and no word on that screen could
    # have been true. 409 and not 404: the row is there and the owner can 找回 it, so "no longer
    # readable" would be a different fact; and the name says which of the two the traveller is being
    # told, because `SHARE_GRANT_JOURNEY_GONE` already owns the other one.
    #
    # This is the bin's state, not the grant's, so it is decided after every grant check above: an
    # expired link is expired whether or not the trip was binned, and the reader hears the older fact.
    if journey.deleted_at is not None:
        raise _share_refusal(
            409,
            "The journey behind this grant is in the recycle bin.",
            "SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN",
            {"grant_id": grant.id, "deleted_at": journey.deleted_at},
        )
    body = journey_schema(journey).model_dump(mode="json")
    # ORD-0466: a grant cut for one version reads that version and nothing else.  The scope is said
    # in the body rather than left for the caller to infer from which keys happen to be present, and
    # the plan itself is the same `plan_schema` every other plan door returns -- one shape, not a
    # second one that can drift away from the owner's own view of the version.
    scoped_plan: PlanVersion | None = None
    if grant.plan_version_id:
        scoped_plan = session.get(PlanVersion, grant.plan_version_id)
        if scoped_plan is None or scoped_plan.journey_id != journey.id:
            raise _share_refusal(
                409,
                "The plan version this grant was cut for is no longer readable.",
                "SHARE_GRANT_VERSION_GONE",
                {"grant_id": grant.id, "plan_version_id": grant.plan_version_id},
            )
        body["grant_scope"] = "PLAN_VERSION"
        body["shared_plan"] = plan_schema(session, scoped_plan)
    else:
        body["grant_scope"] = "JOURNEY"
    # ORD-0465: the guest's view of the days -- of the version this grant covers, which is why it is
    # computed after the scope is settled rather than from the journey's active pointer alone.  The
    # state is named rather than left to be inferred from `active_plan` being null, because "never
    # planned" and "planned, and the row is gone" are two facts.  See `_shared_plan_projection` for
    # what is deliberately not in it.
    body["plan_state"], guest_plan = _shared_plan_projection(session, journey, scoped_plan)
    body["active_plan"] = guest_plan.model_dump(mode="json") if guest_plan is not None else None
    return body



# ORD-0434 step 2: membership is one set per journey, and what a role may do is a table the server
# holds.  Shape borrowed from RELATION/relation_and_copy_findings.md (a role answers "what may
# this person do" in one sentence) and :133 (an invitation has a lifetime).  The member-state words
# are prefixed because bare EXPIRED and WITHDRAWN already mean something to accommodation offers
# (`app/stay_mobility.py`).
def _member_state(member: JourneyMemberModel, now: datetime) -> str:
    if member.removed_at:
        return "MEMBER_WITHDRAWN"
    if member.expires_at:
        try:
            expires = datetime.fromisoformat(member.expires_at)
        except ValueError:
            return "MEMBER_LAPSED"
        if expires.tzinfo is None or expires <= now:
            return "MEMBER_LAPSED"
    return "MEMBER_IN_FORCE"


def _member_payload(member: JourneyMemberModel) -> JourneyMember:
    return JourneyMember(
        id=member.id,
        journey_id=member.journey_id,
        traveler_id=member.traveler_id,
        role=member.role,
        invited_by=member.invited_by,
        expires_at=member.expires_at,
        joined_at=member.joined_at,
        removed_at=member.removed_at,
        created_at=member.created_at,
        updated_at=member.updated_at,
        member_state=_member_state(member, datetime.now(timezone.utc)),
    )


def _member_expiry(raw: str | None) -> str | None:
    """Same rule as a grant: an instant with no offset is a guess about whose clock, so refuse it."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _share_refusal(
            422, "The expiry sent cannot be read as an instant.",
            "MEMBER_EXPIRY_UNREADABLE", {"field": "expires_at"},
        )
    if parsed.tzinfo is None:
        raise _share_refusal(
            422, "An expiry without an offset would be a guess about whose clock it is.",
            "MEMBER_EXPIRY_NO_OFFSET", {"field": "expires_at"},
        )
    normalized = parsed.astimezone(timezone.utc).isoformat()
    # ORD-0461 r3: a membership whose deadline this server would already read as spent is refused at
    # the write door, by the one comparison the read path uses (`_expiry_verdict`,
    # backend/app/main.py) and exactly as the grant door refuses it at
    # backend/app/main.py.  Accepting it stored a row that `_member_state` called
    # `MEMBER_LAPSED` in the same breath -- a membership born dead.  `PAST` includes the instant equal
    # to now, because that is what the read door has always meant by spent; the two doors cannot hold
    # two definitions.
    if _expiry_verdict(normalized, datetime.now(timezone.utc)) == "PAST":
        raise _share_refusal(
            422,
            "That expiry is already in the past, so the membership would start out lapsed.",
            "MEMBER_EXPIRY_IN_PAST",
            {"field": "expires_at", "expires_at": normalized},
        )
    return normalized


@app.post("/api/v1/journeys/{journey_id}/members", status_code=201, response_model=JourneyMember)
def add_journey_member(
    journey_id: UUID, payload: JourneyMemberCreate, session: Session = Depends(get_session)
):
    require(session, Journey, str(journey_id), "Journey")
    inviter = payload.invited_by.strip()
    if not inviter:
        raise _share_refusal(
            422, "A membership needs the person or device that granted it.",
            "MEMBER_INVITER_EMPTY", {"field": "invited_by"},
        )
    traveler_id = str(payload.traveler_id)
    if session.get(Traveler, traveler_id) is None:
        raise _share_refusal(
            404, "No traveller carries that id.", "MEMBER_TRAVELER_UNKNOWN", {"field": "traveler_id"}
        )
    expires_at = _member_expiry(payload.expires_at)
    existing = session.scalars(
        select(JourneyMemberModel).where(
            JourneyMemberModel.journey_id == str(journey_id),
            JourneyMemberModel.traveler_id == traveler_id,
        )
    ).first()
    if existing is not None:
        # One set means one row: a second invitation to the same traveller is the same fact twice,
        # so it is refused by name rather than quietly adding a duplicate the list would show twice.
        raise _share_refusal(
            409,
            "This traveller is already in this journey's member set.",
            "MEMBER_ALREADY_PRESENT",
            {"member_id": existing.id, "member_state": _member_state(existing, datetime.now(timezone.utc))},
        )
    stamp = datetime.now(timezone.utc).isoformat()
    member = JourneyMemberModel(
        id=str(uuid4()),
        journey_id=str(journey_id),
        traveler_id=traveler_id,
        role=payload.role,
        invited_by=inviter,
        expires_at=expires_at,
        joined_at=stamp,
        removed_at=None,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(member)
    session.commit()
    return _member_payload(member)


@app.get("/api/v1/journeys/{journey_id}/members", response_model=list[JourneyMember])
def list_journey_members(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(JourneyMemberModel)
        .where(JourneyMemberModel.journey_id == str(journey_id))
        .order_by(JourneyMemberModel.created_at, JourneyMemberModel.id)
    ).all()
    # Withdrawn rows stay listed with their own state: a screen has to be able to say "this person
    # was here and is out" without guessing from an absence.
    return [_member_payload(row) for row in rows]


@app.post(
    "/api/v1/journeys/{journey_id}/members/{member_id}/withdraw",
    response_model=JourneyMember,
)
def withdraw_journey_member(journey_id: UUID, member_id: str, session: Session = Depends(get_session)):
    member = session.get(JourneyMemberModel, member_id)
    if member is None or member.journey_id != str(journey_id):
        raise _share_refusal(
            404,
            "No member of this journey carries that id.",
            "MEMBER_UNKNOWN",
            {"field": "member_id", "journey_id": str(journey_id)},
        )
    if member.removed_at:
        raise _share_refusal(
            409,
            "This membership was already withdrawn; withdrawing it again is not a new fact.",
            "MEMBER_ALREADY_WITHDRAWN",
            {"member_id": member.id, "removed_at": member.removed_at},
        )
    stamp = datetime.now(timezone.utc).isoformat()
    member.removed_at = stamp
    member.updated_at = stamp
    session.commit()
    return _member_payload(member)


@app.get("/api/v1/journeys/{journey_id}/acl/roles", response_model=list[JourneyRoleRule])
def list_journey_role_rules(journey_id: UUID, session: Session = Depends(get_session)):
    """The product's own answer to what a role may do, read off the seeded table."""
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(JourneyRoleCapability).order_by(JourneyRoleCapability.role, JourneyRoleCapability.capability)
    ).all()
    by_role: dict[str, JourneyRoleRule] = {}
    for row in rows:
        rule = by_role.get(row.role)
        if rule is None:
            rule = JourneyRoleRule(role=row.role, capabilities=[], scope_message_key=row.scope_message_key)
            by_role[row.role] = rule
        rule.capabilities.append(row.capability)
    return [by_role[role] for role in sorted(by_role)]


# --- ORD-0503: the two member write doors the roster screen was missing -------------------------
# ORD-0434 shipped the member set and ORD-0436 shipped the screen that lists it, and the screen had
# to say out loud that two of its own buttons led nowhere.  These are those two doors.
#
# What is *not* here on purpose: an authenticated caller.  `actor_traveler_id` / `traveler_id` are
# claims, exactly as `invited_by` is a claim on the add door (`add_journey_member`, above), and a
# door that read a session this product does not have would be a fabricated default.  What makes the
# claim safe is that it is checked -- against the member set for the journey, and against the seeded
# capability table rather than a list typed into this function -- so the only way through is to name
# somebody who actually holds MEMBER_MANAGE on this journey.
MAX_ADMINISTRATORS_PER_JOURNEY = 2
"""How many in-force ADMIN rows one journey may hold.

Named once more in `contracts/openapi.yaml` as `x-citynexus-max-administrators-per-journey` and
answered to any screen through `GET /api/v1/journeys/{journey_id}/member-role-limits`, because a
roster that printed its own number would be the same value in two places with one of them load
bearing.  The equality of these spellings is an arm, not a hope.
"""


def _role_capabilities(session: Session, role: str) -> set[str]:
    """What a role may do, read out of `journey_role_capabilities` and not out of this function.

    The door asks the table `MEMBER_MANAGE`: if operations narrow the table, the doors follow it the
    same instant.  A hardcoded `role in {"OWNER", "ADMIN"}` here would have kept working after such
    a change and said something untrue about the product.
    """

    return set(
        session.scalars(
            select(JourneyRoleCapability.capability).where(JourneyRoleCapability.role == role)
        ).all()
    )


def _member_admin_count_in_force(session: Session, journey_id: str, now: datetime) -> int:
    """In-force ADMIN rows on this journey, counted the same way the roster state is derived.

    A withdrawn or lapsed row does not hold a seat, so it does not count toward the cap -- the same
    `_member_state` the answers report is what decides it, so the number on the screen and the number
    the door refuses on cannot describe two different rosters.
    """

    rows = session.scalars(
        select(JourneyMemberModel).where(
            JourneyMemberModel.journey_id == journey_id,
            JourneyMemberModel.role == "ADMIN",
        )
    ).all()
    return sum(1 for row in rows if _member_state(row, now) == "MEMBER_IN_FORCE")


def _member_in_force_for(session: Session, journey_id: str, traveler_id: str, now: datetime):
    return session.scalars(
        select(JourneyMemberModel).where(
            JourneyMemberModel.journey_id == journey_id,
            JourneyMemberModel.traveler_id == traveler_id,
        )
    ).first()


def _member_actor_or_unknown(session: Session, journey_id: str, actor_traveler_id: str, now: datetime):
    """The actor has to be a member of *this* journey whose membership is in force.

    Two refusals come out of one read and they are different facts: no row at all for that traveller
    here is `MEMBER_ACTOR_NOT_IN_JOURNEY`, and a row that is here but withdrawn or past its own
    deadline is `MEMBER_ACTOR_LAPSED`.  Collapsing them into one answer would tell a person whose
    invitation simply expired that they were never invited.
    """

    member = _member_in_force_for(session, journey_id, actor_traveler_id, now)
    if member is None:
        raise _share_refusal(
            403,
            "The actor named on this request is not a member of that journey.",
            "MEMBER_ACTOR_NOT_IN_JOURNEY",
            {"field": "actor_traveler_id", "journey_id": journey_id},
        )
    if _member_state(member, now) != "MEMBER_IN_FORCE":
        raise _share_refusal(
            403,
            "That actor's membership is not in force, so it carries no authority here.",
            "MEMBER_ACTOR_LAPSED",
            {
                "field": "actor_traveler_id",
                "actor_member_id": member.id,
                "member_state": _member_state(member, now),
            },
        )
    return member


@app.post(
    "/api/v1/journeys/{journey_id}/members/{member_id}/role",
    response_model=JourneyMember,
    # ORD-0395: a status this decorator does not name never reaches the generated document, and the
    # parity door compares path x method, so an undeclared refusal is a silent disagreement between
    # the contract and the runtime.  Every class this function can answer is named here.
    responses={
        403: {
            "description": "error.reason_code = MEMBER_ACTOR_NOT_IN_JOURNEY / "
            "MEMBER_ACTOR_LAPSED / MEMBER_MANAGE_NOT_PERMITTED"
        },
        404: {
            "description": "error.code = NOT_FOUND with error.reason_code = MEMBER_UNKNOWN"
        },
        409: {
            "description": "error.code = CONFLICT with error.reason_code = MEMBER_ROLE_UNCHANGED / "
            "MEMBER_ALREADY_WITHDRAWN / MEMBER_ROLE_SELF_CHANGE_REFUSED / "
            "MEMBER_OWNER_ROLE_IMMUTABLE / MEMBER_ADMIN_CAP_REACHED"
        },
    },
)
def change_journey_member_role(
    journey_id: UUID,
    member_id: str,
    payload: JourneyMemberRoleChange,
    session: Session = Depends(get_session),
):
    """ORD-0503: move one member of one journey between the roles the server already knows.

    The order the guards run in is the answer, so it is stated here once and each step has its own
    arm in `backend/tests/test_qoder_ord0503_member_doors.py`: a row that is not here at all, a row
    already withdrawn, a change that changes nothing, a person changing their own seat, an OWNER row
    moving through this door, an actor with no authority, and finally a journey whose ADMIN seats are
    full.  Read in any other order some of those would never be reachable -- which is the same as the
    rule not being there.

    `updated_at` moves and `joined_at` does not: this writes the seat, not the day it was taken.
    The answer is the row read back through the same `_member_payload` the list door uses, so the
    role a screen shows after a change is the role the store holds.
    """

    require(session, Journey, str(journey_id), "Journey")
    member = session.get(JourneyMemberModel, member_id)
    if member is None or member.journey_id != str(journey_id):
        raise _share_refusal(
            404,
            "No member of this journey carries that id.",
            "MEMBER_UNKNOWN",
            {"field": "member_id", "journey_id": str(journey_id)},
        )
    now = datetime.now(timezone.utc)
    if member.removed_at:
        raise _share_refusal(
            409,
            "This membership was withdrawn; a role change would write a seat nobody holds.",
            "MEMBER_ALREADY_WITHDRAWN",
            {"member_id": member.id, "removed_at": member.removed_at},
        )
    if payload.role == member.role:
        raise _share_refusal(
            409,
            "That is already the role this member holds, so there is nothing to change.",
            "MEMBER_ROLE_UNCHANGED",
            {"member_id": member.id, "role": member.role},
        )
    if str(payload.actor_traveler_id) == member.traveler_id:
        raise _share_refusal(
            409,
            "A person cannot move their own seat: this door answers for somebody else.",
            "MEMBER_ROLE_SELF_CHANGE_REFUSED",
            {"member_id": member.id, "actor_traveler_id": str(payload.actor_traveler_id)},
        )
    if "OWNER" in {member.role, payload.role}:
        # One OWNER is what makes a journey owned, and a demotion through this door could empty that
        # seat with nobody left to answer for the trip.  Promotion into it is a transfer of
        # ownership, which is its own decision and not a side effect of a role change.
        raise _share_refusal(
            409,
            "The OWNER seat does not move through this door in either direction.",
            "MEMBER_OWNER_ROLE_IMMUTABLE",
            {"member_id": member.id, "from_role": member.role, "to_role": payload.role},
        )
    actor = _member_actor_or_unknown(session, str(journey_id), str(payload.actor_traveler_id), now)
    if "MEMBER_MANAGE" not in _role_capabilities(session, actor.role):
        raise _share_refusal(
            403,
            "The actor's role does not hold MEMBER_MANAGE on this journey.",
            "MEMBER_MANAGE_NOT_PERMITTED",
            {
                "field": "actor_traveler_id",
                "actor_role": actor.role,
                "required_capability": "MEMBER_MANAGE",
            },
        )
    if payload.role == "ADMIN" and _member_admin_count_in_force(session, str(journey_id), now) \
            >= MAX_ADMINISTRATORS_PER_JOURNEY:
        raise _share_refusal(
            409,
            "This journey already holds as many in-force ADMIN members as it may.",
            "MEMBER_ADMIN_CAP_REACHED",
            {
                "max_administrators": MAX_ADMINISTRATORS_PER_JOURNEY,
                "admin_count_in_force": _member_admin_count_in_force(session, str(journey_id), now),
                "journey_id": str(journey_id),
            },
        )
    before_role = member.role
    stamp = datetime.now(timezone.utc).isoformat()
    member.role = payload.role
    member.updated_at = stamp
    session.commit()
    record_audit(
        session,
        action="member.role.changed",
        entity_type="JourneyMember",
        entity_id=member.id,
        journey_id=str(journey_id),
        before={"role": before_role},
        after={"role": member.role},
        actor_type="traveler",
        actor_id=str(payload.actor_traveler_id),
    )
    session.commit()
    return _member_payload(member)


@app.post(
    "/api/v1/journeys/{journey_id}/members/self-exit",
    response_model=JourneyMember,
    responses={
        404: {
            "description": "error.code = NOT_FOUND with error.reason_code = "
            "MEMBER_SELF_EXIT_NOT_A_MEMBER"
        },
        409: {
            "description": "error.code = CONFLICT with error.reason_code = MEMBER_ALREADY_WITHDRAWN"
            " / MEMBER_OWNER_CANNOT_LEAVE"
        },
    },
)
def leave_journey_as_self(journey_id: UUID, payload: JourneyMemberSelfExit,
                          session: Session = Depends(get_session)):
    """ORD-0503: the person named on the request takes themselves out of this journey.

    Not the withdraw door.  `POST .../members/{member_id}/withdraw` answers to whoever calls it about
    whomever it names and asks no question -- right for an OWNER clearing a seat, wrong for leaving,
    where the row being written has to be the caller's own.  So this door takes no `member_id`: the
    seat is found from the traveller it belongs to, and the only row it can write is that one.

    The OWNER refusal is the one that keeps a trip from becoming unowned by its own owner walking
    out.  A withdrawn row stays in the list carrying `removed_at`, which is how the roster can say
    "this person was here and is out" instead of showing a vanishing row.
    """

    require(session, Journey, str(journey_id), "Journey")
    traveler_id = str(payload.traveler_id)
    now = datetime.now(timezone.utc)
    member = _member_in_force_for(session, str(journey_id), traveler_id, now)
    if member is None:
        raise _share_refusal(
            404,
            "That traveller holds no membership row on this journey.",
            "MEMBER_SELF_EXIT_NOT_A_MEMBER",
            {"field": "traveler_id", "journey_id": str(journey_id)},
        )
    if member.removed_at:
        raise _share_refusal(
            409,
            "This membership was already withdrawn; leaving again is not a new fact.",
            "MEMBER_ALREADY_WITHDRAWN",
            {"member_id": member.id, "removed_at": member.removed_at},
        )
    if member.role == "OWNER":
        raise _share_refusal(
            409,
            "The owner of a journey cannot leave it through this door while it is still owned.",
            "MEMBER_OWNER_CANNOT_LEAVE",
            {"member_id": member.id, "journey_id": str(journey_id)},
        )
    stamp = datetime.now(timezone.utc).isoformat()
    member.removed_at = stamp
    member.updated_at = stamp
    session.commit()
    record_audit(
        session,
        action="member.self_exited",
        entity_type="JourneyMember",
        entity_id=member.id,
        journey_id=str(journey_id),
        before={"removed_at": None},
        after={"removed_at": stamp},
        actor_type="traveler",
        actor_id=traveler_id,
    )
    session.commit()
    return _member_payload(member)


@app.get("/api/v1/journeys/{journey_id}/member-role-limits", response_model=MemberRoleLimits,
         responses={
             404: {"description": "error.code = NOT_FOUND for a journey id this server does not hold"}
         })
def read_journey_member_role_limits(journey_id: UUID, session: Session = Depends(get_session)):
    """The cap and the seat count behind it, answered by the server.

    The roster screen's 上限 sentence reads this instead of carrying a number: `max_administrators`
    is `MAX_ADMINISTRATORS_PER_JOURNEY`, which the role door compares against in the same breath it
    refuses on, and which `contracts/openapi.yaml` names a third time.  All three are pinned equal by
    an arm, so a screen cannot be the only place a stale number lives.
    """

    require(session, Journey, str(journey_id), "Journey")
    now = datetime.now(timezone.utc)
    return MemberRoleLimits(
        max_administrators=MAX_ADMINISTRATORS_PER_JOURNEY,
        admin_count_in_force=_member_admin_count_in_force(session, str(journey_id), now),
        roles=["OWNER", "ADMIN", "COLLABORATOR"],
    )


@app.get("/api/v1/journeys/{journey_id}/experience")
def get_journey_experience(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    payload = experience_payload(session, journey)
    # Surface the durable recovery state (and, if the plan is still ACTIVE,
    # re-check feasibility so a stale plan cannot look healthy to the phone).
    failures: list[str] = []
    is_feasible: bool | None = None
    # A stale or infeasible selected plan must be visible to the phone whether
    # the journey is still PLANNED (about to start) or already ACTIVE.
    if journey.status in {"PLANNED", "ACTIVE"} and journey.active_plan_version_id:
        try:
            gate = validate_selected_plan_feasibility(session, journey)
            is_feasible = None if gate is None else bool(gate.passed)
            failures = list(gate.failures) if gate else []
        except ValueError as exc:
            is_feasible = False
            failures = [str(exc)]
    # GET /experience is a projection, not a recovery state transition.
    # Durable recovery is resolved only by transactional workflows that
    # revalidate and change authoritative truth (for example plan selection or
    # capability confirmation).  A read must never hide an unresolved Event.
    recovery = recovery_state(session, journey, is_feasible=is_feasible, failures=failures)
    if recovery is not None:
        recovery["journey_status"] = journey.status
    payload["recovery"] = recovery
    # Staleness is an independent condition: the selected plan's frozen
    # evidence may have expired without any HARD capability recovery event.
    staleness = any("STALE_RECOMPOSE_REQUIRED" in item for item in failures)
    open_recovery = recovery is not None and recovery.get("state") != "RESOLVED"
    flags = recovery_flags(session, journey)
    flags["staleness"] = staleness
    flags["feasibility_failures"] = failures
    flags["needs_review"] = bool(open_recovery) or staleness
    flags["recovery_required"] = bool(open_recovery)
    flags["acknowledged"] = bool(recovery and recovery.get("acknowledged"))
    flags["recovery_state"] = recovery.get("state") if recovery else None
    payload["experience_flags"] = flags
    # ORD-0520: 实时行程 reads through this door, so the "is this trip still live" judgement is
    # answered here rather than derived on the device.  It goes through the one function that
    # decides it (`app/services.py` -- `journey_live_view`), which `listJourneys` and `getJourney`
    # already use through `journey_schema`, so the same row cannot be live in one payload and not
    # live in another.  The two stored facts travel with it because the screen has to say why, in
    # the traveller's own words, and not merely that the panel is empty.
    payload["journey"]["aborted_at"] = journey.aborted_at
    payload["journey"]["abort_reason"] = journey.abort_reason
    payload["journey"]["live_view"] = journey_live_view(journey).model_dump()
    return payload


@app.post("/api/v1/journeys/{journey_id}/stays/offers/capture")
def capture_stay_offers(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    offers = capture_accommodation_offers(session, journey)
    session.commit()
    return {
        "provenance": "CAPTURED_REPLAY",
        "provider_key": offers[0].provider_key if offers else "unavailable",
        "offers": [
            {
                "id": item.id,
                "property_name": item.property_name,
                "room_type": item.room_type,
                "check_in_at": item.check_in_at,
                "check_out_at": item.check_out_at,
                "nightly_amount_minor": item.nightly_amount_minor,
                "taxes_amount_minor": item.taxes_amount_minor,
                "total_amount_minor": item.total_amount_minor,
                "currency": item.currency,
                "refundable": bool(item.refundable),
                "cancellation_policy": json.loads(item.cancellation_policy_json),
                "evidence": json.loads(item.evidence_json),
                "captured_at": item.captured_at,
                "expires_at": item.expires_at,
            }
            for item in offers
        ],
    }


def _generation_request(payload: StayPlanGenerateRequest | None) -> dict:
    request = payload or StayPlanGenerateRequest()
    return request.model_dump(mode="json")


@app.get("/api/v1/destinations/capability")
def destination_capability(query: str, session: Session = Depends(get_session)):
    """What the backend can actually plan for a destination - and which intent is missing.

    ORD-0095-1 §2: this is a server-side policy result, never a place count and never a screen's
    guess.  `capability` is the structural axis (READY / HYDRATABLE / UNKNOWN_RETRYABLE / UNAVAILABLE -
    the third added by the arbiter's answer "one" on 2026-09-22, always paired with
    `retryable: true`); an observation about *right now* is a separate field that is simply absent
    when nothing looked, because a fabricated `OK` would be a measurement nobody took.
    """

    return evaluate_capability(session, query)


@app.post("/api/v1/catalog-hydrations", status_code=202)
def create_or_reuse_catalog_hydration(
    payload: CatalogHydrationRequest,
    session: Session = Depends(get_session),
):
    """Accept a preparation request, or point the caller at the job that already covers it.

    There is no 409 here on purpose (arbitration v1): colliding with somebody else's identical
    job is *success* - the work is happening - and the response says so with `reused`.  The
    response is written before any source is consulted, so the answer cannot be late.
    """

    if payload.journey_id is not None:
        require(session, Journey, str(payload.journey_id), "Journey")
    try:
        job = accept_hydration(
            session,
            payload.query,
            journey_id=str(payload.journey_id) if payload.journey_id else None,
            requested_categories=payload.requested_categories,
        )
    except HydrationStoreBusy:
        return error(
            "HYDRATION_STORE_BUSY",
            "Another catalog write is in flight; this request was not recorded. Try again.",
            status=503,
            details={"retry_after_ms": 500},
            message_key="hydration.store_busy",
        )
    body = dict(job.payload)
    return JSONResponse(status_code=200 if job.reused else 202, content=body)


@app.get("/api/v1/catalog-hydrations/{operation_id}")
def read_catalog_hydration(operation_id: str, session: Session = Depends(get_session)):
    """The job's own record: where it got to, whether it may still move, and what it found.

    No percentage - `phase` names a stage the worker is genuinely inside and `elapsed_ms` says
    how long it has run, which is the same information without the invented denominator.
    """

    row = read_hydration(session, operation_id)
    if row is None:
        raise HTTPException(404, f"Catalog hydration operation {operation_id} was not found")
    return hydration_payload(row)


@app.post("/api/v1/catalog-hydrations/{operation_id}/abandon")
def abandon_catalog_hydration(
    operation_id: str,
    session: Session = Depends(get_session),
):
    """Stop using this job's result.  Idempotent, and never a claim it cannot keep."""

    try:
        row = abandon_hydration(session, operation_id)
    except LookupError:
        raise HTTPException(404, f"Catalog hydration operation {operation_id} was not found") from None
    return hydration_payload(row)


@app.post("/api/v1/journeys/{journey_id}/stay-generations", status_code=202)
def submit_stay_generation(
    journey_id: UUID,
    payload: StayPlanGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    """Accept the work without carrying it (ORD-0057, arbitration `5749459724` §1).

    The response is written before any provider is contacted: the request's job is to record
    an operation and hand it over, so the traveller can put the phone away.  A second tap
    with the same journey and the same question is *the same operation* - that is what stops
    four taps from becoming four 115-second chains.
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        row, reused = submit_operation(session, journey, _generation_request(payload))
    except GenerationStoreBusy:
        # Named, retryable and localised by key: the traveller's tap is not lost, it just
        # could not be written down this instant, and "Internal Server Error" would be a lie
        # about which end broke.
        return error(
            "GENERATION_STORE_BUSY",
            "Another generation is writing right now; this one was not recorded. Try again.",
            status=503,
            details={"retry_after_ms": 500},
            message_key="generation.store_busy",
        )
    body = operation_payload(row)
    return {
        "operation_id": row.id,
        "journey_id": row.journey_id,
        "status": row.status,
        "phase": body["phase"],
        "reused": reused,
        "elapsed_ms": body["elapsed_ms"],
        "retry_after_ms": body["retry_after_ms"],
        "status_url": body["status_url"],
        "abandon_url": body["abandon_url"],
        "fingerprint": row.fingerprint,
    }


@app.get("/api/v1/journeys/{journey_id}/stay-generations")
def list_stay_generations(journey_id: UUID, session: Session = Depends(get_session)):
    """Name the job this journey already has open, so a reload can restore it.

    Without this, "reload restores the same operation" only works for a client that kept the
    id - and a phone that restarted the browser is exactly the client that did not.
    """

    require(session, Journey, str(journey_id), "Journey")
    return operations_for_journey(session, str(journey_id))


@app.get("/api/v1/journeys/{journey_id}/stay-generations/{operation_id}")
def read_stay_generation(
    journey_id: UUID,
    operation_id: str,
    session: Session = Depends(get_session),
):
    """Everything a client needs to decide whether to keep waiting, from the row alone."""

    require(session, Journey, str(journey_id), "Journey")
    row = read_operation(session, operation_id)
    if row is None or row.journey_id != str(journey_id):
        raise HTTPException(404, f"Generation operation {operation_id} was not found")
    return operation_payload(row)


@app.post("/api/v1/journeys/{journey_id}/stay-generations/{operation_id}/abandon")
def abandon_stay_generation(
    journey_id: UUID,
    operation_id: str,
    session: Session = Depends(get_session),
):
    """Stop using this job's result.  Idempotent, and never a claim it cannot keep.

    A job still in flight answers `ABANDON_REQUESTED`, not `ABANDONED`: an Overpass call
    already on the wire is not dead yet, and the worker moves it to `ABANDONED` when it
    returns without writing anything the traveller can see.  A job that already finished
    cannot be un-finished, and this says so instead of reporting a cancellation that did not
    happen.
    """

    require(session, Journey, str(journey_id), "Journey")
    try:
        row = abandon_operation(session, operation_id)
    except LookupError as exc:
        raise HTTPException(404, f"Generation operation {operation_id} was not found") from exc
    if row.journey_id != str(journey_id):
        raise HTTPException(404, f"Generation operation {operation_id} was not found")
    body = operation_payload(row)
    body["abandoned"] = row.status == ABANDONED
    return body


@app.post("/api/v1/journeys/{journey_id}/stay-options/generate")
def generate_stay_journey_options(
    journey_id: UUID,
    http_response: Response,
    payload: StayPlanGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    """The synchronous door, now standing in the queue behind the same operation.

    Deliberately not a second implementation: it submits what `POST /stay-generations`
    submits and waits for that row, so a client that has not moved to the asynchronous
    contract yet is served by the same worker, the same state machine and the same stored
    result.  `X-CityNexus-Phase-Ms` and the named temporal rejections are re-emitted from
    what the worker recorded, which is what keeps ORD-0049's timing contract and ORD-0033's
    422 contract true across the move (see §94 of INTEGRATION_REPORT.md).
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        row, _reused = submit_operation(session, journey, _generation_request(payload))
    except GenerationStoreBusy:
        # Same named answer as the asynchronous door: one condition, one wording, no
        # endpoint-specific invention.
        return error(
            "GENERATION_STORE_BUSY",
            "Another generation is writing right now; this one was not recorded. Try again.",
            status=503,
            details={"retry_after_ms": 500},
            message_key="generation.store_busy",
        )
    finished = wait_for_operation(row.id)
    if finished is None:
        raise HTTPException(500, f"Generation operation {row.id} disappeared")
    header = json.loads(finished.result_json or "{}").get("phase_ms_header")
    if isinstance(header, str) and header:
        http_response.headers["X-CityNexus-Phase-Ms"] = header
    if finished.status == FAILED:
        stored = json.loads(finished.error_json or "{}")
        reason = str(stored.get("reason") or "")
        if str(stored.get("exception") or "") == "ValueError" and reason:
            # Re-raising keeps one conversion point: `temporal_rejection` below decides
            # named-422-versus-bug-500 here exactly as it did when the work ran inline.
            raise ValueError(reason)
        return error(
            str(stored.get("code") or "GENERATION_FAILED"),
            str(stored.get("message") or "Generation failed."),
            status=500,
            message_key=str(stored.get("message_key") or "error.generation_failed"),
        )
    if finished.status == ABANDONED:
        return error(
            "GENERATION_ABANDONED",
            "This generation was abandoned before it produced a result.",
            status=409,
            message_key="generation.abandoned",
        )
    result = json.loads(finished.result_json or "{}")
    result.pop("phase_ms_header", None)
    return result


@app.post("/api/v1/journeys/{journey_id}/stays/search")
def search_stays(
    journey_id: UUID,
    payload: StayPlanGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    """Product-facing alias for the captured accommodation search contract."""
    journey = require(session, Journey, str(journey_id), "Journey")
    offers = capture_accommodation_offers(session, journey)
    session.commit()
    return {
        "query": (payload.query if payload else f"{journey.city_code} stay-aware Journey"),
        "provenance": "CAPTURED_REPLAY",
        "offers": [
            {
                "id": item.id,
                "property_name": item.property_name,
                "total_amount_minor": item.total_amount_minor,
                "nightly_amount_minor": item.nightly_amount_minor,
                "currency": item.currency,
                "refundable": bool(item.refundable),
                "check_in_at": item.check_in_at,
                "check_out_at": item.check_out_at,
                "location": {"latitude": item.latitude, "longitude": item.longitude},
            }
            for item in offers
        ],
    }


@app.get("/api/v1/journeys/{journey_id}/stay-options")
def list_stay_journey_options(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    return experience_payload(session, journey)


@app.get("/api/v1/journeys/{journey_id}/multiday")
def get_multiday_journey(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    return experience_payload(session, journey)


@app.get("/api/v1/journeys/{journey_id}/stay-mobility/providers")
def get_stay_mobility_providers(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    return experience_payload(session, journey)["provider_mesh"]


@app.post("/api/v1/journeys/{journey_id}/stay-options/{option_id}/select")
def select_stay_journey_option(
    journey_id: UUID, option_id: UUID, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    option = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.id == str(option_id), StayJourneyOption.journey_id == journey.id
        )
    )
    if option is None:
        # ORD-0511: this refusal used to be the bare NOT_FOUND class every missing row on the product
        # shares, so a screen could not say "这一枚候选这台服务器没有过" without quoting the identifier
        # back.  The name is attached, not invented here: one conversion point (`http_error` below)
        # turns a raise site's `reason_code` into the envelope, which is how ORD-0101 kept a door from
        # minting a second failure vocabulary -- and ORD-0272's G6 arm is what caught the first take of
        # this slice building its own envelope.
        refused = HTTPException(404, "StayJourneyOption was not found")
        refused.reason_code = "STAY_OPTION_UNKNOWN"
        raise refused
    try:
        materialized = materialize_stay_option(
            session, journey, option, source="STAY_SELECTION"
        )
    except StayItineraryConflict as exc:
        # A hotel whose structural windows cannot coexist with the traveller's
        # confirmed itinerary is a product conflict, not a server fault.  It
        # must reach the traveller as an explicit, readable refusal.
        session.rollback()
        refused = HTTPException(422, "STAY_ITINERARY_CONFLICT: " + str(exc))
        refused.reason_code = "STAY_ITINERARY_CONFLICT"
        raise refused from exc
    if materialized.gate_status != "PASSED" or materialized.plan is None:
        session.rollback()
        # The gate already answered in its own vocabulary; a screen needs the name, not the count.
        refused = HTTPException(
            422,
            "Stay option could not become authoritative Journey truth: "
            + ", ".join(materialized.failures),
        )
        refused.reason_code = "STAY_NOT_AUTHORITATIVE"
        refused.error_details = {"failure_count": len(materialized.failures)}
        raise refused
    session.commit()
    offers = session.scalars(
        select(AccommodationOfferSnapshot)
        .where(AccommodationOfferSnapshot.journey_id == journey.id)
        .order_by(AccommodationOfferSnapshot.total_amount_minor)
    ).all()
    return {
        "selected_option_id": option.id,
        "plan_version_id": materialized.plan.id,
        "journey_status": journey.status,
        "deterministic_feasibility": materialized.gate_status,
        "protected_commitments_preserved": materialized.locked_commitments_preserved,
        "option": option_payload(option, offers),
    }


@app.post("/api/v1/journeys/{journey_id}/stay-options/{option_id}/weather")
def apply_stay_weather(
    journey_id: UUID,
    option_id: UUID,
    payload: WeatherDisruptionRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    option = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.id == str(option_id), StayJourneyOption.journey_id == journey.id
        )
    )
    if option is None:
        raise HTTPException(404, "StayJourneyOption was not found")
    generate_stay_options(session, journey, weather=payload.state)
    option = session.get(StayJourneyOption, option.id)
    session.commit()
    offers = session.scalars(
        select(AccommodationOfferSnapshot)
        .where(AccommodationOfferSnapshot.journey_id == journey.id)
        .order_by(AccommodationOfferSnapshot.total_amount_minor)
    ).all()
    return {
        "weather_state": payload.state,
        "selected_option_id": option.id if option else None,
        "protected_commitments_preserved": True,
        "recovery": "reduced_weather_exposure",
        "option": option_payload(option, offers) if option else None,
    }


@app.post("/api/v1/journeys/{journey_id}/stay-options/{option_id}/autopilot/recover")
def stay_autopilot_recover(
    journey_id: UUID,
    option_id: UUID,
    payload: StayAutopilotRecoveryRequest,
    session: Session = Depends(get_session),
):
    """Run the V8 low-risk mode recovery through the existing delegation policy."""
    journey = require(session, Journey, str(journey_id), "Journey")
    option = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.id == str(option_id), StayJourneyOption.journey_id == journey.id
        )
    )
    if option is None:
        raise HTTPException(404, "StayJourneyOption was not found")
    before_modes = json.loads(option.leg_modes_json or "[]")
    first_mode = before_modes[0] if before_modes else "WALK"
    before_mode = str(first_mode.get("mode")) if isinstance(first_mode, dict) else str(first_mode)
    requested_mode = "TRANSIT" if payload.state in {"RAIN", "TRANSIT_DISRUPTION"} else "AUTO"
    offer = session.get(AccommodationOfferSnapshot, option.accommodation_offer_id)
    if offer is None:
        raise HTTPException(409, "selected accommodation evidence is no longer available")
    first_leg = next((item for item in before_modes if isinstance(item, dict)), None)
    fact = (
        session.scalar(
            select(MobilityFact).where(
                MobilityFact.journey_id == journey.id,
                MobilityFact.from_key == str(first_leg.get("from_key")),
                MobilityFact.to_key == str(first_leg.get("to_key")),
                MobilityFact.mode == requested_mode,
            )
        )
        if first_leg
        else None
    )
    if fact is None:
        raise HTTPException(422, f"{requested_mode} is unavailable for this captured leg")
    policy_row = get_or_create_policy(session, journey.traveler_id)
    current_fact = (
        session.scalar(
            select(MobilityFact).where(
                MobilityFact.journey_id == journey.id,
                MobilityFact.from_key == fact.from_key,
                MobilityFact.to_key == fact.to_key,
                MobilityFact.mode == before_mode,
            )
        )
        if fact
        else None
    )
    known_paid_cost = not (fact.paid_mode and fact.cost_status == "UNKNOWN")
    cost_delta = max(
        0,
        int(fact.cost_amount_minor or 0)
        - int(current_fact.cost_amount_minor or 0 if current_fact else 0),
    )
    authorization = evaluate_action(
        policy_from_model(policy_row),
        AgentAction.RESCHEDULE_ACTIVITY,
        AuthoritativeActionFacts(
            hard_constraints_passed=known_paid_cost,
            locked_commitments_preserved=True,
            c4_affected=False,
            cost_delta_minor=cost_delta,
            loss_delta_minor=0,
            schedule_drift_min=0,
            affected_activity_type="transport",
            action_reversible=True,
            provider_degraded=False,
        ),
    )
    authorized = authorization.decision == AuthorizationDecision.ALLOW_AUTO
    event = EventModel(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key="citynexus-weather-replay",
        provenance="PROVIDER_VERIFIED",
        event_type="WEATHER",
        severity="MAJOR",
        status="DETECTED",
        affected_activity_id=None,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        detected_at=datetime.now(timezone.utc).isoformat(),
        payload_json=json.dumps(
            {"state": payload.state, "source": "captured_replay", "option_id": option.id},
            ensure_ascii=False,
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.add(event)
    materialized = None
    if authorized:
        generate_stay_options(session, journey, weather=payload.state)
        option = session.get(StayJourneyOption, option.id)
        if option is not None:
            materialized = materialize_stay_option(
                session,
                journey,
                option,
                source="AUTOPILOT_RECOVERY",
                force_mode=requested_mode,
            )
    executed = bool(materialized and materialized.gate_status == "PASSED")
    gate_status = (
        materialized.gate_status if materialized is not None else "NOT_RUN_UNTIL_AUTHORIZED"
    )
    if executed:
        notify(
            session,
            journey.id,
            dedupe_key=f"autopilot:{event.id}:{requested_mode}",
            kind="AUTOPILOT_PLAN_CHANGED",
            priority="HIGH",
            payload={
                "event_id": event.id,
                "plan_version_id": materialized.plan.id if materialized.plan else None,
                "mode": requested_mode,
            },
        )
    record_audit(
        session,
        action="v8.autopilot.recovery",
        entity_type="StayJourneyOption",
        entity_id=option.id,
        journey_id=journey.id,
        actor_type="AI_AGENT",
        actor_id="v8-autopilot",
        after={
            "decision": authorization.decision.value,
            "executed": executed,
            "requested_mode": requested_mode,
            "event_id": event.id,
            "protected_commitments_preserved": bool(
                materialized.locked_commitments_preserved if materialized else True
            ),
            "post_action_gate": gate_status,
            "policy_version": authorization.policy_version,
        },
    )
    session.commit()
    offers = session.scalars(
        select(AccommodationOfferSnapshot)
        .where(AccommodationOfferSnapshot.journey_id == journey.id)
        .order_by(AccommodationOfferSnapshot.total_amount_minor)
    ).all()
    return {
        "event_id": event.id,
        "decision": authorization.decision.value,
        "reason_codes": list(authorization.reason_codes),
        "executed": executed,
        "requested_mode": requested_mode,
        "before_mode": before_mode,
        "after_mode": requested_mode if executed else before_mode,
        "policy_version": authorization.policy_version,
        "active_plan_version_id": journey.active_plan_version_id,
        "protected_commitments_preserved": bool(
            materialized.locked_commitments_preserved if materialized else True
        ),
        "post_action_gate": gate_status,
        "option": option_payload(option, offers) if option else None,
    }


@app.get("/api/v1/journeys/{journey_id}/mobility")
def list_journey_mobility(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    return {
        "modes": [
            fact_payload(item)
            for item in session.scalars(
                select(MobilityFact)
                .where(MobilityFact.journey_id == journey.id)
                .order_by(MobilityFact.from_key, MobilityFact.to_key, MobilityFact.mode)
            ).all()
        ],
        "available_modes": ["WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"],
        "choose_for_me": True,
    }


@app.post("/api/v1/journeys/{journey_id}/mobility/compare")
def compare_journey_mobility(
    journey_id: UUID, payload: MobilityModeRequest, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    facts = session.scalars(
        select(MobilityFact)
        .where(MobilityFact.journey_id == journey.id, MobilityFact.mode == payload.mode)
        .order_by(MobilityFact.from_key, MobilityFact.to_key)
    ).all()
    if not facts:
        offers = capture_accommodation_offers(session, journey)
        for offer in offers:
            capture_mobility_facts(session, journey, offer)
        session.commit()
        facts = session.scalars(
            select(MobilityFact)
            .where(MobilityFact.journey_id == journey.id, MobilityFact.mode == payload.mode)
            .order_by(MobilityFact.from_key, MobilityFact.to_key)
        ).all()
    return {
        "mode": payload.mode,
        "facts": [fact_payload(item) for item in facts],
        "deterministic_feasibility": "PASSED",
        "costs_are_provider_evidence": True,
    }


@app.post("/api/v1/journeys/{journey_id}/stay-options/{option_id}/mode")
def change_stay_option_mode(
    journey_id: UUID,
    option_id: UUID,
    payload: StayModeChangeRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    option = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.id == str(option_id), StayJourneyOption.journey_id == journey.id
        )
    )
    if option is None:
        raise HTTPException(404, "StayJourneyOption was not found")
    offer = session.get(AccommodationOfferSnapshot, option.accommodation_offer_id)
    if offer is None:
        raise HTTPException(409, "selected accommodation evidence is no longer available")
    modes = json.loads(option.leg_modes_json or "[]")
    target = next(
        (
            item
            for item in modes
            if isinstance(item, dict)
            and (payload.from_key is None or item.get("from_key") == payload.from_key)
            and (payload.to_key is None or item.get("to_key") == payload.to_key)
        ),
        None,
    )
    if target is None:
        raise HTTPException(422, "requested Journey leg was not found")
    facts_query = select(MobilityFact).where(
        MobilityFact.journey_id == journey.id,
        MobilityFact.mode == payload.mode,
        MobilityFact.from_key == str(target["from_key"]),
        MobilityFact.to_key == str(target["to_key"]),
    )
    fact = session.scalar(facts_query.order_by(MobilityFact.from_key, MobilityFact.to_key))
    if fact is None:
        raise HTTPException(422, f"mode {payload.mode} is unavailable for the requested leg")
    if fact.paid_mode and fact.cost_status == "UNKNOWN":
        raise HTTPException(
            409,
            "UNKNOWN paid mode cost requires confirmation and cannot auto-execute",
        )
    target["mode"] = payload.mode
    target["mobility_fact_id"] = fact.id
    target["cost_status"] = fact.cost_status
    option.leg_modes_json = json.dumps(modes, ensure_ascii=False)
    option.journey_impact_json = json.dumps(
        {
            **json.loads(option.journey_impact_json or "{}"),
            "mode_switch": {
                "mode": payload.mode,
                "duration_min": fact.duration_min,
                "cost_amount_minor": fact.cost_amount_minor,
                "currency": fact.currency,
                "walking_min": fact.walking_min,
                "transfers": fact.transfers,
                "weather_exposure": fact.weather_exposure,
                "reliability_score": fact.reliability_score,
                "cost_status": (
                    "KNOWN_CAPTURED"
                    if fact.cost_status == "KNOWN"
                    and fact.provenance in {"CAPTURED_REPLAY", "SYNTHETIC_FIXTURE"}
                    else fact.cost_status
                ),
                "cost_evidence_status": fact.cost_status,
                "provenance": fact.provenance,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    selected_facts = [
        session.scalar(
            select(MobilityFact).where(
                MobilityFact.journey_id == journey.id,
                MobilityFact.from_key == str(item["from_key"]),
                MobilityFact.to_key == str(item["to_key"]),
                MobilityFact.mode == str(item["mode"]),
            )
        )
        for item in modes
        if isinstance(item, dict)
    ]
    selected_facts = [item for item in selected_facts if item is not None]
    previous_walking = int(option.walking_min)
    previous_travel = int(option.daily_travel_minor)
    option.daily_travel_minor = sum(int(item.cost_amount_minor or 0) for item in selected_facts)
    option.mobility_total_minor = option.daily_travel_minor
    option.score = round(
        max(
            0.0,
            float(option.score)
            + (float(previous_walking - fact.walking_min) / 2000.0)
            - (float(previous_travel - (fact.cost_amount_minor or 0)) / 100000.0),
        ),
        3,
    )
    option.walking_min = sum(item.walking_min for item in selected_facts)
    materialized = None
    if option.status == "SELECTED" or option.materialized_plan_version_id:
        materialized = materialize_stay_option(session, journey, option, source="MODE_CHANGE")
        if materialized.gate_status != "PASSED":
            session.rollback()
            raise HTTPException(422, ", ".join(materialized.failures))
    session.commit()
    return {
        "option_id": option.id,
        "selected_mode": payload.mode,
        "impact": json.loads(option.journey_impact_json),
        "active_plan_version_id": journey.active_plan_version_id,
        "protected_commitments_preserved": bool(
            materialized.locked_commitments_preserved if materialized else True
        ),
        "deterministic_feasibility": (
            materialized.gate_status if materialized else "CANDIDATE_NOT_MATERIALIZED"
        ),
    }


def _draft_payload(item: ExternalReservationDraft) -> dict[str, Any]:
    extracted = json.loads(item.extracted_json or "{}")
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "artifact_type": item.artifact_type,
        "extracted": extracted,
        "interpretation": {
            "anchor_role": extracted.get("anchor_role"),
            "anchor_role_confidence": extracted.get("anchor_role_confidence"),
            "semantic_target": extracted.get("semantic_target"),
            "payment_status": extracted.get("payment_status"),
            "reasoning_summary": extracted.get("reasoning_summary"),
        },
        "smart_intake": extracted.get("smart_intake", {}),
        "unresolved_fields": json.loads(item.unresolved_fields_json or "[]"),
        "extraction_mode": item.extraction_mode,
        "status": item.status,
        "created_at": item.created_at,
        "confirmed_at": item.confirmed_at,
    }


def _reservation_payload(item: TransportReservation) -> dict[str, Any]:
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "draft_id": item.draft_id,
        "reservation_type": item.reservation_type,
        "provider_key": item.provider_key,
        "confirmation_ref": item.confirmation_ref,
        "origin": json.loads(item.origin_json or "{}"),
        "destination": json.loads(item.destination_json or "{}"),
        "scheduled_departure_at": item.scheduled_departure_at,
        "scheduled_arrival_at": item.scheduled_arrival_at,
        "estimated_arrival_at": item.estimated_arrival_at,
        "actual_arrival_at": item.actual_arrival_at,
        "status": item.status,
        "realtime_status": item.realtime_status,
        "passenger_scope": json.loads(item.passenger_scope_json or "{}"),
        "change_policy": json.loads(item.change_policy_json or "{}"),
        "price": {
            "amount_minor": item.price_amount_minor,
            "currency": item.currency,
            "status": item.price_status,
        },
        "source_provenance": item.source_provenance,
        "captured_at": item.captured_at,
        "booking_id": item.booking_id,
        "commitment_id": item.commitment_id,
    }


@app.put("/api/v1/journeys/{journey_id}/operations/luggage")
def put_journey_luggage(
    journey_id: UUID,
    payload: LuggageContextRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    row = set_luggage_context(
        session,
        journey,
        baggage_present=payload.baggage_present,
        baggage_size_class=payload.baggage_size_class,
        luggage_drop_required=payload.luggage_drop_required,
    )
    session.commit()
    return {
        "journey_id": journey.id,
        "baggage_present": bool(row.baggage_present),
        "baggage_size_class": row.baggage_size_class,
        "luggage_drop_required": bool(row.luggage_drop_required),
    }


@app.post("/api/v1/journeys/{journey_id}/inbox/import", status_code=201)
def import_journey_reservation(
    journey_id: UUID,
    payload: ReservationImportRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    draft = import_reservation_artifact(
        session,
        journey,
        artifact_type=payload.artifact_type,
        payload=payload.payload,
    )
    session.commit()
    return _draft_payload(draft)


@app.get("/api/v1/journeys/{journey_id}/inbox")
def list_journey_inbox(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    drafts = session.scalars(
        select(ExternalReservationDraft)
        .where(ExternalReservationDraft.journey_id == journey.id)
        .order_by(ExternalReservationDraft.created_at)
    ).all()
    return {"journey_id": journey.id, "drafts": [_draft_payload(item) for item in drafts]}


@app.get("/api/v1/journeys/{journey_id}/memory")
def get_journey_memory(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    return journey_memory_payload(session, journey)


def _reservation_unresolved(extracted: dict[str, Any]) -> list[str]:
    critical = (
        "reservation_type",
        "origin",
        "destination",
        "scheduled_departure_at",
        "scheduled_arrival_at",
        "confirmation_ref",
    )
    unresolved = [
        field
        for field in critical
        if extracted.get(field) in (None, "", "UNKNOWN", "unknown")
    ]
    if "_semantic_contract" in extracted:
        role = str(extracted.get("anchor_role") or "UNKNOWN").upper()
        if role not in {"ARRIVAL", "DEPARTURE", "MID_TRIP"}:
            unresolved.append("anchor_role")
        if role == "MID_TRIP" and not extracted.get("semantic_node_key"):
            unresolved.append("semantic_target")
    return list(dict.fromkeys(unresolved))


def _refresh_draft_semantics(
    session: Session, journey: Journey, draft: ExternalReservationDraft
) -> dict[str, Any]:
    extracted = json.loads(draft.extracted_json or "{}")
    # Structured artifacts go through the same semantic intake as extracted
    # ones.  Returning early here meant a manually entered booking could never
    # reach a decision, so smart-apply was permanently unavailable for it.
    enriched, _ = prepare_semantic_intake(session, journey, extracted)
    unresolved = _reservation_unresolved(enriched)
    draft.extracted_json = json.dumps(enriched, ensure_ascii=False)
    draft.unresolved_fields_json = json.dumps(unresolved)
    draft.status = "READY_FOR_CONFIRMATION" if not unresolved else "NEEDS_REVIEW"
    return enriched


def _deepseek_client(*, vision: bool = False) -> DeepSeekResponsesClient | None:
    if not settings.ai_api_key:
        return None
    return DeepSeekResponsesClient(
        api_key=settings.ai_api_key,
        model=settings.ai_vision_model if vision else settings.ai_model,
        base_url=settings.deepseek_api_url,
        reasoning_effort=settings.deepseek_reasoning_effort,
        timeout_seconds=settings.impact_timeout_seconds,
    )


@app.post(
    "/api/v1/journeys/{journey_id}/inbox/text-extract",
    status_code=201,
    responses={
        502: {
            "description": (
                "The extraction provider failed this request. The body still names the machine status"
                " (FAILED_EXTERNAL) and the provider's own reason; no draft was created, and none is"
                " claimed."
            )
        }
    },
)
def extract_journey_reservation_text(
    journey_id: UUID,
    payload: ReservationTextExtractionRequest,
    response: Response,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    client = _deepseek_client()
    if client is None:
        return {"status": "NOT_CONFIGURED", "draft": None, "provider": "deepseek"}
    try:
        extracted = extract_reservation_text(
            client,
            payload.text,
            journey_context=journey_interpreter_context(session, journey),
        )
    except DeepSeekResponsesError as exc:
        # ORD-0551: a supplier failure creates nothing, so answering 201 for it told the caller a
        # draft had been made. The machine status stays in the body -- it is what the display
        # vocabulary translates -- but the status code now says what happened upstream.
        response.status_code = 502
        return {"status": "FAILED_EXTERNAL", "draft": None, "provider": "deepseek", "reason": str(exc)}
    extracted, _ = prepare_semantic_intake(session, journey, extracted)
    unresolved = _reservation_unresolved(extracted)
    draft = ExternalReservationDraft(
        id=str(uuid4()),
        journey_id=journey.id,
        artifact_type="TEXT",
        raw_payload_json=json.dumps({"text_length": len(payload.text)}, ensure_ascii=False),
        extracted_json=json.dumps(extracted, ensure_ascii=False),
        unresolved_fields_json=json.dumps(unresolved),
        extraction_mode="DEEPSEEK_RESPONSES_TEXT",
        status="READY_FOR_CONFIRMATION" if not unresolved else "NEEDS_REVIEW",
        created_at=datetime.now(timezone.utc).isoformat(),
        confirmed_at=None,
    )
    session.add(draft)
    session.commit()
    return {"status": "READY", "draft": _draft_payload(draft), "provider": "deepseek"}


@app.post(
    "/api/v1/journeys/{journey_id}/inbox/vision",
    status_code=201,
    responses={
        502: {
            "description": (
                "The extraction provider failed this image read. The body names the machine status"
                " (FAILED_EXTERNAL) and the provider's reason; no draft was created."
            )
        }
    },
)
def extract_journey_reservation_image(
    journey_id: UUID,
    payload: ReservationVisionRequest,
    response: Response,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        client = _deepseek_client(vision=True)
    except TypeError as exc:
        # Preserve the existing V10 provider seam for tests/fakes that expose
        # the original zero-argument factory; production uses the dedicated
        # vision model above.
        if "vision" not in str(exc):
            raise
        client = _deepseek_client()
    if client is None:
        return {"status": "NOT_CONFIGURED", "draft": None, "provider": "deepseek"}
    try:
        extracted = extract_reservation_image(
            client,
            payload.image_url,
            hint=payload.hint,
            journey_context=journey_interpreter_context(session, journey),
        )
    except DeepSeekResponsesError as exc:
        # ORD-0551: same reason as the text route -- nothing was created here.
        response.status_code = 502
        return {"status": "FAILED_EXTERNAL", "draft": None, "provider": "deepseek", "reason": str(exc)}
    extracted, _ = prepare_semantic_intake(session, journey, extracted)
    unresolved = _reservation_unresolved(extracted)
    draft = ExternalReservationDraft(
        id=str(uuid4()),
        journey_id=journey.id,
        artifact_type="IMAGE",
        raw_payload_json=json.dumps({"image_source": "redacted_external_reference"}),
        extracted_json=json.dumps(extracted, ensure_ascii=False),
        unresolved_fields_json=json.dumps(unresolved),
        extraction_mode="DEEPSEEK_RESPONSES_VISION",
        status="READY_FOR_CONFIRMATION" if not unresolved else "NEEDS_REVIEW",
        created_at=datetime.now(timezone.utc).isoformat(),
        confirmed_at=None,
    )
    session.add(draft)
    session.commit()
    return {"status": "READY", "draft": _draft_payload(draft), "provider": "deepseek-vision"}


@app.patch("/api/v1/journeys/{journey_id}/inbox/{draft_id}")
def patch_journey_reservation_draft(
    journey_id: UUID,
    draft_id: UUID,
    payload: ReservationDraftPatchRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    draft = session.get(ExternalReservationDraft, str(draft_id))
    if draft is None or draft.journey_id != journey.id:
        raise HTTPException(404, "reservation draft was not found")
    if draft.status == "CONFIRMED":
        raise HTTPException(409, "confirmed reservation drafts require a new correction artifact")
    extracted = json.loads(draft.extracted_json or "{}")
    changes = payload.model_dump(exclude_unset=True)
    if any(
        key in changes
        for key in ("anchor_role", "semantic_node_key", "semantic_target")
    ):
        extracted["_semantic_contract"] = "V13.2"
    if "anchor_role" in changes and changes["anchor_role"] in {
        "ARRIVAL",
        "DEPARTURE",
        "MID_TRIP",
    }:
        extracted["anchor_role_confidence"] = 1.0
        extracted["uncertain_fields"] = [
            item
            for item in extracted.get("uncertain_fields", [])
            if item != "anchor_role"
        ]
        provenance = dict(extracted.get("field_provenance") or {})
        provenance["anchor_role"] = {"source": "USER", "status": "CORRECTED"}
        extracted["field_provenance"] = provenance
    extracted.update(changes)
    draft.extracted_json = json.dumps(extracted, ensure_ascii=False)
    _refresh_draft_semantics(session, journey, draft)
    session.commit()
    return _draft_payload(draft)


@app.post("/api/v1/journeys/{journey_id}/inbox/{draft_id}/smart-apply")
def smart_apply_journey_reservation(
    journey_id: UUID,
    draft_id: UUID,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    draft = session.get(ExternalReservationDraft, str(draft_id))
    if draft is None or draft.journey_id != journey.id:
        raise HTTPException(404, "reservation draft was not found")
    extracted = _refresh_draft_semantics(session, journey, draft)
    smart = extracted.get("smart_intake", {})
    decision = smart.get("decision") or "NEEDS_REVIEW"
    if draft.unresolved_fields_json != "[]" or decision not in {
        "AUTO_APPLY",
        "APPLY_WITH_NOTICE",
    }:
        session.commit()
        return {
            "status": decision,
            "decision": decision,
            "draft": _draft_payload(draft),
        }
    try:
        reservation = confirm_reservation_draft(session, journey, draft)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {
        "status": "APPLIED",
        "decision": decision,
        "draft": _draft_payload(draft),
        "reservation": _reservation_payload(reservation),
        "commitment": _commitment_payload(
            require(session, CommitmentModel, reservation.commitment_id, "Commitment")
        ),
        "memory": journey_memory_payload(session, journey),
    }


@app.post("/api/v1/journeys/{journey_id}/web-research")
def run_journey_web_research(
    journey_id: UUID,
    payload: WebResearchRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    client = _deepseek_client()
    if client is None or not settings.deepseek_web_search_enabled:
        return {
            "status": "NOT_CONFIGURED",
            "journey_id": journey.id,
            "observations": [],
            "authority": "NON_AUTHORITATIVE",
        }
    try:
        drafts = WebResearchProvider(client).search(payload.query, source_class=payload.source_class)
    except DeepSeekResponsesError as exc:
        return {
            "status": "FAILED_EXTERNAL",
            "journey_id": journey.id,
            "observations": [],
            "authority": "NON_AUTHORITATIVE",
            "reason": str(exc),
        }
    rows: list[WebObservation] = []
    for item in drafts:
        existing = session.scalar(
            select(WebObservation).where(
                WebObservation.journey_id == journey.id,
                WebObservation.evidence_fingerprint == item.evidence_fingerprint,
            )
        )
        if existing is None:
            existing = WebObservation(
                id=str(uuid4()),
                journey_id=journey.id,
                query=item.query,
                url=item.url,
                domain=item.domain,
                title=item.title,
                source_class=item.source_class,
                provider_key=item.provider_key,
                model_key=item.model_key,
                captured_at=item.captured_at,
                evidence_fingerprint=item.evidence_fingerprint,
                content_excerpt=item.content_excerpt,
                authority_status="NON_AUTHORITATIVE",
                created_at=item.captured_at,
            )
            session.add(existing)
        rows.append(existing)
    session.commit()
    return {
        "status": "CAPTURED" if rows else "NO_RESULTS",
        "journey_id": journey.id,
        "authority": "NON_AUTHORITATIVE",
        "observations": [
            {
                "id": item.id,
                "query": item.query,
                "url": item.url,
                "domain": item.domain,
                "title": item.title,
                "source_class": item.source_class,
                "provider_key": item.provider_key,
                "model": item.model_key,
                "captured_at": item.captured_at,
                "evidence_fingerprint": item.evidence_fingerprint,
                "authority_status": item.authority_status,
            }
            for item in rows
        ],
    }


@app.get("/api/v1/journeys/{journey_id}/web-observations")
def list_journey_web_observations(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(WebObservation)
        .where(WebObservation.journey_id == journey.id)
        .order_by(WebObservation.captured_at.desc())
    ).all()
    return {
        "journey_id": journey.id,
        "authority": "NON_AUTHORITATIVE",
        "observations": [
            {
                "id": item.id,
                "query": item.query,
                "url": item.url,
                "domain": item.domain,
                "title": item.title,
                "source_class": item.source_class,
                "provider_key": item.provider_key,
                "model": item.model_key,
                "captured_at": item.captured_at,
                "evidence_fingerprint": item.evidence_fingerprint,
                "authority_status": item.authority_status,
            }
            for item in rows
        ],
    }


@app.get("/api/v1/journeys/{journey_id}/execution-clock")
def get_journey_execution_clock(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    now, used_timezone, honoured = execution_clock.now_honest(journey.timezone)
    return {
        "journey_id": journey.id,
        "now": now.isoformat(),
        "timezone": journey.timezone,
        # Which zone `now` is actually in, and whether it is the one the journey asked for.  Without
        # these two the payload could name a zone and carry a moment from another one, and a reader
        # had no way to tell.
        "used_timezone": used_timezone,
        "timezone_honoured": honoured,
        "source": "EXECUTION_CLOCK",
        "current_activity_id": journey.current_activity_id,
    }


def _draft_confirmation_ref(draft: ExternalReservationDraft) -> str | None:
    try:
        data = json.loads(draft.extracted_json or "{}")
    except json.JSONDecodeError:
        return None
    value = data.get("confirmation_ref")
    return str(value) if value else None


def _confirmed_reservation_for_draft(
    session: Session, journey: Journey, draft: ExternalReservationDraft
) -> TransportReservation | None:
    """Resolve the winner's TransportReservation for an idempotent replay."""

    existing = session.scalar(
        select(TransportReservation).where(TransportReservation.draft_id == draft.id)
    )
    if existing is None:
        reference = _draft_confirmation_ref(draft)
        if reference:
            existing = session.scalar(
                select(TransportReservation).where(
                    TransportReservation.journey_id == journey.id,
                    TransportReservation.confirmation_ref == reference,
                )
            )
    return existing


@app.post("/api/v1/journeys/{journey_id}/inbox/{draft_id}/confirm")
def confirm_journey_reservation(
    journey_id: UUID, draft_id: UUID, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    draft = session.get(ExternalReservationDraft, str(draft_id))
    if draft is None or draft.journey_id != journey.id:
        raise HTTPException(404, "reservation draft was not found")
    try:
        extracted = _refresh_draft_semantics(session, journey, draft)
        smart = extracted.get("smart_intake", {})
        if "DUPLICATE_CONFIRMATION_REF" in smart.get("conflicts", []):
            raise ValueError("DUPLICATE_CONFIRMATION_REF: reservation already exists in this Journey")
        reservation = confirm_reservation_draft(session, journey, draft)
        session.flush()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        # Two racing confirmations of the same confirmation_ref both passed the
        # pre-check.  The unique index (journey_id, confirmation_ref) is the
        # real serialization point: the loser must receive the existing
        # artifact, a deterministic 409 - and never an HTTP 500.
        session.rollback()
        detail = str(getattr(exc, "orig", exc))
        existing = _confirmed_reservation_for_draft(session, journey, draft)
        if existing is not None:
            return {
                "reservation": _reservation_payload(existing),
                "commitment": _commitment_payload(
                    require(session, CommitmentModel, existing.commitment_id, "Commitment")
                ),
                "idempotent_replay": True,
            }
        record_audit(
            session,
            action="reservation.confirmation.conflict",
            entity_type="ExternalReservationDraft",
            entity_id=draft.id,
            journey_id=journey.id,
            actor_type="SYSTEM",
            after={
                "reason": "CONCURRENT_CONFIRMATION_INTEGRITY_ERROR",
                "confirmation_ref": _draft_confirmation_ref(draft),
                "detail": detail[:300],
            },
        )
        session.commit()
        raise HTTPException(
            409, "CONCURRENT_CONFIRMATION_CONFLICT: reservation already confirmed"
        ) from exc
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        existing = _confirmed_reservation_for_draft(session, journey, draft)
        if existing is not None:
            return {
                "reservation": _reservation_payload(existing),
                "commitment": _commitment_payload(
                    require(session, CommitmentModel, existing.commitment_id, "Commitment")
                ),
                "idempotent_replay": True,
            }
        raise HTTPException(
            409, "CONCURRENT_CONFIRMATION_CONFLICT: reservation already confirmed"
        ) from exc
    return {
        "reservation": _reservation_payload(reservation),
        "commitment": _commitment_payload(
            require(session, CommitmentModel, reservation.commitment_id, "Commitment")
        ),
    }


@app.get("/api/v1/journeys/{journey_id}/wallet")
def get_travel_wallet(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    reservations = session.scalars(
        select(TransportReservation)
        .where(TransportReservation.journey_id == journey.id)
        .order_by(TransportReservation.scheduled_arrival_at)
    ).all()
    commitments = session.scalars(
        select(CommitmentModel)
        .where(CommitmentModel.journey_id == journey.id)
        .order_by(CommitmentModel.created_at)
    ).all()
    return {
        "journey_id": journey.id,
        "reservations": [_reservation_payload(item) for item in reservations],
        "commitments": [_commitment_payload(item) for item in commitments],
        "offline_ready": session.get(OfflineJourneySnapshot, journey.id) is not None,
    }


@app.post("/api/v1/journeys/{journey_id}/reservations/{reservation_id}/schedule-change")
def apply_reservation_schedule_change(
    journey_id: UUID,
    reservation_id: UUID,
    payload: ScheduleChangeRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    reservation = session.get(TransportReservation, str(reservation_id))
    if reservation is None or reservation.journey_id != journey.id:
        raise HTTPException(404, "transport reservation was not found")
    captured_at = payload.captured_at.astimezone(timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
    reservation.estimated_arrival_at = payload.estimated_arrival_at.isoformat()
    reservation.status = payload.status
    reservation.realtime_status = "STALE_REALTIME" if age_seconds > 300 else payload.status
    event = EventModel(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=payload.provider_key,
        provenance="PROVIDER_VERIFIED",
        event_type="SCHEDULE_DELAY",
        severity="CRITICAL" if payload.status == "CANCELLED" else "MAJOR",
        status="DETECTED",
        affected_activity_id=None,
        occurred_at=payload.captured_at.isoformat(),
        detected_at=datetime.now(timezone.utc).isoformat(),
        resolved_at=None,
        payload_json=json.dumps(
            {
                "reservation_id": reservation.id,
                "estimated_arrival_at": reservation.estimated_arrival_at,
                "realtime_status": reservation.realtime_status,
                "service_alert": payload.service_alert,
                "wake_guardian": True,
                "journey_risk": "CRITICAL" if payload.status == "CANCELLED" else "AT_RISK",
            },
            ensure_ascii=False,
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.add(event)
    notification = notify(
        session,
        journey.id,
        dedupe_key=f"schedule-change:{reservation.id}:{payload.captured_at.isoformat()}",
        kind="SCHEDULE_CHANGE",
        priority="CRITICAL" if payload.status == "CANCELLED" else "HIGH",
        payload={"reservation_id": reservation.id, "event_id": event.id},
    )
    record_audit(
        session,
        action="long_distance.schedule_changed",
        entity_type="TransportReservation",
        entity_id=reservation.id,
        journey_id=journey.id,
        after={"event_id": event.id, "realtime_status": reservation.realtime_status},
    )
    if journey.active_plan_version_id:
        refresh_offline_snapshot(session, journey)
    session.commit()
    return {
        "reservation": _reservation_payload(reservation),
        "event_id": event.id,
        "notification_id": notification.id,
        "guardian_wakeup": True,
        "journey_risk": "CRITICAL" if payload.status == "CANCELLED" else "AT_RISK",
        "risk_reason_codes": [
            "LONG_DISTANCE_CANCELLED"
            if payload.status == "CANCELLED"
            else "LONG_DISTANCE_SCHEDULE_CHANGED",
            *(["SERVICE_ALERT"] if payload.service_alert else []),
        ],
    }


@app.get("/api/v1/journeys/{journey_id}/offline-snapshot")
def get_offline_journey_snapshot(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    row = session.get(OfflineJourneySnapshot, journey.id)
    if row is None:
        row = refresh_offline_snapshot(session, journey)
        session.commit()
    return {
        "journey_id": row.journey_id,
        "sequence": row.sequence,
        "valid_until": row.valid_until,
        "snapshot": json.loads(row.payload_json),
        "provider_freshness": json.loads(row.provider_freshness_json),
        "updated_at": row.updated_at,
    }


@app.post("/api/v1/journeys/{journey_id}/location", status_code=201)
def post_foreground_location(
    journey_id: UUID,
    payload: LocationObservationRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        row = record_location(
            session,
            journey,
            latitude=payload.latitude,
            longitude=payload.longitude,
            accuracy_m=payload.accuracy_m,
            captured_at=payload.captured_at.isoformat(),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {
        "id": row.id,
        "journey_id": row.journey_id,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "accuracy_m": row.accuracy_m,
        "captured_at": row.captured_at,
        "source": row.source,
        "background_tracking_claimed": False,
    }


@app.post("/api/v1/travelers/{traveler_id}/push-subscriptions", status_code=201)
def create_push_subscription(
    traveler_id: UUID,
    payload: PushSubscriptionRequest,
    session: Session = Depends(get_session),
):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    try:
        row = register_push_subscription(session, traveler.id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"id": row.id, "status": row.status, "endpoint_hash": row.endpoint_hash}


@app.get("/api/v1/travelers/{traveler_id}/push-config")
def get_push_config(traveler_id: UUID, session: Session = Depends(get_session)):
    traveler = require(session, Traveler, str(traveler_id), "Traveler")
    return {
        "traveler_id": traveler.id,
        "capability": push_capability(),
        "public_key": settings.vapid_public_key,
        "record_fallback": True,
    }


@app.get("/api/v1/journeys/{journey_id}/notifications")
def list_journey_notifications(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(NotificationEvent)
        .where(NotificationEvent.journey_id == journey.id)
        .order_by(NotificationEvent.created_at.desc())
    ).all()
    return {
        "journey_id": journey.id,
        "notifications": [
            {
                "id": item.id,
                "kind": item.kind,
                "priority": item.priority,
                "status": item.status,
                "payload": json.loads(item.payload_json),
                "delivery_channels": json.loads(item.delivery_channels_json),
                "occurrence_count": item.occurrence_count,
                "created_at": item.created_at,
            }
            for item in rows
        ],
    }


@app.get("/api/v1/providers/v9")
def get_v9_provider_status():
    amap = amap_status()
    return {
        "region_selection": {
            "CN-*": provider_for_region("CN-TIANJIN"),
            "global": provider_for_region("US-NYC"),
        },
        "amap": {
            "provider_key": amap.provider_key,
            "status": amap.status,
            "provenance": amap.provenance,
            "coordinate_crs": amap.coordinate_crs,
            "reason": amap.reason,
        },
        "transit_realtime": {"status": "NOT_CONFIGURED", "absence_semantics": "UNKNOWN_REALTIME"},
        "otp": {"status": "NOT_CONFIGURED"},
        "duffel": {"status": "NOT_CONFIGURED"},
    }


@app.post("/api/v1/journeys/{journey_id}/edge/devices", status_code=201)
def create_edge_device(
    journey_id: UUID,
    payload: EdgeDeviceCreateRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    device, token = register_edge_device(session, journey, display_name=payload.display_name)
    session.commit()
    return {
        "device_id": device.id,
        "device_token": token,
        "token_returned_once": True,
        "scopes": json.loads(device.scopes_json),
    }


def _authorized_edge(session: Session, device_id: UUID, token: str | None) -> EdgeDevice:
    device = session.get(EdgeDevice, str(device_id))
    if device is None or not token or not authenticate_edge(device, token):
        raise HTTPException(401, "invalid Edge Companion credentials")
    return device


@app.get("/api/v1/edge/devices/{device_id}/journey-snapshot")
def get_edge_journey_snapshot(
    device_id: UUID,
    x_edge_token: str | None = Header(default=None, alias="X-Edge-Token"),
    session: Session = Depends(get_session),
):
    device = _authorized_edge(session, device_id, x_edge_token)
    try:
        payload = edge_snapshot(session, device)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return payload


@app.post("/api/v1/edge/devices/{device_id}/actions", status_code=201)
def post_edge_action(
    device_id: UUID,
    payload: EdgeActionRequest,
    x_edge_token: str | None = Header(default=None, alias="X-Edge-Token"),
    session: Session = Depends(get_session),
):
    device = _authorized_edge(session, device_id, x_edge_token)
    try:
        row = record_edge_action(session, device, action=payload.action, payload=payload.payload)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    session.commit()
    return {"id": row.id, "action": row.action, "status": row.status}


@app.post("/api/v1/edge/devices/{device_id}/observations", status_code=201)
def post_edge_observation(
    device_id: UUID,
    payload: EdgeObservationRequest,
    x_edge_token: str | None = Header(default=None, alias="X-Edge-Token"),
    session: Session = Depends(get_session),
):
    device = _authorized_edge(session, device_id, x_edge_token)
    try:
        row = record_edge_observation(
            session,
            device,
            observation_type=payload.observation_type,
            payload=payload.payload,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"id": row.id, "observation_type": row.observation_type, "recorded": True}


@app.patch("/api/v1/journeys/{journey_id}")
def update_journey(
    journey_id: UUID, payload: JourneyPatch, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    before = journey_schema(journey).model_dump(mode="json")
    start = payload.start_at or datetime.fromisoformat(journey.start_at)
    end = payload.end_at or datetime.fromisoformat(journey.end_at)
    if end <= start:
        raise HTTPException(422, "end_at must be after start_at")
    # ORD-0033's ruling covers this door as well: stretching a saved trip past the supported
    # window would otherwise rebuild the exact state create now refuses - a journey that
    # cannot generate stays.  Measured on the stored anchors through `journey_boundaries`,
    # so an anchor still wins over the window, and before anything is mutated.
    trip_context = session.get(JourneyTripContext, journey.id)
    if trip_context is None:
        derive_journey_temporal_shape(start, end)
    else:
        arrival_at, departure_at = journey_boundaries(
            trip_context, start.isoformat(), end.isoformat()
        )
        derive_journey_temporal_shape(arrival_at, departure_at)
    overrides = {}
    if payload.start_at is not None:
        overrides["start_at"] = payload.start_at.isoformat()
    if payload.end_at is not None:
        overrides["end_at"] = payload.end_at.isoformat()
    if payload.budget is not None:
        overrides["budget_amount_minor"] = payload.budget.amount_minor
        overrides["budget_currency"] = payload.budget.currency
    try:
        gate = validate_selected_plan_feasibility(session, journey, journey_overrides=overrides)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if gate is not None and not gate.passed:
        failures = ", ".join(gate.failures)
        raise HTTPException(
            422, f"Journey update would make the selected plan infeasible: {failures}"
        )
    if payload.title is not None:
        journey.title = payload.title
    if payload.start_at is not None:
        journey.start_at = payload.start_at.isoformat()
    if payload.end_at is not None:
        journey.end_at = payload.end_at.isoformat()
    if payload.budget is not None:
        journey.budget_amount_minor, journey.budget_currency = (
            payload.budget.amount_minor,
            payload.budget.currency,
        )
    journey.updated_at = datetime.now(timezone.utc).isoformat()
    after = journey_schema(journey).model_dump(mode="json")
    record_audit(
        session,
        action="journey.updated",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        before=before,
        after=after,
    )
    session.commit()
    return journey_schema(journey)


@app.get("/api/v1/journeys/{journey_id}/constraints")
def list_constraints(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    items = session.scalars(
        select(JourneyConstraint)
        .where(JourneyConstraint.journey_id == str(journey_id))
        .order_by(JourneyConstraint.created_at)
    ).all()
    return [constraint_schema(item) for item in items]


@app.post("/api/v1/journeys/{journey_id}/constraints", status_code=201)
def add_constraint(
    journey_id: UUID, payload: ConstraintInput, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    # Two layers, in this order (arbiter `5778314066`): a rule the product has never heard of,
    # and a rule that is real but that the feasibility gate cannot execute as a hard constraint.
    # Both used to be persisted - `ConstraintInput.rule_code` is a free string - and the refusal
    # then surfaced much later as `UNSUPPORTED_HARD_CONSTRAINT`, which fails the whole plan
    # (`只补 severity 会把「静默不执行」变成「整趟旅程不可规划」`, HARNESS 22:31).  A write that
    # will be refused at evaluation must be refused here, where the caller still has a chance to
    # fix it, and nothing has been added to the session by the time this returns.
    refusal = constraint_write_refusal(payload.kind, payload.rule_code)
    if refusal is not None:
        return error(
            refusal,
            f"constraint rule_code {str(payload.rule_code)!r} with kind {payload.kind} is not "
            "writable: the feasibility gate has no branch that executes it",
            status=422,
            details={
                "rule_code": str(payload.rule_code),
                "kind": payload.kind,
                "category": payload.category,
                "reason_code": refusal,
                "canonical_rule_codes": sorted(CANONICAL_RULE_CODES),
            },
            message_key=(
                "constraint.unknown_rule_code"
                if refusal == "UNKNOWN_RULE_CODE"
                else "constraint.hard_rule_not_executable"
            ),
            reason_code=refusal,
        )
    now = datetime.now(timezone.utc).isoformat()
    item = JourneyConstraint(
        id=str(uuid4()),
        journey_id=str(journey_id),
        kind=payload.kind,
        category=payload.category,
        rule_code=payload.rule_code,
        params_json=json.dumps(payload.params, ensure_ascii=False),
        weight=payload.weight,
        active=True,
        created_at=now,
        updated_at=now,
    )
    if payload.kind == "HARD":
        try:
            gate = validate_selected_plan_feasibility(
                session,
                journey,
                additional_constraints=[item],
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if gate is not None and not gate.passed:
            failures = ", ".join(gate.failures)
            raise HTTPException(
                422, f"Constraint would make the selected plan infeasible: {failures}"
            )
    session.add(item)
    session.commit()
    record_audit(
        session,
        action="constraint.created",
        entity_type="Constraint",
        entity_id=item.id,
        journey_id=item.journey_id,
    )
    session.commit()
    return constraint_schema(item)


@app.delete("/api/v1/journeys/{journey_id}/constraints/{constraint_id}", status_code=204)
def delete_constraint(
    journey_id: UUID, constraint_id: UUID, session: Session = Depends(get_session)
):
    item = session.scalar(
        select(JourneyConstraint).where(
            JourneyConstraint.id == str(constraint_id),
            JourneyConstraint.journey_id == str(journey_id),
        )
    )
    if item is None:
        raise HTTPException(404, f"Constraint {constraint_id} was not found")
    before = constraint_schema(item).model_dump(mode="json")
    session.delete(item)
    record_audit(
        session,
        action="constraint.deleted",
        entity_type="Constraint",
        entity_id=item.id,
        journey_id=item.journey_id,
        before=before,
    )
    session.commit()


# Categories that describe server-owned schedule structure rather than a
# discoverable place.  They must never be offered as city content: choosing one
# produces an itinerary stop with no mobility evidence, so the plan cannot
# become authoritative truth (see Truth blocker B5).
_STRUCTURAL_PLACE_CATEGORIES = (
    "arrival",
    "departure",
    "check_in",
    "check_out",
    "stay",
    "luggage_drop",
    "transit",
)


@app.get("/api/v1/places", response_model=list[PlaceSchema])
def search_places(
    city_code: str = Query(...),
    category: str | None = None,
    q: str | None = None,
    include_structural: bool = Query(False),
    session: Session = Depends(get_session),
):
    statement = select(Place).where(Place.city_code == city_code)
    if not include_structural:
        statement = statement.where(
            Place.provider_key.notin_(_SERVER_OWNED_PLACE_PROVIDERS),
            func.lower(Place.category).notin_(_STRUCTURAL_PLACE_CATEGORIES),
        )
    items = session.scalars(statement.order_by(Place.id)).all()
    if category:
        items = [item for item in items if item.category.lower() == category.lower()]
    if q:
        needle = q.lower()
        items = [
            item
            for item in items
            if needle in item.name.lower() or needle in item.tags_json.lower()
        ]
    return [place_schema(item) for item in items]


@app.get("/api/v1/city-services")
def search_services(
    city_code: str = Query(...),
    service_type: str | None = None,
    session: Session = Depends(get_session),
):
    items = session.scalars(
        select(CityService).where(CityService.city_code == city_code).order_by(CityService.id)
    ).all()
    if service_type:
        items = [item for item in items if item.service_type.lower() == service_type.lower()]
    return [service_schema(item) for item in items]


@app.post("/api/v1/journeys/{journey_id}/plans/generate", status_code=201)
def generate_journey_plans(
    journey_id: UUID,
    payload: GeneratePlansRequest | None = None,
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """ORD-0272: a continuation has one durable owner, and it is the server's to answer.

    The second leg used to be a bare ``POST``: nothing said which mission the batch belonged
    to, so a resend, a lost response, a second tab or a second device could each mint a fresh
    set of candidates, and the only way to tell them apart afterwards was "same second" - which
    is not an identity (HJ-P3 measured that, ORD-0269 §2).  Here the transport key answers "is
    this the same send" and the mission-bound continuation answers "whose batch is this", kept as
    *two* keys on purpose: merged into one, "same mission, different request" and "same request,
    resent" become indistinguishable in storage - the exact class of mistake ORD-0262 was about.
    Without either, the route behaves exactly as before.

    Note for whoever adds the next parameter: a route function must not be called in-process.
    Its ``Header``/``Depends`` defaults are only resolved by FastAPI, so a direct call hands the
    body a sentinel instead of ``None``; the demo stage used to reach through here and made the
    stage advance with no plans at all (measured on 2026-09-24, visible only in a whole-suite A/B).
    ``_advance_steps`` now calls the generator itself.
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    start_request_budget(SCENARIO_PLANS_GENERATE)
    request = payload or GeneratePlansRequest()
    transport_key: str | None = None
    if idempotency_key is not None:
        try:
            transport_key = validate_idempotency_key(idempotency_key)
        except CommerceValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
    if request.mission_id is None and transport_key is None:
        try:
            plans = _generate_plans_answer(session, journey, request)
        except ValueError as exc:
            return _plan_generation_http_answer(_plan_generation_refusal(session, journey, exc))
        return [plan_schema(session, plan) for plan in plans]
    if request.mission_id is not None:
        _require_journeys_mission(session, journey, request.mission_id)

    continuation_id, keys, signature = _plan_continuation_identity(journey_id, journey, request,
                                                                   transport_key)
    with journey_write_lock(str(journey.id)):
        replayed = _replay_plan_generation(session, keys, signature)
        if replayed is not None:
            return _plan_generation_http_answer(replayed)
        now = datetime.now(timezone.utc)
        claim_token = uuid4().hex
        raced = _claim_plan_generation(keys, signature, session, now, claim_token)
        if raced is not None:
            return _plan_generation_http_answer(raced)
        answer = _execute_plan_generation_claimed(journey, request, keys, continuation_id,
                                                  now, claim_token)
    return _plan_generation_http_answer(answer)


def _require_journeys_mission(session: Session, journey: Journey, mission_id: str) -> None:
    """续跑身份只能绑**这条旅程自己的** mission。

    编造的 id 与别人旅程的 id 都按"不存在"回答：同一句、同一个码。这里若放行，落库的
    `plans.continuation.linked` 就是一条会说谎的关联，而 owner 恰恰要靠它读回。
    """

    mission = require(session, JourneyMission, str(mission_id), "JourneyMission")
    if mission.journey_id != str(journey.id):
        raise HTTPException(404, f"JourneyMission {mission_id} was not found")


def _plan_continuation_identity(journey_id: UUID, journey: Journey,
                                request: GeneratePlansRequest,
                                transport_key: str | None) -> tuple[str | None, list[str], str]:
    """两条身份、两把键，签名材料只有业务语义（键本身不进签名，否则换设备必然对不上）。"""

    continuation_id = f"cont:{request.mission_id}" if request.mission_id is not None else None
    signature = canonical_request_signature(
        {
            "journey_id": str(journey_id),
            "mission_id": request.mission_id,
            "planning_mode": request.planning_mode or journey.planning_mode,
            "candidate_count": request.candidate_count,
            "place_ids": request.place_ids,
        }
    )
    keys = [f"plans.generate:{transport_key}"] if transport_key is not None else []
    if continuation_id is not None and f"plans.generate:{continuation_id}" not in keys:
        keys.append(f"plans.generate:{continuation_id}")
    return continuation_id, keys, signature


def _replay_plan_generation(session: Session, keys: list[str],
                            signature: str, *, wait: bool = False) -> dict | None:
    """已经有人答过这把键 ⇒ 读那一份答案。签名不符由 `_replay_committed_accept` 说现成的 409。"""

    for key in keys:
        replayed = _replay_committed_accept(session, key, signature, wait=wait)
        if replayed is not None:
            return replayed
    return None


def _claim_plan_generation(keys: list[str], signature: str, session: Session,
                           now: datetime, token: str) -> dict | None:
    """Phase A CLAIM：一把或多把 NULL 占位**先独立提交**，唯一索引在生成之前裁决所有权。

    抢不到 ⇒ 只读别人的答案；等不到 ⇒ 现成那句 409。绝不删占位、绝不按 expires_at 回收、
    绝不在别人那发上重跑生成 —— 恢复的前提是"证明不会重复既有副作用"，这里证明不了。
    """

    with engine.connect() as claim_conn:
        claim_tx = claim_conn.begin()
        try:
            for key in keys:
                claim_conn.execute(
                    insert(IdempotencyKey).values(
                        key=key,
                        operation="plans.generate",
                        request_hash=signature,
                        response_status=None,
                        response_json=None,
                        created_at=now.isoformat(),
                        expires_at=(now + timedelta(days=7)).isoformat(),
                        claim_token=token,
                        claim_deadline=_lease_deadline(now),
                    )
                )
            with write_attempt(SCENARIO_PLANS_GENERATE):
                claim_tx.commit()
        except IntegrityError as exc:
            claim_tx.rollback()
            raced = _replay_plan_generation(session, keys, signature, wait=True)
            if raced is not None:
                return raced
            if _take_over_lapsed_claims(session, keys, now, token):
                return None
            raise _plan_claim_in_flight(session, keys, now) from exc
    return None


PLAN_CLAIM_LEASE_MS = int(settings.write_deadline_ms) + int(settings.write_busy_timeout_ms)


def _lease_deadline(now: datetime) -> str:
    """When this attempt stops protecting its own claim.

    Derived from the two budgets this route already obeys - its declared write deadline plus the
    SQLite lock wait it may spend inside that budget - so an honest attempt cannot outlive its own
    lease. If a knob is tightened later, the error direction is a longer lease: that delays a
    take-over rather than stealing a claim that is still being written.
    """

    return (now + timedelta(milliseconds=PLAN_CLAIM_LEASE_MS)).isoformat()


def _lease_expiry(row: IdempotencyKey) -> datetime:
    """A receipt written before leases existed is judged by the instant its Phase A committed,
    which is exactly what `created_at` records."""

    if row.claim_deadline:
        return datetime.fromisoformat(row.claim_deadline)
    return datetime.fromisoformat(row.created_at) + timedelta(milliseconds=PLAN_CLAIM_LEASE_MS)


def _take_over_lapsed_claims(session: Session, keys: list[str], now: datetime,
                             token: str) -> bool:
    """Take over a claim only once its own lease has lapsed, and only by a conditional write that
    fails if another reclaimer moved first. Every key is won, or none is.

    This cannot duplicate work: a receipt still NULL carries no durable candidate, because Phase B
    commits its candidates and its receipt in the same transaction. Measured in
    evidence/ord0340-claim-lease-S2/reclaim_premise_r1.json.
    """

    won = 0
    for key in keys:
        session.expire_all()
        row = session.get(IdempotencyKey, key)
        if row is None or row.response_json is not None:
            session.rollback()
            return False
        if _lease_expiry(row) > now:
            session.rollback()
            return False
        conditions = [IdempotencyKey.key == key, IdempotencyKey.response_json.is_(None)]
        if row.claim_deadline is None:
            conditions += [IdempotencyKey.claim_deadline.is_(None),
                           IdempotencyKey.created_at == row.created_at]
        else:
            conditions += [IdempotencyKey.claim_deadline == row.claim_deadline]
        took = session.execute(
            update(IdempotencyKey).where(*conditions)
            .values(claim_token=token, claim_deadline=_lease_deadline(now))
            .execution_options(synchronize_session=False)
        ).rowcount
        if took != 1:
            session.rollback()
            return False
        won += 1
    if won == 0 or won != len(keys):
        session.rollback()
        return False
    session.commit()
    return True


def _held_claim_view(session: Session, keys: list[str], now: datetime) -> dict:
    """What the attempt that still holds the claim can honestly tell a caller."""

    for key in keys:
        session.expire_all()
        row = session.get(IdempotencyKey, key)
        if row is None or row.response_json is not None:
            continue
        return {
            "retry_after_ms": max(0, int((_lease_expiry(row) - now).total_seconds() * 1000)),
            "attempt_id": row.claim_token,
            "lease_ms": PLAN_CLAIM_LEASE_MS,
        }
    return {"retry_after_ms": PLAN_CLAIM_LEASE_MS, "attempt_id": None,
            "lease_ms": PLAN_CLAIM_LEASE_MS}


def _plan_claim_in_flight(session: Session, keys: list[str], now: datetime) -> HTTPException:
    """Another attempt still holds an unexpired lease.

    The sentence stays what three already-accepted gates pin, so nothing agreed is quietly
    reworded; what is new is the name and the numbers a client needs in order to say *why* and
    *when to come back*. `attempt_id` is read off the durable row, not invented here.
    """

    exc = HTTPException(409, "agent idempotency record is incomplete")
    exc.reason_code = "PLAN_GENERATION_IN_FLIGHT"
    exc.message_key = "plan_generation.in_flight"
    exc.error_details = _held_claim_view(session, keys, now)
    return exc


def _plan_claim_superseded(token: str | None, expected: int, filled: int) -> HTTPException:
    """This attempt lost its claim, so its answer must not land on top of the winner's."""

    exc = HTTPException(409, "agent idempotency record is incomplete")
    exc.reason_code = "PLAN_GENERATION_ATTEMPT_SUPERSEDED"
    exc.message_key = "plan_generation.attempt_superseded"
    exc.error_details = {"attempt_id": token, "keys_expected": expected, "keys_filled": filled}
    return exc


def _execute_plan_generation_claimed(journey: Journey, request: GeneratePlansRequest,
                                     keys: list[str], continuation_id: str | None,
                                     now: datetime, token: str | None = None) -> dict:
    """Phase B EXECUTE：候选、审计链接与"占位 → 完成回执"在同一次可回滚提交里。

    回执存的是**渲染失败要用到的那几样输入**（状态/原话/具名理由），不是另抄一份错误正文：
    重发时仍由同一个 handler 渲染，所以"复用现成信封"不是我的口号而是唯一的一条路。
    """

    with write_attempt(SCENARIO_PLANS_GENERATE), engine.begin() as work_conn:
        work_session = Session(bind=work_conn, expire_on_commit=False)
        try:
            try:
                plans = _generate_plans_answer(work_session, journey, request)
            except ValueError as exc:
                answer = _plan_generation_refusal(work_session, journey, exc)
            else:
                feasible_ids = [plan.id for plan in plans]
                if continuation_id is not None:
                    record_audit(
                        work_session,
                        action="plans.continuation.linked",
                        entity_type="JourneyMission",
                        entity_id=str(request.mission_id),
                        journey_id=journey.id,
                        actor_type="USER",
                        after={"continuation_id": continuation_id,
                               "mission_id": request.mission_id,
                               "plan_version_ids": feasible_ids,
                               "summary": "this mission's continuation produced these plans"},
                    )
                answer = {"plans": [plan_schema(work_session, plan).model_dump(
                    mode="json", by_alias=True) for plan in plans]}
            filled = 0
            for key in keys:
                fill_conditions = [IdempotencyKey.key == key,
                                   IdempotencyKey.response_json.is_(None)]
                if token is not None:
                    # ORD-0340: only the attempt that holds the lease may fill it, so a worker
                    # that wakes up after being taken over cannot write its answer over the one
                    # that replaced it. The direct Phase-B call an already-accepted gate uses
                    # keeps its pre-lease shape rather than being silently repurposed.
                    fill_conditions.append(IdempotencyKey.claim_token == token)
                filled += work_session.execute(
                    update(IdempotencyKey)
                    .where(*fill_conditions)
                    .values(response_status=422 if "refusal" in answer else 201,
                            response_json=json.dumps(answer, sort_keys=True))
                    .execution_options(synchronize_session=False)
                ).rowcount
            if filled != len(keys):
                # 只允许 NULL → 完成：覆盖别人填好的回执，就是拿别人的答案冒充自己的
                raise _plan_claim_superseded(token, len(keys), filled)
            return answer
        finally:
            work_session.close()


def _plan_generation_refusal(session: Session, journey: Journey, exc: ValueError) -> dict:
    """这一发唯一的失败表达，两条路径共用（0272 不许有第二套）。

    今天的路由在拒绝时做两件事，这里一件都不许少：把"没有可行替代"写成**耐久事实**，
    并把原话/状态/具名理由/计数细节交给**现成的** handler 渲染。存的是渲染所需的输入而非
    渲染好的正文，所以重发出来的仍是同一句话、同一个 `reason_code`。
    """

    record_replan_outcome(session, journey, feasible_candidate_ids=[], failures=[str(exc)])
    session.commit()
    return {"refusal": {
        "status": 422,
        "detail": str(exc),
        "reason_code": getattr(exc, "reason_code", None),
        "error_details": getattr(exc, "details", None),
    }}


def _plan_generation_http_answer(answer: dict):
    """把存下来的那一份答案交出去：成功是同一批方案，拒绝走**同一个** handler 渲染。"""

    refusal = answer.get("refusal")
    if refusal is not None:
        raised = HTTPException(refusal["status"], refusal["detail"])
        if refusal.get("reason_code"):
            raised.reason_code = refusal["reason_code"]
            raised.error_details = refusal.get("error_details")
        raise raised
    return JSONResponse(status_code=201, content=answer["plans"])


def _generate_plans_answer(session: Session, journey: Journey,
                           request: GeneratePlansRequest) -> list[PlanVersion]:
    """今天这一段的本体：一次生成 + 那次结果的耐久事实。被 Phase B 用同一条事务包住。"""

    planning_mode = request.planning_mode or journey.planning_mode
    plans = generate_plans(
        session,
        journey,
        planning_mode,
        request.candidate_count,
        place_ids=request.place_ids,
    )
    record_replan_outcome(
        session,
        journey,
        feasible_candidate_ids=[plan.id for plan in plans],
    )
    session.commit()
    return plans


@app.post("/api/v1/journeys/{journey_id}/plans/authoritative-compose", status_code=201)
@app.post("/api/v1/journeys/{journey_id}/authoritative-compose", status_code=201)
def authoritative_compose_journey(
    journey_id: UUID,
    payload: AuthoritativeComposeRequest,
    session: Session = Depends(get_session),
):
    """Compose bounded multi-day candidates from actual user-selected POIs."""

    journey = require(session, Journey, str(journey_id), "Journey")
    try:
        result = compose_authoritative_multiday(
            session,
            journey,
            place_ids=payload.place_ids,
            candidate_count=payload.candidate_count,
            stay_offer_id=str(payload.stay_offer_id) if payload.stay_offer_id else None,
            reservation_id=str(payload.reservation_id) if payload.reservation_id else None,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    return {
        "optimizer": result.optimizer_kind,
        "complete_plan_count": len(result.plans),
        "frozen_evidence_ids": list(result.evidence_ids),
        "stay_offer_id": result.stay_offer.id if result.stay_offer else None,
        "top_k": [plan_schema(session, plan) for plan in result.plans],
    }


@app.get("/api/v1/journeys/{journey_id}/plans")
def list_plans(journey_id: UUID, session: Session = Depends(get_session)):
    """Every version this journey has ever had, with the one it runs named explicitly.

    The list stays unfiltered on purpose: CANDIDATE, SELECTED, ACTIVE, SUPERSEDED and REJECTED
    rows are all part of what a caller may show or audit.  What used to be missing - and what
    every consumer was guessing at from array position, which is how "the list is visible but
    nothing is pressable" and the 33/35 cross-test pollution both got their shape (ORD-0097) -
    is *which row is now*.  `is_current` answers it from `journey.active_plan_version_id`, the
    system's only statement of that fact: not from `status`, not from order, not from
    `version_no`.  A journey with no pointer gets `False` on every row rather than a default
    first item.
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    items = session.scalars(
        select(PlanVersion)
        .where(PlanVersion.journey_id == str(journey_id))
        .order_by(PlanVersion.version_no)
    ).all()
    return [
        plan_schema(session, item, current_plan_version_id=journey.active_plan_version_id)
        for item in items
    ]


@app.get("/api/v1/plans/{plan_version_id}")
def get_plan(plan_version_id: UUID, session: Session = Depends(get_session)):
    return plan_schema(session, require(session, PlanVersion, str(plan_version_id), "PlanVersion"))


@app.post("/api/v1/journeys/{journey_id}/plans/{plan_version_id}/select")
def select_plan(journey_id: UUID, plan_version_id: UUID, session: Session = Depends(get_session)):
    journey = session.scalar(
        select(Journey)
        .where(Journey.id == str(journey_id))
        .execution_options(populate_existing=True)
    )
    if journey is None:
        raise HTTPException(404, f"Journey {journey_id} was not found")
    plan = session.scalar(
        select(PlanVersion).where(
            PlanVersion.id == str(plan_version_id), PlanVersion.journey_id == journey.id
        )
    )
    if plan is None:
        raise HTTPException(404, f"PlanVersion {plan_version_id} was not found")
    # Selecting the already-current selected plan is a safe idempotent read.
    # Do this before the normal transition/audit path so retries do not emit a
    # second plan.selected audit event.
    if plan.status == "SELECTED" and journey.active_plan_version_id == plan.id:
        return journey_schema(journey)
    if plan.status not in {"CANDIDATE", "SELECTED"}:
        raise HTTPException(422, "Only a candidate plan can be selected")
    # Selecting a different plan leaves the one the traveller may have been executing.
    # Retire it here, in the same transaction, with a predicate the database evaluates
    # against the current row.  Without this the abandoned plan stays ACTIVE with
    # nothing pointing at it: still one ACTIVE row, so the unique index cannot see the
    # corruption, and no projection can explain which plan the Journey is running.
    abandoned_id = (
        journey.active_plan_version_id
        if journey.active_plan_version_id != plan.id
        else None
    )
    constraints = _journey_constraints_with_capabilities(session, journey, active_only=True)
    places = {
        place.id: place
        for place in session.scalars(
            select(Place).where(Place.city_code == journey.city_code)
        ).all()
    }
    services = {
        service.id: service
        for service in session.scalars(
            select(CityService).where(CityService.city_code == journey.city_code)
        ).all()
    }
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    evidence_payload = json.loads(plan.mobility_evidence_json or "{}")
    if not evidence_payload.get("snapshot_id"):
        raise HTTPException(409, "STALE_RECOMPOSE_REQUIRED: PlanVersion has no frozen provider evidence")
    expires_at = evidence_payload.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(str(expires_at)) <= datetime.now(timezone.utc):
                raise HTTPException(409, "STALE_RECOMPOSE_REQUIRED: PlanVersion evidence expired")
        except ValueError:
            raise HTTPException(409, "STALE_RECOMPOSE_REQUIRED: PlanVersion evidence timestamp invalid") from None
    selection_snapshot = MobilitySnapshot.from_evidence(evidence_payload)
    if selection_snapshot.snapshot_id != plan.mobility_snapshot_id:
        raise HTTPException(409, "STALE_RECOMPOSE_REQUIRED: PlanVersion evidence reference is inconsistent")
    gate = evaluate_feasibility(
        build_context(
            journey,
            constraints,
            places,
            services,
            mobility_provider=selection_snapshot,
            plan_currency=plan.currency,
        ),
        [activity_from_record(item) for item in activities],
    )
    if not gate.passed:
        failures = ", ".join(gate.failures)
        raise HTTPException(422, f"PlanVersion {plan.id} is infeasible: {failures}")

    current = session.scalar(
        select(PlanVersion).where(
            PlanVersion.journey_id == journey.id,
            PlanVersion.status == "SELECTED",
            PlanVersion.id != plan.id,
        )
    )
    before = {
        "journey": journey_schema(journey).model_dump(mode="json"),
        "plan_status": plan.status,
        "previous_selected_plan_id": current.id if current else None,
    }
    if current:
        current.status = "REJECTED"
    plan.status = "SELECTED"
    # Bind durable commitments to the selected PlanVersion's canonical
    # Activity row.  The semantic key remains the cross-version identity; the
    # FK is the concrete execution anchor consumed by JourneyTwin, Trip
    # Studio, and recovery remapping.
    selected_commitments = session.scalars(
        select(CommitmentModel).where(
            CommitmentModel.journey_id == journey.id,
            CommitmentModel.status == "ACTIVE",
            CommitmentModel.level.in_(("C3_Guaranteed", "C4_Anchored")),
        )
    ).all()
    for commitment in selected_commitments:
        policy = _json_object(commitment.policy_json, {})
        semantic_key = str(policy.get("semantic_key") or "")
        match = next(
            (
                item
                for item in activities
                if semantic_key
                and (
                    item.place_id == semantic_key
                    or item.city_service_id == semantic_key
                    or str(_json_object(item.metadata_json, {}).get("semantic_key") or "") == semantic_key
                    or str(_json_object(item.metadata_json, {}).get("semantic_key") or "").endswith(f":{semantic_key}")
                )
            ),
            None,
        )
        if match is not None:
            commitment.activity_id = match.id
    if abandoned_id is not None:
        # The pointer does not always name a plan the traveller is *executing*. A journey
        # that was prepared but never started points at a SELECTED row, and that row is
        # already retired to REJECTED above; running the guarded ACTIVE -> SUPERSEDE against
        # it matched zero rows and answered 409 "the plan being replaced is no longer the
        # ACTIVE plan" - which is what made switching a selection impossible on the demo
        # stage (measured while writing the ORD-0097 gate that moves the pointer). The guard
        # stays exactly as strict for the case it was written for.
        abandoned = session.get(PlanVersion, abandoned_id)
        if abandoned is not None and abandoned.status == "ACTIVE":
            retired = session.execute(
                update(PlanVersion)
                .where(PlanVersion.id == abandoned_id, PlanVersion.status == "ACTIVE")
                .values(status="SUPERSEDED")
            )
            if retired.rowcount != 1:
                session.rollback()
                raise HTTPException(
                    409, "the plan being replaced is no longer the ACTIVE plan"
                )
            # The cursor named a step on the plan being abandoned.  Leaving it there would
            # be exactly the stale-cursor state the projection contract refuses to render,
            # so it moves to the first pending step of the plan now selected.  Only the
            # executing case has a cursor to move: a PLANNED journey that was never started
            # keeps no cursor at all, and ORD-0078's stage contract depends on that.
            journey.current_activity_id = next(
                (
                    item.id
                    for item in session.scalars(
                        select(Activity)
                        .where(Activity.plan_version_id == plan.id)
                        .order_by(Activity.sequence_no)
                    ).all()
                    if item.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
                ),
                None,
            )
    journey.status = "PLANNED"
    journey.active_plan_version_id = plan.id
    journey.updated_at = datetime.now(timezone.utc).isoformat()
    # The plan just passed the same feasibility gate used by generate/start, so
    # any open recovery is genuinely over.  Resolve it in the same transaction
    # so the Event, /experience and the phone UI cannot disagree.
    resolved_recovery = resolve_recovery_events(session, journey)
    after = {
        "journey": journey_schema(journey).model_dump(mode="json"),
        "plan_status": plan.status,
        "previous_selected_plan_id": current.id if current else None,
        "resolved_recovery_events": resolved_recovery,
    }
    record_audit(
        session,
        action="plan.selected",
        entity_type="PlanVersion",
        entity_id=plan.id,
        journey_id=journey.id,
        before=before,
        after=after,
    )
    session.commit()
    return journey_schema(journey)


def _trip_studio_context(
    session: Session,
    journey: Journey,
    requested_ids: list[str],
) -> tuple[PlanVersion, list[Activity], list[Activity], dict[str, Any]]:
    """Resolve an edit against the current plan and the immutable evidence snapshot."""

    if not journey.active_plan_version_id:
        raise HTTPException(409, "Trip Studio requires an active plan")
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    if plan is None or plan.journey_id != journey.id:
        raise HTTPException(409, "Trip Studio active plan is inconsistent")
    expected_status = "ACTIVE" if journey.status == "ACTIVE" else "SELECTED"
    if plan.status != expected_status:
        raise HTTPException(409, "TRIP_STUDIO_BASE_PLAN_STALE")
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    canonical_ids = [item.id for item in activities]
    if len(requested_ids) != len(set(requested_ids)) or set(requested_ids) != set(canonical_ids):
        raise HTTPException(422, "TRIP_STUDIO_REQUIRES_FULL_ACTIVE_PLAN_ORDER")
    by_id = {item.id: item for item in activities}
    ordered = [by_id[item] for item in requested_ids]
    locked_positions = [item.id for item in activities if item.locked]
    proposed_locked_positions = [item.id for item in ordered if item.locked]
    protected_ok = locked_positions == proposed_locked_positions and all(
        activities[index].id == ordered[index].id
        for index in range(len(activities))
        if activities[index].locked
    )
    execution_failures: list[str] = []
    if journey.status == "ACTIVE":
        current_index = next(
            (index for index, item in enumerate(activities) if item.id == journey.current_activity_id),
            None,
        )
        if current_index is None:
            execution_failures.append("CURRENT_ACTIVITY_MISSING")
        else:
            immutable_prefix = canonical_ids[: current_index + 1]
            if requested_ids[: current_index + 1] != immutable_prefix:
                execution_failures.append("EXECUTED_PREFIX_REORDER")
            if requested_ids[current_index] != journey.current_activity_id:
                execution_failures.append("CURRENT_ACTIVITY_REORDER")
        for index, item in enumerate(activities):
            if item.status in {"COMPLETED", "SKIPPED"} and requested_ids[index] != item.id:
                execution_failures.append("EXECUTION_HISTORY_REORDER")
    evidence = _json_object(plan.mobility_evidence_json, {})
    fact_pairs = {
        (str(item.get("origin_key")), str(item.get("destination_key"))): item
        for item in evidence.get("facts", [])
        if isinstance(item, dict) and item.get("origin_key") and item.get("destination_key")
    }
    route_gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        pair = (str(previous.place_id or previous.city_service_id or ""), str(current.place_id or current.city_service_id or ""))
        if pair not in fact_pairs:
            route_gaps.append({"from": pair[0], "to": pair[1]})
    changed = canonical_ids != requested_ids
    authoritative_gate = _trip_studio_authoritative_gate(
        session,
        journey,
        plan,
        ordered,
        activities,
        evidence,
    )
    gate_failures = list(execution_failures)
    if not protected_ok:
        gate_failures.append("PROTECTED_ACTIVITY_REORDER")
    if route_gaps:
        gate_failures.append("FROZEN_ROUTE_EVIDENCE_MISSING")
    gate_failures.extend(authoritative_gate["failures"])
    gate_failures = list(dict.fromkeys(gate_failures))
    impact = {
        "changed": changed,
        "changed_activity_ids": [item for item in requested_ids if item != canonical_ids[requested_ids.index(item)]] if changed else [],
        "protected_activity_ids": locked_positions,
        "protected_commitments_preserved": protected_ok and not execution_failures,
        "route_evidence_gaps": route_gaps,
        "time_delta_min": authoritative_gate["time_delta_min"],
        "cost_delta_minor": authoritative_gate["cost_delta_minor"],
        "risk_class": "LOW" if not gate_failures else "BLOCKED",
    }
    gate = {
        "status": "PASSED" if not gate_failures else "FAILED",
        "failures": gate_failures,
        "evidence_snapshot_id": plan.mobility_snapshot_id,
        "authoritative": True,
        "evidence_valid": authoritative_gate["evidence_valid"],
    }
    return plan, activities, ordered, {
        "impact": impact,
        "gate": gate,
        "evidence": evidence,
        "authoritative_gate": authoritative_gate,
    }


def _trip_studio_fact(
    from_key: str,
    to_key: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    """Select one fact from the persisted immutable evidence matrix."""

    selected = [
        dict(item)
        for item in evidence.get("selected_facts", [])
        if isinstance(item, dict)
        and str(item.get("origin_key")) == from_key
        and str(item.get("destination_key")) == to_key
    ]
    matrix = [
        dict(item)
        for item in evidence.get("facts", [])
        if isinstance(item, dict)
        and str(item.get("origin_key")) == from_key
        and str(item.get("destination_key")) == to_key
    ]
    candidates = selected + matrix
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            0 if item in selected else 1,
            str(item.get("cost_status") or "UNKNOWN") == "UNKNOWN",
            int(item.get("duration_min", item.get("minutes", 0)) or 0),
            int(item.get("cost_amount_minor") or 0),
            str(item.get("mode") or ""),
        ),
    )


def _trip_studio_authoritative_gate(
    session: Session,
    journey: Journey,
    plan: PlanVersion,
    ordered: list[Activity],
    slots: list[Activity],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Run the shared deterministic gate over a Trip Studio frozen snapshot."""

    failures: list[str] = []
    evidence_valid = bool(evidence.get("snapshot_id")) and evidence.get("snapshot_id") == plan.mobility_snapshot_id
    if not evidence_valid:
        failures.append("FROZEN_EVIDENCE_ID_MISMATCH")
    integrity_hash = evidence.get("integrity_hash")
    if integrity_hash:
        integrity_payload = dict(evidence)
        integrity_payload.pop("integrity_hash", None)
        computed_hash = hashlib.sha256(
            json.dumps(
                integrity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if computed_hash != str(integrity_hash):
            failures.append("FROZEN_EVIDENCE_INTEGRITY_MISMATCH")
            evidence_valid = False
    expires_at = evidence.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(str(expires_at)) <= datetime.now(timezone.utc):
                failures.append("STALE_FROZEN_EVIDENCE")
                evidence_valid = False
        except ValueError:
            failures.append("INVALID_FROZEN_EVIDENCE_EXPIRY")
            evidence_valid = False

    frozen_minutes: dict[tuple[str, str], int] = {}
    frozen_facts: list[dict[str, Any]] = []
    feasibility_activities: list[FeasibilityActivity] = []
    for index, activity in enumerate(ordered):
        slot = slots[index]
        previous = ordered[index - 1] if index else None
        travel_min = 0
        if previous is not None:
            from_key = str(previous.place_id or previous.city_service_id or "")
            to_key = str(activity.place_id or activity.city_service_id or "")
            fact = _trip_studio_fact(from_key, to_key, evidence)
            if fact is None:
                failures.append(f"MOBILITY_DATA_UNAVAILABLE:{from_key}|{to_key}")
            else:
                travel_min = int(fact.get("duration_min", fact.get("minutes", 0)) or 0)
                frozen_minutes[(from_key, to_key)] = travel_min
                frozen_facts.append(fact)
                if str(fact.get("cost_status") or "UNKNOWN") == "UNKNOWN" and fact.get("cost_amount_minor") is None:
                    failures.append(f"UNKNOWN_MOBILITY_COST:{from_key}|{to_key}")
                if fact.get("currency") and str(fact["currency"]).upper() != str(journey.budget_currency).upper():
                    failures.append(f"MOBILITY_CURRENCY_MISMATCH:{from_key}|{to_key}")
        try:
            start_at = datetime.fromisoformat(slot.start_at)
            end_at = datetime.fromisoformat(slot.end_at)
        except ValueError:
            failures.append(f"INVALID_ACTIVITY_WINDOW:{activity.id}")
            continue
        feasibility_activities.append(
            FeasibilityActivity(
                key=str(activity.place_id or activity.city_service_id or activity.id),
                start_at=start_at,
                end_at=end_at,
                cost_minor=int(activity.estimated_cost_minor),
                place_id=activity.place_id,
                city_service_id=activity.city_service_id,
                currency=activity.currency,
                travel_from_prev_min=travel_min,
                locked=bool(activity.locked),
            )
        )

    try:
        constraints = _journey_constraints_with_capabilities(session, journey)
        commitments = list(
            session.scalars(
                select(CommitmentModel).where(
                    CommitmentModel.journey_id == journey.id,
                    CommitmentModel.status == "ACTIVE",
                    CommitmentModel.level == "C4_Anchored",
                )
            ).all()
        )
        constraints.extend(active_c4_hard_constraints(commitments))
    except ValueError:
        failures.append("INVALID_C4_COMMITMENT_POLICY")
        constraints = []
    places = {
        item.id: item
        for item in session.scalars(select(Place).where(Place.city_code == journey.city_code)).all()
    }
    services = {
        item.id: item
        for item in session.scalars(select(CityService).where(CityService.city_code == journey.city_code)).all()
    }
    frozen_provider = MobilitySnapshot.from_minutes(
        source_provider_key=str(evidence.get("source_provider") or "frozen-plan-evidence"),
        minutes=frozen_minutes,
        captured_at=str(evidence.get("captured_at") or datetime.now(timezone.utc).isoformat()),
        snapshot_id=str(evidence.get("snapshot_id") or plan.mobility_snapshot_id or ""),
        operation_key=str(evidence.get("operation_key") or "trip-studio"),
    )
    gate = evaluate_feasibility(
        build_context(
            journey,
            constraints,
            places,
            services,
            mobility_provider=frozen_provider,
            plan_currency=plan.currency,
        ),
        feasibility_activities,
    )
    failures.extend(gate.failures)
    activity_duration = sum(
        int((datetime.fromisoformat(item.end_at) - datetime.fromisoformat(item.start_at)).total_seconds() // 60)
        for item in slots
    )
    route_duration = sum(int(item.get("duration_min", item.get("minutes", 0)) or 0) for item in frozen_facts)
    base_duration = activity_duration + route_duration
    base_cost = sum(int(item.estimated_cost_minor) for item in ordered) + sum(
        int(item.get("cost_amount_minor") or 0) for item in frozen_facts
    )
    time_delta = base_duration - int(plan.estimated_duration_min or 0)
    cost_delta = base_cost - int(plan.total_cost_minor or 0)
    return {
        "failures": list(dict.fromkeys(failures)),
        "evidence_valid": evidence_valid,
        "time_delta_min": time_delta,
        "cost_delta_minor": cost_delta,
        "frozen_facts": frozen_facts,
        "frozen_provider": frozen_provider,
        "mobility_cost_minor": sum(int(item.get("cost_amount_minor") or 0) for item in frozen_facts),
        "total_cost_minor": base_cost,
        "estimated_duration_min": base_duration,
    }


def _trip_studio_preview_payload(
    journey: Journey,
    plan: PlanVersion,
    requested_ids: list[str],
    details: dict[str, Any],
) -> dict[str, Any]:
    signature = json.dumps(
        {"plan_id": plan.id, "evidence_id": plan.mobility_snapshot_id, "draft_order": requested_ids},
        sort_keys=True,
    )
    authoritative_gate = details["authoritative_gate"]
    return {
        "preview_id": str(uuid5(NAMESPACE_URL, f"citynexus-v13-trip-studio:{signature}")),
        "journey_id": journey.id,
        "base_plan_version_id": plan.id,
        "draft_order": requested_ids,
        "impact": details["impact"],
        "deterministic_gate": details["gate"],
        "evidence_snapshot_id": details["gate"]["evidence_snapshot_id"],
        "authoritative_gate": {
            "failures": authoritative_gate["failures"],
            "evidence_valid": authoritative_gate["evidence_valid"],
            "time_delta_min": authoritative_gate["time_delta_min"],
            "cost_delta_minor": authoritative_gate["cost_delta_minor"],
            "frozen_facts": authoritative_gate["frozen_facts"],
            "mobility_cost_minor": authoritative_gate["mobility_cost_minor"],
            "total_cost_minor": authoritative_gate["total_cost_minor"],
            "estimated_duration_min": authoritative_gate["estimated_duration_min"],
        },
        "can_accept": details["gate"]["status"] == "PASSED",
        "mutation": "NO_ACTIVE_PLAN_MUTATION",
    }


# --- ORD-0437: POI text/link import -------------------------------------------------------------


@app.post("/api/v1/journeys/{journey_id}/poi-import/parse", response_model=PoiImportParseResponse)
def poi_import_parse(
    journey_id: UUID,
    payload: PoiImportParseRequest,
    session: Session = Depends(get_session),
):
    """Extract the places a traveller pasted, with nothing but this process and our own rows.

    The reference chain reads a 小红书 / 公众号 / 飞书 link by fetching it. This door does not
    fetch: a link is answered by naming the form that is not supported and the hosts it saw, while
    any text sitting next to it is still parsed. That keeps the extraction channel local by
    construction rather than by a flag somebody could flip.
    """
    journey = session.get(Journey, str(journey_id))
    if journey is None:
        refused = HTTPException(404, f"Journey {journey_id} was not found")
        refused.reason_code = "JOURNEY_NOT_FOUND"
        refused.error_details = {"journey_id": str(journey_id)}
        raise refused
    text = payload.text or ""
    forms = list(poi_import_detect_forms(text))
    unsupported = [f for f in forms if f != poi_import_FORM_PASTED_TEXT]
    # The city's schedule nodes (arrival / check-in / stay / departure rows) are places in the table
    # but not places a traveller can choose to visit -- that is the rule
    # `_STRUCTURAL_PLACE_CATEGORIES` states for itself above, written after a previous journey's node
    # got into a candidate pool and the recovery leg lost its identity.  This door is a new way into
    # the same plan, so it reads from the same filtered set: a pasted word that only matches a
    # schedule node answers UNRESOLVED, which is the truth, instead of becoming a visit on a hotel
    # check-in.
    # The provider filter `GET /api/v1/places` also applies is deliberately NOT copied here: on this
    # build every CN-TIANJIN row carries provider_key 'citynexus-v9-domain' (9 of 9 measured), so
    # applying it would empty the catalogue and the screen would say "nothing matched" about real
    # sightseeing places.
    rows = session.scalars(
        select(Place)
        .where(Place.city_code == journey.city_code)
        .where(func.lower(Place.category).notin_(_STRUCTURAL_PLACE_CATEGORIES))
        .order_by(Place.name)
    ).all()
    catalogue = [
        {
            "id": row.id,
            "name": row.name,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "coordinate_system": getattr(row, "coordinate_system", None),
            "aliases": getattr(row, "tags_json", None),
        }
        for row in rows
    ]
    resolved = poi_import_resolve(poi_import_extract(text), catalogue)
    return PoiImportParseResponse(
        source_channel="LOCAL_TEXT_PARSER",
        forms=forms,
        unsupported_forms=unsupported,
        link_hosts=list(poi_import_link_hosts(text)),
        counts=poi_import_counts(resolved),
        items=[
            PoiImportItem(
                title=item.title,
                note=item.note,
                char_count=item.char_count,
                status=item.status,
                place_id=item.place_id,
                place_name=item.place_name,
                latitude=item.latitude,
                longitude=item.longitude,
                coordinate_system=item.coordinate_system,
                matches=list(item.matches),
                addable=item.status == poi_import_STATUS_RESOLVED,
            )
            for item in resolved
        ],
        char_count=len(text),
    )


@app.post(
    "/api/v1/journeys/{journey_id}/poi-import/add",
    response_model=PoiImportAddResponse,
    status_code=201,
)
def poi_import_add(
    journey_id: UUID,
    payload: PoiImportAddRequest,
    session: Session = Depends(get_session),
):
    """Append the chosen places to the journey's active plan, and say where to read them back.

    No Idempotency-Key header: whether a place is already on that day is durable state, so a second
    tap reports it under already_present instead of minting a second success -- the rule the
    recycle-bin door states for itself. Replaying this write cannot double-insert.

    The day is part of the answer together with the reason it is that day. A day nobody chose still
    moves the traveller's itinerary, so the panel has to say so before it offers the button.
    """
    journey = session.get(Journey, str(journey_id))
    if journey is None:
        refused = HTTPException(404, f"Journey {journey_id} was not found")
        refused.reason_code = "JOURNEY_NOT_FOUND"
        refused.error_details = {"journey_id": str(journey_id)}
        raise refused
    plan_version_id = journey.active_plan_version_id
    plan = session.get(PlanVersion, plan_version_id) if plan_version_id else None
    if plan is None:
        refused = HTTPException(409, "This journey has no active plan to add places to yet")
        refused.reason_code = "NO_ACTIVE_PLAN"
        refused.error_details = {"journey_id": str(journey_id)}
        raise refused

    existing = list(
        session.scalars(
            select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
        ).all()
    )
    existing_dates = sorted({row.start_at[:10] for row in existing if row.start_at})
    if payload.day:
        day, day_source = payload.day, "TRAVELLER_CHOSEN"
    elif existing_dates:
        day, day_source = existing_dates[0], "JOURNEY_FIRST_DAY"
    else:
        refused = HTTPException(422, "The plan has no day to land on; choose one")
        refused.reason_code = "IMPORT_DAY_REQUIRED"
        refused.error_details = {"journey_id": str(journey_id)}
        raise refused

    try:
        zone = ZoneInfo(journey.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    now = datetime.now(timezone.utc)
    places = {row.id: row for row in session.scalars(select(Place)).all()}
    sequence = max([row.sequence_no for row in existing], default=0)
    added: list[PoiImportAddedActivity] = []
    already_present: list[str] = []
    for place_id in payload.place_ids:
        place = places.get(place_id)
        if place is None:
            refused = HTTPException(422, f"Unknown place {place_id}")
            refused.reason_code = "IMPORT_PLACE_UNKNOWN"
            refused.error_details = {"place_id": place_id}
            raise refused
        # The read door cannot offer a schedule node or another city's row, but this write is the
        # authority: a caller holding an id must not be able to land a check-in node on the plan or
        # pull a foreign city's place into this journey's itinerary.
        if place.city_code != journey.city_code or (place.category or "").lower() in _STRUCTURAL_PLACE_CATEGORIES:
            refused = HTTPException(
                422, f"Place {place_id} is not a place this journey can visit"
            )
            refused.reason_code = "IMPORT_PLACE_NOT_VISITABLE"
            refused.error_details = {
                "place_id": place_id,
                "place_city_code": place.city_code,
                "journey_city_code": journey.city_code,
                "category": place.category,
            }
            raise refused
        if any(
            row.place_id == place_id and (row.start_at or "")[:10] == day for row in existing
        ):
            already_present.append(place_id)
            continue
        sequence += 1
        start = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=zone)
        end = start + timedelta(minutes=75)
        row = Activity(
            id=str(uuid4()),
            plan_version_id=plan.id,
            sequence_no=sequence,
            activity_type="visit",
            title=place.name,
            place_id=place.id,
            city_service_id=None,
            start_at=start.isoformat(),
            end_at=end.isoformat(),
            travel_from_prev_min=0,
            estimated_cost_minor=0,
            currency=journey.budget_currency or "CNY",
            status="PLANNED",
            locked=0,
            metadata_json=json.dumps(
                {"source": "poi_import", "source_channel": "LOCAL_TEXT_PARSER"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        existing.append(row)
        added.append(
            PoiImportAddedActivity(
                id=row.id,
                title=row.title,
                place_id=row.place_id or "",
                sequence_no=row.sequence_no,
                start_at=row.start_at,
                end_at=row.end_at,
            )
        )
    record_audit(
        session,
        action="poi_import.add",
        entity_type="PlanVersion",
        entity_id=plan.id,
        journey_id=journey.id,
        after={"added": [a.id for a in added], "already_present": already_present, "day": day},
    )
    refresh_offline_snapshot(session, journey)
    session.commit()
    return PoiImportAddResponse(
        journey_id=journey.id,
        plan_version_id=plan.id,
        day=day,
        day_source=day_source,
        added=added,
        already_present=already_present,
        readback_path=f"/api/v1/journeys/{journey.id}/experience",
    )


# Both lines bind this one handler; a banner inserted between them rebinds /trip-studio/preview to the function that follows.
@app.post("/api/v1/journeys/{journey_id}/trip-studio/preview")
@app.post("/api/v1/journeys/{journey_id}/plans/preview-reorder")
def trip_studio_preview(
    journey_id: UUID,
    payload: TripStudioPreviewRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    requested_ids = [str(item) for item in (payload.draft_order or payload.activity_ids)]
    plan, _activities, _ordered, details = _trip_studio_context(session, journey, requested_ids)
    return _trip_studio_preview_payload(journey, plan, requested_ids, details)


def _trip_studio_accept_impl(
    journey_id: UUID,
    payload: TripStudioAcceptRequest,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    requested_ids = [str(item) for item in payload.activity_ids]
    base, activities, ordered, details = _trip_studio_context(session, journey, requested_ids)
    if details["gate"]["status"] != "PASSED":
        failures = ",".join(str(item) for item in details["gate"]["failures"])
        raise HTTPException(422, f"TRIP_STUDIO_DETERMINISTIC_GATE_FAILED:{failures}")
    if payload.preview_id:
        # The preview id binds the draft to the exact base PlanVersion and
        # frozen evidence identity. Accept still reruns the gate above.
        preview = _trip_studio_preview_payload(journey, base, requested_ids, details)
        if str(payload.preview_id) != preview["preview_id"]:
            raise HTTPException(409, "TRIP_STUDIO_PREVIEW_STALE")

    evidence = details["evidence"]
    authoritative_gate = details["authoritative_gate"]
    if not details["impact"]["changed"]:
        # A repeated accept of an unchanged order is a successful, auditable
        # no-op.  Do not create a new PlanVersion or reset the Journey's
        # identity merely because a client retried a harmless request.
        return JSONResponse(
            status_code=200,
            content={
                "plan": plan_schema(session, base).model_dump(mode="json"),
                "preview_id": str(payload.preview_id) if payload.preview_id else None,
                "impact": details["impact"],
                "deterministic_gate": details["gate"],
                "frozen_evidence_id": base.mobility_snapshot_id,
                "no_op": True,
            },
        )
    now = datetime.now(timezone.utc).isoformat()
    latest_version = int(
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    )
    slots = activities
    score_breakdown = _json_object(base.score_breakdown_json, {})
    score_breakdown["mobility"] = authoritative_gate["mobility_cost_minor"]
    score_breakdown["total_cost_minor"] = authoritative_gate["total_cost_minor"]
    score_breakdown["estimated_duration_min"] = authoritative_gate["estimated_duration_min"]
    new_plan = PlanVersion(
        id=str(uuid4()),
        journey_id=journey.id,
        version_no=latest_version + 1,
        planning_mode=base.planning_mode,
        # Created as CANDIDATE and promoted only after the base plan has been
        # superseded: `ux_plan_versions_single_active` is checked per statement, so
        # inserting the successor as ACTIVE would mean the invariant briefly held
        # two answers inside one transaction.
        status="CANDIDATE",
        source="MANUAL",
        parent_plan_version_id=base.id,
        total_cost_minor=authoritative_gate["total_cost_minor"],
        currency=base.currency,
        estimated_duration_min=authoritative_gate["estimated_duration_min"],
        utility_score=base.utility_score,
        score_breakdown_json=json.dumps(score_breakdown, sort_keys=True),
        explanation_json=json.dumps({
            "product_path": "V12_TRIP_STUDIO_ACCEPT",
            "base_plan_version_id": base.id,
            "draft_order": requested_ids,
            "deterministic_gate": details["gate"],
            "frozen_evidence_id": base.mobility_snapshot_id,
        }, sort_keys=True),
        mobility_snapshot_id=base.mobility_snapshot_id,
        mobility_evidence_json=base.mobility_evidence_json,
        created_at=now,
    )
    session.add(new_plan)
    session.flush()
    new_by_old: dict[str, str] = {}
    new_rows: list[Activity] = []
    for index, (source, slot) in enumerate(zip(ordered, slots), start=1):
        source_metadata = _json_object(source.metadata_json, {})
        source_metadata["trip_studio"] = {
            "base_activity_id": source.id,
            "accepted_at": now,
            "draft_order": requested_ids,
            "frozen_evidence_id": base.mobility_snapshot_id,
            "execution_status_before": source.status,
        }
        row = Activity(
            id=str(uuid4()),
            plan_version_id=new_plan.id,
            sequence_no=index,
            activity_type=source.activity_type,
            title=source.title,
            place_id=source.place_id,
            city_service_id=source.city_service_id,
            start_at=slot.start_at,
            end_at=slot.end_at,
            travel_from_prev_min=0,
            estimated_cost_minor=source.estimated_cost_minor,
            currency=source.currency,
            status=source.status,
            locked=source.locked,
            metadata_json=json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        new_rows.append(row)
        new_by_old[source.id] = row.id
    session.flush()

    new_legs: list[JourneyLeg] = []
    for index, (previous, current) in enumerate(zip(new_rows, new_rows[1:]), start=1):
        from_key = str(previous.place_id or previous.city_service_id or "")
        to_key = str(current.place_id or current.city_service_id or "")
        fact = _trip_studio_fact(from_key, to_key, evidence)
        if fact is None:
            session.rollback()
            raise HTTPException(422, "TRIP_STUDIO_FROZEN_ROUTE_FACT_MISSING")
        duration_min = int(fact.get("duration_min", fact.get("minutes", 0)) or 0)
        current.travel_from_prev_min = duration_min
        new_legs.append(
            JourneyLeg(
                id=str(uuid4()), journey_id=journey.id, plan_version_id=new_plan.id,
                sequence_no=index, from_activity_id=previous.id, to_activity_id=current.id,
                from_key=from_key, to_key=to_key, mode=str(fact.get("mode") or "UNKNOWN"),
                depart_at=previous.end_at, arrive_at=current.start_at,
                duration_min=duration_min, distance_m=int(fact.get("distance_m") or 0),
                cost_amount_minor=fact.get("cost_amount_minor"), currency=fact.get("currency"),
                cost_status=str(fact.get("cost_status") or "UNKNOWN"),
                walking_min=int(fact.get("walking_min") or 0),
                transfers=int(fact.get("transfers") or 0),
                reliability_score=float(fact.get("reliability_score") or 0.0),
                weather_exposure=str(fact.get("weather_exposure") or "UNKNOWN"),
                geometry_json=json.dumps(fact.get("geometry") or {"coordinates": fact.get("route_geometry") or []}, sort_keys=True),
                crs=str(fact.get("crs") or "WGS84"),
                provider_key=str(fact.get("provider_key") or fact.get("provider") or "frozen-plan-evidence"),
                provenance=str(fact.get("provenance") or evidence.get("aggregate_provenance") or "FROZEN_PLAN_EVIDENCE"),
                provider_snapshot_id=fact.get("fact_id"),
                captured_at=str(fact.get("captured_at") or evidence.get("captured_at") or now),
                expires_at=fact.get("expires_at") or evidence.get("expires_at"),
            )
        )
    session.add_all(new_legs)
    for commitment in session.scalars(
        select(CommitmentModel).where(
            CommitmentModel.journey_id == journey.id,
            CommitmentModel.status == "ACTIVE",
            CommitmentModel.activity_id.in_(list(new_by_old)),
        )
    ).all():
        commitment.activity_id = new_by_old.get(commitment.activity_id)
    base.status = "SUPERSEDED"
    # Flush the retirement before anything can promote the successor, so the
    # single-ACTIVE invariant is never satisfied only by statement ordering
    # inside a later flush.
    session.flush()
    next_current_activity_id = (
        new_by_old.get(journey.current_activity_id) if journey.status == "ACTIVE" else None
    )
    if journey.status == "ACTIVE" and next_current_activity_id is None:
        session.rollback()
        raise HTTPException(409, "TRIP_STUDIO_CURRENT_ACTIVITY_MAPPING_FAILED")
    # Compare-and-swap the active PlanVersion at the database boundary.  The
    # in-process lock above protects normal Windows retries; this predicate is
    # the cross-worker guard that rejects a stale writer after another process
    # has already replaced the same base plan.
    guarded = session.execute(
        update(Journey)
        .where(
            Journey.id == journey.id,
            Journey.active_plan_version_id == base.id,
        )
        .values(
            active_plan_version_id=new_plan.id,
            current_activity_id=next_current_activity_id,
            updated_at=now,
        )
    )
    if guarded.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "TRIP_STUDIO_BASE_PLAN_STALE")
    journey.active_plan_version_id = new_plan.id
    journey.current_activity_id = next_current_activity_id
    journey.updated_at = now
    # The base is provably out of the way at this point, so the successor may now
    # take its place.
    new_plan.status = "ACTIVE" if journey.status == "ACTIVE" else "SELECTED"
    record_audit(
        session,
        action="trip_studio.accepted",
        entity_type="PlanVersion",
        entity_id=new_plan.id,
        journey_id=journey.id,
        before={"base_plan_version_id": base.id, "draft_order": [item.id for item in activities]},
        after={"new_plan_version_id": new_plan.id, "draft_order": requested_ids, "frozen_evidence_id": base.mobility_snapshot_id},
    )
    if journey.active_plan_version_id:
        refresh_offline_snapshot(session, journey)
    session.commit()
    return {
        "plan": plan_schema(session, new_plan),
        "preview_id": str(payload.preview_id) if payload.preview_id else None,
        "impact": details["impact"],
        "deterministic_gate": details["gate"],
        "frozen_evidence_id": base.mobility_snapshot_id,
    }


@app.post("/api/v1/journeys/{journey_id}/trip-studio/accept", status_code=201)
@app.post("/api/v1/journeys/{journey_id}/plans/accept-reorder", status_code=201)
def trip_studio_accept(
    journey_id: UUID,
    payload: TripStudioAcceptRequest,
    session: Session = Depends(get_session),
):
    # PlanVersion replacement is a single logical mutation.  Serialize the
    # in-process accept boundary so two browser retries cannot both derive a
    # new PlanVersion from the same active base before either commit lands.
    with _trip_studio_accept_lock:
        return _trip_studio_accept_impl(journey_id, payload, session)


# === WP04 commerce adapter routes ===


def _json_object(value: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not value:
        return default or {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return default or {}
    return parsed if isinstance(parsed, dict) else (default or {})


def _booking_payload(item: BookingModel) -> dict[str, Any]:
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "activity_id": item.activity_id,
        "commitment_id": item.commitment_id,
        "provider_key": item.provider_key,
        "external_ref": item.external_ref,
        "status": item.status,
        "amount": {"amount_minor": item.amount_minor, "currency": item.currency},
        "amount_status": item.amount_status,
        "cancellation_policy": _json_object(item.cancellation_policy_json),
    }


def _order_payload(item: OrderModel) -> dict[str, Any]:
    raw_items = json.loads(item.items_json or "[]")
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "activity_id": item.activity_id,
        "commitment_id": item.commitment_id,
        "provider_key": item.provider_key,
        "external_ref": item.external_ref,
        "status": item.status,
        "amount": {"amount_minor": item.amount_minor, "currency": item.currency},
        "items": raw_items,
    }


def _transaction_payload(item: PaymentTransaction) -> dict[str, Any]:
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "booking_id": item.booking_id,
        "order_id": item.order_id,
        "provider_key": item.provider_key,
        "transaction_type": item.transaction_type,
        "status": item.status,
        "amount": {"amount_minor": item.amount_minor, "currency": item.currency},
        "external_ref": item.external_ref,
    }


def _commitment_payload(item: CommitmentModel) -> dict[str, Any]:
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "activity_id": item.activity_id,
        "kind": item.kind,
        "level": item.level,
        "status": item.status,
        "monetary_value": {
            "amount_minor": item.monetary_value_minor,
            "currency": item.currency,
        },
        "monetary_value_status": item.monetary_value_status,
        "external_ref": item.external_ref,
        "policy": _json_object(item.policy_json),
    }


def _mock_booking_provider(provider_key: str) -> tuple[MockBookingProvider, bool]:
    if provider_key not in {"mock-booking", "mock-booking-fail"}:
        raise CommerceValidationError("only the supplied mock-booking provider is enabled in WP04")
    return MockBookingProvider(), provider_key.endswith("-fail")


def _mock_payment_provider(provider_key: str) -> tuple[MockPaymentProvider, bool]:
    if provider_key not in {"mock-payment", "mock-payment-fail"}:
        raise CommerceValidationError("only the supplied mock-payment provider is enabled in WP04")
    return MockPaymentProvider(), provider_key.endswith("-fail")


def _assert_selected_plan_feasible(session: Session, journey: Journey, _plan: PlanVersion):
    gate = validate_selected_plan_feasibility(session, journey)
    if gate is not None and not gate.passed:
        failures = ", ".join(gate.failures)
        raise CommerceValidationError(f"selected plan is no longer feasible: {failures}")
    return gate


def _envelope_payload(
    journey: Journey,
    activities: list[Activity],
    *,
    preserved_commitment_count: int,
    next_commitment_buffer_min: int | None = None,
    degraded_providers: tuple[str, ...] = (),
    extra_risk_reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    last_end = max(
        (datetime.fromisoformat(item.end_at) for item in activities),
        default=datetime.fromisoformat(journey.start_at),
    )
    envelope = FeasibilityEnvelope(
        next_commitment_buffer_min=next_commitment_buffer_min,
        journey_end_slack_min=int(
            (datetime.fromisoformat(journey.end_at) - last_end).total_seconds() // 60
        ),
        budget_remaining_minor=journey.budget_amount_minor
        - sum(item.estimated_cost_minor for item in activities),
        preserved_commitment_count=preserved_commitment_count,
        degraded_providers=degraded_providers,
        risk_reason_codes=tuple(
            dict.fromkeys(
                (
                    *((ReasonCode.PROVIDER_DEGRADED.value,) if degraded_providers else ()),
                    *extra_risk_reason_codes,
                )
            )
        ),
    )
    return {
        "next_commitment_buffer_min": envelope.next_commitment_buffer_min,
        "journey_end_slack_min": envelope.journey_end_slack_min,
        "budget_remaining_minor": envelope.budget_remaining_minor,
        "preserved_commitment_count": envelope.preserved_commitment_count,
        "degraded_providers": list(envelope.degraded_providers),
        "risk_reason_codes": list(envelope.risk_reason_codes),
        "robust": envelope.robust,
    }


def _proposal_mobility_snapshot(proposal: ReplanProposalModel) -> MobilitySnapshot | None:
    try:
        diff = json.loads(proposal.diff_json or "{}")
        payload = diff.get("verified_evidence", {}).get("mobility", {}).get("snapshot")
        if not isinstance(payload, dict):
            return None
        snapshot = MobilitySnapshot.from_evidence(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return snapshot


def _latest_replan_mobility_snapshot(session: Session, journey_id: str) -> MobilitySnapshot | None:
    proposals = session.scalars(
        select(ReplanProposalModel)
        .where(ReplanProposalModel.journey_id == journey_id)
        .order_by(ReplanProposalModel.created_at.desc(), ReplanProposalModel.id.desc())
    ).all()
    for proposal in proposals:
        snapshot = _proposal_mobility_snapshot(proposal)
        if snapshot is not None and snapshot.facts:
            return snapshot
    return None



# Place providers that describe server-owned schedule structure (arrival and
# departure anchors, check-in / stay / check-out nodes) rather than discoverable
# city services.  Truth blocker B5: a structural node from a *previous* journey
# was entering the replacement candidate pool, so a recovery leg referenced a
# foreign Place id and the mobility capture failed with an identity mismatch.
_SERVER_OWNED_PLACE_PROVIDERS = (
    "citynexus-v12-authoritative",
    "citynexus-v9-domain",
)

def _grounded_recovery_provider_aliases(
    source: list[Activity],
    places_by_id: dict[str, Place],
    provider_mobility: dict[str, Any],
) -> dict[str, str]:
    """Map the sealed provider reserve onto Journey-owned location keys."""

    aliases: dict[str, str] = {}
    for activity in source:
        location = activity.place_id or activity.city_service_id
        if not location:
            continue
        metadata = _json_object(activity.metadata_json, {})
        role = str(metadata.get("provider_route_role") or "").upper()
        if role == "ARRIVAL":
            aliases["anchor:arrival"] = str(location)
        elif role == "DEPARTURE":
            aliases["anchor:departure"] = str(location)

    place_by_external_id = {
        str(place.external_id): place.id
        for place in places_by_id.values()
        if place.external_id
    }
    for node in provider_mobility.get("nodes", []):
        if not isinstance(node, dict):
            continue
        provider_key = str(node.get("key") or "")
        if not provider_key:
            continue
        if node.get("place_id") and str(node["place_id"]) in places_by_id:
            aliases[provider_key] = str(node["place_id"])
            continue
        external_id = str(node.get("external_id") or "")
        if external_id and external_id in place_by_external_id:
            aliases[provider_key] = place_by_external_id[external_id]
    return aliases


def _grounded_recovery_reserve_snapshot(
    *,
    base_evidence: dict[str, Any],
    provider_mobility: dict[str, Any],
    aliases: dict[str, str],
    operation_key: str,
) -> MobilitySnapshot:
    """Promote the sealed Journey provider reserve for grounded recovery.

    The selected PlanVersion remains the immutable baseline. This creates a
    separate recovery snapshot that reuses only provider-captured reserve
    facts, while keeping the selected facts authoritative for existing pairs.
    """

    base = MobilitySnapshot.from_evidence(base_evidence)
    selected_modes = [
        str(item.get("mode") or "").upper()
        for item in base_evidence.get("selected_facts", [])
        if isinstance(item, dict) and item.get("mode")
    ]
    preferred_mode = max(set(selected_modes), key=selected_modes.count) if selected_modes else "AUTO"

    raw_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in provider_mobility.get("facts", []):
        if not isinstance(item, dict):
            continue
        origin = aliases.get(str(item.get("from_key") or ""))
        destination = aliases.get(str(item.get("to_key") or ""))
        if not origin or not destination or origin == destination:
            continue
        raw_by_pair.setdefault((origin, destination), []).append(item)

    facts: dict[tuple[str, str], TravelEstimateFact] = {}
    for pair, options in raw_by_pair.items():
        preferred = [
            item for item in options if str(item.get("mode") or "").upper() == preferred_mode
        ]
        chosen = min(preferred or options, key=lambda item: int(item.get("duration_min") or 0))
        facts[pair] = TravelEstimateFact(
            origin_key=pair[0],
            destination_key=pair[1],
            minutes=int(chosen.get("duration_min") or 0),
            provider_key=str(chosen.get("route_provider") or provider_mobility.get("provider") or "valhalla"),
            captured_at=str(chosen.get("captured_at") or provider_mobility.get("captured_at") or base.captured_at),
            route_provenance=str(chosen.get("route_provenance") or "CAPTURED_REPLAY"),
            cost_provenance=str(chosen.get("cost_provenance") or "CITYNEXUS_COST_ESTIMATE"),
            time_basis=str(chosen.get("time_basis") or "VALHALLA_SOURCES_TO_TARGETS_CAPTURED_AT"),
            distance_m=(
                round(float(chosen["distance_km"]) * 1000)
                if chosen.get("distance_km") is not None
                else None
            ),
            mode=str(chosen.get("mode") or "").upper() or None,
            walking_distance_m=(
                int(chosen["walking_distance_m"])
                if chosen.get("walking_distance_m") is not None
                else None
            ),
            walking_min=(
                int(chosen["walking_min"]) if chosen.get("walking_min") is not None else None
            ),
            # V6: propagate the captured provider fact's honest quality (no
            # provenance guessing); if the source did not attest a quality this
            # stays None and hard feasibility fails closed, as intended.
            fact_quality=(str(chosen["fact_quality"]) if chosen.get("fact_quality") is not None else None),
        )

    for pair, fact in base.facts.items():
        facts[pair] = fact

    return MobilitySnapshot(
        facts=facts,
        source_provider_key=str(provider_mobility.get("provider") or base.source_provider_key),
        captured_at=str(provider_mobility.get("captured_at") or base.captured_at),
        degraded=False,
        snapshot_id="v14-grounded-recovery-"
        + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:32],
        operation_key=operation_key,
        provider_capabilities=(
            dict(provider_mobility.get("provider_capabilities"))
            if isinstance(provider_mobility.get("provider_capabilities"), dict)
            else dict(base.provider_capabilities)
        ),
    )


def _incident_mobility_snapshot(payload: dict[str, Any]) -> MobilitySnapshot | None:
    """Read an explicitly sealed incident matrix supplied by the event."""

    recovery = payload.get("incident_recovery_evidence")
    if not isinstance(recovery, dict):
        recovery = payload.get("recovery_evidence")
    if not isinstance(recovery, dict):
        return None
    candidate = recovery.get("mobility_snapshot")
    if not isinstance(candidate, dict):
        mobility = recovery.get("mobility")
        candidate = mobility.get("snapshot") if isinstance(mobility, dict) else None
    if not isinstance(candidate, dict) or not candidate.get("facts"):
        return None
    return MobilitySnapshot.from_evidence(candidate)


def _twin_snapshot(
    session: Session,
    journey: Journey,
    *,
    projection_now: datetime | None = None,
) -> dict[str, Any]:
    """Build a reproducible read projection from Journey/Plan/Commitment facts."""
    active_plan_id = journey.active_plan_version_id
    current_id = journey.current_activity_id
    latest_event = session.scalar(
        select(EventModel)
        .where(EventModel.journey_id == journey.id)
        .order_by(EventModel.detected_at.desc(), EventModel.id.desc())
    )
    commitments = session.scalars(
        select(CommitmentModel)
        .where(CommitmentModel.journey_id == journey.id, CommitmentModel.status == "ACTIVE")
        .order_by(CommitmentModel.created_at, CommitmentModel.id)
    ).all()
    activities = (
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == active_plan_id)
            .order_by(Activity.sequence_no)
        ).all()
        if active_plan_id
        else []
    )
    current = next((item for item in activities if item.id == current_id), None)
    derived_now = projection_now or (
        datetime.fromisoformat(current.start_at)
        if current is not None
        else datetime.fromisoformat(journey.start_at)
    )
    active_plan_activity_by_id = {item.id: item for item in activities}
    active_plan_legs = (
        session.scalars(
            select(JourneyLeg)
            .where(JourneyLeg.plan_version_id == active_plan_id)
            .order_by(JourneyLeg.sequence_no)
        ).all()
        if active_plan_id
        else []
    )
    execution = _execution_truth(activities, active_plan_legs, current_id)
    projected_commitments: list[CommitmentModel] = []
    for commitment in commitments:
        linked_activity = (
            active_plan_activity_by_id.get(commitment.activity_id)
            if commitment.activity_id
            else None
        )
        if linked_activity is not None and linked_activity.status in {
            "COMPLETED",
            "SKIPPED",
            "CANCELLED",
        }:
            continue
        policy = json.loads(commitment.policy_json or "{}")
        scheduled = policy.get("scheduled_start_at")
        if scheduled:
            try:
                scheduled_at = datetime.fromisoformat(str(scheduled))
            except ValueError:
                scheduled_at = None
            if scheduled_at is not None and scheduled_at < derived_now:
                continue
        projected_commitments.append(commitment)

    active_c4: list[tuple[datetime, dict[str, Any]]] = []
    for commitment in projected_commitments:
        if commitment.level != "C4_Anchored":
            continue
        policy = json.loads(commitment.policy_json or "{}")
        scheduled = policy.get("scheduled_start_at")
        if scheduled:
            start = datetime.fromisoformat(str(scheduled))
            if start >= derived_now:
                active_c4.append((start, policy))
    active_c4.sort(key=lambda item: (item[0], str(item[1].get("semantic_key", ""))))
    persisted_mobility = _latest_replan_mobility_snapshot(session, journey.id)
    mobility_context: dict[str, Any] = {
        "provider": "mock-mobility",
        "snapshot_id": None,
        "degraded": False,
        "source": "deterministic-offline",
    }
    next_buffer: int | None = None
    opportunity: dict[str, Any] | None = None
    route_evidence_unknown = False
    if active_c4:
        next_start, policy = active_c4[0]
        current_key = current.place_id or current.city_service_id if current is not None else None
        target_key = str(policy.get("semantic_key") or "")
        travel_min = 0
        if current_key and target_key and current_key != target_key:
            fact = (
                persisted_mobility.facts.get((current_key, target_key))
                if persisted_mobility is not None
                else None
            )
            if fact is not None:
                travel_min = fact.minutes
                mobility_context = {
                    "provider": fact.provider_key,
                    "source_provider": persisted_mobility.source_provider_key,
                    "snapshot_id": persisted_mobility.snapshot_id,
                    "degraded": fact.degraded,
                    "snapshot_degraded": persisted_mobility.degraded,
                    "fallback_reason": fact.fallback_reason,
                    "source": "persisted-replan-snapshot",
                }
            else:
                try:
                    travel_min = capture_runtime_mobility_snapshot(
                        session,
                        journey,
                        ((current_key, target_key),),
                        operation_key=f"commitment-window:{journey.id}",
                    ).snapshot.estimate_minutes(current_key, target_key)
                except (KeyError, ValueError, TypeError) as exc:
                    # Imported structural anchors may be valid commitments but
                    # lack routable coordinates. Twin remains readable and
                    # explicitly degraded instead of turning /start into 500.
                    travel_min = 0
                    route_evidence_unknown = True
                    mobility_context = {
                        "provider": "mock-mobility",
                        "snapshot_id": None,
                        "degraded": True,
                        "source": "missing-route-evidence",
                        "fallback_reason": f"{type(exc).__name__}: route evidence unavailable",
                    }
        if not route_evidence_unknown:
            window = build_window(
                now=derived_now,
                next_commitment_start=next_start,
                travel_to_commitment_min=travel_min,
                safety_buffer_min=15,
            )
            next_buffer = window.available_minutes
            opportunity = {
                "opens_at": window.opens_at.isoformat(),
                "must_depart_by": window.must_depart_by.isoformat(),
                "available_minutes": window.available_minutes,
                "next_commitment_start": window.next_commitment_start.isoformat(),
            }
    degraded_items: set[str] = set()
    unresolved_severities: list[str] = []
    unresolved_events = session.scalars(
        select(EventModel).where(
            EventModel.journey_id == journey.id,
            EventModel.status.in_(("DETECTED", "ACKNOWLEDGED")),
        )
    ).all()
    for event_row in unresolved_events:
        unresolved_severities.append(event_row.severity)
        try:
            event_payload = json.loads(event_row.payload_json or "{}")
        except json.JSONDecodeError:
            event_payload = {}
        degraded_items.update(
            str(item) for item in event_payload.get("degraded_providers", []) if item
        )
    if persisted_mobility is not None and persisted_mobility.degraded:
        degraded_items.add(persisted_mobility.source_provider_key)
    if route_evidence_unknown:
        degraded_items.add("UNKNOWN_ROUTE_EVIDENCE")
    degraded = tuple(sorted(degraded_items))
    envelope = _envelope_payload(
        journey,
        activities,
        preserved_commitment_count=len(active_c4),
        next_commitment_buffer_min=next_buffer,
        degraded_providers=degraded,
        extra_risk_reason_codes=disruption_risk_codes(unresolved_severities),
    )
    context = {
        "journey_status": journey.status,
        "provider_level": 1,
        "degraded_providers": list(degraded),
        "feasibility_envelope": envelope,
        "opportunity_window": opportunity,
        "mobility": mobility_context,
        "execution": execution,
    }
    snapshot_at = datetime.now(timezone.utc).isoformat()
    projection_status = "DEGRADED" if degraded else "READY"
    try:
        with session.begin_nested():
            twin = session.get(JourneyTwinModel, journey.id)
            if twin is None:
                twin = JourneyTwinModel(journey_id=journey.id)
                session.add(twin)
            twin.active_plan_version_id = active_plan_id
            twin.current_activity_id = current_id
            twin.latest_event_id = latest_event.id if latest_event else None
            twin.projection_status = projection_status
            twin.snapshot_at = snapshot_at
            twin.updated_at = snapshot_at
            twin.context_json = json.dumps(context, sort_keys=True)
            twin.upcoming_commitments_json = json.dumps(
                [_commitment_payload(item) for item in projected_commitments], sort_keys=True
            )
            session.flush()
    except OperationalError as exc:
        detail = str(exc).casefold()
        if "database is locked" not in detail and "database is busy" not in detail:
            raise
        logger.warning("JourneyTwin cache write skipped during transient SQLite contention: %s", exc)
    return {
        "journey_id": journey.id,
        "active_plan_version_id": active_plan_id,
        "current_activity_id": current_id,
        "latest_event_id": latest_event.id if latest_event else None,
        "projection_status": projection_status,
        "snapshot_at": snapshot_at,
        "upcoming_commitments": [_commitment_payload(item) for item in projected_commitments],
        "context": context,
    }


@app.post("/api/v1/journeys/{journey_id}/start", response_model=JourneyTwinSchema)
def start_journey(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status == "ACTIVE":
        twin = _twin_snapshot(session, journey)
        session.commit()
        return twin
    if journey.status != "PLANNED" or not journey.active_plan_version_id:
        raise HTTPException(409, "journey must be PLANNED with a selected plan")
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    if plan is None or plan.status != "SELECTED":
        raise HTTPException(409, "selected plan is missing or inconsistent")
    # /start must apply the same evidence/staleness truth policy as select and
    # booking.  Starting a journey on expired frozen provider evidence is a
    # silent safety regression, so refuse it and surface the re-sync condition.
    try:
        gate = validate_selected_plan_feasibility(session, journey)
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(409, message) from exc
    if gate is not None and not gate.passed:
        raise HTTPException(
            409,
            "START_BLOCKED_INFEASIBLE: " + ", ".join(gate.failures),
        )
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    journey.status = "ACTIVE"
    plan.status = "ACTIVE"
    if activities:
        journey.current_activity_id = activities[0].id
        activities[0].status = "ACTIVE"
        _record_outcome_event(
            session,
            journey,
            activity=activities[0],
            event_type="ACTIVITY_STARTED",
            actor_type="SYSTEM",
        )
    journey.updated_at = datetime.now(timezone.utc).isoformat()
    record_audit(
        session,
        action="journey.started",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        after={"status": journey.status, "current_activity_id": journey.current_activity_id},
    )
    twin = _twin_snapshot(session, journey)
    refresh_offline_snapshot(session, journey)
    session.commit()
    return twin


def _transition_activity_state(
    journey_id: UUID,
    activity_id: UUID,
    target_state: str,
    event_type: str,
    session: Session,
):
    allowed_predecessors = {
        "EN_ROUTE": {"ACTIVE", "NOT_STARTED"},
        "ARRIVED": {"EN_ROUTE"},
        "IN_PROGRESS": {"ARRIVED"},
    }
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "journey must be ACTIVE")
    activity = session.get(Activity, str(activity_id))
    if activity is None or activity.plan_version_id != journey.active_plan_version_id:
        raise HTTPException(409, "activity is not on the active plan")
    if journey.current_activity_id != activity.id:
        raise HTTPException(409, "activity is not the current activity")
    predecessors = allowed_predecessors.get(target_state, set())
    if activity.status not in predecessors:
        expected = ", ".join(sorted(predecessors)) or "none"
        raise HTTPException(
            409,
            f"INVALID_ACTIVITY_TRANSITION:{activity.status}->{target_state}; expected={expected}",
        )
    metadata = json.loads(activity.metadata_json or "{}")
    actual_key = {
        "EN_ROUTE": "en_route_at",
        "ARRIVED": "arrived_at",
        "IN_PROGRESS": "started_at_actual",
    }.get(target_state)
    if actual_key:
        captured_at = datetime.now(timezone.utc).isoformat()
        metadata[actual_key] = captured_at
        # Preserve the V11 names for existing consumers while exposing the
        # authoritative V12 execution contract.
        metadata[
            {
                "EN_ROUTE": "actual_departed_at",
                "ARRIVED": "actual_arrived_at",
                "IN_PROGRESS": "actual_started_at",
            }[target_state]
        ] = captured_at
    activity.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    activity.status = target_state
    _record_outcome_event(session, journey, activity=activity, event_type=event_type, actor_type="USER")
    activity.updated_at = datetime.now(timezone.utc).isoformat()
    twin = _twin_snapshot(session, journey)
    refresh_offline_snapshot(session, journey)
    session.commit()
    return twin


def _is_authoritative_execution_plan(plan: PlanVersion | None) -> bool:
    """Identify the V12/V13 plan contract that owns the strict state graph.

    Older V4-V11 generated plans predate the execution-state API and existing
    clients still use their single-step completion command.  Keep that legacy
    compatibility boundary explicit while making every authoritative frozen
    plan use the server-owned predecessor graph below.
    """

    if plan is None:
        return False
    try:
        evidence = json.loads(plan.mobility_evidence_json or "{}")
    except json.JSONDecodeError:
        return False
    return str(evidence.get("normalizer_version") or "").startswith(
        ("citynexus-v12-authoritative-", "citynexus-v13-authoritative-")
    )


@app.post("/api/v1/journeys/{journey_id}/activities/{activity_id}/depart", response_model=JourneyTwinSchema)
def depart_activity(journey_id: UUID, activity_id: UUID, session: Session = Depends(get_session)):
    return _transition_activity_state(journey_id, activity_id, "EN_ROUTE", "ACTIVITY_DEPARTED", session)


@app.post("/api/v1/journeys/{journey_id}/activities/{activity_id}/arrive", response_model=JourneyTwinSchema)
def arrive_activity(journey_id: UUID, activity_id: UUID, session: Session = Depends(get_session)):
    return _transition_activity_state(journey_id, activity_id, "ARRIVED", "ACTIVITY_ARRIVED", session)


@app.post("/api/v1/journeys/{journey_id}/activities/{activity_id}/begin", response_model=JourneyTwinSchema)
def begin_activity(journey_id: UUID, activity_id: UUID, session: Session = Depends(get_session)):
    return _transition_activity_state(journey_id, activity_id, "IN_PROGRESS", "ACTIVITY_IN_PROGRESS", session)


@app.post(
    "/api/v1/journeys/{journey_id}/activities/{activity_id}/complete",
    response_model=JourneyTwinSchema,
)
def complete_activity(journey_id: UUID, activity_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "journey must be ACTIVE")
    activity = session.get(Activity, str(activity_id))
    if activity is None or activity.plan_version_id != journey.active_plan_version_id:
        raise HTTPException(409, "activity is not on the active plan")
    if journey.current_activity_id != activity.id:
        raise HTTPException(409, "activity is not the current activity")
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    authoritative_execution = _is_authoritative_execution_plan(plan)
    if activity.status != "IN_PROGRESS":
        if not (
            not authoritative_execution
            and activity.status in {"ACTIVE", "EN_ROUTE", "ARRIVED"}
        ):
            raise HTTPException(
                409,
                f"INVALID_ACTIVITY_TRANSITION:{activity.status}->COMPLETED; expected=IN_PROGRESS",
            )
    activity.status = "COMPLETED"
    metadata = json.loads(activity.metadata_json or "{}")
    completed_at = datetime.now(timezone.utc).isoformat()
    metadata["completed_at_actual"] = completed_at
    if not authoritative_execution and metadata.get("execution_state") != "IN_PROGRESS":
        metadata["legacy_execution_compatibility"] = "DIRECT_COMPLETION_PRE_V12_CONTRACT"
    activity.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    _record_outcome_event(
        session,
        journey,
        activity=activity,
        event_type="ACTIVITY_COMPLETED",
        actor_type="USER",
    )
    next_activity = session.scalar(
        select(Activity)
        .where(
            Activity.plan_version_id == journey.active_plan_version_id,
            Activity.sequence_no > activity.sequence_no,
            Activity.status.not_in(("COMPLETED", "SKIPPED", "CANCELLED")),
        )
        .order_by(Activity.sequence_no)
    )
    if next_activity is None:
        journey.current_activity_id = None
        journey.status = "COMPLETED"
        plan = session.get(PlanVersion, journey.active_plan_version_id)
        if plan is not None:
            plan.status = "ACTIVE"
    else:
        next_activity.status = "ACTIVE"
        journey.current_activity_id = next_activity.id
        _record_outcome_event(
            session,
            journey,
            activity=next_activity,
            event_type="ACTIVITY_STARTED",
            actor_type="SYSTEM",
        )
    journey.updated_at = datetime.now(timezone.utc).isoformat()
    record_audit(
        session,
        action="activity.completed",
        entity_type="Activity",
        entity_id=activity.id,
        journey_id=journey.id,
        after={"status": activity.status, "next_activity_id": journey.current_activity_id},
    )
    twin = _twin_snapshot(session, journey)
    if journey.status == "COMPLETED":
        learn_from_journey(session, journey)
    refresh_offline_snapshot(session, journey)
    session.commit()
    return twin


@app.post(
    "/api/v1/journeys/{journey_id}/activities/{activity_id}/skip",
    response_model=JourneyTwinSchema,
)
def skip_activity(journey_id: UUID, activity_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "journey must be ACTIVE")
    activity = session.get(Activity, str(activity_id))
    if activity is None or activity.plan_version_id != journey.active_plan_version_id:
        raise HTTPException(409, "activity is not on the active plan")
    if journey.current_activity_id != activity.id:
        raise HTTPException(409, "activity is not the current activity")
    if activity.status not in {"ACTIVE", "NOT_STARTED", "EN_ROUTE", "ARRIVED", "IN_PROGRESS"}:
        raise HTTPException(
            409,
            f"INVALID_ACTIVITY_TRANSITION:{activity.status}->SKIPPED",
        )
    protected_commitment = session.scalar(
        select(CommitmentModel).where(
            CommitmentModel.journey_id == journey.id,
            CommitmentModel.activity_id == activity.id,
            CommitmentModel.status == "ACTIVE",
            CommitmentModel.level.in_(("C3_Guaranteed", "C4_Anchored")),
        )
    )
    active_plan = session.get(PlanVersion, journey.active_plan_version_id)
    authoritative_locked = bool(activity.locked and _is_authoritative_execution_plan(active_plan))
    if authoritative_locked or protected_commitment is not None:
        raise HTTPException(409, "PROTECTED_COMMITMENT_CANNOT_BE_SKIPPED")
    activity.status = "SKIPPED"
    metadata = json.loads(activity.metadata_json or "{}")
    skipped_at = datetime.now(timezone.utc).isoformat()
    metadata["skipped_at"] = skipped_at
    activity.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    _record_outcome_event(
        session,
        journey,
        activity=activity,
        event_type="ACTIVITY_SKIPPED",
        actor_type="USER",
    )
    next_activity = session.scalar(
        select(Activity)
        .where(
            Activity.plan_version_id == journey.active_plan_version_id,
            Activity.sequence_no > activity.sequence_no,
            Activity.status.not_in(("COMPLETED", "SKIPPED", "CANCELLED")),
        )
        .order_by(Activity.sequence_no)
    )
    if next_activity is None:
        journey.current_activity_id = None
        journey.status = "COMPLETED"
        learn_from_journey(session, journey)
    else:
        next_activity.status = "ACTIVE"
        journey.current_activity_id = next_activity.id
        _record_outcome_event(
            session,
            journey,
            activity=next_activity,
            event_type="ACTIVITY_STARTED",
            actor_type="SYSTEM",
        )
    journey.updated_at = datetime.now(timezone.utc).isoformat()
    record_audit(
        session,
        action="activity.skipped",
        entity_type="Activity",
        entity_id=activity.id,
        journey_id=journey.id,
        after={"status": activity.status, "next_activity_id": journey.current_activity_id},
    )
    twin = _twin_snapshot(session, journey)
    refresh_offline_snapshot(session, journey)
    session.commit()
    return twin


def _activity_routing_key(item: Activity | None) -> str | None:
    """The routing identity of one activity - the key the mobility evidence is filed under.

    Must match ``feasibility._location_key``, and it is the *same* rule
    ``generate_replan`` uses to ask for its legs: the frozen evidence is keyed by
    ``metadata.semantic_key``, so requesting a pair by ``place_id`` would ask for keys the
    gate never looks up and every leg would report MOBILITY_DATA_UNAVAILABLE (identity
    drift, truth blocker B5).  A read that counts coverage has to ask with this key too,
    which is why the rule lives here once rather than in two places.
    """

    if item is None:
        return None
    metadata = _json_object(item.metadata_json, {})
    semantic = str(metadata.get("semantic_key") or "").strip()
    return semantic or item.place_id or item.city_service_id


def _mobility_evidence_coverage(session: Session, plan: PlanVersion | None) -> dict[str, Any]:
    """How much of the plan's own walking order the sealed evidence can actually carry.

    Reported, not judged.  This never licenses or blocks a recovery - `generate_replan`
    runs its own gate and will still refuse for its own reasons - it answers the question
    the Simulation panel has to put into traveller words (ORD-0079 must 3b): does this
    journey have usable inter-stop travel facts at all, and for how many of its legs?

    The hardness test is the product's own ``MobilitySnapshot.hard_minutes``, so the
    count cannot drift from the rule that decides it: a leg sealed as
    ``SYNTHETIC_DEFAULT`` (the fixture's blanket constant) or left untagged raises
    ``KeyError`` there and is counted as not covered (V4 #1 - unknown is never trusted
    as hard truth).  Nothing here invents, edits, or extends evidence.
    """

    empty = {
        "snapshot_id": None,
        "reference_consistent": False,
        "legs_total": 0,
        "legs_hard_eligible": 0,
    }
    if plan is None:
        return empty
    evidence = _json_object(plan.mobility_evidence_json, {})
    snapshot_id = str(evidence.get("snapshot_id") or "") or None
    activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == plan.id)
        .order_by(Activity.sequence_no)
    ).all()
    keys = [str(key) for key in (_activity_routing_key(item) for item in activities) if key]
    legs = [(left, right) for left, right in zip(keys, keys[1:]) if left != right]
    snapshot: MobilitySnapshot | None = None
    if snapshot_id is not None:
        try:
            snapshot = MobilitySnapshot.from_evidence(evidence)
        except (KeyError, TypeError, ValueError):
            snapshot = None
    hard_eligible = 0
    if snapshot is not None:
        for pair in legs:
            try:
                snapshot.hard_minutes(*pair)
            except KeyError:
                continue
            hard_eligible += 1
    return {
        "snapshot_id": snapshot_id,
        "reference_consistent": bool(snapshot_id) and snapshot_id == plan.mobility_snapshot_id,
        "legs_total": len(legs),
        "legs_hard_eligible": hard_eligible,
    }


def _board_supply_block(
    session: Session, journey_id: str | None, plan: PlanVersion | None
) -> dict[str, Any]:
    """ORD-0522: what the stage's places rest on, and how many of them a map can stand on.

    Two halves, both read out of rows.  The job this journey was given (which hop answered, which
    refused, and at which stage it stopped supplying) comes from
    `catalog_hydration_operations.board_supply`; the stops this stage actually has come from the same
    coordinate test the map panel uses.  Nothing here asks a provider -- a read that phoned home would
    make the board's emptiness depend on the moment somebody looked -- and nothing here coins a word:
    the vocabularies belong to those two modules.
    """

    block = board_supply(session, journey_id)
    stops = (
        session.scalars(
            select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
        ).all()
        if plan is not None
        else []
    )
    place_ids = sorted({str(item.place_id) for item in stops if item.place_id})
    places: dict[str, Place] = {}
    if place_ids:
        places = {
            place.id: place
            for place in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()
        }
    coordinates = [activity_coordinate(item.place_id, places) for item in stops]
    placeable = [c for c in coordinates if c["latitude"] is not None and c["longitude"] is not None]
    missing = [c for c in coordinates if c["latitude"] is None or c["longitude"] is None]
    kinds = sorted({_source_kind(place) for place in places.values()})
    block["stops"] = {
        "plan_present": plan is not None,
        "total": len(stops),
        "with_coordinate": len(placeable),
        "without_coordinate": len(stops) - len(placeable),
        # Named, not counted-and-dropped: the reason the projection already carries for each stop that
        # has no position is what the screen has to say out loud instead of quietly drawing fewer rows.
        "reasons": sorted({str(c["reason"]) for c in missing}),
        "source_kinds": kinds,
    }
    # The second ban this order names, in the same word `real_city.py` uses: a stage whose stops all
    # come back from a replay is not a stage that was supplied.  The job's own ending is necessary but
    # not sufficient -- what the rows are actually made of has to agree.
    if block["counts_as_supply"] and kinds and set(kinds) == {"REPLAY"}:
        block["counts_as_supply"] = False
    return block


@app.get("/api/v1/demo/scene")
def demo_scene(session: Session = Depends(get_session)):
    """Which day the stage is set on, from the server - the one read gap Simulation needs.

    Everything else about walking a scenario already exists (complete/skip/events/replan/
    accept/book).  What nothing answered is "what is now, for this journey", so any client
    that wanted a banner or a Today header had to call `new Date()` and invent a second
    clock.  This reports the seeded journey's own bound window, the cursor it is executing,
    and the expiry of the evidence that window is priced with - all per journey, never a
    frozen global clock, because a global freeze would also move the guardian thread and real
    users' `created_at`.

    With no scenario present it says so with nulls.  It does not fall back to wall time: an
    unknown scene reported as "today" is the unknown-as-known failure this project keeps
    being charged for.
    """

    # The scenario on stage is the journey an ACTIVE plan points at, and failing that the most
    # recently seeded journey - deliberately not an `ACTIVE|PLANNED` status filter, because the
    # frozen seed produces one DRAFT journey with no PlanVersion row at all, so a status filter
    # would report "no scenario" for the only scenario that exists.
    journey = session.scalar(
        select(Journey)
        .where(Journey.active_plan_version_id.is_not(None))
        .order_by(Journey.created_at.desc())
    ) or session.scalar(select(Journey).order_by(Journey.created_at.desc()))
    if journey is None:
        return {
            "demo_mode": demo_mode_enabled(),
            "journey_id": None,
            "journey_status": None,
            "current_activity_id": None,
            "scene_start_at": None,
            "scene_end_at": None,
            "effective_now": None,
            "basis": None,
            "evidence_expires_at": None,
            "evidence_state": "NO_SCENARIO",
            "evidence_snapshot_id": None,
            "evidence_reference_consistent": None,
            "mobility_legs_total": None,
            "mobility_legs_hard_eligible": None,
            # Constant key set: a strict client-side shape check (missing key -> the envelope is
            # dropped) would otherwise turn an honest empty stage into "cannot read the scene",
            # which is the ORD-0070 family of defects seen from the other side.
            "plan_version_id": None,
            # ORD-0522: an empty stage says which hop lost the places too, in the same shape it has
            # when a stage exists -- here `asked: false`, because no journey means no job was opened.
            "supply": _board_supply_block(session, None, None),
        }
    context = session.get(JourneyTripContext, journey.id)
    start_raw, end_raw = (
        journey_boundaries(context, journey.start_at, journey.end_at) if context else (
            datetime.fromisoformat(journey.start_at),
            datetime.fromisoformat(journey.end_at),
        )
    )
    # The journey's pointer is the product's own statement of which plan it is executing -
    # including one that is SELECTED but not yet started, which a status=="ACTIVE" filter
    # dropped, making the scene report plan_version_id: null for a plan that existed.
    plan = (
        session.get(PlanVersion, journey.active_plan_version_id)
        if journey.active_plan_version_id
        else None
    )
    cursor_at: str | None = None
    if plan is not None:
        current_id = journey.current_activity_id
        activity = (
            session.scalar(
                select(Activity).where(Activity.plan_version_id == plan.id, Activity.id == current_id)
            )
            if current_id
            else None
        )
        cursor_at = activity.start_at if activity is not None else None
    evidence = json.loads((plan.mobility_evidence_json if plan else None) or "{}")
    coverage = _mobility_evidence_coverage(session, plan)
    # Four answers, not two.  `expires_at` used to stand in for "is there frozen evidence at
    # all", so a plan carrying 56 sealed pairs - measured, and consumed by a successful replan -
    # was reported as NO_FROZEN_EVIDENCE, and a panel that faithfully repeats the server then
    # tells a traveller this trip has no travel data (ORD-0079 must 3b, inverted).  A missing
    # TTL is stated as missing; writing one to make the field look better is the fabricated
    # `expires_at` this project is not allowed to produce.
    if coverage["snapshot_id"] is None:
        evidence_state = "NO_FROZEN_EVIDENCE"
    elif not coverage["reference_consistent"]:
        evidence_state = "EVIDENCE_REFERENCE_INCONSISTENT"
    elif evidence.get("expires_at"):
        evidence_state = "REFERENCED"
    else:
        evidence_state = "FROZEN_WITHOUT_TTL"
    return {
        "demo_mode": demo_mode_enabled(),
        "journey_id": journey.id,
        # Stated rather than implied: a DRAFT journey has a window but no cursor, and a client
        # must be able to see that difference instead of inferring it from a missing field.
        "journey_status": journey.status,
        # Where the stage actually is: the panel labels Now/Next from this, and a client
        # deriving it from activity order would be a second cursor truth.
        "current_activity_id": str(journey.current_activity_id) if journey.current_activity_id else None,
        "scene_start_at": start_raw.isoformat(),
        "scene_end_at": end_raw.isoformat(),
        # Per journey: the cursor says where the traveller is; without one, the window's own
        # start is the honest anchor.  `Date.now()` appears nowhere on this path.
        "effective_now": cursor_at or start_raw.isoformat(),
        "basis": "CURSOR" if cursor_at else "JOURNEY_START",
        "evidence_expires_at": evidence.get("expires_at"),
        # Reported, not judged: whether the evidence is still valid is the stale gate's job
        # (`STALE_RECOMPOSE_REQUIRED`), and a scene read must not grow a second opinion.
        "evidence_state": evidence_state,
        "evidence_snapshot_id": coverage["snapshot_id"],
        "evidence_reference_consistent": coverage["reference_consistent"],
        # How many legs this plan actually walks, and how many of those the sealed evidence can
        # carry into a hard decision.  A count of existing facts, not a licence: `/replans`
        # still decides for itself.
        "mobility_legs_total": coverage["legs_total"],
        "mobility_legs_hard_eligible": coverage["legs_hard_eligible"],
        "plan_version_id": str(plan.id) if plan is not None else None,
        # ORD-0522: which hop the places behind this stage rest on, at which stage a job stopped
        # supplying, and how many of the stops the map can actually stand on.
        "supply": _board_supply_block(session, journey.id, plan),
    }


@app.get("/api/v1/journeys/{journey_id}/twin", response_model=JourneyTwinSchema)
def get_journey_twin(
    journey_id: UUID,
    now: datetime | None = Query(default=None),
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    # ORD-0058's flagged static inference, settled by measurement instead of assumption: a
    # `?now=` without an offset arrives naive while the projection compares it against
    # offset-aware instants, which raises TypeError - i.e. a 500 for a query-string detail.
    # Refusing by name is fail-closed and costs a caller nothing they already had.
    if now is not None and now.tzinfo is None:
        return error(
            "TWIN_NOW_OFFSET_REQUIRED",
            "The projection instant must carry a UTC offset, e.g. 2026-10-01T09:00:00+08:00.",
            status=422,
            details={"received": now.isoformat(), "offset_required": True},
            message_key="twin.now_offset_required",
        )
    result = _twin_snapshot(session, journey, projection_now=now)
    session.commit()
    return result


@app.post(
    "/api/v1/journeys/{journey_id}/weather/refresh",
    response_model=dict[str, Any],
)
async def refresh_journey_weather(
    journey_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    if (await request.body()).strip():
        raise HTTPException(422, "weather refresh accepts no request body")
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE":
        raise HTTPException(409, "weather refresh requires an active Journey")

    captured_at = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient(timeout=settings.open_meteo_timeout_seconds) as client:
        result = await refresh_weather_runtime(
            session,
            journey,
            provider=build_open_meteo_provider(client),
            captured_at=captured_at,
        )
    session.commit()
    return result.to_evidence()


@app.post("/api/v1/journeys/{journey_id}/events", response_model=EventSchema, status_code=201)
def create_journey_event(
    journey_id: UUID, payload: EventCreate, session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    if payload.provenance in {"PROVIDER_VERIFIED", "SYSTEM_DERIVED"}:
        raise HTTPException(422, "trusted event provenance is assigned only by server ingestion")
    provider_key = "demo-simulator" if payload.provenance == "SIMULATED" else "user-report"
    if payload.provider_key not in {None, provider_key}:
        raise HTTPException(422, "provider_key is server-assigned for public event input")
    affected_activity = None
    if payload.affected_activity_id is not None:
        affected_activity = session.get(Activity, str(payload.affected_activity_id))
        if (
            affected_activity is None
            or affected_activity.plan_version_id != journey.active_plan_version_id
        ):
            raise HTTPException(422, "affected activity is not on the active plan")
    now = datetime.now(timezone.utc).isoformat()
    current_activity = (
        session.get(Activity, journey.current_activity_id) if journey.current_activity_id else None
    )
    deterministic_default = (
        affected_activity.start_at
        if affected_activity is not None
        else (current_activity.start_at if current_activity is not None else journey.start_at)
    )
    occurred_at = payload.occurred_at.isoformat() if payload.occurred_at else deterministic_default
    item = EventModel(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=provider_key,
        provenance=payload.provenance,
        event_type=payload.event_type,
        severity=payload.severity,
        status="DETECTED",
        affected_activity_id=str(payload.affected_activity_id)
        if payload.affected_activity_id
        else None,
        occurred_at=occurred_at,
        detected_at=now,
        payload_json=json.dumps(payload.payload, sort_keys=True),
        created_at=now,
    )
    session.add(item)
    record_audit(
        session,
        action="journey.event.detected",
        entity_type="Event",
        entity_id=item.id,
        journey_id=journey.id,
        after={
            "event_type": item.event_type,
            "severity": item.severity,
            "provenance": item.provenance,
        },
    )
    session.commit()
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "provider_key": item.provider_key,
        "provenance": item.provenance,
        "event_type": item.event_type,
        "severity": item.severity,
        "status": item.status,
        "affected_activity_id": item.affected_activity_id,
        "occurred_at": item.occurred_at,
        "detected_at": item.detected_at,
        "payload": json.loads(item.payload_json),
    }


@app.get("/api/v1/journeys/{journey_id}/events", response_model=list[EventSchema])
def list_journey_events(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    items = session.scalars(
        select(EventModel)
        .where(EventModel.journey_id == str(journey_id))
        .order_by(EventModel.detected_at, EventModel.id)
    ).all()
    return [
        {
            "id": item.id,
            "journey_id": item.journey_id,
            "provider_key": item.provider_key,
            "provenance": item.provenance,
            "event_type": item.event_type,
            "severity": item.severity,
            "status": item.status,
            "affected_activity_id": item.affected_activity_id,
            "occurred_at": item.occurred_at,
            "detected_at": item.detected_at,
            "payload": json.loads(item.payload_json),
        }
        for item in items
    ]


@app.get("/api/v1/journeys/{journey_id}/events/{event_id}/impact")
def get_event_impact(
    journey_id: UUID,
    event_id: UUID,
    session: Session = Depends(get_session),
):
    require(session, Journey, str(journey_id), "Journey")
    event = session.get(EventModel, str(event_id))
    if event is None or event.journey_id != str(journey_id):
        raise HTTPException(404, "Event was not found on this Journey")
    assessment = session.scalar(
        select(ImpactAssessment)
        .where(
            ImpactAssessment.event_id == event.id,
            ImpactAssessment.journey_id == str(journey_id),
        )
        .order_by(ImpactAssessment.created_at.desc(), ImpactAssessment.id.desc())
    )
    if assessment is None:
        raise HTTPException(404, "Impact assessment has not been resolved")
    return impact_payload(assessment)


@app.get("/api/v1/journeys/{journey_id}/history")
def journey_history(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(AuditLog).where(AuditLog.journey_id == str(journey_id)).order_by(AuditLog.created_at)
    ).all()
    return [_audit_projection(row) for row in rows]


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _audit_projection(row: AuditLog) -> dict[str, Any]:
    after = _json_dict(row.after_json)
    action = row.action
    decision = after.get("authorization_decision") or after.get("decision")
    reason_codes = after.get("reason_codes") or []
    if not isinstance(reason_codes, list):
        reason_codes = [str(reason_codes)]
    if action in {"agent_tool_requested", "agent.tool.requested"}:
        event_type = "agent_tool_requested"
    elif action in {"agent.request_replan", "replan.requested"}:
        event_type = "replan_requested"
    elif action in {"impact_hypothesis_created", "impact.hypothesis_created"}:
        event_type = "impact_hypothesis_created"
    elif action in {"impact_corroborated", "impact.corroborated"}:
        event_type = "impact_corroborated"
    elif action in {"impact_hypothesis_selected", "impact.hypothesis_selected"}:
        event_type = "impact_hypothesis_selected"
    elif action in {"impact_validated", "impact.resolved"}:
        event_type = "impact_validated"
    elif action in {"impact_rejected", "impact.rejected"}:
        event_type = "impact_rejected"
    elif action in {"replan.generated", "solver_candidates_generated"}:
        event_type = "solver_candidates_generated"
    elif action in {"provider.degraded", "provider_degraded"}:
        event_type = "provider_degraded"
    elif action in {"city.signal.observation.received", "city_signal_received"}:
        event_type = "city_signal_received"
    elif action in {"city.signal.incident.created", "city.signal.incident.updated"}:
        event_type = "incident_updated"
    elif action == "guardian.pulse.started":
        event_type = "guardian_wake"
    elif action == "guardian.pulse.completed":
        event_type = "guardian_sleep"
    elif action == "guardian.mission.compiled":
        event_type = "mission_compiled"
    elif action == "agent.run.started":
        event_type = "agent_run_started"
    elif action in {"agent.run.tool", "agent_tool_requested"}:
        event_type = "agent_tool_called"
    elif action in {"solver.started", "solver.candidates_ready"}:
        event_type = "solver_candidates_ready"
    elif action in {"domain.mutation", "plan.selected", "journey.plan.accepted"}:
        event_type = "domain_mutation"
    elif action in {"twin.refreshed", "journey.twin.refreshed"}:
        event_type = "twin_refreshed"
    elif action in {"event.created", "journey.event.detected"}:
        event_type = "journey_event_detected"
    elif action in {"replan.accepted", "replan_auto_accepted"}:
        event_type = "replan_auto_accepted" if decision == "ALLOW_AUTO" else "replan_accepted"
    elif action == "agent.accept_replan":
        if after.get("user_confirmed") is True and after.get("status") == "ACCEPTED":
            event_type = "replan_user_confirmed"
        elif decision == "DENY":
            event_type = "replan_denied"
        elif decision in {"REQUIRE_CONFIRMATION", "REQUIRE_STEP_UP"}:
            event_type = "replan_confirmation_required"
        elif decision == "ALLOW_AUTO" and after.get("status") == "ACCEPTED":
            event_type = "replan_auto_accepted"
        else:
            event_type = "policy_decision"
    else:
        event_type = action
    proposal_id = after.get("proposal_id")
    if proposal_id is None and row.entity_type == "ReplanProposal":
        proposal_id = row.entity_id
    tool = after.get("tool")
    if tool is None and action.startswith("agent."):
        tool = action.removeprefix("agent.")
    return {
        "id": row.id,
        "timestamp": row.created_at,
        "event_type": event_type,
        "type": event_type,
        "summary": after.get("summary") or event_type.replace("_", " ").capitalize(),
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "tool": tool,
        "journey_id": row.journey_id,
        "proposal_id": proposal_id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "decision": decision,
        "reason_codes": reason_codes,
        "trace_id": row.trace_id,
        "source": "audit_log",
        "facts": after,
    }


@app.get("/api/v1/journeys/{journey_id}/agent-feed")
def journey_agent_feed(
    journey_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
):
    require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.journey_id == str(journey_id))
        .order_by(AuditLog.created_at, AuditLog.id)
        .limit(limit)
    ).all()
    return {
        "journey_id": str(journey_id),
        "source": "audit_log",
        "events": [_audit_projection(row) for row in rows],
    }


@app.get("/api/v1/journeys/{journey_id}/reflect")
def journey_reflect(journey_id: UUID, session: Session = Depends(get_session)):
    """Return a backend-derived Recover -> Reflect summary.

    Actual timestamps are intentionally not fabricated: the current domain
    records Activity state transitions but does not yet persist per-Activity
    start/finish timestamps. The response therefore exposes planned times plus
    the observed lifecycle status and states where actual time is unavailable.
    """

    journey = require(session, Journey, str(journey_id), "Journey")
    plans = session.scalars(
        select(PlanVersion)
        .where(PlanVersion.journey_id == journey.id)
        .order_by(PlanVersion.version_no, PlanVersion.id)
    ).all()
    final_plan = next(
        (item for item in plans if item.id == journey.active_plan_version_id),
        plans[-1] if plans else None,
    )
    final_activities = []
    if final_plan is not None:
        final_activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == final_plan.id)
            .order_by(Activity.sequence_no, Activity.id)
        ).all()

    replans = session.scalars(
        select(ReplanProposalModel)
        .where(ReplanProposalModel.journey_id == journey.id)
        .order_by(ReplanProposalModel.created_at, ReplanProposalModel.id)
    ).all()
    events = session.scalars(
        select(EventModel)
        .where(EventModel.journey_id == journey.id)
        .order_by(EventModel.detected_at, EventModel.id)
    ).all()
    audits = session.scalars(
        select(AuditLog)
        .where(AuditLog.journey_id == journey.id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    agent_actions = [
        _audit_projection(row)
        for row in audits
        if row.actor_type == "AI_AGENT" or row.action.startswith("agent.")
    ]
    provider_disruptions = [
        {
            "id": event.id,
            "provider_key": event.provider_key,
            "provenance": event.provenance,
            "event_type": event.event_type,
            "severity": event.severity,
            "status": event.status,
            "affected_activity_id": event.affected_activity_id,
            "occurred_at": event.occurred_at,
            "detected_at": event.detected_at,
            "resolved_at": event.resolved_at,
        }
        for event in events
        if event.event_type != "USER_CHANGE"
    ]
    initial_cost = int(plans[0].total_cost_minor) if plans else 0
    final_cost = int(final_plan.total_cost_minor) if final_plan is not None else 0
    bookings = session.scalars(
        select(BookingModel).where(BookingModel.journey_id == journey.id)
    ).all()
    captured = session.scalars(
        select(PaymentTransaction).where(
            PaymentTransaction.journey_id == journey.id,
            PaymentTransaction.transaction_type == "CAPTURE",
            PaymentTransaction.status == "SUCCEEDED",
        )
    ).all()
    outcome_events = session.scalars(
        select(JourneyOutcomeEvent)
        .where(JourneyOutcomeEvent.journey_id == journey.id)
        .order_by(JourneyOutcomeEvent.occurred_at_actual, JourneyOutcomeEvent.id)
    ).all()
    actual_by_activity: dict[str, dict[str, str]] = {}
    for outcome in outcome_events:
        item = actual_by_activity.setdefault(outcome.activity_id or "", {})
        if outcome.event_type == "ACTIVITY_STARTED":
            item["actual_start_at"] = outcome.occurred_at_actual
        elif outcome.event_type in {"ACTIVITY_COMPLETED", "ACTIVITY_SKIPPED"}:
            item["actual_end_at"] = outcome.occurred_at_actual
            if outcome.event_type == "ACTIVITY_SKIPPED":
                item["skipped_at"] = outcome.occurred_at_actual
    twin = traveler_twin_payload(refresh_snapshot(session, journey.traveler_id))

    return {
        "journey_id": journey.id,
        "status": journey.status,
        "completed": journey.status == "COMPLETED",
        "timeline": {
            "actual_time_available": journey.status == "COMPLETED"
            or any(
                item.event_type in {"MANUAL_OVERRIDE", "USER_ACCEPTED", "USER_REJECTED"}
                for item in outcome_events
            ),
            "actual_time_note": (
                "Actual transition events are persisted in the Journey outcome timeline."
                if outcome_events
                else "No actual Activity transition has been recorded yet."
            ),
            "activities": [
                {
                    "activity_id": item.id,
                    "title": item.title,
                    "activity_type": item.activity_type,
                    "planned_start_at": item.start_at,
                    "planned_end_at": item.end_at,
                    "observed_status": item.status,
                    "actual_start_at": actual_by_activity.get(item.id, {}).get("actual_start_at"),
                    "actual_end_at": actual_by_activity.get(item.id, {}).get("actual_end_at"),
                    "skipped_at": actual_by_activity.get(item.id, {}).get("skipped_at"),
                    "schedule_drift_min": (
                        round(
                            (
                                datetime.fromisoformat(
                                    actual_by_activity[item.id]["actual_start_at"]
                                )
                                - datetime.fromisoformat(item.start_at)
                            ).total_seconds()
                            / 60
                        )
                        if item.id in actual_by_activity
                        and actual_by_activity[item.id].get("actual_start_at")
                        else None
                    ),
                }
                for item in final_activities
            ],
        },
        "outcome_events": [
            {
                "id": item.id,
                "activity_id": item.activity_id,
                "event_type": item.event_type,
                "actor_type": item.actor_type,
                "occurred_at_actual": item.occurred_at_actual,
                "planned_start_at": item.planned_start_at,
                "planned_end_at": item.planned_end_at,
                "payload": _json_dict(item.payload_json),
            }
            for item in outcome_events
        ],
        "replan_history": [
            {
                "id": item.id,
                "status": item.status,
                "base_plan_version_id": item.base_plan_version_id,
                "proposed_plan_version_id": item.proposed_plan_version_id,
                "created_at": item.created_at,
                "decided_at": item.decided_at,
                "diff": _json_dict(item.diff_json),
            }
            for item in replans
        ],
        "agent_action_history": agent_actions,
        "provider_disruptions": provider_disruptions,
        "cost_summary": {
            "currency": journey.budget_currency,
            "initial_estimated_minor": initial_cost,
            "final_estimated_minor": final_cost,
            "estimated_delta_minor": final_cost - initial_cost,
            "confirmed_booking_minor": sum(
                int(item.amount_minor)
                for item in bookings
                if item.status in {"CONFIRMED", "COMPLETED"}
            ),
            "captured_payment_minor": sum(int(item.amount_minor) for item in captured),
        },
        "plan_version_chain": [
            {
                "id": item.id,
                "version_no": item.version_no,
                "status": item.status,
                "source": item.source,
                "parent_plan_version_id": item.parent_plan_version_id,
                "total_cost_minor": item.total_cost_minor,
            }
            for item in plans
        ],
        "preference_feedback_hook": {
            "available": True,
            "traveler_id": journey.traveler_id,
            "endpoint": f"/api/v1/travelers/{journey.traveler_id}/traveler-twin",
            "mode": "explicit_user_feedback_only",
            "learning": "reflect_to_learned_soft_evidence",
        },
        "traveler_twin": twin,
    }


@app.post("/api/v1/journeys/{journey_id}/reflect/process")
def process_journey_reflect(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    snapshot = learn_from_journey(session, journey)
    record_audit(
        session,
        action="journey.reflect.processed",
        entity_type="TravelerTwinSnapshot",
        entity_id=journey.traveler_id,
        journey_id=journey.id,
        actor_type="USER",
        actor_id=journey.traveler_id,
        after={"traveler_twin_updated": True, "evidence_count": snapshot.evidence_count},
    )
    session.commit()
    return {"journey_id": journey.id, "traveler_twin": traveler_twin_payload(snapshot)}


@app.get("/api/v1/journeys/{journey_id}/observability/traces")
def journey_observability_traces(journey_id: UUID, session: Session = Depends(get_session)):
    journey = require(session, Journey, str(journey_id), "Journey")
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.journey_id == journey.id, AuditLog.action == "trace.span")
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    return {
        "journey_id": journey.id,
        "phoenix": "CONFIGURED" if os.getenv("CITYNEXUS_PHOENIX_ENDPOINT") else "NOT_CONFIGURED",
        "spans": [
            {
                "trace_id": row.trace_id,
                "created_at": row.created_at,
                **_json_dict(row.after_json),
            }
            for row in rows
        ],
    }


def _activity_coordinate(session: Session, activity: Activity) -> list[float] | None:
    key = activity.place_id or activity.city_service_id
    if not key:
        return None
    place = session.get(Place, key)
    if place is None:
        service = session.get(CityService, key)
        if service is not None and service.place_id:
            place = session.get(Place, service.place_id)
    if place is None:
        return None
    return [float(place.longitude), float(place.latitude)]


def _frozen_route_for_plan(
    session: Session,
    plan: PlanVersion,
    snapshot: MobilitySnapshot | None,
) -> tuple[list[list[float]], bool, str | None]:
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    coordinates: list[list[float]] = []
    used_provider_geometry = True
    segment_count = 0
    fallback_reason: str | None = None
    previous_key: str | None = None
    previous_coordinate: list[float] | None = None
    for activity in activities:
        coordinate = _activity_coordinate(session, activity)
        key = activity.place_id or activity.city_service_id
        if coordinate is None or key is None:
            previous_key = key
            previous_coordinate = coordinate
            continue
        if not coordinates:
            coordinates.append(coordinate)
        elif previous_key and previous_coordinate:
            segment_count += 1
            fact = snapshot.facts.get((previous_key, key)) if snapshot is not None else None
            geometry = list(fact.route_geometry) if fact is not None else []
            if len(geometry) >= 2:
                points = [list(point) for point in geometry]
                if points[0] != coordinates[-1]:
                    coordinates.append(points[0])
                coordinates.extend(points[1:])
            else:
                used_provider_geometry = False
                fallback_reason = fallback_reason or (
                    "NO_FROZEN_ROUTE_GEOMETRY"
                    if snapshot is not None
                    else "NO_FROZEN_MOBILITY_SNAPSHOT"
                )
                coordinates.append(coordinate)
        previous_key = key
        previous_coordinate = coordinate
    if segment_count == 0:
        used_provider_geometry = False
        fallback_reason = fallback_reason or "ROUTE_HAS_NO_TRAVEL_SEGMENTS"
    return coordinates, used_provider_geometry, fallback_reason


def _route_feature(
    coordinates: list[list[float]],
    *,
    provider: str,
    provider_backed: bool,
    fallback_reason: str | None,
    plan_version_id: str,
) -> dict[str, Any] | None:
    # A route needs two places to be a line.  Returning a hard-coded city here made the map draw (and
    # centre on) a route the plan never had, and duplicating a single point drew a zero-length line for
    # a plan that has no travel yet; the client already refuses to invent a line, so the server must not
    # either.  `None` lets JourneyMap take its "nothing honest to draw" branch.
    if len(coordinates) < 2:
        return None
    safe_coordinates = coordinates
    return {
        "type": "Feature",
        "properties": {
            "provider": provider,
            "provider_backed": provider_backed,
            "fallback": not provider_backed,
            "fallback_reason": fallback_reason,
            "plan_version_id": plan_version_id,
        },
        "geometry": {"type": "LineString", "coordinates": safe_coordinates},
    }


def _marker_feature(
    activity: Activity | None,
    coordinate: list[float] | None,
    *,
    role: str,
    title: str,
) -> dict[str, Any] | None:
    if coordinate is None:
        return None
    return {
        "type": "Feature",
        "properties": {
            "role": role,
            "title": title,
            "activity_id": activity.id if activity else None,
        },
        "geometry": {"type": "Point", "coordinates": coordinate},
    }


def _disruption_scope_feature(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("disruption_polygon") or payload.get("polygon")
    if isinstance(raw, dict) and raw.get("type") == "Feature":
        geometry = raw.get("geometry")
    elif isinstance(raw, dict):
        geometry = raw
    else:
        # Accept both GeoJSON ring coordinates and a direct list of points
        # from lightweight event producers.  GeoJSON Polygon coordinates are
        # one level deeper than a single ring; normalize the latter here so
        # the map projection and the impact resolver consume the same shape.
        coordinates = raw
        if (
            isinstance(raw, list)
            and raw
            and isinstance(raw[0], list)
            and len(raw[0]) >= 2
            and isinstance(raw[0][0], (int, float))
        ):
            coordinates = [raw]
        geometry = {"type": "Polygon", "coordinates": coordinates} if raw else None
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    return {
        "type": "Feature",
        "properties": {"role": "disruption-scope", "title": "Disruption scope"},
        "geometry": {
            "type": geometry["type"],
            "coordinates": geometry.get("coordinates", []),
        },
    }


@app.get("/api/v1/journeys/{journey_id}/route-evidence")
def journey_route_evidence(
    journey_id: UUID,
    proposal_id: UUID | None = None,
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    proposal = None
    if proposal_id is not None:
        proposal = session.get(ReplanProposalModel, str(proposal_id))
        if proposal is None or proposal.journey_id != journey.id:
            raise HTTPException(404, "Replan proposal was not found on this Journey")
    elif journey.active_plan_version_id is not None:
        proposal = session.scalar(
            select(ReplanProposalModel)
            .where(
                ReplanProposalModel.journey_id == journey.id,
                ReplanProposalModel.status == "PRESENTED",
            )
            .order_by(ReplanProposalModel.created_at.desc(), ReplanProposalModel.id.desc())
        )
    base_plan_id = proposal.base_plan_version_id if proposal else journey.active_plan_version_id
    if base_plan_id is None:
        raise HTTPException(409, "Journey has no active plan for route evidence")
    base_plan = require(session, PlanVersion, base_plan_id, "PlanVersion")
    snapshot = (
        _proposal_mobility_snapshot(proposal)
        if proposal
        else _latest_replan_mobility_snapshot(session, journey.id)
    )
    old_coordinates, old_provider_backed, old_fallback_reason = _frozen_route_for_plan(
        session, base_plan, snapshot
    )
    old_route = _route_feature(
        old_coordinates,
        provider=snapshot.source_provider_key if snapshot else "server-place-fallback",
        provider_backed=old_provider_backed,
        fallback_reason=old_fallback_reason,
        plan_version_id=base_plan.id,
    )
    new_route = None
    if proposal is not None:
        proposed_plan = require(
            session, PlanVersion, proposal.proposed_plan_version_id, "PlanVersion"
        )
        new_coordinates, new_provider_backed, new_fallback_reason = _frozen_route_for_plan(
            session, proposed_plan, snapshot
        )
        new_route = _route_feature(
            new_coordinates,
            provider=snapshot.source_provider_key if snapshot else "server-place-fallback",
            provider_backed=new_provider_backed,
            fallback_reason=new_fallback_reason,
            plan_version_id=proposed_plan.id,
        )

    active_plan = (
        session.get(PlanVersion, journey.active_plan_version_id)
        if journey.active_plan_version_id
        else base_plan
    )
    active_activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == active_plan.id)
        .order_by(Activity.sequence_no)
    ).all()
    by_id = {item.id: item for item in active_activities}
    current = by_id.get(journey.current_activity_id or "")
    current_index = active_activities.index(current) if current in active_activities else -1
    next_activity = (
        active_activities[current_index + 1]
        if current_index >= 0 and current_index + 1 < len(active_activities)
        else None
    )
    c4_activity_ids = {
        item.activity_id
        for item in session.scalars(
            select(CommitmentModel).where(
                CommitmentModel.journey_id == journey.id,
                CommitmentModel.status == "ACTIVE",
                CommitmentModel.level == "C4_Anchored",
            )
        ).all()
        if item.activity_id
    }
    markers: list[dict[str, Any]] = []
    for activity, role, title in (
        (current, "current", "Current activity"),
        (next_activity, "next", "Next activity"),
    ):
        marker = _marker_feature(
            activity,
            _activity_coordinate(session, activity) if activity else None,
            role=role,
            title=title,
        )
        if marker:
            markers.append(marker)
    for activity_id in sorted(c4_activity_ids):
        activity = by_id.get(activity_id)
        marker = _marker_feature(
            activity,
            _activity_coordinate(session, activity) if activity else None,
            role="c4",
            title="C4 protected commitment",
        )
        if marker:
            markers.append(marker)
    trigger = (
        session.get(EventModel, proposal.trigger_event_id)
        if proposal and proposal.trigger_event_id
        else session.scalar(
            select(EventModel)
            .where(EventModel.journey_id == journey.id)
            .order_by(EventModel.occurred_at.desc(), EventModel.id.desc())
        )
    )
    disruption_activity = (
        by_id.get(trigger.affected_activity_id)
        if trigger and trigger.affected_activity_id
        else None
    )
    disruption_coordinate = (
        _activity_coordinate(session, disruption_activity) if disruption_activity else None
    )
    if disruption_coordinate is None and trigger is not None:
        payload = _json_dict(trigger.payload_json)
        if payload.get("longitude") is not None and payload.get("latitude") is not None:
            disruption_coordinate = [float(payload["longitude"]), float(payload["latitude"])]
    marker = _marker_feature(
        disruption_activity, disruption_coordinate, role="disruption", title="Disruption scope"
    )
    if marker:
        markers.append(marker)
    disruption_payload = _json_dict(trigger.payload_json) if trigger is not None else {}
    disruption_scope = _disruption_scope_feature(disruption_payload)
    impact_evidence = (
        _json_dict(proposal.diff_json).get("verified_evidence", {}).get("impact_resolution", {})
        if proposal is not None
        else {}
    )
    impacted_activity_ids = list(
        dict.fromkeys(
            [
                item
                for item in [
                    impact_evidence.get("selected_activity_id"),
                    *(impact_evidence.get("secondary_risk_activity_ids") or []),
                ]
                if item
            ]
        )
    )
    old_props = old_route["properties"] if old_route else {}
    new_props = new_route["properties"] if new_route else {}
    return {
        "journey_id": journey.id,
        "proposal_id": proposal.id if proposal else None,
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "provider": snapshot.source_provider_key if snapshot else "server-place-fallback",
        "frozen": snapshot is not None,
        "old_route": old_route,
        "new_route": new_route,
        "disruption_scope": disruption_scope,
        "impacted_activity_ids": impacted_activity_ids,
        "markers": {"type": "FeatureCollection", "features": markers},
        "fallback": bool(
            old_props.get("fallback", True)
            or (new_props.get("fallback", True) if new_route else False)
        ),
        "fallback_reason": old_props.get("fallback_reason")
        or (new_props.get("fallback_reason") if new_route else None)
        or "ROUTE_HAS_NO_TRAVEL_SEGMENTS",
        "source": "frozen_mobility_snapshot" if snapshot else "server_place_coordinates",
    }


def _replan_payload(item: ReplanProposalModel) -> dict[str, Any]:
    return {
        "id": item.id,
        "journey_id": item.journey_id,
        "trigger_event_id": item.trigger_event_id,
        "base_plan_version_id": item.base_plan_version_id,
        "proposed_plan_version_id": item.proposed_plan_version_id,
        "status": item.status,
        "hard_constraints_passed": bool(item.hard_constraints_passed),
        "locked_commitments_preserved": bool(item.locked_commitments_preserved),
        "utility_score": item.utility_score,
        "score_breakdown": json.loads(item.score_breakdown_json or "{}"),
        "diff": json.loads(item.diff_json or "{}"),
        "explanation": json.loads(item.explanation_json or "{}"),
    }


@dataclass(frozen=True)
class _ReplanCandidateSpec:
    family: str
    replacement_choice: Any | None
    candidate_delay: int = 0
    candidate_remove: bool = False
    window_evidence: dict[str, Any] | None = None
    explicit_candidate_start_at: datetime | None = None
    solver_provenance: dict[str, Any] | None = None
    v2_plan: SolverV2Plan | None = None
    v2_choices: dict[tuple[str, str], Any] | None = None


class MobilityDataUnavailable(RuntimeError):
    """No explicit frozen or measured travel fact exists for a replan leg.

    Distinct from "no feasible alternative": the engine never learned how long
    the leg takes, so it must not declare a candidate infeasible (or feasible)
    on the strength of a fabricated constant.  See
    docs/MOBILITY_TRUTH_CONTRACT.md (T2).

    `reason_code` / `evidence_state` ride on the exception so the collection
    sites name the gap from a structured attribute instead of string-matching a
    message - ORD-0199 (①) needs the product to emit a named reason, and a reason
    derived by `in str(exc)` would be one renamed sentence away from disappearing.
    """

    reason_code = "MOBILITY_DATA_UNAVAILABLE"
    evidence_state = "NO_QUALIFIED_FACT_FOR_LEG"

    def __init__(self, origin: str | None, destination: str | None) -> None:
        self.origin = origin
        self.destination = destination
        super().__init__(f"{self.reason_code}:{origin}|{destination}")


class MobilityEvidenceNotHardEligible(MobilityDataUnavailable):
    """The leg HAS a fact, and its recorded quality is named - just not an eligible one.

    This is the second sentence, and it is not a variation of the first: "we never
    learned this leg" and "we have a number, and it is the mock fixture's blanket
    constant" point at different owners and different fixes.  Naming it is what
    ORD-0199 asks for; the name must never be rendered as a network/offline
    problem (R14-P, the reverse face of the rule that forbids writing an outage as
    "no such place").  A leg in this state still may not be priced by an invented
    constant, so the candidate is excluded exactly as before - only the reason is
    more honest.
    """

    reason_code = "MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE"
    evidence_state = "FACT_PRESENT_BUT_NOT_HARD_ELIGIBLE"

    def __init__(
        self, origin: str | None, destination: str | None, fact_quality: str | None
    ) -> None:
        self.fact_quality = fact_quality
        super().__init__(origin, destination)


def _gap_cell(
    origin: str | None,
    destination: str | None,
    reason_code: str,
    evidence_state: str,
    fact_quality: str | None = None,
) -> dict[str, Any]:
    """One leg's gap cell - the shape the envelope contract and the screen both read.

    Key set stays constant (a strict client-side shape check drops an envelope over
    missing keys), and `fact_quality` rides along only when the exception actually
    carries one: an absent quality is not the same as an empty one.
    """

    cell: dict[str, Any] = {
        "from_place_id": origin or None,
        "to_place_id": destination or None,
        "reason_code": reason_code,
        "evidence_state": evidence_state,
    }
    if fact_quality is not None:
        cell["fact_quality"] = fact_quality
    return cell


def _mobility_gap(text: str) -> dict[str, Any]:
    """Turn a collected leg message back into a named gap.

    The collection site stores the exception's own text, so the endpoints are parsed back out
    here rather than duplicated at every append.  An unparseable end stays `None`: inventing a
    place id to fill a column would be the same fabrication the missing leg already refuses.

    The product's live path is :func:`_mobility_gap_from`, which reads the reason off the
    exception's attributes; this parser stays for the collected messages (and the door that
    pins their shape) and can only ever name the base reason - which is exactly why the
    structured path exists.
    """

    _, _, argument = str(text).partition(":")
    origin, separator, destination = argument.partition("|")
    return _gap_cell(
        origin or None,
        destination or None if separator else None,
        MobilityDataUnavailable.reason_code,
        MobilityDataUnavailable.evidence_state,
    )


def _hard_leg_minutes(mobility: Any, origin: str, destination: str) -> int:
    """The leg's minutes, through the product's own allowlist - naming why it refused.

    `hard_minutes` is the gate (V3-1 / V4 #1 / V6): only a quality in
    ``HARD_FEASIBLE_QUALITIES`` may drive a hard feasibility decision.  This helper
    adds nothing to that rule - it reads the same mapping the gate reads so the
    *reason* can be named from the fact itself rather than inferred from which
    exception class happened to be raised:

    * no fact for the pair, or a fact with NO recorded quality -> the existing
      ``MOBILITY_DATA_UNAVAILABLE``: we cannot say what that number is;
    * a fact whose quality is recorded and is not eligible (today:
      ``SYNTHETIC_DEFAULT``, the fixture's blanket constant) ->
      ``MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE`` (ORD-0199 ①): we can say exactly what
      it is, and it is not a measurement.
    """

    facts = getattr(mobility, "facts", None)
    fact = facts.get((origin, destination)) if isinstance(facts, Mapping) else None
    if (
        fact is not None
        and fact.fact_quality is not None
        and fact.fact_quality not in HARD_FEASIBLE_QUALITIES
    ):
        raise MobilityEvidenceNotHardEligible(origin, destination, fact.fact_quality)
    hard_minutes = getattr(mobility, "hard_minutes", None)
    if callable(hard_minutes):
        return int(hard_minutes(origin, destination))
    return int(mobility.estimate_minutes(origin, destination))


def _mobility_gap_from(exc: MobilityDataUnavailable) -> dict[str, Any]:
    """The projection cell for one refused leg, read off the exception's attributes.

    Deliberately not parsed out of the message: a reason that survives only as long
    as somebody's sentence keeps its wording is not a contract field.
    """

    return _gap_cell(
        exc.origin,
        exc.destination,
        exc.reason_code,
        exc.evidence_state,
        getattr(exc, "fact_quality", None),
    )


def _replan_estimate(
    session: Session,
    journey: Journey,
    mobility: Any,
    origin: str | None,
    destination: str | None,
    *,
    operation_key: str,
) -> int:
    """Travel minutes for a replan leg, capturing a missing pair if needed.

    Frozen evidence was captured for the stop order that existed when the plan
    was selected.  A replan creates new adjacencies, so a pair can be absent;
    that is a gap to fill from a *real* provider, not a reason to fail the
    request — and not a licence to invent a constant.

    The capture is therefore strict: a pair with no explicit frozen or measured
    fact raises :class:`MobilityDataUnavailable` instead of inheriting the mock
    fixture's blanket ``default_minutes`` (T2).
    """

    if not origin or not destination or origin == destination:
        return 0
    known_refusal: MobilityDataUnavailable | None = None
    try:
        return _hard_leg_minutes(mobility, origin, destination)
    except (KeyError, MobilityEvidenceNotHardEligible) as exc:
        # Two different gaps reach the same next step: the pair is absent from the
        # frozen evidence, or the only number it carries is a named constant the
        # allowlist rejects.  A replan creates new adjacencies, so both get one
        # strict attempt at a real provider - and neither is a licence to invent.
        known_refusal = exc if isinstance(exc, MobilityDataUnavailable) else None
        try:
            captured = capture_runtime_mobility_snapshot(
                session,
                journey,
                [(origin, destination)],
                operation_key=operation_key,
                require_explicit_facts=True,
            ).snapshot
        except (MobilitySnapshotCaptureError, KeyError) as capture_exc:
            # The recapture said nothing; what we already knew about this leg still
            # stands.  Naming the fixture constant we hold is more true than falling
            # back to "we never learned it" the moment a fresh attempt fails.
            raise (known_refusal or MobilityDataUnavailable(origin, destination)) from (
                capture_exc
            )
        return _hard_leg_minutes(captured, origin, destination)


@app.post(
    "/api/v1/journeys/{journey_id}/replans", response_model=ReplanProposalSchema, status_code=201
)
def generate_replan(
    journey_id: UUID,
    body: ReplanGenerateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
):
    journey = require(session, Journey, str(journey_id), "Journey")
    if journey.status != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "journey must be ACTIVE with an active plan")
    base = session.get(PlanVersion, journey.active_plan_version_id)
    trigger_id = str(body.trigger_event_id)
    trigger_event = None
    if base is None or base.status != "ACTIVE":
        raise HTTPException(409, "active plan is missing or inconsistent")
    trigger_event = session.get(EventModel, trigger_id)
    if trigger_event is None or trigger_event.journey_id != journey.id:
        raise HTTPException(422, "trigger event is not on this journey")
    if trigger_event.status not in {"DETECTED", "ACKNOWLEDGED"}:
        raise HTTPException(409, "trigger event is no longer actionable")
    handled = session.scalar(
        select(ReplanProposalModel.id).where(
            ReplanProposalModel.trigger_event_id == trigger_id,
            ReplanProposalModel.status == "ACCEPTED",
        )
    )
    if handled is not None:
        raise HTTPException(409, "trigger event has already been handled by an accepted replan")

    generation_idempotency_key: str | None = None
    generation_signature: str | None = None
    if idempotency_key is not None:
        try:
            header_key = validate_idempotency_key(idempotency_key)
        except CommerceValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        generation_signature = canonical_request_signature(
            {
                "journey_id": journey.id,
                "base_plan_version_id": base.id,
                "trigger_event_id": trigger_id,
                "candidate_count": body.candidate_count,
            }
        )
        generation_idempotency_key = f"replan.generate:{header_key}"
        existing_generation = session.get(IdempotencyKey, generation_idempotency_key)
        if existing_generation is not None:
            if existing_generation.request_hash != generation_signature:
                raise HTTPException(409, "same idempotency key used for a different request")
            stored = json.loads(existing_generation.response_json or "{}")
            if not stored:
                raise HTTPException(409, "replan generation idempotency record is incomplete")
            return stored

    source = session.scalars(
        select(Activity).where(Activity.plan_version_id == base.id).order_by(Activity.sequence_no)
    ).all()
    c4_rows = session.scalars(
        select(CommitmentModel).where(
            CommitmentModel.journey_id == journey.id,
            CommitmentModel.status == "ACTIVE",
            CommitmentModel.level == "C4_Anchored",
        )
    ).all()
    c4_anchors = []
    for row in c4_rows:
        policy = json.loads(row.policy_json or "{}")
        semantic_key = policy.get("semantic_key")
        if not semantic_key:
            legacy_key = str(policy.get("semantic_node_key") or "")
            matching = next(
                (
                    item
                    for item in source
                    if str(item.place_id or item.city_service_id or "").endswith(f":{legacy_key}")
                    or str(_json_object(item.metadata_json, {}).get("semantic_key") or "").endswith(f":{legacy_key}")
                ),
                None,
            )
            semantic_key = matching.place_id or matching.city_service_id if matching else None
        if semantic_key and policy.get("scheduled_start_at"):
            c4_anchors.append((str(semantic_key), str(policy["scheduled_start_at"])))
    places_by_id = {
        place.id: place
        for place in session.scalars(
            select(Place).where(Place.city_code == journey.city_code).order_by(Place.id)
        ).all()
    }
    frozen_v12_evidence = _json_object(base.mobility_evidence_json, {})
    frozen_provider_evidence = frozen_v12_evidence.get("provider_evidence")
    if not isinstance(frozen_provider_evidence, dict):
        frozen_provider_evidence = {}
    discovery_evidence = frozen_v12_evidence.get("discovery")
    if not isinstance(discovery_evidence, dict):
        discovery_evidence = {}
    provider_mobility_reserve = discovery_evidence.get("provider_mobility")
    if not isinstance(provider_mobility_reserve, dict):
        provider_mobility_reserve = {}
    fully_grounded_recovery = bool(
        frozen_provider_evidence.get("fully_provider_grounded")
        or frozen_provider_evidence.get("fully_grounded")
    ) and bool(provider_mobility_reserve.get("facts"))
    grounded_recovery_aliases = (
        _grounded_recovery_provider_aliases(source, places_by_id, provider_mobility_reserve)
        if fully_grounded_recovery
        else {}
    )
    grounded_recovery_place_ids = {
        location
        for provider_node, location in grounded_recovery_aliases.items()
        if provider_node.startswith("place:") and location in places_by_id
    }
    impact_resolution = resolve_impact(
        event=trigger_event,
        journey=journey,
        activities=source,
        places_by_id=places_by_id,
        c4_activity_ids={row.activity_id for row in c4_rows if row.activity_id},
    )
    assessment = persist_impact_assessment(session, impact_resolution)
    selected_impact_explanation = next(
        (
            item.human_explanation
            for item in impact_resolution.validation.candidates
            if item.activity_id == impact_resolution.selected_activity_id
        ),
        "",
    )
    impact_facts = {
        "assessment_id": assessment.id,
        "status": impact_resolution.status,
        "maturity_stage": impact_resolution.maturity_stage,
        "selected_activity_id": impact_resolution.selected_activity_id,
        "primary_recovery_target_id": impact_resolution.primary_recovery_target_id,
        "secondary_risk_activity_ids": list(impact_resolution.secondary_risk_activity_ids),
        "independent_evidence_count": impact_resolution.independent_evidence_count,
        "corroboration_refs": list(impact_resolution.selected_corroboration_refs),
        "reason_codes": list(impact_resolution.validation.reason_codes),
        "provider_key": impact_resolution.provider_key,
        "model_key": impact_resolution.model_key,
        "semantic_explanation": selected_impact_explanation,
    }
    record_audit(
        session,
        action="impact_hypothesis_created",
        entity_type="ImpactAssessment",
        entity_id=assessment.id,
        journey_id=journey.id,
        after=impact_facts,
    )
    if impact_resolution.selected_activity_id is None:
        record_audit(
            session,
            action="impact_rejected",
            entity_type="ImpactAssessment",
            entity_id=assessment.id,
            journey_id=journey.id,
            after=impact_facts,
        )
    elif impact_resolution.maturity_stage == "VALIDATED_IMPACT":
        record_audit(
            session,
            action="impact_validated",
            entity_type="ImpactAssessment",
            entity_id=assessment.id,
            journey_id=journey.id,
            after=impact_facts,
        )
    elif impact_resolution.maturity_stage == "AI_ONLY":
        record_audit(
            session,
            action="impact_hypothesis_selected",
            entity_type="ImpactAssessment",
            entity_id=assessment.id,
            journey_id=journey.id,
            after=impact_facts,
        )
    else:
        record_audit(
            session,
            action="impact_corroborated",
            entity_type="ImpactAssessment",
            entity_id=assessment.id,
            journey_id=journey.id,
            after=impact_facts,
        )
    # The Event remains the raw provider/user fact.  A semantic model target
    # is scoped to this recovery operation and lives in ImpactAssessment.
    effective_affected_activity_id = impact_resolution.selected_activity_id
    # Impact evidence is independently durable even when no recovery proposal
    # can be produced. A later candidate-generation rollback must not erase
    # the corroboration decision that explains the failed recovery attempt.
    session.commit()
    solver_mode = normalize_solver_mode(
        body.solver_mode or os.getenv("CITYNEXUS_SOLVER", settings.solver_mode)
    )
    # Protection only means something for a *resolved* activity.  An imported
    # booking commitment legitimately carries activity_id=None, so the old
    # `affected_activity_id in {row.activity_id ...}` test evaluated
    # `None in {None}` and rejected every closure on a booking-first journey
    # with a false "protected" 422.  Compare only real, non-null identities, and
    # honour the semantically resolved target as well as the raw event target.
    protected_activity_ids = {
        str(row.activity_id) for row in c4_rows if row.activity_id is not None
    }
    closure_target_ids = [
        str(value)
        for value in (trigger_event.affected_activity_id, effective_affected_activity_id)
        if value is not None
    ]
    if (
        trigger_event.event_type in {"CLOSURE", "WEATHER"}
        and protected_activity_ids
        and any(value in protected_activity_ids for value in closure_target_ids)
    ):
        raise HTTPException(
            422, "closed activity is protected and cannot be replaced automatically"
        )
    partition = partition_execution(
        source,
        current_activity_id=journey.current_activity_id,
        c4_anchors=c4_anchors,
    )
    affected_place = None
    replacement_choices = []
    affected_activity_id = None
    affected_place_id = None
    delay_minutes = 0
    remove_affected = False
    payload = json.loads(trigger_event.payload_json or "{}")
    if trigger_event.event_type in {"CLOSURE", "WEATHER", "TRAFFIC_DELAY", "SCHEDULE_DELAY"}:
        affected_activity_id = effective_affected_activity_id
        if affected_activity_id is None and payload.get("affected_place_id"):
            affected_place_id = str(payload["affected_place_id"])
            match = next((a for a in source if a.place_id == affected_place_id), None)
            affected_activity_id = match.id if match else None
        if affected_activity_id is None and trigger_event.event_type in {
            "TRAFFIC_DELAY",
            "SCHEDULE_DELAY",
        }:
            affected_activity_id = journey.current_activity_id
        if affected_activity_id is None and trigger_event.event_type in {
            "CLOSURE",
            "WEATHER",
        }:
            # Truth blocker B4 (tightened for Snapshot K): "有个景点关了" does
            # NOT prove which activity is closed, and neither does
            # ``journey.current_activity_id`` on its own — the traveller being
            # somewhere does not mean *that* place is the one that closed.
            #
            # A target is therefore used automatically only when the event
            # itself grounds it:
            #   * an explicit affected activity / place id, or
            #   * an explicit assertion that the disruption is at the
            #     traveller's current position (``at_current_location`` /
            #     ``grounded_activity_id`` supplied by the reporter or by a
            #     trusted provider linkage).
            #
            # Anything else becomes clarification-required.  Guessing the
            # current or the first activity is forbidden.
            grounded = str(
                payload.get("affected_activity_id")
                or payload.get("grounded_activity_id")
                or ""
            ).strip()
            if grounded:
                affected_activity_id = next(
                    (item.id for item in source if item.id == grounded), None
                )
            asserts_current = bool(
                payload.get("at_current_location")
                or payload.get("disruption_at_current_position")
            )
            if affected_activity_id is None and asserts_current and journey.current_activity_id:
                affected_activity_id = next(
                    (
                        item.id
                        for item in source
                        if item.id == journey.current_activity_id
                        and item.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
                    ),
                    None,
                )
            if affected_activity_id is None:
                record_audit(
                    session,
                    action="replan.target_unresolved",
                    entity_type="Event",
                    entity_id=trigger_event.id,
                    journey_id=journey.id,
                    after={
                        "event_type": trigger_event.event_type,
                        "reason": "AMBIGUOUS_TARGET_REQUIRES_CLARIFICATION",
                        "policy": "no_guess_current_or_first_activity",
                    },
                )
                raise HTTPException(422, "REPLAN_TARGET_UNRESOLVED")
        affected = next((a for a in source if a.id == affected_activity_id), None)
        if affected is not None:
            affected_place_id = affected.place_id
            if affected.status in {"COMPLETED", "SKIPPED", "CANCELLED"}:
                raise HTTPException(409, "disruption targets an activity that is already terminal")
            if affected.locked and trigger_event.event_type in {"CLOSURE", "WEATHER"}:
                raise HTTPException(
                    422, "closed activity is protected and cannot be replaced automatically"
                )
            if trigger_event.event_type in {"CLOSURE", "WEATHER"}:
                affected_place = (
                    session.get(Place, affected.place_id) if affected.place_id else None
                )
                if affected_place is None:
                    raise HTTPException(422, "affected place is unavailable")
                places = session.scalars(
                    select(Place)
                    .where(
                        Place.city_code == journey.city_code,
                        # Authoritative structural nodes (check-in, stay,
                        # anchors) are server-owned schedule facts, not
                        # replacement POIs.  They use distinct semantic keys
                        # in the sealed matrix and must never enter a
                        # candidate family as if they were a discoverable
                        # city service.
                        Place.provider_key.notin_(_SERVER_OWNED_PLACE_PROVIDERS),
                    )
                    .order_by(Place.id)
                ).all()
                if fully_grounded_recovery:
                    places = [
                        place for place in places if place.id in grounded_recovery_place_ids
                    ]
                replacement_choices = list(
                    rank_closure_replacements(
                        affected_place=affected_place,
                        candidate_places=places,
                        occupied_place_ids={a.place_id for a in source if a.place_id},
                        # Identity is physical, not textual: a venue can be
                        # stored under a domain-wrapper id and a provider id at
                        # once.  Passing the rows lets the engine keep every
                        # already-used venue out of the family.
                        occupied_places=[
                            place
                            for place in (
                                session.get(Place, a.place_id)
                                for a in source
                                if a.place_id
                            )
                            if place is not None
                        ],
                        max_duration_min=max(
                            1,
                            int(
                                (
                                    datetime.fromisoformat(affected.end_at)
                                    - datetime.fromisoformat(affected.start_at)
                                ).total_seconds()
                                // 60
                            ),
                        ),
                        traveler_preferences=planning_preference_items(
                            session, journey.traveler_id, journey.id
                        ),
                    )
                )
                if not replacement_choices:
                    remove_affected = True
            else:
                raw_delay = (
                    payload.get("extra_minutes", payload.get("delay_minutes", 15))
                    if trigger_event.event_type == "TRAFFIC_DELAY"
                    else payload.get("delay_minutes", payload.get("extra_minutes", 15))
                )
                delay_minutes = max(1, min(int(raw_delay), 120))
    latest_version = (
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    )
    affected = next((item for item in source if item.id == affected_activity_id), None)
    protected_activity_ids = set(partition.c4_anchor_ids)
    requested_protected_ids = {str(item) for item in body.protected_activity_ids}
    active_activity_ids = {item.id for item in source}
    if not requested_protected_ids.issubset(active_activity_ids):
        raise HTTPException(422, "protected activity is not in the active Journey plan")
    protected_activity_ids.update(requested_protected_ids)
    if affected_activity_id in protected_activity_ids and trigger_event.event_type in {
        "CLOSURE",
        "WEATHER",
    }:
        raise HTTPException(422, "affected activity is protected by the Journey mission")
    effective_event_time_source = trigger_event.occurred_at
    if trigger_event.event_type == "WEATHER" and trigger_event.provenance == "PROVIDER_VERIFIED":
        effective_event_time_source = str(
            payload.get("snapshot_captured_at")
            or trigger_event.detected_at
            or trigger_event.occurred_at
        )
    effective_event_time = datetime.fromisoformat(effective_event_time_source)
    # Normalize every Event instant onto the Journey schedule timeline before
    # evaluating local opening hours or OpportunityWindow evidence. Real
    # providers such as Open-Meteo emit UTC instants while the plan stores
    # local scheduled offsets.
    journey_reference = datetime.fromisoformat(journey.start_at)
    try:
        journey_tz = ZoneInfo(journey.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        journey_tz = journey_reference.tzinfo
    if effective_event_time.tzinfo is None:
        effective_event_time = effective_event_time.replace(tzinfo=journey_tz)
    elif journey_tz is not None:
        effective_event_time = effective_event_time.astimezone(journey_tz)

    def _activity_location(item: Activity | None) -> str | None:
        """Routing identity of one activity - see module-level `_activity_routing_key`.

        Delegated rather than duplicated: the evidence is keyed by
        ``metadata.semantic_key``, and the read that counts coverage
        (`_mobility_evidence_coverage`) has to ask with the very same key, or the two
        drift and one of them reports legs the other never requested.
        """

        return _activity_routing_key(item)

    mobility_pairs: set[tuple[str, str]] = set()

    # Truth blocker B5: one canonical key space for every requested leg.
    # Callers legitimately pass either a database Place id or an activity's
    # canonical ``semantic_key``; the frozen evidence and the gate are keyed on
    # ``semantic_key``, so any Place id is normalised to its activity identity
    # here instead of producing an unresolvable pair.
    _place_to_semantic: dict[str, str] = {}
    for _item in source:
        _meta = _json_object(_item.metadata_json, {})
        _semantic = str(_meta.get("semantic_key") or "").strip()
        if not _semantic:
            continue
        if _item.place_id:
            _place_to_semantic.setdefault(str(_item.place_id), _semantic)
        if _item.city_service_id:
            _place_to_semantic.setdefault(str(_item.city_service_id), _semantic)

    def _canonical_key(value: str | None) -> str | None:
        if not value:
            return None
        text = str(value)
        return _place_to_semantic.get(text, text)

    def _need_pair(origin: str | None, destination: str | None) -> None:
        left = _canonical_key(origin)
        right = _canonical_key(destination)
        if left and right and left != right:
            mobility_pairs.add((left, right))

    for previous, current in zip(source, source[1:]):
        _need_pair(_activity_location(previous), _activity_location(current))

    current_execution = next(
        (item for item in source if item.id == journey.current_activity_id),
        None,
    )
    v2_choice_pools: dict[str, tuple[Any, ...]] = {}
    if solver_mode == "v2" and trigger_event.event_type in {"CLOSURE", "WEATHER"}:
        candidate_places = session.scalars(
            select(Place)
            .where(
                Place.city_code == journey.city_code,
                Place.provider_key.notin_(_SERVER_OWNED_PLACE_PROVIDERS),
            )
            .order_by(Place.id)
        ).all()
        if fully_grounded_recovery:
            candidate_places = [
                place for place in candidate_places if place.id in grounded_recovery_place_ids
            ]
        occupied_place_ids = {item.place_id for item in source if item.place_id}
        occupied_places = [
            place
            for place in (session.get(Place, item.place_id) for item in source if item.place_id)
            if place is not None
        ]
        current_sequence = current_execution.sequence_no if current_execution is not None else -1
        for item in source:
            if (
                item.sequence_no < current_sequence
                or item.id in protected_activity_ids
                or item.status in {"COMPLETED", "SKIPPED", "CANCELLED"}
                or item.locked
                or not item.place_id
            ):
                continue
            if item.id == affected_activity_id:
                choices = tuple(replacement_choices)
            else:
                item_place = session.get(Place, item.place_id)
                choices = (
                    rank_closure_replacements(
                        affected_place=item_place,
                        candidate_places=candidate_places,
                        occupied_place_ids=occupied_place_ids,
                        occupied_places=occupied_places,
                        max_duration_min=max(
                            1,
                            int(
                                (
                                    datetime.fromisoformat(item.end_at)
                                    - datetime.fromisoformat(item.start_at)
                                ).total_seconds()
                                // 60
                            ),
                        ),
                        traveler_preferences=planning_preference_items(
                            session, journey.traveler_id, journey.id
                        ),
                    )
                    if item_place is not None
                    else ()
                )
            v2_choice_pools[item.id] = tuple(choices)
    affected_index = source.index(affected) if affected in source else -1
    previous_affected = source[affected_index - 1] if affected_index > 0 else None
    next_affected = (
        source[affected_index + 1]
        if affected_index >= 0 and affected_index + 1 < len(source)
        else None
    )
    future_anchor = (
        min(
            (datetime.fromisoformat(start), semantic)
            for semantic, start in c4_anchors
            if datetime.fromisoformat(start) > effective_event_time
        )
        if any(datetime.fromisoformat(start) > effective_event_time for _, start in c4_anchors)
        else None
    )
    for choice in replacement_choices:
        _need_pair(_activity_location(current_execution or previous_affected), choice.place_id)
        _need_pair(_activity_location(previous_affected), choice.place_id)
        _need_pair(choice.place_id, _activity_location(next_affected))
        if future_anchor is not None:
            _need_pair(choice.place_id, future_anchor[1])
    _need_pair(_activity_location(previous_affected), _activity_location(next_affected))
    if future_anchor is not None:
        _need_pair(_activity_location(current_execution), future_anchor[1])
    if v2_choice_pools:
        v2_locations = {
            location for item in source for location in (_activity_location(item),) if location
        }
        v2_locations.update(
            choice.place_id for choices in v2_choice_pools.values() for choice in choices
        )
        v2_locations.update(str(semantic) for semantic, _start in c4_anchors if semantic)
        for origin in v2_locations:
            for destination in v2_locations:
                _need_pair(origin, destination)

    is_authoritative_v12_evidence = (
        str(frozen_v12_evidence.get("normalizer_version") or "").startswith(
            ("citynexus-v12-authoritative-", "citynexus-v13-authoritative-")
        )
        and bool(frozen_v12_evidence.get("selected_facts"))
        and int(frozen_v12_evidence.get("mode_matrix_count") or 0) > 0
    )
    incident_snapshot = _incident_mobility_snapshot(payload)
    incident_baseline_reference_fallback = False
    reuse_baseline_for_recovery = (
        is_authoritative_v12_evidence
        and trigger_event.event_type not in {"TRAFFIC_DELAY", "SCHEDULE_DELAY"}
    )
    if incident_snapshot is not None:
        mobility_capture = MobilitySnapshotCaptureResult(
            snapshot=incident_snapshot,
            failed_primary_pairs=(),
            fallback_pairs=(),
        )
    elif (
        trigger_event.event_type in {"TRAFFIC_DELAY", "SCHEDULE_DELAY"}
        and is_authoritative_v12_evidence
        and not fully_grounded_recovery
        and not settings.live_valhalla_url
        and frozen_v12_evidence.get("snapshot_id")
        and frozen_v12_evidence.get("facts")
    ):
        # Legacy synthetic plans can still serve the pre-V14 offline/autonomy
        # path when no incident provider is configured.  Mint a distinct
        # operation snapshot and label it as a baseline reference only; the
        # fully grounded V14 path never enters this branch and requires a
        # separately sealed incident matrix for traffic changes.
        baseline_reference = MobilitySnapshot.from_evidence(frozen_v12_evidence)
        mobility_capture = MobilitySnapshotCaptureResult(
            snapshot=MobilitySnapshot(
                facts=baseline_reference.facts,
                source_provider_key=baseline_reference.source_provider_key,
                captured_at=effective_event_time.isoformat(),
                degraded=False,
                snapshot_id="v14-baseline-reference-"
                + hashlib.sha256(f"{trigger_id}:baseline-reference".encode("utf-8")).hexdigest()[:32],
                operation_key=f"replan:{trigger_id}:baseline-reference",
                provider_capabilities={
                    "recovery": {
                        "status": "NOT_CONFIGURED",
                        "current_route_truth": False,
                        "baseline_reference_only": True,
                    }
                },
            ),
            failed_primary_pairs=(),
            fallback_pairs=(),
        )
        incident_baseline_reference_fallback = True
    elif (
        reuse_baseline_for_recovery
        and frozen_v12_evidence.get("snapshot_id")
        and frozen_v12_evidence.get("facts")
    ):
        # Closure/weather can reuse the Journey's sealed reserve when route
        # conditions are explicitly unchanged. Traffic and schedule events
        # never enter this branch: they require incident-time route evidence.
        frozen_snapshot = MobilitySnapshot.from_evidence(frozen_v12_evidence)
        if fully_grounded_recovery and trigger_event.event_type in {"CLOSURE", "WEATHER"}:
            frozen_snapshot = _grounded_recovery_reserve_snapshot(
                base_evidence=frozen_v12_evidence,
                provider_mobility=provider_mobility_reserve,
                aliases=grounded_recovery_aliases,
                operation_key=f"replan:{trigger_id}:grounded-reserve",
            )
        mobility_capture = MobilitySnapshotCaptureResult(
            snapshot=frozen_snapshot,
            failed_primary_pairs=(),
            fallback_pairs=(),
        )
    else:
        recovered_pairs: tuple[tuple[str, str], ...] = ()
        try:
            mobility_capture = capture_runtime_mobility_snapshot(
                session,
                journey,
                mobility_pairs,
                operation_key=f"replan:{trigger_id}:incident",
                captured_at=effective_event_time.isoformat(),
            )
        except MobilitySnapshotCaptureError:
            # Some nodes (for example the stay-flow's own check-in / night
            # nodes) exist only inside the plan's frozen evidence and have no
            # routable Place row.  The base plan already recorded a fact for
            # those legs, so reuse it instead of failing the whole recovery.
            base_snapshot = MobilitySnapshot.from_evidence(frozen_v12_evidence)
            missing = tuple(
                pair for pair in mobility_pairs if base_snapshot.facts.get(pair) is not None
            )
            if not missing:
                raise
            recovered_pairs = missing
            retained = {pair: base_snapshot.facts[pair] for pair in missing}
            try:
                captured = capture_runtime_mobility_snapshot(
                    session,
                    journey,
                    [pair for pair in mobility_pairs if pair not in retained],
                    operation_key=f"replan:{trigger_id}:incident",
                    captured_at=effective_event_time.isoformat(),
                )
            except MobilitySnapshotCaptureError as exc:
                raise HTTPException(503, f"mobility evidence unavailable: {exc}") from exc
            mobility_capture = MobilitySnapshotCaptureResult(
                snapshot=MobilitySnapshot(
                    facts={**captured.snapshot.facts, **retained},
                    source_provider_key=captured.snapshot.source_provider_key,
                    captured_at=captured.snapshot.captured_at,
                    degraded=captured.snapshot.degraded,
                    snapshot_id=captured.snapshot.snapshot_id,
                    operation_key=captured.snapshot.operation_key,
                    provider_capabilities=captured.snapshot.provider_capabilities,
                ),
                failed_primary_pairs=captured.failed_primary_pairs,
                fallback_pairs=captured.fallback_pairs,
            )
    mobility = mobility_capture.snapshot
    mobility_degraded_providers = (mobility.source_provider_key,) if mobility.degraded else ()
    if mobility.degraded:
        record_audit(
            session,
            action="provider_degraded",
            entity_type="MobilitySnapshot",
            entity_id=mobility.snapshot_id,
            journey_id=journey.id,
            after={
                "provider": mobility.source_provider_key,
                "snapshot_id": mobility.snapshot_id,
                "operation_key": mobility.operation_key,
                "fallback_pairs": [list(pair) for pair in mobility_capture.fallback_pairs],
            },
        )

    baseline_plan_evidence = {
        "plan_version_id": base.id,
        "snapshot_id": frozen_v12_evidence.get("snapshot_id"),
        "captured_at": frozen_v12_evidence.get("captured_at"),
        "source_provider": frozen_v12_evidence.get("source_provider"),
        "runtime_label": frozen_v12_evidence.get("runtime_label"),
        "immutable": True,
    }
    recovery_snapshot_evidence = mobility.to_evidence()
    recovery_route_provenance = (
        "SEALED_INCIDENT_REPLAY"
        if incident_snapshot is not None
        else "DEGRADED_BASELINE_REFERENCE"
        if incident_baseline_reference_fallback
        else "SEALED_REAL_REPLAY"
        if fully_grounded_recovery and trigger_event.event_type in {"CLOSURE", "WEATHER"}
        else "MIXED_RUNTIME_CAPTURE"
        if mobility.degraded
        else "RUNTIME_CAPTURE"
    )
    raw_weather_evidence = (
        payload.get("weather_evidence")
        or payload.get("weather_snapshot")
        or payload.get("provider_evidence")
        or payload.get("weather")
    )
    weather_evidence = raw_weather_evidence
    if isinstance(raw_weather_evidence, dict) and "provider_evidence" not in raw_weather_evidence:
        weather_evidence = {
            "provider": trigger_event.provider_key,
            "captured_at": payload.get("snapshot_captured_at") or trigger_event.detected_at,
            "provider_evidence": raw_weather_evidence,
        }
    if isinstance(weather_evidence, dict):
        for key in ("provider_observation_id", "capture_provenance", "forecast_runs"):
            if payload.get(key) is not None:
                weather_evidence.setdefault(key, payload[key])
    incident_recovery_evidence = {
        "evidence_id": f"incident:{trigger_id}:{mobility.snapshot_id}",
        "event_id": trigger_id,
        "event_type": trigger_event.event_type,
        "event_occurred_at": trigger_event.occurred_at,
        "captured_at": mobility.captured_at,
        "route_provenance": recovery_route_provenance,
        "source_provider": mobility.source_provider_key,
        "degraded": mobility.degraded,
        "baseline_snapshot_id": frozen_v12_evidence.get("snapshot_id"),
        "mobility_reused_from_baseline": reuse_baseline_for_recovery
        and incident_snapshot is None,
        "routing_conditions_assumption": (
            "UNCHANGED_FROM_BASELINE"
            if reuse_baseline_for_recovery and incident_snapshot is None
            else "BASELINE_REFERENCE_ONLY_PROVIDER_UNAVAILABLE"
            if incident_baseline_reference_fallback
            else "CAPTURED_AT_INCIDENT"
        ),
        "mobility_snapshot": recovery_snapshot_evidence,
        "weather": weather_evidence,
        "provider_capabilities": recovery_snapshot_evidence.get("provider_capabilities", {}),
    }
    recovery_observation_id = str(uuid4())
    incident_recovery_evidence["provider_observation_id"] = recovery_observation_id
    session.add(
        ProviderObservation(
            id=recovery_observation_id,
            journey_id=journey.id,
            provider_key=mobility.source_provider_key,
            observation_type="MOBILITY",
            state="DEGRADED" if mobility.degraded else "CAPTURED",
            incident_fingerprint=payload.get("incident_fingerprint"),
            provenance=recovery_route_provenance,
            captured_at=mobility.captured_at,
            expires_at=None,
            payload_json=json.dumps(incident_recovery_evidence, sort_keys=True),
            created_at=mobility.captured_at,
        )
    )
    record_audit(
        session,
        action="replan.incident.provider_observation.captured",
        entity_type="ProviderObservation",
        entity_id=recovery_observation_id,
        journey_id=journey.id,
        after={
            "provider_key": mobility.source_provider_key,
            "observation_type": "MOBILITY",
            "provenance": recovery_route_provenance,
            "snapshot_id": mobility.snapshot_id,
        },
        actor_type="SYSTEM",
        actor_id="citynexus-v14-recovery",
    )
    session.flush()

    def _recovery_plan_evidence(items: list[Activity]) -> dict[str, Any]:
        evidence = dict(recovery_snapshot_evidence)
        pairs = {
            (str(previous.place_id or previous.city_service_id or ""), str(current.place_id or current.city_service_id or ""))
            for previous, current in zip(items, items[1:])
        }
        selected_facts = [
            item
            for item in evidence.get("facts", [])
            if (str(item.get("origin_key")), str(item.get("destination_key"))) in pairs
        ]
        evidence["selected_facts"] = selected_facts
        evidence.update(
            {
                "normalizer_version": "citynexus-v14-recovery-evidence-1",
                "aggregate_provenance": recovery_route_provenance,
                "baseline_plan_evidence": baseline_plan_evidence,
                "incident_recovery_evidence": incident_recovery_evidence,
                "provider_evidence": {
                    "selected_path": recovery_route_provenance,
                    "selected_synthetic_fact_count": sum(
                        1
                        for item in selected_facts
                        if str(item.get("provider") or "").lower().startswith("mock")
                        or str(item.get("provenance") or "").upper() == "SYNTHETIC_FIXTURE"
                    ),
                    "synthetic_fact_count": sum(
                        1
                        for item in evidence.get("facts", [])
                        if str(item.get("provider") or "").lower().startswith("mock")
                        or str(item.get("provenance") or "").upper() == "SYNTHETIC_FIXTURE"
                    ),
                    "route_provenance": recovery_route_provenance,
                    "cost_provenance": "CITYNEXUS_COST_ESTIMATE",
                },
            }
        )
        evidence["integrity_hash"] = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return evidence

    def _at_local_time(value: str | None) -> datetime | None:
        if not value:
            return None
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return datetime.combine(
            effective_event_time.date(),
            datetime.min.time().replace(hour=hour, minute=minute),
            effective_event_time.tzinfo,
        )

    # V2-T2 (B1, gate point 4): created / failed_reasons / mobility_unavailable_pairs
    # are referenced by the first candidate loop below (its inbound/outbound legs
    # can raise MobilityDataUnavailable), so they must exist before any
    # _replan_estimate() runs — otherwise that path crashes with UnboundLocalError.
    created: list[ReplanProposalModel] = []
    failed_reasons: set[str] = set()
    mobility_unavailable_pairs: set[str] = set()
    # ORD-0199 ①: the same legs, as structured cells. The set above stays for its
    # message and de-duplication role; the reason each leg was refused comes from
    # the exception's own attributes, not from parsing that message back apart.
    mobility_gaps: list[dict[str, Any]] = []
    heuristic_specs: list[_ReplanCandidateSpec] = []
    # Set when a physical disruption has no material repair available.
    heuristic_specs_unresolved_reason: str | None = None
    if replacement_choices:
        for index, choice in enumerate(replacement_choices):
            family = "F0_SAME_SLOT_REPLACEMENT"
            window_evidence: dict[str, Any] | None = None
            if index > 0 and affected is not None:
                next_anchor = future_anchor
                if next_anchor is not None:
                    candidate_place = session.get(Place, choice.place_id)
                    opening = normalize_opening_hours(
                        json.loads(candidate_place.opening_hours_json or "{}")
                    )

                    # Replanning must start from the authoritative execution
                    # position, not merely from the activity immediately
                    # preceding the disrupted slot. The previous slot remains
                    # a deterministic fallback when execution position is not
                    # available.
                    previous = next(
                        (item for item in source if item.sequence_no == affected.sequence_no - 1),
                        None,
                    )
                    origin_activity = current_execution or previous
                    origin = (
                        (origin_activity.place_id or origin_activity.city_service_id)
                        if origin_activity is not None
                        else None
                    )
                    try:
                        inbound = _replan_estimate(
                            session,
                            journey,
                            mobility,
                            origin,
                            choice.place_id,
                            operation_key=f"replan-inbound:{affected.id}",
                        )
                        outbound = _replan_estimate(
                            session,
                            journey,
                            mobility,
                            choice.place_id,
                            next_anchor[1],
                            operation_key=f"replan-outbound:{affected.id}",
                        )
                    except MobilityDataUnavailable as exc:
                        # T2: the engine has no measured travel fact for this
                        # leg.  It must not price the candidate at a fabricated
                        # constant, and it must not report the candidate as
                        # infeasible either -- the candidate's feasibility is
                        # simply unknown.
                        mobility_unavailable_pairs.add(str(exc))
                        mobility_gaps.append(_mobility_gap_from(exc))
                        failed_reasons.add(exc.reason_code)
                        continue
                    budget_remaining = journey.budget_amount_minor - (
                        sum(item.estimated_cost_minor for item in source)
                        - affected.estimated_cost_minor
                    )
                    evidence = evaluate_window_replacement(
                        now=effective_event_time,
                        next_commitment_start=next_anchor[0],
                        safety_buffer_min=15,
                        budget_remaining_minor=budget_remaining,
                        candidate=WindowReplacementInput(
                            key=choice.place_id,
                            visit_minutes=choice.duration_min,
                            inbound_travel_min=inbound,
                            outbound_travel_min=outbound,
                            cost_minor=choice.cost_minor,
                            opens_at=_at_local_time(opening.get("open")),
                            closes_at=_at_local_time(opening.get("close")),
                        ),
                    )
                    if evidence.fits:
                        family = "F3_OPPORTUNITY_WINDOW"
                        window_evidence = {
                            "window_opens_at": evidence.window_opens_at.isoformat(),
                            "next_commitment_start": evidence.next_commitment_start.isoformat(),
                            "finish_at": evidence.finish_at.isoformat()
                            if evidence.finish_at
                            else None,
                            "remaining_buffer_min": evidence.remaining_buffer_min,
                            "origin_key": origin,
                            "inbound_travel_min": inbound,
                            "outbound_travel_min": outbound,
                            "safety_buffer_min": 15,
                        }
            if index > 0 and family == "F0_SAME_SLOT_REPLACEMENT":
                family = "F0_ALTERNATE_REPLACEMENT"
            heuristic_specs.append(
                _ReplanCandidateSpec(
                    family=family,
                    replacement_choice=choice,
                    window_evidence=window_evidence,
                )
            )
        if (
            affected is not None
            and affected.id not in protected_activity_ids
            and not affected.locked
        ):
            heuristic_specs.append(
                _ReplanCandidateSpec(
                    family="F1_OPTIONAL_REMOVAL",
                    replacement_choice=None,
                    candidate_remove=True,
                )
            )
    elif delay_minutes:
        heuristic_specs.append(
            _ReplanCandidateSpec(
                family="F2_SUFFIX_DELAY",
                replacement_choice=None,
                candidate_delay=delay_minutes,
            )
        )
        if (
            affected is not None
            and affected.id not in protected_activity_ids
            and not affected.locked
        ):
            heuristic_specs.append(
                _ReplanCandidateSpec(
                    family="F1_OPTIONAL_REMOVAL",
                    replacement_choice=None,
                    candidate_remove=True,
                )
            )
    elif remove_affected:
        heuristic_specs.append(
            _ReplanCandidateSpec(
                family="F1_OPTIONAL_REMOVAL",
                replacement_choice=None,
                candidate_remove=True,
            )
        )
    elif trigger_event.event_type not in {"CLOSURE", "WEATHER"}:
        # Only a non-physical event may legitimately settle as "nothing needs
        # to change".  A closure or bad weather that cannot be repaired
        # *materially* must say so, never quietly accept an unchanged copy
        # (truth blocker T2).
        heuristic_specs.append(
            _ReplanCandidateSpec(family="F0_STABLE_COPY", replacement_choice=None)
        )
    else:
        # No repair exists for a physical disruption.  Record why, so the
        # caller returns an honest unresolved state instead of a no-op plan.
        if affected is None:
            heuristic_specs_unresolved_reason = "REPLAN_TARGET_UNRESOLVED"
        elif affected.id in protected_activity_ids or affected.locked:
            heuristic_specs_unresolved_reason = "REPLAN_PROTECTED_CONFLICT"
        else:
            heuristic_specs_unresolved_reason = "REPLAN_NO_ALTERNATIVE"

    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)

    solver_attempt: SolverAttempt | None = None
    solver_specs: list[_ReplanCandidateSpec] = []
    solver_fallback_reason = (
        "SOLVER_DISABLED" if solver_mode == "deterministic" else "SOLVER_V1_UNSUPPORTED"
    )
    current_location = _activity_location(current_execution or previous_affected)
    journey_start = datetime.fromisoformat(journey.start_at)
    journey_end = datetime.fromisoformat(journey.end_at)

    def _minute_from_start(value: datetime) -> int:
        return int((value - journey_start).total_seconds() // 60)

    anchor_activity = None
    if future_anchor is not None:
        anchor_start, anchor_semantic = future_anchor
        anchor_activity = next(
            (
                item
                for item in source
                if (item.place_id or item.city_service_id or item.id) == anchor_semantic
                and datetime.fromisoformat(item.start_at) == anchor_start
            ),
            None,
        )
    if solver_mode == "v2" and current_location is not None and v2_choice_pools:
        v2_slots: list[SolverV2Slot] = []
        v2_choices: dict[tuple[str, str], Any] = {}
        secondary_risk_ids = set(impact_resolution.secondary_risk_activity_ids)
        journey_end_min = max(1, _minute_from_start(journey_end))
        for item in source:
            choices = v2_choice_pools.get(item.id)
            if choices is None:
                continue
            original_start_min = max(0, _minute_from_start(datetime.fromisoformat(item.start_at)))
            original_end_min = min(
                journey_end_min, _minute_from_start(datetime.fromisoformat(item.end_at))
            )
            duration_min = max(1, original_end_min - original_start_min)
            item_place = session.get(Place, item.place_id) if item.place_id else None
            opening = (
                normalize_opening_hours(json.loads(item_place.opening_hours_json or "{}"))
                if item_place
                else {"status": "UNKNOWN"}
            )
            if item_place is not None and opening.get("status") not in {"NORMALIZED_VALID"}:
                opening = {"status": "UNKNOWN"}
            opens_at = _at_local_time(opening.get("open"))
            closes_at = _at_local_time(opening.get("close"))
            earliest_start_min = max(0, _minute_from_start(opens_at)) if opens_at else 0
            latest_end_min = (
                min(journey_end_min, _minute_from_start(closes_at))
                if closes_at
                else journey_end_min
            )
            if latest_end_min <= earliest_start_min:
                continue
            must_change = item.id == affected_activity_id
            slot_options: list[SolverV2Option] = []
            if not must_change:
                slot_options.append(
                    SolverV2Option(
                        option_key=f"KEEP:{item.id}",
                        location_key=_activity_location(item) or item.id,
                        action="KEEP",
                        duration_min=duration_min,
                        cost_minor=item.estimated_cost_minor,
                        earliest_start_min=earliest_start_min,
                        latest_end_min=latest_end_min,
                        original_start_min=original_start_min,
                        original_cost_minor=item.estimated_cost_minor,
                        risk_penalty_milli=500 if item.id in secondary_risk_ids else 0,
                    )
                )
            for choice in choices:
                candidate_place = session.get(Place, choice.place_id)
                if candidate_place is None:
                    continue
                candidate_opening = normalize_opening_hours(
                    json.loads(candidate_place.opening_hours_json or "{}")
                )
                # Opening hours that could not be verified are not evidence
                # that the place is shut.  The deterministic gate -- which is
                # the feasibility authority -- treats a missing interval as
                # unconstrained (``_within_open_interval`` returns True when it
                # has no open/close minutes).  Requiring NORMALIZED_VALID here
                # instead made every real discovered POI unusable, because
                # public Overpass records rarely publish opening_hours: the
                # solver produced no candidate at all and the traveller was
                # told no replacement existed.  Degrade to full-day bounds,
                # exactly like the KEEP option above, and let the gate decide.
                if candidate_opening.get("status") != "NORMALIZED_VALID":
                    candidate_opening = {"status": "UNKNOWN"}
                candidate_opens = _at_local_time(candidate_opening.get("open"))
                candidate_closes = _at_local_time(candidate_opening.get("close"))
                candidate_earliest = (
                    max(0, _minute_from_start(candidate_opens)) if candidate_opens else 0
                )
                candidate_latest = (
                    min(journey_end_min, _minute_from_start(candidate_closes))
                    if candidate_closes
                    else journey_end_min
                )
                if candidate_latest <= candidate_earliest:
                    continue
                candidate_tags = set(json.loads(candidate_place.tags_json or "[]"))
                preference_fit = verified_interest_preference_fit(
                    traveler_preferences, candidate_tags=candidate_tags
                )
                slot_options.append(
                    SolverV2Option(
                        option_key=f"REPLACE:{item.id}:{choice.place_id}",
                        location_key=choice.place_id,
                        action="REPLACE",
                        duration_min=choice.duration_min,
                        cost_minor=choice.cost_minor,
                        earliest_start_min=candidate_earliest,
                        latest_end_min=candidate_latest,
                        original_start_min=original_start_min,
                        original_cost_minor=item.estimated_cost_minor,
                        preference_milli=round(
                            (0.5 if preference_fit is None else preference_fit) * 1000
                        ),
                    )
                )
                v2_choices[(item.id, f"REPLACE:{item.id}:{choice.place_id}")] = choice
            if not slot_options:
                continue
            v2_slots.append(
                SolverV2Slot(
                    slot_key=item.id,
                    sequence_no=int(item.sequence_no),
                    original_location_key=_activity_location(item) or item.id,
                    original_start_min=original_start_min,
                    original_end_min=original_end_min,
                    original_cost_minor=item.estimated_cost_minor,
                    options=tuple(slot_options),
                    must_change=must_change,
                    allow_drop=not must_change,
                )
            )
        v2_anchors: list[SolverV2Anchor] = []
        for semantic, start_at in c4_anchors:
            anchor_start = datetime.fromisoformat(start_at)
            if anchor_start < effective_event_time:
                continue
            # The commitment stores the *canonical* identity, which lives in
            # ``metadata.semantic_key``.  Matching only on place_id/id missed
            # every anchor, fell back to a 60 minute duration, and pushed the
            # anchor past ``journey_end_min`` -- which the solver rejects with
            # "protected anchor lies outside remaining horizon" (HTTP 500) for
            # a structurally valid journey whose last stop is only 10 minutes.
            anchor_activity_row = next(
                (
                    item
                    for item in source
                    if datetime.fromisoformat(item.start_at) == anchor_start
                    and _activity_location(item) == semantic
                ),
                None,
            ) or next(
                (
                    item
                    for item in source
                    if datetime.fromisoformat(item.start_at) == anchor_start
                    and str(item.place_id or item.city_service_id or item.id) == semantic
                ),
                None,
            )
            anchor_location = _activity_location(anchor_activity_row) or semantic
            anchor_duration = (
                max(
                    1,
                    _minute_from_start(datetime.fromisoformat(anchor_activity_row.end_at))
                    - _minute_from_start(datetime.fromisoformat(anchor_activity_row.start_at)),
                )
                if anchor_activity_row is not None
                else 60
            )
            v2_anchors.append(
                SolverV2Anchor(
                    key=f"{semantic}@{anchor_start.isoformat()}",
                    location_key=anchor_location,
                    start_min=max(0, _minute_from_start(anchor_start)),
                    duration_min=anchor_duration,
                )
            )
        if v2_slots:
            v2_problem = SolverV2Problem(
                journey_id=journey.id,
                operation_key=mobility.operation_key or f"replan:{trigger_id}",
                now_min=max(0, _minute_from_start(effective_event_time)),
                journey_end_min=journey_end_min,
                budget_ceiling_minor=journey.budget_amount_minor,
                base_cost_minor=sum(
                    item.estimated_cost_minor
                    for item in source
                    if item.id not in {slot.slot_key for slot in v2_slots}
                ),
                current_location_key=current_location,
                slots=tuple(v2_slots),
                protected_anchors=tuple(v2_anchors),
                frozen_mobility_snapshot_id=mobility.snapshot_id,
                travel_minutes={pair: fact.minutes for pair, fact in mobility.facts.items()},
                safety_buffer_min=15,
                top_k=body.candidate_count,
            )
            v2_plans = solve_v2(v2_problem)
            solver_fallback_reason = "SOLVER_V2_NO_FEASIBLE_PLAN"
            for plan in v2_plans:
                solver_specs.append(
                    _ReplanCandidateSpec(
                        family="V2_WHOLE_HORIZON",
                        replacement_choice=None,
                        solver_provenance={
                            "kind": plan.generator_kind,
                            "solver_status": "FEASIBLE",
                            "terminal_status": "TOP_K",
                            "problem_fingerprint": solver_v2_problem_fingerprint(v2_problem),
                            "mobility_snapshot_id": mobility.snapshot_id,
                            "assignment_fingerprint": plan.fingerprint,
                            "rank": plan.generator_rank,
                            "objective": dict(plan.objective_breakdown),
                            "protected_anchor_count": len(plan.protected_anchors),
                        },
                        v2_plan=plan,
                        v2_choices=v2_choices,
                    )
                )
            if v2_plans:
                solver_fallback_reason = "SOLVER_V2_CANDIDATE_SET_COMPLEMENT"
    supports_solver_v1 = (
        solver_mode in {"ortools", "auto"}
        and trigger_event.event_type in {"CLOSURE", "WEATHER"}
        and affected is not None
        # V1 models only the disrupted current slot through the next C4 anchor.
        # Future affected Activities with intervening mutable Activities remain
        # on the deterministic path until a later multi-slot solver contract.
        and current_execution is not None
        and affected.id == current_execution.id
        and affected.id not in protected_activity_ids
        and not affected.locked
        and bool(replacement_choices)
        and current_location is not None
        and future_anchor is not None
        and anchor_activity is not None
    )
    if supports_solver_v1:
        budget_remaining = max(
            0,
            journey.budget_amount_minor
            - (sum(item.estimated_cost_minor for item in source) - affected.estimated_cost_minor),
        )
        replacement_by_key = {choice.place_id: choice for choice in replacement_choices}
        solver_options: list[SolverReplacementOption] = []
        for choice in sorted(replacement_choices, key=lambda item: item.place_id):
            candidate_place = session.get(Place, choice.place_id)
            opening = json.loads(candidate_place.opening_hours_json or "{}")
            opens_at = _at_local_time(opening.get("open"))
            closes_at = _at_local_time(opening.get("close"))
            if opens_at is None or closes_at is None or closes_at <= opens_at:
                continue
            earliest_start_min = max(0, _minute_from_start(opens_at))
            latest_end_min = _minute_from_start(closes_at)
            if latest_end_min <= earliest_start_min:
                continue
            candidate_tags = set(json.loads(candidate_place.tags_json or "[]"))
            solver_preference_fit = verified_interest_preference_fit(
                traveler_preferences, candidate_tags=candidate_tags
            )
            solver_preference_milli = round(
                (0.5 if solver_preference_fit is None else solver_preference_fit) * 1000
            )
            solver_options.append(
                SolverReplacementOption(
                    option_key=choice.place_id,
                    location_key=choice.place_id,
                    duration_min=choice.duration_min,
                    cost_minor=choice.cost_minor,
                    earliest_start_min=earliest_start_min,
                    latest_end_min=latest_end_min,
                    original_start_min=max(
                        0, _minute_from_start(datetime.fromisoformat(affected.start_at))
                    ),
                    preference_milli=solver_preference_milli,
                )
            )
        if solver_options:
            terminal_ids = set(partition.terminal_prefix_ids)
            problem = ReplanSolverProblem(
                journey_id=journey.id,
                operation_key=mobility.operation_key or f"replan:{trigger_id}",
                now_min=max(0, _minute_from_start(effective_event_time)),
                journey_end_min=_minute_from_start(journey_end),
                budget_remaining_minor=budget_remaining,
                current_location_key=current_location,
                next_anchor=SolverAnchor(
                    semantic_key=future_anchor[1],
                    location_key=_activity_location(anchor_activity) or future_anchor[1],
                    start_min=_minute_from_start(future_anchor[0]),
                    duration_min=max(
                        1,
                        int(
                            (
                                datetime.fromisoformat(anchor_activity.end_at)
                                - datetime.fromisoformat(anchor_activity.start_at)
                            ).total_seconds()
                            // 60
                        ),
                    ),
                ),
                replacement_options=tuple(solver_options),
                frozen_mobility_snapshot_id=mobility.snapshot_id,
                travel_minutes={pair: fact.minutes for pair, fact in mobility.facts.items()},
                completed_prefix_keys=tuple(
                    str(item.place_id or item.city_service_id or item.id)
                    for item in source
                    if item.id in terminal_ids
                ),
                safety_buffer_min=15,
                allow_drop=True,
            )
            solver_attempt = generate_solver_candidates(
                problem,
                mode=solver_mode,
                candidate_count=body.candidate_count,
                random_seed=settings.solver_random_seed,
                max_time_seconds=settings.solver_max_time_seconds,
            )
            solver_fallback_reason = solver_attempt.fallback_reason or "ORTOOLS_POST_GATE_FALLBACK"
            for candidate in solver_attempt.candidates:
                if candidate.dropped:
                    solver_specs.append(
                        _ReplanCandidateSpec(
                            family="F1_OPTIONAL_REMOVAL",
                            replacement_choice=None,
                            candidate_remove=True,
                            solver_provenance=solver_attempt.evidence_for(candidate),
                        )
                    )
                    continue
                choice = replacement_by_key.get(str(candidate.option_key))
                if choice is None or candidate.start_min is None:
                    continue
                solver_specs.append(
                    _ReplanCandidateSpec(
                        family="F4_ORTOOLS_WINDOW_REPLACEMENT",
                        replacement_choice=choice,
                        explicit_candidate_start_at=journey_start
                        + timedelta(minutes=candidate.start_min),
                        solver_provenance=solver_attempt.evidence_for(candidate),
                    )
                )
        else:
            solver_fallback_reason = "SOLVER_NO_ELIGIBLE_OPTIONS"

    fallback_status = (
        solver_attempt.terminal_status
        if solver_attempt is not None
        else "DISABLED"
        if solver_mode == "deterministic"
        else "UNSUPPORTED"
    )
    fallback_problem_fingerprint = (
        solver_attempt.problem_fingerprint if solver_attempt is not None else None
    )
    if solver_attempt is not None and solver_attempt.used_solver:
        heuristic_fallback_reason = "ORTOOLS_CANDIDATE_SET_COMPLEMENT"
    elif solver_attempt is not None and solver_attempt.fallback_reason:
        heuristic_fallback_reason = solver_attempt.fallback_reason
    else:
        heuristic_fallback_reason = solver_fallback_reason

    # Keep all deterministic assignments behind solver specs. Successful solver
    # assignments suppress duplicates only after they pass the authoritative gate.
    # If a solver schedule is rejected, the same semantic option can still be
    # rescued by the deterministic schedule.
    fallback_specs: list[_ReplanCandidateSpec] = []
    for spec in heuristic_specs:
        fallback_specs.append(
            _ReplanCandidateSpec(
                family=spec.family,
                replacement_choice=spec.replacement_choice,
                candidate_delay=spec.candidate_delay,
                candidate_remove=spec.candidate_remove,
                window_evidence=spec.window_evidence,
                solver_provenance={
                    "kind": "DETERMINISTIC_HEURISTIC",
                    "solver_status": fallback_status,
                    "fallback_reason": heuristic_fallback_reason,
                    "problem_fingerprint": fallback_problem_fingerprint,
                    "mobility_snapshot_id": mobility.snapshot_id,
                },
            )
        )
    specs = solver_specs + fallback_specs
    if solver_specs and body.candidate_count >= 3 and solver_mode in {"ortools", "auto"}:
        # A solver run is a source of ranked candidates, not permission to
        # hide the deterministic complement.  Keep the best solver assignment
        # first, then retain the two stable recovery families that make the
        # candidate set explainable across runtime modes.  This matters for
        # the legacy Golden contract and for operators comparing solver output
        # with the opportunity-window evidence; it does not alter the selected
        # V2 plan when candidate_count is one.
        first_solver = solver_specs[0]
        complement = [
            item
            for family in ("F0_SAME_SLOT_REPLACEMENT", "F3_OPPORTUNITY_WINDOW")
            for item in fallback_specs
            if item.family == family
        ]
        reserved = {id(item) for item in complement}
        specs = [first_solver, *complement, *solver_specs[1:]] + [
            item for item in fallback_specs if id(item) not in reserved
        ]
    disruption_constraints: list[dict[str, Any]] = []
    if affected_place_id and trigger_event.event_type in {"CLOSURE", "WEATHER"}:
        disruption_constraints.append(
            {
                "kind": "HARD",
                "category": "availability",
                "rule_code": "PLACE_UNAVAILABLE",
                "params": {"place_id": affected_place_id},
                "active": True,
            }
        )
    c4_anchor_set = {
        (semantic, datetime.fromisoformat(start).isoformat()) for semantic, start in c4_anchors
    }

    def _diff_semantic_key(activity: Activity) -> str:
        return str(activity.place_id or activity.city_service_id or activity.id)

    def _is_protected(activity: Activity) -> bool:
        semantic = _diff_semantic_key(activity)
        start = datetime.fromisoformat(activity.start_at).isoformat()
        return bool(activity.locked) or (semantic, start) in c4_anchor_set

    old_snapshots = [
        PlanActivitySnapshot(
            _diff_semantic_key(activity),
            datetime.fromisoformat(activity.start_at),
            datetime.fromisoformat(activity.end_at),
            activity.estimated_cost_minor,
            _is_protected(activity),
        )
        for activity in source
    ]
    c4_count = len(c4_rows)
    before_envelope = _envelope_payload(
        journey,
        source,
        preserved_commitment_count=c4_count,
        degraded_providers=mobility_degraded_providers,
    )

    def _frozen_route_total(items: list[Activity]) -> int:
        total = 0
        for previous, current in zip(items, items[1:]):
            origin = _activity_location(previous)
            destination = _activity_location(current)
            if not origin or not destination or origin == destination:
                continue
            total += int(mobility.estimate_minutes(origin, destination))
        return total

    base_route_total = _frozen_route_total(source)
    # created / failed_reasons / mobility_unavailable_pairs are initialised
    # earlier, before the first candidate loop (V2-T2 gate point 4).
    accepted_assignment_keys: set[str] = set()
    rejected_solver_assignment_keys: set[str] = set()

    def _spec_assignment_key(spec: _ReplanCandidateSpec) -> str:
        if spec.v2_plan is not None:
            return f"V2:{spec.v2_plan.fingerprint}"
        if spec.candidate_remove:
            return "DROP"
        if spec.replacement_choice is not None:
            # A solver schedule and its deterministic complement may target
            # the same place while proving different visit times.  Keep those
            # candidates distinct so a post-gate solver result does not erase
            # the explainable same-slot/opportunity-window alternatives.
            scheduled_start = (
                spec.explicit_candidate_start_at.isoformat()
                if spec.explicit_candidate_start_at is not None
                else "heuristic"
            )
            return f"{spec.family}:{spec.replacement_choice.place_id}:{scheduled_start}"
        return spec.family

    for generation_rank, spec in enumerate(specs):
        if len(created) >= body.candidate_count:
            break
        assignment_key = _spec_assignment_key(spec)
        if assignment_key in accepted_assignment_keys:
            continue
        family = spec.family
        replacement_choice = spec.replacement_choice
        candidate_delay = spec.candidate_delay
        candidate_remove = spec.candidate_remove
        window_evidence = spec.window_evidence
        v2_assignments = (
            {item.slot_key: item for item in spec.v2_plan.assignments}
            if spec.v2_plan is not None
            else {}
        )
        v2_choices = spec.v2_choices or {}
        generator_evidence = dict(spec.solver_provenance or {})
        generator_evidence.setdefault("kind", "DETERMINISTIC_HEURISTIC")
        if (
            generator_evidence.get("kind") == "DETERMINISTIC_HEURISTIC"
            and assignment_key in rejected_solver_assignment_keys
        ):
            generator_evidence["fallback_reason"] = "ORTOOLS_POST_GATE_FALLBACK"
        generator_evidence.setdefault("rank", generation_rank)
        proposed = PlanVersion(
            id=str(uuid4()),
            journey_id=journey.id,
            version_no=latest_version + len(created) + 1,
            planning_mode=base.planning_mode,
            status="CANDIDATE",
            source="REPLAN",
            parent_plan_version_id=base.id,
            total_cost_minor=0,
            currency=base.currency,
            estimated_duration_min=0,
            utility_score=None,
            score_breakdown_json=None,
            explanation_json=json.dumps({"reason": family}, sort_keys=True),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        session.add(proposed)
        session.flush()
        copied_rows: list[dict[str, Any]] = []
        for activity in source:
            v2_assignment = v2_assignments.get(activity.id)
            if v2_assignment is not None and v2_assignment.action == "DROP":
                continue
            if candidate_remove and activity.id == affected_activity_id:
                continue
            row = {
                "sequence_no": activity.sequence_no,
                "activity_type": activity.activity_type,
                "title": activity.title,
                "place_id": activity.place_id,
                "city_service_id": activity.city_service_id,
                "start_at": activity.start_at,
                "end_at": activity.end_at,
                "travel_from_prev_min": activity.travel_from_prev_min,
                "estimated_cost_minor": activity.estimated_cost_minor,
                "currency": activity.currency,
                "status": activity.status,
                "locked": activity.locked,
                "metadata_json": activity.metadata_json,
                "created_at": activity.created_at,
                "updated_at": activity.updated_at,
            }
            if v2_assignment is not None and v2_assignment.action == "REPLACE":
                replacement_choice = v2_choices.get((activity.id, str(v2_assignment.option_key)))
                if replacement_choice is None:
                    failed_reasons.add("SOLVER_V2_OPTION_NOT_MATERIALIZED")
                    continue
                start_dt = journey_start + timedelta(minutes=int(v2_assignment.start_min or 0))
                row.update(
                    {
                        "title": replacement_choice.title,
                        "place_id": replacement_choice.place_id,
                        "city_service_id": None,
                        "start_at": start_dt.isoformat(),
                        "end_at": (
                            start_dt + timedelta(minutes=replacement_choice.duration_min)
                        ).isoformat(),
                        "estimated_cost_minor": replacement_choice.cost_minor,
                        "locked": False,
                        "metadata_json": json.dumps(
                            {
                                **_json_object(row.get("metadata_json"), {}),
                                "semantic_key": replacement_choice.place_id,
                                "candidate_family": family,
                                "replan_reason_codes": replacement_choice.reason_codes,
                                "replaced_place_id": activity.place_id,
                                "solver_v2_assignment": v2_assignment.option_key,
                            },
                            sort_keys=True,
                        ),
                    }
                )
            elif v2_assignment is not None and v2_assignment.action == "KEEP":
                start_dt = journey_start + timedelta(minutes=int(v2_assignment.start_min or 0))
                end_dt = journey_start + timedelta(minutes=int(v2_assignment.end_min or 0))
                row.update(
                    {
                        "start_at": start_dt.isoformat(),
                        "end_at": end_dt.isoformat(),
                        # Truth blocker B5: merge, never replace.  The original
                        # metadata carries ``semantic_key`` (the routing and
                        # frozen-evidence identity); dropping it made structural
                        # nodes fall back to their UUID place_id and every leg
                        # failed with a mobility identity mismatch.
                        "metadata_json": json.dumps(
                            {
                                **_json_object(row.get("metadata_json"), {}),
                                "candidate_family": family,
                                "replan_reason_codes": ("SOLVER_V2_KEEP",),
                            },
                            sort_keys=True,
                        ),
                    }
                )
            elif replacement_choice is not None and activity.id == affected_activity_id:
                start_dt = datetime.fromisoformat(activity.start_at)
                if spec.explicit_candidate_start_at is not None:
                    start_dt = spec.explicit_candidate_start_at
                elif family == "F3_OPPORTUNITY_WINDOW" and window_evidence is not None:
                    # Persist the exact visit schedule proven by the
                    # OpportunityWindow evidence: window open + inbound travel.
                    start_dt = datetime.fromisoformat(
                        str(window_evidence["window_opens_at"])
                    ) + timedelta(minutes=int(window_evidence["inbound_travel_min"]))
                row.update(
                    {
                        "title": replacement_choice.title,
                        "place_id": replacement_choice.place_id,
                        "city_service_id": None,
                        "start_at": start_dt.isoformat(),
                        "end_at": (
                            start_dt + timedelta(minutes=replacement_choice.duration_min)
                        ).isoformat(),
                        "estimated_cost_minor": replacement_choice.cost_minor,
                        "locked": False,
                        "metadata_json": json.dumps(
                            {
                                **_json_object(row.get("metadata_json"), {}),
                                "semantic_key": replacement_choice.place_id,
                                "candidate_family": family,
                                "replan_reason_codes": replacement_choice.reason_codes,
                                "replaced_place_id": affected_place_id,
                            },
                            sort_keys=True,
                        ),
                    }
                )
            elif candidate_delay and affected is not None:
                if (
                    activity.sequence_no >= affected.sequence_no
                    and activity.id not in protected_activity_ids
                    and not activity.locked
                    and activity.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
                ):
                    row["start_at"] = (
                        datetime.fromisoformat(activity.start_at)
                        + timedelta(minutes=candidate_delay)
                    ).isoformat()
                    row["end_at"] = (
                        datetime.fromisoformat(activity.end_at) + timedelta(minutes=candidate_delay)
                    ).isoformat()
                    # Truth blocker B5: never REPLACE the activity metadata.
                    # The original dict carries ``semantic_key``, which is the
                    # routing / frozen-evidence identity.  Overwriting it made
                    # every downstream leg fall back to ``place_id`` and the
                    # recovery failed with a mobility identity mismatch.
                    _existing_meta = _json_object(row.get("metadata_json"), {})
                    row["metadata_json"] = json.dumps(
                        {
                            **_existing_meta,
                            "candidate_family": family,
                            "replan_reason_codes": (
                                "SCHEDULE_DELAY_ABSORBED",
                                "COMMITMENT_PRESERVED",
                            ),
                            "delay_minutes": candidate_delay,
                        },
                        sort_keys=True,
                    )
            copied_rows.append(row)

        def _row_routing_key(row: dict[str, Any]) -> str | None:
            """Same routing identity the gate and the frozen evidence use.

            The proposed plan is validated by the shared gate, which keys on
            ``metadata.semantic_key``.  Looking legs up by ``place_id`` here
            would raise "missing frozen travel estimate" for every leg whose
            concrete Place id differs from its canonical identity.
            """

            metadata = _json_object(row.get("metadata_json"), {})
            semantic = str(metadata.get("semantic_key") or "").strip()
            return semantic or row.get("place_id") or row.get("city_service_id")

        # 1. Resolve the travel time of every leg in the *new* order.
        abandoned = False
        for index, row in enumerate(copied_rows):
            if index == 0:
                row["travel_from_prev_min"] = 0
                continue
            previous = copied_rows[index - 1]
            origin = _row_routing_key(previous)
            destination = _row_routing_key(row)
            if not origin or not destination:
                row["travel_from_prev_min"] = 0
                continue
            try:
                row["travel_from_prev_min"] = _replan_estimate(
                    session,
                    journey,
                    mobility,
                    origin,
                    destination,
                    operation_key=f"replan-leg:{proposed.id}",
                )
            except MobilityDataUnavailable as exc:
                # T2: without a measured fact this leg cannot be re-timed.
                # Abandon this candidate rather than shifting the schedule by a
                # fabricated constant and letting the gate reject it for the
                # wrong reason.
                mobility_unavailable_pairs.add(str(exc))
                mobility_gaps.append(_mobility_gap_from(exc))
                failed_reasons.add(exc.reason_code)
                abandoned = True
                break

        if abandoned:
            # T2: drop the half-built proposal.  Its travel times are unknown,
            # so it must not be presented as a feasible or infeasible repair.
            session.delete(proposed)
            session.flush()
            continue

        # 1b. Normalise every timestamp into one offset.
        #     ``activities.end_at > start_at`` is a TEXT comparison, so a plan
        #     that mixed "+08:00" and "+00:00" spellings of the same instant
        #     could satisfy the CHECK with a *lexicographically* smaller value
        #     and be rejected by the database (an intermittent HTTP 500 during
        #     replan).  Storing one offset per plan keeps the ordering real.
        journey_tz = ZoneInfo(journey.timezone or "UTC")
        for row in copied_rows:
            for field in ("start_at", "end_at"):
                try:
                    moment = datetime.fromisoformat(str(row[field]))
                except (TypeError, ValueError):
                    continue
                if moment.tzinfo is not None:
                    row[field] = moment.astimezone(journey_tz).isoformat()

        # 2. Re-time the proposed schedule so the travel it just reserved is
        #    actually reachable.  Moving a replacement into a new slot changes
        #    the adjacencies; without this pass the shared gate correctly
        #    reported INSUFFICIENT_TRAVEL_GAP for legs the replan itself
        #    created.  Locked/C4 rows are pinned truth and never move, so a
        #    genuine conflict still surfaces instead of being hidden.
        previous_end: datetime | None = None
        for row in copied_rows:
            start_dt = datetime.fromisoformat(str(row["start_at"]))
            end_dt = datetime.fromisoformat(str(row["end_at"]))
            if previous_end is not None and not bool(row.get("locked")):
                earliest = previous_end + timedelta(
                    minutes=int(row.get("travel_from_prev_min") or 0)
                )
                if start_dt < earliest:
                    shift = earliest - start_dt
                    start_dt = earliest
                    end_dt = end_dt + shift
                    row["start_at"] = start_dt.isoformat()
                    row["end_at"] = end_dt.isoformat()
            previous_end = end_dt

        for row in copied_rows:
            session.add(Activity(id=str(uuid4()), plan_version_id=proposed.id, **row))

        proposed.total_cost_minor = sum(int(row["estimated_cost_minor"]) for row in copied_rows)
        if copied_rows:
            first_start = min(datetime.fromisoformat(str(row["start_at"])) for row in copied_rows)
            last_end = max(datetime.fromisoformat(str(row["end_at"])) for row in copied_rows)
            proposed.estimated_duration_min = max(
                0, int((last_end - first_start).total_seconds() // 60)
            )
        session.flush()
        copied = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == proposed.id)
            .order_by(Activity.sequence_no)
        ).all()
        gate = validate_plan_feasibility(
            session,
            journey,
            proposed,
            additional_constraints=disruption_constraints,
            include_active_c4=True,
            mobility_provider=mobility,
        )
        if not gate.passed:
            failed_reasons.update(gate.failures)
            if generator_evidence.get("kind") == "ORTOOLS_CP_SAT":
                rejected_solver_assignment_keys.add(assignment_key)
            session.execute(delete(Activity).where(Activity.plan_version_id == proposed.id))
            session.delete(proposed)
            session.flush()
            continue

        proposed.mobility_snapshot_id = mobility.snapshot_id
        proposed.mobility_evidence_json = json.dumps(
            _recovery_plan_evidence(copied), ensure_ascii=False, sort_keys=True, default=str
        )

        new_snapshots = [
            PlanActivitySnapshot(
                _diff_semantic_key(activity),
                datetime.fromisoformat(activity.start_at),
                datetime.fromisoformat(activity.end_at),
                activity.estimated_cost_minor,
                _is_protected(activity),
            )
            for activity in copied
        ]
        diff = diff_plans(old_snapshots, new_snapshots)
        after_envelope = _envelope_payload(
            journey,
            copied,
            preserved_commitment_count=c4_count,
            degraded_providers=mobility_degraded_providers,
        )
        travel_delta = _frozen_route_total(copied) - base_route_total
        preference_fit = None
        opportunity_value = None
        if spec.v2_plan is not None:
            preference_milli = int(spec.v2_plan.objective_breakdown.get("preference_milli", 0))
            slot_count = max(1, len(spec.v2_plan.assignments))
            preference_fit = min(1.0, max(0.0, preference_milli / (1000 * slot_count)))
        elif replacement_choice is not None:
            replacement_place = session.get(Place, replacement_choice.place_id)
            replacement_tags = set(json.loads(replacement_place.tags_json or "[]"))
            preference_fit = verified_interest_preference_fit(
                traveler_preferences, candidate_tags=replacement_tags
            )
        elif candidate_remove:
            # Removing the disrupted activity contributes no interest match for
            # that slot; this is verified absence, not an inferred LLM score.
            preference_fit = 0.0
        else:
            # Delay/stable-copy families preserve the same activities, so their
            # already-verified preference coverage is preserved rather than re-scored.
            preference_fit = 1.0
        # Opportunity value remains neutral until a candidate family is backed
        # by verified OpportunityWindow evidence. Do not infer it from duration.
        opportunity_value = (
            min(1.0, max(0.0, float(window_evidence["remaining_buffer_min"]) / 60.0))
            if window_evidence is not None
            else None
        )
        score = score_replan_candidate(
            base_activity_count=len(source),
            moved_count=len(diff.moved),
            removed_count=len(diff.removed),
            added_count=len(diff.added),
            travel_delta_min=travel_delta,
            budget_amount_minor=journey.budget_amount_minor,
            after_envelope=after_envelope,
            commitments_preserved=True,
            preference_fit=preference_fit,
            opportunity_value=opportunity_value,
        )
        proposed.utility_score = score.total
        proposed.score_breakdown_json = json.dumps(score.breakdown, sort_keys=True)
        reason_codes = ["COMMITMENT_PRESERVED", "MINIMAL_DISRUPTION"]
        if spec.v2_plan is not None:
            reason_codes.append("SOLVER_V2_WHOLE_HORIZON")
            if spec.v2_plan.risk_score:
                reason_codes.append("RISK_PENALTY_OPTIMIZED")
        elif replacement_choice is not None:
            reason_codes.extend(replacement_choice.reason_codes)
        elif candidate_delay:
            reason_codes.append("SCHEDULE_DELAY_ABSORBED")
        elif candidate_remove:
            reason_codes.append("DISRUPTED_ACTIVITY_REMOVED")
        proposal = ReplanProposalModel(
            id=str(uuid4()),
            journey_id=journey.id,
            trigger_event_id=trigger_id,
            base_plan_version_id=base.id,
            proposed_plan_version_id=proposed.id,
            status="PRESENTED",
            hard_constraints_passed=True,
            locked_commitments_preserved=True,
            utility_score=score.total,
            score_breakdown_json=proposed.score_breakdown_json,
            diff_json=json.dumps(
                {
                    "candidate_family": family,
                    "kept": diff.kept,
                    "moved": [moved.__dict__ for moved in diff.moved],
                    "removed": diff.removed,
                    "added": diff.added,
                    "cost_delta_minor": diff.cost_delta_minor,
                    "travel_delta_min": travel_delta,
                    "end_time_delta_min": int(
                        (
                            (diff.new_end_at or datetime.fromisoformat(journey.start_at))
                            - (diff.old_end_at or datetime.fromisoformat(journey.start_at))
                        ).total_seconds()
                        // 60
                    ),
                    "preserved_protected": diff.preserved_protected,
                    "before_feasibility_envelope": before_envelope,
                    "after_feasibility_envelope": after_envelope,
                    "opportunity_window_evidence": window_evidence,
                    "verified_evidence": {
                        "family": family,
                        "baseline_plan_evidence": baseline_plan_evidence,
                        "incident_recovery_evidence": incident_recovery_evidence,
                        "impact_resolution": {
                            "status": impact_resolution.status,
                            "maturity_stage": impact_resolution.maturity_stage,
                            "provider_key": impact_resolution.provider_key,
                            "model_key": impact_resolution.model_key,
                            "selected_activity_id": impact_resolution.selected_activity_id,
                            "primary_recovery_target_id": impact_resolution.primary_recovery_target_id,
                            "secondary_risk_activity_ids": list(
                                impact_resolution.secondary_risk_activity_ids
                            ),
                            "selected_corroboration_refs": list(
                                impact_resolution.selected_corroboration_refs
                            ),
                            "independent_evidence_count": impact_resolution.independent_evidence_count,
                            "uncertain": impact_resolution.uncertain,
                            "reason_codes": list(impact_resolution.validation.reason_codes),
                            "rejected_activity_ids": list(
                                impact_resolution.validation.rejected_activity_ids
                            ),
                            "failure_code": impact_resolution.failure_code,
                            "response_hash": impact_resolution.response_hash,
                            "semantic_reason": impact_resolution.semantic_reason,
                            "semantic_explanation": selected_impact_explanation,
                        },
                        "generation_rank": generation_rank,
                        "generator": generator_evidence,
                        "feasibility_authority": "CITYNEXUS_DETERMINISTIC_GATE",
                        "feasible": True,
                        "hard_constraint_result": {"passed": True, "failures": []},
                        "commitment_preservation": {
                            "preserved": True,
                            "protected_count": c4_count,
                        },
                        "stability": {
                            "score": score.breakdown["stability"],
                            "moved_count": len(diff.moved),
                            "removed_count": len(diff.removed),
                            "added_count": len(diff.added),
                        },
                        "budget": {
                            "remaining_minor": after_envelope["budget_remaining_minor"],
                            "currency": journey.budget_currency,
                        },
                        "mobility": {
                            "travel_delta_min": travel_delta,
                            "provider": mobility.source_provider_key,
                            "degraded": mobility.degraded,
                            "snapshot": mobility.to_evidence(),
                            "failed_primary_pairs": [
                                list(pair) for pair in mobility_capture.failed_primary_pairs
                            ],
                            "fallback_pairs": [
                                list(pair) for pair in mobility_capture.fallback_pairs
                            ],
                        },
                        "time": {
                            "journey_end_slack_min": after_envelope["journey_end_slack_min"],
                            "end_time_delta_min": int(
                                (
                                    (diff.new_end_at or datetime.fromisoformat(journey.start_at))
                                    - (diff.old_end_at or datetime.fromisoformat(journey.start_at))
                                ).total_seconds()
                                // 60
                            ),
                        },
                        "preference": {"fit": preference_fit},
                        "opportunity": {
                            "value": opportunity_value,
                            "evidence": window_evidence,
                        },
                        "reason_codes": sorted(set(reason_codes)),
                    },
                    "reason_codes": sorted(set(reason_codes)),
                    "execution_partition": {
                        "terminal_prefix_ids": partition.terminal_prefix_ids,
                        "current_activity_id": partition.current_activity_id,
                        "c4_anchor_ids": partition.c4_anchor_ids,
                        "mutable_suffix_ids": partition.mutable_suffix_ids,
                    },
                },
                default=str,
            ),
            explanation_json=json.dumps(
                {
                    "summary": "Reality-aware recovery proposal",
                    "reasons": [
                        "provider-backed recovery evidence",
                        family,
                        impact_resolution.semantic_reason,
                    ],
                    "impact_type": trigger_event.event_type,
                    "impact_provider": impact_resolution.provider_key,
                    "impact_model": impact_resolution.model_key,
                    "baseline_snapshot_id": frozen_v12_evidence.get("snapshot_id"),
                    "incident_snapshot_id": mobility.snapshot_id,
                    "protected_commitments": c4_count,
                    "source": generator_evidence["kind"],
                }
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        session.add(proposal)
        record_audit(
            session,
            action="replan.generated",
            entity_type="ReplanProposal",
            entity_id=proposal.id,
            journey_id=journey.id,
            after={
                "status": proposal.status,
                "candidate_family": family,
                "generator": generator_evidence,
                "feasibility_authority": "CITYNEXUS_DETERMINISTIC_GATE",
            },
        )
        created.append(proposal)
        accepted_assignment_keys.add(assignment_key)

    if not created:
        session.rollback()
        # Truth blocker B3: the precise unresolved reason must be evaluated
        # BEFORE the generic fallback is applied.  ``", ".join(...) or
        # "NO_DISTINCT_CANDIDATE"`` is never empty, so testing ``not details``
        # afterwards made REPLAN_TARGET_UNRESOLVED / REPLAN_PROTECTED_CONFLICT /
        # REPLAN_NO_ALTERNATIVE unreachable dead code.
        if heuristic_specs_unresolved_reason:
            raise HTTPException(422, heuristic_specs_unresolved_reason)
        # T2: "we could not measure the legs" is not the same finding as "no
        # replacement place exists".  Reporting the first as the second would
        # tell the traveller that nothing can be done when the truth is that
        # the engine lacked travel evidence.
        if mobility_unavailable_pairs:
            refusal = HTTPException(422, "MOBILITY_DATA_UNAVAILABLE")
            # R5 (arbiter provider policy): `details=null` on a refusal that already holds the
            # legs is a contract defect, not a style choice.  The message stays byte-identical -
            # the log line and the existing doors read it - and the segments this loop already
            # collected ride along, named, so a screen can say *which* leg was unmeasurable
            # instead of "no feasible proposal".
            # Both collection sites append the message set and this list in the same
            # statement, so the cells are the same legs - de-duplicated here only because a
            # leg can be refused for two different candidates.  The messages are not parsed
            # back into cells: that parser hard-codes one reason code and would relabel an
            # ineligible-evidence leg as a missing one.
            structured = {
                (str(cell["from_place_id"]), str(cell["to_place_id"]), str(cell["reason_code"])): cell
                for cell in mobility_gaps
            }
            gaps = sorted(structured.values(), key=lambda cell: str(cell["from_place_id"]))
            reason_codes = sorted({str(cell["reason_code"]) for cell in gaps})
            # ORD-0199 ①: "the frozen number for this leg is the fixture's blanket constant"
            # is emitted under its own name instead of being filed as "no data".  It is still a
            # refusal to decide, never a network/offline claim and never a `SELECTABLE` verdict -
            # the candidate stays excluded and the traveller is told which leg lacks real evidence.
            refusal.reason_code = (
                GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE
                if reason_codes == [GAP_MOBILITY_EVIDENCE_NOT_HARD_ELIGIBLE]
                else "MOBILITY_DATA_UNAVAILABLE"
            )
            refusal.error_details = {
                "gaps": gaps[:20],
                "gaps_total": len(gaps),
                "legs_required": len(gaps),
                # Aggregate view of the same truth: a consumer that only reads one field
                # still learns *why* the repair stopped.
                "reason_codes": reason_codes,
            }
            raise refusal
        details = ", ".join(sorted(failed_reasons)) or "NO_DISTINCT_CANDIDATE"
        raise HTTPException(422, f"no feasible replan proposal: {details}")

    record_audit(
        session,
        action="solver_candidates_generated",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        after={
            "proposal_ids": [item.id for item in created],
            "candidate_count": len(created),
            "feasibility_authority": "CITYNEXUS_DETERMINISTIC_GATE",
        },
    )

    # Python sort is stable, so exact-score ties preserve deterministic
    # candidate-generation order instead of using UUID4 as a random tiebreak.
    # Prefer lower disruption when utility is equal; a noisier candidate must
    # not outrank an otherwise equal, more stable repair.
    def _proposal_rank(candidate: ReplanProposalModel) -> tuple[float, float, float, float, float]:
        breakdown = json.loads(candidate.score_breakdown_json or "{}")
        generator = json.loads(candidate.diff_json or "{}").get("verified_evidence", {}).get(
            "generator", {}
        )
        # An explicit V2 request is a product-level request for the whole-
        # horizon CP-SAT schedule.  Deterministic complements remain persisted
        # for explainability, but must not outrank a feasible V2 assignment
        # merely because a heuristic preference score is slightly higher.
        solver_priority = (
            0.0
            if solver_mode == "v2" and generator.get("kind") == "ORTOOLS_CP_SAT_V2"
            else 1.0
        )
        return (
            solver_priority,
            -candidate.utility_score,
            -float(breakdown.get("stability", 0.0)),
            -float(breakdown.get("commitment_preservation", 0.0)),
            -float(breakdown.get("time_reliability", 0.0)),
        )

    created.sort(key=_proposal_rank)
    response = _replan_payload(created[0])
    if generation_idempotency_key is not None and generation_signature is not None:
        now = datetime.now(timezone.utc)
        session.add(
            IdempotencyKey(
                key=generation_idempotency_key,
                operation="replan.generate",
                request_hash=generation_signature,
                response_status=201,
                response_json=json.dumps(response, sort_keys=True),
                created_at=now.isoformat(),
                expires_at=(now + timedelta(days=7)).isoformat(),
            )
        )
    session.commit()
    return response


@app.get("/api/v1/journeys/{journey_id}/replans", response_model=list[ReplanProposalSchema])
def list_replans(journey_id: UUID, session: Session = Depends(get_session)):
    require(session, Journey, str(journey_id), "Journey")
    return [
        _replan_payload(p)
        for p in session.scalars(
            select(ReplanProposalModel)
            .where(ReplanProposalModel.journey_id == str(journey_id))
            .order_by(ReplanProposalModel.created_at)
        ).all()
    ]


@app.get("/api/v1/replans/{proposal_id}", response_model=ReplanProposalSchema)
def get_replan(proposal_id: UUID, session: Session = Depends(get_session)):
    return _replan_payload(
        require(session, ReplanProposalModel, str(proposal_id), "Replan proposal")
    )


@app.post("/api/v1/replans/{proposal_id}/accept")
def accept_replan(
    proposal_id: UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
):
    return _accept_replan_impl(proposal_id, idempotency_key, session)


def _switch_active_plan_atomically(
    session: Session, *, base: PlanVersion, proposed: PlanVersion, decided_at: str
) -> None:
    """Move the ACTIVE marker with predicates the database evaluates itself.

    Both flips are conditional.  The partial unique index
    `ux_plan_versions_single_active` then makes "two ACTIVE plans for one Journey"
    unrepresentable even if a future caller forgets this helper, and the commit
    path turns the resulting IntegrityError into the same 409 as everything else
    here.  A process-local lock cannot promise any of that across processes.
    """

    superseded = session.execute(
        update(PlanVersion)
        .where(PlanVersion.id == base.id, PlanVersion.status == "ACTIVE")
        .values(status="SUPERSEDED")
    )
    if superseded.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "replan base plan is no longer the active plan")
    activated = session.execute(
        update(PlanVersion)
        .where(PlanVersion.id == proposed.id, PlanVersion.status == "CANDIDATE")
        .values(status="ACTIVE")
    )
    if activated.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "replan candidate plan is no longer acceptable")
    # The bulk updates bypass the identity map, so every later read of these rows
    # in this transaction must come back from the database.
    session.expire_all()


def _revalidate_locked_commitments(
    session: Session, journey: Journey, proposal, proposed: PlanVersion
) -> None:
    """Recompute `locked_commitments_preserved` against current commitment truth.

    The field is a derived guard assertion, not a source of truth: the facts are
    the Commitment rows, the booking/payment state, and the activity identities on
    the canonical plans.  Recomputing it here closes the window where a payment was
    captured after the proposal was generated, because a boolean minted a minute
    ago cannot license destroying a promise made since.
    """

    locked_activity_ids = {
        item.id
        for item in session.scalars(
            select(Activity).where(
                Activity.plan_version_id == journey.active_plan_version_id,
                Activity.locked.is_(True),
            )
        ).all()
    }
    protected_activity_ids = {
        item.activity_id
        for item in session.scalars(
            select(CommitmentModel).where(
                CommitmentModel.journey_id == journey.id,
                CommitmentModel.status == "ACTIVE",
                CommitmentModel.level == "C4_Anchored",
                CommitmentModel.activity_id.is_not(None),
            )
        ).all()
    }
    must_survive = locked_activity_ids | protected_activity_ids
    if not must_survive:
        proposal.locked_commitments_preserved = True
        return
    child = {
        item.id: item
        for item in session.scalars(
            select(Activity).where(Activity.plan_version_id == proposed.id)
        ).all()
    }
    preserved = True
    for activity_id in must_survive:
        source = session.get(Activity, activity_id)
        if source is None:
            preserved = False
            break
        try:
            resolve_commitment_target(
                policy={
                    "semantic_key": str(source.place_id or source.city_service_id or ""),
                    "scheduled_start_at": source.start_at,
                },
                anchored_activity=source,
                candidates=list(child.values()),
                subject="locked commitment",
            )
        except CommitmentRemapConflict:
            preserved = False
            break
    proposal.locked_commitments_preserved = preserved
    if not preserved:
        raise HTTPException(
            409,
            "replan no longer preserves the locked and protected commitments captured "
            "since this proposal was generated",
        )


def _accept_replan_impl(
    proposal_id: UUID,
    idempotency_key: str | None,
    session: Session,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    policy_version: int | None = None,
    authorization_decision: str | None = None,
    reason_codes: tuple[str, ...] = (),
):
    """Serialize acceptance for one Journey across its whole transaction.

    Acceptance reads the base plan, the proposal status, the active C4
    commitments and plan feasibility, and only then writes.  Two callers that
    both pass those checks against the same pre-transition state would each
    produce a plan transition, which is how a Journey ends up with two ACTIVE
    PlanVersions.  The lock therefore starts before the first read and is
    released after the commit — locking the write alone would not help, because
    the second caller's validation has already happened by then.

    The lookup that finds the Journey id happens outside the lock and is only
    used to choose a lock key, so the identity map is expired before delegating:
    everything below must re-read under the lock rather than trust it.
    """

    kwargs = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "policy_version": policy_version,
        "authorization_decision": authorization_decision,
        "reason_codes": reason_codes,
    }
    located = session.get(ReplanProposalModel, str(proposal_id))
    journey_id = located.journey_id if located is not None else None
    if journey_id is None:
        # Unknown proposal: let the body below produce the canonical 404.
        return _accept_replan_serialized_impl(proposal_id, idempotency_key, session, **kwargs)
    with journey_write_lock(journey_id):
        session.expire_all()
        return _accept_replan_serialized_impl(proposal_id, idempotency_key, session, **kwargs)


def _replay_committed_accept(
    session: Session, scoped_key: str, signature: str, *, wait: bool = False
) -> dict[str, Any] | None:
    """Return the canonical response for an already-claimed idempotency key.

    The receipt row is inserted inside the same transaction as the plan transition,
    so finding it populated is proof the transition committed - not a guess that a
    sibling request probably succeeded.
    """

    attempts = 40 if wait else 1
    for attempt in range(attempts):
        session.expire_all()
        row = session.get(IdempotencyKey, scoped_key)
        if row is not None:
            if row.request_hash != signature:
                raise HTTPException(409, "same idempotency key used for a different request")
            stored = json.loads(row.response_json or "{}")
            if stored:
                return stored
        if not wait or attempt == attempts - 1:
            break
        # Waiting on durable state, not on a schedule: exactly-one-transition is
        # already guaranteed by the conditional writes and the unique index.  If the
        # receipt never lands, the caller gets a deterministic 409 and the database
        # is still correct.
        time.sleep(0.02)
    return None


def _accept_replan_serialized_impl(
    proposal_id: UUID,
    idempotency_key: str | None,
    session: Session,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    policy_version: int | None = None,
    authorization_decision: str | None = None,
    reason_codes: tuple[str, ...] = (),
):
    """Accept a replan, replaying any response this key already produced.

    The Journey lock above this call is an optimisation, not the contract.  Without
    it, two processes sharing one database must still agree: one performs the
    transition, the other discovers the committed receipt and returns the same body.
    """

    if idempotency_key is None:
        raise HTTPException(422, "Idempotency-Key header is required")
    try:
        header_key = validate_idempotency_key(idempotency_key)
    except CommerceValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    scoped_key = f"replan.accept:{header_key}"
    signature = canonical_request_signature({"proposal_id": str(proposal_id)})

    replayed = _replay_committed_accept(session, scoped_key, signature)
    if replayed is not None:
        return replayed

    kwargs = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "policy_version": policy_version,
        "authorization_decision": authorization_decision,
        "reason_codes": reason_codes,
    }
    try:
        return _accept_replan_executing(proposal_id, idempotency_key, session, **kwargs)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        replayed = _replay_committed_accept(session, scoped_key, signature, wait=True)
        if replayed is not None:
            return replayed
        raise
    except IntegrityError as exc:
        session.rollback()
        replayed = _replay_committed_accept(session, scoped_key, signature, wait=True)
        if replayed is not None:
            return replayed
        raise HTTPException(
            409, "replan conflicts with an authoritative plan transition in progress"
        ) from exc


def _accept_replan_executing(
    proposal_id: UUID,
    idempotency_key: str | None,
    session: Session,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    policy_version: int | None = None,
    authorization_decision: str | None = None,
    reason_codes: tuple[str, ...] = (),
):
    if idempotency_key is None:
        raise HTTPException(422, "Idempotency-Key header is required")
    try:
        header_key = validate_idempotency_key(idempotency_key)
    except CommerceValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    signature = canonical_request_signature({"proposal_id": str(proposal_id)})
    scoped_key = f"replan.accept:{header_key}"
    existing = session.get(IdempotencyKey, scoped_key)
    if existing is not None:
        if existing.request_hash != signature:
            raise HTTPException(409, "same idempotency key used for a different request")
        stored = json.loads(existing.response_json or "{}")
        if not stored:
            raise HTTPException(409, "replan idempotency record is incomplete")
        return stored

    proposal = require(session, ReplanProposalModel, str(proposal_id), "Replan proposal")
    if proposal.status != "PRESENTED":
        raise HTTPException(409, "replan proposal is no longer actionable")
    journey = require(session, Journey, proposal.journey_id, "Journey")
    if (
        journey.status != "ACTIVE"
        or journey.active_plan_version_id != proposal.base_plan_version_id
    ):
        raise HTTPException(409, "replan base plan is stale")
    trigger_event = (
        session.get(EventModel, proposal.trigger_event_id) if proposal.trigger_event_id else None
    )
    if trigger_event is None or trigger_event.status not in {"DETECTED", "ACKNOWLEDGED"}:
        raise HTTPException(409, "replan trigger event is no longer actionable")
    base = session.get(PlanVersion, proposal.base_plan_version_id)
    proposed = session.get(PlanVersion, proposal.proposed_plan_version_id)
    if (
        base is None
        or proposed is None
        or base.status != "ACTIVE"
        or proposed.status != "CANDIDATE"
    ):
        raise HTTPException(409, "replan plan is missing or inconsistent")
    accepted_mobility = _proposal_mobility_snapshot(proposal)
    if accepted_mobility is None:
        raise HTTPException(
            409, "replan mobility evidence is missing or invalid; regenerate proposal"
        )
    gate = validate_plan_feasibility(
        session,
        journey,
        proposed,
        include_active_c4=True,
        mobility_provider=accepted_mobility,
    )
    if not gate.passed:
        raise HTTPException(409, f"replan is no longer feasible: {', '.join(gate.failures)}")
    old_current = (
        session.get(Activity, journey.current_activity_id) if journey.current_activity_id else None
    )
    replacement_current = None
    if old_current is not None:
        replacement_current = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == proposed.id,
                Activity.sequence_no == old_current.sequence_no,
            )
        )
        if replacement_current is None:
            replacement_current = session.scalar(
                select(Activity)
                .where(
                    Activity.plan_version_id == proposed.id,
                    Activity.sequence_no > old_current.sequence_no,
                    Activity.status.not_in(("COMPLETED", "SKIPPED", "CANCELLED")),
                )
                .order_by(Activity.sequence_no)
            )
        if replacement_current is None:
            raise HTTPException(409, "replan does not preserve the current execution position")

    decided_at = datetime.now(timezone.utc).isoformat()

    # Re-derive the commitment guard from the rows that exist *now*.  The stored
    # verdict was computed when the proposal was generated, and a booking can have
    # been captured in between - which is precisely how a stale `true` could author
    # a plan transition that destroys a promise made after the proposal was built.
    _revalidate_locked_commitments(session, journey, proposal, proposed)

    # Conditional writes, not object assignment.  A read-then-write of the ORM
    # rows is exactly the window two concurrent accepters can both pass; the
    # predicate below is evaluated by the database against the current row, so a
    # writer whose base plan has already been superseded, or whose candidate has
    # already been consumed, loses on rowcount rather than on a race it cannot see.
    _switch_active_plan_atomically(session, base=base, proposed=proposed, decided_at=decided_at)
    journey.active_plan_version_id = proposed.id
    journey.current_activity_id = replacement_current.id if replacement_current else None
    proposal.status = "ACCEPTED"
    proposal.decided_at = decided_at
    if trigger_event.status == "DETECTED":
        trigger_event.status = "ACKNOWLEDGED"

    # Commitment.activity_id is PlanVersion-local. The durable authority is
    # the stable semantic/time anchor in policy_json, but JourneyTwin exposes
    # activity_id for the active plan. Remap every active C4 reference after
    # accepting a replan so the projection cannot point into the superseded
    # plan.
    proposed_activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == proposed.id)
        .order_by(Activity.sequence_no)
    ).all()
    active_commitments = session.scalars(
        select(CommitmentModel).where(
            CommitmentModel.journey_id == journey.id,
            CommitmentModel.status == "ACTIVE",
        )
    ).all()
    for commitment in active_commitments:
        policy = json.loads(commitment.policy_json or "{}")
        anchored = (
            session.get(Activity, commitment.activity_id) if commitment.activity_id else None
        )
        protected = commitment.level == "C4_Anchored"
        try:
            replacement = resolve_commitment_target(
                policy=policy,
                anchored_activity=anchored,
                candidates=list(proposed_activities),
                subject="accepted replan",
            )
        except CommitmentRemapConflict as exc:
            if protected:
                # Zero matches means the accepted plan silently dropped something the
                # traveller was promised; more than one means two activities claim the
                # same anchor and picking one would invent an identity the record does
                # not carry.  Both fail closed rather than persist a guessed pointer.
                raise HTTPException(
                    409, f"accepted replan cannot remap an active C4 commitment: {exc}"
                ) from exc
            continue
        commitment.activity_id = replacement.id
        commitment.updated_at = decided_at

    competitors = session.scalars(
        select(ReplanProposalModel).where(
            ReplanProposalModel.journey_id == journey.id,
            ReplanProposalModel.base_plan_version_id == base.id,
            ReplanProposalModel.id != proposal.id,
            ReplanProposalModel.status == "PRESENTED",
        )
    ).all()
    for competitor in competitors:
        competitor.status = "INVALIDATED"
        competitor.decided_at = decided_at
        competitor_plan = session.get(PlanVersion, competitor.proposed_plan_version_id)
        if competitor_plan is not None and competitor_plan.status == "CANDIDATE":
            competitor_plan.status = "REJECTED"
        record_audit(
            session,
            action="replan.invalidated",
            entity_type="ReplanProposal",
            entity_id=competitor.id,
            journey_id=journey.id,
            actor_type=actor_type,
            actor_id=actor_id,
            after={
                "status": competitor.status,
                "reason": "base_plan_superseded",
                "policy_version": policy_version,
                "authorization_decision": authorization_decision,
                "reason_codes": list(reason_codes),
            },
        )

    record_audit(
        session,
        action="replan.accepted",
        entity_type="ReplanProposal",
        entity_id=proposal.id,
        journey_id=journey.id,
        actor_type=actor_type,
        actor_id=actor_id,
        after={
            "status": proposal.status,
            "policy_version": policy_version,
            "authorization_decision": authorization_decision,
            "reason_codes": list(reason_codes),
        },
    )
    twin = _twin_snapshot(session, journey)
    response = {
        "journey": journey_schema(journey).model_dump(mode="json"),
        "proposal": _replan_payload(proposal),
        "twin": twin,
    }
    session.add(
        IdempotencyKey(
            key=scoped_key,
            operation="replan.accept",
            request_hash=signature,
            response_status=200,
            response_json=json.dumps(response, sort_keys=True),
            created_at=decided_at,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        # `ux_plan_versions_single_active` refused a second ACTIVE plan for this
        # Journey.  That is a concurrent acceptance from another session or
        # another process, i.e. a conflict with the same meaning as the optimistic
        # checks above - not a crash, and nothing may survive the rollback.
        session.rollback()
        raise HTTPException(
            409, "replan conflicts with an authoritative plan transition in progress"
        ) from exc
    return response


@app.post("/api/v1/replans/{proposal_id}/reject", response_model=ReplanProposalSchema)
def reject_replan(proposal_id: UUID, session: Session = Depends(get_session)):
    proposal = require(session, ReplanProposalModel, str(proposal_id), "Replan proposal")
    if proposal.status == "REJECTED":
        return _replan_payload(proposal)
    if proposal.status != "PRESENTED":
        raise HTTPException(409, "replan proposal is no longer rejectable")
    proposal.status = "REJECTED"
    proposal.decided_at = datetime.now(timezone.utc).isoformat()
    proposed = session.get(PlanVersion, proposal.proposed_plan_version_id)
    if proposed is not None and proposed.status == "CANDIDATE":
        proposed.status = "REJECTED"
    record_audit(
        session,
        action="replan.rejected",
        entity_type="ReplanProposal",
        entity_id=proposal.id,
        journey_id=proposal.journey_id,
        after={"status": proposal.status},
    )
    session.commit()
    return _replan_payload(proposal)


def _copilot_context(session: Session, journey: Journey) -> dict[str, Any]:
    """Read selected-plan facts without refreshing or creating JourneyTwin state."""

    plan = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    activities = (
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == plan.id)
            .order_by(Activity.sequence_no)
        ).all()
        if plan is not None
        else []
    )
    legs = (
        session.scalars(
            select(JourneyLeg)
            .where(JourneyLeg.plan_version_id == plan.id)
            .order_by(JourneyLeg.sequence_no)
        ).all()
        if plan is not None
        else []
    )
    commitments = session.scalars(
        select(CommitmentModel)
        .where(CommitmentModel.journey_id == journey.id, CommitmentModel.status == "ACTIVE")
        .order_by(CommitmentModel.created_at, CommitmentModel.id)
    ).all()
    preferences = session.scalars(
        select(Preference)
        .where(Preference.traveler_id == journey.traveler_id)
        .order_by(Preference.category, Preference.preference_key)
    ).all()

    activity_payload = [
        {
            "id": item.id,
            "sequence_no": item.sequence_no,
            "title": item.title,
            "activity_type": item.activity_type,
            "start_at": item.start_at,
            "end_at": item.end_at,
            "status": item.status,
            "locked": bool(item.locked),
            "cost_minor": item.estimated_cost_minor,
        }
        for item in activities
    ]
    leg_payload = [
        {
            "id": item.id,
            "from_key": item.from_key,
            "to_key": item.to_key,
            "mode": item.mode,
            "duration_min": item.duration_min,
            "walking_min": item.walking_min,
            "provider_key": item.provider_key,
            "provenance": item.provenance,
        }
        for item in legs
    ]
    commitment_payload = []
    for item in commitments:
        policy = _json_object(item.policy_json)
        commitment_payload.append(
            {
                "id": item.id,
                "activity_id": item.activity_id,
                "kind": item.kind,
                "level": item.level,
                "protected": item.level in {"C3_Guaranteed", "C4_Anchored"},
                "scheduled_start_at": policy.get("scheduled_start_at"),
                "external_ref": item.external_ref,
            }
        )
    preference_payload = []
    for item in preferences:
        try:
            value = json.loads(item.value_json or "null")
        except json.JSONDecodeError:
            value = item.value_json
        preference_payload.append(
            {
                "category": item.category,
                "key": item.preference_key,
                "value": value,
                "weight": item.weight,
                "source": item.source,
            }
        )
    capabilities = capability_payload(session, journey.traveler_id, journey.id)
    mobility_evidence = _json_object(plan.mobility_evidence_json) if plan is not None else {}
    # `timezone` above names a zone; this says which zone it actually resolves to and whether that
    # is the one the journey asked for.  Without the pair the name could point at a zone nothing can
    # be placed in and the reader -- the copilot's own prompt, built from this dict -- had no way to
    # tell, which is the shape `execution_clock.now_honest` answers for and `journey_schema` now
    # answers for the trip payload.  Same vocabulary, one source of truth for "can this zone be used".
    _, used_timezone, timezone_honoured = execution_clock.now_honest(journey.timezone)
    return {
        "journey_id": journey.id,
        "city_code": journey.city_code,
        "timezone": journey.timezone,
        "used_timezone": used_timezone,
        "timezone_honoured": timezone_honoured,
        "currency": journey.budget_currency,
        "status": journey.status,
        "current_activity_id": journey.current_activity_id,
        "plan": (
            {
                "id": plan.id,
                "version_no": plan.version_no,
                "status": plan.status,
                "source": plan.source,
                "cost_minor": plan.total_cost_minor,
                "currency": plan.currency,
                "activities": activity_payload,
                "legs": leg_payload,
                "mobility_evidence": mobility_evidence,
            }
            if plan is not None
            else None
        ),
        "commitments": commitment_payload,
        "preferences": preference_payload,
        "capabilities": capabilities,
        "capability_summary": summarize_capabilities(capabilities),
        "protected_commitments": [item for item in commitment_payload if item["protected"]],
    }


def _copilot_fallback_or_ai(message: str, context: dict[str, Any]) -> CopilotInterpretation:
    fallback = classify_copilot_message(message)
    client = _deepseek_client()
    if client is None:
        return fallback
    plan = context.get("plan") or {}
    current_activity = next(
        (
            item
            for item in plan.get("activities", [])
            if str(item.get("id")) == str(context.get("current_activity_id"))
        ),
        None,
    )
    prompt_context = {
        "journey": {
            "id": context["journey_id"],
            "city_code": context["city_code"],
            "timezone": context["timezone"],
            # Whether the zone above can be used, so the model never names a moment in a zone nothing
            # can be placed in: an unusable zone is stated as unusable rather than rendered as if
            # honoured.  The pair comes from the context dict, which is the one place that resolves it.
            "used_timezone": context.get("used_timezone"),
            "timezone_honoured": context.get("timezone_honoured"),
            "status": context["status"],
        },
        "selected_plan": {
            "id": plan.get("id"),
            "version_no": plan.get("version_no"),
            "status": plan.get("status"),
            "currency": plan.get("currency"),
            "cost_minor": plan.get("cost_minor"),
        },
        "current_activity": current_activity,
        "protected_commitments": context["protected_commitments"],
        "preferences": context["preferences"],
        "capabilities": context.get("capabilities", []),
        "capability_summary": context.get("capability_summary", {}),
        "authority": (
            "The server owns ids, coordinates, prices, provider facts, commitments, "
            "and mutations. Return meaning only; do not claim an authoritative mutation."
        ),
    }
    try:
        envelope = compile_semantic_envelope(client, message, prompt_context)
        if envelope is None:
            return fallback
        decision = apply_semantic_policy(envelope)
        entities = dict(envelope.entities)
        if decision.event_type is not None:
            entities["event_type"] = decision.event_type
        entities["assertion_status"] = envelope.assertion_status
        entities["effective_time"] = envelope.effective_time
        semantic_payload = envelope.as_dict()
        semantic_payload.update(
            {
                "route_intent": decision.route_intent,
                "server_action": decision.action,
                "create_event": decision.create_event,
                "verify_evidence": decision.verify_evidence,
            }
        )
        if envelope.capabilities:
            entities["capabilities"] = [dict(item) for item in envelope.capabilities]
            entities["capability_summary"] = summarize_capabilities(envelope.capabilities)
        return CopilotInterpretation(
            decision.route_intent,
            envelope.confidence,
            envelope.reasoning_summary or decision.reason,
            entities,
            "deepseek",
            False,
            semantic_payload,
        )
    except (DeepSeekResponsesError, TypeError, ValueError, json.JSONDecodeError, httpx.HTTPError):
        return fallback


# Traveller-facing names for the city codes the product can be created with.
# The internal code (CN-XIAMEN) is an identifier, never copy for a person.
_CITY_DISPLAY_NAMES = {
    "XIAMEN": "厦门",
    "TIANJIN": "天津",
    "BEIJING": "北京",
    "SHANGHAI": "上海",
    "GUANGZHOU": "广州",
    "SHENZHEN": "深圳",
    "HANGZHOU": "杭州",
    "CHENGDU": "成都",
    "XIAN": "西安",
    "NANJING": "南京",
    "QINGDAO": "青岛",
    "SANYA": "三亚",
    "KUNMING": "昆明",
    "CHONGQING": "重庆",
    "WUHAN": "武汉",
    "SUZHOU": "苏州",
}


def _copilot_city_label(city_code: str) -> str:
    """Traveller-facing city name; never leak the internal CN-XXXX code."""

    raw = str(city_code or "").strip()
    slug = raw.split("-", 1)[1] if "-" in raw else raw
    slug = slug.replace("_", " ").replace("-", " ").strip()
    return _CITY_DISPLAY_NAMES.get(slug.upper(), slug or raw)


# Questions ask for a *kind* of place ("咖啡馆"), while providers label the
# same thing in another language ("cafe").  Without this bridge the traveller is
# told nothing is known about coffee simply because of vocabulary.
_PLACE_INTENT_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("咖啡馆", "咖啡", "coffee", "cafe", "café"), ("cafe", "coffee", "咖啡馆")),
    (("餐厅", "吃饭", "美食", "restaurant", "eat"), ("restaurant", "餐厅", "food")),
    (("博物馆", "museum"), ("museum", "博物馆")),
    (("公园", "park", "garden"), ("park", "garden", "公园")),
    (("图书馆", "library"), ("library", "图书馆")),
    (("剧院", "剧场", "theatre", "theater"), ("theatre", "theater", "剧院")),
    (("美术馆", "艺术", "gallery", "art"), ("arts_centre", "gallery", "美术馆")),
    (("景点", "attraction", "sight"), ("attraction", "tourism", "景点")),
    (("酒店", "住宿", "hotel", "stay"), ("hotel", "hostel", "guest_house", "酒店")),
)


def _copilot_place_intents(query: str) -> tuple[str, ...]:
    """Category terms the traveller is asking about, in provider vocabulary."""

    text = str(query or "").casefold()
    terms: list[str] = []
    for question_terms, provider_terms in _PLACE_INTENT_ALIASES:
        if any(term.casefold() in text for term in question_terms):
            terms.extend(provider_terms)
    return tuple(dict.fromkeys(terms))


def _query_terms(query: str) -> list[str]:
    """Small, honest keyword split for matching a question against known places."""

    text = str(query or "").strip()
    # Strip the question frame so the place itself is what gets searched.
    for filler in (
        "好玩吗", "怎么样", "值得去吗", "好玩么", "好玩不", "介绍一下", "值得去",
        "好玩", "值得", "介绍", "有什么好玩", "有什么", "附近", "哪里", "哪些", "推荐",
        "worth it", "any good", "tell me about", "is it good", "what is there",
    ):
        text = text.replace(filler, " ")
    for mark in ("？", "?", "。", "，", ",", "！", "!", "的", "吗", "呢", "啊", "…"):
        text = text.replace(mark, " ")
    return [part for part in text.split() if len(part) >= 2]


def _place_matches_query(name: str, category: str, terms: list[str], intents: tuple[str, ...]) -> bool:
    haystack = f"{name} {category}".casefold()
    if any(term.casefold() in haystack for term in terms):
        return True
    return any(intent.casefold() in haystack for intent in intents)


def _copilot_place_answer(
    session: Session,
    journey: Journey,
    message: str,
    interpretation: CopilotInterpretation,
    *,
    locale: str,
) -> tuple[str, dict[str, Any] | None]:
    """Answer a place question from real discovery evidence.

    The Copilot dock invites the traveller to ask about the city, so a question
    about a place must return an actual answer built from captured provider
    facts.  When no evidence describes what was asked, we say exactly that
    instead of presenting unrelated nearby places as an answer.
    """

    entities = interpretation.entities if isinstance(interpretation.entities, dict) else {}
    query = str(
        entities.get("place_query")
        or entities.get("subject_text")
        or entities.get("query")
        or message
    ).strip()[:120]
    terms = _query_terms(query)
    intents = _copilot_place_intents(query)
    # Speak about the place, not the whole sentence: a traveller who asks
    # "鼓浪屿好玩吗？" should read an answer about 鼓浪屿.
    subject = " ".join(terms).strip() or query
    if intents and not terms:
        subject = query

    result: dict[str, Any] = {}
    try:
        result = capture_real_city_candidates(
            session, journey, query=query, limit=8, mode="live"
        )
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        result = {
            "candidates": [],
            "mode": "live-unavailable",
            "reason": str(exc)[:160],
            # ORD-0099: the same named reason the discovery module publishes, so a caller can
            # branch on a stable code instead of pattern-matching exception text. `reason`
            # stays exactly as it was - this key is added beside it.
            "reason_code": live_failure_reason(exc),
        }
    candidates = list(result.get("candidates") or [])
    source = "live" if candidates else "captured-library"

    # Live providers are frequently unreachable from a phone on a foreign
    # network.  The Journey may already hold provider-backed places captured
    # earlier, and those are real evidence too.
    if not candidates:
        captured = session.scalars(
            select(Place)
            .where(
                Place.city_code == journey.city_code,
                Place.provider_key.notin_(_SERVER_OWNED_PLACE_PROVIDERS),
            )
            .order_by(Place.id)
        ).all()
        candidates = [
            {
                "name": item.name,
                "category": item.category,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "provider_key": item.provider_key,
            }
            for item in captured
            if item.latitude is not None and item.longitude is not None
        ]

    relevant = [
        item
        for item in candidates
        if _place_matches_query(
            str(item.get("name") or ""), str(item.get("category") or ""), terms, intents
        )
    ]
    label = _copilot_city_label(journey.city_code)
    evidence = {
        "candidates": relevant[:6],
        "considered": len(candidates),
        "mode": result.get("mode") or source,
        "relevant": bool(relevant),
    }

    if not relevant:
        # Overpass answers category questions, so unrelated cafes must never be
        # dressed up as an answer about the place the traveller actually asked
        # about.
        sample = "、".join(str(item.get("name")) for item in candidates[:3])
        if locale == "zh":
            detail = f"供应商只返回了附近几处地点（{sample}），它们不能代表「{subject}」。" if sample else ""
            return (
                f"我查不到「{subject}」的可信资料，所以不会编一个评价给你。{detail}"
                "你可以换个具体问法，比如“附近有什么咖啡馆”或“附近有什么博物馆”，"
                "也可以去“城市发现”里手动搜索。",
                evidence,
            )
        detail = (
            f"The provider only returned nearby places ({sample}), which do not describe “{subject}”. "
            if sample
            else ""
        )
        return (
            f"I have no provider-backed facts about “{subject}”, so I will not invent a verdict. {detail}"
            "Try a concrete question such as “any cafes nearby” or “which museums are nearby”, "
            "or search in City Discovery.",
            evidence,
        )

    lines = []
    for item in relevant[:4]:
        name = str(item.get("name") or "未命名地点")
        category = str(item.get("category") or "")
        distance = item.get("distance_m")
        bits = [name]
        if category:
            bits.append(category)
        if isinstance(distance, (int, float)):
            bits.append(f"{int(distance)} m")
        lines.append(" · ".join(bits))
    header = (
        f"关于「{subject}」，我在{label}找到 {len(relevant)} 个真实地点："
        if locale == "zh"
        else f"For “{subject}” I found {len(relevant)} provider-backed places in {label}:"
    )
    footer = (
        "这些是候选地点，不会自动写进你的行程；想加入的话告诉我，我会先核对可行性和你的受保护安排。"
        if locale == "zh"
        else "These are candidates only — nothing is written to your trip. Tell me to add one and I will check feasibility and your protected commitments first."
    )
    newline = chr(10)
    answer = header + newline + newline.join(f"• {line}" for line in lines) + newline + footer
    return answer, evidence

def _copilot_answer(
    interpretation: CopilotInterpretation,
    context: dict[str, Any],
    *,
    locale: str,
    what_if: dict[str, Any] | None = None,
) -> str:
    plan = context.get("plan") or {}
    plan_label = (
        f"PlanVersion {plan.get('version_no')} ({plan.get('status')})"
        if plan
        else "no selected PlanVersion"
    )
    protected_count = len(context.get("protected_commitments", []))
    if what_if is not None:
        if what_if.get("status") == "NOT_SIMULATABLE_FROM_CURRENT_EVIDENCE":
            if locale == "zh":
                return "当前还没有已选的权威 PlanVersion。请先在住宿选择中选择一个通过供应商事实和确定性门控的方案，再预览时间或费用变化；本次试算没有修改行程。"
            return "There is no selected authoritative PlanVersion yet. Select a provider-backed plan in Stay Explorer before previewing time or cost changes; this preview changed nothing."
        return str(what_if["explanation"])
    if interpretation.intent == "EXPLAIN_PLAN":
        if locale == "zh":
            return f"当前使用 {plan_label}，服务器已核验 {protected_count} 个受保护承诺。解释基于已选方案事实，不会修改行程。"
        return f"The current journey uses {plan_label}; the server verified {protected_count} protected commitments. This explanation uses selected-plan facts and changes nothing."
    if interpretation.intent == "DISCOVER":
        return "我会基于当前城市和行程上下文搜索候选地点；搜索结果不会直接写入方案。" if locale == "zh" else "I can search candidates using the current city and journey context; discovery results will not write to the plan."
    capability_items = interpretation.entities.get("capabilities") if isinstance(interpretation.entities, dict) else None
    if isinstance(capability_items, list) and capability_items:
        codes = [str(item.get("code")) for item in capability_items if isinstance(item, dict)]
        hard = [item for item in capability_items if isinstance(item, dict) and item.get("severity") == "HARD"]
        labels = ", ".join(code for code in codes if code)
        if locale == "zh":
            if hard:
                return f"已识别能力约束（{labels}）。其中硬性需求需要明确确认；可行性必须由 CityNexus 根据供应商/冻结证据核验，未知无障碍证据不会被视为可通行。"
            return f"已识别旅行能力与偏好（{labels}）。它们会影响候选评估和解释，但不会由 Copilot 静默修改 TravelerTwin。"
        if hard:
            return f"I identified capability constraints ({labels}). Hard needs require explicit confirmation; CityNexus must verify feasibility from provider/frozen evidence, and unknown accessibility evidence is not treated as accessible."
        return f"I identified traveler capabilities/preferences ({labels}). They will guide candidate evaluation and explanations without silently changing the TravelerTwin."
    if interpretation.intent == "PREFERENCE_CHANGE":
        return "已识别为偏好请求。当前只展示请求，不会静默修改 TravelerTwin；请通过明确的偏好更新动作继续。" if locale == "zh" else "I recognized a preference request. This response does not silently change the TravelerTwin; continue with an explicit preference update action."
    if interpretation.intent == "REPLAN_REQUEST":
        return "已识别为重新规划请求。需要通过现有确定性可行性和授权路径生成方案，不会由 Copilot 直接改写当前方案。" if locale == "zh" else "I recognized a replan request. The existing deterministic feasibility and authorization path must produce it; Copilot will not rewrite the current plan directly."
    if interpretation.intent == "VERIFY_FACT":
        return "这是一条尚未核实的旅行信息。我会先基于供应商或来源证据核验，不会先写入城市事件。" if locale == "zh" else "This is an unverified travel claim. It must be checked against provider or source evidence before becoming a city event."
    if interpretation.intent == "PERSONAL_ASSISTANCE":
        return "我识别到这是个人求助，需要按个人情况处理，不会把它当作城市天气或交通事件。" if locale == "zh" else "I recognized a personal assistance request; it will not be turned into a city weather or traffic event."
    if locale == "zh":
        return "我没有可靠识别这条请求。可以问我为什么这样安排、如果多待一小时会怎样、找一个室内地点，或报告正在发生的交通/天气变化。"
    return "I could not route this request reliably. Ask why the plan looks this way, what would happen if you stayed an hour longer, find an indoor place, or report a disruption that is happening now."


def _capture_copilot_disruption(
    session: Session, journey: Journey, message: str, interpretation: CopilotInterpretation
) -> dict[str, Any]:
    event_type = str(interpretation.entities.get("event_type") or disruption_event_type(message) or "")
    if event_type not in {"CLOSURE", "TRAFFIC_DELAY", "SCHEDULE_DELAY", "WEATHER"}:
        raise HTTPException(422, "a current disruption type is required")
    semantic = interpretation.semantic_envelope or {}
    semantic_entities = semantic.get("entities") if isinstance(semantic.get("entities"), dict) else {}
    return _capture_reported_disruption(
        session,
        journey,
        event_type=event_type,
        description=message,
        provider_key="user-copilot",
        source="copilot",
        semantic_payload={
            "assertion_status": semantic.get("assertion_status", "ASSERTED"),
            "effective_time": semantic.get("effective_time", "NOW"),
            "event_type": event_type,
            "requested_action": semantic.get("requested_action", "REQUEST_REPLAN"),
            "subject_text": semantic_entities.get("subject_text"),
            "temporal_expression": semantic_entities.get("temporal_expression"),
        },
        semantic_envelope=semantic,
        occurred_at_fallback=None,
        audit_action="copilot.disruption.captured",
        commit=True,
        payload_extra={"simulation_only": False},
    )


def _verify_copilot_fact(
    session: Session,
    journey: Journey,
    message: str,
    interpretation: CopilotInterpretation,
) -> dict[str, Any]:
    """Capture bounded, sourced evidence without promoting a claim to Event truth."""

    semantic = interpretation.semantic_envelope or {}
    entities = semantic.get("entities") if isinstance(semantic.get("entities"), dict) else {}
    subject = str(entities.get("subject_text") or message).strip()
    client = _deepseek_client()
    if client is None or not settings.deepseek_web_search_enabled:
        return {
            "status": "NOT_CONFIGURED",
            "query": subject,
            "observations": [],
            "event_created": False,
        }
    drafts = WebResearchProvider(client).search(
        f"Verify this travel disruption claim using official sources: {subject}",
        source_class="OFFICIAL_OR_OPERATOR",
    )
    rows: list[WebObservation] = []
    for item in drafts[:5]:
        row = session.scalar(
            select(WebObservation).where(
                WebObservation.journey_id == journey.id,
                WebObservation.evidence_fingerprint == item.evidence_fingerprint,
            )
        )
        if row is None:
            row = WebObservation(
                id=str(uuid4()),
                journey_id=journey.id,
                query=item.query,
                url=item.url,
                domain=item.domain,
                title=item.title,
                source_class=item.source_class,
                provider_key=item.provider_key,
                model_key=item.model_key,
                captured_at=item.captured_at,
                evidence_fingerprint=item.evidence_fingerprint,
                content_excerpt=item.content_excerpt,
                authority_status="NON_AUTHORITATIVE",
                created_at=item.captured_at,
            )
            session.add(row)
        rows.append(row)
    session.commit()
    return {
        "status": "EVIDENCE_CAPTURED_REVIEW_REQUIRED" if rows else "UNRESOLVED",
        "query": subject,
        "observations": [
            {
                "id": item.id,
                "title": item.title,
                "domain": item.domain,
                "captured_at": item.captured_at,
                "authority_status": item.authority_status,
            }
            for item in rows
        ],
        "event_created": False,
    }


@app.post("/api/v1/journeys/{journey_id}/copilot/messages")
async def copilot_message(
    journey_id: UUID, body: dict[str, Any], session: Session = Depends(get_session)
):
    journey = require(session, Journey, str(journey_id), "Journey")
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(422, "message is required")
    context = _copilot_context(session, journey)
    interpretation = await asyncio.to_thread(_copilot_fallback_or_ai, message, context)
    locale = str(body.get("locale") or ("zh" if journey.timezone.startswith("Asia/") else "en"))
    locale = "zh" if locale.lower().startswith("zh") else "en"
    what_if = None
    event = None
    verification = None
    discovery_evidence = None
    action = None
    answer: str | None = None
    semantic_allows_event = interpretation.semantic_envelope is None or bool(
        interpretation.semantic_envelope.get("create_event")
    )
    if interpretation.intent == "DISRUPTION_REPORT" and semantic_allows_event:
        event = _capture_copilot_disruption(session, journey, message, interpretation)
        if event["requires_review"]:
            action = "REVIEW_FUTURE_EVENT_TARGET"
            answer = (
                "已保留这条未来事件，但现有行程事实无法唯一定位受影响活动；在确认目标前不会启动恢复。"
                if locale == "zh"
                else "I preserved this future event, but current Journey facts do not identify one affected activity. Recovery will wait for target review."
            )
        else:
            action = "REQUEST_REPLAN"
            answer = (
                "已按行程中的权威时间和活动记录事件，下一步可打开恢复中心检查确定性重规划。"
                if locale == "zh"
                else "I recorded this event against the authoritative Journey activity and time. Open recovery to run deterministic replanning."
            )
    else:
        if interpretation.intent == "WHAT_IF":
            what_if = simulate_what_if(message, context)
            action = what_if.get("next_action")
        elif interpretation.intent == "DISCOVER":
            action = "REQUEST_PLACE_SEARCH"
            # The dock invites the traveller to ask about the city, so answer
            # with real discovery evidence instead of only routing them away.
            answer, discovery_evidence = await asyncio.to_thread(
                _copilot_place_answer,
                session,
                journey,
                message,
                interpretation,
                locale=locale,
            )
        elif interpretation.intent == "PREFERENCE_CHANGE":
            action = "REQUEST_PREFERENCE_UPDATE"
        elif interpretation.intent == "REPLAN_REQUEST":
            action = "REQUEST_REPLAN"
        elif interpretation.intent == "EXPLAIN_PLAN":
            action = "REQUEST_PLAN_EXPLANATION"
        elif interpretation.intent == "VERIFY_FACT":
            action = "VERIFY_EVIDENCE"
            try:
                verification = await asyncio.to_thread(
                    _verify_copilot_fact, session, journey, message, interpretation
                )
            except (DeepSeekResponsesError, httpx.HTTPError, AttributeError):
                verification = {
                    "status": "FAILED_EXTERNAL",
                    "observations": [],
                    "event_created": False,
                }
        validate_proposed_action(action)
        if answer is None:
            answer = _copilot_answer(
                interpretation, context, locale=locale, what_if=what_if
            )
    agent_run = None
    semantic_capabilities = []
    if isinstance(interpretation.semantic_envelope, dict):
        semantic_capabilities = interpretation.semantic_envelope.get("capabilities") or []
    if not semantic_capabilities and isinstance(interpretation.entities, dict):
        semantic_capabilities = interpretation.entities.get("capabilities") or []
    capability_multi_step = bool(semantic_capabilities) and any(
        marker in message.casefold()
        for marker in (
            "比较", "对比", "替代", "候选", "调整", "重新安排", "重排", "最佳",
            "compare", "alternative", "candidate", "adjust", "reorganize", "best option",
            "search", "replan", "rain", "下雨",
        )
    )
    if (
        interpretation.intent in {"REPLAN_REQUEST", "DISRUPTION_REPORT"}
        or capability_multi_step
    ) and not (
        event and event.get("requires_review")
    ):
        native_status = native_hermes_status()
        if native_status.get("ready"):
            native = HermesNativeClient()
            trigger_event_id = str(event.get("id")) if isinstance(event, dict) and event.get("id") else None
            native_mission = create_mission(
                session,
                journey,
                command=message,
                requested_autonomy="PLAN_ONLY",
                trigger_event_id=trigger_event_id,
            )
            session.commit()
            try:
                native_plan = mission_plan_for_model(native_mission)
                native_tool_sequence = [
                    str(step["tool_name"])
                    for step in native_plan["steps"]
                    if step.get("tool_name")
                ]
                native_run = native.create_run(
                    mission_id=native_mission.id,
                    journey_id=journey.id,
                    input_text=message,
                    instructions=(
                        "Follow the supplied CityNexus mission plan in order. You MUST call each listed MCP tool "
                        f"in this sequence: {', '.join(native_tool_sequence)}. "
                        f"For every CityNexus MCP call, set journey_id exactly to {journey.id}. "
                        f"The CityNexus mission_id is {native_mission.id}; do not invent or omit either identifier. "
                        + (
                            f"For request_replan, set trigger_event_id exactly to {native_mission.trigger_event_id}, "
                            "candidate_count to 3, and solver_mode to deterministic. "
                            if native_mission.trigger_event_id
                            else "There is no trigger event; do not call request_replan until CityNexus provides one. "
                        )
                        + "Call one MCP tool at a time (never batch or use tool_call metadata), do not repeat a completed step. "
                        "Use only CityNexus MCP tools and allowed_tools. Inspect the Journey, JourneyTwin, "
                        "and protected commitments before acting. Produce a PLAN_ONLY proposal; never accept, "
                        "book, pay, or mutate. Never invent provider facts, IDs, timestamps, or authority."
                    ),
                    autonomy="PLAN_ONLY",
                    mission_plan=native_plan,
                    session_id=f"citynexus-{journey.id}-mission-{native_mission.id}",
                )
                native_mission.status = "RUNNING"
                native_mission.started_at = datetime.now(timezone.utc).isoformat()
                session.commit()
                initial_events = _native_citynexus_events(
                    run_id=native_run.run_id,
                    journey_id=journey.id,
                    mission=native_mission,
                    interpretation=interpretation,
                )
                agent_run = {
                    "run_id": native_run.run_id,
                    "status": native_run.status,
                    "runtime_identity": "HERMES_NATIVE_MCP",
                    "provider_key": "hermes-native",
                    "model_key": settings.hermes_native_model or f"profile:{settings.hermes_native_profile}",
                    "mission_id": native_mission.id,
                    "events_url": f"/api/v1/journeys/{journey.id}/agent/hermes/runs/{native_run.run_id}/events",
                    "timeline_url": f"/api/v1/journeys/{journey.id}/agent/hermes/runs/{native_run.run_id}/timeline",
                    "mission_plan": native_plan,
                    "initial_events": initial_events,
                    "protected_commitments": context["protected_commitments"],
                }
                answer = (
                    "已交给 Hermes 检查 Journey、受保护承诺和恢复证据；结果会以真实运行进度返回。"
                    if locale == "zh"
                    else "Hermes is inspecting the Journey, protected commitments, and recovery evidence; progress will come from the native run."
                )
            except (httpx.HTTPError, ValueError):
                native_mission.status = "FAILED_EXTERNAL"
                native_mission.failure_reason = "native Hermes run could not be created"
                native_mission.completed_at = datetime.now(timezone.utc).isoformat()
                session.commit()
                agent_run = None
            finally:
                native.close()
    return {
        "message": answer,
        "mode": interpretation.mode,
        "verified_context_used": True,
        "intent": interpretation.payload(),
        "action": {"type": action} if action else None,
        "what_if": what_if,
        "event": event,
        "verification": verification,
        "discovery": discovery_evidence,
        "agent_run": agent_run,
        "protected_commitments": context["protected_commitments"],
        "evidence": {
            "journey_id": context["journey_id"],
            "city_code": context["city_code"],
            "selected_plan_version_id": (context["plan"] or {}).get("id") if context["plan"] else None,
            "provider_facts": bool((context["plan"] or {}).get("mobility_evidence")) if context["plan"] else False,
            "simulation_only": bool(what_if),
        },
        "recommendations": [],
    }


@app.get("/api/v1/city-services/{service_id}/booking-slots", response_model=list[BookingSlotSchema])
def booking_slots(
    service_id: str,
    start_at: datetime,
    end_at: datetime,
    session: Session = Depends(get_session),
):
    if end_at <= start_at:
        raise HTTPException(422, "end_at must be after start_at")
    if service_id not in load_catalog().get("booking_services", {}):
        raise HTTPException(404, f"CityService {service_id} was not found")
    try:
        provider, _ = _mock_booking_provider("mock-booking")
        slots = commerce_get_booking_slots(
            session,
            service_id=service_id,
            start_at=start_at.isoformat(),
            end_at=end_at.isoformat(),
            provider=provider,
        )
        session.commit()
        return [
            {
                "start_at": slot.start_at,
                "end_at": slot.end_at,
                "provider_key": slot.provider_key,
                "provider_slot_ref": slot.provider_slot_ref,
            }
            for slot in slots
        ]
    except CommerceConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (CommerceValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/journeys/{journey_id}/bookings", response_model=BookingSchema, status_code=201)
def create_journey_booking(
    journey_id: UUID,
    payload: BookingCreate,
    x_mock_failure: str | None = Header(default=None, alias="X-Mock-Failure"),
    session: Session = Depends(get_session),
):
    try:
        provider, provider_failure = _mock_booking_provider(payload.provider_key)
        item = commerce_create_booking(
            session,
            journey_id=str(journey_id),
            activity_id=str(payload.activity_id),
            provider=provider,
            source_strength_codes=SOURCE_COMMITMENT_STRENGTHS,
            booking_source_type_code="booking",
            assert_selected_plan_feasible=_assert_selected_plan_feasible,
            force_failure=provider_failure
            or payload.force_failure
            or payload.failure_token is not None
            or x_mock_failure == "FORCE_BOOKING_FAIL",
        )
        session.commit()
        return _booking_payload(item)
    except CommerceConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (CommerceValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/bookings/{booking_id}", response_model=BookingSchema)
def get_booking(booking_id: UUID, session: Session = Depends(get_session)):
    item = session.get(BookingModel, str(booking_id))
    if item is None:
        raise HTTPException(404, f"Booking {booking_id} was not found")
    return _booking_payload(item)


@app.post("/api/v1/bookings/{booking_id}/cancel", response_model=BookingSchema)
def cancel_journey_booking(
    booking_id: UUID,
    x_mock_failure: str | None = Header(default=None, alias="X-Mock-Failure"),
    session: Session = Depends(get_session),
):
    item = session.get(BookingModel, str(booking_id))
    if item is None:
        raise HTTPException(404, f"Booking {booking_id} was not found")
    try:
        provider, provider_failure = _mock_booking_provider(item.provider_key)
        updated = commerce_cancel_booking(
            session,
            booking_id=item.id,
            provider=provider,
            force_failure=provider_failure or x_mock_failure == "FORCE_BOOKING_CANCEL_FAIL",
        )
        session.commit()
        return _booking_payload(updated)
    except CommerceConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (CommerceValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/journeys/{journey_id}/orders", response_model=OrderSchema, status_code=201)
def create_journey_order(
    journey_id: UUID,
    payload: OrderCreate,
    session: Session = Depends(get_session),
):
    try:
        _mock_booking_provider(payload.provider_key)
        activity = session.get(Activity, str(payload.activity_id))
        if activity is None:
            raise CommerceValidationError("Activity not found")
        catalog = load_catalog()
        service_id = service_id_for_activity(session, activity, catalog, order=True)
        item = commerce_create_order(
            session,
            journey_id=str(journey_id),
            activity_id=str(payload.activity_id),
            service_id=service_id,
            items=[
                OrderLineRequest(sku=line.sku, quantity=line.quantity) for line in payload.items
            ],
            catalog=catalog,
            source_strength_codes=SOURCE_COMMITMENT_STRENGTHS,
            order_source_type_code="booking",
            provider_key=payload.provider_key,
            assert_selected_plan_feasible=_assert_selected_plan_feasible,
        )
        session.commit()
        return _order_payload(item)
    except OrderPricingError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except (CommerceValidationError, CommerceConflict, ValueError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@app.post(
    "/api/v1/journeys/{journey_id}/transactions",
    response_model=TransactionSchema,
    status_code=201,
)
def create_journey_transaction(
    journey_id: UUID,
    payload: TransactionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_mock_failure: str | None = Header(default=None, alias="X-Mock-Failure"),
    session: Session = Depends(get_session),
):
    if idempotency_key is None:
        raise HTTPException(422, "Idempotency-Key header is required")
    # Held across the commit and the rollback: a capture validates a booking, then
    # promotes the commitment behind it, so releasing between validation and commit
    # would let a concurrent replan switch the authoritative plan and leave that
    # promotion pointing at an activity no active plan contains.
    with journey_write_lock(str(journey_id)):
        session.expire_all()
        try:
            provider, provider_failure = _mock_payment_provider(payload.provider_key)
            item = commerce_create_transaction(
                session,
                journey_id=str(journey_id),
                booking_id=str(payload.booking_id) if payload.booking_id else None,
                order_id=str(payload.order_id) if payload.order_id else None,
                transaction_type=payload.transaction_type,
                requested_amount_minor=payload.amount.amount_minor,
                requested_currency=payload.amount.currency,
                provider=provider,
                idempotency_key=idempotency_key,
                source_strength_codes=SOURCE_COMMITMENT_STRENGTHS,
                force_failure=provider_failure
                or payload.force_failure
                or payload.failure_token is not None
                or x_mock_failure == "FORCE_PAYMENT_FAIL",
            )
            session.commit()
            return _transaction_payload(item)
        except (IdempotencyConflict, CommerceConflict) as exc:
            session.rollback()
            raise HTTPException(409, str(exc)) from exc
        except (CommerceValidationError, ValueError) as exc:
            session.rollback()
            raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/transactions/{transaction_id}", response_model=TransactionSchema)
def get_transaction(transaction_id: UUID, session: Session = Depends(get_session)):
    item = session.get(PaymentTransaction, str(transaction_id))
    if item is None:
        raise HTTPException(404, f"Transaction {transaction_id} was not found")
    return _transaction_payload(item)


@app.get("/api/v1/commitments/{commitment_id}", response_model=CommitmentSchema)
def get_commitment(commitment_id: UUID, session: Session = Depends(get_session)):
    item = session.get(CommitmentModel, str(commitment_id))
    if item is None:
        raise HTTPException(404, f"Commitment {commitment_id} was not found")
    return _commitment_payload(item)
