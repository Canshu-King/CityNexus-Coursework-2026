from __future__ import annotations

import json
from typing import Any

from app.integrations.ai.base import AITextResult
from app.intelligence.reason_codes import VerifiedExplanationFacts


class LiteLLMExplanationProvider:
    """Optional provider for DeepSeek/other models via LiteLLM.

    It receives only verified structured facts. The returned text is never a
    feasibility result and never triggers a state mutation by itself.
    """

    def __init__(self, *, model: str, api_key: str | None = None) -> None:
        self.provider_key = "litellm"
        self._model = model
        self._api_key = api_key

    async def explain(self, facts: VerifiedExplanationFacts, *, task: str) -> AITextResult:
        try:
            from litellm import acompletion
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("litellm is not installed") from exc

        system = (
            "You are the CityNexus explanation layer. Use only the verified facts supplied. "
            "Do not invent opening hours, ETA, price, booking/payment status, commitment strength, "
            "or hard-constraint results. If a fact is absent, say it is unavailable."
        )
        payload = {"task": task, **facts.as_prompt_payload()}
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        response = await acompletion(**kwargs)
        text = response.choices[0].message.content or ""
        return AITextResult(provider_key=self.provider_key, text=str(text), degraded=False)
