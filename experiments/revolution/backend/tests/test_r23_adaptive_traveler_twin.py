from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.autorun import stop_autorun
from app.agent.mission import compile_mission
from app.agent.runtime import _server_completion_contract
from app.replanning.candidate_evidence import (
    canonical_place_tags,
    verified_interest_preference_breakdown,
)
from app.services import _strategy_place_score
from app.execution.traveler_twin import evidence_decay_factor
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_r23_plain_completion_does_not_become_liking() -> None:
    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    generated = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(
        f"/api/v1/journeys/{ids['journey_id']}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{ids['journey_id']}/start").status_code == 200
    completed = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/activities/{plan['activities'][0]['id']}/complete"
    )
    assert completed.status_code == 200, completed.text
    twin = client.get(f"/api/v1/travelers/{ids['traveler_id']}/traveler-twin")
    assert twin.status_code == 200, twin.text
    assert twin.json()["learned_soft_preferences"] == []


def test_r23_canonical_category_and_tags_are_the_fit_surface() -> None:
    tags = canonical_place_tags(category="Museum", tags=["Culture", "indoor"])
    assert {"museum", "category:museum", "culture", "indoor"} <= tags


def test_r23_receipt_separates_explicit_and_learned_soft_fit() -> None:
    explicit = type(
        "Preference",
        (),
        {
            "category": "interest",
            "preference_key": "museum",
            "value_json": "true",
            "weight": 1.0,
            "source": "EXPLICIT",
        },
    )()
    learned = type(
        "Preference",
        (),
        {
            "category": "interest",
            "preference_key": "garden",
            "value_json": "true",
            "weight": 0.5,
            "source": "LEARNED_SOFT",
        },
    )()
    result = verified_interest_preference_breakdown(
        [explicit, learned], candidate_tags={"museum", "category:museum"}
    )
    assert result["explicit_preference_fit"] == 1.0
    assert result["learned_soft_preference_fit"] == 0.0
    assert result["effective_preference_fit"] is not None


def test_r23_learned_soft_evidence_changes_provider_place_order() -> None:
    """The learned signal changes ranking while hard facts stay untouched."""

    museum = SimpleNamespace(
        id="a-museum",
        name="Museum",
        category="museum",
        tags_json='["culture"]',
        opening_hours_json='{"open":"09:00","close":"17:00"}',
        metadata_json='{"cost_evidence":{"status":"VERIFIED_PROVIDER","amount_minor":500}}',
    )
    garden = SimpleNamespace(
        id="z-garden",
        name="Garden",
        category="garden",
        tags_json='["quiet"]',
        opening_hours_json='{"open":"09:00","close":"17:00"}',
        metadata_json='{"cost_evidence":{"status":"VERIFIED_PROVIDER","amount_minor":500}}',
    )
    explicit: list[SimpleNamespace] = []
    learned = [
        SimpleNamespace(
            category="interest",
            preference_key="garden",
            value_json="true",
            weight=1.0,
            source="LEARNED_SOFT",
        )
    ]

    before = sorted(
        [museum, garden],
        key=lambda place: (-_strategy_place_score(place, "balanced", explicit)[0], place.id),
    )
    after = sorted(
        [museum, garden],
        key=lambda place: (-_strategy_place_score(place, "balanced", learned)[0], place.id),
    )

    assert [place.id for place in before] == ["a-museum", "z-garden"]
    assert [place.id for place in after] == ["z-garden", "a-museum"]
    assert _strategy_place_score(museum, "balanced", explicit)[1] == _strategy_place_score(
        museum, "balanced", learned
    )[1]


def test_r23_failed_replan_does_not_block_typed_no_action() -> None:
    """A failed strategy attempt is not a server-created proposal.

    The Agent may try a candidate, receive a deterministic feasibility refusal, and
    then intentionally choose NO_ACTION.  Completion must be derived from the
    persisted refusal plus the typed conclusion, not from model prose or the mere
    presence of a request_replan tool call.
    """

    trace = [
        {
            "tool_name": "request_replan",
            "status": "TOOL_ERROR",
            "result": {"error": "OPPORTUNITY_CANDIDATES_NONE_FEASIBLE:COST_UNKNOWN"},
        },
        {
            "tool_name": "conclude_recovery",
            "status": "COMPLETED",
            "result": {
                "requested_outcome": "NO_ACTION",
                "reason": "No grounded candidate passed the deterministic gate.",
            },
        },
    ]

    contract = _server_completion_contract(trace, run_status="COMPLETED")

    assert contract == {"kind": "NO_ACTION", "source": "server_trace"}


def test_r23_english_opportunity_bounds_reach_mission_spec(monkeypatch) -> None:
    journey = SimpleNamespace(budget_currency="JPY", active_plan_version_id=None)
    monkeypatch.setattr("app.agent.mission._resolve_protected_activities", lambda *_args, **_kwargs: ())

    spec = compile_mission(
        None,
        journey,
        "Use the free opportunity only if extra cost is at most 1000 JPY and schedule drift is at most 120 minutes.",
        requested_autonomy="EXECUTE_WITHIN_BOUNDS",
    )

    assert spec.max_cost_delta_minor == 1000
    assert spec.max_schedule_drift_min == 120


def test_r24_avoidance_evidence_is_a_real_soft_negative_signal() -> None:
    learned_avoidance = [
        SimpleNamespace(
            category="interest",
            preference_key="avoid:museum",
            value_json="true",
            weight=1.0,
            source="LEARNED_SOFT",
        )
    ]

    assert verified_interest_preference_breakdown(
        learned_avoidance, candidate_tags={"garden", "category:garden"}
    )["effective_preference_fit"] == 1.0
    assert verified_interest_preference_breakdown(
        learned_avoidance, candidate_tags={"museum", "category:museum"}
    )["effective_preference_fit"] == 0.0


def test_r24_learned_evidence_decays_before_hard_expiry() -> None:
    now = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    evidence = SimpleNamespace(
        created_at=(now - timedelta(days=90)).isoformat(),
        expires_at=(now + timedelta(days=90)).isoformat(),
    )

    factor = evidence_decay_factor(evidence, now=now)

    assert 0.49 <= factor <= 0.51
