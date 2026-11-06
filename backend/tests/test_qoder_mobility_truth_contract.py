"""T2 — mobility truth contract (fail closed on missing travel evidence).

The mock fixture holds nine explicit route facts plus a blanket
``default_minutes = 20``.  That constant is not a measurement.  Before this
contract a replan leg absent from the explicit matrix silently inherited it,
and the fabricated 20 minutes then fed the deterministic feasibility gate,
which correctly refused the resulting overlap — so the traveller was told no
replacement existed when the truth was that the engine never knew the travel
time at all.

Contract: docs/MOBILITY_TRUTH_CONTRACT.md
"""

from __future__ import annotations

import json

import pytest

from app.integrations.mock.providers import MockMobilityProvider

# A pair that IS in data/mock/mobility_matrix.json (explicit fixture fact).
EXPLICIT_PAIR = ("place-old-town", "place-riverside-museum")
# A pair that is NOT in the matrix, so the fixture would otherwise default it.
MISSING_PAIR = ("real-place-unknown-a", "real-place-unknown-b")


def test_explicit_matrix_pair_returns_its_explicit_minutes() -> None:
    provider = MockMobilityProvider()
    assert provider.estimate_minutes(*EXPLICIT_PAIR) == 15


def test_non_strict_provider_keeps_the_demo_default() -> None:
    """Seeded offline demo paths intentionally rely on the fixture default."""

    provider = MockMobilityProvider()
    assert provider.estimate_minutes(*MISSING_PAIR) == 20


def test_strict_provider_refuses_the_fabricated_default() -> None:
    """A missing pair must never be priced at the constant on the replan path."""

    provider = MockMobilityProvider(strict=True)
    with pytest.raises(KeyError):
        provider.estimate_minutes(*MISSING_PAIR)


def test_strict_provider_still_serves_explicit_facts() -> None:
    provider = MockMobilityProvider(strict=True)
    assert provider.estimate_minutes(*EXPLICIT_PAIR) == 15


def test_missing_pair_never_becomes_twenty_in_strict_mode() -> None:
    """Regression 3 of the contract, stated directly."""

    provider = MockMobilityProvider(strict=True)
    try:
        minutes = provider.estimate_minutes(*MISSING_PAIR)
    except KeyError:
        return
    pytest.fail(f"missing pair was priced at a fabricated {minutes} minutes")


def test_capture_fails_closed_when_a_pair_has_no_explicit_fact() -> None:
    """The capture layer must raise rather than freeze a fabricated fact."""

    from app.integrations.mobility.snapshot_capture import (
        MobilitySnapshotCaptureError,
        capture_mobility_snapshot,
    )

    provider = MockMobilityProvider(strict=True)
    with pytest.raises(MobilitySnapshotCaptureError):
        capture_mobility_snapshot(
            [MISSING_PAIR],
            primary=provider,
            captured_at="2026-09-18T00:00:00+00:00",
            operation_key="t2-test",
        )


def test_capture_freezes_an_explicit_fact_unchanged() -> None:
    from app.integrations.mobility.snapshot_capture import capture_mobility_snapshot

    provider = MockMobilityProvider(strict=True)
    captured = capture_mobility_snapshot(
        [EXPLICIT_PAIR],
        primary=provider,
        captured_at="2026-09-18T00:00:00+00:00",
        operation_key="t2-test",
    ).snapshot
    assert captured.facts[EXPLICIT_PAIR].minutes == 15


def test_mobility_data_unavailable_is_distinct_from_no_alternative() -> None:
    """The two 422 reasons must be separate outcomes.

    "We could not measure the legs" is not "no replacement place exists".
    Reporting the first as the second tells the traveller nothing can be done
    when the engine merely lacked evidence.
    """

    from app.main import MobilityDataUnavailable

    error = MobilityDataUnavailable("origin-key", "destination-key")
    assert str(error) == "MOBILITY_DATA_UNAVAILABLE:origin-key|destination-key"
    assert "REPLAN_NO_ALTERNATIVE" != str(error)
    assert isinstance(error, RuntimeError)


def test_fixture_default_is_documented_as_a_constant() -> None:
    """Guard the fixture shape this contract reasons about."""

    from app.integrations.mock.providers import MOCK_ROOT

    fixture = json.loads((MOCK_ROOT / "mobility_matrix.json").read_text(encoding="utf-8"))
    assert fixture["default_minutes"] == 20
    # Pinned by value, not by count: the count coupled this guard to how many cities the
    # fixture carries, so adding a second city's legs read as a contract change (ORD-0089).
    demo_legs = {
        "place-old-town|place-riverside-museum": 15,
        "place-riverside-museum|place-food-hall": 15,
        "place-food-hall|place-tech-art": 20,
        "place-tech-art|place-river-cruise": 20,
        "place-food-hall|place-city-park": 15,
        "place-city-park|place-river-cruise": 20,
        "place-food-hall|place-design-library": 15,
        "place-design-library|place-river-cruise": 20,
        "place-river-cruise|place-skyline-dinner": 20,
    }
    demo_in_fixture = {
        key: value
        for key, value in fixture["minutes"].items()
        if not key.startswith("place-beijing-")
    }
    assert demo_in_fixture == demo_legs
    assert f"{EXPLICIT_PAIR[0]}|{EXPLICIT_PAIR[1]}" in fixture["minutes"]
    assert f"{MISSING_PAIR[0]}|{MISSING_PAIR[1]}" not in fixture["minutes"]


