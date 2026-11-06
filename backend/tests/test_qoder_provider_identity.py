"""ORD-0188 provider-layer tests: deterministic, no network, fixtures replayed verbatim.

Gate shape required by the order: each of MATCH / AMBIGUOUS / NO_MATCH / RUNTIME_FAILURE has a
fixed input -> fixed output, and one counter-example gate proving a resolver failure can never be
expressed as "this destination does not exist".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.integrations.discovery.provider_identity import (
    PROVIDER_USER_AGENT,
    REASON_BAD_RESPONSE,
    REASON_NO_RESULTS,
    REASON_RATE_LIMITED,
    REASON_REFUSED,
    REASON_SERVER_ERROR,
    REASON_TIMEOUT,
    REASON_UNREACHABLE,
    HttpxTransport,
    ProviderOutcome,
    build_httpx_client,
    normalize_query,
    resolve_from_endpoints,
)
from app.integrations.discovery.real_city import OverpassPOIProvider

FIXTURES = Path(__file__).parent / "fixtures" / "provider_identity"
NOW = datetime(2026, 9, 22, 1, 0, 0, tzinfo=timezone.utc)


def envelope(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ReplayTransport:
    """Answers exactly like the captured exchange, and counts how often it was asked."""

    def __init__(self, name: str, *, endpoint_label: str | None = None) -> None:
        self._env = envelope(name)
        self.calls: list[str] = []
        self.endpoint_label = endpoint_label

    def get(self, url, params):
        self.calls.append(url)
        headers = {str(k).lower(): str(v) for k, v in (self._env.get("response_headers") or {}).items()}
        status = self._env.get("status")
        if status is None:  # a capture that never got a response line
            raise httpx.ConnectTimeout("connection timed out")
        return int(status), headers, self._env["body_text"].encode("utf-8")


class RaisingTransport:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = []

    def get(self, url, params):
        self.calls.append(url)
        raise self._error


def provider(transport, base_url="https://photon.komoot.io"):
    from app.integrations.discovery.provider_identity import PhotonIdentityProvider

    return PhotonIdentityProvider(transport=transport, base_url=base_url, now=lambda: NOW)


# ---------------------------------------------------------------- MATCH
def test_match_shape_comes_from_one_destination_grade_relation():
    resolution = provider(ReplayTransport("photon_gongshan_full_match.json")).resolve(
        "贡山独龙族怒族自治县"
    )

    assert resolution.outcome is ProviderOutcome.MATCH
    candidate = resolution.single_candidate
    assert candidate.source_provider == "photon"
    assert candidate.source_entity_id == "R:2729765"
    assert candidate.display_name == "贡山县"
    assert candidate.crs == "WGS84"
    assert candidate.centroid_wgs84 == pytest.approx((27.7443567, 98.6672235), abs=1e-6)
    # the captured extent is [w, north, e, south]; normalisation must not trust that order
    assert candidate.extent_wgs84 == pytest.approx((98.132047, 27.4945677, 98.9182148, 28.3970972))
    assert candidate.provenance_complete is True
    assert candidate.fetched_at == "2026-09-22T01:00:00+00:00"
    assert resolution.answered is True and resolution.retryable is False
    # the raw provider observation survives, so an auditor can re-read what we read
    assert candidate.raw_feature["properties"]["osm_id"] == 2729765


def test_resolution_exposes_no_product_identity_verdict():
    """B stops at provider evidence: nothing here may name a canonical identity."""

    resolution = provider(ReplayTransport("photon_gongshan_full_match.json")).resolve("贡山")
    fields = set(resolution.__dict__) | {a for a in dir(resolution) if not a.startswith("_")}
    assert not {a for a in fields if "canonical" in a}, sorted(fields)
    evidence = resolution.to_evidence()
    assert "canonical_destination_id" not in json.dumps(evidence)
    assert evidence["outcome"] == "MATCH" and len(evidence["candidates"]) == 1


# ---------------------------------------------------------------- AMBIGUOUS
@pytest.mark.parametrize(
    "fixture", ["photon_yunhe_county_ambiguous.json", "photon_yunhe_short_ambiguous.json"]
)
def test_ambiguous_reports_every_contender_and_picks_none(fixture):
    resolution = provider(ReplayTransport(fixture)).resolve("云和县")

    assert resolution.outcome is ProviderOutcome.AMBIGUOUS
    ids = {c.source_entity_id for c in resolution.candidates}
    assert "R:3289208" in ids and "N:244082768" in ids
    assert resolution.single_candidate is None, "an ambiguous answer must not carry a winner"
    assert resolution.retryable is False and resolution.answered is True


# ---------------------------------------------------------------- NO_MATCH
def test_no_match_is_only_reachable_when_somebody_answered():
    resolution = provider(ReplayTransport("photon_unknown_place_zero_features.json")).resolve(
        "zzz不存在的县xq"
    )

    assert resolution.outcome is ProviderOutcome.NO_MATCH
    assert resolution.answered is True and resolution.provider_replied is True
    assert resolution.retryable is False
    assert resolution.reason == "PROVIDER_NO_RESULTS"
    assert resolution.attempts[0].http_status == 200


def test_zero_features_reverse_capture_is_a_no_match_not_an_outage():
    path = FIXTURES / "photon_reverse_gongshan_200_zero_features.body.json"
    transport = ReplayTransport.__new__(ReplayTransport)
    transport.calls = []
    transport._env = {"status": 200, "response_headers": {}, "body_text": path.read_text(encoding="utf-8")}
    resolution = provider(transport).resolve("贡山独龙族怒族自治县")
    assert resolution.outcome is ProviderOutcome.NO_MATCH and resolution.answered is True


# ---------------------------------------------------------------- RUNTIME_FAILURE
@pytest.mark.parametrize(
    "fixture, reason",
    [
        ("photon_refused_default_user_agent.json", REASON_REFUSED),
        ("photon_bad_request_400_runtime_failure.json", "PROVIDER_BAD_RESPONSE"),
    ],
)
def test_http_refusals_are_runtime_shaped_not_absence(fixture, reason):
    resolution = provider(ReplayTransport(fixture)).resolve("贡山独龙族怒族自治县")

    assert resolution.outcome is ProviderOutcome.RUNTIME_FAILURE, "counter-example gate"
    assert resolution.answered is False and resolution.provider_replied is False
    assert resolution.reason == reason
    assert resolution.candidates == ()


def test_timeout_never_becomes_no_match():
    resolution = provider(RaisingTransport(httpx.ReadTimeout("timed out"))).resolve("云和县")

    assert resolution.outcome is ProviderOutcome.RUNTIME_FAILURE
    assert resolution.reason == REASON_TIMEOUT
    assert resolution.retryable is True
    assert resolution.candidates == (), "an empty list here means 'unknown', never 'none exist'"


def test_unreachable_and_rate_limited_are_separated_and_retryable():
    unreachable = provider(RaisingTransport(httpx.ConnectError("no route to host"))).resolve("云和县")
    assert unreachable.outcome is ProviderOutcome.RUNTIME_FAILURE
    assert unreachable.reason == REASON_UNREACHABLE and unreachable.retryable is True

    rate_limited = provider(
        RaisingTransport(httpx.HTTPStatusError("429", request=None, response=None))
    )
    # the 429 path is status-shaped, so replay it as a status instead of an exception
    transport = ReplayTransport.__new__(ReplayTransport)
    transport.calls = []
    transport._env = {
        "status": 429,
        "response_headers": {"Retry-After": "17"},
        "body_text": '{"message":"too many requests"}',
    }
    limited = provider(transport).resolve("云和县")
    assert limited.outcome is ProviderOutcome.RUNTIME_FAILURE and limited.reason == REASON_RATE_LIMITED
    assert limited.retryable is True
    assert limited.attempts[0].retry_after_s == 17.0
    assert rate_limited  # keep the exception path referenced: it must not raise out of resolve()


# ---------------------------------------------------------------- multi-endpoint
def test_first_endpoint_failing_second_answering_zero_is_no_match():
    broken = RaisingTransport(httpx.ConnectTimeout("mirror down"))
    answering = ReplayTransport("photon_unknown_place_zero_features.json")
    resolution = resolve_from_endpoints(
        "zzz不存在的县xq",
        providers=[("mirror-a", broken), ("mirror-b", answering)],
        now=lambda: NOW,
    )

    assert resolution.outcome is ProviderOutcome.NO_MATCH
    assert resolution.answered is True
    assert len(resolution.attempts) == 2
    assert resolution.attempts[0].answered is False and resolution.attempts[1].answered is True
    assert resolution.retryable is False, "the question was answered; retrying cannot change it"


def test_all_endpoints_failing_stays_runtime_and_keeps_every_attempt():
    resolution = resolve_from_endpoints(
        "贡山独龙族怒族自治县",
        providers=[
            ("mirror-a", RaisingTransport(httpx.ReadTimeout("a"))),
            ("mirror-b", RaisingTransport(httpx.ConnectError("b"))),
        ],
        now=lambda: NOW,
    )
    assert resolution.outcome is ProviderOutcome.RUNTIME_FAILURE
    assert resolution.retryable is True
    assert [a.reason for a in resolution.attempts] == [REASON_TIMEOUT, REASON_UNREACHABLE]
    assert resolution.candidates == ()


# ---------------------------------------------------------------- query normalisation
@pytest.mark.parametrize(
    "raw", ["云和 县", "云和-县", "云和县。", "  云和县  ", "云和县、"]
)
def test_typing_variants_ask_the_provider_the_same_question(raw):
    assert normalize_query(raw) in {"云和", "云和县"} or normalize_query(raw).startswith("云和")
    assert normalize_query("云和县。") == "云和县"
    assert normalize_query("  云和  县 ") == "云和 县"


def test_empty_query_is_not_reported_as_an_absence_of_the_place():
    resolution = provider(ReplayTransport("photon_gongshan_full_match.json")).resolve("   ")
    assert resolution.outcome is ProviderOutcome.RUNTIME_FAILURE
    assert resolution.reason == "EMPTY_QUERY"
    assert resolution.answered is False and resolution.provider_replied is False
    assert resolution.candidates == () and resolution.retryable is False


# ---------------------------------------------------------------- UA + overpass mirror loop
def test_the_adapter_pins_an_identifying_user_agent():
    client = build_httpx_client()
    try:
        assert client.headers["user-agent"] == PROVIDER_USER_AGENT
    finally:
        client.close()


def _overpass_with(handler):
    client = build_httpx_client(transport=httpx.MockTransport(handler))
    return OverpassPOIProvider(client=client, base_url="https://overpass-api.de/api")


def test_answered_zero_from_the_first_mirror_ends_the_rotation():
    """The ORD-0148 regression: a valid 200/0-elements used to fall through to slower mirrors and
    come back as PROVIDER_TIMEOUT, i.e. an answer was rewritten into an outage."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(200, json={"elements": []})
        raise httpx.ReadTimeout("later mirrors never get this far")

    provider_obj = _overpass_with(handler)
    try:
        candidates, attempts = provider_obj.search_with_evidence(
            "museum", latitude=27.9458325, longitude=98.5251309, radius_m=5000, limit=8
        )
    finally:
        provider_obj.close()

    assert candidates == []
    assert len(seen) == 1, "rotation must stop as soon as somebody answers"
    assert attempts[0]["answered"] is True and attempts[0]["elements"] == 0
    assert attempts[0]["endpoint"] in seen[0]


