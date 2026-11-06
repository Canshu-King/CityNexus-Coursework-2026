from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import Event
from app.integrations.context.weather_snapshot import WeatherRiskFact


def _weather_event_identity(
    *,
    journey_id: str,
    fact: WeatherRiskFact,
    affected_activity_id: str | None,
) -> dict[str, str | float | None]:
    return {
        "journey_id": str(journey_id),
        "provider_key": fact.provider_key,
        "reason_code": fact.code,
        "forecast_valid_from": fact.at,
        "forecast_valid_until": fact.valid_until,
        "affected_activity_id": str(affected_activity_id) if affected_activity_id else None,
        "scope_place_id": fact.scope.place_id if fact.scope is not None else None,
        "scope_latitude": fact.scope.latitude if fact.scope is not None else None,
        "scope_longitude": fact.scope.longitude if fact.scope is not None else None,
    }


def _existing_weather_event(
    session: Session,
    *,
    journey_id: str,
    fact: WeatherRiskFact,
    affected_activity_id: str | None,
) -> Event | None:
    """Return an existing Event for the same provider fact/activity identity."""

    candidates = session.scalars(
        select(Event).where(
            Event.journey_id == str(journey_id),
            Event.provider_key == fact.provider_key,
            Event.provenance == "PROVIDER_VERIFIED",
            Event.event_type == "WEATHER",
            Event.affected_activity_id
            == (str(affected_activity_id) if affected_activity_id else None),
        )
    ).all()
    expected = _weather_event_identity(
        journey_id=str(journey_id), fact=fact, affected_activity_id=affected_activity_id
    )
    for item in candidates:
        try:
            payload = json.loads(item.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        persisted_identity = payload.get("weather_identity")
        if persisted_identity == expected:
            return item
        if persisted_identity is not None:
            continue
        if payload.get("reason_code") == fact.code and payload.get("forecast_valid_until") in {
            None,
            fact.valid_until,
        }:
            scope = payload.get("query_scope") or {}
            expected_scope = fact.scope
            if expected_scope is None:
                if not scope:
                    return item
            else:
                if scope.get("place_id") not in {None, expected_scope.place_id}:
                    continue
                if scope.get("latitude") not in {None, expected_scope.latitude}:
                    continue
                if scope.get("longitude") not in {None, expected_scope.longitude}:
                    continue
                return item
    return None


def ingest_provider_verified_weather_event(
    session: Session,
    *,
    journey_id: str,
    fact: WeatherRiskFact,
    affected_activity_id: str | None = None,
) -> Event:
    """Persist or refresh one server-owned provider weather phenomenon.

    The stable Event identity is the provider risk bucket + Activity + exact query
    scope. Re-polling the same bucket reuses the Event but refreshes its trusted
    evidence and latest snapshot capture timestamp.
    """

    if fact.provider_key != "open-meteo":
        raise ValueError("provider-verified weather events require the open-meteo provider")
    severity = {
        "LOW": "INFO",
        "MEDIUM": "MINOR",
        "HIGH": "MAJOR",
        "EXTREME": "CRITICAL",
    }.get(fact.severity.upper(), fact.severity.upper())
    if severity not in {"INFO", "MINOR", "MAJOR", "CRITICAL"}:
        raise ValueError("weather severity does not map to an Event severity")

    now = datetime.now(timezone.utc).isoformat()
    observed_at = fact.snapshot_captured_at or now
    weather_identity = _weather_event_identity(
        journey_id=str(journey_id), fact=fact, affected_activity_id=affected_activity_id
    )
    payload = {
        "reason_code": fact.code,
        "snapshot_captured_at": fact.snapshot_captured_at,
        "forecast_valid_from": fact.at,
        "forecast_valid_until": fact.valid_until,
        "provider_evidence": dict(fact.evidence),
        "query_scope": fact.scope.to_evidence() if fact.scope is not None else None,
        "weather_identity": weather_identity,
    }

    existing = _existing_weather_event(
        session,
        journey_id=str(journey_id),
        fact=fact,
        affected_activity_id=affected_activity_id,
    )
    if existing is not None:
        before = {
            "status": existing.status,
            "severity": existing.severity,
            "payload": json.loads(existing.payload_json or "{}"),
        }
        existing.severity = severity
        existing.payload_json = json.dumps(payload, sort_keys=True)
        if existing.status == "RESOLVED":
            existing.status = "DETECTED"
            existing.resolved_at = None
            existing.occurred_at = observed_at
        record_audit(
            session,
            action="journey.event.provider_verified_refreshed",
            entity_type="Event",
            entity_id=existing.id,
            journey_id=str(journey_id),
            before=before,
            after={
                "status": existing.status,
                "severity": existing.severity,
                "snapshot_captured_at": fact.snapshot_captured_at,
                "weather_identity": weather_identity,
            },
        )
        session.flush()
        return existing

    item = Event(
        id=str(uuid4()),
        journey_id=str(journey_id),
        provider_key="open-meteo",
        provenance="PROVIDER_VERIFIED",
        event_type="WEATHER",
        severity=severity,
        status="DETECTED",
        affected_activity_id=affected_activity_id,
        occurred_at=observed_at,
        detected_at=observed_at,
        payload_json=json.dumps(payload, sort_keys=True),
        created_at=now,
    )
    session.add(item)
    record_audit(
        session,
        action="journey.event.provider_verified",
        entity_type="Event",
        entity_id=item.id,
        journey_id=str(journey_id),
        after={
            "provider_key": item.provider_key,
            "provenance": item.provenance,
            "affected_activity_id": item.affected_activity_id,
            "snapshot_captured_at": fact.snapshot_captured_at,
            "weather_identity": weather_identity,
        },
    )
    session.flush()
    return item


def resolve_cleared_weather_events(
    session: Session,
    *,
    journey_id: str,
    scope: object,
    observation_evidence_by_time: dict[str, dict[str, object]],
    active_facts: tuple[WeatherRiskFact, ...],
    resolved_at: str,
) -> tuple[Event, ...]:
    """Resolve active weather risks only when a successful forecast covers them."""

    place_id = getattr(scope, "place_id", None)
    latitude = getattr(scope, "latitude", None)
    longitude = getattr(scope, "longitude", None)
    if place_id is None or not observation_evidence_by_time:
        return ()

    active_keys = {(fact.code, fact.at) for fact in active_facts}
    resolved: list[Event] = []
    rows = session.scalars(
        select(Event).where(
            Event.journey_id == str(journey_id),
            Event.provider_key == "open-meteo",
            Event.provenance == "PROVIDER_VERIFIED",
            Event.event_type == "WEATHER",
            Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
        )
    ).all()
    for item in rows:
        try:
            payload = json.loads(item.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        query_scope = payload.get("query_scope") or {}
        if query_scope.get("place_id") != place_id:
            continue
        if query_scope.get("latitude") != latitude or query_scope.get("longitude") != longitude:
            continue
        valid_from = payload.get("forecast_valid_from")
        reason_code = payload.get("reason_code")
        if valid_from not in observation_evidence_by_time:
            continue
        if (reason_code, valid_from) in active_keys:
            continue

        before = {"status": item.status, "payload": payload}
        item.status = "RESOLVED"
        item.resolved_at = resolved_at
        payload["weather_resolution"] = {
            "reason": "FORECAST_CLEARED",
            "resolved_at": resolved_at,
            "provider_evidence": observation_evidence_by_time[valid_from],
        }
        item.payload_json = json.dumps(payload, sort_keys=True)
        resolved.append(item)
        record_audit(
            session,
            action="journey.event.weather_cleared",
            entity_type="Event",
            entity_id=item.id,
            journey_id=str(journey_id),
            before=before,
            after={
                "status": "RESOLVED",
                "resolved_at": resolved_at,
                "reason": "FORECAST_CLEARED",
            },
        )
    session.flush()
    return tuple(resolved)


def _existing_provider_degradation(
    session: Session,
    *,
    journey_id: str,
    provider_key: str,
) -> Event | None:
    rows = session.scalars(
        select(Event).where(
            Event.journey_id == str(journey_id),
            Event.provider_key == provider_key,
            Event.provenance == "SYSTEM_DERIVED",
            Event.event_type == "SERVICE_UNAVAILABLE",
            Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
        )
    ).all()
    for item in rows:
        try:
            payload = json.loads(item.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if provider_key in payload.get("degraded_providers", []):
            return item
    return None


def record_provider_degradation(
    session: Session,
    *,
    journey_id: str,
    provider_key: str = "open-meteo",
    detail: str | None = None,
    occurred_at: str | None = None,
) -> Event:
    """Record a server-owned provider outage without inventing a weather fact.

    Repeated polling failures reuse the unresolved provider-state Event.
    """

    if provider_key != "open-meteo":
        raise ValueError("weather provider degradation must identify open-meteo")
    existing = _existing_provider_degradation(
        session, journey_id=str(journey_id), provider_key=provider_key
    )
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc).isoformat()
    item = Event(
        id=str(uuid4()),
        journey_id=str(journey_id),
        provider_key=provider_key,
        provenance="SYSTEM_DERIVED",
        event_type="SERVICE_UNAVAILABLE",
        severity="MAJOR",
        status="DETECTED",
        occurred_at=occurred_at or now,
        detected_at=now,
        payload_json=json.dumps(
            {
                "degraded_providers": [provider_key],
                "provider_status": "DEGRADED",
                "detail": detail,
            },
            sort_keys=True,
        ),
        created_at=now,
    )
    session.add(item)
    record_audit(
        session,
        action="journey.provider.degraded",
        entity_type="Event",
        entity_id=item.id,
        journey_id=str(journey_id),
        after={"provider_key": provider_key, "status": "DEGRADED"},
    )
    session.flush()
    return item


def resolve_provider_degradation(
    session: Session,
    *,
    journey_id: str,
    provider_key: str = "open-meteo",
    resolved_at: str | None = None,
) -> tuple[Event, ...]:
    """Resolve outstanding server-owned degradation events after recovery."""

    if provider_key != "open-meteo":
        raise ValueError("weather provider recovery must identify open-meteo")
    stamp = resolved_at or datetime.now(timezone.utc).isoformat()
    resolved: list[Event] = []
    rows = session.scalars(
        select(Event).where(
            Event.journey_id == str(journey_id),
            Event.provider_key == provider_key,
            Event.provenance == "SYSTEM_DERIVED",
            Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
        )
    ).all()
    for item in rows:
        try:
            payload = json.loads(item.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if provider_key not in payload.get("degraded_providers", []):
            continue
        item.status = "RESOLVED"
        item.resolved_at = stamp
        resolved.append(item)
        record_audit(
            session,
            action="journey.provider.recovered",
            entity_type="Event",
            entity_id=item.id,
            journey_id=str(journey_id),
            after={"provider_key": provider_key, "status": "RESOLVED"},
        )
    session.flush()
    return tuple(resolved)
