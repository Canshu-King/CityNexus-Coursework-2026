from __future__ import annotations

import json
from types import SimpleNamespace

from app.main import _replacement_activity_metadata


def test_replacement_metadata_uses_candidate_place_facts_only() -> None:
    candidate = SimpleNamespace(
        category="museum",
        latitude=34.6919721,
        longitude=135.7927067,
        coordinate_system="WGS84",
        opening_hours_json=json.dumps({"status": "UNKNOWN", "raw": None, "intervals": []}),
        metadata_json=json.dumps(
            {
                "cost_evidence": {"status": "KNOWN", "amount_minor": 0, "currency": "JPY"},
                "price_known": True,
            }
        ),
    )
    choice = SimpleNamespace(
        place_id="real-place-candidate",
        reason_codes=("DISRUPTED_ACTIVITY_REPLACED",),
    )

    metadata = json.loads(
        _replacement_activity_metadata(
            candidate,
            choice,
            family="F0_SAME_SLOT_REPLACEMENT",
            replaced_place_id="real-place-closed",
        )
    )

    assert metadata["semantic_key"] == "real-place-candidate"
    assert metadata["category"] == "museum"
    assert metadata["opening_hours"]["status"] == "UNKNOWN"
    assert metadata["cost_evidence"]["status"] == "KNOWN"
    assert metadata["cost_status"] == "KNOWN"
    assert metadata["price_known"] is True
    assert metadata["latitude"] == 34.6919721
    assert metadata["longitude"] == 135.7927067
    assert metadata["replaced_place_id"] == "real-place-closed"
