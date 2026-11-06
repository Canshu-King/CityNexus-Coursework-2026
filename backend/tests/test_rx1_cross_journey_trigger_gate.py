"""Recovery gate RX-1 — a trigger event may never cross a journey boundary.

Mutant J-CROSS-JOURNEY-TRIGGER (Codex Extreme v2, track J) removed the journey
scope check in ``generate_replan``:

    if trigger_event is None or trigger_event.journey_id != journey.id
 -> if trigger_event is None

and the whole existing suite stayed green, so the guard had no real gate.  The
reason is a vacuous assertion: ``test_replan_trigger_event_must_belong_to_same_
journey`` posts a ``WEATHER`` event that carries no affected activity, and that
request already fails later with ``REPLAN_TARGET_UNRESOLVED`` — which is *also*
HTTP 422.  ``assert response.status_code == 422`` therefore passes whether or not
the scope guard exists: it cannot tell "the guard refused a foreign journey" from
"the replan could not resolve a target".

This gate closes that hole by (a) asserting the guard's own message, and (b)
proving the same event is accepted on its own journey, so the refusal is caused
by the journey boundary and nothing else.

Layer: API integration (real FastAPI routes + real persistence).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.models import Event, ReplanProposal
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)

_GUARD_MESSAGE = "trigger event is not on this journey"


def _traveler_id() -> str:
    return client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]


def _activate(traveler_id: str, title: str, index: int) -> tuple[str, str]:
    """Drive a journey through the real product path to ACTIVE with a plan.

    Returns (journey_id, id of a live activity that a closure can disrupt).
    """
    start = datetime(2026, 10, 10 + index, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    journey_id = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": title,
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=10)).isoformat(),
            "budget": {"amount_minor": 50000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    ).json()["id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    activities = generated.json()[0]["activities"]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{generated.json()[0]['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, activities[-1]["id"]


def _closure_event(journey_id: str, affected_activity_id: str, tag: str) -> str:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": affected_activity_id,
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": tag},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_foreign_trigger_is_refused_by_the_journey_scope_guard() -> None:
    traveler_id = _traveler_id()
    journey_a, activity_a = _activate(traveler_id, "Journey A", 0)
    journey_b, _activity_b = _activate(traveler_id, "Journey B", 1)
    event_a = _closure_event(journey_a, activity_a, "recovery RX-1")

    # Attack: B's own endpoint, A's trigger.  Without the scope guard this walks
    # into the normal generation path and lets B consume A's disruption.
    attack = client.post(
        f"/api/v1/journeys/{journey_b}/replans", json={"trigger_event_id": event_a}
    )
    assert attack.status_code == 422, attack.text
    body = attack.json()
    assert body.get("error", {}).get("message") == _GUARD_MESSAGE, (
        "422 came from somewhere other than the journey scope guard, so this "
        f"gate is not measuring the guard: {body}"
    )

    # A's disruption must still be A's to act on, untouched by the refusal.
    with SessionLocal() as session:
        assert session.get(Event, event_a).status in {"DETECTED", "ACKNOWLEDGED"}
        leaked = session.query(ReplanProposal).filter_by(trigger_event_id=event_a).count()
        assert leaked == 0, "a rejected cross-journey request still created a proposal"

    # Control: the identical event is actionable on its own journey, which proves
    # the refusal above was the journey boundary and not an unresolvable target.
    control = client.post(
        f"/api/v1/journeys/{journey_a}/replans", json={"trigger_event_id": event_a}
    )
    assert control.status_code == 201, control.text


def test_existing_status_code_assertion_would_pass_for_the_wrong_reason() -> None:
    """Pin down *why* the legacy gate was vacuous, so nobody re-simplifies this.

    A WEATHER event with no affected activity is refused with
    REPLAN_TARGET_UNRESOLVED (422) even though it belongs to the journey, i.e.
    ``status_code == 422`` is satisfied with the scope guard deleted.
    """
    traveler_id = _traveler_id()
    journey, _activity = _activate(traveler_id, "Journey C", 0)
    event_id = client.post(
        f"/api/v1/journeys/{journey}/events", json={"event_type": "WEATHER"}
    ).json()["id"]
    response = client.post(
        f"/api/v1/journeys/{journey}/replans", json={"trigger_event_id": event_id}
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] != _GUARD_MESSAGE
    assert "REPLAN_TARGET_UNRESOLVED" in response.text
