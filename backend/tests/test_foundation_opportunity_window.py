from datetime import datetime, timezone

from app.intelligence.opportunity_window import (
    OpportunityCandidate,
    build_window,
    evaluate_candidate,
)


def dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 10, hour, minute, tzinfo=timezone.utc)


def test_opportunity_window_fits_safe_candidate():
    window = build_window(
        now=dt(14, 0),
        next_commitment_start=dt(16, 0),
        travel_to_commitment_min=25,
        safety_buffer_min=20,
    )
    assert window.available_minutes == 75
    candidate = OpportunityCandidate(
        key="museum-mini",
        visit_minutes=40,
        inbound_travel_min=5,
        outbound_travel_min=5,
        cost_minor=0,
    )
    result = evaluate_candidate(window, candidate, budget_remaining_minor=5000)
    assert result.fits
    assert result.remaining_buffer_min == 25


def test_opportunity_window_rejects_commitment_risk():
    window = build_window(
        now=dt(14, 0),
        next_commitment_start=dt(16, 0),
        travel_to_commitment_min=25,
        safety_buffer_min=20,
    )
    candidate = OpportunityCandidate(
        key="too-long",
        visit_minutes=70,
        inbound_travel_min=10,
        outbound_travel_min=10,
        cost_minor=0,
    )
    result = evaluate_candidate(window, candidate, budget_remaining_minor=5000)
    assert not result.fits
    assert "NEXT_COMMITMENT_AT_RISK" in result.failures
