from __future__ import annotations

import json
from typing import Any, Iterable


def canonical_place_tags(*, category: str | None, tags: Iterable[str]) -> set[str]:
    """Return bounded provider-backed category/tags used for preference fit."""

    result = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    normalized_category = str(category or "").strip().lower()
    if normalized_category:
        result.add(normalized_category)
        result.add(f"category:{normalized_category}")
    return result


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


def _preference_matches_candidate(key: str, tags: set[str]) -> bool:
    """Evaluate a bounded positive or avoidance interest against provider tags.

    Avoidance is still soft evidence: it changes ranking only.  Treating
    ``avoid:museum`` as a first-class negative signal keeps a voluntary reject
    from merely diluting every candidate's score.
    """

    normalized = str(key or "").strip().lower()
    if normalized.startswith("avoid:"):
        target = normalized.removeprefix("avoid:").strip()
        if not target:
            return False
        return not (target in tags or f"category:{target}" in tags)
    return normalized in tags or f"category:{normalized}" in tags


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

    return verified_interest_preference_breakdown(
        preferences, candidate_tags=candidate_tags
    )["effective_preference_fit"]


def verified_interest_preference_breakdown(
    preferences: Iterable[Any], *, candidate_tags: Iterable[str]
) -> dict[str, Any]:
    """Expose preference provenance without promoting it into feasibility truth."""

    tags = {str(tag).strip().lower() for tag in candidate_tags if str(tag).strip()}
    explicit_total = explicit_match = 0.0
    learned_total = learned_match = 0.0
    mission_total = mission_match = 0.0
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
        source = str(getattr(item, "source", "")).upper()
        key = str(getattr(item, "preference_key", "")).strip().lower()
        matched = _preference_matches_candidate(key, tags)
        if source == "EXPLICIT_MISSION":
            mission_total += weight
            mission_match += weight if matched else 0.0
        elif source == "LEARNED_SOFT":
            learned_total += weight
            learned_match += weight if matched else 0.0
        else:
            explicit_total += weight
            explicit_match += weight if matched else 0.0
    explicit_fit = explicit_match / explicit_total if explicit_total else None
    learned_fit = learned_match / learned_total if learned_total else None
    mission_fit = mission_match / mission_total if mission_total else None
    effective_fit = next((fit for fit in (mission_fit, explicit_fit, learned_fit) if fit is not None), None)
    tier = "CURRENT_EXPLICIT_MISSION" if mission_total else "EXPLICIT_PERSISTED_PREFERENCE" if explicit_total else "LEARNED_SOFT" if learned_total else "NEUTRAL"
    return {
        "current_mission_preference_fit": mission_fit,
        "explicit_preference_fit": explicit_fit,
        "learned_soft_preference_fit": learned_fit,
        "effective_preference_fit": effective_fit,
        "effective_preference_tier": tier,
    }
