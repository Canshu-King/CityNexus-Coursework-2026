"""V4 #4 — a live-configured HARD capture must never launder the mock fixture.

When an operator has configured a real routing provider, that provider is the
authority.  If it fails to measure one leg:

  * a *hard* replan capture (require_explicit_facts=True) has NO mock fallback,
    so the leg stays unavailable -> the candidate becomes unavailable -> other
    trustworthy candidates can still proceed, and only an all-untrusted result
    reports MOBILITY_DATA_UNAVAILABLE (T2 / GPT v4 live-provider gate);
  * a *soft* offline capture keeps the pair-level mock fallback and degrades.

Before this fix the hard path silently inherited an explicit mock fixture number
for the failed leg and let the hard replan proceed on a fabricated fact.
"""

from __future__ import annotations

import types

import pytest

import app.integrations.mobility.runtime as runtime
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureError


class _FailingLiveProvider:
    provider_key = "valhalla"

    def __init__(self, *, failing_pairs=None, **_kwargs):
        self.failing = set(failing_pairs or set())

    def status_capabilities(self):
        return {"status": "OK", "has_live_traffic": True}

    def estimate_minutes(self, origin_key, destination_key):
        if (origin_key, destination_key) in self.failing:
            raise RuntimeError("live provider unavailable for leg")
        return 5

    def fact_quality(self, origin_key, destination_key):
        return "LIVE_PROVIDER_CAPTURE"


def _configure_live(monkeypatch, failing_pairs):
    monkeypatch.setattr(
        runtime, "settings", types.SimpleNamespace(live_valhalla_url="http://valhalla.test")
    )
    monkeypatch.setattr(runtime, "_place_lookup", lambda session, key: {"latitude": 0.0, "longitude": 0.0})
    monkeypatch.setattr(
        runtime,
        "ValhallaHTTPMobilityProvider",
        lambda **kwargs: _FailingLiveProvider(failing_pairs=failing_pairs, **{}),
    )


def test_live_configured_hard_capture_does_not_fall_back_to_mock(monkeypatch):
    _configure_live(monkeypatch, {("b", "c")})
    with pytest.raises(MobilitySnapshotCaptureError):
        runtime.capture_runtime_mobility_snapshot(
            None,
            None,
            [("a", "b"), ("b", "c")],
            operation_key="replan:event-x",
            require_explicit_facts=True,
        )


def test_live_configured_soft_capture_still_degrades_to_mock(monkeypatch):
    _configure_live(monkeypatch, {("b", "c")})
    result = runtime.capture_runtime_mobility_snapshot(
        None,
        None,
        [("a", "b"), ("b", "c")],
        operation_key="replan:event-y",
        require_explicit_facts=False,
    )
    assert result.snapshot.degraded is True
    # The failed leg came from the mock fallback; the good leg from the live provider.
    assert result.snapshot.facts[("a", "b")].provider_key == "valhalla"
    assert result.snapshot.facts[("b", "c")].provider_key == "mock-mobility"