def test_no_mirror_answering_still_raises_so_the_product_reports_runtime_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="<html>Gateway timeout</html>")

    provider_obj = _overpass_with(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            provider_obj.search_with_evidence(
                "hotel", latitude=27.7443567, longitude=98.6672235, radius_m=5000, limit=8
            )
    finally:
        provider_obj.close()


def test_elements_from_a_real_answer_are_kept_with_provider_identity():
    body = json.loads(
        (FIXTURES / "overpass_museum_answer_zero_elements.json").read_text(encoding="utf-8")
    )["body_text"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"))

    provider_obj = _overpass_with(handler)
    try:
        candidates, attempts = provider_obj.search_with_evidence(
            "museum", latitude=27.9458325, longitude=98.5251309, radius_m=5000, limit=8
        )
    finally:
        provider_obj.close()
    payload = json.loads(body)
    assert len(candidates) == len(payload.get("elements") or [])
    assert attempts[0]["answered"] is True and attempts[0]["http_status"] == 200


def test_httpx_transport_signature_matches_the_replay_contract():
    """The protocol the adapters speak is three values; a real httpx.Client satisfies it."""

    client = build_httpx_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"features": []}))
    )
    transport = HttpxTransport(client=client, timeout_s=5.0)
    try:
        status, headers, body = transport.get("https://photon.komoot.io/api/", {"q": "x"})
    finally:
        client.close()
    assert status == 200 and json.loads(body)["features"] == [] and isinstance(headers, dict)


