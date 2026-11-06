from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import httpx

AMAP_BASE_URL = "https://restapi.amap.com"
MODE_PATHS = {
    "WALK": "walking",
    "BICYCLE": "bicycling",
    "TRANSIT": "transit/integrated",
    "AUTO": "driving",
}


@dataclass(frozen=True)
class ProviderStatus:
    provider_key: str
    status: str
    provenance: str
    coordinate_crs: str
    reason: str | None = None


@dataclass(frozen=True)
class NormalizedRoute:
    mode: str
    duration_min: int
    distance_m: int
    cost_amount_minor: int | None
    currency: str | None
    cost_status: str
    walking_min: int
    transfers: int
    reliability_score: float
    geometry: dict[str, Any]
    source_crs: str
    domain_crs: str
    conversion: str
    provider_key: str = "amap-web-service"


def amap_status() -> ProviderStatus:
    configured = bool(os.getenv("AMAP_WEB_KEY"))
    return ProviderStatus(
        provider_key="amap-web-service",
        status="READY" if configured else "NOT_CONFIGURED",
        provenance="LIVE" if configured else "UNAVAILABLE",
        coordinate_crs="GCJ02",
        reason=None if configured else "AMAP_WEB_KEY is absent",
    )


def provider_for_region(city_code: str) -> str:
    return (
        "amap-web-service" if str(city_code).upper().startswith("CN-") else "global-provider-mesh"
    )


def _outside_china(latitude: float, longitude: float) -> bool:
    return longitude < 72.004 or longitude > 137.8347 or latitude < 0.8293 or latitude > 55.8271


def _transform(latitude: float, longitude: float) -> tuple[float, float]:
    x = longitude - 105.0
    y = latitude - 35.0
    dlat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    dlon = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    dlat += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    dlon += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    dlat += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    dlon += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    dlat += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    dlon += (
        (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    )
    radius = 6378245.0
    eccentricity = 0.006693421622965943
    radlat = latitude / 180.0 * math.pi
    magic = 1 - eccentricity * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = dlat * 180.0 / ((radius * (1 - eccentricity)) / (magic * sqrtmagic) * math.pi)
    dlon = dlon * 180.0 / (radius / sqrtmagic * math.cos(radlat) * math.pi)
    return latitude + dlat, longitude + dlon


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    if _outside_china(latitude, longitude):
        return latitude, longitude
    return _transform(latitude, longitude)


def gcj02_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    if _outside_china(latitude, longitude):
        return latitude, longitude
    guess_lat, guess_lon = latitude, longitude
    for _ in range(8):
        transformed_lat, transformed_lon = wgs84_to_gcj02(guess_lat, guess_lon)
        guess_lat -= transformed_lat - latitude
        guess_lon -= transformed_lon - longitude
    return guess_lat, guess_lon


def _parse_polyline(polyline: str) -> list[list[float]]:
    points: list[list[float]] = []
    for raw in polyline.split(";"):
        if not raw:
            continue
        longitude, latitude = (float(value) for value in raw.split(",", 1))
        wgs_lat, wgs_lon = gcj02_to_wgs84(latitude, longitude)
        points.append([wgs_lon, wgs_lat])
    return points


class AmapPoiProvider:
    provider_key = "amap-web-service"

    async def search(self, query: str, city: str, *, limit: int = 10) -> list[dict[str, Any]]:
        key = os.getenv("AMAP_WEB_KEY")
        if not key:
            raise RuntimeError("AMAP_WEB_KEY is not configured")
        params = {
            "key": key,
            "keywords": query,
            "region": city,
            "page_size": min(limit, 25),
            "show_fields": "business",
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(f"{AMAP_BASE_URL}/v5/place/text", params=params)
            response.raise_for_status()
            payload = response.json()
        if str(payload.get("status")) != "1":
            raise RuntimeError(str(payload.get("info") or "Amap POI request failed"))
        result = []
        for item in payload.get("pois", [])[:limit]:
            longitude, latitude = (float(value) for value in str(item["location"]).split(",", 1))
            wgs_lat, wgs_lon = gcj02_to_wgs84(latitude, longitude)
            result.append(
                {
                    "external_id": item.get("id"),
                    "name": item.get("name"),
                    "latitude": wgs_lat,
                    "longitude": wgs_lon,
                    "source_crs": "GCJ02",
                    "domain_crs": "WGS84",
                    "conversion": "ITERATIVE_GCJ02_TO_WGS84",
                    "provider_key": self.provider_key,
                }
            )
        return result


class AmapMobilityProvider:
    provider_key = "amap-web-service"

    async def route(
        self,
        mode: str,
        *,
        origin_wgs84: tuple[float, float],
        destination_wgs84: tuple[float, float],
        city_code: str,
    ) -> NormalizedRoute:
        normalized_mode = mode.upper()
        path = MODE_PATHS.get(normalized_mode)
        key = os.getenv("AMAP_WEB_KEY")
        if not key:
            raise RuntimeError("AMAP_WEB_KEY is not configured")
        if not path:
            raise ValueError(f"Amap does not provide mode {mode}")
        origin_lat, origin_lon = wgs84_to_gcj02(*origin_wgs84)
        dest_lat, dest_lon = wgs84_to_gcj02(*destination_wgs84)
        params = {
            "key": key,
            "origin": f"{origin_lon},{origin_lat}",
            "destination": f"{dest_lon},{dest_lat}",
            "city1": city_code,
            "city2": city_code,
            "show_fields": "cost,polyline",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{AMAP_BASE_URL}/v5/direction/{path}", params=params)
            response.raise_for_status()
            payload = response.json()
        if str(payload.get("status")) != "1":
            raise RuntimeError(str(payload.get("info") or "Amap route request failed"))
        route = payload.get("route", {})
        paths = route.get("paths") or route.get("transits") or []
        if not paths:
            raise RuntimeError("Amap returned no route path")
        selected = paths[0]
        steps = selected.get("steps") or []
        polyline = ";".join(str(step.get("polyline") or "") for step in steps)
        distance = int(float(selected.get("distance") or 0))
        duration = max(
            1,
            math.ceil(
                float(selected.get("cost", {}).get("duration") or selected.get("duration") or 60)
                / 60
            ),
        )
        taxi_cost = selected.get("cost", {}).get("taxi_fee")
        cost = int(float(taxi_cost) * 100) if taxi_cost not in (None, "") else None
        cost_status = (
            "ESTIMATED"
            if cost is not None
            else ("FREE_KNOWN" if normalized_mode == "WALK" else "UNKNOWN")
        )
        return NormalizedRoute(
            mode=normalized_mode,
            duration_min=duration,
            distance_m=distance,
            cost_amount_minor=cost,
            currency="CNY" if cost is not None else None,
            cost_status=cost_status,
            walking_min=duration if normalized_mode == "WALK" else 0,
            transfers=0,
            reliability_score=0.9,
            geometry={
                "type": "Feature",
                "properties": {"source_crs": "GCJ02", "domain_crs": "WGS84"},
                "geometry": {"type": "LineString", "coordinates": _parse_polyline(polyline)},
            },
            source_crs="GCJ02",
            domain_crs="WGS84",
            conversion="ITERATIVE_GCJ02_TO_WGS84",
        )


class AmapTrafficProvider:
    provider_key = "amap-web-service"

    def status(self) -> ProviderStatus:
        return amap_status()
