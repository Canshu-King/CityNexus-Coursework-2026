from app.intelligence.decision_sensitivity import (
    clarification_epoch_fingerprint,
    decision_sensitivity_receipt,
)
from app.agent.runtime import _server_completion_contract


def _window(key: str, place_id: str, status: str, cost: int, fit: float = 0.5):
    return {
        "window_id": key,
        "status": status,
        "window_boundary": {"opens_at": "2026-10-04T10:00:00+00:00", "closes_at": "2026-10-04T12:00:00+00:00"},
        "candidate": {
            "place_id": place_id,
            "category": "garden" if key == "w1" else "museum",
            "cost_evidence": {"status": "KNOWN_PAID", "amount_minor": cost},
            "preference_fit": fit,
        },
    }


def test_r26_only_two_feasible_conflicting_actions_are_decision_sensitive():
    receipt = decision_sensitivity_receipt(
        [_window("w1", "place-a", "GROUNDED_FEASIBLE", 700), _window("w2", "place-b", "GROUNDED_FEASIBLE", 800)],
        traveler_twin_context={"explicit_preferences": [], "learned_soft_preferences": []},
    )
    assert receipt["decision_sensitive"] is True
    assert receipt["reason"] == "DECISION_SENSITIVE_HUMAN_VALUE"
    assert [item["top_action"] for item in receipt["counterfactuals"]] == ["TAKE_NOW", "DEFER"]


def test_r26_known_cost_and_route_but_infeasible_window_cannot_enable_defer():
    receipt = decision_sensitivity_receipt(
        [_window("w1", "place-a", "GROUNDED_INFEASIBLE", 700), _window("w2", "place-b", "GROUNDED_FEASIBLE", 800)],
    )
    assert receipt["decision_sensitive"] is False
    assert receipt["reason"] == "INSUFFICIENT_GROUNDED_FEASIBLE_OPTIONS"


def test_r26_explicit_preference_suppresses_question_and_fingerprint_is_epoch_bound():
    windows = [_window("w1", "place-a", "GROUNDED_FEASIBLE", 700), _window("w2", "place-b", "GROUNDED_FEASIBLE", 800)]
    receipt = decision_sensitivity_receipt(windows, current_mission_preferences={"garden": "positive"})
    assert receipt["decision_sensitive"] is False
    first = clarification_epoch_fingerprint(
        journey_id="j", active_plan_version_id="p1", current_activity_id="a1", windows=windows
    )
    second = clarification_epoch_fingerprint(
        journey_id="j", active_plan_version_id="p2", current_activity_id="a1", windows=windows
    )
    assert first != second


def test_r26_epoch_changes_when_mobility_truth_changes_even_if_status_stays_feasible():
    windows = [_window("w1", "place-a", "GROUNDED_FEASIBLE", 700), _window("w2", "place-b", "GROUNDED_FEASIBLE", 800)]
    windows[0]["planning_mobility"] = {
        "state": "FROZEN_PLAN_EVIDENCE",
        "provider": "valhalla-http",
        "minutes": 18,
        "fact_quality": "FROZEN_PLAN_EVIDENCE",
    }
    changed = [dict(item) for item in windows]
    changed[0]["planning_mobility"] = {
        "state": "CURRENT_LIVE_EVIDENCE",
        "provider": "valhalla-http",
        "minutes": 31,
        "fact_quality": "LIVE_PROVIDER_CAPTURE",
    }
    first = clarification_epoch_fingerprint(
        journey_id="j", active_plan_version_id="p1", current_activity_id="a1", windows=windows
    )
    second = clarification_epoch_fingerprint(
        journey_id="j", active_plan_version_id="p1", current_activity_id="a1", windows=changed
    )
    assert first != second


