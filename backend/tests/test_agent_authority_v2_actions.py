from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import Activity, Journey
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _selected_plan() -> tuple[str, dict[str, object]]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
    return journey_id, plan


def test_v2_catalog_exposes_typed_actions_without_changing_v1():
    v1 = {item["name"] for item in client.get("/api/v1/agent/tools").json()}
    v2 = {item["name"] for item in client.get("/api/v1/agent/tools/v2").json()}
    assert v1 == {
        "inspect_journey",
        "inspect_twin",
        "list_replans",
        "search_city_services",
        "request_replan",
        "accept_replan",
    }
    assert v2 >= v1 | {
        "replace_activity",
        "reschedule_activity",
        "create_booking",
        "cancel_refundable_booking",
        "request_commitment_change",
    }


def test_v2_reschedule_uses_server_approval_receipt_and_feasibility_gate():
    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if not item["locked"])
    start = datetime.fromisoformat(target["start_at"])
    end = datetime.fromisoformat(target["end_at"])
    request_id = "v2-reschedule-pending-" + str(uuid4())
    pending = client.post(
        "/api/v1/agent/tools/v2/reschedule_activity",
        json={
            "request_id": request_id,
            "journey_id": journey_id,
            "activity_id": target["id"],
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        },
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["executed"] is False
    assert pending.json()["approval_status"] == "PENDING"

    approval_id = pending.json()["approval_id"]
    approved = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert approved.status_code == 200, approved.text

    accepted = client.post(
        "/api/v1/agent/tools/v2/reschedule_activity",
        json={
            "request_id": "v2-reschedule-consume-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "approval_id": approval_id,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["executed"] is True
    with SessionLocal() as session:
        row = session.get(Activity, target["id"])
        assert row is not None
        assert row.start_at == start.isoformat()
        assert session.get(Journey, journey_id) is not None


def test_v2_create_booking_is_exposed_but_requires_receipt_for_protected_activity():
    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    pending = client.post(
        "/api/v1/agent/tools/v2/create_booking",
        json={
            "request_id": "v2-booking-pending-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
        },
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["executed"] is False
    assert pending.json()["decision"] in {"REQUIRE_CONFIRMATION", "REQUIRE_STEP_UP"}
    assert pending.json()["approval_id"]
