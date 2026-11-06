from __future__ import annotations

import httpx

from app.integrations.ai.responses import DeepSeekResponsesClient, DeepSeekResponsesError
from app.integrations.discovery.real_city import (
    _captured_replay_candidates,
    _promote_captured_evidence,
)


def test_captured_real_snapshot_promotes_only_sealed_official_facts() -> None:
    candidates = _captured_replay_candidates("museum indoor culture", limit=8)

    enriched = [item for item in candidates if item.opening_hours.get("status") == "OFFICIAL_WEB_VERIFIED"]
    assert enriched
    assert enriched[0].cost_evidence["status"] == "VERIFIED_OFFICIAL_WEB"
    assert enriched[0].metadata["evidence_completion"]["observations"]


def test_unsourced_model_claim_stays_unknown() -> None:
    hours, cost, promotion = _promote_captured_evidence(
        "node/model-only",
        {
            "candidate_external_id": "node/model-only",
            "facts": {
                "opening_hours": {
                    "status": "NORMALIZED_VALID",
                    "open": "09:00",
                    "close": "18:00",
                    "source_observation_ids": ["missing-source"],
                },
                "cost_evidence": {
                    "status": "KNOWN",
                    "amount_minor": 0,
                    "currency": "CNY",
                    "source_observation_ids": ["missing-source"],
                },
            },
        },
        opening_hours={"status": "UNKNOWN", "intervals": []},
        cost_evidence={"status": "UNKNOWN", "currency": "CNY"},
    )

    assert hours["status"] == "UNKNOWN"
    assert cost["status"] == "UNKNOWN"
    assert promotion is None


def test_deepseek_http_diagnostics_are_safe_and_specific() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"code": "insufficient_balance"}})

    client = DeepSeekResponsesClient(
        api_key="secret-key-not-for-output",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        client.complete([{"role": "user", "content": "hello"}])
    except DeepSeekResponsesError as exc:
        assert exc.status_code == 402
        assert exc.provider_code == "insufficient_balance"
        assert "secret-key-not-for-output" not in str(exc)
        assert "status=402" in str(exc)
    else:  # pragma: no cover - the mocked provider always fails
        raise AssertionError("expected the provider diagnostic")
