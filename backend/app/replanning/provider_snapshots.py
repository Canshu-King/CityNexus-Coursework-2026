from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


# Mobility fact quality.  A fact's provenance is recorded at capture so hard
# feasibility can trust real measurements while rejecting the mock fixture's
# blanket default (T2 / B1).
EXPLICIT_FROZEN_FIXTURE = "EXPLICIT_FROZEN_FIXTURE"
LIVE_PROVIDER_CAPTURE = "LIVE_PROVIDER_CAPTURE"
SYNTHETIC_DEFAULT = "SYNTHETIC_DEFAULT"

#: Fact qualities allowed to drive a deterministic hard-feasibility decision.
HARD_FEASIBLE_QUALITIES = frozenset({EXPLICIT_FROZEN_FIXTURE, LIVE_PROVIDER_CAPTURE})


@dataclass(frozen=True)
class TravelEstimateFact:
    origin_key: str
    destination_key: str
    minutes: int
    provider_key: str
    captured_at: str
    degraded: bool = False
    fallback_reason: str | None = None
    route_geometry: tuple[tuple[float, float], ...] = ()
    geometry_provider: str | None = None
    route_provenance: str | None = None
    cost_provenance: str | None = None
    time_basis: str | None = None
    distance_m: int | None = None
    walking_distance_m: int | None = None
    walking_min: int | None = None
    # The travel mode is what makes ``distance_m`` interpretable.  Without it a
    # total route distance could be silently read as walking distance.
    mode: str | None = None
    # Fact quality separates a *real* mobility measurement from the mock
    # fixture's blanket ``default_minutes``.  Only EXPLICIT_FROZEN_FIXTURE /
    # LIVE_PROVIDER_CAPTURE are hard-eligible.  ``None`` means the quality is
    # UNKNOWN, and ``MobilitySnapshot.hard_minutes`` FAILS CLOSED on it (V4 #1 /
    # V6) — unknown/legacy-unclassified provenance is never trusted for hard truth.
    fact_quality: str | None = None

    def __post_init__(self) -> None:
        if self.minutes < 0:
            raise ValueError("travel minutes must be non-negative")


def _derive_fact_quality_from_provenance(item: Mapping[str, Any]) -> str | None:
    """Honest quality for a sealed fact — EXACT and fail-closed (V6).

    1. An explicit persisted ``fact_quality`` always wins (capture/seal now write
       it for every current fact — see authoritative_v12 and snapshot_capture).
    2. The ONLY legacy provenance promoted to LIVE is the exact label
       ``LIVE_CAPTURE``, which historically was emitted solely by the real
       valhalla capture path.
    3. Everything else — ``CAPTURED_REPLAY``, any ``SEALED*`` (including labels
       that literally name ``SYNTHETIC_FALLBACK`` / ``SYNTHETIC_FIXTURE``),
       ``RUNTIME_CAPTURE``, and any unrecognised string — is UNKNOWN and refused.

    Rationale (V6 review): the previous substring/prefix rules ("LIVE" in prov,
    prov.startswith("SEALED"), promote CAPTURED_REPLAY) were wider than any
    actual proof.  Old authoritative_v12 code could normalise unrecognised
    provenance to ``CAPTURED_REPLAY``, and historical seal labels such as
    ``SEALED_REAL_REPLAY_WITH_EXPLICIT_SYNTHETIC_FALLBACK`` satisfied
    ``startswith("SEALED")`` — so "looks sealed/live" was being granted a hard-
    truth passport.  A genuinely-trusted fact carries an explicit quality; an
    ambiguous historical row must be re-captured to earn trust, not inferred.
    """

    explicit = item.get("fact_quality")
    if explicit is not None:
        return str(explicit)
    prov = str(item.get("route_provenance") or item.get("provenance") or "").upper()
    if prov == "LIVE_CAPTURE":
        # Exact, historically-proven real-capture label only.
        return LIVE_PROVIDER_CAPTURE
    return None


