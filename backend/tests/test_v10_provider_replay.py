import hashlib
import json

import pytest

from app.integrations.mobility.replay import load_sealed_provider_capture


def _sealed(tmp_path, provenance="CAPTURED_REPLAY"):
    payload = {
        "provenance": provenance,
        "source_provider": "valhalla",
        "captured_at": "2026-09-12T08:00:00+00:00",
        "facts": [
            {
                "origin_key": "a",
                "destination_key": "b",
                "minutes": 17,
                "provider": "valhalla",
                "captured_at": "2026-09-12T08:00:00+00:00",
            }
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["checksum"] = hashlib.sha256(canonical.encode()).hexdigest()
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_v10_provider_capture_replays_through_the_same_normalizer(tmp_path):
    snapshot = load_sealed_provider_capture(_sealed(tmp_path))
    assert snapshot.estimate_minutes("a", "b") == 17
    assert snapshot.facts[("a", "b")].provider_key == "valhalla"


def test_v10_provider_replay_rejects_tampering_and_unlabelled_synthetic(tmp_path):
    path = _sealed(tmp_path, provenance="SYNTHETIC_FIXTURE")
    with pytest.raises(ValueError, match="synthetic"):
        load_sealed_provider_capture(path)
    with pytest.raises(ValueError, match="synthetic"):
        load_sealed_provider_capture(path, allow_synthetic=False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["facts"][0]["minutes"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_sealed_provider_capture(path, allow_synthetic=True)
