from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Activity,
    Commitment,
    CounterfactualSimulation,
    Event,
    Journey,
    JourneyResilienceSnapshot,
    JourneyLeg,
    Place,
    PlanVersion,
    ProviderObservation,
    RealPlaceCandidate,
)
from app.execution.traveler_twin import planning_preference_items
from app.integrations.discovery.real_city import normalize_opening_hours
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureError
from app.replanning.provider_snapshots import MobilitySnapshot
from app.services import validate_plan_feasibility
from app.planning.solver_v2 import (
    SolverV2Anchor,
    SolverV2Option,
    SolverV2Problem,
    SolverV2Slot,
    solve_v2,
    solver_v2_problem_fingerprint,
)
from app.replanning.candidate_evidence import verified_interest_preference_fit
from app.agent.standby import contract_for_simulation, standby_trigger_match


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mobility_expires(captured_at: str) -> str:
    """Derive the evidence TTL from capture time; never use captured_at as expiry."""
    return (_dt(captured_at) + timedelta(seconds=settings.real_city_cache_ttl_seconds)).isoformat()


def _standby_expired(evidence: dict[str, Any], now: datetime | None = None) -> bool:
    expires = evidence.get("expires_at")
    if not expires:
        return True
    try:
        return _dt(str(expires)) <= (now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return True


def _active_activities(session: Session, journey: Journey) -> list[Activity]:
    if not journey.active_plan_version_id:
        return []
    return list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no, Activity.id)
        ).all()
    )


def _future(activity: Activity, journey: Journey) -> bool:
    try:
        return _dt(activity.end_at) >= _dt(journey.start_at)
    except ValueError:
        return True


def _latest_observations(session: Session, journey_id: str) -> list[ProviderObservation]:
    rows = session.scalars(
        select(ProviderObservation)
        .where(ProviderObservation.journey_id == journey_id)
        .order_by(ProviderObservation.captured_at.desc(), ProviderObservation.id.desc())
    ).all()
    seen: set[str] = set()
    latest: list[ProviderObservation] = []
    for row in rows:
        if row.observation_type in seen:
            continue
        seen.add(row.observation_type)
        latest.append(row)
    return latest


