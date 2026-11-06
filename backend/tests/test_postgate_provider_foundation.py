from __future__ import annotations

import asyncio

import httpx

from app.integrations.ai.base import AITextResult
from app.integrations.ai.resilient import ResilientAIExplanationService
from app.integrations.mobility.snapshot_capture import (
    MobilitySnapshotCaptureError,
    capture_mobility_snapshot,
)
from app.integrations.mobility.valhalla_http import (
    ValhallaHTTPMobilityProvider,
    ValhallaProviderError,
)
from app.intelligence.reason_codes import ReasonCode, VerifiedExplanationFacts
from app.replanning.feasibility import FeasibilityActivity, build_context, evaluate_feasibility


class _Provider:
    def __init__(self, key: str, values: dict[tuple[str, str], int], fail=()):
        self.provider_key = key
        self.values = values
        self.fail = set(fail)

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        pair = (origin_key, destination_key)
        if pair in self.fail:
            raise RuntimeError("provider down")
        return self.values[pair]


def test_snapshot_capture_freezes_primary_and_explicit_fallback_provenance():
    primary = _Provider(
        "valhalla",
        {("a", "b"): 10, ("b", "c"): 20},
        fail={("b", "c")},
    )
    fallback = _Provider("mock-mobility", {("b", "c"): 18})
    result = capture_mobility_snapshot(
        [("a", "b"), ("b", "c")],
        primary=primary,
        fallback=fallback,
        captured_at="2026-09-09T20:00:00+00:00",
    )
    assert result.degraded is True
    assert result.snapshot.estimate_minutes("a", "b") == 10
    assert result.snapshot.facts[("a", "b")].provider_key == "valhalla"
    assert result.snapshot.facts[("b", "c")].provider_key == "mock-mobility"
    assert result.snapshot.facts[("b", "c")].degraded is True
    assert result.fallback_pairs == (("b", "c"),)


def test_snapshot_capture_fails_explicitly_when_no_safe_fact_exists():
    primary = _Provider("valhalla", {}, fail={("a", "b")})
    try:
        capture_mobility_snapshot([("a", "b")], primary=primary)
    except MobilitySnapshotCaptureError as exc:
        assert "a|b" in str(exc)
    else:
        raise AssertionError("capture must fail instead of inventing ETA")


def test_captured_snapshot_is_accepted_by_authoritative_gate():
    primary = _Provider("valhalla", {("a", "b"): 10})
    snapshot = capture_mobility_snapshot([("a", "b")], primary=primary).snapshot
    journey = {
        "id": "j",
        "city_code": "X",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T12:00:00+08:00",
        "budget_amount_minor": 10000,
        "budget_currency": "CNY",
    }
    places = {
        "a": {
            "id": "a",
            "city_code": "X",
            "status": "available",
            "opening_hours": {"open": "08:00", "close": "22:00"},
        },
        "b": {
            "id": "b",
            "city_code": "X",
            "status": "available",
            "opening_hours": {"open": "08:00", "close": "22:00"},
        },
    }
    context = build_context(journey, [], places, {}, mobility_provider=snapshot)
    from datetime import datetime

    activities = [
        FeasibilityActivity(
            key="a",
            place_id="a",
            start_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            end_at=datetime.fromisoformat("2026-10-10T09:30:00+08:00"),
            cost_minor=0,
            currency="CNY",
        ),
        FeasibilityActivity(
            key="b",
            place_id="b",
            start_at=datetime.fromisoformat("2026-10-10T09:45:00+08:00"),
            end_at=datetime.fromisoformat("2026-10-10T10:15:00+08:00"),
            cost_minor=0,
            currency="CNY",
        ),
    ]
    result = evaluate_feasibility(context, activities)
    assert result.passed is True, result.failures


