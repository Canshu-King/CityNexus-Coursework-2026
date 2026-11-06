from __future__ import annotations

from typing import Protocol


class CityDataProvider(Protocol):
    def places(self) -> list[dict]: ...

    def city_services(self) -> list[dict]: ...


class MobilityProvider(Protocol):
    def estimate_minutes(self, origin_place_id: str, destination_place_id: str) -> int: ...
