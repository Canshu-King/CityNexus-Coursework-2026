from datetime import datetime, timedelta, timezone

from app.agent.journey_ahead import actionable_future_risks, journey_ahead_state


NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)


def _risk(*, minutes=180, score=70, grounded=True, activity_id="a3"):
    return {
        "activity_id": activity_id,
        "title": "Future stop",
        "start_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "risk_score": score,
        "grounded": grounded,
        "risk_kind": "FORECAST_WEATHER_EXPOSURE",
    }


def test_no_future_risk_keeps_operator_clear():
    result = journey_ahead_state(activity_risks=[], now=NOW)
    assert result["state"] == "CLEAR"
    assert result["top_risk"] is None


def test_ungrounded_future_risk_is_watched_not_acted_on():
    result = journey_ahead_state(
        activity_risks=[_risk(grounded=False)],
        now=NOW,
    )
    assert result["state"] == "WATCHING_FUTURE"
    assert result["recommended_action"] == "KEEP_WATCHING"


def test_grounded_actionable_future_risk_is_found():
    result = journey_ahead_state(
        activity_risks=[_risk(minutes=180, score=75, grounded=True)],
        now=NOW,
    )
    assert result["state"] == "FUTURE_RISK_FOUND"
    assert result["recommended_action"] == "PREPARE_RECOVERY"


def test_far_future_risk_does_not_start_prevention_too_early():
    result = journey_ahead_state(
        activity_risks=[_risk(minutes=420, score=80, grounded=True)],
        now=NOW,
    )
    assert result["state"] == "WATCHING_FUTURE"


def test_near_term_future_risk_hands_back_to_normal_guardian():
    result = journey_ahead_state(
        activity_risks=[_risk(minutes=20, score=80, grounded=True)],
        now=NOW,
    )
    assert result["state"] == "FUTURE_RISK_FOUND"
    assert result["recommended_action"] == "HAND_OFF_TO_NEAR_TERM_GUARDIAN"


def test_prevention_planning_is_visible_without_claiming_success():
    result = journey_ahead_state(
        activity_risks=[_risk()],
        now=NOW,
        prevention_status="PLANNING",
    )
    assert result["state"] == "PREVENTION_PLANNING"


def test_preventive_approval_is_product_state():
    result = journey_ahead_state(
        activity_risks=[_risk()],
        now=NOW,
        prevention_status="WAITING_FOR_APPROVAL",
    )
    assert result["state"] == "WAITING_FOR_APPROVAL"
    assert result["recommended_action"] == "REVIEW_PREVENTION"


def test_completed_prevention_is_not_same_as_current_disruption():
    result = journey_ahead_state(
        activity_risks=[_risk()],
        now=NOW,
        prevention_status="APPLIED",
    )
    assert result["state"] == "PREVENTION_APPLIED"


def test_brain_outage_preserves_future_risk_visibility():
    result = journey_ahead_state(
        activity_risks=[_risk()],
        now=NOW,
        brain_blocked=True,
    )
    assert result["state"] == "FUTURE_RISK_UNCERTAIN"


def test_completed_journey_stops_ahead_work():
    result = journey_ahead_state(
        activity_risks=[_risk()],
        now=NOW,
        journey_completed=True,
    )
    assert result["state"] == "CLEAR"
    assert result["recommended_action"] == "NONE"


def test_actionability_orders_protected_booking_before_score_only_risk():
    risks = actionable_future_risks(
        [
            {
                **_risk(minutes=300, score=98, activity_id="far"),
                "protected_commitment": False,
                "booking_exposure": False,
            },
            {
                **_risk(minutes=180, score=65, activity_id="near"),
                "protected_commitment": True,
                "booking_exposure": True,
                "route_slack_minutes": 10,
            },
        ],
        now=NOW,
    )
    assert risks[0]["activity_id"] == "near"
    assert risks[0]["actionability_score"] > risks[1]["actionability_score"]
