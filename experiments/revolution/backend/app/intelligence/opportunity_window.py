from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class OpportunityWindow:
    opens_at: datetime
    must_depart_by: datetime
    next_commitment_start: datetime
    travel_to_commitment_min: int
    safety_buffer_min: int

    @property
    def available_minutes(self) -> int:
        return max(0, int((self.must_depart_by - self.opens_at).total_seconds() // 60))


@dataclass(frozen=True)
class OpportunityCandidate:
    key: str
    visit_minutes: int
    inbound_travel_min: int
    outbound_travel_min: int
    cost_minor: int
    opens_at: datetime | None = None
    closes_at: datetime | None = None


@dataclass(frozen=True)
class OpportunityFit:
    candidate_key: str
    fits: bool
    finish_at: datetime | None
    remaining_buffer_min: int
    failures: tuple[str, ...]


def build_window(
    *,
    now: datetime,
    next_commitment_start: datetime,
    travel_to_commitment_min: int,
    safety_buffer_min: int,
) -> OpportunityWindow:
    if travel_to_commitment_min < 0 or safety_buffer_min < 0:
        raise ValueError("travel and safety buffer must be non-negative")
    if next_commitment_start < now:
        raise ValueError("next commitment is already in the past")
    must_depart = next_commitment_start - timedelta(
        minutes=travel_to_commitment_min + safety_buffer_min
    )
    return OpportunityWindow(
        opens_at=now,
        must_depart_by=must_depart,
        next_commitment_start=next_commitment_start,
        travel_to_commitment_min=travel_to_commitment_min,
        safety_buffer_min=safety_buffer_min,
    )


def evaluate_candidate(
    window: OpportunityWindow,
    candidate: OpportunityCandidate,
    *,
    budget_remaining_minor: int,
) -> OpportunityFit:
    failures: list[str] = []
    if (
        min(candidate.visit_minutes, candidate.inbound_travel_min, candidate.outbound_travel_min)
        < 0
    ):
        failures.append("INVALID_DURATION")
    if candidate.cost_minor < 0:
        failures.append("INVALID_COST")
    if candidate.cost_minor > budget_remaining_minor:
        failures.append("BUDGET_EXCEEDED")

    visit_start = window.opens_at + timedelta(minutes=max(0, candidate.inbound_travel_min))
    visit_end = visit_start + timedelta(minutes=max(0, candidate.visit_minutes))
    finish = visit_end + timedelta(minutes=max(0, candidate.outbound_travel_min))

    if candidate.opens_at is not None and visit_start < candidate.opens_at:
        failures.append("NOT_OPEN_YET")
    if candidate.closes_at is not None and visit_end > candidate.closes_at:
        failures.append("CLOSED_BEFORE_VISIT_END")
    if finish > window.must_depart_by:
        failures.append("NEXT_COMMITMENT_AT_RISK")

    remaining = int((window.must_depart_by - finish).total_seconds() // 60)
    return OpportunityFit(
        candidate_key=candidate.key,
        fits=not failures,
        finish_at=finish,
        remaining_buffer_min=max(0, remaining),
        failures=tuple(sorted(set(failures))),
    )


def feasible_candidates(
    window: OpportunityWindow,
    candidates: Iterable[OpportunityCandidate],
    *,
    budget_remaining_minor: int,
) -> list[OpportunityFit]:
    return [
        result
        for item in candidates
        if (
            result := evaluate_candidate(
                window, item, budget_remaining_minor=budget_remaining_minor
            )
        ).fits
    ]
