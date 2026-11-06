"""Recovery-surface sufficiency rules for bounded open-world expansion."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class RecoverySurfaceDecision:
    state: str
    grounded_option_count: int
    should_expand: bool
    reason: str


def _semantic_option_key(option: dict[str, Any]) -> str:
    """Collapse duplicate proposal ids that represent the same grounded future."""

    places = sorted(
        {
            str(item)
            for item in (option.get("fit_subject_place_ids") or [])
            if item
        }
    )
    tags = sorted(
        {
            str(item).strip().lower()
            for item in (option.get("fit_subject_tags") or [])
            if str(item).strip()
        }
    )
    plan_id = str(option.get("plan_id") or "")
    payload = {"places": places, "tags": tags}
    if not places and not tags:
        payload["plan_id"] = plan_id
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def distinct_grounded_options(options: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return materially distinct server-grounded recovery options."""

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for option in options:
        if str(option.get("status") or "").upper() != "GROUNDED_FEASIBLE":
            continue
        key = _semantic_option_key(option)
        if key in seen:
            continue
        seen.add(key)
        result.append(option)
    return result


def recovery_surface_decision(
    *,
    options: Iterable[dict[str, Any]],
    discovery_attempted: bool,
    discovery_degraded: bool = False,
    collective_choice_needed: bool = False,
    mediation_needs_progress: bool = False,
) -> RecoverySurfaceDecision:
    """Decide whether another bounded city-discovery step can change the recovery."""

    grounded = distinct_grounded_options(options)
    count = len(grounded)

    if discovery_degraded:
        return RecoverySurfaceDecision(
            state="DISCOVERY_DEGRADED",
            grounded_option_count=count,
            should_expand=False,
            reason="OPEN_WORLD_PROVIDER_DEGRADED",
        )

    if count >= 2:
        return RecoverySurfaceDecision(
            state="SUFFICIENT_GROUNDED_OPTIONS",
            grounded_option_count=count,
            should_expand=False,
            reason="MATERIAL_CHOICE_ALREADY_EXISTS",
        )

    needs_choice = bool(collective_choice_needed or mediation_needs_progress)
    if discovery_attempted:
        return RecoverySurfaceDecision(
            state="DISCOVERY_EXHAUSTED",
            grounded_option_count=count,
            should_expand=False,
            reason=(
                "NO_ADDITIONAL_GROUNDED_OPTION"
                if count <= 1
                else "DISCOVERY_BUDGET_CONSUMED"
            ),
        )

    if count == 0:
        return RecoverySurfaceDecision(
            state="NO_GROUNDED_OPTION",
            grounded_option_count=0,
            should_expand=True,
            reason="NO_GROUNDED_RECOVERY",
        )

    if count == 1 and needs_choice:
        return RecoverySurfaceDecision(
            state="SINGLE_GROUNDED_OPTION",
            grounded_option_count=1,
            should_expand=True,
            reason="MEANINGFUL_CHOICE_REQUIRES_MORE_SURFACE",
        )

    return RecoverySurfaceDecision(
        state="SINGLE_GROUNDED_OPTION",
        grounded_option_count=1,
        should_expand=False,
        reason="ONE_GROUNDED_RECOVERY_IS_ACTIONABLE",
    )