@dataclass(frozen=True)
class MobilitySnapshot:
    """Frozen travel facts consumed by the deterministic feasibility gate.

    External routing adapters run *before* the gate and their outputs are
    frozen into this value object.  The gate never performs a network call.
    """

    facts: Mapping[tuple[str, str], TravelEstimateFact]
    source_provider_key: str
    captured_at: str
    degraded: bool = False
    snapshot_id: str = ""
    operation_key: str | None = None
    provider_capabilities: Mapping[str, Any] = field(default_factory=dict)

    provider_key: str = "mobility-snapshot"
    is_frozen_snapshot: bool = True

    def __post_init__(self) -> None:
        frozen: dict[tuple[str, str], TravelEstimateFact] = {}
        for raw_pair, fact in self.facts.items():
            if len(raw_pair) != 2:
                raise ValueError("mobility snapshot keys must be origin/destination pairs")
            pair = (str(raw_pair[0]), str(raw_pair[1]))
            if pair != (fact.origin_key, fact.destination_key):
                raise ValueError("mobility snapshot key/fact identity mismatch")
            frozen[pair] = fact
        object.__setattr__(self, "facts", MappingProxyType(frozen))

    @classmethod
    def from_minutes(
        cls,
        *,
        source_provider_key: str,
        minutes: Mapping[tuple[str, str], int],
        captured_at: str | None = None,
        degraded: bool = False,
        snapshot_id: str | None = None,
        operation_key: str | None = None,
        route_geometries: Mapping[tuple[str, str], Any] | None = None,
        provider_capabilities: Mapping[str, Any] | None = None,
        # V4 #3: unknown provenance by default.  A caller building a genuinely
        # explicit frozen matrix must opt in with EXPLICIT_FROZEN_FIXTURE; the
        # builder cannot assume where a number came from.
        fact_quality: str | None = None,
    ) -> MobilitySnapshot:
        stamp = captured_at or datetime.now(timezone.utc).isoformat()
        return cls(
            facts={
                pair: TravelEstimateFact(
                    origin_key=pair[0],
                    destination_key=pair[1],
                    minutes=int(value),
                    provider_key=source_provider_key,
                    captured_at=stamp,
                    degraded=degraded,
                    route_geometry=_normalize_geometry((route_geometries or {}).get(pair)),
                    geometry_provider=(
                        source_provider_key if (route_geometries or {}).get(pair) else None
                    ),
                    fact_quality=fact_quality,
                )
                for pair, value in minutes.items()
            },
            source_provider_key=source_provider_key,
            captured_at=stamp,
            degraded=degraded,
            snapshot_id=snapshot_id or str(uuid4()),
            operation_key=operation_key,
            provider_capabilities=dict(provider_capabilities or {}),
        )

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        fact = self.facts.get((origin_key, destination_key))
        if fact is None:
            raise KeyError(f"missing frozen travel estimate: {origin_key}|{destination_key}")
        return fact.minutes

    def hard_minutes(self, origin_key: str, destination_key: str) -> int:
        """Travel minutes allowed into a deterministic hard-feasibility decision.

        Unlike :meth:`estimate_minutes` — the raw accessor the offline demo and
        geometry use — this is the *truth gate* for replan feasibility and is a
        strict ALLOWLIST (V3-1 / V4 #1 / V6): only a fact whose quality is
        ``EXPLICIT_FROZEN_FIXTURE`` or ``LIVE_PROVIDER_CAPTURE`` may pass.  A leg
        positively tagged ``SYNTHETIC_DEFAULT`` (the mock blanket constant), or
        one with NO recorded quality / unknown / legacy-unclassified provenance,
        all raise ``KeyError`` and fail closed — so the caller reports
        ``MOBILITY_DATA_UNAVAILABLE`` instead of trusting a number we cannot
        source.  Genuine sealed replays keep working because they carry an
        explicit, persisted ``fact_quality`` (V4 #1), not because untagged rows
        are trusted.
        """

        fact = self.facts.get((origin_key, destination_key))
        if fact is None:
            raise KeyError(f"missing frozen travel estimate: {origin_key}|{destination_key}")
        # True allowlist (V3-1): only an explicitly trusted quality may drive
        # hard feasibility.  A pre-v2 snapshot can hold an *untagged* blanket
        # default 20, so None / unknown / LEGACY must fail closed — we would
        # rather say "mobility unavailable" than trust a number we cannot source.
        if fact.fact_quality not in HARD_FEASIBLE_QUALITIES:
            raise KeyError(
                f"fact quality {fact.fact_quality!r} is not eligible for hard "
                f"feasibility: {origin_key}|{destination_key}"
            )
        return fact.minutes

    def to_evidence(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "operation_key": self.operation_key,
            "captured_at": self.captured_at,
            "source_provider": self.source_provider_key,
            "degraded": self.degraded,
            "provider_capabilities": dict(self.provider_capabilities),
            "facts": [
                {
                    "origin_key": fact.origin_key,
                    "destination_key": fact.destination_key,
                    "minutes": fact.minutes,
                    "provider": fact.provider_key,
                    "captured_at": fact.captured_at,
                    "degraded": fact.degraded,
                    "fallback_reason": fact.fallback_reason,
                    "route_geometry": [list(point) for point in fact.route_geometry],
                    "geometry_provider": fact.geometry_provider,
                    "route_provenance": fact.route_provenance,
                    "cost_provenance": fact.cost_provenance,
                    "time_basis": fact.time_basis,
                    "distance_m": fact.distance_m,
                    "walking_distance_m": fact.walking_distance_m,
                    "walking_min": fact.walking_min,
                    "mode": fact.mode,
                    "fact_quality": fact.fact_quality,
                }
                for _, fact in sorted(self.facts.items())
            ],
        }

    @classmethod
    def from_evidence(cls, payload: Mapping[str, Any]) -> MobilitySnapshot:
        facts: dict[tuple[str, str], TravelEstimateFact] = {}
        # V12 persists the complete directed matrix plus the exact mode fact
        # selected for each concrete leg.  Apply selected_facts last so a
        # replay/replan follows the original plan's frozen mode decision,
        # while still retaining matrix alternatives for Trip Studio edits.
        evidence_items = [
            *[item for item in payload.get("facts", []) if isinstance(item, Mapping)],
            *[item for item in payload.get("selected_facts", []) if isinstance(item, Mapping)],
        ]
        for item in evidence_items:
            if not isinstance(item, Mapping):
                continue
            origin = str(item.get("origin_key") or "")
            destination = str(item.get("destination_key") or "")
            if not origin or not destination:
                continue
            facts[(origin, destination)] = TravelEstimateFact(
                origin_key=origin,
                destination_key=destination,
                minutes=int(item["minutes"]),
                provider_key=str(
                    item.get("provider") or payload.get("source_provider") or "unknown"
                ),
                captured_at=str(item.get("captured_at") or payload.get("captured_at") or ""),
                degraded=bool(item.get("degraded", False)),
                fallback_reason=(
                    str(item["fallback_reason"])
                    if item.get("fallback_reason") is not None
                    else None
                ),
                route_geometry=_normalize_geometry(item.get("route_geometry")),
                geometry_provider=(
                    str(item["geometry_provider"])
                    if item.get("geometry_provider") is not None
                    else None
                ),
                route_provenance=(
                    str(item["route_provenance"])
                    if item.get("route_provenance") is not None
                    else None
                ),
                cost_provenance=(
                    str(item["cost_provenance"])
                    if item.get("cost_provenance") is not None
                    else None
                ),
                time_basis=(
                    str(item["time_basis"]) if item.get("time_basis") is not None else None
                ),
                distance_m=(
                    int(item["distance_m"]) if item.get("distance_m") is not None else None
                ),
                walking_distance_m=(
                    int(item["walking_distance_m"])
                    if item.get("walking_distance_m") is not None
                    else None
                ),
                walking_min=(
                    int(item["walking_min"]) if item.get("walking_min") is not None else None
                ),
                mode=(
                    str(item["mode"]).upper()
                    if item.get("mode") is not None
                    else None
                ),
                fact_quality=_derive_fact_quality_from_provenance(item),
            )
        return cls(
            facts=facts,
            source_provider_key=str(payload.get("source_provider") or "unknown"),
            captured_at=str(payload.get("captured_at") or ""),
            degraded=bool(payload.get("degraded", False)),
            snapshot_id=str(payload.get("snapshot_id") or ""),
            operation_key=(
                str(payload["operation_key"]) if payload.get("operation_key") is not None else None
            ),
            provider_capabilities=(
                dict(payload.get("provider_capabilities"))
                if isinstance(payload.get("provider_capabilities"), Mapping)
                else {}
            ),
        )


def _normalize_geometry(value: Any) -> tuple[tuple[float, float], ...]:
    """Normalize provider/GeoJSON geometry into immutable ``[lon, lat]`` pairs."""

    if isinstance(value, Mapping):
        geometry = value.get("geometry") if value.get("type") == "Feature" else value
        if isinstance(geometry, Mapping):
            if geometry.get("type") == "LineString":
                value = geometry.get("coordinates")
            elif geometry.get("type") == "MultiLineString":
                lines = geometry.get("coordinates")
                value = lines[0] if isinstance(lines, list) and lines else None
    if value is None:
        return ()
    if hasattr(value, "coordinates"):
        value = value.coordinates
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[tuple[float, float]] = []
    for point in value:
        if isinstance(point, Mapping):
            try:
                lon = float(point.get("longitude", point.get("lon")))
                lat = float(point.get("latitude", point.get("lat")))
            except (TypeError, ValueError):
                continue
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                lon = float(point[0])
                lat = float(point[1])
            except (TypeError, ValueError):
                continue
        else:
            continue
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            normalized.append((lon, lat))
    return tuple(normalized) if len(normalized) >= 2 else ()
