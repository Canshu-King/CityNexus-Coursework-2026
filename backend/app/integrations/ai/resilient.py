from __future__ import annotations

from app.integrations.ai.base import AIProvider, AITextResult
from app.integrations.ai.fallback import DeterministicFallbackAIProvider
from app.intelligence.reason_codes import VerifiedExplanationFacts


class ResilientAIExplanationService:
    """Use an optional real LLM without making it authoritative or mandatory."""

    def __init__(
        self,
        *,
        primary: AIProvider | None,
        fallback: AIProvider | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or DeterministicFallbackAIProvider()

    async def explain(self, facts: VerifiedExplanationFacts, *, task: str) -> AITextResult:
        if self._primary is not None:
            try:
                result = await self._primary.explain(facts, task=task)
                if result.text.strip():
                    return result
            except Exception:  # noqa: BLE001,S110 - external LLM boundary
                pass
        return await self._fallback.explain(facts, task=task)
