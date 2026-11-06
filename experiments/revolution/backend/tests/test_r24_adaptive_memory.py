from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.autorun import stop_autorun
from app.db.session import SessionLocal
from app.db.models import Preference, TravelerPreferenceEvidence
from app.execution.traveler_twin import planning_preference_items
from app.replanning.candidate_evidence import verified_interest_preference_fit
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_r24_voluntary_rejection_becomes_bounded_avoidance_evidence() -> None:
    """A real outcome can teach a soft avoidance without changing hard truth."""

    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    traveler_id = ids["traveler_id"]

    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    activity = next(
        item for item in plan["activities"] if not bool(item.get("locked"))
    )
    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/outcomes",
        json={
            "event_type": "USER_REJECTED",
            "activity_id": activity["id"],
            "payload": {
                "voluntary_choice": True,
                "learning_dimension": "interest",
            },
        },
    )
    assert rejected.status_code == 201, rejected.text
    reflected = client.post(f"/api/v1/journeys/{journey_id}/reflect/process")
    assert reflected.status_code == 200, reflected.text

    twin = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin")
    assert twin.status_code == 200, twin.text
    learned = twin.json()["learned_soft_preferences"]
    avoidance = next(item for item in learned if item["direction"].startswith("avoid:"))
    assert avoidance["source"] == "LEARNED_SOFT"
    assert avoidance["evidence_class"] == "VOLUNTARY_CHOICE"
    assert avoidance["effective_strength"] > 0
    assert 0 < avoidance["decay_factor"] <= 1

    with SessionLocal() as session:
        items = planning_preference_items(session, traveler_id, journey_id)
        soft = next(
            item
            for item in items
            if getattr(item, "source", "") == "LEARNED_SOFT"
            and str(getattr(item, "preference_key", "")).startswith("avoid:")
        )
        target = str(soft.preference_key).removeprefix("avoid:")
        assert verified_interest_preference_fit(
            [soft], candidate_tags={"category:other", "other"}
        ) == 1.0
        assert verified_interest_preference_fit(
            [soft], candidate_tags={target, f"category:{target}"}
        ) == 0.0


def test_r24_explicit_interest_overrides_learned_avoidance() -> None:
    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    traveler_id = ids["traveler_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    activity = next(item for item in plan["activities"] if not bool(item.get("locked")))
    assert client.post(
        f"/api/v1/journeys/{journey_id}/outcomes",
        json={
            "event_type": "USER_REJECTED",
            "activity_id": activity["id"],
            "payload": {"voluntary_choice": True, "learning_dimension": "interest"},
        },
    ).status_code == 201
    assert client.post(f"/api/v1/journeys/{journey_id}/reflect/process").status_code == 200

    twin = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin").json()
    learned = next(
        item for item in twin["learned_soft_preferences"] if item["direction"].startswith("avoid:")
    )
    target = learned["direction"].removeprefix("avoid:")
    explicit = client.put(
        f"/api/v1/travelers/{traveler_id}/preferences",
        json=[{"category": "interest", "key": target, "value": True, "weight": 1.0}],
    )
    assert explicit.status_code == 200, explicit.text

    with SessionLocal() as session:
        items = planning_preference_items(session, traveler_id, journey_id)
        assert any(
            str(getattr(item, "source", "")).upper() in {"EXPLICIT", "USER"}
            and getattr(item, "preference_key", "") == target
            for item in items
        )
        assert not any(
            getattr(item, "source", "") == "LEARNED_SOFT"
            and getattr(item, "preference_key", "") in {target, f"avoid:{target}"}
            for item in items
        )

    refreshed = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin").json()
    refreshed_learned = next(
        item
        for item in refreshed["learned_soft_preferences"]
        if item["direction"].startswith("avoid:")
    )
    assert refreshed_learned["override_state"] == "EXPLICIT_WINS"


def test_r24_explicit_avoidance_overrides_learned_positive() -> None:
    """The inverse direction must be strategy-visible, not just UI-visible."""

    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    traveler_id = ids["traveler_id"]
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Preference(
                id=str(uuid4()),
                traveler_id=traveler_id,
                category="interest",
                preference_key="avoid:museum",
                value_json=json.dumps(True),
                weight=1.0,
                source="EXPLICIT",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TravelerPreferenceEvidence(
                id=str(uuid4()),
                traveler_id=traveler_id,
                journey_id=None,
                evidence_type="VOLUNTARY_CHOICE",
                preference_dimension="interest",
                direction="museum",
                strength=0.55,
                confidence=0.7,
                source="VOLUNTARY_OUTCOME",
                evidence_count=1,
                explanation="A previous voluntary choice favored museum.",
                related_ids_json="[]",
                created_at=now,
                expires_at=None,
                status="ACTIVE",
            )
        )
        session.commit()
        items = planning_preference_items(session, traveler_id)
        assert any(
            getattr(item, "preference_key", "") == "avoid:museum"
            and getattr(item, "source", "") == "EXPLICIT"
            for item in items
        )
        assert not any(
            getattr(item, "preference_key", "") == "museum"
            and getattr(item, "source", "") == "LEARNED_SOFT"
            for item in items
        )
    twin = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin").json()
    learned = next(
        item for item in twin["learned_soft_preferences"] if item["direction"] == "museum"
    )
    assert learned["override_state"] == "EXPLICIT_WINS"
