from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

from app.integrations.mobility.routingpy_adapter import TravelEstimate
from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, SYNTHETIC_DEFAULT

MOCK_ROOT = Path(__file__).resolve().parents[4] / "data" / "mock"


class MockCityDataProvider:
    provider_key = "mock-city"

    def places(self) -> list[dict]:
        return json.loads((MOCK_ROOT / "places.json").read_text(encoding="utf-8"))

    def city_services(self) -> list[dict]:
        return json.loads((MOCK_ROOT / "city_services.json").read_text(encoding="utf-8"))


class MockMobilityProvider:
    provider_key = "mock-mobility"

    def __init__(
        self,
        place_lookup: Callable[[str], Mapping[str, object]] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        """Deterministic fixture-backed mobility provider.

        ``strict`` distinguishes an *explicit fixture fact* from the fixture's
        blanket ``default_minutes`` fallback.  The fallback is a constant, not a
        measurement, so it must never become a hard travel fact in replan
        feasibility (T2 / docs/MOBILITY_TRUTH_CONTRACT.md).  With ``strict=True``
        a pair absent from the explicit matrix raises ``KeyError`` instead of
        silently returning the constant; the caller's fail-closed path then
        reports ``MOBILITY_DATA_UNAVAILABLE``.

        The non-strict default preserves the seeded offline demo paths, which
        intentionally rely on the fixture default.
        """

        self._fixture = json.loads((MOCK_ROOT / "mobility_matrix.json").read_text(encoding="utf-8"))
        self._place_lookup = place_lookup
        self._strict = strict

    def estimate_minutes(self, origin_place_id: str, destination_place_id: str) -> int:
        key = f"{origin_place_id}|{destination_place_id}"
        explicit = self._fixture["minutes"].get(key)
        if explicit is None:
            if self._strict:
                raise KeyError(f"no explicit mobility fact for {key}")
            return int(self._fixture["default_minutes"])
        return int(explicit)

    def fact_quality(self, origin_place_id: str, destination_place_id: str) -> str:
        """Whether a pair is an explicit fixture fact or the blanket default.

        ``SYNTHETIC_DEFAULT`` marks the constant that must never become a hard
        replan-travel fact; ``EXPLICIT_FROZEN_FIXTURE`` marks a genuine fixture
        entry that demo paths may still use (T2 / B1).
        """

        key = f"{origin_place_id}|{destination_place_id}"
        if self._fixture["minutes"].get(key) is None:
            return SYNTHETIC_DEFAULT
        return EXPLICIT_FROZEN_FIXTURE

    def estimate(self, origin_place_id: str, destination_place_id: str) -> TravelEstimate:
        """Return a frozen, deterministic route geometry for offline demos.

        This is intentionally still marked ``mock-mobility`` in the snapshot;
        the UI must show that provenance rather than presenting it as Valhalla.
        """

        minutes = self.estimate_minutes(origin_place_id, destination_place_id)
        geometry = None
        if self._place_lookup is not None:
            origin = self._place_lookup(origin_place_id)
            destination = self._place_lookup(destination_place_id)
            origin_location = origin.get("location", origin)
            destination_location = destination.get("location", destination)
            origin_point = (
                float(origin_location["longitude"]),
                float(origin_location["latitude"]),
            )
            destination_point = (
                float(destination_location["longitude"]),
                float(destination_location["latitude"]),
            )
            geometry = [
                origin_point,
                (
                    (origin_point[0] + destination_point[0]) / 2,
                    (origin_point[1] + destination_point[1]) / 2,
                ),
                destination_point,
            ]
        return TravelEstimate(
            provider_key=self.provider_key,
            duration_min=minutes,
            geometry=geometry,
        )
