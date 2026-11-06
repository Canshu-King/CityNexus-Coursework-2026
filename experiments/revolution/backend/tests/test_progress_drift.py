import json
from types import SimpleNamespace

from app.execution.progress_drift import (
    _execution_milestone,
    classify_progress_risk,
    drift_from_milestone,
    execution_drift_fingerprint,
)


def test_confirmed_execution_creates_grounded_positive_drift():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:28:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    assert drift.authoritative is True
    assert drift.drift_min == 28


def test_simulated_execution_cannot_prove_live_progress_drift():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:28:00+00:00",
        truth_kind="SIMULATED_STATE_TRANSITION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=20,
    )
    assert decision.state == "UNKNOWN_PROGRESS"
    assert decision.material is False


def test_simulated_milestone_cannot_supply_a_system_drift_event_source():
    activity = SimpleNamespace(
        metadata_json=json.dumps({
            "execution_truth_history": [{
                "kind": "SIMULATED_STATE_TRANSITION",
                "transition": "COMPLETED",
                "captured_at": "2026-10-07T05:28:00+00:00",
            }],
            "completed_at_actual": "2026-10-07T05:28:00+00:00",
        }),
        start_at="2026-10-07T05:00:00+00:00",
        end_at="2026-10-07T05:20:00+00:00",
    )
    assert _execution_milestone(activity) is None


def test_late_but_absorbable_progress_stays_quiet():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:12:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=False,
        slack_before_min=40,
    )
    assert decision.state == "MINOR_DRIFT"
    assert decision.material is False
    assert decision.remaining_slack_min == 28


def test_small_delay_can_endanger_tight_protected_commitment():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:10:00+00:00",
        truth_kind="DEVICE_LOCATION_VERIFIED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=12,
        safety_buffer_min=5,
    )
    assert decision.state == "COMMITMENT_AT_RISK"
    assert decision.material is True
    assert decision.remaining_slack_min == 2


def test_large_delay_with_large_buffer_need_not_be_material():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:40:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=95,
    )
    assert decision.state == "MINOR_DRIFT"
    assert decision.material is False


def test_unknown_route_prevents_fake_downstream_precision():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:25:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=None,
        route_grounded=False,
    )
    assert decision.state == "ROUTE_UNKNOWN"
    assert decision.remaining_slack_min is None


def test_contested_commitment_time_prevents_precise_risk():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T05:25:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=15,
        commitment_contested=True,
    )
    assert decision.state == "REALITY_CONTESTED"
    assert decision.material is False


def test_early_execution_is_not_a_recovery_incident():
    drift = drift_from_milestone(
        planned_at="2026-10-07T05:00:00+00:00",
        actual_at="2026-10-07T04:50:00+00:00",
        truth_kind="OPERATOR_CONFIRMED_EXECUTION",
    )
    decision = classify_progress_risk(
        drift=drift,
        protected_commitment=True,
        slack_before_min=20,
    )
    assert decision.state == "AHEAD"
    assert decision.material is False


def test_drift_event_identity_is_stable_for_same_real_milestone():
    first = execution_drift_fingerprint(
        journey_id="j1",
        activity_id="a1",
        milestone="COMPLETED",
        actual_at="2026-10-07T05:28:00+00:00",
        affected_target_id="commitment-1",
    )
    second = execution_drift_fingerprint(
        journey_id="j1",
        activity_id="a1",
        milestone="completed",
        actual_at="2026-10-07T05:28:00+00:00",
        affected_target_id="commitment-1",
    )
    assert first == second