# --------------------------------------------------------------------------
# V2-T2 fact-quality tagging + hard-feasibility gate (B1 unit layer)
# --------------------------------------------------------------------------


def test_provider_fact_quality_separates_explicit_from_default() -> None:
    from app.replanning.provider_snapshots import (
        EXPLICIT_FROZEN_FIXTURE,
        SYNTHETIC_DEFAULT,
    )

    provider = MockMobilityProvider()
    assert provider.fact_quality(*EXPLICIT_PAIR) == EXPLICIT_FROZEN_FIXTURE
    assert provider.fact_quality(*MISSING_PAIR) == SYNTHETIC_DEFAULT


def test_capture_tags_default_fact_quality_even_when_non_strict() -> None:
    """The demo may read the constant, but the snapshot records it as synthetic.

    This is the front-door fix: the value 20 stays available to demo paths via
    ``estimate_minutes`` while the fact is *tagged* so hard feasibility can reject
    it (V2-T2 / B1).
    """

    from app.replanning.provider_snapshots import (
        EXPLICIT_FROZEN_FIXTURE,
        SYNTHETIC_DEFAULT,
    )
    from app.integrations.mobility.snapshot_capture import capture_mobility_snapshot

    captured = capture_mobility_snapshot(
        [EXPLICIT_PAIR, MISSING_PAIR],
        primary=MockMobilityProvider(),
        captured_at="2026-09-18T00:00:00+00:00",
        operation_key="t2-quality",
    ).snapshot
    # Both are readable (minutes present)...
    assert captured.estimate_minutes(*MISSING_PAIR) == 20
    # ...but the default one is flagged SYNTHETIC_DEFAULT, the explicit one not.
    assert captured.facts[MISSING_PAIR].fact_quality == SYNTHETIC_DEFAULT
    assert captured.facts[EXPLICIT_PAIR].fact_quality == EXPLICIT_FROZEN_FIXTURE


def test_hard_minutes_rejects_synthetic_default_but_uses_explicit() -> None:
    """hard_minutes is the truth gate; estimate_minutes stays the raw accessor."""

    from app.integrations.mobility.snapshot_capture import capture_mobility_snapshot

    captured = capture_mobility_snapshot(
        [EXPLICIT_PAIR, MISSING_PAIR],
        primary=MockMobilityProvider(),
        captured_at="2026-09-18T00:00:00+00:00",
        operation_key="t2-hard",
    ).snapshot
    assert captured.hard_minutes(*EXPLICIT_PAIR) == 15
    with pytest.raises(KeyError):
        captured.hard_minutes(*MISSING_PAIR)


def test_hard_minutes_fails_closed_on_unrecorded_quality() -> None:
    """V3-1: an untagged / unknown quality must NOT drive hard feasibility.

    A pre-v2 snapshot can hold an untagged blanket default 20, so None cannot be
    assumed trustworthy.  ``from_minutes`` now defaults to an honest
    ``EXPLICIT_FROZEN_FIXTURE`` tag and is allowed; a snapshot explicitly built
    with no provenance fails closed, as does a genuinely missing pair.
    """

    from app.replanning.provider_snapshots import MobilitySnapshot

    # Explicitly-declared frozen map -> allowed (caller opts into EXPLICIT).
    explicit = MobilitySnapshot.from_minutes(
        source_provider_key="mock-mobility",
        minutes={EXPLICIT_PAIR: 15},
        captured_at="2026-09-18T00:00:00+00:00",
        operation_key="t2-explicit",
        fact_quality="EXPLICIT_FROZEN_FIXTURE",
    )
    assert explicit.facts[EXPLICIT_PAIR].fact_quality == "EXPLICIT_FROZEN_FIXTURE"
    assert explicit.hard_minutes(*EXPLICIT_PAIR) == 15

    # V4 #3: from_minutes now defaults to unknown provenance -> refused.
    untagged = MobilitySnapshot.from_minutes(
        source_provider_key="mock-mobility",
        minutes={EXPLICIT_PAIR: 15},
        captured_at="2026-09-18T00:00:00+00:00",
        operation_key="t2-untagged",
    )
    assert untagged.facts[EXPLICIT_PAIR].fact_quality is None
    assert untagged.estimate_minutes(*EXPLICIT_PAIR) == 15  # raw read still works
    with pytest.raises(KeyError):
        untagged.hard_minutes(*EXPLICIT_PAIR)

    # Genuinely absent pair -> refused.
    with pytest.raises(KeyError):
        explicit.hard_minutes(*MISSING_PAIR)


