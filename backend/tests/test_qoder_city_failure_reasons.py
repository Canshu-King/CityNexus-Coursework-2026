"""ORD-0099 - a live provider failure has to arrive named, and asked in the traveller's words.

Three things are pinned here, one per clause of the order, and the last one keeps the other
two honest:

* **Same words, both geocode calls.** `real_city.py` grounded the city anchor with a stripped
  label ("TIANJIN") and then asked its bounded fallback with `journey.city_code` - i.e. it
  searched "tea shops, CN-TIANJIN", a string no traveller ever types. Both calls are now
  recorded and compared, so the two cannot drift apart again.
* **Named reasons, no exception class on any surface.** Stable codes replace
  `type(exc).__name__`, chosen from the failure's *shape* (timeout vs unreachable vs 429 vs a
  malformed answer vs nothing found vs our own bad request). The class name and message go to
  the log, where an operator reads them. `PROVIDER_NO_RESULTS` exists so a provider that
  answered "nothing" is not reported as down, and
  `RESOLVER_LABEL_CARRIES_INTERNAL_CODE` so our own malformed request is not blamed on
  somebody else's outage.
* **The default resolver is still the Nominatim client, and the Tianjin replay fallback still
  answers.** Swapping the default resolver is a contract decision (bus R1), and silently
  flipping behaviour because some environment value exists is exactly the implicit switch the
  order banned. What is observable now is *why* a live attempt failed and at which of the
  three provider calls - the evidence a later decision has to be argued from.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.integrations.discovery import real_city
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))

CLASS_NAMES = (
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "HTTPStatusError",
    "TimeoutException",
    "KeyError",
    "TypeError",
    "ValueError",
    "AttributeError",
    "RuntimeError",
)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/search")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"server returned {status}", request=request, response=response)


def _anchor_row() -> real_city.DiscoveryCandidate:
    return real_city.DiscoveryCandidate(
        provider_key="nominatim",
        external_id="anchor-tianjin",
        name="Tianjin",
        category="city",
        latitude=39.1,
        longitude=117.2,
        tags=(),
        opening_hours={},
        metadata={},
    )


class _RecordingGeocoder:
    """A Nominatim stand-in that remembers the words it was asked with.

    The anchor call (`limit=1`) succeeds so the flow reaches the two calls the order is about;
    everything else answers "found nothing", which is a separate outcome from failing.
    """

    provider_key = "nominatim"

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, city: str, limit: int):
        self.calls.append((str(city), int(limit)))
        if self.error is not None:
            raise self.error
        if limit == 1:
            return [_anchor_row()]
        return []

    def close(self) -> None:
        return None


class _RecordingPOI:
    provider_key = "overpass"

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def search(self, query: str, **kwargs):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return []

    def close(self) -> None:
        return None


@pytest.fixture()
def _providers(monkeypatch) -> dict:
    """Install the fakes and hand back the objects the run actually used."""

    # `settings` is a frozen dataclass, so the live flag is patched as a copy rather than by
    # assigning onto the shared instance (measured: FrozenInstanceError, during setup).
    monkeypatch.setattr(real_city, "settings", replace(settings, real_city_enabled=True))
    box: dict = {}

    def make_geocoder(*args, **kwargs):
        instance = _RecordingGeocoder(error=box.get("geocode_error"))
        box["geocoder"] = instance
        return instance

    def make_poi(*args, **kwargs):
        instance = _RecordingPOI(error=box.get("poi_error"))
        box["poi"] = instance
        return instance

    monkeypatch.setattr(real_city, "NominatimGeocoder", make_geocoder)
    monkeypatch.setattr(real_city, "OverpassPOIProvider", make_poi)
    return box

class _RecordingPhoton:
    """A Photon stand-in for the anchor leg, so an arm is never about the campus network.

    Measured: with no double here, which leg grounds the city depends on whether
    `photon.komoot.io` answers, and the assertion below would follow the network instead of the
    product. `answer` grounds the anchor, `fail` never replies.
    """

    provider_key = "photon"

    def __init__(self, *, mode: str, error: BaseException | None = None) -> None:
        self.mode = mode
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def search(self, query: str, *, city: str, limit: int):
        self.calls.append((str(query), str(city)))
        if self.error is not None:
            raise self.error
        if self.mode == "fail":
            return [], {
                "leg": "photon", "attempted": True, "answered": False, "http_status": 503,
                "bytes": 0, "elapsed_ms": 0.1, "outcome": "NOT_ANSWERED_FORCED",
            }
        return [{"latitude": 39.1, "longitude": 117.2, "name": "Tianjin", "osm_id": 7}], {
            "leg": "photon", "attempted": True, "answered": True, "http_status": 200,
            "bytes": 200, "elapsed_ms": 0.1, "outcome": "ANSWERED",
        }

    def close(self) -> None:
        return None


def _install_photon_leg(monkeypatch, box, mode: str) -> None:
    """Put the anchor leg under test control, for the arms about which leg answered.

    Not part of the shared fixture: sibling arms here assert on the default wiring, and a
    fixture-wide double changed their world (measured: six reds where the file had two).
    """

    def make_photon(*args, **kwargs):
        instance = _RecordingPhoton(mode=mode, error=box.get("photon_error"))
        box["photon"] = instance
        return instance

    monkeypatch.setattr(real_city, "PhotonCityAnchor", make_photon)


def _give_the_trip_its_own_centre(monkeypatch, box) -> None:
    """The single case the bounded fallback still serves: no provider was asked where the city is."""

    def derive(session, journey):
        box["journey_state_anchor"] = True
        return {
            "latitude": 39.13, "longitude": 117.2,
            "source": "journey_current_activity", "location": "天津站",
        }

    monkeypatch.setattr(real_city, "derive_search_anchor", derive)




def _journey(city_code: str) -> str:
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    traveler = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]
    start = datetime(2026, 10, 12, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{city_code} live discovery",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=1, hours=9)).isoformat(),
            "budget": {"amount_minor": 300000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _discover(journey_id: str) -> dict:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/discover",
        json={"query": "tea shops", "mode": "live", "limit": 5},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_an_answered_anchor_costs_the_fallback_door_nothing(_providers, monkeypatch) -> None:
    """CMDR 02:35 (5): a leg that grounded the city closes the second geocode.

    Overpass answered with nothing, which used to send the flow to Nominatim with the same bounded
    words a second time. That ask cannot change where the city is, and it turned an answered zero
    into fresh outbound work - the sibling of the standing rule that an answered zero must not fall
    through to another provider. Asserted as *no second ask at all*, with a limit-5 probe, so the
    arm holds whichever leg grounded the anchor.
    """

    _install_photon_leg(monkeypatch, _providers, "answer")
    body = _discover(_journey("CN-TIANJIN"))
    limits = [limit for _city, limit in _providers["geocoder"].calls]
    assert 5 not in limits, f"the bounded fallback ran although a leg grounded the city: {limits}"
    status = body["provider_status"]
    assert str(status.get("fallback_geocode", "")).startswith("SKIPPED_ANCHOR_GROUNDED_BY_"), status
    assert status.get("anchor"), status


def test_when_no_leg_was_asked_the_fallback_door_is_still_asked(_providers, monkeypatch) -> None:
    """The other side of the same rule, and the target of the mutant.

    The centre came from the trip's own state, so nothing answered the geography question in this
    call; the bounded fallback is the only attempt left at the traveller's question and must run -
    asked with the human label, never with our identifier (ORD-0099 (1) survives the narrowing).
    """

    _install_photon_leg(monkeypatch, _providers, "answer")
    _give_the_trip_its_own_centre(monkeypatch, _providers)
    body = _discover(_journey("CN-TIANJIN"))
    calls = _providers["geocoder"].calls
    assert [limit for _city, limit in calls] == [5], calls
    city_used = calls[0][0]
    assert city_used == "TIANJIN", calls
    assert "CN" not in city_used.upper().replace(" ", ""), city_used
    assert body["provider_status"]["fallback_geocode"] == "ASKED", body["provider_status"]


def test_a_failed_anchor_leg_still_closes_the_second_ask(_providers, monkeypatch) -> None:
    """Photon failing is no licence to geocode twice: Nominatim answered, once, at limit 1.

    This is the shape the line always had, so it also pins that the ruling is not a Photon-only
    rule: whichever provider leg grounds the city keeps the door shut.
    """

    _install_photon_leg(monkeypatch, _providers, "fail")
    body = _discover(_journey("CN-TIANJIN"))
    calls = _providers["geocoder"].calls
    assert [limit for _city, limit in calls] == [1], calls
    status = body["provider_status"]
    # the credit lives in `anchor_legs` in a failure shape (ORD-0519), so that is where the leg
    # outcomes are read; the top-level words here name the ending, not who answered
    by_leg = {str(item.get("leg")): item for item in status.get("anchor_legs") or []}
    assert by_leg["photon"]["answered"] is False, by_leg
    assert by_leg["nominatim"]["outcome"] == "ANSWERED", by_leg
    assert status["anchor"]["source"] == "nominatim_city_anchor", status
    assert str(status["fallback_geocode"]).startswith("SKIPPED_ANCHOR_GROUNDED_BY_"), status

def test_a_live_failure_arrives_named_and_carries_the_stage_it_broke_at(_providers) -> None:
    """Clause ②: provider-shaped outcomes and our own bad request, all distinguishable.

    Asserted at the mapping seam so every shape is named, and again through the endpoint for
    the ones that can be driven there, because a mapping no response can reach proves nothing.
    """

    assert real_city.live_failure_reason(httpx.ConnectTimeout("t")) == "PROVIDER_TIMEOUT"
    assert real_city.live_failure_reason(httpx.ReadError("x")) == "RESOLVER_UNREACHABLE"
    assert real_city.live_failure_reason(_status_error(429)) == "PROVIDER_RATE_LIMITED"
    assert real_city.live_failure_reason(_status_error(500)) == "PROVIDER_SERVER_ERROR"
    assert real_city.live_failure_reason(KeyError("lat")) == "PROVIDER_BAD_RESPONSE"
    assert (
        real_city.live_failure_reason(ValueError("RESOLVER_LABEL_CARRIES_INTERNAL_CODE"))
        == "RESOLVER_LABEL_CARRIES_INTERNAL_CODE"
    )
    assert real_city.live_failure_reason(RuntimeError("NO_JOURNEY_DERIVED_CITY_ANCHOR")) == (
        "PROVIDER_NO_RESULTS"
    )
    assert set(real_city.LIVE_FAILURE_REASONS) >= {
        "RESOLVER_UNREACHABLE",
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_BAD_RESPONSE",
        "PROVIDER_NO_RESULTS",
    }

    for error, expected in (
        (httpx.ConnectError("down"), "RESOLVER_UNREACHABLE"),
        (httpx.ConnectTimeout("slow"), "PROVIDER_TIMEOUT"),
        (_status_error(429), "PROVIDER_RATE_LIMITED"),
        (_status_error(502), "PROVIDER_SERVER_ERROR"),
        (KeyError("elements"), "PROVIDER_BAD_RESPONSE"),
    ):
        _providers["poi_error"] = error
        body = _discover(_journey("CN-BEIJING"))
        status = body["provider_status"]
        assert status["fallback_reason"] == expected, (expected, status)
        assert status["fallback_reason"] in real_city.LIVE_FAILURE_REASONS, status
        assert status["failure_stage"] == "category_search", status
        assert not [name for name in CLASS_NAMES if name in json.dumps(body, ensure_ascii=False)], (
            body
        )


def test_finding_nothing_is_not_reported_as_a_provider_failure(_providers, monkeypatch) -> None:
    """`PROVIDER_NO_RESULTS` is a different answer from `RESOLVER_UNREACHABLE`.

    Both providers answered cleanly and returned nothing: the honest sentence is "nobody found
    any", not "the resolver is down" - a distinction a blanket `type(exc).__name__` could not
    express at all.
    """

    _give_the_trip_its_own_centre(monkeypatch, _providers)
    body = _discover(_journey("CN-BEIJING"))
    status = body["provider_status"]
    assert body["mode"] == "live-unavailable", body
    assert status["fallback_reason"] == "PROVIDER_NO_RESULTS", status
    assert status["failure_stage"] is None, status
    assert status["wrong_city_replay_blocked"] is True, status
    assert body["candidates"] == [], body
    # and the two calls really did use the same label, which is where this answer came from
    assert {city for city, _limit in _providers["geocoder"].calls} == {"BEIJING"}, (
        _providers["geocoder"].calls
    )


def test_our_own_malformed_label_is_not_blamed_on_the_provider(_providers) -> None:
    """The guard clause ① adds, seen from the response.

    A code the label helper cannot strip means *we* built a bad request; it is named as ours
    instead of landing in `PROVIDER_BAD_RESPONSE`, where it would read as somebody else's
    outage. Nothing leaves the process: the geocoder is never called.
    """

    body = _discover(_journey("CN-CN-TIANJIN"))
    status = body["provider_status"]
    assert status["fallback_reason"] == "RESOLVER_LABEL_CARRIES_INTERNAL_CODE", status
    assert status["failure_stage"] == "anchor_geocode", status
    # The gate that writes this field only runs after the legs, so on a path that raises before any
    # leg is asked the honest sentence is "never reached" - and the field has to be present rather
    # than an unbound name turning the named reason code into a 500.
    assert status["fallback_geocode"] == "NOT_REACHED", status
    assert _providers["geocoder"].calls == [], _providers["geocoder"].calls


def test_the_default_resolver_was_not_swapped_and_the_replay_fallback_still_answers(
    _providers, monkeypatch
) -> None:
    """Clause ③'s fence, measured rather than promised.

    Both assertions are reachable from the response: the live path still runs through the
    `NominatimGeocoder` symbol the fakes replaced (if the resolver had been swapped inside,
    those fakes would have seen nothing), and a Tianjin journey whose live attempt finds
    nothing still gets its replay candidates - so "we added reason codes" cannot quietly mean
    "we also broke the demo".
    """

    _give_the_trip_its_own_centre(monkeypatch, _providers)
    body = _discover(_journey("CN-TIANJIN"))
    assert _providers["geocoder"].calls, "the live path no longer uses the Nominatim client"
    assert _providers["poi"].calls, "the bounded category search is still Overpass"
    assert body["mode"] == "replay-fallback", body
    assert body["candidates"], body
    assert body["provider_status"]["fallback_reason"] == "PROVIDER_NO_RESULTS", body
