from app.planning.adapter import DeterministicVariantSelector, PlannerVariant


def test_deterministic_variant_selector_prefers_utility_then_low_disruption_then_key():
    items = [
        PlannerVariant("b", 1000, 2),
        PlannerVariant("a", 1000, 1),
        PlannerVariant("x", 9999, 0, feasible=False),
    ]
    assert DeterministicVariantSelector().choose(items).key == "a"
