from app.reality_reconciliation import (
    RealityClaimView,
    dependent_action_allowed,
    reconcile_reservation_time_claims,
    reservation_time_claim_fingerprint,
)


def claim(cid, value, provenance, observed):
    return RealityClaimView(
        claim_id=cid,
        fact_domain="RESERVATION_TIME",
        value=value,
        provenance=provenance,
        observed_at=observed,
        exact_binding=True,
    )


def test_identical_claims_are_not_conflict():
    d = reconcile_reservation_time_claims([
        claim("a", "2026-10-09T17:40:00+00:00", "USER_PASTED_TEXT", "2026-10-07T10:00:00+00:00"),
        claim("b", "2026-10-09T17:40:00+00:00", "CAPTURED_REPLAY", "2026-10-06T10:00:00+00:00"),
    ])
    assert d.contested is False
    assert d.state == "NO_CONFLICT"


def test_newer_user_update_can_override_stale_replay():
    d = reconcile_reservation_time_claims([
        claim("user", "2026-10-09T18:10:00+00:00", "USER_PASTED_TEXT", "2026-10-07T10:00:00+00:00"),
        claim("replay", "2026-10-09T17:40:00+00:00", "CAPTURED_REPLAY", "2026-10-06T10:00:00+00:00"),
    ])
    assert d.state == "TEMPORARY_USER_OVERRIDE"
    assert d.winning_claim_id == "user"


def test_fresh_live_provider_can_win_over_user_message():
    d = reconcile_reservation_time_claims([
        claim("live", "2026-10-09T17:40:00+00:00", "LIVE_PROVIDER", "2026-10-07T11:00:00+00:00"),
        claim("user", "2026-10-09T18:10:00+00:00", "USER_PASTED_TEXT", "2026-10-07T10:00:00+00:00"),
    ])
    assert d.state == "NEWER_HIGHER_AUTHORITY_WINS"
    assert d.winning_claim_id == "live"


def test_stale_live_provider_does_not_beat_newer_user_claim_solely_by_source_label():
    d = reconcile_reservation_time_claims([
        claim("live-yesterday", "2026-10-09T17:40:00+00:00", "LIVE_PROVIDER", "2026-10-06T10:00:00+00:00"),
        claim("user-today", "2026-10-09T18:10:00+00:00", "USER_PASTED_TEXT", "2026-10-07T10:00:00+00:00"),
    ])
    assert d.state == "UNRESOLVED_MATERIAL_CONFLICT"
    assert d.contested is True


def test_estimated_agent_never_becomes_authoritative_winner():
    d = reconcile_reservation_time_claims([
        claim("estimate", "2026-10-09T18:10:00+00:00", "ESTIMATED_AGENT", "2026-10-07T11:00:00+00:00"),
    ])
    assert d.winning_claim_id is None
    assert d.reason == "NO_AUTHORITATIVE_CLAIM"


def test_material_current_claims_can_remain_contested():
    d = reconcile_reservation_time_claims([
        claim("user", "2026-10-09T18:10:00+00:00", "USER_CONFIRMED_REALITY_RESOLUTION", "2026-10-07T10:30:00+00:00"),
        claim("provider-derived", "2026-10-09T17:40:00+00:00", "SYSTEM_DERIVED_PROVIDER_FACT", "2026-10-07T10:31:00+00:00"),
    ])
    assert d.state == "UNRESOLVED_MATERIAL_CONFLICT"
    assert d.contested is True
    assert d.winning_claim_id is None


def test_nonmaterial_difference_does_not_freeze_journey():
    d = reconcile_reservation_time_claims([
        claim("user", "2026-10-09T17:45:00+00:00", "USER_CONFIRMED_REALITY_RESOLUTION", "2026-10-07T10:30:00+00:00"),
        claim("provider-derived", "2026-10-09T17:40:00+00:00", "SYSTEM_DERIVED_PROVIDER_FACT", "2026-10-07T10:31:00+00:00"),
    ])
    assert d.state == "NONMATERIAL_CONFLICT"
    assert d.contested is False


def test_contested_fact_blocks_only_dependent_actions():
    assert dependent_action_allowed(contested=True, depends_on_fact=True) is False
    assert dependent_action_allowed(contested=True, depends_on_fact=False) is True
    assert dependent_action_allowed(contested=False, depends_on_fact=True) is True


def test_claim_replay_fingerprint_is_stable_for_same_bound_source_claim():
    first = reservation_time_claim_fingerprint(
        journey_id="j1", reservation_id="r1", value="2026-10-09T17:40:00+00:00",
        provenance="CAPTURED_REPLAY", source_ref="sealed-capture:cap-1",
    )
    second = reservation_time_claim_fingerprint(
        journey_id="j1", reservation_id="r1", value="2026-10-09T17:40:00+00:00",
        provenance="CAPTURED_REPLAY", source_ref="sealed-capture:cap-1",
    )
    changed_value = reservation_time_claim_fingerprint(
        journey_id="j1", reservation_id="r1", value="2026-10-09T18:10:00+00:00",
        provenance="CAPTURED_REPLAY", source_ref="sealed-capture:cap-1",
    )
    assert first == second
    assert first != changed_value
