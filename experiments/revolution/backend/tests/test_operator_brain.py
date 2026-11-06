from types import SimpleNamespace

from app.agent.operator_brain import (
    RuntimeCandidate,
    configured_runtime_candidates,
    operator_brain_state,
    safe_external_handoff,
)


def test_unconfigured_operator_can_keep_watching():
    state = operator_brain_state(
        configured_candidates=[
            RuntimeCandidate("hermes", False, "Hermes"),
            RuntimeCandidate("deepseek", False, "DeepSeek"),
        ]
    )
    assert state["state"] == "UNCONFIGURED"
    assert state["recovery_resume"]["pending"] is False


def test_configured_runtime_is_ready_without_fake_network_claim():
    state = operator_brain_state(
        configured_candidates=[
            RuntimeCandidate("deepseek", True, "DeepSeek", "deepseek-v4"),
        ]
    )
    assert state["state"] == "READY"
    assert state["preferred_runtime"] == "deepseek"


def test_blocked_recovery_becomes_recovering_when_runtime_returns():
    state = operator_brain_state(
        configured_candidates=[
            RuntimeCandidate("deepseek", True, "DeepSeek"),
        ],
        blocked_recovery_pending=True,
        safe_to_resume=True,
    )
    assert state["state"] == "RECOVERING"
    assert state["recovery_resume"] == {"pending": True, "safe_to_resume": True}


def test_blocked_recovery_stays_unavailable_when_not_safe_to_resume():
    state = operator_brain_state(
        configured_candidates=[
            RuntimeCandidate("deepseek", True, "DeepSeek"),
        ],
        blocked_recovery_pending=True,
        safe_to_resume=False,
        last_failure_reason="MODEL_TIMEOUT",
    )
    assert state["state"] == "TEMPORARILY_UNAVAILABLE"


def test_cross_provider_handoff_only_before_tool_side_effects():
    assert safe_external_handoff(tool_step_count=0, approval_created=False) is True
    assert safe_external_handoff(tool_step_count=1, approval_created=False) is False
    assert safe_external_handoff(tool_step_count=0, approval_created=True) is False


def test_runtime_candidates_are_configuration_truth_not_provider_health():
    settings = SimpleNamespace(
        hermes_api_url="https://example.invalid/v1",
        hermes_api_key="secret",
        hermes_model="model-a",
        ai_api_key=None,
        ai_model="model-b",
        hermes_native_api_url=None,
        hermes_api_server_key=None,
        hermes_native_model=None,
    )
    rows = configured_runtime_candidates(settings)
    by_key = {row.key: row for row in rows}

    assert by_key["hermes"].configured is True
    assert by_key["deepseek"].configured is False
    assert by_key["hermes-native"].configured is False
