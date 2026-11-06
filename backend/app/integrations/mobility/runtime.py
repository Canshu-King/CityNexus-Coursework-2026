from __future__ import annotations

from dataclasses import replace
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CityService, Journey, Place
from app.integrations.mobility.snapshot_capture import (
    MobilitySnapshotCaptureResult,
    capture_mobility_snapshot,
)
from app.integrations.mobility.valhalla_http import (
    ValhallaHTTPMobilityProvider,
    ValhallaProviderError,
)
from app.integrations.mock.providers import MockMobilityProvider


def _place_lookup(session: Session, key: str) -> dict[str, float]:
    """Resolve a routing key to coordinates.

    Two key spaces are in play and they must agree, or every leg reports
    MOBILITY_DATA_UNAVAILABLE:

    * activity metadata carries the canonical structural identity
      ``v12:{journey_id}:{arrival-anchor|departure-anchor}``;
    * the materialised Place row for that same anchor may be stored under the
      bare structural key ``arrival-anchor``.

    Trying both keeps the frozen evidence and the gate on one identity.
    """

    candidates = [key]
    if ":arrival-anchor" in key or ":departure-anchor" in key:
        candidates.append(key.rsplit(":", 1)[-1])
    place = None
    for candidate in candidates:
        place = session.get(Place, candidate)
        if place is None:
            service = session.get(CityService, candidate)
            if service is not None and service.place_id:
                place = session.get(Place, service.place_id)
        if place is not None:
            break
    if place is None:
        # Structural nodes are materialised with a *generated* Place id, so the
        # canonical ``semantic_key`` is not a primary key.  Resolve it through
        # the activity metadata instead of failing the whole recovery
        # (truth blocker B5).
        from app.db.models import Activity

        for candidate in candidates:
            rows = session.scalars(
                select(Activity).where(Activity.metadata_json.like(f"%{candidate}%"))
            ).all()
            for row in rows:
                metadata = row.metadata_json or ""
                if f'"{candidate}"' not in metadata:
                    continue
                if row.place_id:
                    place = session.get(Place, row.place_id)
                    if place is not None:
                        break
            if place is not None:
                break
    if place is None:
        raise KeyError(f"no routable coordinates for {key}")
    return {"latitude": float(place.latitude), "longitude": float(place.longitude)}