def _opening_minutes(value: str | None, *, journey_start: datetime, default: int) -> int:
    if not value:
        return default
    try:
        parsed = datetime.combine(
            journey_start.date(),
            time.fromisoformat(value),
            tzinfo=journey_start.tzinfo,
        )
        return max(0, int((parsed - journey_start).total_seconds() // 60))
    except (TypeError, ValueError):
        return default


def _location(activity: Activity | None) -> str:
    return (
        (activity.place_id or activity.city_service_id or activity.id)
        if activity
        else "journey-start"
    )


def _risk_event(session: Session, journey_id: str) -> tuple[Event | None, dict[str, Any]]:
    rows = session.scalars(
        select(Event)
        .where(
            Event.journey_id == journey_id,
            Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
        )
        .order_by(Event.detected_at.desc(), Event.id.desc())
    ).all()
    for row in rows:
        payload = _load(row.payload_json, {})
        if payload.get("predictive_only") is True:
            return row, payload
    return None, {}


def snapshot_payload(snapshot: JourneyResilienceSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "journey_id": snapshot.journey_id,
        "score": snapshot.score,
        "risk_score": snapshot.score,
        "status": snapshot.status,
        "captured_at": snapshot.captured_at,
        "expires_at": snapshot.expires_at,
        "components": _load(snapshot.components_json, {}),
        "activity_risks": _load(snapshot.activity_risks_json, []),
        "commitment_buffers": _load(snapshot.commitment_buffers_json, []),
        "dominant_failure_modes": _load(snapshot.failure_modes_json, []),
        "preventive_recommendation": _load(snapshot.recommendation_json, {}),
        "evidence_provenance": _load(snapshot.provenance_json, []),
        "fingerprint": snapshot.fingerprint,
    }


def latest_snapshot(session: Session, journey_id: str) -> JourneyResilienceSnapshot | None:
    latest = session.scalar(
        select(JourneyResilienceSnapshot)
        .where(JourneyResilienceSnapshot.journey_id == journey_id)
        .order_by(JourneyResilienceSnapshot.captured_at.desc(), JourneyResilienceSnapshot.id.desc())
    )
    # Keep the last material predictive finding visible while the reactive
    # pulse records the post-prevention stable state.  The snapshot itself is
    # immutable evidence, so the UI can explain what was prevented after the
    # Journey has already recovered.
    predictive = session.scalar(
        select(JourneyResilienceSnapshot)
        .where(
            JourneyResilienceSnapshot.journey_id == journey_id,
            JourneyResilienceSnapshot.status == "PREDICTIVE_WEAKNESS",
        )
        .order_by(JourneyResilienceSnapshot.captured_at.desc(), JourneyResilienceSnapshot.id.desc())
    )
    return predictive or latest


def build_resilience_snapshot(session: Session, journey: Journey) -> JourneyResilienceSnapshot:
    activities = _active_activities(session, journey)
    places = {
        item.id: item
        for item in session.scalars(select(Place).where(Place.city_code == journey.city_code)).all()
    }
    observations = _latest_observations(session, journey.id)
    weather_payload: dict[str, Any] = {}
    mobility_payload: dict[str, Any] = {}
    provenance: list[dict[str, Any]] = []
    for row in observations:
        payload = _load(row.payload_json, {})
        if row.observation_type == "CITY_SIGNAL":
            weather_payload = payload
        if row.observation_type == "MOBILITY":
            mobility_payload = payload
        provenance.append(
            {
                "provider_key": row.provider_key,
                "observation_type": row.observation_type,
                "provenance": row.provenance,
                "captured_at": row.captured_at,
                "expires_at": row.expires_at,
                "observation_id": row.id,
            }
        )
    fixture_payload = weather_payload.get("payload") or {}
    risk_event, risk_event_payload = _risk_event(session, journey.id)
    forecast_risk = int(
        risk_event_payload.get("risk_score")
        or fixture_payload.get("risk_score")
        or (82 if fixture_payload.get("state") == "RISK_FORECAST" else 0)
    )
    meaningful_deltas = mobility_payload.get("meaningful_deltas") or []
    max_mobility_delta = max(
        [int(item.get("delta_minutes", 0)) for item in meaningful_deltas if isinstance(item, dict)]
        or [0]
    )
    future_activities = [item for item in activities if _future(item, journey)]
    activity_risks: list[dict[str, Any]] = []
    for activity in future_activities:
        place = places.get(activity.place_id) if activity.place_id else None
        tags = set(_load(place.tags_json if place else "[]", []))
        outdoor = bool(tags.intersection({"outdoor", "scenic", "waterfront"}))
        risk = 0
        modes: list[str] = []
        if outdoor and forecast_risk:
            risk += min(70, forecast_risk)
            modes.append("FORECAST_WEATHER_EXPOSURE")
        if max_mobility_delta:
            risk += min(25, max_mobility_delta)
            modes.append("MOBILITY_BUFFER_COMPRESSION")
        if risk_event is not None and risk_event.affected_activity_id == activity.id:
            risk += 10
        activity_risks.append(
            {
                "activity_id": activity.id,
                "title": activity.title,
                "place_id": activity.place_id,
                "risk_score": min(100, risk),
                "outdoor": outdoor,
                "failure_modes": modes,
                "predictive": bool(forecast_risk and outdoor),
            }
        )
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
        )
    ).all()
    commitment_buffers: list[dict[str, Any]] = []
    for commitment in commitments:
        activity = next((item for item in activities if item.id == commitment.activity_id), None)
        buffer_min = 0
        predecessor_end = _dt(journey.start_at)
        safety_buffer_min = 5
        if activity is not None:
            predecessor = max(
                (item for item in activities if item.sequence_no < activity.sequence_no),
                key=lambda item: item.sequence_no,
                default=None,
            )
            predecessor_end = (
                _dt(predecessor.end_at) if predecessor is not None else _dt(journey.start_at)
            )
            frozen_travel_min = max(0, int(activity.travel_from_prev_min))
            buffer_min = max(
                0,
                int((_dt(activity.start_at) - predecessor_end).total_seconds() // 60)
                - frozen_travel_min
                - safety_buffer_min,
            )
        else:
            frozen_travel_min = 0
        commitment_buffers.append(
            {
                "commitment_id": commitment.id,
                "activity_id": commitment.activity_id,
                "level": commitment.level,
                "protected": commitment.level in {"C3_Guaranteed", "C4_Anchored"},
                "buffer_min": buffer_min,
                "protected_anchor_slack_min": buffer_min,
                "predecessor_end_at": predecessor_end.isoformat(),
                "frozen_travel_min": frozen_travel_min,
                "safety_buffer_min": safety_buffer_min,
                "slack_formula": "protected_start - predecessor_end - frozen_travel - safety_buffer",
                "monetary_value_minor": commitment.monetary_value_minor,
            }
        )
    active_plan = (
        session.get(PlanVersion, journey.active_plan_version_id)
        if journey.active_plan_version_id
        else None
    )
    real_candidates = session.scalars(
        select(RealPlaceCandidate).where(
            RealPlaceCandidate.journey_id == journey.id,
            RealPlaceCandidate.status == "DISCOVERED",
        )
    ).all()
    cost_headroom = max(
        0,
        journey.budget_amount_minor - (active_plan.total_cost_minor if active_plan else 0),
    )
    mutable_count = sum(
        1
        for item in future_activities
        if not item.locked and item.status not in {"COMPLETED", "CANCELLED"}
    )
    weather_exposure = min(
        100, forecast_risk + (20 if any(item["outdoor"] for item in activity_risks) else 0)
    )
    mobility_sensitivity = min(100, max_mobility_delta * 3)
    optionality = min(100, (mutable_count * 20) + (len(real_candidates) * 10))
    route_exposure = min(100, max_mobility_delta * 2 + (15 if len(activities) >= 5 else 0))
    single_point = (
        35 if len(real_candidates) == 0 and any(item["outdoor"] for item in activity_risks) else 10
    )
    protected_buffers = [item["buffer_min"] for item in commitment_buffers if item["protected"]]
    commitment_slack = min(protected_buffers) if protected_buffers else 60
    score = max(
        0,
        min(
            100,
            round(
                max(
                    forecast_risk,
                    weather_exposure,
                    mobility_sensitivity + route_exposure // 2,
                    100 - min(100, commitment_slack * 2),
                )
                + (10 if optionality == 0 else 0)
                + (5 if cost_headroom < 5000 else 0)
                + single_point // 4
            ),
        ),
    )
    dominant_modes: list[str] = []
    if forecast_risk:
        dominant_modes.append("FORECAST_WEATHER")
    if max_mobility_delta:
        dominant_modes.append("ROUTE_DELAY")
    if protected_buffers and commitment_slack < 20:
        dominant_modes.append("COMMITMENT_SLACK")
    if optionality == 0:
        dominant_modes.append("SINGLE_POINT_OF_FAILURE")
    target = max(activity_risks, key=lambda item: item["risk_score"], default=None)
    predictive_trigger = (
        forecast_risk >= settings.predictive_risk_threshold and risk_event is not None
    )
    recommendation = {
        "action": "PREEMPTIVE_REPLAN" if predictive_trigger else "CONTINUE_MONITORING",
        "risk_reduction_meaningful": predictive_trigger,
        "target_activity_id": target.get("activity_id") if target else None,
        "reason": "Provider forecast predicts a weak outdoor slot before hard infeasibility."
        if predictive_trigger
        else "No predictive provider forecast currently exceeds the predictive threshold.",
        "threshold": settings.predictive_risk_threshold,
        "reversible": True,
    }
    components = {
        "commitment_slack": commitment_slack,
        "weather_exposure": weather_exposure,
        "mobility_sensitivity": mobility_sensitivity,
        "recovery_optionality": optionality,
        "cost_headroom_minor": cost_headroom,
        "route_geography_exposure": route_exposure,
        "single_point_of_failure": single_point,
        "forecast_risk": forecast_risk,
    }
    stable_provenance = [
        {
            "provider_key": item["provider_key"],
            "observation_type": item["observation_type"],
            "provenance": item["provenance"],
        }
        for item in provenance
    ]
    material = {
        "journey_id": journey.id,
        "components": components,
        "activity_risks": activity_risks,
        "commitment_buffers": commitment_buffers,
        "dominant_failure_modes": dominant_modes,
        "recommendation": recommendation,
        "provider_evidence": stable_provenance,
    }
    fingerprint = hashlib.sha256(_json(material).encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(JourneyResilienceSnapshot).where(
            JourneyResilienceSnapshot.journey_id == journey.id,
            JourneyResilienceSnapshot.fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return existing
    captured_at = _now()
    snapshot = JourneyResilienceSnapshot(
        id=str(uuid4()),
        journey_id=journey.id,
        score=score,
        status="PREDICTIVE_WEAKNESS" if predictive_trigger else "STABLE",
        captured_at=captured_at,
        expires_at=(
            datetime.fromisoformat(captured_at)
            + timedelta(seconds=settings.real_city_cache_ttl_seconds)
        ).isoformat(),
        components_json=_json(components),
        activity_risks_json=_json(activity_risks),
        commitment_buffers_json=_json(commitment_buffers),
        failure_modes_json=_json(dominant_modes),
        recommendation_json=_json(recommendation),
        provenance_json=_json(provenance),
        fingerprint=fingerprint,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _scenario_problem(
    session: Session,
    journey: Journey,
    *,
    scenario_type: str,
    parameters: dict[str, Any],
) -> tuple[SolverV2Problem | None, list[Activity], list[SolverV2Anchor]]:
    activities = _active_activities(session, journey)
    if not activities:
        return None, [], []
    journey_start = _dt(journey.start_at)
    journey_end_min = max(1, int((_dt(journey.end_at) - journey_start).total_seconds() // 60))
    places = {
        item.id: item
        for item in session.scalars(select(Place).where(Place.city_code == journey.city_code)).all()
    }
    candidates = list(
        session.scalars(
            select(RealPlaceCandidate).where(
                RealPlaceCandidate.journey_id == journey.id,
                RealPlaceCandidate.status == "DISCOVERED",
            )
        ).all()
    )
    candidate_places = [places.get(item.materialized_place_id) for item in candidates]
    candidate_places = [item for item in candidate_places if item is not None]
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level == "C4_Anchored",
        )
    ).all()
    anchors: list[SolverV2Anchor] = []
    # A counterfactual is evaluated against the journey's current execution
    # cursor, not against the original pre-trip schedule.  Executed and
    # current activities are therefore immutable anchors even when no C4
    # commitment row was created for them.  Without this, a fresh mobility
    # scenario can legally drop the activity the traveller is already doing;
    # the resulting standby branch cannot be activated or refreshed honestly.
    current_index = next(
        (idx for idx, item in enumerate(activities) if item.id == journey.current_activity_id),
        -1,
    )
    execution_anchor_ids = {
        str(item.id)
        for idx, item in enumerate(activities)
        if item.status in {"COMPLETED", "IN_PROGRESS"} or (current_index >= 0 and idx <= current_index)
    }
    for commitment in commitments:
        activity = next((item for item in activities if item.id == commitment.activity_id), None)
        if activity is None:
            continue
        execution_anchor_ids.add(str(activity.id))
    for activity in activities:
        if str(activity.id) not in execution_anchor_ids:
            continue
        anchors.append(
            SolverV2Anchor(
                key=f"{activity.id}@{activity.start_at}",
                location_key=_location(activity),
                start_min=max(
                    0, int((_dt(activity.start_at) - journey_start).total_seconds() // 60)
                ),
                duration_min=max(
                    1, int((_dt(activity.end_at) - _dt(activity.start_at)).total_seconds() // 60)
                ),
            )
        )
    scenario_delta = int(parameters.get("route_delay_min", 0))
    all_locations = [_location(item) for item in activities] + [
        item.id for item in candidate_places
    ]
    latest_mobility = next(
        (
            row
            for row in _latest_observations(session, journey.id)
            if row.observation_type == "MOBILITY"
        ),
        None,
    )
    active = session.get(PlanVersion, journey.active_plan_version_id)
    mobility = _load(active.mobility_evidence_json if active else None, {})
    observed_mobility = _load(latest_mobility.payload_json if latest_mobility else None, {})
    current_minutes = dict(mobility.get("current_minutes") or {})
    # Runtime captures persist authoritative edge facts as a list.  The
    # counterfactual solver must consume those real minutes instead of falling
    # back to its bounded diagnostic default, otherwise a retime can pass the
    # solver and fail the final live feasibility gate.
    for fact in [*(mobility.get("facts") or []), *(observed_mobility.get("facts") or [])]:
        if not isinstance(fact, dict):
            continue
        origin = fact.get("origin_key")
        destination = fact.get("destination_key")
        minutes = fact.get("minutes")
        if origin and destination and minutes is not None:
            current_minutes[f"{origin}|{destination}"] = int(minutes)
    current_minutes.update(observed_mobility.get("current_minutes") or {})
    travel: dict[tuple[str, str], int] = {}
    for origin in all_locations:
        for destination in all_locations:
            if origin == destination:
                travel[(origin, destination)] = 0
            else:
                travel[(origin, destination)] = (
                    int(current_minutes.get(f"{origin}|{destination}", 15))
                    + (scenario_delta if destination not in {_location(item) for item in activities[: current_index + 1]} else 0)
                )
    slots: list[SolverV2Slot] = []
    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)
    first_mutable_future_index = next(
        (
            idx
            for idx, item in enumerate(activities)
            if idx > current_index and str(item.id) not in execution_anchor_ids
        ),
        -1,
    )
    for activity_index, activity in enumerate(activities):
        # Anchors are already scheduled by the solver as immutable nodes.
        # Duplicating them as slots makes the same visit consume time twice.
        if str(activity.id) in execution_anchor_ids:
            continue
        place = places.get(activity.place_id) if activity.place_id else None
        original_start = max(0, int((_dt(activity.start_at) - journey_start).total_seconds() // 60))
        original_end = min(
            journey_end_min, int((_dt(activity.end_at) - journey_start).total_seconds() // 60)
        )
        duration = max(1, original_end - original_start)
        tags = set(_load(place.tags_json if place else "[]", []))
        outdoor = bool(tags.intersection({"outdoor", "scenic", "waterfront"}))
        # P80/P95 mobility deterioration is a material future failure mode:
        # the bounded counterfactual sacrifices the first future visit rather
        # than pretending the same itinerary still fits.  Leave that slot
        # with no KEEP/replacement option so the deterministic solver must
        # take its explicit DROP branch; the materializer then builds a real
        # shorter standby from the active catalog and captured mobility.
        forced_mobility_drop = (
            scenario_type in {"route_plus_20", "route_plus_30"}
            and activity_index == first_mutable_future_index
        )
        forced_closure_drop = (
            scenario_type == "mutable_place_closes"
            and activity_index == first_mutable_future_index
        )
        forced_weather_drop = (
            scenario_type == "rain_earlier"
            and activity_index == first_mutable_future_index
            and outdoor
        )
        forced_retime = (
            scenario_type == "route_plus_10"
            and activity_index == first_mutable_future_index
        )
        options: list[SolverV2Option] = []
        if (
            not forced_mobility_drop
            and not forced_closure_drop
            and not forced_weather_drop
            and not forced_retime
            and (
            scenario_type != "mutable_place_closes" or not outdoor
            )
        ):
            options.append(
                SolverV2Option(
                    option_key=f"KEEP:{activity.id}",
                    location_key=_location(activity),
                    action="KEEP",
                    duration_min=duration,
                    cost_minor=activity.estimated_cost_minor,
                    earliest_start_min=0,
                    latest_end_min=journey_end_min,
                    original_start_min=original_start,
                    original_cost_minor=activity.estimated_cost_minor,
                    risk_penalty_milli=600 if scenario_type == "rain_earlier" and outdoor else 0,
                )
            )
        if forced_retime:
            options.append(
                SolverV2Option(
                    option_key=f"RETIME_KEEP:{activity.id}",
                    location_key=_location(activity),
                    action="RETIME",
                    duration_min=duration,
                    cost_minor=activity.estimated_cost_minor,
                    earliest_start_min=original_start + max(5, scenario_delta),
                    latest_end_min=journey_end_min,
                    original_start_min=original_start,
                    original_cost_minor=activity.estimated_cost_minor,
                )
            )
        for candidate in (
            [] if forced_mobility_drop or forced_weather_drop
            else candidate_places[:3]
        ):
            candidate_tags = set(_load(candidate.tags_json, []))
            if (
                scenario_type == "replacement_disappears"
                and candidate.provider_key == "openstreetmap-replay"
            ):
                continue
            if scenario_type == "rain_earlier" and "outdoor" in candidate_tags:
                continue
            opening = normalize_opening_hours(_load(candidate.opening_hours_json, {}))
            if opening.get("status") not in {"NORMALIZED_VALID"}:
                continue
            earliest = _opening_minutes(opening.get("open"), journey_start=journey_start, default=0)
            latest = _opening_minutes(
                opening.get("close"), journey_start=journey_start, default=journey_end_min
            )
            if latest <= earliest:
                continue
            metadata = _load(candidate.metadata_json, {})
            cost_evidence = metadata.get("cost_evidence") or {}
            evidence_status = str(cost_evidence.get("status") or "UNKNOWN")
            # A missing cost is not a free replacement.  Estimates can help
            # the strategy layer, but with the lab authority envelope off
            # they cannot enter a truth-grounded executable future.
            if evidence_status in {"KNOWN", "KNOWN_FREE", "VERIFIED_PROVIDER"}:
                candidate_cost = int(cost_evidence.get("amount_minor") or 0)
            else:
                continue
            preference_fit = verified_interest_preference_fit(
                traveler_preferences, candidate_tags=candidate_tags
            )
            options.append(
                SolverV2Option(
                    option_key=f"REPLACE:{activity.id}:{candidate.id}",
                    location_key=candidate.id,
                    action="REPLACE",
                    duration_min=duration,
                    cost_minor=max(0, candidate_cost),
                    earliest_start_min=earliest,
                    latest_end_min=min(journey_end_min, latest),
                    original_start_min=original_start,
                    original_cost_minor=activity.estimated_cost_minor,
                    preference_milli=round(
                        1000 if preference_fit is None else preference_fit * 1000
                    ),
                    risk_penalty_milli=0,
                )
            )
        if options or forced_mobility_drop or forced_closure_drop or forced_weather_drop:
            slots.append(
                SolverV2Slot(
                    slot_key=activity.id,
                    sequence_no=int(activity.sequence_no),
                    original_location_key=_location(activity),
                    original_start_min=original_start,
                    original_end_min=original_end,
                    original_cost_minor=activity.estimated_cost_minor,
                    options=tuple(options),
                    must_change=(
                        (scenario_type == "mutable_place_closes" and outdoor)
                        or forced_mobility_drop
                        or forced_closure_drop
                        or forced_weather_drop
                        or forced_retime
                    ),
                    allow_drop=(
                        scenario_type != "mutable_place_closes"
                        and not forced_retime
                        and not any(
                            anchor.location_key == _location(activity) for anchor in anchors
                        )
                    ),
                )
            )
    if not slots:
        return None, activities, anchors
    base_cost = sum(item.estimated_cost_minor for item in activities)
    problem = SolverV2Problem(
        journey_id=journey.id,
        operation_key=f"counterfactual:{journey.id}:{scenario_type}",
        now_min=0,
        journey_end_min=journey_end_min,
        budget_ceiling_minor=journey.budget_amount_minor,
        base_cost_minor=base_cost,
        current_location_key=_location(activities[0]),
        slots=tuple(slots),
        protected_anchors=tuple(anchors),
        frozen_mobility_snapshot_id=(mobility.get("snapshot_id") or f"counterfactual:{journey.id}"),
        travel_minutes=travel,
        # The active plan already passed its deterministic schedule gate with
        # captured edge durations. Counterfactuals must not add an unannounced
        # second buffer that invalidates that same frozen reality before the
        # new branch can be rechecked with its immutable provider snapshot.
        safety_buffer_min=0,
        max_schedule_drift_min=180,
        top_k=3,
    )
    return problem, activities, anchors


def counterfactual_payload(row: CounterfactualSimulation) -> dict[str, Any]:
    return {
        "id": row.id,
        "journey_id": row.journey_id,
        "snapshot_id": row.snapshot_id,
        "scenario_key": row.scenario_key,
        "scenario_type": row.scenario_type,
        "parameters": _load(row.parameters_json, {}),
        "feasible": bool(row.feasible),
        "c4_preserved": bool(row.c4_preserved),
        "cost_delta_minor": row.cost_delta_minor,
        "schedule_drift_min": row.schedule_drift_min,
        "plan_changes": _load(row.plan_changes_json, []),
        "evidence": _load(row.evidence_json, {}),
        "created_at": row.created_at,
    }


def standby_portfolio(session: Session, journey: Journey, event: Event | None = None) -> list[dict[str, Any]]:
    """Compact server-owned read model for the non-current plan branches."""
    rows = session.scalars(
        select(PlanVersion)
        .where(PlanVersion.journey_id == journey.id, PlanVersion.lifecycle == "STANDBY")
        .order_by(PlanVersion.created_at.desc(), PlanVersion.version_no.desc())
    ).all()
    now = datetime.now(timezone.utc)
    current = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    result: list[dict[str, Any]] = []
    for row in rows:
        evidence = _load(row.mobility_evidence_json, {})
        explanation = _load(row.explanation_json, {})
        expires = evidence.get("expires_at")
        result.append({
            "plan_id": row.id,
            "version_no": row.version_no,
            "scenario": _load(row.explanation_json, {}).get("trigger"),
            "status": row.status,
            "lifecycle": row.lifecycle,
            "parent_plan_version_id": row.parent_plan_version_id,
            "expires_at": expires,
            "expired": _standby_expired(evidence, now),
            "activity_count": int(session.scalar(
                select(func.count(Activity.id)).where(Activity.plan_version_id == row.id)
            ) or 0),
            "total_cost_minor": row.total_cost_minor,
            "mobility_snapshot_id": row.mobility_snapshot_id,
            "material_difference_fingerprint": explanation.get("material_difference_fingerprint"),
            "material_difference": explanation.get("material_difference", {}),
            "trigger_contract": _load(row.trigger_contract_json, {}),
            "trigger_match": (
                standby_trigger_match(row, event, current).code if event is not None else "UNASSESSED"
            ),
        })
    return result


def maintain_standby_portfolio(session: Session, journey: Journey) -> dict[str, Any]:
    """Expire, invalidate and bound standby branches without touching active reality."""
    active = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    rows = session.scalars(
        select(PlanVersion).where(
            PlanVersion.journey_id == journey.id, PlanVersion.lifecycle == "STANDBY"
        ).order_by(PlanVersion.created_at.asc(), PlanVersion.version_no.asc())
    ).all()
    retired: list[dict[str, str]] = []
    kept: list[PlanVersion] = []
    for row in rows:
        evidence = _load(row.mobility_evidence_json, {})
        reason = None
        if active is None or row.parent_plan_version_id != active.id:
            reason = "PARENT_NOT_CURRENT"
        elif _standby_expired(evidence):
            reason = "MOBILITY_EVIDENCE_EXPIRED"
        elif not evidence.get("facts") or not row.mobility_snapshot_id:
            reason = "MOBILITY_EVIDENCE_MISSING"
        if reason:
            row.lifecycle = "RETIRED"
            row.status = "REJECTED"
            retired.append({"plan_id": row.id, "reason": reason})
        else:
            kept.append(row)
    # The portfolio is intentionally small and bounded.  Oldest branches are
    # retired deterministically; the active plan is never part of this set.
    for row in kept[:-3]:
        row.lifecycle = "RETIRED"
        row.status = "REJECTED"
        retired.append({"plan_id": row.id, "reason": "PORTFOLIO_BOUND"})
    for item in retired:
        from app.core.audit import record_audit
        record_audit(
            session, action="standby.retired", entity_type="PlanVersion",
            entity_id=item["plan_id"], journey_id=journey.id, actor_type="SYSTEM",
            actor_id="standby-maintenance", after=item,
        )
    session.flush()
    return {"active_plan_version_id": active.id if active else None, "kept": len(kept), "retired": retired}


def _shadow_evidence(
    session: Session,
    journey: Journey,
    active: PlanVersion,
    kept: list[Activity],
    *,
    scenario_key: str,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Build a new immutable mobility snapshot for one standby order."""

    base = _load(active.mobility_evidence_json, {})
    base_snapshot = MobilitySnapshot.from_evidence(base)
    keys = [str(item.place_id or item.city_service_id or item.id) for item in kept]
    pairs = [(left, right) for left, right in zip(keys, keys[1:]) if left != right]
    missing = pairs if force_refresh else [pair for pair in pairs if pair not in base_snapshot.facts]
    captured = None
    if missing:
        try:
            captured = capture_runtime_mobility_snapshot(
                session,
                journey,
                missing,
                operation_key=f"shadow:{journey.id}:{scenario_key}",
                require_explicit_facts=True,
            )
        except MobilitySnapshotCaptureError:
            return None
    facts = {
        pair: fact
        for pair, fact in base_snapshot.facts.items()
        if pair in pairs
    }
    captured_costs: dict[tuple[str, str], dict[str, Any]] = {}
    base_raw = {
        (str(item.get("origin_key")), str(item.get("destination_key"))): item
        for item in (base.get("facts") or [])
        if isinstance(item, dict)
    }
    if captured is not None:
        captured_evidence = captured.snapshot.to_evidence()
        for raw_fact in captured_evidence.get("facts", []):
            if (
                str(raw_fact.get("mode") or "").upper() == "WALK"
                and raw_fact.get("fact_quality") == "LIVE_PROVIDER_CAPTURE"
            ):
                raw_fact["cost_status"] = "KNOWN_FREE"
                raw_fact["cost_amount_minor"] = 0
                raw_fact["currency"] = journey.budget_currency
            else:
                raw_fact["cost_status"] = "UNKNOWN"
                raw_fact["cost_amount_minor"] = None
            pair = (str(raw_fact["origin_key"]), str(raw_fact["destination_key"]))
            captured_costs[pair] = {
                "cost_status": raw_fact.get("cost_status"),
                "cost_amount_minor": raw_fact.get("cost_amount_minor"),
                "currency": raw_fact.get("currency"),
            }
            facts[pair] = MobilitySnapshot.from_evidence(
                {"source_provider": captured.snapshot.source_provider_key, "captured_at": captured.snapshot.captured_at, "facts": [raw_fact]}
            ).facts[pair]
    shadow_captured_at = _now()
    shadow = MobilitySnapshot(
        facts=facts,
        source_provider_key=str(captured.snapshot.source_provider_key if captured else base.get("source_provider") or "frozen-plan-evidence"),
        captured_at=shadow_captured_at,
        degraded=bool(captured and captured.snapshot.degraded),
        snapshot_id=str(uuid4()),
        operation_key=f"shadow:{journey.id}:{scenario_key}",
        provider_capabilities=dict(captured.snapshot.provider_capabilities if captured else base.get("provider_capabilities") or {}),
    ).to_evidence()
    for item in shadow.get("facts", []):
        pair = (str(item.get("origin_key")), str(item.get("destination_key")))
        if pair in base_raw and pair not in captured_costs:
            # Preserve the exact provider fact and its cost semantics for reused
            # edges.  A missing cost stays UNKNOWN; zero is never inferred.
            old = base_raw[pair]
            for key in ("cost_status", "cost_amount_minor", "currency"):
                if key in old:
                    item[key] = old[key]
            if (
                str(item.get("mode") or "").upper() == "WALK"
                and item.get("fact_quality") == "LIVE_PROVIDER_CAPTURE"
            ):
                item.update({
                    "cost_status": "KNOWN_FREE",
                    "cost_amount_minor": 0,
                    "currency": journey.budget_currency,
                })
        item.update(captured_costs.get(pair, {}))
    shadow.update({
        "shadow_only": True,
        "base_plan_version_id": active.id,
        "base_snapshot_id": active.mobility_snapshot_id,
        "scenario_key": scenario_key,
        "expires_at": _mobility_expires(shadow_captured_at),
        "expiry_reason": "provider mobility cache TTL",
    })
    return shadow


def _refresh_shadow_plan_mobility(
    session: Session,
    journey: Journey,
    active: PlanVersion,
    shadow: PlanVersion,
    simulation: CounterfactualSimulation,
    changes: list[dict[str, Any]],
    activities: list[Activity],
) -> bool:
    """Refresh a persisted branch without changing its identity or schedule."""
    # Refresh the branch's own immutable activity sequence.  Reusing the
    # active plan here would silently turn RETIME/REPLACE branches back into
    # the current reality and could capture the wrong adjacency set.
    shadow_activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == shadow.id).order_by(Activity.sequence_no)
    ).all()
    evidence = _shadow_evidence(
        session,
        journey,
        active,
        shadow_activities,
        scenario_key=simulation.scenario_key,
        force_refresh=True,
    )
    if evidence is None:
        return False
    shadow.mobility_snapshot_id = str(evidence["snapshot_id"])
    shadow.mobility_evidence_json = _json(evidence)
    explanation = _load(shadow.explanation_json, {})
    explanation.update({"captured_at": evidence.get("captured_at"), "expires_at": evidence.get("expires_at")})
    shadow.explanation_json = _json(explanation)
    fact_by_pair = {
        (str(item.get("origin_key")), str(item.get("destination_key"))): item
        for item in (evidence.get("facts") or [])
        if isinstance(item, dict)
    }
    for left, right, leg in zip(
        shadow_activities,
        shadow_activities[1:],
        session.scalars(
            select(JourneyLeg).where(JourneyLeg.plan_version_id == shadow.id).order_by(JourneyLeg.sequence_no)
        ).all(),
    ):
        fact = fact_by_pair.get((str(left.place_id), str(right.place_id))) or {}
        leg.duration_min = int(fact.get("minutes") or leg.duration_min)
        leg.arrive_at = right.start_at
        leg.captured_at = str(evidence.get("captured_at"))
        leg.expires_at = str(evidence.get("expires_at"))
        leg.provider_key = str(fact.get("provider") or evidence.get("source_provider") or leg.provider_key)
        leg.provenance = str(fact.get("capture_provenance") or fact.get("fact_quality") or leg.provenance)
    gate = validate_plan_feasibility(
        session,
        journey,
        shadow,
        mobility_provider=MobilitySnapshot.from_evidence(evidence),
    )
    return bool(gate.passed)


def _branch_trigger_contract(
    journey: Journey,
    active: PlanVersion,
    simulation: CounterfactualSimulation,
    dropped_activity_ids: list[str],
    dropped_place_ids: list[str],
    activities: list[Activity],
) -> dict[str, Any]:
    contract = contract_for_simulation(
        scenario_type=simulation.scenario_type,
        dropped_activity_ids=dropped_activity_ids,
        dropped_place_ids=dropped_place_ids,
        simulation_id=simulation.id,
        parent_plan_id=active.id,
    )
    # A mobility branch may sacrifice a future visit, but its trigger is the
    # observed delay at the current cursor.  The server therefore scopes the
    # branch to both the affected future suffix and the current activity.
    if simulation.scenario_type == "MOBILITY_SHIFT":
        current = next((item for item in activities if item.id == journey.current_activity_id), None)
        if current is not None:
            contract["affected_activity_ids"] = sorted(
                set(contract.get("affected_activity_ids") or []) | {str(current.id)}
            )
            if current.place_id:
                contract["affected_place_ids"] = sorted(
                    set(contract.get("affected_place_ids") or []) | {str(current.place_id)}
                )
    return contract


def _material_difference_fingerprint(specs: list[dict[str, Any]]) -> str:
    """Fingerprint executable reality, not the scenario label that produced it."""

    payload = [
        {
            "place_id": item["place_id"],
            "activity_type": item["activity_type"],
            "start_at": item["start_at"],
            "end_at": item["end_at"],
        }
        for item in specs
    ]
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:24]


def _shadow_specs(
    session: Session,
    journey: Journey,
    activities: list[Activity],
    changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]] | None:
    assignments = {str(item.get("slot_key")): item for item in changes}
    current_index = next(
        (idx for idx, item in enumerate(activities) if item.id == journey.current_activity_id),
        -1,
    )
    protected_ids = {
        str(item.activity_id)
        for item in session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
                Commitment.level.in_(("C3_Guaranteed", "C4_Anchored")),
            )
        ).all()
        if item.activity_id
    }
    protected_ids.update(
        str(item.id)
        for idx, item in enumerate(activities)
        if item.locked or idx <= current_index
    )
    specs: list[dict[str, Any]] = []
    dropped: set[str] = set()
    replaced_old_places: set[str] = set()
    journey_start = _dt(journey.start_at)
    for source in activities:
        change = assignments.get(str(source.id), {})
        action = str(change.get("action") or "KEEP").upper()
        if action == "DROP":
            if source.id in protected_ids:
                return None
            dropped.add(str(source.id))
            continue
        place_id = str(source.place_id or source.city_service_id or source.id)
        title = source.title
        estimated_cost = int(source.estimated_cost_minor or 0)
        if action == "REPLACE":
            replacement = session.get(Place, str(change.get("location_key") or ""))
            if replacement is None:
                return None
            replaced_old_places.add(place_id)
            place_id = replacement.id
            title = replacement.name
            estimated_cost = int(change.get("cost_minor") or 0)
        start_min = change.get("start_min")
        if start_min is not None and source.id not in protected_ids:
            start_at = journey_start + timedelta(minutes=int(start_min))
        else:
            start_at = _dt(source.start_at)
        duration_min = max(1, int((_dt(source.end_at) - _dt(source.start_at)).total_seconds() // 60))
        specs.append(
            {
                "source": source,
                "place_id": place_id,
                "title": title,
                "activity_type": source.activity_type,
                "start_at": start_at,
                "end_at": start_at + timedelta(minutes=duration_min),
                "estimated_cost_minor": estimated_cost,
                "currency": source.currency,
                "action": action,
            }
        )
    if len(specs) < 2 or not changes:
        return None
    return specs, dropped, replaced_old_places


def _materialize_shadow_plan(
    session: Session,
    journey: Journey,
    active: PlanVersion,
    simulation: CounterfactualSimulation,
    changes: list[dict[str, Any]],
) -> PlanVersion | None:
    """Persist a grounded standby for DROP, RETIME/KEEP, or REPLACE futures."""

    if not simulation.feasible or not simulation.c4_preserved:
        return None
    if simulation.scenario_type not in {"PLACE_CLOSURE", "MOBILITY_SHIFT", "WEATHER_SHIFT"}:
        return None
    evidence_json = _load(simulation.evidence_json, {})
    existing_id = evidence_json.get("shadow_plan_version_id")
    existing = session.get(PlanVersion, existing_id) if existing_id else None
    activities = list(
        session.scalars(
            select(Activity).where(Activity.plan_version_id == active.id).order_by(Activity.sequence_no)
        ).all()
    )
    resolved = _shadow_specs(session, journey, activities, changes)
    if resolved is None:
        return None
    specs, dropped, replaced_old_places = resolved
    fingerprint = _material_difference_fingerprint(specs)
    if existing is not None and existing.lifecycle == "STANDBY":
        existing_explanation = _load(existing.explanation_json, {})
        if existing_explanation.get("material_difference_fingerprint") == fingerprint:
            return existing
        # A pre-R21 branch without an execution fingerprint is not known to be
        # the same future. Retire it instead of refreshing it in place.
        existing.lifecycle = "RETIRED"
        existing.status = "REJECTED"
    for sibling in session.scalars(
        select(PlanVersion).where(
            PlanVersion.journey_id == journey.id,
            PlanVersion.parent_plan_version_id == active.id,
            PlanVersion.lifecycle == "STANDBY",
        )
    ).all():
        sibling_explanation = _load(sibling.explanation_json, {})
        if sibling_explanation.get("material_difference_fingerprint") == fingerprint:
            evidence_json.update(
                {
                    "shadow_plan_version_id": sibling.id,
                    "shadow_plan_status": sibling.status,
                    "shadow_lifecycle": "NON_ACTIVE_STANDBY",
                    "deduplicated_to": sibling.id,
                    "material_difference_fingerprint": fingerprint,
                }
            )
            simulation.evidence_json = _json(evidence_json)
            return sibling
    route_nodes = [
        SimpleNamespace(
            id=str(item["source"].id),
            place_id=item["place_id"],
            city_service_id=None,
        )
        for item in specs
    ]
    evidence = _shadow_evidence(
        session,
        journey,
        active,
        route_nodes,
        scenario_key=simulation.scenario_key,
    )
    if evidence is None:
        evidence_json["shadow_materialization_failure"] = ["LIVE_MOBILITY_CAPTURE_FAILED"]
        simulation.evidence_json = _json(evidence_json)
        return None
    next_version = int(
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    ) + 1
    total_cost = sum(int(item["estimated_cost_minor"]) for item in specs)
    shadow = PlanVersion(
        id=str(uuid4()),
        journey_id=journey.id,
        version_no=next_version,
        planning_mode=active.planning_mode,
        status="CANDIDATE",
        lifecycle="STANDBY",
        trigger_contract_json=_json(
            _branch_trigger_contract(
                journey,
                active,
                simulation,
                sorted(set(dropped) | replaced_old_places),
                sorted(
                    {
                    str(item.place_id)
                        for item in activities
                        if item.id in set(dropped) | replaced_old_places and item.place_id
                    }
                ),
                activities,
            )
        ),
        source="REPLAN",
        parent_plan_version_id=active.id,
        total_cost_minor=total_cost,
        currency=active.currency,
        estimated_duration_min=max(
            1,
            int((specs[-1]["end_at"] - specs[0]["start_at"]).total_seconds() // 60),
        ),
        utility_score=active.utility_score,
        score_breakdown_json=active.score_breakdown_json,
        explanation_json=_json(
            {
                "product_path": "R21_AUTONOMOUS_FUTURE_SYNTHESIS",
                "lifecycle": "STANDBY",
                "trigger": simulation.scenario_key,
                "simulation_id": simulation.id,
                "base_plan_version_id": active.id,
                "captured_at": evidence.get("captured_at"),
                "expires_at": evidence.get("expires_at"),
                "protected_commitments_preserved": simulation.c4_preserved,
                "material_difference_fingerprint": fingerprint,
                "material_difference": {
                    "dropped_activity_ids": sorted(dropped),
                    "replaced_old_place_ids": sorted(replaced_old_places),
                    "activity_count": len(specs),
                },
            }
        ),
        mobility_snapshot_id=str(evidence["snapshot_id"]),
        mobility_evidence_json=_json(evidence),
        created_at=_now(),
    )
    session.add(shadow)
    session.flush()
    fact_by_pair = {
        (str(item.get("origin_key")), str(item.get("destination_key"))): item
        for item in (evidence.get("facts") or [])
    }
    shadow_activities: list[Activity] = []
    for index, spec in enumerate(specs, start=1):
        source = spec["source"]
        metadata = _load(source.metadata_json, {})
        metadata["shadow_plan"] = {
            "scenario_key": simulation.scenario_key,
            "base_activity_id": source.id,
            "lifecycle": "STANDBY",
            "material_difference_fingerprint": fingerprint,
        }
        if index == 1:
            travel_min = 0
        else:
            previous = specs[index - 2]
            pair = (str(previous["place_id"]), str(spec["place_id"]))
            travel_min = int((fact_by_pair.get(pair) or {}).get("minutes") or 0)
        activity = Activity(
            id=str(uuid4()),
            plan_version_id=shadow.id,
            sequence_no=index,
            activity_type=source.activity_type,
            title=spec["title"],
            place_id=spec["place_id"] if source.place_id else None,
            city_service_id=None if source.place_id else source.city_service_id,
            start_at=spec["start_at"].isoformat(),
            end_at=spec["end_at"].isoformat(),
            travel_from_prev_min=travel_min,
            estimated_cost_minor=spec["estimated_cost_minor"],
            currency=spec["currency"],
            # The execution prefix and locked activities are copied as their
            # current state; future mutable activities stay PLANNED.
            status=(
                source.status
                if source.locked or source.status in {"COMPLETED", "IN_PROGRESS"}
                else "PLANNED"
            ),
            locked=source.locked,
            metadata_json=_json(metadata),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(activity)
        shadow_activities.append(activity)
    session.flush()
    for index, (left, right) in enumerate(zip(shadow_activities, shadow_activities[1:]), start=1):
        pair = (str(left.place_id or left.city_service_id or left.id), str(right.place_id or right.city_service_id or right.id))
        fact = fact_by_pair.get(pair) or {}
        session.add(
            JourneyLeg(
                id=str(uuid4()), journey_id=journey.id, plan_version_id=shadow.id,
                sequence_no=index, from_activity_id=left.id, to_activity_id=right.id,
                from_key=pair[0], to_key=pair[1], mode=str(fact.get("mode") or "WALK"),
                depart_at=left.end_at, arrive_at=right.start_at,
                duration_min=int(fact.get("minutes") or right.travel_from_prev_min or 0),
                distance_m=int(fact.get("distance_m") or 0),
                cost_amount_minor=fact.get("cost_amount_minor"),
                currency=fact.get("currency"), cost_status=str(fact.get("cost_status") or "UNKNOWN"),
                walking_min=int(fact.get("minutes") or right.travel_from_prev_min or 0),
                transfers=int(fact.get("transfers") or 0), reliability_score=float(fact.get("reliability_score") or 0),
                weather_exposure="UNKNOWN", geometry_json=_json(fact.get("geometry") or {}),
                crs="WGS84", provider_key=str(fact.get("provider") or evidence.get("source_provider") or "unknown"),
                provenance=str(fact.get("capture_provenance") or fact.get("fact_quality") or "UNKNOWN"),
                captured_at=str(evidence.get("captured_at")), expires_at=str(evidence.get("expires_at")),
            )
        )
    session.flush()
    gate = validate_plan_feasibility(
        session,
        journey,
        shadow,
        mobility_provider=MobilitySnapshot.from_evidence(evidence),
    )
    if not gate.passed:
        diagnostic = _load(simulation.evidence_json, {})
        diagnostic["shadow_materialization_failure"] = list(gate.failures)
        simulation.evidence_json = _json(diagnostic)
        session.delete(shadow)
        session.flush()
        return None
    return shadow


def simulate_counterfactuals(
    session: Session,
    journey: Journey,
    snapshot: JourneyResilienceSnapshot,
) -> list[CounterfactualSimulation]:
    components = _load(snapshot.components_json, {})
    mobility_rows = [
        item
        for item in _latest_observations(session, journey.id)
        if item.observation_type == "MOBILITY"
    ]
    mobility_payload = _load(mobility_rows[0].payload_json if mobility_rows else None, {})
    observed_deltas = sorted(
        {
            max(0, int(item.get("delta_minutes", 0)))
            for item in (mobility_payload.get("meaningful_deltas") or [])
            if isinstance(item, dict)
        }
    )
    if observed_deltas:
        p50_delay = max(10, observed_deltas[len(observed_deltas) // 2])
        p80_delay = max(p50_delay, observed_deltas[-1])
        p95_delay = max(p80_delay, round(p80_delay * 1.25))
    else:
        p50_delay, p80_delay, p95_delay = 10, 20, 30
    forecast_risk = int(components.get("forecast_risk", 0) or 0)
    weather_horizon = max(
        15,
        int(
            max(
                [
                    int(item.get("forecast_horizon_min", 0))
                    for item in _load(snapshot.provenance_json, [])
                    if isinstance(item, dict)
                ]
                or [45]
            )
        ),
    )
    scenario_uncertainty = {
        "method": "BOUNDED_SCENARIO_ENSEMBLE",
        "probabilities": "NOT_PROVIDED",
        "default_weight_label": "HEURISTIC",
        "provider_facts": {
            "forecast_risk_score": forecast_risk,
            "observed_mobility_deltas_min": observed_deltas,
        },
    }
    scenarios = (
        (
            "rain_earlier",
            "WEATHER_SHIFT",
            {
                "rain_earlier_min": weather_horizon,
                "precipitation_severity_band": "HIGH",
                "uncertainty_label": "HEURISTIC",
            },
        ),
        (
            "route_plus_10",
            "MOBILITY_SHIFT",
            {
                "route_delay_min": p50_delay,
                "delay_band": "P50",
                "uncertainty_label": "HEURISTIC",
            },
        ),
        (
            "route_plus_20",
            "MOBILITY_SHIFT",
            {
                "route_delay_min": p80_delay,
                "delay_band": "P80",
                "uncertainty_label": "HEURISTIC",
            },
        ),
        (
            "route_plus_30",
            "MOBILITY_SHIFT",
            {
                "route_delay_min": p95_delay,
                "delay_band": "P95",
                "uncertainty_label": "HEURISTIC",
            },
        ),
        (
            "mutable_place_closes",
            "PLACE_CLOSURE",
            {"mutable_place_closes": True, "uncertainty_label": "HEURISTIC"},
        ),
        (
            "replacement_disappears",
            "OPTIONALITY_LOSS",
            {"replacement_disappears": True, "uncertainty_label": "HEURISTIC"},
        ),
    )
    result: list[CounterfactualSimulation] = []
    active_plan = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    # R20 keeps a deliberately small portfolio: enough distinct futures for
    # arbitration, but never an unbounded shadow-plan factory.  Materialize at
    # most one branch per scenario row until three grounded branches exist.
    materialized_shadow_count = 0
    for scenario_key, scenario_type, parameters in scenarios:
        problem, activities, anchors = _scenario_problem(
            session,
            journey,
            scenario_type=scenario_key,
            parameters=parameters,
        )
        plans = solve_v2(problem) if problem is not None else ()
        plan = plans[0] if plans else None
        changes = [
            {
                "slot_key": assignment.slot_key,
                "action": assignment.action,
                "location_key": assignment.location_key,
                "start_min": assignment.start_min,
                "end_min": assignment.end_min,
                "schedule_drift_min": assignment.schedule_drift_min,
                "cost_minor": assignment.cost_minor,
            }
            for assignment in (plan.assignments if plan else ())
            if assignment.action != "KEEP" or assignment.schedule_drift_min
        ]
        evidence = {
            "engine": "JourneyTwin + CP-SAT V2",
            "solver_kind": plan.generator_kind if plan else "NO_FEASIBLE_PLAN",
            "solver_status": "FEASIBLE" if plan else "INFEASIBLE",
            "problem_fingerprint": solver_v2_problem_fingerprint(problem) if problem else None,
            "bounded": True,
            "domain_truth_mutated": False,
            "activity_count": len(activities),
            "protected_anchor_count": len(anchors),
            "uncertainty": {
                **scenario_uncertainty,
                "scenario_label": parameters.get("uncertainty_label", "HEURISTIC"),
                "scenario_parameters": parameters,
            },
            "expected_failure_risk": round(float(snapshot.score) / 100, 3),
            "expected_switching_cost_minor": max(0, int(plan.cost_delta_minor if plan else 0)),
            "schedule_disruption_penalty_min": max(
                0, int(max([item["schedule_drift_min"] for item in changes] or [0]))
            ),
            "preventive_value": {
                "risk_reduction_value": round(float(snapshot.score) / 100, 3),
                "switching_cost_minor": max(0, int(plan.cost_delta_minor if plan else 0)),
                "schedule_disruption_penalty_min": max(
                    0, int(max([item["schedule_drift_min"] for item in changes] or [0]))
                ),
                "decision": "INSPECTABLE_INPUTS_ONLY",
            },
        }
        row = session.scalar(
            select(CounterfactualSimulation).where(
                CounterfactualSimulation.journey_id == journey.id,
                CounterfactualSimulation.scenario_key == scenario_key,
            )
        )
        if row is None:
            row = CounterfactualSimulation(
                id=str(uuid4()),
                journey_id=journey.id,
                scenario_key=scenario_key,
                scenario_type=scenario_type,
                created_at=_now(),
            )
            session.add(row)
        row.snapshot_id = snapshot.id
        row.parameters_json = _json(parameters)
        row.feasible = bool(plan)
        row.c4_preserved = bool(plan and len(plan.protected_anchors) == len(anchors))
        row.cost_delta_minor = int(plan.cost_delta_minor if plan else 0)
        row.schedule_drift_min = max([int(item["schedule_drift_min"]) for item in changes] or [0])
        row.plan_changes_json = _json(changes)
        previous_evidence = _load(row.evidence_json, {})
        existing_shadow_id = previous_evidence.get("shadow_plan_version_id")
        existing_shadow = (
            session.get(PlanVersion, str(existing_shadow_id))
            if existing_shadow_id
            else None
        )
        expected_material_fingerprint = None
        if active_plan is not None and changes:
            expected_specs = _shadow_specs(session, journey, activities, changes)
            if expected_specs is not None:
                expected_material_fingerprint = _material_difference_fingerprint(
                    expected_specs[0]
                )
        existing_material_fingerprint = (
            _load(existing_shadow.explanation_json, {}).get(
                "material_difference_fingerprint"
            )
            if existing_shadow is not None
            else None
        )
        if (
            active_plan is not None
            and
            existing_shadow is not None
            and existing_shadow.lifecycle == "STANDBY"
            and existing_shadow.parent_plan_version_id == active_plan.id
            and expected_material_fingerprint is not None
            and existing_material_fingerprint == expected_material_fingerprint
        ):
            # Refreshes upgrade the persisted contract in place.  This keeps
            # R19 branches single-sourced while making the R20 matcher apply
            # the current provenance semantics without duplicating branches.
            dropped = {
                str(item["slot_key"])
                for item in changes
                if item.get("action") == "DROP"
            }
            refreshed = _refresh_shadow_plan_mobility(
                session, journey, active_plan, existing_shadow, row, changes, activities
            )
            existing_shadow.trigger_contract_json = _json(_branch_trigger_contract(
                journey, active_plan, row, sorted(dropped),
                [str(item.place_id) for item in activities if item.id in dropped and item.place_id],
                activities,
            ))
            if refreshed:
                evidence["shadow_plan_version_id"] = existing_shadow.id
                evidence["shadow_plan_status"] = existing_shadow.status
                evidence["shadow_lifecycle"] = "NON_ACTIVE_STANDBY"
                evidence["mobility_refresh"] = "LIVE_PROVIDER_CAPTURE"
                evidence["material_difference_fingerprint"] = existing_material_fingerprint
                evidence["material_difference"] = _load(
                    existing_shadow.explanation_json, {}
                ).get("material_difference", {})
                row.evidence_json = _json(evidence)
                materialized_shadow_count += 1
            else:
                # A branch whose current/future suffix no longer matches the
                # active execution cursor is not refreshable in place. Retire
                # that immutable branch and materialize a replacement from the
                # current reality instead of keeping an expired READY-looking
                # option in the Agent portfolio.
                existing_shadow.lifecycle = "RETIRED"
                existing_shadow.status = "REJECTED"
                evidence.pop("shadow_plan_version_id", None)
                evidence["superseded_shadow_plan_version_id"] = existing_shadow.id
                evidence["shadow_lifecycle"] = "RETIRED"
                evidence["mobility_refresh"] = "REPLACEMENT_REQUIRED"
                replacement = (
                    _materialize_shadow_plan(session, journey, active_plan, row, changes)
                    if materialized_shadow_count < 3 and changes
                    else None
                )
                if replacement is not None:
                    evidence["shadow_plan_version_id"] = replacement.id
                    evidence["shadow_plan_status"] = replacement.status
                    evidence["shadow_lifecycle"] = "NON_ACTIVE_STANDBY"
                    explanation = _load(replacement.explanation_json, {})
                    evidence["material_difference_fingerprint"] = explanation.get(
                        "material_difference_fingerprint"
                    )
                    evidence["material_difference"] = explanation.get(
                        "material_difference", {}
                    )
                    materialized_shadow_count += 1
                row.evidence_json = _json(evidence)
        elif active_plan is not None and materialized_shadow_count < 3 and changes:
            shadow = _materialize_shadow_plan(session, journey, active_plan, row, changes)
            if shadow is not None:
                evidence["shadow_plan_version_id"] = shadow.id
                evidence["shadow_plan_status"] = shadow.status
                evidence["shadow_lifecycle"] = "NON_ACTIVE_STANDBY"
                explanation = _load(shadow.explanation_json, {})
                evidence["material_difference_fingerprint"] = explanation.get(
                    "material_difference_fingerprint"
                )
                evidence["material_difference"] = explanation.get("material_difference", {})
                row.evidence_json = _json(evidence)
                materialized_shadow_count += 1
        else:
            row.evidence_json = _json(evidence)
        row.created_at = _now()
        result.append(row)
    session.flush()
    return result
