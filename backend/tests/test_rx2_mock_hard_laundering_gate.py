"""Recovery gate RX-2 — a failed live leg must not be laundered through the mock.

Mutant J-MOCK-HARD-FALLBACK (Codex Extreme v2, track J) inverted the hard-capture
fallback in ``app/integrations/mobility/runtime.py``:

    fallback=None if hard_capture else mock,  ->  fallback=mock,

and the selected gate survived.  So did ``tests/test_qoder_live_hard_fallback.py``,
for a reason worth recording: that gate captures ``("a","b")``/``("b","c")``, and
neither pair exists in the fixture matrix, so ``MockMobilityProvider(strict=True)``
raises ``KeyError`` on its own and the capture fails closed *even when the mock is
wired in as the fallback*.  It proves the strictness of the fixture, not the
absence of a fallback.  A leg that *is* an explicit fixture fact — exactly the
case V4 #4 was about — flows straight through.

This gate therefore uses an explicit fixture pair and asserts on the fact itself,
so the mutant has nowhere to hide.

Layer: provider/runtime integration (real capture pipeline, fake live provider).
"""

from __future__ import annotations

import json
import types

import pytest

import app.integrations.mobility.runtime as runtime
from app.integrations.mock.providers import MOCK_ROOT
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureError

# An explicit pair in data/mock/mobility_matrix.json whose value differs from the
# fixture's blanket default_minutes, so a laundered number is recognizable.
_FIXTURE = json.loads((MOCK_ROOT / "mobility_matrix.json").read_text(encoding="utf-8"))
_EXPLICIT_PAIR, _EXPLICIT_MINUTES = next(
    (
        (tuple(key.split("|")), int(minutes))
        for key, minutes in _FIXTURE["minutes"].items()
        if int(minutes) != int(_FIXTURE["default_minutes"])
    )
)


class _LiveThatFailsEveryLeg:
    provider_key = "valhalla"

    def __init__(self, **_kwargs):
        pass

    def status_capabilities(self):
        return {"status": "OK", "has_live_traffic": True}

    def estimate_minutes(self, origin_key, destination_key):
        raise RuntimeError("live routing unavailable")

    def fact_quality(self, origin_key, destination_key):  # pragma: no cover - unreachable
        return "LIVE_PROVIDER_CAPTURE"


def _configure_live(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime, "settings", types.SimpleNamespace(live_valhalla_url="http://valhalla.test")
    )
    monkeypatch.setattr(
        runtime, "_place_lookup", lambda session, key: {"latitude": 0.0, "longitude": 0.0}
    )
    monkeypatch.setattr(
        runtime, "ValhallaHTTPMobilityProvider", lambda **kwargs: _LiveThatFailsEveryLeg()
    )


def _fixture_pair_is_what_this_gate_assumes() -> None:
    """Guard the premise: the chosen pair really is an explicit fixture fact."""
    assert _EXPLICIT_PAIR in _FIXTURE["minutes"]
    assert _EXPLICIT_MINUTES != _FIXTURE["default_minutes"]


def test_hard_capture_has_no_mock_fallback_for_an_explicit_fixture_leg(monkeypatch) -> None:
    _configure_live(monkeypatch)
    # Only the explicit fixture leg is captured: a strict mock would answer it,
    # so the one possible source of a number here is the forbidden fallback.
    with pytest.raises(MobilitySnapshotCaptureError):
        runtime.capture_runtime_mobility_snapshot(
            None,
            None,
            [_EXPLICIT_PAIR],
            operation_key="replan:rx2-hard",
            require_explicit_facts=True,
        )


def test_soft_capture_may_still_degrade_to_the_explicit_fixture_leg(monkeypatch) -> None:
    """The hard path is the one under test; soft degradation must stay available."""
    _configure_live(monkeypatch)
    result = runtime.capture_runtime_mobility_snapshot(
        None,
        None,
        [_EXPLICIT_PAIR],
        operation_key="replan:rx2-soft",
        require_explicit_facts=False,
    )
    assert result.snapshot.degraded is True
    fact = result.snapshot.facts[_EXPLICIT_PAIR]
    assert fact.provider_key == "mock-mobility"
    # A mock-fallback number is kept for display but downgraded to UNKNOWN, so
    # the feasibility gate must reject it as a hard travel fact.
    assert fact.fact_quality is None
    with pytest.raises(KeyError):
        result.snapshot.hard_minutes(*_EXPLICIT_PAIR)
