from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.intelligence.reason_codes import VerifiedExplanationFacts


@dataclass(frozen=True)
class AITextResult:
    provider_key: str
    text: str
    degraded: bool = False


class AIProvider(Protocol):
    provider_key: str

    async def explain(self, facts: VerifiedExplanationFacts, *, task: str) -> AITextResult: ...
