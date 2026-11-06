from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.intelligence.opportunity_window import (
    OpportunityCandidate,
    OpportunityFit,
    build_window,
    evaluate_candidate,
)


@dataclass(frozen=True)
class WindowReplacementInput:
    key: str
    visit_minutes: int
    inbound_travel_min: int
    outbound_travel_min: int
    cost_minor: int
    opens_at: datetime | None = None
    closes_at: datetime | None = None


@dataclass(frozen=True)
class WindowReplacementEvidence:
    candidate_key: str
    fits: bool
    window_opens_at: datetime
    next_commitment_start: datetime
    finish_at: datetime | None
    remaining_buffer_min: int
    failures: tuple[str, ...]

    @property
    def as_dict(self) -> dict[str, object]:
        return {
            "window_opens_at": self.window_opens_at.isoformat(),
            "next_commitment_start": self.next_commitment_start.isoformat(),
            "finish_at": self.finish_at.isoformat() if self.finish_at else None,
            "remaining_buffer_min": self.remaining_buffer_min,
            "fits": self.fits,
            "failures": list(self.failures),
        }


def evaluate_window_replacement(
    *,
    now: datetime,
    next_commitment_start: datetime,
    safety_buffer_min: int,
    budget_remaining_minor: int,
    candidate: WindowReplacementInput,
) -> WindowReplacementEvidence:
    """Evaluate a real F3 Opportunity Window candidate from verified facts.

    ``travel_to_commitment_min`` is zero on the window because outbound travel
    belongs to the candidate itself. This avoids subtracting the same travel leg
    twice. The caller remains responsible for turning a passing candidate into a
    PlanVersion and re-running the authoritative full-plan feasibility gate.
    """

    window = build_window(
        now=now,
        next_commitment_start=next_commitment_start,
        travel_to_commitment_min=0,
        safety_buffer_min=safety_buffer_min,
    )
    fit: OpportunityFit = evaluate_candidate(
        window,
        OpportunityCandidate(
            key=candidate.key,
            visit_minutes=candidate.visit_minutes,
            inbound_travel_min=candidate.inbound_travel_min,
            outbound_travel_min=candidate.outbound_travel_min,
            cost_minor=candidate.cost_minor,
            opens_at=candidate.opens_at,
            closes_at=candidate.closes_at,
        ),
        budget_remaining_minor=budget_remaining_minor,
    )
    return WindowReplacementEvidence(
        candidate_key=candidate.key,
        fits=fit.fits,
        window_opens_at=window.opens_at,
        next_commitment_start=window.next_commitment_start,
        finish_at=fit.finish_at,
        remaining_buffer_min=fit.remaining_buffer_min,
        failures=fit.failures,
    )
