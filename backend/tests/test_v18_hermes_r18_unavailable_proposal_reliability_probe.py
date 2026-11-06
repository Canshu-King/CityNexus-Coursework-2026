from scripts.hermes_r18_unavailable_proposal_reliability_probe import ARMS


def test_r18_both_arms_use_unavailable_synthetic_state():
    assert {spec["provider_state"] for spec in ARMS.values()} == {"UNAVAILABLE"}
    assert all(spec["provider_state"] == "UNAVAILABLE" for spec in ARMS.values())


def test_r18_read_only_arm_is_not_allowed_to_request_a_proposal():
    assert ARMS["provider-unavailable-proposal"]["expect_request_replan"] is True
    assert ARMS["provider-unavailable-read-only"]["expect_request_replan"] is False