def capture_runtime_mobility_snapshot(
    session: Session,
    journey: Journey,
    pairs: Iterable[tuple[str, str]],
    *,
    operation_key: str,
    captured_at: str | None = None,
    require_explicit_facts: bool = False,
) -> MobilitySnapshotCaptureResult:
    """Capture one immutable mobility reality for a product operation.

    The default/offline profile uses the deterministic mock provider. When an
    opt-in Valhalla URL is configured, Valhalla becomes the primary provider.
    For *soft* (non-hard) captures the deterministic mock stays an explicit
    pair-level fallback so a transient live outage degrades rather than
    aborts.  For *hard* replan captures (``require_explicit_facts=True``) there
    is deliberately NO mock fallback: a leg the live provider could not measure
    must stay unavailable instead of inheriting a fixture number and being
    laundered into a hard feasibility decision (V4 #4).  The returned snapshot
    is the only object allowed into the feasibility gate.

    ``require_explicit_facts`` is for the replan path: a pair with no explicit
    frozen or measured fact must fail closed rather than inherit the mock
    fixture's blanket ``default_minutes`` constant.  The constant is not a
    measurement, so it must never become hard replan evidence
    (T2 / docs/MOBILITY_TRUTH_CONTRACT.md).
    """

    normalized_pairs = tuple(sorted({(str(a), str(b)) for a, b in pairs if a and b and a != b}))
    mock = MockMobilityProvider(
        place_lookup=lambda key: _place_lookup(session, key),
        strict=require_explicit_facts,
    )
    if not settings.live_valhalla_url:
        return capture_mobility_snapshot(
            normalized_pairs,
            primary=mock,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    with httpx.Client(timeout=10.0) as client:
        primary: Any = ValhallaHTTPMobilityProvider(
            client=client,
            base_url=settings.live_valhalla_url,
            place_lookup=lambda key: _place_lookup(session, key),
        )
        try:
            capabilities = primary.status_capabilities()
        except ValhallaProviderError as exc:
            capabilities = {
                "status": "UNAVAILABLE",
                "has_live_traffic": False,
                "error_type": type(exc).__name__,
            }
        # V4 #4: a live-configured HARD capture must not fall back to the mock
        # fixture.  Soft/offline captures keep the pair-level mock fallback.
        hard_capture = require_explicit_facts
        captured = capture_mobility_snapshot(
            normalized_pairs,
            primary=primary,
            fallback=None if hard_capture else mock,
            captured_at=captured_at,
            operation_key=operation_key,
        )
        # V5 blocker-1: with a live routing provider configured, a leg the live
        # provider FAILED must not enter hard feasibility just because the mock
        # happened to carry an explicit matrix value for it.  The bulk replan
        # capture runs soft (fallback=mock), so we DOWNGRADE every mock-fallback
        # fact to UNKNOWN quality (kept for soft/display reads, but rejected by
        # hard_minutes) -> the per-candidate hard gate refuses it, the on-demand
        # strict recapture (live, fallback=None) reports that leg unavailable, and
        # the candidate is excluded while a fully-live candidate still proceeds.
        # Offline demos have no fallback pairs, so they keep using their
        # legitimate explicit mock facts as hard truth.
        snapshot = captured.snapshot
        if captured.fallback_pairs:
            facts = dict(snapshot.facts)
            for pair in captured.fallback_pairs:
                fact = facts.get(pair)
                if fact is not None and fact.fact_quality is not None:
                    facts[pair] = replace(fact, fact_quality=None)
            snapshot = replace(snapshot, facts=facts)
        return replace(
            captured,
            snapshot=replace(
                snapshot,
                provider_capabilities={"valhalla": capabilities},
            ),
        )


def capture_journey_mobility_matrix(
    nodes: Iterable[dict[str, Any]],
    *,
    operation_key: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Capture one Journey-owned Valhalla matrix per supported costing.

    The node keys are the structural Journey identities, not a city-wide
    centroid or a reusable place-only snapshot.  The caller freezes this
    result into PlanVersion evidence; it is intentionally not a fallback to
    the deterministic fixture provider.
    """

    captured = captured_at or datetime.now(timezone.utc).isoformat()
    normalized_nodes = [
        {
            "key": str(item["key"]),
            "role": str(item.get("role") or "ACTIVITY"),
            "place_id": item.get("place_id"),
            "external_id": item.get("external_id"),
            "latitude": float(item["latitude"]),
            "longitude": float(item["longitude"]),
        }
        for item in nodes
    ]
    result: dict[str, Any] = {
        "status": "NOT_CONFIGURED",
        "provider": "valhalla",
        "operation_key": operation_key,
        "captured_at": captured,
        "nodes": normalized_nodes,
        "facts": [],
        "supported_modes": [],
        "route_provenance": "NONE",
        "cost_provenance": "NONE",
        "time_basis": "NOT_CAPTURED",
    }
    if not settings.live_valhalla_url or not normalized_nodes:
        return result

    keys = [item["key"] for item in normalized_nodes]
    lookup = {item["key"]: item for item in normalized_nodes}
    facts: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=10.0) as client:
            capability_by_deployment: dict[str, Any]
            for mode, costing in (("WALK", "pedestrian"), ("AUTO", "auto")):
                provider = ValhallaHTTPMobilityProvider(
                    client=client,
                    base_url=settings.live_valhalla_url,
                    place_lookup=lambda key: lookup[key],
                    costing=costing,
                )
                if mode == "WALK":
                    try:
                        capability_by_deployment = provider.status_capabilities()
                    except ValhallaProviderError as exc:
                        capability_by_deployment = {
                            "status": "UNAVAILABLE",
                            "has_live_traffic": False,
                            "error_type": type(exc).__name__,
                        }
                # g14b: the provider refuses requests above 100 cells (sources x targets), and
                # the count is cells, not the "locations" figure in its error text.  Square
                # blocks over one key list stay inside the cap; cross blocks pair sub-chunks of
                # 5 keys so their union never exceeds 10 entries.
                block_size = max(1, int(100 ** 0.5))
                cross_size = block_size // 2
                matrix = [
                    [None for _ in range(len(keys))] for _ in range(len(keys))
                ]
                block_starts = list(range(0, len(keys), block_size))
                for start in block_starts:
                    block_keys = keys[start : start + block_size]
                    block = provider.matrix(block_keys)
                    for local_row, source_key in enumerate(block_keys):
                        row = matrix[keys.index(source_key)]
                        for local_col, target_key in enumerate(block_keys):
                            row[keys.index(target_key)] = block[local_row][local_col]
                for source_start in block_starts:
                    for target_start in block_starts:
                        if source_start == target_start:
                            continue
                        source_block = keys[source_start : source_start + block_size]
                        target_block = keys[target_start : target_start + block_size]
                        for source_chunk_start in range(0, len(source_block), cross_size):
                            source_chunk = source_block[source_chunk_start : source_chunk_start + cross_size]
                            for target_chunk_start in range(0, len(target_block), cross_size):
                                target_chunk = target_block[target_chunk_start : target_chunk_start + cross_size]
                                union_keys = list(dict.fromkeys(source_chunk + target_chunk))
                                if len(union_keys) ** 2 > 100:
                                    raise ValhallaProviderError(
                                        "matrix chunk exceeds the 100-cell provider cap"
                                    )
                                block = provider.matrix(union_keys)
                                for source_key in source_chunk:
                                    row = matrix[keys.index(source_key)]
                                    for target_key in target_chunk:
                                        row[keys.index(target_key)] = block[
                                            union_keys.index(source_key)
                                        ][union_keys.index(target_key)]
                for source_index, row in enumerate(matrix):
                    for target_index, cell in enumerate(row):
                        if source_index == target_index or cell is None:
                            continue
                        facts.append(
                            {
                                "from_key": keys[source_index],
                                "to_key": keys[target_index],
                                "mode": mode,
                                "duration_min": cell.duration_min,
                                "distance_km": None
                                if cell.distance_m is None
                                else cell.distance_m / 1000.0,
                                "captured_at": captured,
                                "route_provider": "valhalla",
                                "route_provenance": "LIVE_CAPTURE",
                                "fact_quality": "LIVE_PROVIDER_CAPTURE",
                                "cost_provenance": "CITYNEXUS_COST_ESTIMATE",
                                "time_basis": "VALHALLA_SOURCES_TO_TARGETS",
                                "matrix_cell": cell.raw,
                            }
                        )
    except ValhallaProviderError as exc:
        result["status"] = "FAILED"
        result["error_type"] = type(exc).__name__
        return result

    result.update(
        {
            "status": "CAPTURED" if facts else "NO_RESULTS",
            "facts": facts,
            "supported_modes": sorted({str(item["mode"]) for item in facts}),
            "route_provenance": "LIVE_CAPTURE" if facts else "NONE",
            "cost_provenance": "CITYNEXUS_COST_ESTIMATE" if facts else "NONE",
            "time_basis": "VALHALLA_SOURCES_TO_TARGETS" if facts else "NOT_CAPTURED",
            "provider_capabilities": {"valhalla": capability_by_deployment},
        }
    )
    return result
