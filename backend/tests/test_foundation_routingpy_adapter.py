from dataclasses import dataclass

from app.integrations.mobility.routingpy_adapter import RoutingPyMobilityProvider


@dataclass
class FakeRoute:
    duration: float = 901.0
    distance: float = 1820.0
    geometry: str = "encoded"
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {"ok": True}


@dataclass
class FakeMatrix:
    durations: list = None

    def __post_init__(self):
        if self.durations is None:
            self.durations = [[0, 601], [602, 0]]


class FakeClient:
    def directions(self, *, locations, profile):
        assert locations == [[117.18, 39.1], [117.19, 39.105]]
        assert profile == "pedestrian"
        return FakeRoute()

    def matrix(self, *, locations, profile):
        assert len(locations) == 2
        return FakeMatrix()


PLACES = {
    "a": {"location": {"latitude": 39.1, "longitude": 117.18}},
    "b": {"location": {"latitude": 39.105, "longitude": 117.19}},
}


def test_legacy_estimate_minutes_and_rich_estimate():
    provider = RoutingPyMobilityProvider(
        provider_key="test-valhalla",
        client=FakeClient(),
        place_lookup=lambda key: PLACES[key],
    )
    estimate = provider.estimate("a", "b")
    assert estimate.duration_min == 16
    assert estimate.distance_m == 1820
    assert provider.estimate_minutes("a", "b") == 16
    assert provider.matrix_minutes(["a", "b"]) == [[0, 11], [11, 0]]
