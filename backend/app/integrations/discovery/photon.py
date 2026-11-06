from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class GeocodedPlace:
    external_id: str
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    city: str | None = None
    raw: dict[str, Any] | None = None


class PhotonGeocoder:
    provider_key = "photon"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str = "https://photon.komoot.io",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def search(
        self, query: str, *, limit: int = 8, lang: str | None = None
    ) -> list[GeocodedPlace]:
        if not query.strip():
            return []
        params: dict[str, Any] = {"q": query, "limit": limit}
        if lang:
            params["lang"] = lang
        response = await self._client.get(f"{self._base_url}/api/", params=params)
        response.raise_for_status()
        features = response.json().get("features", [])
        result: list[GeocodedPlace] = []
        for feature in features:
            geometry = feature.get("geometry", {})
            coordinates = geometry.get("coordinates", [])
            properties = feature.get("properties", {})
            if len(coordinates) < 2:
                continue
            name = properties.get("name") or properties.get("street") or properties.get("city")
            osm_id = properties.get("osm_id")
            osm_type = properties.get("osm_type", "unknown")
            if name is None or osm_id is None:
                continue
            result.append(
                GeocodedPlace(
                    external_id=f"{osm_type}:{osm_id}",
                    name=str(name),
                    latitude=float(coordinates[1]),
                    longitude=float(coordinates[0]),
                    country=properties.get("country"),
                    city=properties.get("city"),
                    raw=feature,
                )
            )
        return result
