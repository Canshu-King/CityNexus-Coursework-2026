from __future__ import annotations

from app.integrations.ai.base import AITextResult
from app.intelligence.reason_codes import VerifiedExplanationFacts


class DeterministicFallbackAIProvider:
    provider_key = "deterministic-fallback"

    async def explain(self, facts: VerifiedExplanationFacts, *, task: str) -> AITextResult:
        codes = ", ".join(code.value for code in facts.reason_codes) or "NO_REASON_CODES"
        evidence = ", ".join(f"{key}={value}" for key, value in sorted(facts.evidence.items()))
        if facts.locale.lower().startswith("zh"):
            text = (
                f"已根据已验证事实生成说明。原因：{codes}。证据：{evidence or '无额外数值证据'}。"
            )
        else:
            text = f"Explanation based on verified facts. Reasons: {codes}. Evidence: {evidence or 'none supplied'}."
        return AITextResult(provider_key=self.provider_key, text=text, degraded=True)
