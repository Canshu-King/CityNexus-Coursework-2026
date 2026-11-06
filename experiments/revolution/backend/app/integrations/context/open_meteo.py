from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(frozen=True)
class WeatherObservation:
    at: datetime
    precipitation_probability: int | None
    weather_code: int | None


@dataclass(frozen=True)
class WeatherRisk:
    code: str
    severity: str
    at: datetime
    evidence: dict[str, Any]


class OpenMeteoProviderError(RuntimeError):
    """Controlled error for unavailable or malformed Open-Meteo responses."""


class OpenMeteoContextProvider:
    provider_key = "open-meteo"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str = "https://api.open-meteo.com",
        rain_probability_threshold: int = 70,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._rain_threshold = rain_probability_threshold

    async def forecast(self, *, latitude: float, longitude: float) -> list[WeatherObservation]:
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "hourly": "precipitation_probability,weather_code",
                    # Keep provider timestamps deterministic. Unix timestamps
                    # are absolute UTC instants and avoid silently treating
                    # local wall-clock strings as timezone-free facts.
                    "timezone": "UTC",
                    "timeformat": "unixtime",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OpenMeteoProviderError("Open-Meteo forecast request failed") from exc
        if not isinstance(payload, dict):
            raise OpenMeteoProviderError("Open-Meteo forecast response must be an object")
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise OpenMeteoProviderError("Open-Meteo forecast response has no hourly object")
        times = hourly.get("time", [])
        probabilities = hourly.get("precipitation_probability", [])
        codes = hourly.get("weather_code", [])
        if (
            not isinstance(times, list)
            or not isinstance(probabilities, list)
            or not isinstance(codes, list)
        ):
            raise OpenMeteoProviderError("Open-Meteo hourly fields must be arrays")
        if not (len(times) == len(probabilities) == len(codes)):
            raise OpenMeteoProviderError("Open-Meteo hourly arrays must have identical lengths")
        observations: list[WeatherObservation] = []
        for index, raw_time in enumerate(times):
            probability = probabilities[index] if index < len(probabilities) else None
            code = codes[index] if index < len(codes) else None
            try:
                if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
                    at = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
                else:
                    text = str(raw_time).strip()
                    # Compatible fixtures may still return ISO strings. Because
                    # this adapter requests UTC, naive ISO is interpreted as UTC.
                    at = datetime.fromisoformat(text)
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                normalized_probability = None if probability is None else int(probability)
                normalized_code = None if code is None else int(code)
            except (TypeError, ValueError) as exc:
                raise OpenMeteoProviderError("Open-Meteo hourly value is malformed") from exc
            if normalized_probability is not None and not 0 <= normalized_probability <= 100:
                raise OpenMeteoProviderError("Open-Meteo precipitation probability is out of range")
            if normalized_code is not None and normalized_code < 0:
                raise OpenMeteoProviderError("Open-Meteo weather code is invalid")
            observations.append(
                WeatherObservation(
                    at=at,
                    precipitation_probability=normalized_probability,
                    weather_code=normalized_code,
                )
            )
        return observations

    def risks(self, observations: list[WeatherObservation]) -> list[WeatherRisk]:
        risks: list[WeatherRisk] = []
        for item in observations:
            if (
                item.precipitation_probability is not None
                and item.precipitation_probability >= self._rain_threshold
            ):
                risks.append(
                    WeatherRisk(
                        code="HEAVY_RAIN_RISK",
                        severity="HIGH",
                        at=item.at,
                        evidence={
                            "precipitation_probability": item.precipitation_probability,
                            "weather_code": item.weather_code,
                        },
                    )
                )
        return risks
