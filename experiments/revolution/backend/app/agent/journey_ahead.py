"""Journey Ahead product projection over predictive resilience evidence.

This module does not create incidents or authority. It only classifies a
future-risk horizon into a traveller-facing preventive Operator state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select

from app.db.models import Activity, Journey, JourneyResilienceSnapshot
from app.execution.clock import execution_clock


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def actionable_future_risks(
    activity_risks: Iterable[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return grounded, still-future risks ordered by urgency then severity."""

    rows: list[dict[str, Any]] = []
    stamp = now.astimezone(timezone.utc)
    for item in activity_risks:
        start = _aware(item.get("start_at"))
        if start is None or start <= stamp:
            continue
        score = int(item.get("risk_score") or 0)
        grounded = bool(
            item.get("grounded") is True
            or str(item.get("authority") or "").upper() in {
                "PROVIDER_GROUNDED",
                "FROZEN_PROVIDER_EVIDENCE",
                "SYSTEM_DERIVED_PROVIDER_FACT",
            }
        )
        lead_minutes = int((start - stamp).total_seconds() // 60)
        rows.append(
            {
                **item,
                "risk_score": score,
                "lead_time_minutes": lead_minutes,
                "grounded": grounded,
            }
        )
    for item in rows:
        protected = 1 if item.get("protected_commitment") or item.get("protected") else 0
        slack = max(0, int(item.get("route_slack_minutes") or item.get("buffer_min") or 0))
        booking = 1 if item.get("booking_exposure") or item.get("reservation_id") else 0
        # Actionability is deliberately part of the ordering: a nearer, protected
        # and feasible risk should outrank a distant higher score.
        item["actionability_score"] = max(
            0,
            min(100, (100 - min(100, item["lead_time_minutes"] // 3)) + protected * 18 + booking * 12 - min(30, slack // 5)),
        )
    rows.sort(key=lambda item: (-item["actionability_score"], item["lead_time_minutes"], -item["risk_score"]))
    return rows


def journey_ahead_state(
    *,
    activity_risks: Iterable[dict[str, Any]],
    now: datetime,
    prevention_status: str | None = None,
    provider_degraded: bool = False,
    brain_blocked: bool = False,
    journey_completed: bool = False,
) -> dict[str, Any]:
    """Classify the future horizon without asserting a disruption has happened."""

    if journey_completed:
        return {
            "state": "CLEAR",
            "headline": "Journey complete.",
            "top_risk": None,
            "recommended_action": "NONE",
        }

    rows = actionable_future_risks(activity_risks, now=now)
    top = rows[0] if rows else None

    if provider_degraded:
        return {
            "state": "PROVIDER_DEGRADED",
            "headline": "I can’t fully check the rest of the journey right now.",
            "top_risk": top,
            "recommended_action": "KEEP_WATCHING",
        }
    if brain_blocked and top is not None:
        return {
            "state": "FUTURE_RISK_UNCERTAIN",
            "headline": "I found a future risk, but my planning AI is unavailable.",
            "top_risk": top,
            "recommended_action": "WAIT_FOR_BRAIN",
        }

    prevention = str(prevention_status or "").upper()
    if prevention in {"PLANNING", "DISCOVERING", "INVESTIGATING"}:
        return {
            "state": "PREVENTION_PLANNING",
            "headline": "I’m preparing a safer option for later.",
            "top_risk": top,
            "recommended_action": "WAIT",
        }
    if prevention in {"READY", "WAITING_FOR_APPROVAL"}:
        return {
            "state": "WAITING_FOR_APPROVAL" if prevention == "WAITING_FOR_APPROVAL" else "PREVENTION_READY",
            "headline": (
                "I found a preventive change and need your approval."
                if prevention == "WAITING_FOR_APPROVAL"
                else "A preventive option is ready."
            ),
            "top_risk": top,
            "recommended_action": "REVIEW_PREVENTION",
        }
    if prevention in {"APPLIED", "COMPLETED", "RECOVERED"}:
        return {
            "state": "PREVENTION_APPLIED",
            "headline": "I handled the future risk and I’m still watching ahead.",
            "top_risk": top,
            "recommended_action": "NONE",
        }

    if top is None:
        return {
            "state": "CLEAR",
            "headline": "I’m watching the rest of your journey.",
            "top_risk": None,
            "recommended_action": "NONE",
        }

    score = int(top["risk_score"])
    lead = int(top["lead_time_minutes"])
    grounded = bool(top["grounded"])

    if not grounded or score < 40:
        state = "WATCHING_FUTURE"
        action = "KEEP_WATCHING"
    elif lead > 240:
        state = "WATCHING_FUTURE"
        action = "KEEP_WATCHING"
    elif lead >= 30:
        state = "FUTURE_RISK_FOUND"
        action = "PREPARE_RECOVERY"
    else:
        state = "FUTURE_RISK_FOUND"
        action = "HAND_OFF_TO_NEAR_TERM_GUARDIAN"

    return {
        "state": state,
        "headline": (
            "I found a credible risk later in the journey."
            if state == "FUTURE_RISK_FOUND"
            else "I’m watching a possible risk later in the journey."
        ),
        "top_risk": top,
        "recommended_action": action,
    }


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _risk_identity(
    *, journey_id: str, activity_id: str, risk_kind: str, provenance: Any, world_epoch: str | None = None
) -> str:
    material = {
        "journey_id": str(journey_id),
        "activity_id": str(activity_id),
        "risk_kind": str(risk_kind),
        "provenance": provenance,
        "material_world_epoch": world_epoch,
    }
    return "ahead:" + hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:24]


def journey_ahead_projection(
    session: Any,
    journey: Journey,
    *,
    now: datetime | None = None,
    horizon_hours: int = 12,
    prevention_status: str | None = None,
    provider_degraded: bool = False,
    brain_blocked: bool = False,
    execution_drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose a bounded, read-only future horizon beside R48 foreground flow."""
    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    horizon_end = stamp + timedelta(hours=max(1, min(24, int(horizon_hours))))
    rows = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
    ) if journey.active_plan_version_id else []
    current_id = str(journey.current_activity_id or "")
    future_rows = []
    for row in rows:
        if row.status in {"COMPLETED", "SKIPPED", "CANCELLED"} or (
            str(row.id) == current_id and row.status in {"EN_ROUTE", "ARRIVED", "IN_PROGRESS"}
        ):
            continue
        try:
            start = datetime.fromisoformat(str(row.start_at).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
        except ValueError:
            continue
        if stamp < start <= horizon_end:
            future_rows.append((row, start))

    snapshot = session.scalar(
        select(JourneyResilienceSnapshot)
        .where(JourneyResilienceSnapshot.journey_id == journey.id)
        .order_by(JourneyResilienceSnapshot.captured_at.desc(), JourneyResilienceSnapshot.id.desc())
    )
    raw_risks = _load(snapshot.activity_risks_json if snapshot else None, [])
    by_activity = {str(item.get("activity_id")): item for item in raw_risks if isinstance(item, dict)}
    provenance = _load(snapshot.provenance_json if snapshot else None, [])
    grounded_default = any(
        str(item.get("provenance") or "").upper() in {"LIVE_PROVIDER", "PROVIDER_GROUNDED", "CAPTURED_REPLAY"}
        for item in provenance if isinstance(item, dict)
    )
    risks: list[dict[str, Any]] = []
    for row, start in future_rows:
        source = dict(by_activity.get(str(row.id)) or {})
        risk_score = int(source.get("risk_score") or 0)
        if risk_score <= 0:
            continue
        risk_kind = str((source.get("failure_modes") or ["FUTURE_RISK"])[0])
        world_epoch = source.get("material_world_epoch") or source.get("world_epoch")
        if world_epoch is None:
            world_epoch = next(
                (
                    item.get("material_world_epoch") or item.get("world_epoch")
                    for item in provenance
                    if isinstance(item, dict)
                    and (item.get("material_world_epoch") or item.get("world_epoch"))
                ),
                None,
            )
        item = {
            **source,
            "activity_id": str(row.id),
            "title": row.title,
            "start_at": row.start_at,
            "starts_at": row.start_at,
            "risk_kind": risk_kind,
            "grounded": bool(source.get("grounded") is True or grounded_default),
            "authority": source.get("authority") or ("PROVIDER_GROUNDED" if grounded_default else "ESTIMATED_AGENT"),
            "source_observation_ids": [item.get("observation_id") for item in provenance if isinstance(item, dict) and item.get("observation_id")],
            "protected_commitment": bool(source.get("protected") or source.get("protected_commitment")),
            "material_world_epoch": world_epoch,
            "risk_identity": _risk_identity(
                journey_id=journey.id,
                activity_id=str(row.id),
                risk_kind=risk_kind,
                provenance=provenance,
                world_epoch=world_epoch,
            ),
        }
        risks.append(item)

    # A material, confirmed execution delay is a distinct Journey Ahead input.
    # Keep it out of provider weather/mobility labels and preserve its source.
    if execution_drift and execution_drift.get("material"):
        target = execution_drift.get("next_risk") or {}
        target_start = _aware(target.get("start_at"))
        if target_start is not None and target_start > stamp:
            risks.append({
                "activity_id": str(target.get("activity_id") or ""),
                "title": str(target.get("activity_title") or "Protected commitment"),
                "start_at": target_start.isoformat(),
                "starts_at": target_start.isoformat(),
                "risk_score": 95,
                "risk_kind": "EXECUTION_DRIFT_EXPOSURE",
                "failure_modes": ["EXECUTION_DRIFT_EXPOSURE"],
                "grounded": True,
                "authority": "SYSTEM_DERIVED_EXECUTION_FACT",
                "source_execution_activity_id": (execution_drift.get("basis") or {}).get("activity_id"),
                "source_truth_kind": (execution_drift.get("basis") or {}).get("truth_kind"),
                "route_slack_minutes": target.get("slack_after_min"),
                "protected_commitment": True,
                "risk_identity": _risk_identity(
                    journey_id=journey.id,
                    activity_id=str(target.get("activity_id") or ""),
                    risk_kind="EXECUTION_DRIFT_EXPOSURE",
                    provenance={"execution_drift": execution_drift.get("basis")},
                ),
            })

    classified = journey_ahead_state(
        activity_risks=risks,
        now=stamp,
        prevention_status=prevention_status,
        provider_degraded=provider_degraded,
        brain_blocked=brain_blocked,
        journey_completed=journey.status == "COMPLETED",
    )
    from app.agent.journey_intent import active_journey_intents

    active_intents = active_journey_intents(session, journey, now=stamp)
    return {
        **classified,
        "horizon": {
            "remaining_activity_count": len(future_rows),
            "lookahead_until": horizon_end.isoformat(),
            "bounded_hours": max(1, min(24, int(horizon_hours))),
        },
        "risks": actionable_future_risks(risks, now=stamp),
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_captured_at": snapshot.captured_at if snapshot else None,
        "current_activity_id": current_id or None,
        "active_intents": active_intents,
        "read_only": True,
        "predictive_only": True,
    }