def test_r26_defer_without_server_mutation_is_not_resolved():
    steps = [{
        "tool_name": "conclude_recovery",
        "status": "COMPLETED",
        "result": {"requested_outcome": "NO_ACTION", "horizon_decision": "DEFER"},
    }]
    receipt = _server_completion_contract(steps, run_status="COMPLETED")
    assert receipt["kind"] == "UNRESOLVED"
    assert receipt["reason_code"] == "DEFER_WITHOUT_EXECUTION_EFFECT"


def test_r26_clarified_skip_is_a_machine_mutation():
    steps = [
        {
            "tool_name": "apply_clarified_horizon_choice",
            "status": "COMPLETED",
            "result": {
                "executed": True,
                "mutation": "ACTIVITY_SKIPPED",
                "reason": "CLARIFIED_DEFER_FOR_LATER_OPTION",
            },
        },
        {
            "tool_name": "conclude_recovery",
            "status": "COMPLETED",
            "result": {"requested_outcome": "MUTATION_EXECUTED"},
        },
    ]
    receipt = _server_completion_contract(steps, run_status="COMPLETED")
    assert receipt == {"kind": "MUTATION_EXECUTED", "source": "server_trace"}


def test_r26_take_now_child_keeps_current_activity_without_clarified_skip():
    import inspect
    from app.agent.runtime import _system_prompt

    prompt = inspect.getsource(_system_prompt)
    assert "TAKE_NOW answer" in prompt
    assert "do not call apply_clarified_horizon_choice" in prompt


def test_r26_take_now_requires_server_answer_not_model_assertion():
    import inspect
    from app.agent.gateway import AgentToolGateway

    source = inspect.getsource(AgentToolGateway.conclude_recovery)
    assert "TAKE_NOW_CLARIFICATION_NOT_AUTHORIZED" in source
    assert "TAKE_NOW_REQUIRES_CLARIFICATION_CHILD" in source


def test_r26_take_now_is_rejected_when_reality_moved_since_answer(monkeypatch):
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import HTTPException
    from app.agent.gateway import AgentToolGateway
    from app.db.models import Journey, JourneyMission
    from app.schemas import AgentToolCall

    journey_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    windows = [_window("w1", "place-a", "GROUNDED_FEASIBLE", 700), _window("w2", "place-b", "GROUNDED_FEASIBLE", 800)]
    journey = SimpleNamespace(
        id=journey_id,
        active_plan_version_id="plan-1",
        current_activity_id="activity-w1",
    )
    parent = SimpleNamespace(
        id=parent_id,
        status="ANSWERED",
        clarification_json=json.dumps({
            "decision_epoch": clarification_epoch_fingerprint(
                journey_id=str(journey_id),
                active_plan_version_id="plan-1",
                current_activity_id="activity-w1",
                windows=windows,
            ),
            "answer": {"option_key": "TAKE_NOW"},
        }),
    )
    child = SimpleNamespace(
        id=child_id,
        journey_id=journey_id,
        parent_mission_id=parent_id,
        preferences_json=json.dumps({"current_explicit_mission": {"strategy": "TAKE_NOW", "decision_option": "TAKE_NOW"}}),
        resolved_intent_json="{}",
    )

    class FakeSession:
        def get(self, model, key):
            if model is Journey:
                return journey
            if model is JourneyMission:
                return {str(parent_id): parent, str(child_id): child}.get(str(key))
            return None

    moved_twin = {
        "active_plan_version_id": "plan-1",
        "current_activity_id": "activity-w2",
        "context": {"remaining_opportunity_horizon": {"windows": windows}},
    }
    monkeypatch.setattr("app.main._twin_snapshot", lambda session, current: moved_twin)
    request = AgentToolCall(
        request_id="r26-take-now-stale",
        journey_id=journey_id,
        mission_id=child_id,
        outcome="NO_ACTION",
        horizon_decision="TAKE_NOW",
        reason="Keep the current activity.",
    )

    with __import__("pytest").raises(HTTPException, match="CLARIFICATION_STALE_RECOMPUTE_REQUIRED"):
        AgentToolGateway(FakeSession()).conclude_recovery(request)
