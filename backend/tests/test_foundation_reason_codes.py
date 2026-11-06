import pytest

from app.integrations.ai.fallback import DeterministicFallbackAIProvider
from app.intelligence.reason_codes import ReasonCode, VerifiedExplanationFacts


def test_verified_payload_contains_only_supplied_evidence():
    facts = VerifiedExplanationFacts(
        reason_codes=(ReasonCode.COMMITMENT_PRESERVED, ReasonCode.MINIMAL_DISRUPTION),
        evidence={"changed_activities": 2, "cost_delta_minor": 800},
    )
    assert facts.as_prompt_payload()["evidence"] == {
        "changed_activities": 2,
        "cost_delta_minor": 800,
    }


@pytest.mark.asyncio
async def test_fallback_ai_is_explicitly_degraded():
    provider = DeterministicFallbackAIProvider()
    result = await provider.explain(
        VerifiedExplanationFacts(
            reason_codes=(ReasonCode.NEXT_COMMITMENT_SAFE,),
            evidence={"buffer_min": 22},
        ),
        task="explain recommendation",
    )
    assert result.degraded is True
    assert "22" in result.text
