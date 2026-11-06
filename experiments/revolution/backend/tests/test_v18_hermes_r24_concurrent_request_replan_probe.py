from scripts.hermes_r24_concurrent_request_replan_probe import SCENARIO_ID


def test_r24_scenario_identity_is_stable():
    assert SCENARIO_ID == "hermes-r24-concurrent-request-replan-idempotency"
