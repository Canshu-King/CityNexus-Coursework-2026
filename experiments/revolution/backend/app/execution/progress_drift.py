"""Server-owned progress and downstream slack from confirmed execution.

This module only reads Journey truth and derives a bounded projection. It never
changes the plan or chooses a recovery strategy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    Commitment,
    Journey,
    JourneyLeg,
    RealityConflict,
    TransportReservation,
)
from app.execution.clock import execution_clock


_AUTHORITATIVE_EXECUTION = {
    "OPERATOR_CONFIRMED_EXECUTION",
    "DEVICE_LOCATION_VERIFIED_EXECUTION",
}
_ROUTE_PROVENANCE = {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
_TERMINAL = {"COMPLETED", "SKIPPED", "CANCELLED"}


@dataclass(frozen=True)
class ProgressDrift:
    drift_min: int
    planned_at: str
    actual_at: str
    truth_kind: str
    authoritative: bool


@dataclass(frozen=True)
class ProgressRiskDecision:
    state: str
    material: bool
    reason: str
    remaining_slack_min: int | None


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("DRIFT_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return parsed.astimezone(timezone.utc)


def _minutes(value: datetime) -> int:
    return int(value.total_seconds() // 60)


def drift_from_milestone(
    *, planned_at: str, actual_at: str, truth_kind: str
) -> ProgressDrift:
    planned = _aware(planned_at)
    actual = _aware(actual_at)
    kind = str(truth_kind or "").upper()
    return ProgressDrift(
        drift_min=_minutes(actual - planned),
        planned_at=planned.isoformat(),
        actual_at=actual.isoformat(),
        truth_kind=kind,
        authoritative=kind in _AUTHORITATIVE_EXECUTION,
    )


def classify_progress_risk(
    *,
    drift: ProgressDrift,
    protected_commitment: bool,
    slack_before_min: int | None,
    safety_buffer_min: int = 5,
    route_grounded: bool = True,
    commitment_contested: bool = False,
) -> ProgressRiskDecision:
    """Classify grounded execution drift against actual remaining plan slack."""
    if not drift.authoritative:
        return ProgressRiskDecision("UNKNOWN_PROGRESS", False, "EXECUTION_MILESTONE_NOT_AUTHORITATIVE", None)
    if commitment_contested:
        return ProgressRiskDecision("REALITY_CONTESTED", False, "DOWNSTREAM_COMMITMENT_TRUTH_CONTESTED", None)
    if not route_grounded or slack_before_min is None:
        return ProgressRiskDecision("ROUTE_UNKNOWN", False, "DOWNSTREAM_SLACK_NOT_GROUNDED", None)

    if drift.drift_min < 0:
        return ProgressRiskDecision("AHEAD", False, "EXECUTION_AHEAD_OF_PLAN", int(slack_before_min))
    if drift.drift_min == 0:
        return ProgressRiskDecision("ON_SCHEDULE", False, "EXECUTION_MATCHES_PLAN", int(slack_before_min))

    remaining = int(slack_before_min) - drift.drift_min
    if protected_commitment and remaining < max(0, int(safety_buffer_min)):
        return ProgressRiskDecision("COMMITMENT_AT_RISK", True, "PROTECTED_COMMITMENT_SLACK_BELOW_SAFETY_BUFFER", remaining)
    if remaining < 0:
        return ProgressRiskDecision("RECOVERABLE_DRIFT", True, "NEXT_SCHEDULE_WINDOW_NO_LONGER_FITS", remaining)
    if remaining < max(0, int(safety_buffer_min)) * 2:
        return ProgressRiskDecision("RECOVERABLE_DRIFT", False, "LOW_REMAINING_SLACK", remaining)
    return ProgressRiskDecision("MINOR_DRIFT", False, "DRIFT_ABSORBED_BY_REMAINING_SLACK", remaining)


def execution_drift_fingerprint(
    *, journey_id: str, activity_id: str, milestone: str, actual_at: str,
    affected_target_id: str | None,
) -> str:
    payload = {
        "journey_id": str(journey_id),
        "activity_id": str(activity_id),
        "milestone": str(milestone).upper(),
        "actual_at": _aware(actual_at).isoformat(),
        "affected_target_id": str(affected_target_id or ""),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _execution_milestone(activity: Activity) -> dict[str, str] | None:
    try:
        metadata = json.loads(activity.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    history = metadata.get("execution_truth_history")
    if not isinstance(history, list):
        current = metadata.get("execution_truth")
        history = [current] if isinstance(current, dict) else []
    candidates: list[dict[str, str]] = []
    plan_keys = {
        "EN_ROUTE": ("actual_departed_at", "en_route_at"),
        "ARRIVED": ("actual_arrived_at", "arrived_at"),
        "IN_PROGRESS": ("actual_started_at", "started_at_actual"),
        "COMPLETED": ("completed_at_actual",),
    }
    for truth in history:
        if not isinstance(truth, dict) or str(truth.get("kind") or "").upper() not in _AUTHORITATIVE_EXECUTION:
            continue
        transition = str(truth.get("transition") or "").upper()
        actual_at = truth.get("captured_at")
        try:
            metadata_key = {
                "EN_ROUTE": "actual_departed_at",
                "ARRIVED": "actual_arrived_at",
                "IN_PROGRESS": "actual_started_at",
                "COMPLETED": "completed_at_actual",
            }.get(transition)
            if metadata_key:
                planned_at = metadata.get(metadata_key) or actual_at
            else:
                continue
            if transition == "COMPLETED":
                planned_at = activity.end_at
            elif transition in {"IN_PROGRESS", "ARRIVED"}:
                planned_at = activity.start_at
            elif transition == "EN_ROUTE":
                planned_at = activity.start_at
            if not actual_at or not planned_at:
                continue
            _aware(str(actual_at))
            _aware(str(planned_at))
            candidates.append({
                "milestone": transition,
                "actual_at": str(actual_at),
                "planned_at": str(planned_at),
                "truth_kind": str(truth["kind"]).upper(),
            })
        except (ValueError, TypeError):
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: _aware(item["actual_at"]))


def _commitment_safety_buffer(commitment: Commitment) -> int:
    try:
        policy = json.loads(commitment.policy_json or "{}")
    except (TypeError, json.JSONDecodeError):
        policy = {}
    for key in ("safety_buffer_min", "required_buffer_min", "resilience_buffer_min"):
        if policy.get(key) is not None:
            try:
                return max(0, int(policy[key]))
            except (ValueError, TypeError):
                pass
    return 5


def _commitment_time_contested(
    session: Session, journey_id: str, commitment: Commitment
) -> bool:
    reservations = session.scalars(
        select(TransportReservation).where(
            TransportReservation.journey_id == journey_id,
            TransportReservation.commitment_id == commitment.id,
        )
    ).all()
    if not reservations:
        return False
    keys = {f"reservation:{row.id}:scheduled_departure_at" for row in reservations}
    return session.scalar(
        select(RealityConflict.id).where(
            RealityConflict.journey_id == journey_id,
            RealityConflict.status == "UNRESOLVED_MATERIAL_CONFLICT",
            RealityConflict.fact_domain == "RESERVATION_TIME",
            RealityConflict.fact_key.in_(keys),
        ).limit(1)
    ) is not None


def _grounded_path(
    session: Session, journey: Journey, activities: list[Activity], source_index: int,
    target_index: int,
) -> tuple[list[dict[str, Any]], int] | None:
    if target_index <= source_index:
        return None
    legs = session.scalars(
        select(JourneyLeg).where(
            JourneyLeg.journey_id == journey.id,
            JourneyLeg.plan_version_id == journey.active_plan_version_id,
            JourneyLeg.sequence_no >= activities[source_index].sequence_no,
            JourneyLeg.sequence_no < activities[target_index].sequence_no,
        ).order_by(JourneyLeg.sequence_no)
    ).all()
    expected = target_index - source_index
    if len(legs) != expected:
        return None
    evidence: list[dict[str, Any]] = []
    duration_min = 0
    for index, leg in enumerate(legs, start=source_index):
        if (
            leg.from_activity_id != activities[index].id
            or leg.to_activity_id != activities[index + 1].id
            or not leg.provider_snapshot_id
            or str(leg.provenance or "").upper() not in _ROUTE_PROVENANCE
        ):
            return None
        try:
            duration = max(0, int(leg.duration_min))
            _aware(leg.depart_at)
            _aware(leg.arrive_at)
        except (ValueError, TypeError):
            return None
        duration_min += duration
        evidence.append({
            "from_activity_id": leg.from_activity_id,
            "to_activity_id": leg.to_activity_id,
            "duration_min": duration,
            "provenance": leg.provenance,
            "provider_snapshot_id": leg.provider_snapshot_id,
            "assumption": "FROZEN_PROVIDER_MOBILITY",
        })
    intervening_min = 0
    for row in activities[source_index + 1 : target_index]:
        try:
            intervening_min += max(0, _minutes(_aware(row.end_at) - _aware(row.start_at)))
        except (ValueError, TypeError):
            return None
    return evidence, duration_min + intervening_min


def progress_drift_projection(session: Session, journey: Journey) -> dict[str, Any]:
    """Read-only server projection of confirmed progress and protected slack."""
    if not journey.active_plan_version_id:
        return {"state": "UNKNOWN_PROGRESS", "headline": "I’m waiting for a confirmed trip update.", "drift_min": None, "basis": None, "next_risk": None, "read_only": True}
    activities = list(session.scalars(
        select(Activity).where(Activity.plan_version_id == journey.active_plan_version_id)
        .order_by(Activity.sequence_no)
    ).all())
    milestones: list[tuple[int, int, Activity, dict[str, str]]] = []
    for index, activity in enumerate(activities):
        milestone = _execution_milestone(activity)
        if milestone:
            try:
                milestones.append((int(_aware(milestone["actual_at"]).timestamp() * 1_000_000), index, activity, milestone))
            except ValueError:
                continue
    if not milestones:
        return {"state": "UNKNOWN_PROGRESS", "headline": "I’m waiting for a confirmed trip update.", "drift_min": None, "basis": None, "next_risk": None, "read_only": True}
    _, source_index, source_activity, milestone = max(milestones, key=lambda item: (item[0], item[1]))
    drift = drift_from_milestone(
        planned_at=milestone["planned_at"], actual_at=milestone["actual_at"],
        truth_kind=milestone["truth_kind"],
    )
    basis = {
        "activity_id": source_activity.id,
        "activity_title": source_activity.title,
        "milestone": milestone["milestone"],
        "planned_at": drift.planned_at,
        "actual_at": drift.actual_at,
        "truth_kind": drift.truth_kind,
    }
    if not drift.authoritative:
        return {"state": "UNKNOWN_PROGRESS", "headline": "I’m waiting for a confirmed trip update.", "drift_min": None, "basis": basis, "next_risk": None, "read_only": True}
    if milestone["milestone"] != "COMPLETED":
        if milestone["milestone"] == "IN_PROGRESS":
            try:
                running_long = execution_clock.now().astimezone(timezone.utc) > _aware(source_activity.end_at)
            except ValueError:
                running_long = False
            state = "RUNNING_LONG" if running_long else ("MINOR_DRIFT" if drift.drift_min > 0 else "ON_SCHEDULE" if drift.drift_min == 0 else "AHEAD")
            headline = (
                "This activity is running past its planned end; I’ll measure the downstream effect when you finish."
                if running_long
                else f"You started {drift.drift_min} minutes later than planned; I’ll confirm the downstream effect when you finish."
                if drift.drift_min > 0
                else "This activity started ahead of plan."
                if drift.drift_min < 0
                else "This activity started on schedule."
            )
            return {"state": state, "drift_min": drift.drift_min, "basis": basis, "next_risk": None, "material": False, "headline": headline, "read_only": True}
        return {"state": "MINOR_DRIFT" if drift.drift_min > 0 else "AHEAD" if drift.drift_min < 0 else "ON_SCHEDULE", "drift_min": drift.drift_min, "basis": basis, "next_risk": None, "material": False, "headline": "The latest confirmed milestone is recorded; I’ll judge the rest of the Journey from the next execution update.", "read_only": True}

    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level.in_(("C3_Guaranteed", "C4_Anchored")),
        )
    ).all()
    indices = {str(activity.id): index for index, activity in enumerate(activities)}
    candidates: list[tuple[int, dict[str, Any], ProgressRiskDecision]] = []
    for commitment in commitments:
        target_index = indices.get(str(commitment.activity_id or ""))
        if target_index is None or target_index <= source_index:
            continue
        target = activities[target_index]
        contested = _commitment_time_contested(session, journey.id, commitment)
        if contested:
            decision = classify_progress_risk(
                drift=drift, protected_commitment=True, slack_before_min=None,
                safety_buffer_min=_commitment_safety_buffer(commitment),
                commitment_contested=True,
            )
            row = {"commitment_id": commitment.id, "activity_id": target.id,
                   "activity_title": target.title, "start_at": target.start_at,
                   "slack_before_min": None, "slack_after_min": None,
                   "safety_buffer_min": _commitment_safety_buffer(commitment),
                   "route": [], "assumptions": [], "truth_contested": True}
            candidates.append((target_index, row, decision))
            continue
        route = _grounded_path(session, journey, activities, source_index, target_index)
        if route is None:
            decision = classify_progress_risk(
                drift=drift, protected_commitment=True, slack_before_min=None,
                safety_buffer_min=_commitment_safety_buffer(commitment), route_grounded=False,
            )
            row = {"commitment_id": commitment.id, "activity_id": target.id,
                   "activity_title": target.title, "start_at": target.start_at,
                   "slack_before_min": None, "route": [], "assumptions": ["PLAN_ACTIVITY_DURATIONS"]}
            candidates.append((target_index, row, decision))
            continue
        legs, route_and_stops_min = route
        try:
            slack_before = _minutes(_aware(target.start_at) - _aware(source_activity.end_at)) - route_and_stops_min
        except (ValueError, TypeError):
            slack_before = None
        decision = classify_progress_risk(
            drift=drift, protected_commitment=True, slack_before_min=slack_before,
            safety_buffer_min=_commitment_safety_buffer(commitment),
            route_grounded=slack_before is not None, commitment_contested=contested,
        )
        row = {
            "commitment_id": commitment.id, "activity_id": target.id,
            "activity_title": target.title, "start_at": target.start_at,
            "slack_before_min": slack_before,
            "slack_after_min": decision.remaining_slack_min,
            "safety_buffer_min": _commitment_safety_buffer(commitment),
            "route": legs,
            "assumptions": ["UNEXECUTED_ACTIVITY_DURATIONS", "SCHEDULE_GAPS"],
            "truth_contested": contested,
        }
        candidates.append((target_index, row, decision))

    if not candidates:
        if drift.drift_min < 0:
            state = "AHEAD"
        elif drift.drift_min > 0:
            state = "MINOR_DRIFT"
        else:
            state = "ON_SCHEDULE"
        return {
            "state": state, "drift_min": drift.drift_min,
            "headline": (f"You’re {abs(drift.drift_min)} minutes ahead of plan." if drift.drift_min < 0 else f"You’re {drift.drift_min} minutes behind the original plan." if drift.drift_min > 0 else "You’re on schedule."),
            "basis": basis, "next_risk": None, "read_only": True,
        }

    _, next_risk, decision = min(candidates, key=lambda item: (item[0], item[1]["commitment_id"]))
    state = decision.state
    if state == "COMMITMENT_AT_RISK":
        headline = f"You’re {drift.drift_min} minutes behind. {next_risk['activity_title']} is now at risk."
    elif state == "REALITY_CONTESTED":
        headline = "A protected commitment time is still being confirmed, so I’m holding off on downstream timing."
    elif state == "ROUTE_UNKNOWN":
        headline = "I can confirm the delay, but I need a grounded route before judging the next protected stop."
    elif state == "AHEAD":
        headline = f"You’re {abs(drift.drift_min)} minutes ahead of plan. The protected stop remains safe."
    elif state in {"MINOR_DRIFT", "RECOVERABLE_DRIFT"}:
        headline = f"You’re {drift.drift_min} minutes behind, but {next_risk['activity_title']} is still reachable."
    elif state == "ON_SCHEDULE":
        headline = f"You’re on schedule for {next_risk['activity_title']}."
    else:
        headline = "I’m waiting for a confirmed trip update."
    return {
        "state": state,
        "drift_min": drift.drift_min,
        "basis": basis,
        "next_risk": next_risk,
        "reason": decision.reason,
        "material": decision.material,
        "headline": headline,
        "read_only": True,
    }
