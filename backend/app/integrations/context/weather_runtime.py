from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import Activity, Event, Journey, Place, ProviderObservation
from app.integrations.context.open_meteo import (
    OpenMeteoProviderError,
    WeatherObservation,
    WeatherRisk,
)
from app.integrations.context.weather_ingestion import (
    ingest_provider_verified_weather_event,
    record_provider_degradation,
    resolve_cleared_weather_events,
    resolve_provider_degradation,
)
from app.integrations.context.weather_snapshot import (
    WeatherQueryScope,
    WeatherRiskFact,
    WeatherSnapshot,
    freeze_weather_risks,
)

TERMINAL_ACTIVITY_STATES = {"COMPLETED", "SKIPPED", "CANCELLED"}


class WeatherForecastProvider(Protocol):
    provider_key: str

    async def forecast(self, *, latitude: float, longitude: float) -> list[WeatherObservation]: ...

    def risks(self, observations: list[WeatherObservation]) -> list[WeatherRisk]: ...


@dataclass(frozen=True)
class WeatherActivityAttribution:
    activity_id: str
    place_id: str
    fact: WeatherRiskFact
    activity_start_at: str
    activity_end_at: str
    local_risk_start_at: str
    local_risk_end_at: str
    reason_codes: tuple[str, ...]

    def to_evidence(self) -> dict[str, object]:
        return {
            "activity_id": self.activity_id,
            "place_id": self.place_id,
            "activity_start_at": self.activity_start_at,
            "activity_end_at": self.activity_end_at,
            "local_risk_start_at": self.local_risk_start_at,
            "local_risk_end_at": self.local_risk_end_at,
            "reason_codes": list(self.reason_codes),
            "weather_fact": self.fact.to_evidence(),
        }


@dataclass(frozen=True)
class WeatherRuntimeResult:
    provider_key: str
    queried_place_ids: tuple[str, ...]
    successful_place_ids: tuple[str, ...]
    failed_place_ids: tuple[str, ...]
    snapshots: tuple[WeatherSnapshot, ...]
    attributions: tuple[WeatherActivityAttribution, ...]
    event_ids: tuple[str, ...]
    created_event_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    degraded: bool

    def to_evidence(self) -> dict[str, object]:
        return {
            "provider": self.provider_key,
            "queried_place_ids": list(self.queried_place_ids),
            "successful_place_ids": list(self.successful_place_ids),
            "failed_place_ids": list(self.failed_place_ids),
            "degraded": self.degraded,
            "snapshots": [snapshot.to_evidence() for snapshot in self.snapshots],
            "attributions": [item.to_evidence() for item in self.attributions],
            "event_ids": list(self.event_ids),
            "created_event_ids": list(self.created_event_ids),
            "observation_ids": list(self.observation_ids),
        }


