from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from app.integrations.context.open_meteo import WeatherRisk


@dataclass(frozen=True)
class WeatherQueryScope:
    """Exact provider query scope that produced a weather fact."""

    place_id: str
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not self.place_id:
            raise ValueError("weather query scope requires place_id")
        if not -90 <= float(self.latitude) <= 90:
            raise ValueError("weather query latitude is out of range")
        if not -180 <= float(self.longitude) <= 180:
            raise ValueError("weather query longitude is out of range")

    def to_evidence(self) -> dict[str, Any]:
        return {
            "place_id": self.place_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


@dataclass(frozen=True)
class WeatherRiskFact:
    code: str
    severity: str
    at: str
    provider_key: str
    evidence: Mapping[str, Any]
    valid_until: str | None = None
    scope: WeatherQueryScope | None = None
    snapshot_captured_at: str | None = None

    def __post_init__(self) -> None:
        start = datetime.fromisoformat(self.at)
        if start.tzinfo is None:
            raise ValueError("weather fact timestamp must be timezone-aware")
        normalized_start = start.astimezone(timezone.utc)
        object.__setattr__(self, "at", normalized_start.isoformat())
        if self.valid_until is not None:
            end = datetime.fromisoformat(self.valid_until)
            if end.tzinfo is None:
                raise ValueError("weather fact validity end must be timezone-aware")
            if end <= start:
                raise ValueError("weather fact validity interval must be positive")
            object.__setattr__(self, "valid_until", end.astimezone(timezone.utc).isoformat())
        if self.snapshot_captured_at is not None:
            captured = datetime.fromisoformat(self.snapshot_captured_at)
            if captured.tzinfo is None:
                raise ValueError("weather fact capture timestamp must be timezone-aware")
            object.__setattr__(
                self, "snapshot_captured_at", captured.astimezone(timezone.utc).isoformat()
            )
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    @property
    def valid_from(self) -> str:
        return self.at

    def to_evidence(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "valid_from": self.at,
            "valid_until": self.valid_until,
            "provider": self.provider_key,
            "snapshot_captured_at": self.snapshot_captured_at,
            "provider_evidence": dict(self.evidence),
            "query_scope": self.scope.to_evidence() if self.scope is not None else None,
        }


@dataclass(frozen=True)
class WeatherSnapshot:
    provider_key: str
    captured_at: str
    facts: tuple[WeatherRiskFact, ...]
    degraded: bool = False
    scope: WeatherQueryScope | None = None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "provider": self.provider_key,
            "captured_at": self.captured_at,
            "degraded": self.degraded,
            "query_scope": self.scope.to_evidence() if self.scope is not None else None,
            "facts": [fact.to_evidence() for fact in self.facts],
        }


def freeze_weather_risks(
    risks: Iterable[WeatherRisk],
    *,
    provider_key: str = "open-meteo",
    captured_at: str | None = None,
    degraded: bool = False,
    scope: WeatherQueryScope | None = None,
    bucket_minutes: int = 60,
) -> WeatherSnapshot:
    """Freeze external weather output as scoped half-open validity buckets."""

    if bucket_minutes <= 0:
        raise ValueError("weather bucket_minutes must be positive")
    captured = datetime.fromisoformat(captured_at) if captured_at else datetime.now(timezone.utc)
    if captured.tzinfo is None:
        raise ValueError("weather snapshot capture timestamp must be timezone-aware")
    stamp = captured.astimezone(timezone.utc).isoformat()
    facts = tuple(
        sorted(
            (
                WeatherRiskFact(
                    code=str(item.code),
                    severity=str(item.severity),
                    at=item.at.isoformat(),
                    valid_until=(item.at + timedelta(minutes=bucket_minutes)).isoformat(),
                    provider_key=provider_key,
                    evidence=dict(item.evidence),
                    scope=scope,
                    snapshot_captured_at=stamp,
                )
                for item in risks
            ),
            key=lambda fact: (fact.at, fact.code, fact.severity),
        )
    )
    return WeatherSnapshot(
        provider_key=provider_key,
        captured_at=stamp,
        facts=facts,
        degraded=degraded,
        scope=scope,
    )


def provider_verified_event_payload(fact: WeatherRiskFact) -> dict[str, Any]:
    """Return server-owned evidence for an Event ingestion service.

    This is deliberately not a public EventCreate payload. Public clients must
    not be able to forge PROVIDER_VERIFIED provenance.
    """

    return {
        "provider_key": fact.provider_key,
        "provenance": "PROVIDER_VERIFIED",
        "event_type": "WEATHER",
        "severity": fact.severity,
        "occurred_at": fact.snapshot_captured_at or fact.at,
        "payload": {
            "reason_code": fact.code,
            "snapshot_captured_at": fact.snapshot_captured_at,
            "forecast_valid_from": fact.at,
            "forecast_valid_until": fact.valid_until,
            "provider_evidence": dict(fact.evidence),
            "query_scope": fact.scope.to_evidence() if fact.scope is not None else None,
        },
    }
