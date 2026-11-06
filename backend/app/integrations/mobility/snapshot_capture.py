from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol
from uuid import uuid4

from app.replanning.provider_snapshots import (
    EXPLICIT_FROZEN_FIXTURE,
    LIVE_PROVIDER_CAPTURE,
    SYNTHETIC_DEFAULT,
    MobilitySnapshot,
    TravelEstimateFact,
)


class MinuteEstimator(Protocol):
    provider_key: str

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int: ...


def _estimate_with_optional_geometry(
    provider: MinuteEstimator, origin: str, destination: str
) -> tuple[int, tuple[tuple[float, float], ...]]:
    estimate = getattr(provider, "estimate", None)
    if callable(estimate):
        result: Any = estimate(origin, destination)
        minutes = int(result.duration_min)
        geometry = _normalize_geometry(getattr(result, "geometry", None))
        return minutes, geometry
    return int(provider.estimate_minutes(origin, destination)), ()


def _normalize_geometry(value: Any) -> tuple[tuple[float, float], ...]:
    if isinstance(value, dict):
        if value.get("type") == "Feature":
            value = value.get("geometry")
        if isinstance(value, dict):
            value = value.get("coordinates") if value.get("type") == "LineString" else value
    if hasattr(value, "coordinates"):
        value = value.coordinates
    if not isinstance(value, (list, tuple)):
        return ()
    points: list[tuple[float, float]] = []
    for point in value:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                lon, lat = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                points.append((lon, lat))
    return tuple(points) if len(points) >= 2 else ()


def _resolve_fact_quality(
    provider: MinuteEstimator, origin: str, destination: str, provider_key: str
) -> str | None:
    """Classify a captured fact so hard feasibility can trust or reject it.

    V4 #2 (fully fail-closed): a provider that distinguishes an explicit fixture
    entry from its blanket default exposes ``fact_quality``; that attestation is
    the only source of truth here.  A provider that cannot attest a leg, or
    whose classifier raises, yields ``None`` (UNKNOWN), which hard feasibility
    rejects.  We never infer trustworthiness from the provider's *name* — a mock
    or fixture label is not a measurement (T2 / B1), and doing so would launder
    an unmeasured leg into a hard fact.
    """

    provider_quality = getattr(provider, "fact_quality", None)
    if callable(provider_quality):
        try:
            return str(provider_quality(origin, destination))
        except Exception:
            return None
    return None


class MobilitySnapshotCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class MobilitySnapshotCaptureResult:
    snapshot: MobilitySnapshot
    failed_primary_pairs: tuple[tuple[str, str], ...]
    fallback_pairs: tuple[tuple[str, str], ...]

    @property
    def degraded(self) -> bool:
        return self.snapshot.degraded


def capture_mobility_snapshot(
    pairs: Iterable[tuple[str, str]],
    *,
    primary: MinuteEstimator,
    fallback: MinuteEstimator | None = None,
    captured_at: str | None = None,
    operation_key: str | None = None,
    snapshot_id: str | None = None,
) -> MobilitySnapshotCaptureResult:
    """Capture external mobility facts before the deterministic feasibility gate.

    The function deliberately performs provider calls outside the gate and freezes
    their results into ``MobilitySnapshot``. If the primary provider fails for a
    pair, an explicit fallback may fill that pair; the resulting snapshot is then
    marked degraded and every fact retains its own provider provenance.
    """

    stamp = captured_at or datetime.now(timezone.utc).isoformat()
    normalized_pairs = tuple(sorted({(str(a), str(b)) for a, b in pairs if a and b}))
    facts: dict[tuple[str, str], TravelEstimateFact] = {}
    primary_failures: list[tuple[str, str]] = []
    fallback_pairs: list[tuple[str, str]] = []
    fallback_reasons: dict[tuple[str, str], str] = {}
    geometries: dict[tuple[str, str], tuple[tuple[float, float], ...]] = {}
    geometry_providers: dict[tuple[str, str], str] = {}

    for origin, destination in normalized_pairs:
        try:
            minutes, geometry = _estimate_with_optional_geometry(primary, origin, destination)
            if minutes < 0:
                raise ValueError("negative travel minutes")
            provider_key = str(primary.provider_key)
            active_provider: MinuteEstimator = primary
            degraded = False
            if geometry:
                geometries[(origin, destination)] = geometry
                geometry_providers[(origin, destination)] = provider_key
            route_provenance = str(
                getattr(
                    primary,
                    "capture_provenance",
                    "SYNTHETIC_FIXTURE"
                    if provider_key.lower().startswith(("mock", "fixture"))
                    else "LIVE_PROVIDER_CAPTURE",
                )
            )
            time_basis = str(
                getattr(
                    primary,
                    "time_basis",
                    "MOCK_DETERMINISTIC" if degraded else "PROVIDER_CAPTURED_AT",
                )
            )
        except Exception as primary_exc:  # external-provider boundary
            primary_failures.append((origin, destination))
            if fallback is None:
                raise MobilitySnapshotCaptureError(
                    f"mobility fact unavailable for {origin}|{destination}: "
                    f"{type(primary_exc).__name__}"
                ) from primary_exc
            try:
                minutes, geometry = _estimate_with_optional_geometry(fallback, origin, destination)
                if minutes < 0:
                    raise ValueError("negative fallback travel minutes")
            except Exception as fallback_exc:  # external-provider boundary
                raise MobilitySnapshotCaptureError(
                    f"primary and fallback mobility facts unavailable for {origin}|{destination}"
                ) from fallback_exc
            provider_key = str(fallback.provider_key)
            active_provider = fallback
            degraded = True
            fallback_pairs.append((origin, destination))
            fallback_reasons[(origin, destination)] = type(primary_exc).__name__
            if geometry:
                geometries[(origin, destination)] = geometry
                geometry_providers[(origin, destination)] = provider_key
            route_provenance = str(
                getattr(
                    fallback,
                    "capture_provenance",
                    "SYNTHETIC_FIXTURE"
                    if provider_key.lower().startswith(("mock", "fixture"))
                    else "FALLBACK_PROVIDER_CAPTURE",
                )
            )
            time_basis = str(
                getattr(
                    fallback,
                    "time_basis",
                    "MOCK_DETERMINISTIC" if degraded else "PROVIDER_CAPTURED_AT",
                )
            )

        facts[(origin, destination)] = TravelEstimateFact(
            origin_key=origin,
            destination_key=destination,
            minutes=minutes,
            provider_key=provider_key,
            captured_at=stamp,
            degraded=degraded,
            fallback_reason=fallback_reasons.get((origin, destination)),
            route_geometry=geometries.get((origin, destination), ()),
            geometry_provider=geometry_providers.get((origin, destination)),
            route_provenance=route_provenance,
            cost_provenance="CITYNEXUS_COST_ESTIMATE",
            time_basis=time_basis,
            mode=str(
                getattr(primary, "mode", None)
                or getattr(fallback, "mode", None)
                or ""
            ).upper()
            or None,
            fact_quality=_resolve_fact_quality(
                active_provider, origin, destination, provider_key
            ),
        )

    snapshot = MobilitySnapshot(
        facts=facts,
        source_provider_key=str(primary.provider_key),
        captured_at=stamp,
        degraded=bool(primary_failures),
        snapshot_id=snapshot_id or str(uuid4()),
        operation_key=operation_key,
    )
    return MobilitySnapshotCaptureResult(
        snapshot=snapshot,
        failed_primary_pairs=tuple(primary_failures),
        fallback_pairs=tuple(fallback_pairs),
    )