def _journey_timezone(journey: Journey) -> ZoneInfo:
    try:
        return ZoneInfo(journey.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Journey timezone is invalid: {journey.timezone}") from exc


def _activity_weather_sensitive(place: Place) -> bool:
    """Apply the explicit Place-tag weather sensitivity policy."""

    try:
        tags = {str(item).strip().lower() for item in json.loads(place.tags_json or "[]")}
    except (TypeError, ValueError, json.JSONDecodeError):
        tags = set()
    if "weather_sensitive" in tags:
        return True
    if "indoor" in tags:
        return False
    return "outdoor" in tags


def _eligible_weather_activities(
    session: Session, journey: Journey
) -> list[tuple[Activity, Place]]:
    if not journey.active_plan_version_id:
        return []
    activities = session.scalars(
        select(Activity)
        .where(Activity.plan_version_id == journey.active_plan_version_id)
        .order_by(Activity.sequence_no)
    ).all()
    current_sequence: int | None = None
    if journey.current_activity_id:
        current = next(
            (item for item in activities if item.id == journey.current_activity_id), None
        )
        if current is not None:
            current_sequence = current.sequence_no

    eligible: list[tuple[Activity, Place]] = []
    for activity in activities:
        if activity.status in TERMINAL_ACTIVITY_STATES:
            continue
        if current_sequence is not None and activity.sequence_no < current_sequence:
            continue
        if not activity.place_id:
            continue
        place = session.get(Place, activity.place_id)
        if place is None or not _activity_weather_sensitive(place):
            continue
        eligible.append((activity, place))
    return eligible


def attribute_weather_fact_to_activity(
    *,
    journey: Journey,
    activity: Activity,
    place: Place,
    fact: WeatherRiskFact,
) -> WeatherActivityAttribution | None:
    """Map one frozen provider fact to one Activity with explicit evidence."""

    if activity.status in TERMINAL_ACTIVITY_STATES:
        return None
    if activity.place_id != place.id:
        return None
    if fact.scope is None or fact.scope.place_id != place.id:
        return None
    if float(fact.scope.latitude) != float(place.latitude) or float(fact.scope.longitude) != float(
        place.longitude
    ):
        return None
    if not _activity_weather_sensitive(place):
        return None

    timezone = _journey_timezone(journey)
    risk_start = datetime.fromisoformat(fact.at)
    if risk_start.tzinfo is None:
        raise ValueError("WeatherRiskFact.at must be timezone-aware")
    risk_end = (
        datetime.fromisoformat(fact.valid_until)
        if fact.valid_until is not None
        else risk_start + timedelta(hours=1)
    )
    if risk_end.tzinfo is None:
        raise ValueError("WeatherRiskFact.valid_until must be timezone-aware")
    local_risk_start = risk_start.astimezone(timezone)
    local_risk_end = risk_end.astimezone(timezone)

    activity_start = datetime.fromisoformat(activity.start_at)
    activity_end = datetime.fromisoformat(activity.end_at)
    if activity_start.tzinfo is None:
        activity_start = activity_start.replace(tzinfo=timezone)
    else:
        activity_start = activity_start.astimezone(timezone)
    if activity_end.tzinfo is None:
        activity_end = activity_end.replace(tzinfo=timezone)
    else:
        activity_end = activity_end.astimezone(timezone)

    if max(local_risk_start, activity_start) >= min(local_risk_end, activity_end):
        return None

    return WeatherActivityAttribution(
        activity_id=activity.id,
        place_id=place.id,
        fact=fact,
        activity_start_at=activity_start.isoformat(),
        activity_end_at=activity_end.isoformat(),
        local_risk_start_at=local_risk_start.isoformat(),
        local_risk_end_at=local_risk_end.isoformat(),
        reason_codes=(
            "WEATHER_TIME_OVERLAP",
            "WEATHER_SCOPE_MATCH",
            "WEATHER_SENSITIVE_PLACE",
        ),
    )


async def refresh_weather_runtime(
    session: Session,
    journey: Journey,
    *,
    provider: WeatherForecastProvider,
    captured_at: str | None = None,
) -> WeatherRuntimeResult:
    """Capture, freeze and deterministically attribute Open-Meteo weather."""

    provider_key = str(getattr(provider, "provider_key", ""))
    if provider_key != "open-meteo":
        raise ValueError("weather runtime currently requires the open-meteo provider")

    eligible = _eligible_weather_activities(session, journey)
    by_place: dict[str, tuple[Place, list[Activity]]] = {}
    for activity, place in eligible:
        existing = by_place.get(place.id)
        if existing is None:
            by_place[place.id] = (place, [activity])
        else:
            existing[1].append(activity)

    snapshots: list[WeatherSnapshot] = []
    attributions: list[WeatherActivityAttribution] = []
    event_ids: list[str] = []
    created_event_ids: list[str] = []
    observation_ids: list[str] = []
    successful: list[str] = []
    failed: list[str] = []

    for place_id in sorted(by_place):
        place, activities = by_place[place_id]
        scope = WeatherQueryScope(
            place_id=place.id,
            latitude=float(place.latitude),
            longitude=float(place.longitude),
        )
        try:
            observations = await provider.forecast(
                latitude=scope.latitude,
                longitude=scope.longitude,
            )
            risks = provider.risks(observations)
        except OpenMeteoProviderError as exc:
            failed.append(place_id)
            record_provider_degradation(
                session,
                journey_id=journey.id,
                provider_key="open-meteo",
                detail=f"{type(exc).__name__}: {exc}",
                occurred_at=captured_at,
            )
            continue
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes client failures
            failed.append(place_id)
            record_provider_degradation(
                session,
                journey_id=journey.id,
                provider_key="open-meteo",
                detail=f"{type(exc).__name__}: {exc}",
                occurred_at=captured_at,
            )
            continue

        successful.append(place_id)
        snapshot = freeze_weather_risks(
            risks,
            provider_key=provider_key,
            captured_at=captured_at,
            degraded=False,
            scope=scope,
        )
        snapshots.append(snapshot)
        capture_provenance = str(getattr(provider, "capture_provenance", "LIVE_PROVIDER"))
        forecast_runs = sorted(
            {
                str(item.evidence["forecast_run"])
                for item in risks
                if item.evidence.get("forecast_run") not in (None, "")
            }
        )
        provider_observation = ProviderObservation(
            id=str(uuid4()),
            journey_id=journey.id,
            provider_key=provider_key,
            observation_type="WEATHER",
            state="CAPTURED",
            provenance=capture_provenance,
            captured_at=snapshot.captured_at,
            expires_at=None,
            payload_json=json.dumps(
                {
                    "provider": provider_key,
                    "capture_provenance": capture_provenance,
                    "forecast_runs": forecast_runs,
                    "target_times": [
                        item.at.astimezone(timezone.utc).isoformat() for item in observations
                    ],
                    "query_scope": scope.to_evidence(),
                    "snapshot": snapshot.to_evidence(),
                },
                sort_keys=True,
            ),
            created_at=snapshot.captured_at,
        )
        session.add(provider_observation)
        observation_ids.append(provider_observation.id)
        record_audit(
            session,
            action="weather.provider.observation.captured",
            entity_type="ProviderObservation",
            entity_id=provider_observation.id,
            journey_id=journey.id,
            after={
                "provider_key": provider_key,
                "observation_type": "WEATHER",
                "provenance": capture_provenance,
                "captured_at": snapshot.captured_at,
                "query_scope": scope.to_evidence(),
            },
            actor_type="SYSTEM",
            actor_id="open-meteo-runtime",
        )
        observation_evidence_by_time: dict[str, dict[str, object]] = {}
        for observation in observations:
            observed_at = observation.at
            if observed_at.tzinfo is None:
                raise ValueError("weather observation timestamp must be timezone-aware")
            observation_evidence_by_time[observed_at.astimezone(timezone.utc).isoformat()] = {
                "precipitation_probability": observation.precipitation_probability,
                "weather_code": observation.weather_code,
            }
        resolve_cleared_weather_events(
            session,
            journey_id=journey.id,
            scope=scope,
            observation_evidence_by_time=observation_evidence_by_time,
            active_facts=snapshot.facts,
            resolved_at=snapshot.captured_at,
        )
        for fact in snapshot.facts:
            for activity in activities:
                attribution = attribute_weather_fact_to_activity(
                    journey=journey,
                    activity=activity,
                    place=place,
                    fact=fact,
                )
                if attribution is None:
                    continue
                attributions.append(attribution)
                before = session.scalars(
                    select(Event).where(
                        Event.journey_id == journey.id,
                        Event.provider_key == "open-meteo",
                        Event.provenance == "PROVIDER_VERIFIED",
                        Event.event_type == "WEATHER",
                        Event.affected_activity_id == activity.id,
                    )
                ).all()
                event = ingest_provider_verified_weather_event(
                    session,
                    journey_id=journey.id,
                    fact=fact,
                    affected_activity_id=activity.id,
                )
                event_payload = json.loads(event.payload_json or "{}")
                event_payload["provider_observation_id"] = provider_observation.id
                event_payload["capture_provenance"] = capture_provenance
                if forecast_runs:
                    event_payload["forecast_runs"] = forecast_runs
                event.payload_json = json.dumps(event_payload, sort_keys=True)
                event_ids.append(event.id)
                if all(item.id != event.id for item in before):
                    created_event_ids.append(event.id)

    if by_place and not failed:
        resolve_provider_degradation(
            session,
            journey_id=journey.id,
            provider_key="open-meteo",
            resolved_at=captured_at,
        )

    session.flush()
    return WeatherRuntimeResult(
        provider_key=provider_key,
        queried_place_ids=tuple(sorted(by_place)),
        successful_place_ids=tuple(successful),
        failed_place_ids=tuple(failed),
        snapshots=tuple(snapshots),
        attributions=tuple(attributions),
        event_ids=tuple(dict.fromkeys(event_ids)),
        created_event_ids=tuple(dict.fromkeys(created_event_ids)),
        observation_ids=tuple(dict.fromkeys(observation_ids)),
        degraded=bool(failed),
    )
