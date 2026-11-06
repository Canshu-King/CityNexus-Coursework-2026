from scripts.hermes_r22_response_loss_recovery_probe import R23_SCENARIO_ID


def test_r23_has_a_distinct_unguided_scenario_identity():
    assert R23_SCENARIO_ID == "hermes-r23-unguided-response-loss-recovery"
