"""Seal validation and replay for captured mobility evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.replanning.provider_snapshots import MobilitySnapshot


def load_sealed_provider_capture(
    path: str | Path, *, allow_synthetic: bool = False
) -> MobilitySnapshot:
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    supplied_checksum = str(payload.pop("checksum", ""))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not supplied_checksum or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != supplied_checksum:
        raise ValueError("provider capture checksum mismatch")
    provenance = str(payload.get("provenance") or "")
    if provenance == "SYNTHETIC_FIXTURE" and not allow_synthetic:
        raise ValueError("synthetic fixture is not a captured provider replay")
    if provenance not in {"LIVE_CAPTURE", "CAPTURED_REPLAY", "SYNTHETIC_FIXTURE"}:
        raise ValueError("provider capture provenance is unknown")
    return MobilitySnapshot.from_evidence(payload)


def replay_provider_capture(payload: Mapping[str, Any], *, allow_synthetic: bool = False) -> MobilitySnapshot:
    """Replay an already-loaded sealed payload through the normalizer."""

    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2)
    # Keep one validation path for files and in-memory replay tests.
    import tempfile

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        handle.write(serialized)
        temporary_path = handle.name
    try:
        return load_sealed_provider_capture(temporary_path, allow_synthetic=allow_synthetic)
    finally:
        Path(temporary_path).unlink(missing_ok=True)
