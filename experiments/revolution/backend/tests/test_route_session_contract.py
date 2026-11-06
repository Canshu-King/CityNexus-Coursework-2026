from __future__ import annotations

from app.execution.route_session_contract import (
    COMPLETED,
    FAILED,
    OUTCOME_PROVIDER_FAILURE,
    OUTCOME_ROUTE_READY,
    OUTCOME_ROUTE_UNAVAILABLE,
    ROUTE_PROVIDER_POLICY_VERSION,
    ROUTE_TRUTH_POLICY_VERSION,
    UNAVAILABLE,
    RouteRequest,
    classify_capture,
    fact_is_eligible_for_live_readiness,
    route_request_fingerprint,
)


def _request(**overrides):
    values = {
        "journey_id": "j1",
        "origin_key": "origin-a",
        "target_key": "target-b",
        "target_activity_id": "a1",
        "requested_mode": "WALK",
        "provider_policy_version": ROUTE_PROVIDER_POLICY_VERSION,
        "route_truth_policy_version": ROUTE_TRUTH_POLICY_VERSION,
    }
    values.update(overrides)
    return RouteRequest(**values)


def test_same_semantic_request_has_same_fingerprint():
    first = route_request_fingerprint(_request())
    second = route_request_fingerprint(_request(requested_mode="walk"))
    assert first == second


def test_origin_target_and_policy_change_fingerprint():
    baseline = route_request_fingerprint(_request())
    assert route_request_fingerprint(_request(origin_key="origin-c")) != baseline
    assert route_request_fingerprint(_request(target_key="target-c")) != baseline
    assert route_request_fingerprint(_request(target_activity_id="a2")) != baseline
    assert (
        route_request_fingerprint(_request(provider_policy_version="different-policy"))
        != baseline
    )


def test_provider_unavailable_is_not_runtime_failure():
    status, outcome = classify_capture(
        explicit_fact=None,
        provider_failed=False,
    )
    assert status == UNAVAILABLE
    assert outcome == OUTCOME_ROUTE_UNAVAILABLE


def test_provider_exception_is_failed_not_unavailable():
    status, outcome = classify_capture(
        explicit_fact=None,
        provider_failed=True,
    )
    assert status == FAILED
    assert outcome == OUTCOME_PROVIDER_FAILURE


def test_explicit_live_fact_is_route_ready():
    status, outcome = classify_capture(
        explicit_fact={"duration_min": 18},
        provider_failed=False,
    )
    assert status == COMPLETED
    assert outcome == OUTCOME_ROUTE_READY


def test_synthetic_fact_cannot_drive_live_leave_by():
    assert not fact_is_eligible_for_live_readiness(
        {
            "duration_min": 18,
            "provider_key": "mock-mobility",
            "provenance": "SYNTHETIC_FIXTURE",
        }
    )
    assert not fact_is_eligible_for_live_readiness(
        {
            "duration_min": 18,
            "provider_key": "valhalla-http",
            "provenance": "CITYNEXUS_ESTIMATE",
        }
    )


def test_explicit_provider_fact_can_drive_live_leave_by():
    assert fact_is_eligible_for_live_readiness(
        {
            "duration_min": 18,
            "provider_key": "valhalla-http",
            "provenance": "LIVE_PROVIDER_CAPTURE",
        }
    )
