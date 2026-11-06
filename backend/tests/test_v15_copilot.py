from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.db.models import Event, JourneyTwin
from app.db.session import SessionLocal
from app.integrations.ai.copilot_contract import (
    classify_copilot_message,
    simulate_what_if,
)
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def test_v15_deterministic_intent_matrix_keeps_hypothetical_separate_from_events():
    cases = {
        "为什么这样安排？": "EXPLAIN_PLAN",
        "如果我延长 60 分钟会怎样？": "WHAT_IF",
        "what if I spend 30 more to walk less?": "WHAT_IF",
        "找一个室内博物馆": "DISCOVER",
        "我希望少走一点": "PREFERENCE_CHANGE",
        "如果下雨就坐公交": "PREFERENCE_CHANGE",
        "现在堵车了": "DISRUPTION_REPORT",
        "前面封路了": "DISRUPTION_REPORT",
        "重新规划下午": "REPLAN_REQUEST",
        "能不能把上午活动去掉，明天十一点再出门？": "REPLAN_REQUEST",
        "我和对象晚上会很晚休息，第二天十一点才起床，上午不要排活动": "REPLAN_REQUEST",
        "tell me something about the trip": "UNKNOWN",
    }
    for message, expected in cases.items():
        assert classify_copilot_message(message).intent == expected, message


def test_v15_what_if_contract_is_read_only_and_preserves_protected_commitments():
    context = {
        "currency": "CNY",
        "current_activity_id": "museum",
        "plan": {
            "id": "plan-1",
            "version_no": 1,
            "status": "SELECTED",
            "cost_minor": 2000,
            "currency": "CNY",
            "activities": [
                {
                    "id": "museum",
                    "title": "Museum",
                    "start_at": "2026-10-10T13:00:00+08:00",
                    "end_at": "2026-10-10T15:00:00+08:00",
                    "locked": False,
                }
            ],
            "legs": [{"walking_min": 15}],
        },
        "commitments": [
            {
                "id": "cruise",
                "activity_id": "cruise",
                "protected": True,
                "scheduled_start_at": "2026-10-10T16:30:00+08:00",
            }
        ],
    }
    result = simulate_what_if("如果我延长 30 分钟会怎样？", context)
    assert result["simulation_only"] is True
    assert result["status"] == "SIMULATED"
    assert result["feasibility"] == "PASS"
    assert result["protected_commitments"]["preserved"] is True
    assert result["next_action"] == "REQUEST_REPLAN"


def test_v15_what_if_skips_locked_current_anchor_for_flexible_activity():
    context = {
        "currency": "CNY",
        "current_activity_id": "arrival",
        "plan": {
            "id": "plan-locked-first",
            "version_no": 1,
            "status": "SELECTED",
            "cost_minor": 2000,
            "currency": "CNY",
            "activities": [
                {
                    "id": "arrival",
                    "title": "Arrival anchor",
                    "start_at": "2026-10-10T11:00:00+08:00",
                    "end_at": "2026-10-10T12:00:00+08:00",
                    "locked": True,
                },
                {
                    "id": "museum",
                    "title": "Museum",
                    "start_at": "2026-10-10T13:00:00+08:00",
                    "end_at": "2026-10-10T15:00:00+08:00",
                    "locked": False,
                },
            ],
            "legs": [{"walking_min": 15}],
        },
        "commitments": [],
    }
    result = simulate_what_if("如果我多待 30 分钟会怎样？", context)
    assert result["status"] == "SIMULATED"
    assert result["affected_activities"][0]["id"] == "museum"


def test_v15_copilot_what_if_does_not_create_twin_or_event():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    with SessionLocal() as session:
        before_events = session.scalar(
            select(func.count()).select_from(Event).where(Event.journey_id == journey_id)
        )
        before_twins = session.scalar(
            select(func.count()).select_from(JourneyTwin).where(JourneyTwin.journey_id == journey_id)
        )
    response = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "如果我延长 60 分钟会怎样？", "locale": "zh"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"]["name"] == "WHAT_IF"
    assert body["what_if"]["simulation_only"] is True
    with SessionLocal() as session:
        after_events = session.scalar(
            select(func.count()).select_from(Event).where(Event.journey_id == journey_id)
        )
        after_twins = session.scalar(
            select(func.count()).select_from(JourneyTwin).where(JourneyTwin.journey_id == journey_id)
        )
    assert after_events == before_events
    assert after_twins == before_twins


def test_v15_what_if_explains_that_authoritative_selection_is_required():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    response = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "如果我多待 60 分钟会怎样？", "locale": "zh"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["what_if"]["status"] == "NOT_SIMULATABLE_FROM_CURRENT_EVIDENCE"
    assert "已选的权威 PlanVersion" in body["message"]


def test_v15_only_current_disruption_creates_event():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    hypothetical = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "如果下雨会怎样？", "locale": "zh"},
    )
    assert hypothetical.status_code == 200
    assert hypothetical.json()["intent"]["name"] == "WHAT_IF"
    assert hypothetical.json()["event"] is None

    report = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "现在堵车了", "locale": "zh"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["intent"]["name"] == "DISRUPTION_REPORT"
    assert body["event"]["event_type"] == "TRAFFIC_DELAY"
    with SessionLocal() as session:
        events = session.scalars(
            select(Event).where(Event.journey_id == journey_id).order_by(Event.created_at)
        ).all()
        assert len(events) == 1
        assert events[0].provider_key == "user-copilot"
        assert events[0].provenance == "USER_REPORTED"


def test_v15_copilot_reports_fallback_mode_without_deepseek_configuration():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    response = client.post(
        f"/api/v1/journeys/{journey_id}/copilot/messages",
        json={"message": "为什么这样安排？", "locale": "en"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "FALLBACK"
    assert response.json()["intent"]["provider"] == "deterministic"
