from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.config import settings
from app.db.models import (
    Activity,
    CitySignalFixture,
    Event,
    Journey,
    Place,
    PlanVersion,
    ProviderObservation,
)
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureResult
from app.replanning.provider_snapshots import MobilitySnapshot

ACTIONABLE_EVENT_TYPES = frozenset({"WEATHER", "TRAFFIC_DELAY", "CLOSURE", "SCHEDULE_DELAY"})
FIXTURE_STATES = frozenset({"NORMAL", "DISRUPTED", "OUTAGE", "RISK_FORECAST"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _time_bucket(value: str, *, minutes: int = 15) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    rounded = parsed.replace(
        minute=(parsed.minute // minutes) * minutes,
        second=0,
        microsecond=0,
    )
    return rounded.isoformat()


def incident_fingerprint(
    *,
    journey_id: str,
    provider_key: str,
    event_type: str,
    scope_key: str | None,
    occurred_at: str,
    disruption_class: str | None = None,
) -> str:
    """Create semantic incident identity independent of provider Event UUIDs."""

    material = {
        "journey_id": str(journey_id),
        "provider_key": provider_key,
        "event_type": event_type,
        "scope": scope_key,
        "time_bucket": _time_bucket(occurred_at),
        "disruption_class": disruption_class or event_type,
    }
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SignalPipelineResult:
    provider_key: str
    state: str
    observation_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    created_event_ids: tuple[str, ...]
    resolved_event_ids: tuple[str, ...]
    degraded: bool
    provider_states: dict[str, str] = field(default_factory=dict)
    provider_provenance: dict[str, str] = field(default_factory=dict)
    captured_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "state": self.state,
            "observation_ids": list(self.observation_ids),
            "event_ids": list(self.event_ids),
            "created_event_ids": list(self.created_event_ids),
            "resolved_event_ids": list(self.resolved_event_ids),
            "degraded": self.degraded,
            "provider_states": dict(self.provider_states),
            "provider_provenance": dict(self.provider_provenance),
            "captured_at": self.captured_at,
        }


def ensure_fixture(session: Session, journey_id: str) -> CitySignalFixture:
    row = session.get(CitySignalFixture, str(journey_id))
    if row is not None:
        return row
    row = CitySignalFixture(
        journey_id=str(journey_id),
        provider_key="city-signal-fixture",
        state="NORMAL",
        revision=0,
        payload_json=_json({"description": "normal city conditions"}),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def set_fixture_state(
    session: Session,
    journey: Journey,
    *,
    state: str,
    payload: dict[str, Any] | None = None,
) -> CitySignalFixture:
    normalized = str(state).upper()
    if normalized not in FIXTURE_STATES:
        raise ValueError(f"unsupported city signal fixture state: {state}")
    row = ensure_fixture(session, journey.id)
    row.state = normalized
    row.revision = int(row.revision) + 1
    row.payload_json = _json(payload or {})
    row.updated_at = _now()
    record_audit(
        session,
        action="city.signal.fixture.updated",
        entity_type="CitySignalFixture",
        entity_id=journey.id,
        journey_id=journey.id,
        after={
            "provider_key": row.provider_key,
            "state": row.state,
            "revision": row.revision,
            "payload": json.loads(row.payload_json),
        },
        actor_type="USER",
        actor_id=journey.traveler_id,
    )
    session.flush()
    return row


def fixture_payload(row: CitySignalFixture) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "journey_id": row.journey_id,
        "provider_key": row.provider_key,
        "state": row.state,
        "revision": row.revision,
        "payload": payload,
        "updated_at": row.updated_at,
    }


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


def _place(session: Session, activity: Activity) -> Place | None:
    return session.get(Place, activity.place_id) if activity.place_id else None


def _find_activity(activities: list[Activity], *place_ids: str) -> Activity | None:
    for place_id in place_ids:
        item = next((activity for activity in activities if activity.place_id == place_id), None)
        if item is not None:
            return item
    return None


def _fallback_mutable_activity(
    activities: list[Activity],
    *,
    current_activity: Activity | None,
    exclude_ids: set[str] | None = None,
) -> Activity | None:
    """Choose a server-owned future activity when a provider omits a target.

    The deterministic fixture is used by more than the original demo Journey.
    Provider incidents may carry only a city-level semantic signal, so the
    pipeline must attach that signal to an actual mutable Activity rather than
    inventing a place id or leaving impact resolution targetless.
    """

    excluded = exclude_ids or set()
    current_sequence = current_activity.sequence_no if current_activity else -1
    return next(
        (
            item
            for item in activities
            if item.id not in excluded
            and item.sequence_no >= current_sequence
            and item.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
            and not item.locked
            and item.place_id
        ),
        None,
    )


def _event_time(activity: Activity | None, *, minutes_before: int) -> str:
    if activity is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        value = datetime.fromisoformat(activity.start_at)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (value - timedelta(minutes=minutes_before)).isoformat()
    except ValueError:
        return activity.start_at


def _upsert_fixture_event(
    session: Session,
    journey: Journey,
    *,
    event_type: str,
    severity: str,
    activity: Activity | None,
    scope_key: str,
    occurred_at: str,
    payload: dict[str, Any],
    provenance: str = "PROVIDER_VERIFIED",
) -> tuple[Event, bool]:
    place_id = activity.place_id if activity else None
    fingerprint = incident_fingerprint(
        journey_id=journey.id,
        provider_key="city-signal-fixture",
        event_type=event_type,
        scope_key=scope_key or place_id or (activity.id if activity else "journey"),
        occurred_at=occurred_at,
        disruption_class=str(payload.get("disruption_class") or event_type),
    )
    payload = {
        **payload,
        "incident_fingerprint": fingerprint,
        "scope_place_id": scope_key or place_id,
        # The deterministic demo provider describes a schedule-relative
        # observation. Keeping that instant in the payload lets the replan
        # gate evaluate the Journey's local opening hours rather than the
        # machine clock used to ingest the fixture.
        "snapshot_captured_at": occurred_at,
        "provider_captured_at": _now(),
        "source": "server_city_signal_pipeline",
    }
    # Query by semantic fingerprint rather than the provider Event UUID.  This
    # is the important dedup boundary for repeated watcher refreshes.
    candidates = session.scalars(
        select(Event).where(
            Event.journey_id == journey.id,
            Event.provider_key == "city-signal-fixture",
            Event.provenance == provenance,
            Event.event_type == event_type,
        )
    ).all()
    existing = next(
        (
            item
            for item in candidates
            if json.loads(item.payload_json or "{}").get("incident_fingerprint") == fingerprint
        ),
        None,
    )
    if existing is None and payload.get("fixture_revision") is not None:
        # A recovery can replace the active PlanVersion between two watcher
        # pulses.  The provider incident is still the same fixture revision;
        # do not mint a second semantic incident merely because its mutable
        # Activity target moved with the new plan.
        existing = next(
            (
                item
                for item in candidates
                if (
                    json.loads(item.payload_json or "{}").get("fixture_revision")
                    == payload.get("fixture_revision")
                    and json.loads(item.payload_json or "{}").get("disruption_class")
                    == payload.get("disruption_class")
                )
            ),
            None,
        )
    if existing is None and payload.get("fixture_revision") is not None:
        # The demo/provider adapter may advance its transport revision on a
        # repeated observation even when the underlying disruption has not
        # changed.  Keep the semantic incident stable when event type,
        # disruption class, and provider-observed instant are unchanged.  A
        # genuinely new incident has a different observed instant or class.
        existing = next(
            (
                item
                for item in candidates
                if (
                    json.loads(item.payload_json or "{}").get("disruption_class")
                    == payload.get("disruption_class")
                    and item.occurred_at == occurred_at
                )
            ),
            None,
        )
    now = _now()
    if existing is not None:
        previous_payload = json.loads(existing.payload_json or "{}")
        existing.severity = severity
        existing.status = "DETECTED"
        if activity is not None and existing.affected_activity_id is None:
            existing.affected_activity_id = activity.id
        existing.occurred_at = occurred_at
        payload["incident_fingerprint"] = previous_payload.get(
            "incident_fingerprint", fingerprint
        )
        payload["scope_place_id"] = previous_payload.get(
            "scope_place_id", payload.get("scope_place_id")
        )
        existing.payload_json = _json(payload)
        existing.resolved_at = None
        session.flush()
        return existing, False
    event = Event(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key="city-signal-fixture",
        provenance=provenance,
        event_type=event_type,
        severity=severity,
        status="DETECTED",
        affected_activity_id=activity.id if activity else None,
        occurred_at=occurred_at,
        detected_at=now,
        payload_json=_json(payload),
        created_at=now,
    )
    session.add(event)
    record_audit(
        session,
        action="city.signal.incident.created",
        entity_type="Event",
        entity_id=event.id,
        journey_id=journey.id,
        after={
            "provider_key": event.provider_key,
            "event_type": event.event_type,
            "incident_fingerprint": fingerprint,
            "affected_activity_id": event.affected_activity_id,
        },
        actor_type="SYSTEM",
        actor_id="city-signal-pipeline",
    )
    session.flush()
    return event, True


def _fixture_event_time(
    session: Session,
    journey: Journey,
    *,
    event_type: str,
    scope_key: str,
    activity: Activity | None,
    minutes_before: int,
) -> str:
    for event in session.scalars(
        select(Event).where(
            Event.journey_id == journey.id,
            Event.provider_key == "city-signal-fixture",
            Event.event_type == event_type,
        )
    ).all():
        try:
            if json.loads(event.payload_json or "{}").get("scope_place_id") == scope_key:
                return event.occurred_at
        except json.JSONDecodeError:
            continue
    if activity is not None:
        return _event_time(activity, minutes_before=minutes_before)
    return _event_time(None, minutes_before=minutes_before)


def _record_observation(
    session: Session,
    journey: Journey,
    *,
    state: str,
    payload: dict[str, Any],
    fingerprints: list[str],
    captured_at: str,
    provider_key: str = "city-signal-fixture",
    provenance: str = "REPLAY",
    observation_type: str = "CITY_SIGNAL",
) -> ProviderObservation:
    expires_at = None
    try:
        expires_at = (
            datetime.fromisoformat(captured_at)
            + timedelta(seconds=settings.real_city_cache_ttl_seconds)
        ).isoformat()
    except ValueError:
        pass
    row = ProviderObservation(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=provider_key,
        observation_type=observation_type,
        state=state,
        incident_fingerprint=(fingerprints[0] if fingerprints else None),
        provenance=provenance,
        captured_at=captured_at,
        expires_at=expires_at,
        payload_json=_json({**payload, "incident_fingerprints": fingerprints}),
        created_at=captured_at,
    )
    session.add(row)
    record_audit(
        session,
        action="city.signal.observation.received",
        entity_type="ProviderObservation",
        entity_id=row.id,
        journey_id=journey.id,
        after={
            "provider_key": row.provider_key,
            "observation_type": row.observation_type,
            "state": state,
            "incident_fingerprints": fingerprints,
        },
        actor_type="SYSTEM",
        actor_id="city-signal-pipeline",
    )
    session.flush()
    return row


def _record_mobility_observation(
    session: Session,
    journey: Journey,
    *,
    payload: dict[str, Any],
    captured_at: str,
    provider_key: str = "city-signal-fixture",
    provenance: str = "REPLAY",
) -> ProviderObservation:
    expires_at = None
    try:
        expires_at = (
            datetime.fromisoformat(captured_at)
            + timedelta(seconds=settings.real_city_cache_ttl_seconds)
        ).isoformat()
    except ValueError:
        pass
    row = ProviderObservation(
        id=str(uuid4()),
        journey_id=journey.id,
        provider_key=provider_key,
        observation_type="MOBILITY",
        state="CAPTURED",
        incident_fingerprint=None,
        provenance=provenance,
        captured_at=captured_at,
        expires_at=expires_at,
        payload_json=_json(payload),
        created_at=captured_at,
    )
    session.add(row)
    record_audit(
        session,
        action="city.signal.mobility_captured",
        entity_type="ProviderObservation",
        entity_id=row.id,
        journey_id=journey.id,
        after={"provider_key": row.provider_key, "observation_type": row.observation_type},
        actor_type="SYSTEM",
        actor_id="city-signal-pipeline",
    )
    session.flush()
    return row


def _mobility_evidence(
    session: Session,
    journey: Journey,
    activities: list[Activity],
    *,
    state: str,
    captured_at: str,
) -> tuple[dict[str, Any], ProviderObservation]:
    keys = [activity.place_id for activity in activities if activity.place_id]
    pairs = list(zip(keys, keys[1:]))
    active_plan = (
        session.get(PlanVersion, journey.active_plan_version_id)
        if journey.active_plan_version_id
        else None
    )
    frozen_evidence: dict[str, Any] = {}
    if active_plan is not None:
        try:
            frozen_evidence = json.loads(active_plan.mobility_evidence_json or "{}")
        except json.JSONDecodeError:
            frozen_evidence = {}
    is_authoritative = (
        str(frozen_evidence.get("normalizer_version") or "").startswith(
            ("citynexus-v12-authoritative-", "citynexus-v13-authoritative-")
        )
        and bool(frozen_evidence.get("facts"))
        and int(frozen_evidence.get("mode_matrix_count") or 0) > 0
    )
    if is_authoritative:
        # Authoritative plans may contain structural nodes (check-in, stay,
        # and anchors) that have no Place coordinates.  Guardian sensing must
        # replay the plan's sealed matrix instead of asking a runtime provider
        # to route those synthetic-but-authoritative node keys again.
        capture = MobilitySnapshotCaptureResult(
            snapshot=MobilitySnapshot.from_evidence(frozen_evidence),
            failed_primary_pairs=(),
            fallback_pairs=(),
        )
    else:
        capture = capture_runtime_mobility_snapshot(
            session,
            journey,
            pairs,
            operation_key=f"guardian-pulse:{journey.id}",
            captured_at=captured_at,
        )
    baseline_row = session.scalar(
        select(ProviderObservation)
        .where(
            ProviderObservation.journey_id == journey.id,
            ProviderObservation.observation_type == "MOBILITY",
        )
        .order_by(ProviderObservation.captured_at.desc(), ProviderObservation.id.desc())
    )
    try:
        baseline_payload = json.loads(baseline_row.payload_json or "{}") if baseline_row else {}
    except json.JSONDecodeError:
        baseline_payload = {}
    baseline = {
        str(key): int(value)
        for key, value in (baseline_payload.get("current_minutes") or {}).items()
    }
    current = {
        f"{origin}|{destination}": int(fact.minutes)
        for (origin, destination), fact in capture.snapshot.facts.items()
    }
    if state == "DISRUPTED":
        for key in list(current):
            if key.endswith("|place-river-cruise"):
                current[key] += 20
    deltas = [
        {
            "pair": key,
            "baseline_minutes": baseline[key],
            "current_minutes": current[key],
            "delta_minutes": current[key] - baseline[key],
            "meaningful": current[key] - baseline[key] >= 10,
        }
        for key in sorted(current)
        if key in baseline and current[key] - baseline[key] >= 10
    ]
    payload = {
        "snapshot_id": capture.snapshot.snapshot_id,
        "source_provider": capture.snapshot.source_provider_key,
        "provider_provenance": (
            "DETERMINISTIC_FIXTURE_OVERLAY"
            if state == "DISRUPTED"
            else "REPLAY"
            if is_authoritative
            else "PROVIDER_VERIFIED"
        ),
        "active_plan_version_id": journey.active_plan_version_id,
        "captured_at": captured_at,
        "baseline_captured_at": baseline_payload.get("captured_at"),
        "baseline_minutes": baseline,
        "current_minutes": current,
        "meaningful_deltas": deltas,
        "degraded": capture.degraded,
        "authoritative_replay": is_authoritative,
        "baseline_plan_evidence": {
            "plan_version_id": journey.active_plan_version_id,
            "snapshot_id": frozen_evidence.get("snapshot_id"),
            "captured_at": frozen_evidence.get("captured_at"),
            "immutable": True,
        },
        "incident_recovery_evidence": {
            "evidence_id": f"guardian-pulse:{journey.id}:{captured_at}",
            "captured_at": captured_at,
            "snapshot_id": capture.snapshot.snapshot_id,
            "provenance": (
                "DETERMINISTIC_FIXTURE_OVERLAY"
                if state == "DISRUPTED"
                else "REPLAY"
                if is_authoritative
                else "PROVIDER_VERIFIED"
            ),
            "fixture_overlay_minutes": 20 if state == "DISRUPTED" else 0,
            "snapshot": capture.snapshot.to_evidence(),
        },
    }
    observation = _record_mobility_observation(
        session,
        journey,
        payload=payload,
        captured_at=captured_at,
    )
    return payload, observation


def refresh_demo_signals(
    session: Session,
    journey: Journey,
    *,
    captured_at: str | None = None,
) -> SignalPipelineResult:
    """Run the deterministic provider fixture through the same ingestion seam.

    The browser can change only the fixture state. Trusted Event rows are
    created, refreshed, or resolved here by the server-owned pipeline.
    """

    stamp = captured_at or _now()
    fixture = ensure_fixture(session, journey.id)
    state = fixture.state
    activities = _active_activities(session, journey)
    current_activity = next(
        (item for item in activities if item.id == journey.current_activity_id), None
    )
    # A fast Guardian cadence must not turn an unchanged deterministic fixture
    # into an unbounded write stream.  Reuse the sealed observation while the
    # fixture revision, state, and active PlanVersion are unchanged; a state
    # transition or accepted recovery naturally captures a new observation.
    latest_signal = session.scalar(
        select(ProviderObservation)
        .where(
            ProviderObservation.journey_id == journey.id,
            ProviderObservation.provider_key == "open-meteo-replay",
            ProviderObservation.observation_type == "CITY_SIGNAL",
        )
        .order_by(ProviderObservation.captured_at.desc(), ProviderObservation.id.desc())
    )
    if latest_signal is not None:
        try:
            cached = json.loads(latest_signal.payload_json or "{}")
        except json.JSONDecodeError:
            cached = {}
        if (
            cached.get("revision") == fixture.revision
            and cached.get("state") == state
            and cached.get("active_plan_version_id") == journey.active_plan_version_id
        ):
            latest_mobility = session.scalar(
                select(ProviderObservation)
                .where(
                    ProviderObservation.journey_id == journey.id,
                    ProviderObservation.observation_type == "MOBILITY",
                )
                .order_by(ProviderObservation.captured_at.desc(), ProviderObservation.id.desc())
            )
            try:
                cached_mobility = json.loads(latest_mobility.payload_json or "{}") if latest_mobility else {}
            except json.JSONDecodeError:
                cached_mobility = {}
            # A signal and its route matrix are one sealed observation pair.
            # Never reuse a city signal with mobility facts captured for a
            # different active PlanVersion; that would make Guardian impact
            # evaluation observe a plan that no longer owns the route facts.
            if latest_mobility is None or cached_mobility.get("active_plan_version_id") != journey.active_plan_version_id:
                latest_mobility = None
            if latest_mobility is None:
                pass
            else:
                open_events = session.scalars(
                    select(Event).where(
                        Event.journey_id == journey.id,
                        Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
                    )
                ).all()
                return SignalPipelineResult(
                    provider_key="city-signal-fixture",
                    state=state,
                    observation_ids=tuple(
                        item
                        for item in (latest_signal.id, latest_mobility.id)
                        if item
                    ),
                    event_ids=tuple(item.id for item in open_events),
                    created_event_ids=(),
                    resolved_event_ids=(),
                    degraded=state == "OUTAGE",
                    provider_states={
                        "weather": "RISK_FORECAST" if state == "RISK_FORECAST" else state,
                        "mobility": "DEGRADED" if state == "OUTAGE" else "AVAILABLE",
                        "city_discovery": "AVAILABLE",
                    },
                    provider_provenance={
                        "weather": "REPLAY",
                        "mobility": "REPLAY",
                        "city_discovery": "REPLAY",
                    },
                    captured_at=latest_signal.captured_at,
                )
    weather_activity = _find_activity(activities, "place-tech-art")
    if weather_activity is None:
        weather_activity = _fallback_mutable_activity(
            activities,
            current_activity=current_activity,
        )
    traffic_activity = _find_activity(activities, "place-river-cruise")
    if traffic_activity is None:
        traffic_activity = _fallback_mutable_activity(
            activities,
            current_activity=current_activity,
            exclude_ids={weather_activity.id} if weather_activity else None,
        )
    mobility, mobility_observation = _mobility_evidence(
        session,
        journey,
        activities,
        state=state,
        captured_at=stamp,
    )
    event_ids: list[str] = []
    created_event_ids: list[str] = []
    resolved_event_ids: list[str] = []
    fingerprints: list[str] = []
    payload = fixture_payload(fixture)
    degraded = state == "OUTAGE"

    if state in {"DISRUPTED", "RISK_FORECAST"}:
        predictive = state == "RISK_FORECAST"
        fixture_payload_data = payload.get("payload") or {}
        prevented = None
        if predictive:
            prevented = next(
                (
                    item
                    for item in session.scalars(
                        select(Event).where(
                            Event.journey_id == journey.id,
                            Event.provider_key == "city-signal-fixture",
                            Event.provenance == "SYSTEM_DERIVED",
                            Event.event_type == "WEATHER",
                            Event.status == "ACKNOWLEDGED",
                        )
                    ).all()
                    if json.loads(item.payload_json or "{}").get("predictive_prevented") is True
                    and json.loads(item.payload_json or "{}").get("fixture_revision")
                    == fixture.revision
                ),
                None,
            )
        if prevented is not None:
            weather, created = prevented, False
        else:
            weather, created = _upsert_fixture_event(
                session,
                journey,
                event_type="WEATHER",
                severity="MINOR" if predictive else "MAJOR",
                activity=weather_activity,
                scope_key=weather_activity.place_id if weather_activity else "journey-weather",
                occurred_at=_fixture_event_time(
                    session,
                    journey,
                    event_type="WEATHER",
                    scope_key="place-tech-art",
                    activity=current_activity or weather_activity,
                    minutes_before=5,
                ),
                payload={
                    "description": (
                        "Forecast rain is likely to weaken the outdoor connection before it becomes infeasible"
                        if predictive
                        else "Heavy rain threatens an outdoor city connection"
                    ),
                    "disruption_class": "FORECAST_HEAVY_RAIN" if predictive else "HEAVY_RAIN",
                    "weather": "HEAVY_RAIN",
                    "fixture_revision": fixture.revision,
                    "predictive_only": predictive,
                    "forecast_horizon_min": int(
                        fixture_payload_data.get("forecast_horizon_min", 45)
                    ),
                    "risk_score": int(
                        fixture_payload_data.get("risk_score", 82 if predictive else 0)
                    ),
                    "expected_route_delay_min": int(
                        fixture_payload_data.get(
                            "expected_route_delay_min", 18 if predictive else 0
                        )
                    ),
                },
                provenance="SYSTEM_DERIVED" if predictive else "PROVIDER_VERIFIED",
            )
        events_to_record = [(weather, created)]
        if not predictive:
            traffic, traffic_created = _upsert_fixture_event(
                session,
                journey,
                    event_type="TRAFFIC_DELAY",
                    severity="MINOR",
                    activity=traffic_activity,
                    scope_key=traffic_activity.place_id if traffic_activity else "journey-traffic",
                occurred_at=_fixture_event_time(
                    session,
                    journey,
                    event_type="TRAFFIC_DELAY",
                    scope_key="place-river-cruise",
                    activity=traffic_activity,
                    minutes_before=4,
                ),
                payload={
                    "description": "River corridor mobility deterioration",
                    "disruption_class": "ROUTE_DELAY",
                    "extra_minutes": 20,
                    "mobility": mobility,
                    "fixture_revision": fixture.revision,
                },
            )
            events_to_record.append((traffic, traffic_created))
        for event, was_created in events_to_record:
            event_ids.append(event.id)
            created_event_ids.extend([event.id] if was_created else [])
            try:
                fingerprints.append(json.loads(event.payload_json).get("incident_fingerprint"))
            except (TypeError, json.JSONDecodeError):
                pass
    else:
        rows = session.scalars(
            select(Event).where(
                Event.journey_id == journey.id,
                Event.provider_key == "city-signal-fixture",
                Event.provenance.in_(("PROVIDER_VERIFIED", "SYSTEM_DERIVED")),
                Event.event_type.in_(tuple(ACTIONABLE_EVENT_TYPES)),
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
        ).all()
        for event in rows:
            event.status = "RESOLVED"
            event.resolved_at = stamp
            resolved_event_ids.append(event.id)

    mobility_payload = {
        **mobility,
        "fixture_revision": fixture.revision,
        "fixture_state": state,
        "active_plan_version_id": journey.active_plan_version_id,
    }
    observation = _record_observation(
        session,
        journey,
        state=state,
        payload={
            **payload,
            "active_plan_version_id": journey.active_plan_version_id,
            "mobility": mobility_payload,
            "provider_mesh": {
                "weather": {
                    "provider_key": "open-meteo-replay",
                    "state": "FORECAST_CAPTURED" if state == "RISK_FORECAST" else "CAPTURED",
                    "provenance": "REPLAY",
                },
                "mobility": {
                    "provider_key": "valhalla-replay",
                    "state": "DEGRADED" if mobility.get("degraded") else "CAPTURED",
                    "provenance": "REPLAY",
                },
                "city_discovery": {
                    "provider_key": "openstreetmap-replay",
                    "state": "AVAILABLE",
                    "provenance": "REPLAY",
                },
            },
        },
        fingerprints=[item for item in fingerprints if item],
        captured_at=stamp,
        provider_key="open-meteo-replay",
        provenance="REPLAY",
    )
    session.flush()
    return SignalPipelineResult(
        provider_key="city-signal-fixture",
        state=state,
        observation_ids=(observation.id, mobility_observation.id),
        event_ids=tuple(dict.fromkeys(event_ids)),
        created_event_ids=tuple(dict.fromkeys(created_event_ids)),
        resolved_event_ids=tuple(dict.fromkeys(resolved_event_ids)),
        degraded=degraded,
        provider_states={
            "weather": "RISK_FORECAST" if state == "RISK_FORECAST" else state,
            "mobility": "DEGRADED" if mobility.get("degraded") else "AVAILABLE",
            "city_discovery": "AVAILABLE",
        },
        provider_provenance={
            "weather": "REPLAY",
            "mobility": "REPLAY",
            "city_discovery": "REPLAY",
        },
        captured_at=stamp,
    )