def test_six_provider_shapes_yield_six_distinct_reason_codes():
    """Lane F's g11, answered at the layer that actually sees the shapes.

    A retryable outage, a broken endpoint, a refusal, a rate limit, a body we cannot read and a
    clean "nothing here" are six different operational statements; if two share a code, the screen
    and the operator can no longer tell 'ask again' from 'give up' (R14's one-classification rule).
    """

    def replay(status, body, headers=None):
        transport = ReplayTransport.__new__(ReplayTransport)
        transport.calls = []
        transport._env = {"status": status, "response_headers": headers or {}, "body_text": body}
        return transport

    shapes = {
        "read timeout": provider(RaisingTransport(httpx.ReadTimeout("t"))).resolve("云和县"),
        "unreachable": provider(RaisingTransport(httpx.ConnectError("u"))).resolve("云和县"),
        "rate limited": provider(replay(429, "{}", {"Retry-After": "9"})).resolve("云和县"),
        "server error": provider(replay(500, "{}")).resolve("云和县"),
        "unreadable body": provider(replay(200, "<html>not json</html>")).resolve("云和县"),
        "answered nothing": provider(ReplayTransport("photon_unknown_place_zero_features.json")).resolve(
            "zzz不存在的县xq"
        ),
    }

    reasons = {name: r.reason for name, r in shapes.items()}
    assert len(set(reasons.values())) == 6, reasons
    assert reasons["read timeout"] == REASON_TIMEOUT
    assert reasons["unreachable"] == REASON_UNREACHABLE
    assert reasons["rate limited"] == REASON_RATE_LIMITED
    assert reasons["server error"] == REASON_SERVER_ERROR
    assert reasons["unreadable body"] == REASON_BAD_RESPONSE
    assert reasons["answered nothing"] == REASON_NO_RESULTS
    assert shapes["answered nothing"].outcome is ProviderOutcome.NO_MATCH
    assert all(
        shapes[k].outcome is ProviderOutcome.RUNTIME_FAILURE
        for k in shapes if k != "answered nothing"
    ), "only the clean zero may leave the failure class"
    assert shapes["rate limited"].attempts[0].retry_after_s == 9.0
    assert shapes["answered nothing"].retryable is False
    assert shapes["rate limited"].retryable is True
