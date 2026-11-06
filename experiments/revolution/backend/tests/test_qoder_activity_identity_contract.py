"""Activity identity contract across a replan (pass-16 C).

Measured in the field, not theorised: pass-15 found that after an accepted
recovery, `GET /api/v1/journeys/{id}/experience` reported `place_id: null` for
**every** activity while `GET /api/v1/plans/{id}` for the very same child plan
version still had all six.  The identity was not lost when the child plan was
created — it was dropped by the consumer projection, which left the phone with
titles as its only handle.  A title is localisable, renameable, can collide, and
is not a key, so "is the protected commitment still here?" silently degraded into
a string match.

Contract this pins:

* every projected activity exposes a stable `place_id`;
* an activity the recovery did not touch keeps the *same* `place_id` it had on the
  plan the traveller selected;
* a replacement activity is not left anonymous — it exposes its real `place_id`;
* the canonical plan projection and the /experience projection agree, because they
  describe the same rows.

Fixing this in the frontend by looking a title up in a place table is explicitly
not acceptable, and this test is the reason: it compares the two server
projections, so a client-side patch would leave it red.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _activities(payload: dict) -> list[dict]:
    return payload.get("activities") or []


def _by_place(activities: list[dict]) -> dict[str, str]:
    return {a["id"]: a.get("place_id") for a in activities}


def test_experience_and_canonical_plan_agree_on_activity_identity() -> None:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]

    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    assert plans, "the deterministic generator produced no plan"
    selected = plans[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{selected['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    selected_by_place = {a["place_id"]: a["id"] for a in _activities(selected)}
    assert "place-river-cruise" in selected_by_place, "the fixture lost its protected place"
    closed_activity_id = selected_by_place["place-tech-art"]

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": closed_activity_id,
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "identity contract regression"},
        },
    )
    assert event.status_code == 201, event.text
    event_id = event.json()["id"]

    requested = client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"identity-{event_id}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event_id,
            "candidate_count": 3,
        },
    )
    assert requested.status_code == 200, requested.text

    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    presented = [
        item
        for item in proposals
        if item.get("trigger_event_id") == event_id and item.get("status") == "PRESENTED"
    ]
    assert presented, "the closure produced no proposal to accept"
    proposal = presented[0]

    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"identity-accept-{proposal['id']}"},
    )
    assert accepted.status_code == 200, accepted.text

    child_id = proposal["proposed_plan_version_id"]
    canonical = client.get(f"/api/v1/plans/{child_id}").json()
    experience = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]

    assert experience["id"] == child_id, "/experience is projecting a different plan"

    canonical_activities = _activities(canonical)
    experience_activities = _activities(experience)

    # 1. nothing anonymous in either projection.
    anonymous = [a["id"] for a in experience_activities if not a.get("place_id")]
    assert not anonymous, (
        "/experience dropped place_id for these activities, leaving titles as the only "
        f"identity a consumer can match on: {anonymous}"
    )
    assert not [a["id"] for a in canonical_activities if not a.get("place_id")]

    # 2. the two server views of the same rows agree.
    assert _by_place(experience_activities) == _by_place(canonical_activities), (
        "the canonical plan and the consumer projection disagree about which place an "
        "activity is; one of them is lying to the phone"
    )

    # 3. an untouched place keeps its identity across the replan.  Matched by
    #    place_id, not activity id: an accepted recovery writes a child
    #    PlanVersion whose activity rows are new objects, which is exactly why
    #    stable place identity is the contract and activity id is not.
    canonical_places = {a["place_id"] for a in canonical_activities}
    for place_id in selected_by_place:
        if place_id == "place-tech-art":
            continue  # this is the one the recovery is allowed to replace
        assert place_id in canonical_places, (
            f"{place_id} vanished from the accepted plan; it held "
            f"{sorted(canonical_places)}"
        )

    # 4. the protected commitment is still identifiable by key, not by name.
    cruise = [a for a in experience_activities if a.get("place_id") == "place-river-cruise"]
    assert len(cruise) == 1, "the River Cruise commitment cannot be found by place_id"


def test_replacement_activity_is_not_left_anonymous() -> None:
    """The point of a recovery is a *different* place, so the new row must carry a
    real identity too — otherwise the surface can only say "something was
    substituted here"."""
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    selected = plans[0]
    client.post(f"/api/v1/journeys/{journey_id}/plans/{selected['id']}/select")
    client.post(f"/api/v1/journeys/{journey_id}/start")

    original_ids = {a["id"] for a in _activities(selected)}
    closed = next(a for a in _activities(selected) if a["place_id"] == "place-tech-art")

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": closed["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "replacement identity"},
        },
    ).json()
    client.post(
        "/api/v1/agent/tools/request_replan",
        json={
            "request_id": f"replacement-{event['id']}",
            "agent_id": "citynexus-local-agent",
            "journey_id": journey_id,
            "trigger_event_id": event["id"],
            "candidate_count": 3,
        },
    )
    proposals = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    proposal = next(
        item
        for item in proposals
        if item.get("trigger_event_id") == event["id"] and item.get("status") == "PRESENTED"
    )
    client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"replacement-accept-{proposal['id']}"},
    )

    activities = _activities(
        client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]
    )
    replacements = [a for a in activities if a["id"] not in original_ids]
    assert replacements, "the accepted recovery introduced no new activity to check"
    for item in replacements:
        assert item.get("place_id"), (
            f"the substituted activity {item['title']!r} has no place_id, so the surface "
            "can only describe it by name"
        )
