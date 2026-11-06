"""Small deterministic receipt for human-value decision sensitivity.

This module deliberately does not estimate confidence.  It answers one bounded
question from server-owned horizon facts: do two individually feasible,
resource-conflicting actions reverse when the traveller values option A versus
option B?  If not, asking the traveller is noise.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _has_current_explicit(context: dict[str, Any] | None) -> bool:
    context = context or {}
    explicit = context.get("explicit_preferences") or []
    return bool(explicit)


def decision_sensitivity_receipt(
    windows: list[dict[str, Any]],
    *,
    traveler_twin_context: dict[str, Any] | None = None,
    current_mission_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact, machine-owned clarification decision.

    The only accepted options are the first two server-produced horizon
    windows.  A model may phrase the question, but it cannot create options or
    decide that a merely known route is feasible.
    """

    feasible = [
        item for item in windows
        if str(item.get("status") or "") == "GROUNDED_FEASIBLE"
    ]
    base: dict[str, Any] = {
        "decision_sensitive": False,
        "reason": "INSUFFICIENT_GROUNDED_FEASIBLE_OPTIONS",
        "options": [],
        "counterfactuals": [],
        "resource_conflict": False,
    }
    if len(feasible) < 2:
        return base
    first, later = feasible[0], feasible[1]
    budget = int(first.get("optional_budget_remaining_minor") or 0)
    first_cost = first.get("candidate", {}).get("cost_evidence", {}).get("amount_minor")
    later_cost = later.get("candidate", {}).get("cost_evidence", {}).get("amount_minor")
    if not isinstance(first_cost, int) or not isinstance(later_cost, int):
        return base
    conflict = first_cost + later_cost > budget
    if not conflict:
        return {
            **base,
            "reason": "ANSWERS_DO_NOT_CHANGE_ACTION_RESOURCE_FEASIBLE",
            "options": ["TAKE_NOW", "TAKE_LATER"],
        }
    if _has_current_explicit(traveler_twin_context) or current_mission_preferences:
        return {
            **base,
            "reason": "CURRENT_EXPLICIT_INTENT_ALREADY_RESOLVES_VALUE",
            "options": ["TAKE_NOW", "TAKE_LATER"],
            "resource_conflict": True,
        }
    option_a = {
        "key": "TAKE_NOW",
        "window_id": first.get("window_id"),
        "action": "TAKE_NOW",
        "place_id": first.get("candidate", {}).get("place_id"),
    }
    option_b = {
        "key": "TAKE_LATER",
        "window_id": later.get("window_id"),
        "action": "DEFER",
        "place_id": later.get("candidate", {}).get("place_id"),
    }
    return {
        "decision_sensitive": True,
        "reason": "DECISION_SENSITIVE_HUMAN_VALUE",
        "preference_dimension": "relative_value_of_current_vs_later_experience",
        "options": [option_a, option_b],
        "counterfactuals": [
            {"explicit_value": "TAKE_NOW", "top_action": "TAKE_NOW", "chosen_window_id": first.get("window_id")},
            {"explicit_value": "TAKE_LATER", "top_action": "DEFER", "chosen_window_id": later.get("window_id")},
        ],
        "resource_conflict": True,
        "resource": {
            "optional_budget_remaining_minor": budget,
            "take_now_cost_minor": first_cost,
            "take_later_cost_minor": later_cost,
            "combined_cost_exceeds_budget": True,
        },
    }


def clarification_epoch_fingerprint(
    *,
    journey_id: str,
    active_plan_version_id: str | None,
    current_activity_id: str | None,
    windows: list[dict[str, Any]],
) -> str:
    material = {
        "journey_id": str(journey_id),
        "active_plan_version_id": active_plan_version_id,
        "current_activity_id": current_activity_id,
        "windows": [
            {
                "window_id": item.get("window_id"),
                "status": item.get("status"),
                "place_id": item.get("candidate", {}).get("place_id"),
                "category": item.get("candidate", {}).get("category"),
                "tags": item.get("candidate", {}).get("tags", []),
                "visit_minutes": item.get("candidate", {}).get("visit_minutes"),
                "opening": item.get("candidate", {}).get("opening", {}),
                "cost": item.get("candidate", {}).get("cost_evidence", {}).get("amount_minor"),
                "cost_status": item.get("candidate", {}).get("cost_evidence", {}).get("status"),
                "planning_mobility": item.get("planning_mobility", {}),
                "future_live_traffic": item.get("future_live_traffic"),
                "boundary": item.get("window_boundary"),
                "next_commitment": item.get("next_commitment"),
                "feasibility": item.get("feasibility", {}),
                "resource_consumption": item.get("resource_consumption", {}),
            }
            for item in windows[:2]
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
