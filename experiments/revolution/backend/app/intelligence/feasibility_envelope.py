from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeasibilityEnvelope:
    next_commitment_buffer_min: int | None
    journey_end_slack_min: int
    budget_remaining_minor: int
    preserved_commitment_count: int
    degraded_providers: tuple[str, ...] = ()
    risk_reason_codes: tuple[str, ...] = ()

    @property
    def robust(self) -> bool:
        commitment_ok = (
            self.next_commitment_buffer_min is None or self.next_commitment_buffer_min >= 0
        )
        return (
            commitment_ok
            and self.journey_end_slack_min >= 0
            and self.budget_remaining_minor >= 0
            and not self.risk_reason_codes
        )
