from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx

from app.replanning.provider_snapshots import LIVE_PROVIDER_CAPTURE


@dataclass(frozen=True)
class ValhallaRouteEstimate:
    duration_min: int
    distance_m: int | None
    shape: str | None
    raw: dict[str, Any]
    geometry: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class ValhallaMatrixCell:
    duration_min: int
    distance_m: int | None
    raw: dict[str, Any]


class ValhallaProviderError(RuntimeError):
    """Controlled error for unavailable or malformed Valhalla responses."""


class ValhallaHTTPMobilityProvider:
    """Thin injectable adapter for a Valhalla HTTP service.

    It is intentionally independent of the deterministic feasibility gate. Route
    or matrix results should be frozen through ``capture_mobility_snapshot``
    before the gate consumes them.
    """

    provider_key = "valhalla-http"
    capture_provenance = "LIVE_PROVIDER_CAPTURE"
    time_basis = "VALHALLA_ROUTE_CAPTURED_AT"

    def __init__(
        self,
        *,
        client: httpx.Client,
        base_url: str,
        place_lookup: Callable[[str], Mapping[str, Any]],
        costing: str = "pedestrian",
        timeout_s: float = 10.0,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("Valhalla timeout must be positive")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._place_lookup = place_lookup
        self._costing = costing
        self._timeout_s = timeout_s

    def _location(self, place_id: str) -> dict[str, float]:
        item = self._place_lookup(place_id)
        location = item.get("location", item)
        return {"lat": float(location["latitude"]), "lon": float(location["longitude"])}

    def estimate(self, origin_place_id: str, destination_place_id: str) -> ValhallaRouteEstimate:
        payload = self._post(
            "/route",
            {
                "locations": [
                    self._location(origin_place_id),
                    self._location(destination_place_id),
                ],
                "costing": self._costing,
                "units": "kilometers",
            },
        )
        trip = payload.get("trip")
        summary = trip.get("summary") if isinstance(trip, dict) else None
        if not isinstance(summary, dict) or "time" not in summary:
            raise ValhallaProviderError("Valhalla route response has no valid summary time")
        try:
            seconds = float(summary["time"])
        except (TypeError, ValueError) as exc:
            raise ValhallaProviderError("Valhalla route time is not numeric") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise ValhallaProviderError("Valhalla returned invalid route time")
        length_km = summary.get("length")
        if length_km is not None:
            try:
                length_km = float(length_km)
            except (TypeError, ValueError) as exc:
                raise ValhallaProviderError("Valhalla route length is not numeric") from exc
            if not math.isfinite(length_km) or length_km < 0:
                raise ValhallaProviderError("Valhalla returned invalid route length")
        legs = trip.get("legs") or []
        shape = legs[0].get("shape") if legs and isinstance(legs[0], dict) else None
        return ValhallaRouteEstimate(
            duration_min=max(0, math.ceil(seconds / 60.0)),
            distance_m=None if length_km is None else round(float(length_km) * 1000),
            shape=shape,
            raw=payload,
            geometry=_decode_polyline6(shape),
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"{self._base_url}{path}", json=payload, timeout=self._timeout_s
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ValhallaProviderError(f"Valhalla {path} request failed") from exc
        if not isinstance(result, dict):
            raise ValhallaProviderError(f"Valhalla {path} response must be an object")
        return result

    def estimate_minutes(self, origin_place_id: str, destination_place_id: str) -> int:
        return self.estimate(origin_place_id, destination_place_id).duration_min

    def fact_quality(self, origin_place_id: str, destination_place_id: str) -> str:
        """Valhalla returns a measured route, so it attests LIVE explicitly.

        Hard feasibility trusts this declaration, not the provider's name
        (V4 #2).  If a request fails, the capture records a failure and the leg
        is unavailable — it never falls back to a synthetic default here.
        """

        return LIVE_PROVIDER_CAPTURE

    def status_capabilities(self) -> dict[str, Any]:
        """Read deployment capabilities before labelling traffic as live."""

        try:
            response = self._client.get(
                f"{self._base_url}/status", timeout=self._timeout_s
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ValhallaProviderError("Valhalla status request failed") from exc
        if not isinstance(payload, dict):
            raise ValhallaProviderError("Valhalla status response must be an object")
        raw_live = payload.get("has_live_traffic")
        if raw_live is None:
            raw_live = payload.get("live_traffic")
        return {
            "status": "AVAILABLE",
            "has_live_traffic": bool(raw_live) if raw_live is not None else False,
            "deployment": payload.get("version") or payload.get("tileset"),
            "raw_status": payload,
        }

    def matrix_minutes(self, place_ids: list[str]) -> list[list[int | None]]:
        return [
            [None if cell is None else cell.duration_min for cell in row]
            for row in self.matrix(place_ids)
        ]

    def matrix(self, place_ids: list[str]) -> list[list[ValhallaMatrixCell | None]]:
        locations = [self._location(place_id) for place_id in place_ids]
        payload = self._post(
            "/sources_to_targets",
            {
                "sources": locations,
                "targets": locations,
                "costing": self._costing,
                "verbose": False,
            },
        )
        raw_cells = payload.get("sources_to_targets")
        if isinstance(raw_cells, dict):
            nested_cells = raw_cells.get("sources_to_targets") or raw_cells.get("cells")
            if isinstance(nested_cells, list):
                raw_cells = nested_cells
            elif isinstance(raw_cells.get("durations"), list):
                return self._normalize_duration_matrix(
                    raw_cells["durations"],
                    len(place_ids),
                    raw_cells.get("distances"),
                )
        if isinstance(raw_cells, list):
            return self._normalize_cells(raw_cells, len(place_ids))
        raw_durations = payload.get("durations")
        if isinstance(raw_durations, list):
            return self._normalize_duration_matrix(
                raw_durations,
                len(place_ids),
                payload.get("distances"),
            )
        raise ValhallaProviderError("Valhalla matrix response does not contain route cells")

    @staticmethod
    def _normalize_duration_matrix(
        raw: list[Any], size: int, distances: Any = None
    ) -> list[list[ValhallaMatrixCell | None]]:
        if len(raw) != size:
            raise ValhallaProviderError("Valhalla matrix row count does not match request")
        matrix: list[list[ValhallaMatrixCell | None]] = []
        if distances is not None and (
            not isinstance(distances, list)
            or len(distances) != size
            or any(not isinstance(row, list) or len(row) != size for row in distances)
        ):
            raise ValhallaProviderError("Valhalla matrix distances must be square")
        for row_index, row in enumerate(raw):
            if not isinstance(row, list) or len(row) != size:
                raise ValhallaProviderError("Valhalla matrix durations must be square")
            normalized: list[ValhallaMatrixCell | None] = []
            for value in row:
                if value is None:
                    normalized.append(None)
                    continue
                try:
                    seconds = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValhallaProviderError("Valhalla matrix duration is not numeric") from exc
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValhallaProviderError("Valhalla matrix contains invalid duration")
                distance_m = None
                if distances is not None and distances[row_index][len(normalized)] is not None:
                    try:
                        distance_m = round(float(distances[row_index][len(normalized)]) * 1000)
                    except (TypeError, ValueError) as exc:
                        raise ValhallaProviderError("Valhalla matrix distance is not numeric") from exc
                normalized.append(
                    ValhallaMatrixCell(
                        duration_min=math.ceil(seconds / 60.0),
                        distance_m=distance_m,
                        raw={"time": value},
                    )
                )
            matrix.append(normalized)
        return matrix

    @staticmethod
    def _normalize_cells(
        raw: list[Any], size: int
    ) -> list[list[ValhallaMatrixCell | None]]:
        if len(raw) != size:
            raise ValhallaProviderError("Valhalla matrix row count does not match request")
        matrix: list[list[ValhallaMatrixCell | None]] = [
            [None for _ in range(size)] for _ in range(size)
        ]
        for source_index, row in enumerate(raw):
            if not isinstance(row, list):
                raise ValhallaProviderError("Valhalla matrix route cells must be rows")
            for positional_index, cell in enumerate(row):
                if cell is None:
                    continue
                if isinstance(cell, dict):
                    target_index = int(cell.get("to_index", positional_index))
                    seconds_value = cell.get("time", cell.get("duration"))
                    distance_value = cell.get("distance")
                    raw_cell = dict(cell)
                else:
                    target_index = positional_index
                    seconds_value = cell
                    distance_value = None
                    raw_cell = {"time": cell}
                if not 0 <= target_index < size:
                    raise ValhallaProviderError("Valhalla matrix target index is out of range")
                try:
                    seconds = float(seconds_value)
                except (TypeError, ValueError) as exc:
                    raise ValhallaProviderError("Valhalla matrix duration is not numeric") from exc
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValhallaProviderError("Valhalla matrix contains invalid duration")
                distance_m = None
                if distance_value is not None:
                    try:
                        distance_m = round(float(distance_value) * 1000)
                    except (TypeError, ValueError) as exc:
                        raise ValhallaProviderError("Valhalla matrix distance is not numeric") from exc
                    if distance_m < 0:
                        raise ValhallaProviderError("Valhalla matrix contains invalid distance")
                matrix[source_index][target_index] = ValhallaMatrixCell(
                    duration_min=math.ceil(seconds / 60.0),
                    distance_m=distance_m,
                    raw=raw_cell,
                )
        return matrix


def _decode_polyline6(shape: str | None) -> tuple[tuple[float, float], ...]:
    """Decode Valhalla's encoded polyline6 into immutable GeoJSON coordinates."""

    if not shape:
        return ()
    points: list[tuple[float, float]] = []
    index = 0
    latitude = 0
    longitude = 0
    try:
        while index < len(shape):
            values: list[int] = []
            for _ in range(2):
                shift = 0
                result = 0
                while True:
                    if index >= len(shape):
                        raise ValueError("truncated polyline")
                    byte = ord(shape[index]) - 63
                    index += 1
                    result |= (byte & 0x1F) << shift
                    shift += 5
                    if byte < 0x20:
                        break
                    if shift > 35:
                        raise ValueError("polyline varint too long")
                values.append(-(result >> 1) if result & 1 else result >> 1)
            latitude += values[0]
            longitude += values[1]
            point = (longitude / 1_000_000, latitude / 1_000_000)
            if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                return ()
            points.append(point)
    except (TypeError, ValueError):
        return ()
    return tuple(points) if len(points) >= 2 else ()