def test_sealed_legacy_synthetic_fixture_without_quality_fails_closed() -> None:
    """V4 #1: a legacy sealed fact that only says SYNTHETIC_FIXTURE must not be
    laundered into a hard fact; only an honest persisted quality is trusted."""

    from app.replanning.provider_snapshots import MobilitySnapshot

    legacy_synthetic = {
        "source_provider": "mock-mobility",
        "captured_at": "2026-10-10T09:00:00+00:00",
        "facts": [
            {
                "origin_key": "a",
                "destination_key": "b",
                "minutes": 20,
                "duration_min": 20,
                "route_provenance": "SYNTHETIC_FIXTURE",
                # NOTE: no "fact_quality" — exactly the ambiguous legacy shape.
            }
        ],
        "selected_facts": [],
    }
    snapshot = MobilitySnapshot.from_evidence(legacy_synthetic)
    assert snapshot.facts[("a", "b")].fact_quality is None
    with pytest.raises(KeyError):
        snapshot.hard_minutes("a", "b")


def test_explicit_fact_quality_survives_round_trip() -> None:
    from app.replanning.provider_snapshots import (
        EXPLICIT_FROZEN_FIXTURE,
        MobilitySnapshot,
    )

    original = MobilitySnapshot.from_minutes(
        source_provider_key="mock-mobility",
        minutes={("a", "b"): 15},
        fact_quality=EXPLICIT_FROZEN_FIXTURE,
    )
    reloaded = MobilitySnapshot.from_evidence(original.to_evidence())
    assert reloaded.facts[("a", "b")].fact_quality == EXPLICIT_FROZEN_FIXTURE
    assert reloaded.hard_minutes("a", "b") == 15


def test_resolve_fact_quality_is_fail_closed() -> None:
    from app.integrations.mobility.snapshot_capture import _resolve_fact_quality
    from app.replanning.provider_snapshots import MobilitySnapshot

    class NoAttestation:
        provider_key = "mystery"

    class RaisingAttestation:
        provider_key = "mystery"

        def fact_quality(self, _o, _d):
            raise RuntimeError("classifier blew up")

    # No declared quality and a classifier exception both yield UNKNOWN (None).
    assert _resolve_fact_quality(NoAttestation(), "a", "b", "mystery") is None
    assert _resolve_fact_quality(RaisingAttestation(), "a", "b", "mystery") is None

    # An unrecognized quality string is never trusted by the hard gate.
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="p",
        minutes={("a", "b"): 10},
        fact_quality="SOMETHING_UNMAPPED",
    )
    assert snapshot.facts[("a", "b")].fact_quality == "SOMETHING_UNMAPPED"
    with pytest.raises(KeyError):
        snapshot.hard_minutes("a", "b")


# --- V6: legacy provenance inference must be EXACT and fail-closed ---

import pytest as _pytest  # noqa: E402
from app.replanning.provider_snapshots import (  # noqa: E402
    EXPLICIT_FROZEN_FIXTURE,
    LIVE_PROVIDER_CAPTURE,
    MobilitySnapshot as _MS,
    _derive_fact_quality_from_provenance as _derive,
)

# Historical labels the old substring/prefix rules wrongly promoted to LIVE.
RISKY_LEGACY_LABELS = [
    "SEALED_REAL_REPLAY_WITH_EXPLICIT_SYNTHETIC_FALLBACK",
    "SEALED_REPLAY_OR_SYNTHETIC_FIXTURE_EXPLICIT",
    "CAPTURED_REPLAY",
    "RUNTIME_CAPTURE",
    "SEALED_INCIDENT_REPLAY",
]


@_pytest.mark.parametrize("label", RISKY_LEGACY_LABELS)
def test_v6_ambiguous_legacy_provenance_fails_closed(label) -> None:
    assert _derive({"route_provenance": label}) is None
    assert _derive({"provenance": label}) is None


def test_v6_only_exact_live_capture_promotes() -> None:
    assert _derive({"route_provenance": "LIVE_CAPTURE"}) == LIVE_PROVIDER_CAPTURE


def test_v6_explicit_quality_overrides_provenance_label() -> None:
    # Even a legacy LIVE-looking provenance cannot override an explicit tag.
    assert _derive(
        {"route_provenance": "CAPTURED_REPLAY", "fact_quality": EXPLICIT_FROZEN_FIXTURE}
    ) == EXPLICIT_FROZEN_FIXTURE
    assert _derive({"route_provenance": "LIVE_CAPTURE", "fact_quality": None}) == LIVE_PROVIDER_CAPTURE


def test_v6_sealed_synthetic_fallback_payload_hard_rejects() -> None:
    payload = {
        "source_provider": "mock-mobility",
        "captured_at": "2026-10-10T09:00:00+00:00",
        "facts": [
            {
                "origin_key": "a",
                "destination_key": "b",
                "minutes": 20,
                "duration_min": 20,
                "route_provenance": "SEALED_REAL_REPLAY_WITH_EXPLICIT_SYNTHETIC_FALLBACK",
            }
        ],
        "selected_facts": [],
    }
    snapshot = _MS.from_evidence(payload)
    assert snapshot.facts[("a", "b")].fact_quality is None
    with pytest.raises(KeyError):
        snapshot.hard_minutes("a", "b")
