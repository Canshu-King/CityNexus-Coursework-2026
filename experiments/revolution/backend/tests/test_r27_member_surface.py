from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import app.main as main_module
from app.core.config import settings
from app.traveler_principal import issue_traveler_token
from app.agent.autorun import stop_autorun
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


@pytest.fixture
def _signed_member_secret():
    previous = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r74-signed-member-surface-test-secret-012345")
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous)


def test_r27_member_projection_keeps_owner_and_collaborator_context_separate(_signed_member_secret) -> None:
    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    second = client.post(
        "/api/v1/travelers",
        json={"display_name": "R27 collaborator", "email": None, "locale": "en-US", "timezone": "UTC"},
    ).json()
    journey_id = ids["journey_id"]
    owner_headers = {"Authorization": f"Bearer {issue_traveler_token(ids['traveler_id'])}"}
    plans = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    assert plans.status_code == 201, plans.text
    plan_id = plans.json()[0]["id"]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select", headers=owner_headers
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start", headers=owner_headers).status_code == 200
    offered = client.post(
        f"/api/v1/journeys/{journey_id}/membership-invitations",
        json={"target_traveler_id": second["id"]},
        headers={**owner_headers, "Idempotency-Key": "r27-membership-invite"},
    )
    assert offered.status_code == 201, offered.text
    accepted = client.post(
        f"/api/v1/journey-membership-invitations/{offered.json()['invitation_id']}/respond",
        json={"decision": "ACCEPT"},
        headers={"Authorization": f"Bearer {issue_traveler_token(second['id'])}"},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()["membership"]
    preferences = client.put(
        f"/api/v1/journeys/{journey_id}/members/{member['id']}/preferences",
        json={"preferences": [{"category": "interest", "key": "craft", "value": True, "weight": 1.0}]},
        headers={"Authorization": f"Bearer {issue_traveler_token(second['id'])}"},
    )
    assert preferences.status_code == 200, preferences.text

    response = client.post(
        "/api/v1/agent/tools/v2/inspect_journey",
        json={"request_id": "r27-member-surface-001", "journey_id": ids["journey_id"]},
    )
    assert response.status_code == 200, response.text
    members = response.json()["collective_journey"]["members"]
    assert {item["traveler_id"] for item in members} == {ids["traveler_id"], second["id"]}
    by_traveler = {item["traveler_id"]: item for item in members}
    assert by_traveler[ids["traveler_id"]]["role"] == "OWNER"
    assert by_traveler[second["id"]]["role"] == "COLLABORATOR"
    assert by_traveler[second["id"]]["journey_scoped_explicit_preferences"] == [
        {"category": "interest", "key": "craft", "value": True, "weight": 1.0}
    ]
    assert by_traveler[ids["traveler_id"]]["journey_scoped_explicit_preferences"] == []
    assert by_traveler[ids["traveler_id"]]["capabilities"] != by_traveler[second["id"]]["capabilities"]

    withdrawn = client.post(
        f"/api/v1/journeys/{journey_id}/members/{member['id']}/withdraw",
        headers={"Authorization": f"Bearer {issue_traveler_token(ids['traveler_id'])}"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    response = client.post(
        "/api/v1/agent/tools/v2/inspect_journey",
        json={"request_id": "r27-member-surface-002", "journey_id": ids["journey_id"]},
    )
    assert [item["traveler_id"] for item in response.json()["collective_journey"]["members"]] == [
        ids["traveler_id"]
    ]


def test_r74_membership_mutations_require_signed_manager_and_server_bound_inviter(_signed_member_secret) -> None:
    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    owner_id = ids["traveler_id"]
    target = client.post(
        "/api/v1/travelers",
        json={"display_name": "R74 target", "email": None, "locale": "en-US", "timezone": "UTC"},
    ).json()
    stranger = client.post(
        "/api/v1/travelers",
        json={"display_name": "R74 stranger", "email": None, "locale": "en-US", "timezone": "UTC"},
    ).json()
    add_url = f"/api/v1/journeys/{journey_id}/members"
    payload = {"traveler_id": target["id"], "role": "COLLABORATOR", "invited_by": owner_id}

    unsigned = client.post(add_url, json=payload)
    assert unsigned.status_code in {401, 403}, unsigned.text
    foreign = client.post(
        add_url,
        json={**payload, "invited_by": stranger["id"]},
        headers={"Authorization": f"Bearer {issue_traveler_token(stranger['id'])}"},
    )
    assert foreign.status_code in {401, 403}, foreign.text
    fake_inviter = client.post(
        add_url,
        json={**payload, "invited_by": stranger["id"]},
        headers={"Authorization": f"Bearer {issue_traveler_token(owner_id)}"},
    )
    assert fake_inviter.status_code == 403, fake_inviter.text
    authenticated_manager_bypass = client.post(
        add_url,
        json=payload,
        headers={"Authorization": f"Bearer {issue_traveler_token(owner_id)}"},
    )
    assert authenticated_manager_bypass.status_code == 409
    assert "RECIPIENT_ACCEPTANCE_REQUIRED" in authenticated_manager_bypass.text
    self_owner = client.post(
        add_url,
        json={"traveler_id": stranger["id"], "role": "OWNER", "invited_by": stranger["id"]},
        headers={"Authorization": f"Bearer {issue_traveler_token(stranger['id'])}"},
    )
    assert self_owner.status_code in {401, 403}, self_owner.text

    unsigned_withdraw = client.post(
        f"{add_url}/not-a-member/withdraw"
    )
    assert unsigned_withdraw.status_code in {401, 403}, unsigned_withdraw.text
    members = client.get(
        add_url,
        headers={"Authorization": f"Bearer {issue_traveler_token(owner_id)}"},
    )
    assert members.status_code == 200, members.text
    assert {item["traveler_id"] for item in members.json()} == {owner_id}
