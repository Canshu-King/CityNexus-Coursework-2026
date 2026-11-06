from __future__ import annotations

import inspect
from uuid import uuid4

from test_agent_authority_v2_actions import _selected_plan, client


def test_aiv3_01_plain_confirmation_cannot_satisfy_step_up():
    """REQUIRE_STEP_UP must mean more than a caller-chosen approval_method label."""

    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    pending = client.post(
        "/api/v1/agent/tools/v2/create_booking",
        json={
            "request_id": "aiv3-stepup-pending-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
        },
    )
    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["decision"] == "REQUIRE_STEP_UP", body

    approved = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{body['approval_id']}/approve",
        json={"approval_method": "USER_CONFIRMATION"},
    )
    assert approved.status_code in {400, 403, 409, 422}, (
        "plain USER_CONFIRMATION currently upgrades a REQUIRE_STEP_UP receipt"
    )


def test_aiv3_02_v2_mcp_builder_includes_read_plan_and_action_tools():
    """A real inspect -> plan -> act surface must be exposed by one V2 builder."""

    from citynexus_mcp.schemas import V1_TOOL_NAMES, V2_TOOL_NAMES
    from citynexus_mcp.server import build_mcp_v2_server

    source = inspect.getsource(build_mcp_v2_server)
    missing = [name for name in V1_TOOL_NAMES if f'name="{name}"' not in source]
    assert not missing, f"V2 MCP builder is action-only; missing V1 planning tools: {missing}"
    assert all(name in V2_TOOL_NAMES for name in V1_TOOL_NAMES)


def test_aiv3_03_webauthn_label_alone_is_not_proof_of_step_up():
    """The string WEBAUTHN must not be accepted as if an assertion had been verified."""

    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    pending = client.post(
        "/api/v1/agent/tools/v2/create_booking",
        json={
            "request_id": "aiv3-webauthn-pending-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
        },
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["decision"] == "REQUIRE_STEP_UP", pending.text

    fake_step_up = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{pending.json()['approval_id']}/approve",
        json={"approval_method": "WEBAUTHN"},
    )
    assert fake_step_up.status_code in {400, 403, 409, 422}, (
        "the current API accepts the word WEBAUTHN without a verified step-up proof"
    )


def test_aiv3_04_verified_step_up_receipt_can_execute_protected_action(monkeypatch):
    """The stronger boundary is usable when the server verifies real proof."""

    monkeypatch.setenv("CITYNEXUS_DEMO_STEP_UP_CODE", "v3-test-step-up")
    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    pending = client.post(
        "/api/v1/agent/tools/v2/create_booking",
        json={
            "request_id": "aiv3-stepup-success-pending-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
        },
    )
    assert pending.status_code == 200, pending.text
    approval_id = pending.json()["approval_id"]

    approved = client.post(
        f"/api/v1/journeys/{journey_id}/agent/approvals/{approval_id}/approve",
        json={"approval_method": "STEP_UP", "step_up_code": "v3-test-step-up"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    executed = client.post(
        "/api/v1/agent/tools/v2/create_booking",
        json={
            "request_id": "aiv3-stepup-success-consume-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
            "approval_id": approval_id,
        },
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["executed"] is True
