from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.intelligence.reason_codes import ReasonCode

#: Which severities of an unresolved event make a trip's summary risky.  An INFO or MINOR note is
#: deliberately absent: marking it as a risk would read "review" over a trip that is fine.
UNRESOLVED_DISRUPTION_SEVERITIES: dict[str, ReasonCode] = {
    "MAJOR": ReasonCode.DISRUPTION_MAJOR,
    "CRITICAL": ReasonCode.DISRUPTION_CRITICAL,
}


def disruption_risk_codes(severities: Iterable[str]) -> tuple[str, ...]:
    """Unresolved event severities -> risk reason codes, worst first."""
    named = {item for item in severities if item in UNRESOLVED_DISRUPTION_SEVERITIES}
    ordered = sorted(named, key=("CRITICAL", "MAJOR").index)
    return tuple(UNRESOLVED_DISRUPTION_SEVERITIES[item].value for item in ordered)


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
