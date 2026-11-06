from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Activity,
    Commitment,
    CounterfactualSimulation,
    Event,
    Journey,
    JourneyResilienceSnapshot,
    Place,
    PlanVersion,
    ProviderObservation,
    RealPlaceCandidate,
)
from app.execution.traveler_twin import planning_preference_items
from app.integrations.discovery.real_city import normalize_opening_hours
from app.planning.solver_v2 import (
    SolverV2Anchor,
    SolverV2Option,
    SolverV2Problem,
    SolverV2Slot,
    solve_v2,
    solver_v2_problem_fingerprint,
)
from app.replanning.candidate_evidence import verified_interest_preference_fit


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
    for commitment in commitments:
        activity = next((item for item in activities if item.id == commitment.activity_id), None)
        if activity is None:
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
    mobility = _load(latest_mobility.payload_json if latest_mobility else None, {})
    current_minutes = mobility.get("current_minutes") or {}
    travel: dict[tuple[str, str], int] = {}
    for origin in all_locations:
        for destination in all_locations:
            if origin == destination:
                travel[(origin, destination)] = 0
            else:
                travel[(origin, destination)] = (
                    int(current_minutes.get(f"{origin}|{destination}", 15)) + scenario_delta
                )
    slots: list[SolverV2Slot] = []
    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)
    for activity in activities:
        place = places.get(activity.place_id) if activity.place_id else None
        original_start = max(0, int((_dt(activity.start_at) - journey_start).total_seconds() // 60))
        original_end = min(
            journey_end_min, int((_dt(activity.end_at) - journey_start).total_seconds() // 60)
        )
        duration = max(1, original_end - original_start)
        tags = set(_load(place.tags_json if place else "[]", []))
        outdoor = bool(tags.intersection({"outdoor", "scenic", "waterfront"}))
        options: list[SolverV2Option] = []
        if scenario_type != "mutable_place_closes" or not outdoor:
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
        for candidate in candidate_places[:3]:
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
            candidate_cost = (
                int(cost_evidence.get("amount_minor"))
                if evidence_status in {"KNOWN", "ESTIMATED"}
                and cost_evidence.get("amount_minor") is not None
                else 0
            )
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
        if options:
            slots.append(
                SolverV2Slot(
                    slot_key=activity.id,
                    sequence_no=int(activity.sequence_no),
                    original_location_key=_location(activity),
                    original_start_min=original_start,
                    original_end_min=original_end,
                    original_cost_minor=activity.estimated_cost_minor,
                    options=tuple(options),
                    must_change=(scenario_type == "mutable_place_closes" and outdoor),
                    allow_drop=not any(
                        anchor.location_key == _location(activity) for anchor in anchors
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
        safety_buffer_min=15,
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
                "schedule_drift_min": assignment.schedule_drift_min,
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
            )
            session.add(row)
        row.snapshot_id = snapshot.id
        row.parameters_json = _json(parameters)
        row.feasible = bool(plan)
        row.c4_preserved = bool(plan and len(plan.protected_anchors) == len(anchors))
        row.cost_delta_minor = int(plan.cost_delta_minor if plan else 0)
        row.schedule_drift_min = max([int(item["schedule_drift_min"]) for item in changes] or [0])
        row.plan_changes_json = _json(changes)
        row.evidence_json = _json(evidence)
        row.created_at = _now()
        result.append(row)
    session.flush()
    return result
