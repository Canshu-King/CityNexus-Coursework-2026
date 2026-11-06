from scripts.hermes_r16_protected_target_reliability_probe import _is_protected_target_blocker


def test_r16_preserves_the_actual_fail_closed_reason_codes():
    assert _is_protected_target_blocker(
        "no feasible replan proposal: LOCKED_COMMITMENT_LOST:place-old-town, PLACE_CLOSED:place-design-library"
    ) is True


def test_r16_does_not_substitute_a_different_product_message():
    assert _is_protected_target_blocker(
        "closed activity is protected and cannot be replaced automatically"
    ) is False
    assert _is_protected_target_blocker(
        "no feasible replan proposal: LOCKED_COMMITMENT_LOST:place-old-town"
    ) is False
