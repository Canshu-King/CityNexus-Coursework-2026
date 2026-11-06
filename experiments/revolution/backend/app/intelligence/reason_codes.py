from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ReasonCode(StrEnum):
    INTEREST_MATCH = "INTEREST_MATCH"
    WITHIN_OPPORTUNITY_WINDOW = "WITHIN_OPPORTUNITY_WINDOW"
    OPEN_AT_VISIT_TIME = "OPEN_AT_VISIT_TIME"
    LOW_DETOUR = "LOW_DETOUR"
    BUDGET_FIT = "BUDGET_FIT"
    NEXT_COMMITMENT_SAFE = "NEXT_COMMITMENT_SAFE"
    COMMITMENT_PRESERVED = "COMMITMENT_PRESERVED"
    WEATHER_RISK_REDUCED = "WEATHER_RISK_REDUCED"
    TRAVEL_TIME_REDUCED = "TRAVEL_TIME_REDUCED"
    MINIMAL_DISRUPTION = "MINIMAL_DISRUPTION"
    PROVIDER_DEGRADED = "PROVIDER_DEGRADED"


@dataclass(frozen=True)
class VerifiedExplanationFacts:
    reason_codes: tuple[ReasonCode, ...]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    locale: str = "en"

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "reason_codes": [code.value for code in self.reason_codes],
            "evidence": dict(self.evidence),
            "locale": self.locale,
        }
