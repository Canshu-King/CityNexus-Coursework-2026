from scripts.hermes_r25_concurrent_request_replan_conflict_probe import SCENARIO_ID


def test_r25_scenario_identity_is_stable():
    assert SCENARIO_ID == "hermes-r25-concurrent-request-replan-signature-conflict"
