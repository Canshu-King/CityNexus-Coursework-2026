"""ORD-0049 - the cold stay path must say where its time went and why it is empty.

Two separate claims are tested here, because the incident mixed them up:

* **Where the seconds go.** `POST /stay-options/generate` on a cold city was measured at
  114.98s, and every candidate explanation (provider, geocoder, routing, solver, database)
  was a guess until each segment was timed. The spans nest, so the parent proves the
  children are inside it rather than beside it.
* **Why an empty answer is empty.** HTTP 200 with `top_k: []` used to be reachable from at
  least four different states - no nights needed, provider unreachable, city unmapped, and
  every hotel conflicting with the itinerary - which is why the screen could only draw a
  blank. Each now has its own outcome code, and the provider attempts come with it.

Timing is opt-in through `CITYNEXUS_PROVIDER_TIMING`; the off case is asserted too, so the
instrumentation cannot quietly become part of the contract.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import phase_timing
from app.integrations import stay_osm
from app.main import app
from app import stay_mobility
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """A provider that is slow and empty on purpose, with the shape of a real one.

    Sleeping (rather than returning instantly) is the point: a span that measures ~0ms
    cannot distinguish "answered nothing" from "never asked".
    """

    def __init__(self, *, delay_s: float = 0.0, fail_overpass: bool = True, photon=True):
        self.delay_s = delay_s
        self.fail_overpass = fail_overpass
        self.photon = photon
        self.calls: list[str] = []

    def _sleep(self) -> None:
        if self.delay_s:
            time.sleep(self.delay_s)

    def get(self, url, params=None, headers=None):
        self.calls.append(f"GET {url.split('//', 1)[-1].split('/', 1)[0]}")
        self._sleep()
        if "photon" in url:
            if not self.photon:
                raise httpx.ConnectError("photon unreachable")
            return _Response({"features": [{"geometry": {"coordinates": [118.1, 24.5]}}]})
        return _Response([])  # nominatim: a real, empty answer

    def post(self, url, data=None, headers=None):
        self.calls.append(f"POST {url.split('//', 1)[-1].split('/', 1)[0]}")
        self._sleep()
        if self.fail_overpass:
            raise httpx.TimeoutException("overpass did not answer")
        return _Response(
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 9001,
                        "lat": 24.49,
                        "lon": 118.12,
                        "tags": {"name": "Fake Hotel One", "tourism": "hotel"},
                    },
                    {
                        "type": "node",
                        "id": 9002,
                        "lat": 24.51,
                        "lon": 118.14,
                        "tags": {"name": "Fake Hotel Two", "tourism": "guest_house"},
                    },
                ]
            }
        )

    def close(self) -> None:
        return None


def _use_http(monkeypatch, http: _FakeHttp) -> None:
    real = stay_osm.OsmAccommodationProvider
    monkeypatch.setattr(
        stay_mobility, "OsmAccommodationProvider", lambda: real(client=http)
    )


def _cold_journey(
    city_code: str = "CN-XIAMEN", days: int = 3, stay_budget: dict | None = None
) -> str:
    traveler = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]
    start = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{city_code} {days}d",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=days - 1, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
            **({"stay_budget": stay_budget} if stay_budget else {}),
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _generate(journey_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate", json={})


def test_timing_is_off_by_default_and_adds_nothing_to_the_response(monkeypatch) -> None:
    monkeypatch.delenv("CITYNEXUS_PROVIDER_TIMING", raising=False)
    _use_http(monkeypatch, _FakeHttp())
    assert phase_timing.timing_enabled() is False
    # With no trace, `phase` must be a no-op that yields nothing rather than an empty object
    # the instrumented code has to guard against.
    with phase_timing.phase("anything") as handle:
        assert handle is None

    journey_id = _cold_journey()
    response = _generate(journey_id)
    assert response.status_code == 200, response.text
    assert "X-CityNexus-Phase-Ms" not in response.headers


def test_the_cold_path_reports_where_its_time_went(monkeypatch) -> None:
    monkeypatch.setenv("CITYNEXUS_PROVIDER_TIMING", "1")
    _use_http(monkeypatch, _FakeHttp(delay_s=0.05))
    journey_id = _cold_journey()
    response = _generate(journey_id)

    assert response.status_code == 200, response.text
    header = response.headers.get("X-CityNexus-Phase-Ms", "")
    def ms_of(row: str) -> float:
        # `render_header` emits `name=milliseconds:outcome`; parsed, not guessed at.
        return float(row.split("=", 1)[1].split(":", 1)[0])

    spans = {row.split("=", 1)[0]: row for row in header.split(",") if "=" in row}
    for name in ("stay.generate", "stay.offers", "provider.photon", "provider.overpass"):
        assert name in spans, f"{name} missing from {header}"
    mirror_rows = [row for row in header.split(",") if row.startswith("provider.overpass.mirror[")]
    assert len(mirror_rows) == 3, f"expected one span per mirror, got {mirror_rows}"
    # Each mirror really waited, and the parent really contains them - that is the whole
    # difference between attribution and a guess about which layer is slow.
    for row in mirror_rows:
        assert ms_of(row) >= 45.0, row
    parent = ms_of(spans["provider.overpass"])
    children = sum(ms_of(row) for row in mirror_rows)
    assert parent >= children - 5.0, f"parent {parent}ms is smaller than its children {children}ms"


def test_an_unanswered_provider_is_reported_as_no_offers_not_as_success(monkeypatch) -> None:
    monkeypatch.delenv("CITYNEXUS_PROVIDER_TIMING", raising=False)
    _use_http(monkeypatch, _FakeHttp())
    journey_id = _cold_journey()
    response = _generate(journey_id)
    body = response.json()

    assert response.status_code == 200 and body["top_k"] == []
    generation = body["generation"]
    # Before this change this was indistinguishable from "Xiamen has no hotels".
    assert generation["outcome"] == "NO_OFFERS_FROM_PROVIDER", generation
    assert generation["offers_from_provider"] == 0
    hosts = [item["target"] for item in generation["provider_attempts"] if item["step"] == "overpass"]
    assert len(hosts) == 3 and hosts[0] != hosts[-1], generation["provider_attempts"]
    assert all(
        item["outcome"] == "TimeoutException"
        for item in generation["provider_attempts"]
        if item["step"] == "overpass"
    ), generation["provider_attempts"]


def test_a_trip_that_needs_no_stay_says_that_instead_of_looking_like_a_failure(monkeypatch) -> None:
    monkeypatch.delenv("CITYNEXUS_PROVIDER_TIMING", raising=False)
    _use_http(monkeypatch, _FakeHttp())
    journey_id = _cold_journey(days=1)
    body = _generate(journey_id).json()

    assert body["top_k"] == []
    assert body["generation"]["outcome"] == "NOT_APPLICABLE_ZERO_NIGHTS", body["generation"]


def test_a_real_provider_answer_on_a_fixture_city_still_reports_what_it_used(monkeypatch) -> None:
    monkeypatch.delenv("CITYNEXUS_PROVIDER_TIMING", raising=False)
    # Tianjin has a sealed fixture, so the OSM provider must not be constructed at all;
    # the generation account has to say which source answered.
    journey_id = _cold_journey(city_code="CN-TIANJIN")
    body = _generate(journey_id).json()

    assert body["generation"]["outcome"] == "OPTIONS_FOUND", body["generation"]
    assert body["generation"]["options_ranked"] == len(body["top_k"]) or len(body["top_k"]) < len(
        body["generation"]["provider_attempts"]
    )
    assert any(
        item["step"] == "provider" and "fixture=yes" in str(item.get("detail", ""))
        for item in body["generation"]["provider_attempts"]
    ), body["generation"]["provider_attempts"]


def test_every_hotel_conflicting_with_the_itinerary_is_named_as_rejected(monkeypatch) -> None:
    monkeypatch.delenv("CITYNEXUS_PROVIDER_TIMING", raising=False)
    _use_http(monkeypatch, _FakeHttp(fail_overpass=False))
    # ORD-0355: naming a money constraint needs a budget the traveller declared; an
    # undeclared one now reports STAY_BUDGET_NOT_DECLARED instead of this.
    journey_id = _cold_journey(stay_budget={"amount_minor": 30000, "currency": "CNY"})

    def _conflict(*_args, **_kwargs):
        raise stay_mobility.StayItineraryConflict("pinned window")

    monkeypatch.setattr(stay_mobility, "build_candidate_nodes", _conflict)
    body = _generate(journey_id).json()

    assert body["top_k"] == []
    generation = body["generation"]
    # Offers existed and were all rejected: a different fact from "the provider said nothing",
    # and the reason the traveller's own dates are in the way is only visible here.
    assert generation["outcome"] == "ALL_CANDIDATES_REJECTED", generation
    assert generation["offers_from_provider"] > 0, generation
    assert generation["rejections_by_reason"].get("STAY_ITINERARY_CONFLICT"), generation


def test_an_exception_inside_a_span_is_recorded_and_still_propagates(monkeypatch) -> None:
    monkeypatch.setenv("CITYNEXUS_PROVIDER_TIMING", "1")
    trace = phase_timing.begin_trace()
    with pytest.raises(ZeroDivisionError):
        with phase_timing.phase("boom"):
            raise ZeroDivisionError("deliberate")
    phase_timing.end_trace()

    assert trace is not None
    assert [span.outcome for span in trace.spans] == ["error:ZeroDivisionError"]
    # A measured failure must not become a swallowed one.
    assert trace.spans[0].ms >= 0.0
