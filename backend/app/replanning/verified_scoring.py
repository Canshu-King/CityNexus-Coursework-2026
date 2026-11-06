from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.replanning.reference_replanner import WEIGHTS, score_metrics


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class VerifiedReplanScore:
    total: float
    breakdown: Mapping[str, float]


def score_replan_candidate(
    *,
    base_activity_count: int,
    moved_count: int,
    removed_count: int,
    added_count: int,
    travel_delta_min: int,
    budget_amount_minor: int,
    after_envelope: Mapping[str, Any],
    commitments_preserved: bool,
    preference_fit: float | None = None,
    opportunity_value: float | None = None,
) -> VerifiedReplanScore:
    """Score only persisted, gate-verified plan facts.

    Preference fit and opportunity value do not yet have candidate-specific
    evidence in WP06, so both use a documented neutral value instead of copying
    the base score or inventing AI-derived evidence.
    """

    changed = moved_count + removed_count + added_count
    stability = 1.0 - min(1.0, changed / max(1, base_activity_count))
    slack = int(after_envelope.get("journey_end_slack_min", 0))
    time_reliability = _clamp(slack / 60.0) if slack >= 0 else 0.0
    if budget_amount_minor > 0:
        budget_fit = _clamp(
            int(after_envelope.get("budget_remaining_minor", 0)) / budget_amount_minor
        )
    else:
        budget_fit = 1.0 if int(after_envelope.get("budget_remaining_minor", 0)) >= 0 else 0.0
    mobility_fit = 1.0 if travel_delta_min <= 0 else _clamp(1.0 - travel_delta_min / 60.0)
    robustness = 1.0 if bool(after_envelope.get("robust")) else 0.0
    metrics = {
        "commitment_preservation": 1.0 if commitments_preserved else 0.0,
        "stability": stability,
        "time_reliability": time_reliability,
        "preference_fit": 0.5 if preference_fit is None else _clamp(preference_fit),
        "mobility_fit": mobility_fit,
        "budget_fit": budget_fit,
        "robustness": robustness,
        "opportunity_value": (0.5 if opportunity_value is None else _clamp(opportunity_value)),
    }
    if set(metrics) != set(WEIGHTS):  # defensive contract synchronization
        raise ValueError("verified score metrics do not match the frozen utility weights")
    rounded = {name: round(value, 6) for name, value in metrics.items()}
    total = round(score_metrics(rounded), 6)
    return VerifiedReplanScore(total=total, breakdown={**rounded, "total": total})
