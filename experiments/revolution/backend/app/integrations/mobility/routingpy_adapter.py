from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float

    def routingpy(self) -> list[float]:
        return [self.longitude, self.latitude]


@dataclass(frozen=True)
class TravelEstimate:
    provider_key: str
    duration_min: int
    distance_m: int | None = None
    geometry: Any | None = None
    raw: Any | None = None


class RoutingPyMobilityProvider:
    """Compatibility adapter for CityNexus's current `estimate_minutes` port.

    The routingpy client is injected, which keeps tests deterministic and lets
    CityNexus swap Valhalla, GraphHopper, OSRM or OTP without changing the
    domain service. Real-network construction belongs in the provider registry.
    """

    def __init__(
        self,
        *,
        provider_key: str,
        client: Any,
        place_lookup: Callable[[str], Mapping[str, Any]],
        profile: str = "pedestrian",
    ) -> None:
        self.provider_key = provider_key
        self._client = client
        self._place_lookup = place_lookup
        self._profile = profile

    def _point(self, place_id: str) -> GeoPoint:
        item = self._place_lookup(place_id)
        location = item.get("location", item)
        return GeoPoint(
            latitude=float(location["latitude"]),
            longitude=float(location["longitude"]),
        )

    def estimate(self, origin_place_id: str, destination_place_id: str) -> TravelEstimate:
        origin = self._point(origin_place_id)
        destination = self._point(destination_place_id)
        route = self._client.directions(
            locations=[origin.routingpy(), destination.routingpy()],
            profile=self._profile,
        )
        seconds = float(route.duration)
        if seconds < 0:
            raise ValueError("routing provider returned negative duration")
        distance = (
            None if getattr(route, "distance", None) is None else round(float(route.distance))
        )
        return TravelEstimate(
            provider_key=self.provider_key,
            duration_min=max(0, math.ceil(seconds / 60.0)),
            distance_m=distance,
            geometry=getattr(route, "geometry", None),
            raw=getattr(route, "raw", None),
        )

    def estimate_minutes(self, origin_place_id: str, destination_place_id: str) -> int:
        return self.estimate(origin_place_id, destination_place_id).duration_min

    def matrix_minutes(self, place_ids: list[str]) -> list[list[int | None]]:
        locations = [self._point(place_id).routingpy() for place_id in place_ids]
        matrix = self._client.matrix(locations=locations, profile=self._profile)
        result: list[list[int | None]] = []
        for row in matrix.durations:
            result.append(
                [None if value is None else max(0, math.ceil(float(value) / 60.0)) for value in row]
            )
        return result


def build_routingpy_client(
    router_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    """Lazy factory using routingpy's own router registry.

    Supported routingpy names currently include `valhalla`, `graphhopper`,
    `opentripplanner`/`otp` and `osrm`. Import is delayed so the core MVP does
    not require routingpy until the real-provider phase is enabled.
    """

    try:
        from routingpy.routers import get_router_by_name
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("routingpy is not installed") from exc

    router_cls = get_router_by_name(router_name)
    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    return router_cls(**kwargs)
