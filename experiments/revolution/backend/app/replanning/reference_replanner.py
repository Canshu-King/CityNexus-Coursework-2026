"""Pure-Python reference invariants for CityNexus replanning.

This is deliberately framework-free. Codex should port the same invariants into
backend/app/replanning rather than import FastAPI/SQLAlchemy into this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

WEIGHTS: Mapping[str, float] = {
    "commitment_preservation": 0.24,
    "stability": 0.18,
    "time_reliability": 0.15,
    "preference_fit": 0.12,
    "mobility_fit": 0.10,
    "budget_fit": 0.08,
    "robustness": 0.07,
    "opportunity_value": 0.06,
}


@dataclass(frozen=True)
class Activity:
    key: str
    start_min: int
    end_min: int
    cost_minor: int = 0
    available: bool = True


@dataclass(frozen=True)
class Commitment:
    activity_key: str
    level: str
    start_min: int
    time_boundary: str = "START"


@dataclass(frozen=True)
class Candidate:
    fingerprint: str
    activities: tuple[Activity, ...]
    metrics: Mapping[str, float]
    commitment_changes: int = 0
    activity_changes: int = 0
    schedule_shift_min: int = 0
    travel_minutes: int = 0


@dataclass(frozen=True)
class GateResult:
    passed: bool
    failures: tuple[str, ...]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_metrics(metrics: Mapping[str, float]) -> float:
    missing = set(WEIGHTS) - set(metrics)
    extra = set(metrics) - set(WEIGHTS)
    if missing or extra:
        raise ValueError(f"metric mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    return sum(WEIGHTS[name] * _clamp01(float(metrics[name])) for name in WEIGHTS)


def validate_candidate(
    candidate: Candidate,
    *,
    journey_start_min: int,
    journey_end_min: int,
    budget_max_minor: int,
    locked_commitments: Iterable[Commitment],
) -> GateResult:
    failures: list[str] = []
    activities = sorted(candidate.activities, key=lambda a: (a.start_min, a.end_min, a.key))

    if any(a.start_min < journey_start_min or a.end_min > journey_end_min for a in activities):
        failures.append("JOURNEY_WINDOW_EXCEEDED")
    if any(a.end_min <= a.start_min for a in activities):
        failures.append("INVALID_ACTIVITY_WINDOW")
    if any(not a.available for a in activities):
        failures.append("ACTIVITY_UNAVAILABLE")
    if any(curr.end_min > nxt.start_min for curr, nxt in zip(activities, activities[1:])):
        failures.append("ACTIVITY_OVERLAP")
    if sum(a.cost_minor for a in activities) > budget_max_minor:
        failures.append("BUDGET_EXCEEDED")

    for commitment in locked_commitments:
        if commitment.level != "LOCKED":
            continue
        # A semantic location may occur more than once in a candidate.  Do not
        # collapse activities into a key->activity mapping: each lock is
        # satisfied only by an activity with both the same stable identity and
        # the same relative scheduled start.
        boundary = str(commitment.time_boundary or "START").upper()
        matching = any(
            activity.key == commitment.activity_key
            and (activity.end_min if boundary == "END" else activity.start_min)
            == commitment.start_min
            for activity in activities
        )
        if not matching:
            failures.append(f"LOCKED_COMMITMENT_LOST:{commitment.activity_key}")

    return GateResult(passed=not failures, failures=tuple(sorted(set(failures))))


def choose_best(candidates: Sequence[Candidate]) -> Candidate:
    if not candidates:
        raise ValueError("no candidates")

    def rank(c: Candidate):
        return (
            -score_metrics(c.metrics),
            c.commitment_changes,
            c.activity_changes,
            c.schedule_shift_min,
            sum(a.cost_minor for a in c.activities),
            c.travel_minutes,
            c.fingerprint,
        )

    return min(candidates, key=rank)


def weights_sum() -> float:
    return sum(WEIGHTS.values())
