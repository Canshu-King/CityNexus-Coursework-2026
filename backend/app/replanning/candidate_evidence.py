from __future__ import annotations

import json
from typing import Any, Iterable


def _truthy_preference_value(raw: Any) -> bool:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw > 0
    if isinstance(raw, str):
        return raw.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    return bool(raw)


def verified_interest_preference_fit(
    preferences: Iterable[Any],
    *,
    candidate_tags: Iterable[str],
) -> float | None:
    """Return a weighted fit using only persisted positive interest preferences.

    ``None`` means there is no verified positive-interest evidence and callers
    should use their documented neutral policy. This deliberately avoids using
    similarity-to-the-disrupted-place as a stand-in for traveler preference.
    """

    tags = {str(tag) for tag in candidate_tags if tag}
    total_weight = 0.0
    matched_weight = 0.0
    for item in preferences:
        if str(getattr(item, "category", "")) != "interest":
            continue
        if not _truthy_preference_value(getattr(item, "value_json", None)):
            continue
        try:
            weight = max(0.0, float(getattr(item, "weight", 0.0)))
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        total_weight += weight
        if str(getattr(item, "preference_key", "")) in tags:
            matched_weight += weight
    if total_weight <= 0:
        return None
    return max(0.0, min(1.0, matched_weight / total_weight))