def test_valhalla_http_adapter_parses_route_and_matrix_without_network():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/route"):
            return httpx.Response(
                200,
                json={
                    "trip": {
                        "summary": {"time": 601, "length": 1.25},
                        "legs": [{"shape": "encoded-shape"}],
                    }
                },
            )
        if request.url.path.endswith("/sources_to_targets"):
            return httpx.Response(200, json={"durations": [[0, 601], [605, 0]]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    points = {
        "a": {"latitude": 39.1, "longitude": 117.2},
        "b": {"latitude": 39.2, "longitude": 117.3},
    }
    provider = ValhallaHTTPMobilityProvider(
        client=client,
        base_url="http://valhalla.test",
        place_lookup=lambda key: points[key],
    )
    estimate = provider.estimate("a", "b")
    assert estimate.duration_min == 11
    assert estimate.distance_m == 1250
    assert estimate.shape == "encoded-shape"
    assert provider.matrix_minutes(["a", "b"]) == [[0, 11], [11, 0]]
    client.close()


def test_valhalla_http_adapter_reports_timeout_and_malformed_payloads_without_inventing_eta():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    timeout_client = httpx.Client(transport=httpx.MockTransport(timeout_handler))
    timeout_provider = ValhallaHTTPMobilityProvider(
        client=timeout_client,
        base_url="http://valhalla.test",
        place_lookup=lambda _key: {"latitude": 39.1, "longitude": 117.2},
        timeout_s=1.0,
    )
    try:
        timeout_provider.estimate("a", "b")
    except ValhallaProviderError as exc:
        assert "request failed" in str(exc)
    else:
        raise AssertionError("timeout must be a controlled provider error")
    timeout_client.close()

    malformed_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"trip": {}}))
    )
    malformed_provider = ValhallaHTTPMobilityProvider(
        client=malformed_client,
        base_url="http://valhalla.test",
        place_lookup=lambda _key: {"latitude": 39.1, "longitude": 117.2},
    )
    try:
        malformed_provider.estimate("a", "b")
    except ValhallaProviderError as exc:
        assert "summary time" in str(exc)
    else:
        raise AssertionError("malformed route payload must be rejected")
    malformed_client.close()


class _FailingAI:
    provider_key = "deepseek"

    async def explain(self, facts, *, task):
        raise RuntimeError("model unavailable")


class _WorkingAI:
    provider_key = "deepseek"

    async def explain(self, facts, *, task):
        return AITextResult(provider_key=self.provider_key, text="verified explanation")


class _BlankAI:
    provider_key = "deepseek"

    async def explain(self, facts, *, task):
        return AITextResult(provider_key=self.provider_key, text="   ")


class _MalformedAI:
    provider_key = "deepseek"

    async def explain(self, facts, *, task):
        return None


def test_resilient_ai_uses_real_provider_when_available_and_falls_back_when_not():
    facts = VerifiedExplanationFacts(
        reason_codes=(ReasonCode.COMMITMENT_PRESERVED,),
        evidence={"cost_delta_minor": 600},
        locale="en",
    )
    working = asyncio.run(
        ResilientAIExplanationService(primary=_WorkingAI()).explain(facts, task="explain")
    )
    assert working.provider_key == "deepseek"
    assert working.degraded is False

    degraded = asyncio.run(
        ResilientAIExplanationService(primary=_FailingAI()).explain(facts, task="explain")
    )
    assert degraded.provider_key == "deterministic-fallback"
    assert degraded.degraded is True


def test_resilient_ai_falls_back_for_blank_or_malformed_provider_results():
    facts = VerifiedExplanationFacts(
        reason_codes=(ReasonCode.COMMITMENT_PRESERVED,),
        evidence={"cost_delta_minor": 600},
        locale="en",
    )
    for provider in (_BlankAI(), _MalformedAI()):
        result = asyncio.run(
            ResilientAIExplanationService(primary=provider).explain(facts, task="explain")
        )
        assert result.provider_key == "deterministic-fallback"
        assert result.degraded is True
